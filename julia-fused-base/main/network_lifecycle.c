/**
 * @file network_lifecycle.c
 * @brief Wi-Fi station lifecycle that never makes application startup depend on AP availability.
 *
 * 本模块只负责三件事：
 * 1. Wi-Fi station 初始化与永久重连（有界指数退避 + 负向抖动）；
 * 2. IP_EVENT_STA_GOT_IP 后按注册顺序调用 IP 就绪启动回调；
 * 3. 回调失败时按独立的有界退避计数重试，直到成功或 IP 失效。
 *
 * 回调是不透明的业务启动入口（MQTT、WSS 语音服务等），由各业务模块在
 * network_lifecycle_start() 之前注册；本模块不知道也不需要知道它们的语义。
 */

#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <limits.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_wifi_default.h"

#include "network_lifecycle.h"
#include "protocol_examples_common.h"

static const char *TAG = "network_lifecycle";

/**
 * @brief 一个已注册的 IP 就绪服务启动槽位。
 *
 * started_ok 表示该回调已成功返回（本 IP 会话内不再重试）；retry_us 为下次
 * 重试截止时间，INT64_MAX 表示立即/尚未调度。attempt 是该回调独立的退避计数，
 * 不同服务之间不会互相吃掉退避档位。
 */
typedef struct {
    network_ip_ready_cb_t callback;
    void *arg;
    bool started_ok;
    int64_t retry_us;
    uint32_t attempt;
} network_service_slot_t;

/* The Wi-Fi retry worker and all event registrations are process-lifetime singletons. */
static TaskHandle_t s_network_task;
static esp_netif_t *s_wifi_netif;
static esp_event_handler_instance_t s_wifi_start_handler;
static esp_event_handler_instance_t s_wifi_disconnect_handler;
static esp_event_handler_instance_t s_got_ip_handler;
static portMUX_TYPE s_state_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_started;
static bool s_wifi_driver_started;
static bool s_ip_ready;
static uint32_t s_retry_attempt;
static int64_t s_next_retry_us = INT64_MAX;

/** IP 就绪服务启动回调注册表；注册只发生在 network_lifecycle_start() 之前。 */
static network_service_slot_t s_slots[NETWORK_MAX_IP_READY_CALLBACKS];
static size_t s_slot_count;

static void network_lifecycle_cleanup(void)
{
    if (s_got_ip_handler != NULL) {
        (void)esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                    s_got_ip_handler);
        s_got_ip_handler = NULL;
    }
    if (s_wifi_disconnect_handler != NULL) {
        (void)esp_event_handler_instance_unregister(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
                                                    s_wifi_disconnect_handler);
        s_wifi_disconnect_handler = NULL;
    }
    if (s_wifi_start_handler != NULL) {
        (void)esp_event_handler_instance_unregister(WIFI_EVENT, WIFI_EVENT_STA_START,
                                                    s_wifi_start_handler);
        s_wifi_start_handler = NULL;
    }
    if (s_network_task != NULL) {
        vTaskDelete(s_network_task);
        s_network_task = NULL;
    }
    if (s_wifi_driver_started) {
        (void)esp_wifi_stop();
        s_wifi_driver_started = false;
    }
    (void)esp_wifi_deinit();
    if (s_wifi_netif != NULL) {
        esp_netif_destroy_default_wifi(s_wifi_netif);
        s_wifi_netif = NULL;
    }
    portENTER_CRITICAL(&s_state_lock);
    s_started = false;
    s_ip_ready = false;
    s_retry_attempt = 0;
    s_next_retry_us = INT64_MAX;
    for (size_t i = 0; i < s_slot_count; i++) {
        s_slots[i].started_ok = false;
        s_slots[i].retry_us = INT64_MAX;
        s_slots[i].attempt = 0;
    }
    portEXIT_CRITICAL(&s_state_lock);
}

/**
 * Computes a bounded exponential delay with negative-only jitter.  Keeping the
 * jitter below the cap means CONFIG_NETWORK_WIFI_RETRY_MAX_MS is a real upper
 * bound even at the final retry level.  The attempt counter belongs to the
 * caller so Wi-Fi and every service callback never steal each other's backoff.
 */
static uint32_t network_backoff_delay_ms(uint32_t *attempt)
{
    uint32_t base_ms = CONFIG_NETWORK_WIFI_RETRY_BASE_MS;
    uint32_t max_ms = CONFIG_NETWORK_WIFI_RETRY_MAX_MS;
    if (max_ms < base_ms) {
        max_ms = base_ms;
    }

    uint32_t delay_ms = base_ms;
    /* Once the cap is reached, later retries must remain O(1) rather than
     * looping once per historical outage attempt.  31 shifts covers every
     * supported base/max ratio with ample margin. */
    uint32_t shifts = *attempt > 31U ? 31U : *attempt;
    while (shifts-- > 0U && delay_ms < max_ms) {
        if (delay_ms > max_ms / 2U) {
            delay_ms = max_ms;
        } else {
            delay_ms *= 2U;
        }
    }
    if (*attempt != UINT32_MAX) {
        (*attempt)++;
    }

#if CONFIG_NETWORK_WIFI_RETRY_JITTER_PERCENT > 0
    uint32_t jitter_range = (delay_ms * CONFIG_NETWORK_WIFI_RETRY_JITTER_PERCENT) / 100U;
    if (jitter_range > 0U) {
        delay_ms -= esp_random() % (jitter_range + 1U);
    }
#endif
    return delay_ms;
}

static uint32_t network_next_retry_delay_ms(void)
{
    return network_backoff_delay_ms(&s_retry_attempt);
}

/* Must be called with s_state_lock held. */
static uint32_t network_schedule_retry_locked(void)
{
    uint32_t delay_ms = network_next_retry_delay_ms();
    s_next_retry_us = esp_timer_get_time() + (int64_t)delay_ms * 1000LL;
    return delay_ms;
}

/**
 * @brief 把全部服务槽位重置为"待启动"状态。
 *
 * 每次新的 GOT_IP 都重新触发全部回调并清零各自退避计数，与"每个 IP 会话启动
 * 一次服务"的语义一致。必须持 s_state_lock 调用。
 */
static void network_reset_service_slots_locked(void)
{
    for (size_t i = 0; i < s_slot_count; i++) {
        s_slots[i].started_ok = false;
        s_slots[i].retry_us = INT64_MAX;
        s_slots[i].attempt = 0;
    }
}

static void network_wifi_event_handler(void *arg, esp_event_base_t event_base,
                                       int32_t event_id, void *event_data)
{
    (void)arg;
    (void)event_base;

    if (event_id == WIFI_EVENT_STA_START) {
        portENTER_CRITICAL(&s_state_lock);
        s_next_retry_us = esp_timer_get_time();
        portEXIT_CRITICAL(&s_state_lock);
        xTaskNotifyGive(s_network_task);
        return;
    }

    if (event_id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *disconnected = event_data;
        int reason = disconnected != NULL ? disconnected->reason : -1;
        uint32_t delay_ms;
        uint32_t retry_attempt;
        portENTER_CRITICAL(&s_state_lock);
        s_ip_ready = false;
        /* Every disconnect closes the previous association attempt. Schedule
         * the next one through the capped backoff, whether the failed attempt
         * was initiated at startup or after an online connection. */
        delay_ms = network_schedule_retry_locked();
        retry_attempt = s_retry_attempt;
        portEXIT_CRITICAL(&s_state_lock);
        ESP_LOGW(TAG, "Wi-Fi disconnected (reason=%d); retry #%" PRIu32 " in %" PRIu32 " ms",
                 reason, retry_attempt, delay_ms);
        xTaskNotifyGive(s_network_task);
    }
}

static void network_got_ip_handler(void *arg, esp_event_base_t event_base,
                                   int32_t event_id, void *event_data)
{
    (void)arg;
    (void)event_base;
    (void)event_id;

    const ip_event_got_ip_t *got_ip = event_data;
    if (got_ip == NULL || got_ip->esp_netif != s_wifi_netif) {
        return;
    }

    portENTER_CRITICAL(&s_state_lock);
    s_ip_ready = true;
    s_retry_attempt = 0;
    s_next_retry_us = INT64_MAX;
    network_reset_service_slots_locked();
    portEXIT_CRITICAL(&s_state_lock);
    ESP_LOGI(TAG, "Wi-Fi has IPv4 address " IPSTR "; dispatching registered services",
             IP2STR(&got_ip->ip_info.ip));
    xTaskNotifyGive(s_network_task);
}

/**
 * @brief 计算最近一次需要唤醒的截止时间（微秒）。
 *
 * 服务重试与 Wi-Fi 重连共用同一次阻塞等待；任一时间点到期都会唤醒。
 *
 * @param[in] next_retry_us     下一次 Wi-Fi 重连截止时间。
 * @param[in] service_retry_us  最近的服务回调重试截止时间；无则为 INT64_MAX。
 * @return FreeRTOS 等待 tick；无任何截止时间时为 portMAX_DELAY。
 */
static TickType_t network_wait_ticks_until(int64_t next_retry_us, int64_t service_retry_us)
{
    int64_t deadline_us = next_retry_us < service_retry_us ? next_retry_us : service_retry_us;
    if (deadline_us == INT64_MAX) {
        return portMAX_DELAY;
    }
    int64_t remaining_us = deadline_us - esp_timer_get_time();
    if (remaining_us <= 0) {
        return 0;
    }
    uint64_t remaining_ms = ((uint64_t)remaining_us + 999ULL) / 1000ULL;
    TickType_t ticks = pdMS_TO_TICKS(remaining_ms);
    return ticks == 0 ? 1 : ticks;
}

/**
 * @brief 在 IP 就绪窗口内推进所有服务启动槽位。
 *
 * 对每个"到期且未成功"的槽位调用其回调：成功标记完成，失败按独立退避调度
 * 下一次重试。回调在生命周期任务上下文中执行（不在中断/事件回调中）。
 *
 * @return true 本轮至少调用了一个回调；false 没有可调用的回调。
 */
static bool network_dispatch_service_slots(void)
{
    bool invoked_any = false;
    for (size_t i = 0; i < s_slot_count; i++) {
        bool invoke = false;
        network_ip_ready_cb_t callback;
        void *arg;
        portENTER_CRITICAL(&s_state_lock);
        if (!s_slots[i].started_ok &&
            (s_slots[i].retry_us == INT64_MAX ||
             esp_timer_get_time() >= s_slots[i].retry_us)) {
            /* 先清截止时间：失败路径会重新调度，成功路径不再需要。 */
            s_slots[i].retry_us = INT64_MAX;
            invoke = true;
            callback = s_slots[i].callback;
            arg = s_slots[i].arg;
        }
        portEXIT_CRITICAL(&s_state_lock);
        if (!invoke) {
            continue;
        }
        invoked_any = true;

        esp_err_t err = callback(arg);
        portENTER_CRITICAL(&s_state_lock);
        if (err == ESP_OK) {
            s_slots[i].started_ok = true;
            s_slots[i].attempt = 0;
            portEXIT_CRITICAL(&s_state_lock);
            ESP_LOGI(TAG, "Registered service %u started", (unsigned)i);
        } else {
            uint32_t delay_ms = network_backoff_delay_ms(&s_slots[i].attempt);
            s_slots[i].retry_us = esp_timer_get_time() + (int64_t)delay_ms * 1000LL;
            portEXIT_CRITICAL(&s_state_lock);
            ESP_LOGW(TAG, "Registered service %u start failed: %s; retry in %" PRIu32 " ms",
                     (unsigned)i, esp_err_to_name(err), delay_ms);
        }
    }
    return invoked_any;
}

/**
 * @brief 计算服务槽位最近的重试截止时间（微秒）。
 *
 * @param[in] ip_ready 当前是否持有 IP；无 IP 时返回 INT64_MAX，槽位不参与等待。
 * @return 最近的槽位重试截止时间；无待重试槽位时为 INT64_MAX。
 */
static int64_t network_service_retry_deadline(bool ip_ready)
{
    int64_t nearest_us = INT64_MAX;
    if (!ip_ready) {
        return nearest_us;
    }
    portENTER_CRITICAL(&s_state_lock);
    for (size_t i = 0; i < s_slot_count; i++) {
        if (!s_slots[i].started_ok && s_slots[i].retry_us != INT64_MAX &&
            s_slots[i].retry_us < nearest_us) {
            nearest_us = s_slots[i].retry_us;
        }
    }
    portEXIT_CRITICAL(&s_state_lock);
    return nearest_us;
}

static void network_lifecycle_task(void *parameter)
{
    (void)parameter;

    while (true) {
        bool ip_ready;
        int64_t next_retry_us;
        int64_t service_retry_us;
        portENTER_CRITICAL(&s_state_lock);
        ip_ready = s_ip_ready;
        next_retry_us = s_next_retry_us;
        portEXIT_CRITICAL(&s_state_lock);

        if (ip_ready) {
            if (network_dispatch_service_slots()) {
                /* 回调可能重新调度了自己或后续槽位；立即回到循环重算截止时间。 */
                continue;
            }
        }
        service_retry_us = network_service_retry_deadline(ip_ready);

        if (!ip_ready && next_retry_us != INT64_MAX &&
            esp_timer_get_time() >= next_retry_us) {
            esp_err_t err = esp_wifi_connect();
            if (err == ESP_OK || err == ESP_ERR_WIFI_STATE) {
                /* A completed association failure emits STA_DISCONNECTED, which
                 * schedules the next backoff slot.  Do not issue a second
                 * connect while this one is in flight. */
                portENTER_CRITICAL(&s_state_lock);
                s_next_retry_us = INT64_MAX;
                portEXIT_CRITICAL(&s_state_lock);
            } else {
                ESP_LOGW(TAG, "esp_wifi_connect failed: %s", esp_err_to_name(err));
                portENTER_CRITICAL(&s_state_lock);
                (void)network_schedule_retry_locked();
                portEXIT_CRITICAL(&s_state_lock);
            }
            continue;
        }

        (void)ulTaskNotifyTake(pdTRUE,
                               network_wait_ticks_until(next_retry_us, service_retry_us));
    }
}

esp_err_t network_lifecycle_register_ip_ready(network_ip_ready_cb_t callback, void *arg)
{
    if (callback == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t result = ESP_ERR_NO_MEM;
    portENTER_CRITICAL(&s_state_lock);
    if (s_started) {
        result = ESP_ERR_INVALID_STATE;
    } else if (s_slot_count < NETWORK_MAX_IP_READY_CALLBACKS) {
        s_slots[s_slot_count].callback = callback;
        s_slots[s_slot_count].arg = arg;
        s_slots[s_slot_count].started_ok = false;
        s_slots[s_slot_count].retry_us = INT64_MAX;
        s_slots[s_slot_count].attempt = 0;
        s_slot_count++;
        result = ESP_OK;
    }
    portEXIT_CRITICAL(&s_state_lock);
    return result;
}

esp_err_t network_lifecycle_start(void)
{
#if !CONFIG_EXAMPLE_CONNECT_WIFI
    ESP_LOGW(TAG, "Wi-Fi lifecycle is disabled by configuration");
    return ESP_ERR_NOT_SUPPORTED;
#else
    portENTER_CRITICAL(&s_state_lock);
    bool already_started = s_started;
    if (!already_started) {
        s_started = true;
    }
    portEXIT_CRITICAL(&s_state_lock);
    if (already_started) {
        return ESP_OK;
    }

    wifi_init_config_t wifi_init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_err_t err = esp_wifi_init(&wifi_init_cfg);
    if (err != ESP_OK) {
        goto failed;
    }
    s_wifi_netif = esp_netif_create_default_wifi_sta();
    if (s_wifi_netif == NULL) {
        err = ESP_ERR_NO_MEM;
        goto failed;
    }

    err = esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_STA_START,
                                              network_wifi_event_handler, NULL,
                                              &s_wifi_start_handler);
    if (err != ESP_OK) {
        goto failed;
    }
    err = esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
                                              network_wifi_event_handler, NULL,
                                              &s_wifi_disconnect_handler);
    if (err != ESP_OK) {
        goto failed;
    }
    err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                              network_got_ip_handler, NULL,
                                              &s_got_ip_handler);
    if (err != ESP_OK) {
        goto failed;
    }

    wifi_config_t wifi_cfg = {
        .sta = {
#if !CONFIG_EXAMPLE_WIFI_SSID_PWD_FROM_STDIN
            .ssid = CONFIG_EXAMPLE_WIFI_SSID,
            .password = CONFIG_EXAMPLE_WIFI_PASSWORD,
#endif
            .scan_method = EXAMPLE_WIFI_SCAN_METHOD,
            .sort_method = EXAMPLE_WIFI_CONNECT_AP_SORT_METHOD,
            .threshold.rssi = CONFIG_EXAMPLE_WIFI_SCAN_RSSI_THRESHOLD,
            .threshold.authmode = EXAMPLE_WIFI_SCAN_AUTH_MODE_THRESHOLD,
        },
    };
    err = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (err == ESP_OK) {
        err = esp_wifi_set_mode(WIFI_MODE_STA);
    }
    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
    }
    if (err == ESP_OK) {
        err = esp_wifi_set_ps(WIFI_PS_NONE);
    }
    if (err != ESP_OK) {
        goto failed;
    }

    if (xTaskCreate(network_lifecycle_task, "network_lifecycle", 4096, NULL, 4,
                    &s_network_task) != pdPASS) {
        err = ESP_ERR_NO_MEM;
        goto failed;
    }
    err = esp_wifi_start();
    if (err != ESP_OK) {
        goto failed;
    }
    s_wifi_driver_started = true;
    ESP_LOGI(TAG, "Wi-Fi lifecycle started; local application continues while offline");
    return ESP_OK;

failed:
    /* Startup failures are logged to the caller and do not abort app_main.
     * Tear down the partial singleton so a deliberate later call can retry
     * initialization without leaking a task, netif, or event registration. */
    ESP_LOGE(TAG, "Unable to start Wi-Fi lifecycle: %s", esp_err_to_name(err));
    network_lifecycle_cleanup();
    return err;
#endif
}
