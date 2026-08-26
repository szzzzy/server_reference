/**
 * @file    mqtt_comm.c
 * @brief   基于 ESP-MQTT 的通信层：连接管理、topic 注册表、分片重组与主动 OTA 检查。
 *
 * 主要职责：
 * 1. 使用官方 `esp_mqtt_client_*` 接口建立并保持 MQTT 连接；
 * 2. 订阅所有已注册的 topic（注册表由各业务模块在启动前填写），并按注册上限
 *    重组可能分片的消息后路由给对应 handler；
 * 3. 连接就绪后立即上报设备身份、硬件版本和当前固件版本，之后按配置周期检查；
 * 4. 提供通用 mqtt_comm_publish()（QoS 1 尽力而为）供辅助状态上报。
 *
 * 本模块不感知任何具体业务：不解析语音命令、不解析音频清单、不下载固件、不写 Flash。
 */

#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/param.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "mqtt_client.h"

#include "mqtt_comm.h"
#include "native_ota_example.h"
#include "ota_report.h"

/** 本模块统一使用的日志标签。 */
static const char *TAG = "mqtt_comm";

/** MQTT topic 的本地最大长度，包含前缀、设备 ID 和末尾 NUL。 */
#define MQTT_TOPIC_SIZE 192
/** ota_notify 只允许轻量 JSON，避免通知通道成为无界输入入口。 */
#define MQTT_OTA_NOTIFY_MAX_LEN 512
/** 表示 MQTT 已连接且全部 critical 订阅均已确认。 */
#define MQTT_OTA_READY_BIT BIT0
/** A validated response for the check currently being awaited by ota_check_task. */
#define MQTT_OTA_RESPONSE_BIT BIT1
/** A structurally invalid response should trigger bounded retry without waiting for timeout. */
#define MQTT_OTA_RESPONSE_REJECTED_BIT BIT2
/** 用于记录已交给 ESP-MQTT 的关键状态事件，等待对应 QoS 1 PUBACK。 */
#define MQTT_STATUS_TRACK_SIZE (CONFIG_OTA_REPORT_QUEUE_DEPTH + 4)

/** 已注册下行 topic 的最大数量；OTA 占 2 个，语音命令占 1 个，剩余供扩展。 */
#define MQTT_MAX_REGISTERED_TOPICS 8

/**
 * @brief 一条已注册的下行 topic。
 *
 * topic 为注册时的深拷贝；sub_msg_id 跟踪当前会话该 topic 的订阅确认，
 * -1 表示无待确认订阅。critical 为 true 的 topic 全部确认后通信层才置
 * MQTT_OTA_READY_BIT 放行检查任务与状态 flush。
 */
typedef struct {
    char topic[MQTT_TOPIC_SIZE]; /**< 订阅 topic，含末尾 NUL。 */
    size_t max_payload_len; /**< 单条消息最大长度（不含末尾 NUL）。 */
    bool critical; /**< 是否计入连接就绪判定。 */
    mqtt_inbound_handler_t handler; /**< 完整载荷处理回调。 */
    int sub_msg_id; /**< 当前会话待确认的 SUBACK 消息 ID；-1 表示无。 */
} mqtt_registered_topic_t;

/** 下行 topic 注册表；注册只发生在 mqtt_comm_start() 之前，之后只读。 */
static mqtt_registered_topic_t s_registered_topics[MQTT_MAX_REGISTERED_TOPICS];
static size_t s_registered_topic_count;
/** 保护注册表的短临界区锁；注册与查询都可能来自不同任务。 */
static portMUX_TYPE s_registry_lock = portMUX_INITIALIZER_UNLOCKED;

/** MQTT 客户端句柄，自启动成功后持续有效，由 ESP-MQTT 管理其内部任务。 */
static esp_mqtt_client_handle_t s_client;
/** Protects start idempotency; a Wi-Fi IP reacquisition must never allocate a
 * second client, task, queue or event group. */
static portMUX_TYPE s_start_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_started;
static bool s_starting;
/** 通知周期检查任务当前是否可以安全发布请求。 */
static EventGroupHandle_t s_connection_events;
/** 周期版本检查任务句柄，由 MQTT 事件回调在连接状态变化时唤醒。 */
static TaskHandle_t s_check_task;
/** 由配置前缀和设备唯一标识组成的专属 OTA 响应 topic。 */
static char s_response_topic[MQTT_TOPIC_SIZE];
/** 由配置前缀和设备唯一标识组成的专属 OTA 通知 topic。 */
static char s_notify_topic[MQTT_TOPIC_SIZE];
/** 由配置前缀和设备唯一标识组成的专属 OTA 状态 topic。 */
static char s_status_topic[MQTT_TOPIC_SIZE];
/** 稳定且每台设备唯一的 MQTT client_id，生命周期覆盖整个客户端会话。 */
static char s_client_id[NATIVE_OTA_DEVICE_ID_SIZE];
/** 当前分片消息的重组缓冲区，容量覆盖所有已注册 topic 的上限，末尾保留 NUL。 */
static char s_payload[NATIVE_OTA_JSON_MAX_LEN + 1];
/** 当前待重组消息的总长度，单位为字节；0 表示没有有效消息。 */
static int s_payload_len;
/** 是否正在接收一条 topic 已验证且长度合法的消息。 */
static bool s_receiving_payload;
/** 当前分片消息对应的注册表下标；-1 表示没有活动消息。 */
static int s_active_topic = -1;

/** MQTT 状态事件的有界关键队列；进度不进入该队列。 */
static QueueHandle_t s_status_queue;
/** 状态发送任务，唯一执行 esp_mqtt_client_enqueue() 的状态上报任务。 */
static TaskHandle_t s_status_task;
/** 保护状态队列关联表和最新进度槽位。 */
static SemaphoreHandle_t s_status_lock;
/** MQTT_EVENT_PUBLISHED 只入队消息 ID，由状态任务完成 event_id 关联和 NVS ACK 入队。 */
static QueueHandle_t s_status_ack_queue;
/** Deadline for receiving all critical SUBACKs, in monotonic milliseconds. */
static int64_t s_suback_deadline_ms;
/** Event callback requests a client restart; ota_check_task performs it outside callback context. */
static volatile bool s_reconnect_requested;
/** ota_check_task owns this flag; it distinguishes response timeout from other wakeups.
 * The MQTT event task reads it through mqtt_check_is_waiting() under s_check_state_lock. */
static bool s_waiting_for_check_response;
/** Set by the MQTT event task and consumed by ota_check_task after a connection loss. */
static bool s_check_session_reset_requested;
/** Protects the cross-task session-reset flag and the waiting-state read. */
static portMUX_TYPE s_check_state_lock = portMUX_INITIALIZER_UNLOCKED;
/** 队列曾因满而拒绝关键事件，发送任务在腾出空间后重新 flush。 */
static volatile bool s_status_need_flush;
/** 断线后的旧 msg_id 不再可信，由状态任务在持锁时清空关联表。 */
static volatile bool s_status_reset_tracks;

/**
 * @brief 已入 ESP-MQTT 发送队列但尚未完成 PUBACK 关联的状态事件。
 *
 * used 表示槽位有效；msg_id 在 enqueue 成功后写入，断线时整表作废并由 NVS 事件重发。
 */
typedef struct {
    bool used; /**< 槽位是否正在跟踪一条关键状态。 */
    int msg_id; /**< ESP-MQTT 返回的消息 ID；0 表示尚未建立关联。 */
    char event_id[NATIVE_OTA_EVENT_ID_SIZE]; /**< 报告模块生成的幂等事件 ID。 */
} mqtt_status_track_t;

/** 关键状态事件的 msg_id/event_id 关联表；访问时由 s_status_lock 保护。 */
static mqtt_status_track_t s_status_tracks[MQTT_STATUS_TRACK_SIZE];
/** 当前尚未发送的最新进度；发送后可丢弃，不进入 NVS。 */
static native_ota_report_message_t s_latest_progress;
/** s_latest_progress 是否包含可发送数据。 */
static bool s_latest_progress_valid;
/** 最近一次合法 ota_notify 的单调时间，单位为 ms；初始值 -1 表示从未收到。 */
static int64_t s_last_notify_ms = -1;

static esp_err_t mqtt_start_finish(esp_err_t err)
{
    portENTER_CRITICAL(&s_start_lock);
    s_starting = false;
    s_started = (err == ESP_OK);
    portEXIT_CRITICAL(&s_start_lock);
    return err;
}

#if CONFIG_COMM_DEVICE_AUTH_CERTIFICATE
/* 仅在选择证书鉴权时由 CMake 嵌入客户端证书和私钥。 */
extern const char device_client_cert_pem_start[] asm("_binary_device_client_cert_pem_start");
extern const char device_client_key_pem_start[] asm("_binary_device_client_key_pem_start");
#endif

/**
 * @brief 解析当前配置的 MQTT 鉴权材料，但不把凭据写入日志。
 *
 * 用户名/密码模式优先使用通用设备凭据；为兼容旧 sdkconfig，当通用配置为空时回退到
 * 旧 MQTT 专用配置。证书模式只返回构建系统嵌入的证书指针。
 *
 * @param[out] username 输出用户名指针，可为空表示匿名模式。
 * @param[out] password 输出密码/token 指针，可为空表示匿名模式。
 * @param[out] client_cert 输出客户端证书指针；非证书模式为 NULL。
 * @param[out] client_key 输出客户端私钥指针；非证书模式为 NULL。
 * @return ESP_OK 鉴权配置有效。
 * @return ESP_ERR_INVALID_ARG 选定的凭据模式缺少必需材料。
 * @return ESP_ERR_INVALID_STATE 编译配置没有选择已知鉴权模式。
 */
static esp_err_t mqtt_get_device_auth(const char **username, const char **password,
                                      const char **client_cert, const char **client_key)
{
    if (username == NULL || password == NULL || client_cert == NULL || client_key == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    *username = NULL;
    *password = NULL;
    *client_cert = NULL;
    *client_key = NULL;

#if CONFIG_COMM_DEVICE_AUTH_NONE
    return ESP_OK;
#elif CONFIG_COMM_DEVICE_AUTH_USERNAME_PASSWORD
    *username = CONFIG_COMM_DEVICE_AUTH_USERNAME[0] != '\0' ?
                CONFIG_COMM_DEVICE_AUTH_USERNAME :
                (CONFIG_COMM_MQTT_USERNAME[0] != '\0' ? CONFIG_COMM_MQTT_USERNAME : NULL);
    *password = CONFIG_COMM_DEVICE_AUTH_PASSWORD[0] != '\0' ?
                CONFIG_COMM_DEVICE_AUTH_PASSWORD :
                (CONFIG_COMM_MQTT_PASSWORD[0] != '\0' ? CONFIG_COMM_MQTT_PASSWORD : NULL);
    if (*username == NULL || *password == NULL) {
        ESP_LOGE(TAG, "MQTT username/password authentication is selected but credentials are empty");
        return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
#elif CONFIG_COMM_DEVICE_AUTH_TOKEN
    if (CONFIG_COMM_DEVICE_AUTH_TOKEN_VALUE[0] == '\0') {
        ESP_LOGE(TAG, "Token authentication is selected but the device token is empty");
        return ESP_ERR_INVALID_ARG;
    }
    *username = CONFIG_COMM_DEVICE_AUTH_TOKEN_USERNAME;
    *password = CONFIG_COMM_DEVICE_AUTH_TOKEN_VALUE;
    return ESP_OK;
#elif CONFIG_COMM_DEVICE_AUTH_CERTIFICATE
    *client_cert = device_client_cert_pem_start;
    *client_key = device_client_key_pem_start;
    return ESP_OK;
#else
    return ESP_ERR_INVALID_STATE;
#endif
}

/**
 * @brief 在持有 s_status_lock 时查找 event_id 的跟踪槽位。
 *
 * @param[in] event_id NUL 结尾的报告事件 ID，不允许为 NULL。
 * @return 0～MQTT_STATUS_TRACK_SIZE-1 槽位下标；-1 表示未找到。
 */
static int mqtt_status_find_event_locked(const char *event_id)
{
    for (size_t i = 0; i < MQTT_STATUS_TRACK_SIZE; ++i) {
        if (s_status_tracks[i].used && strcmp(s_status_tracks[i].event_id, event_id) == 0) {
            return (int)i;
        }
    }
    return -1;
}

/**
 * @brief 在持有 s_status_lock 时查找一条空闲的 msg_id 关联槽位。
 *
 * @return 空闲槽位下标；-1 表示所有槽位都在等待 ACK。
 */
static int mqtt_status_find_free_locked(void)
{
    for (size_t i = 0; i < MQTT_STATUS_TRACK_SIZE; ++i) {
        if (!s_status_tracks[i].used) {
            return (int)i;
        }
    }
    return -1;
}

/**
 * @brief 将状态事件放入有界 MQTT 队列。
 *
 * 该回调只复制固定大小消息并执行零等待队列操作；OTA 下载任务不会直接调用
 * esp_mqtt_client_publish/enqueue。关键事件队列满时保留 NVS 待发送记录，进度则
 * 覆盖一个 RAM 槽位。
 *
 * @param[in] message 报告模块生成的消息，不允许为 NULL。
 * @param[in] context 通信上下文，本实现未使用，可为 NULL。
 * @return ESP_OK 已复制到队列或最新进度槽位。
 * @return ESP_ERR_TIMEOUT 无法立即取得状态锁，稍后由状态任务 flush。
 * @return ESP_ERR_NO_MEM 关键队列或关联表已满。
 */
static esp_err_t mqtt_status_transport(const native_ota_report_message_t *message,
                                       void *context)
{
    (void)context;
    if (message == NULL || s_status_queue == NULL || s_status_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (xSemaphoreTake(s_status_lock, 0) != pdTRUE) {
        s_status_need_flush = true;
        if (s_status_task != NULL) {
            xTaskNotifyGive(s_status_task);
        }
        return ESP_ERR_TIMEOUT;
    }

    esp_err_t err = ESP_OK;
    if (s_status_reset_tracks) {
        memset(s_status_tracks, 0, sizeof(s_status_tracks));
        s_status_reset_tracks = false;
    }
    if (message->critical) {
        if (mqtt_status_find_event_locked(message->event_id) >= 0) {
            /* 重连 flush 可能再次看到同一持久化事件；event_id 保证幂等。 */
            xSemaphoreGive(s_status_lock);
            return ESP_OK;
        }
        int slot = mqtt_status_find_free_locked();
        if (slot < 0 || xQueueSend(s_status_queue, message, 0) != pdTRUE) {
            s_status_need_flush = true;
            err = ESP_ERR_NO_MEM;
        } else {
            s_status_tracks[slot].used = true;
            s_status_tracks[slot].msg_id = 0;
            strncpy(s_status_tracks[slot].event_id, message->event_id,
                    sizeof(s_status_tracks[slot].event_id) - 1U);
        }
    } else {
        s_latest_progress = *message;
        s_latest_progress_valid = true;
    }
    xSemaphoreGive(s_status_lock);
    if (s_status_task != NULL) {
        xTaskNotifyGive(s_status_task);
    }
    return err;
}

/**
 * @brief 判断 MQTT 是否已连接且两个 OTA 专属订阅都已确认。
 *
 * @return true 可以调用 ESP-MQTT enqueue 发送 OTA 检查或状态。
 * @return false 客户端、事件组或订阅就绪位尚未满足。
 */
static bool mqtt_status_is_ready(void)
{
    return s_connection_events != NULL &&
           (xEventGroupGetBits(s_connection_events) & MQTT_OTA_READY_BIT) != 0 &&
           s_client != NULL;
}

/**
 * @brief 从关键状态队列发送一条消息并建立 msg_id 关联。
 *
 * @return true 一条关键事件已交给 ESP-MQTT。
 * @return false 当前未就绪、队列为空、发送失败或关联失败。
 *
 * @note 先 peek 再 enqueue，只有 enqueue 成功后才移除队列头，避免发送失败丢事件。
 */
static bool mqtt_status_drain_one_critical(void)
{
    if (!mqtt_status_is_ready() || s_status_queue == NULL) {
        return false;
    }

    native_ota_report_message_t message;
    if (xQueuePeek(s_status_queue, &message, 0) != pdTRUE) {
        return false;
    }
    int msg_id = esp_mqtt_client_enqueue(s_client, s_status_topic, message.json,
                                         (int)message.json_len, 1, 0, true);
    if (msg_id < 0) {
        return false;
    }
    if (xQueueReceive(s_status_queue, &message, 0) != pdTRUE) {
        return false;
    }

    if (s_status_lock != NULL && xSemaphoreTake(s_status_lock, 0) == pdTRUE) {
        int slot = mqtt_status_find_event_locked(message.event_id);
        if (slot >= 0) {
            s_status_tracks[slot].msg_id = msg_id;
        }
        xSemaphoreGive(s_status_lock);
    }
    ESP_LOGI(TAG, "Queued OTA status state=%s msg_id=%d",
             native_ota_report_state_name(message.state), msg_id);
    return true;
}

/**
 * @brief 发送 RAM 中最新的一条下载进度。
 *
 * @return true 进度已交给 ESP-MQTT。
 * @return false 当前没有可发送进度、连接未就绪或发送失败。
 *
 * @note 发送后仅当槽位仍对应同一 event_id 才清空，避免并发产生的新进度被误删。
 */
static bool mqtt_status_drain_progress(void)
{
    if (!mqtt_status_is_ready() || s_status_lock == NULL) {
        return false;
    }
    native_ota_report_message_t message;
    if (xSemaphoreTake(s_status_lock, 0) != pdTRUE) {
        return false;
    }
    bool valid = s_latest_progress_valid;
    if (valid) {
        message = s_latest_progress;
    }
    xSemaphoreGive(s_status_lock);
    if (!valid) {
        return false;
    }

    int msg_id = esp_mqtt_client_enqueue(s_client, s_status_topic, message.json,
                                         (int)message.json_len, 1, 0, true);
    if (msg_id < 0) {
        return false;
    }
    if (xSemaphoreTake(s_status_lock, 0) == pdTRUE) {
        if (s_latest_progress_valid &&
            strcmp(s_latest_progress.event_id, message.event_id) == 0) {
            s_latest_progress_valid = false;
        }
        xSemaphoreGive(s_status_lock);
    }
    ESP_LOGD(TAG, "Queued OTA progress msg_id=%d", msg_id);
    return true;
}

/**
 * @brief 在状态任务中处理 QoS 1 PUBACK。
 *
 * MQTT_EVENT_PUBLISHED 可能早于发送任务完成 msg_id 关联，因此事件回调只把消息
 * ID 放入有界队列；等状态任务完成发送关联后再查找 event_id，避免丢失 ACK。
 *
 * @return true 至少处理了一条 PUBACK 消息。
 * @return false 当前未就绪或没有待处理 PUBACK。
 */
static bool mqtt_status_process_published(void)
{
    if (!mqtt_status_is_ready() || s_status_ack_queue == NULL || s_status_lock == NULL) {
        return false;
    }

    bool processed = false;
    int msg_id;
    while (xQueueReceive(s_status_ack_queue, &msg_id, 0) == pdTRUE) {
        char event_id[NATIVE_OTA_EVENT_ID_SIZE] = { 0 };
        bool found = false;
        if (xSemaphoreTake(s_status_lock, portMAX_DELAY) == pdTRUE) {
            for (size_t i = 0; i < MQTT_STATUS_TRACK_SIZE; ++i) {
                if (s_status_tracks[i].used && s_status_tracks[i].msg_id == msg_id &&
                    msg_id > 0) {
                    strncpy(event_id, s_status_tracks[i].event_id, sizeof(event_id) - 1U);
                    s_status_tracks[i].used = false;
                    found = true;
                    break;
                }
            }
            xSemaphoreGive(s_status_lock);
        }
        if (found) {
            ESP_LOGI(TAG, "OTA status PUBACK received, event_id=%s", event_id);
            native_ota_report_ack_event(event_id);
        } else {
            /* ota_check and best-effort progress also use QoS 1 but intentionally have no
             * persistent event_id.  Their PUBACK is normal and must not look like a fault. */
            ESP_LOGD(TAG, "PUBACK belongs to a non-persistent MQTT publish, msg_id=%d", msg_id);
        }
        processed = true;
    }
    return processed;
}

/**
 * @brief 异步排空 OTA 状态队列、进度槽位并处理 PUBACK。
 *
 * @param[in] parameter FreeRTOS 任务参数，本实现未使用。
 *
 * 任务只在 MQTT 就绪时调用 ESP-MQTT enqueue；空闲时最多阻塞 1 s 等待通知。它不直接
 * 写 NVS，PUBACK 删除由 ota_report_ack_task 完成。
 */
static void mqtt_status_task(void *parameter)
{
    (void)parameter;
    while (1) {
        bool sent = false;
        if (s_status_reset_tracks && s_status_lock != NULL &&
            xSemaphoreTake(s_status_lock, portMAX_DELAY) == pdTRUE) {
            memset(s_status_tracks, 0, sizeof(s_status_tracks));
            s_status_reset_tracks = false;
            xSemaphoreGive(s_status_lock);
        }
        if (mqtt_status_is_ready()) {
            while (mqtt_status_drain_one_critical()) {
                sent = true;
            }
            if (mqtt_status_drain_progress()) {
                sent = true;
            }
            if (mqtt_status_process_published()) {
                sent = true;
            }

            bool need_flush = false;
            if (s_status_lock != NULL && xSemaphoreTake(s_status_lock, 0) == pdTRUE) {
                need_flush = s_status_need_flush &&
                             uxQueueMessagesWaiting(s_status_queue) == 0;
                if (need_flush) {
                    s_status_need_flush = false;
                }
                xSemaphoreGive(s_status_lock);
            }
            if (need_flush) {
                (void)native_ota_report_flush_pending();
                sent = true;
            }
        }
        if (!sent) {
            (void)ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000));
        }
    }
}

/**
 * @brief 在 MQTT_EVENT_PUBLISHED 回调中转发一条 PUBACK 消息 ID。
 *
 * @param[in] msg_id ESP-MQTT 返回的消息 ID，应为正数。
 *
 * @note 回调只做零等待入队；确认队列满时保留 NVS 事件，不能在此处写 Flash。
 */
static void mqtt_status_handle_published(int msg_id)
{
    if (s_status_ack_queue == NULL || xQueueSend(s_status_ack_queue, &msg_id, 0) != pdTRUE) {
        /* ACK 队列满时丢弃本条 PUBACK：非持久化发布（ota_check、进度）没有
         * event_id，丢弃无影响；持久化状态事件仍留在 NVS，重连后按 event_id 重发。 */
        ESP_LOGD(TAG, "PUBACK queue full; dropped ack for msg_id=%d", msg_id);
    } else if (s_status_task != NULL) {
        xTaskNotifyGive(s_status_task);
    }
}

/**
 * @brief 标记 MQTT 断线并安排状态关联表在状态任务中清理。
 *
 * 断线会使旧 msg_id 失去意义，但 ota_report NVS 中的关键事件仍有效；重连订阅成功后
 * 由 flush_pending() 使用同一 event_id 重新发送。
 */
static void mqtt_status_handle_disconnected(void)
{
    /* 已交给 ESP-MQTT 但尚未收到 PUBACK 的事件仍在 ota_report NVS 中；重连时重发。 */
    s_status_reset_tracks = true;
    s_status_need_flush = true;
    if (s_status_task != NULL) {
        xTaskNotifyGive(s_status_task);
    }
}

/**
 * @brief 释放状态上报任务、队列、锁和内存关联表。
 *
 * @note 只用于 mqtt_comm_start() 的失败回滚路径；函数不删除 ota_report NVS 事件。
 */
static void mqtt_status_cleanup(void)
{
    if (s_status_task != NULL) {
        vTaskDelete(s_status_task);
        s_status_task = NULL;
    }
    if (s_status_queue != NULL) {
        vQueueDelete(s_status_queue);
        s_status_queue = NULL;
    }
    if (s_status_ack_queue != NULL) {
        vQueueDelete(s_status_ack_queue);
        s_status_ack_queue = NULL;
    }
    if (s_status_lock != NULL) {
        vSemaphoreDelete(s_status_lock);
        s_status_lock = NULL;
    }
    memset(s_status_tracks, 0, sizeof(s_status_tracks));
    s_latest_progress_valid = false;
    s_status_need_flush = false;
    s_status_reset_tracks = false;
}

/**
 * @brief 在注册表中查找 MQTT 数据事件对应的 topic 下标。
 *
 * 事件中的 topic 不是 NUL 结尾字符串，因此必须同时比较长度和原始字节。
 *
 * @param[in] event ESP-MQTT 提供的数据事件，不允许为 NULL。
 * @return 匹配的注册表下标；无匹配返回 -1。
 */
static int mqtt_find_registered_topic(const esp_mqtt_event_handle_t event)
{
    if (event == NULL || event->topic == NULL) {
        return -1;
    }
    int match = -1;
    portENTER_CRITICAL(&s_registry_lock);
    for (size_t i = 0; i < s_registered_topic_count; i++) {
        const size_t configured_len = strlen(s_registered_topics[i].topic);
        if (event->topic_len == (int)configured_len &&
            memcmp(event->topic, s_registered_topics[i].topic, configured_len) == 0) {
            match = (int)i;
            break;
        }
    }
    portEXIT_CRITICAL(&s_registry_lock);
    return match;
}

/**
 * @brief 判断全部 critical 订阅是否都已收到 SUBACK。
 *
 * sub_msg_id 为 -1 表示该 topic 当前没有待确认订阅；配合 s_suback_deadline_ms
 * 非零（表示正处于一轮订阅确认窗口内）共同判定就绪。
 *
 * @return true 所有 critical topic 均已确认；false 仍有待确认订阅。
 */
static bool mqtt_all_critical_subscribed(void)
{
    bool ready = true;
    portENTER_CRITICAL(&s_registry_lock);
    for (size_t i = 0; i < s_registered_topic_count; i++) {
        if (s_registered_topics[i].critical && s_registered_topics[i].sub_msg_id != -1) {
            ready = false;
            break;
        }
    }
    portEXIT_CRITICAL(&s_registry_lock);
    return ready;
}

/**
 * @brief 把注册表中所有 topic 的待确认订阅 ID 重置为 -1。
 *
 * 断线或重建连接后，旧会话的订阅确认不再可信，必须整表作废。
 */
static void mqtt_reset_sub_msg_ids(void)
{
    portENTER_CRITICAL(&s_registry_lock);
    for (size_t i = 0; i < s_registered_topic_count; i++) {
        s_registered_topics[i].sub_msg_id = -1;
    }
    portEXIT_CRITICAL(&s_registry_lock);
}

esp_err_t mqtt_comm_register_topic(const char *topic, size_t max_payload_len,
                                   bool critical, mqtt_inbound_handler_t handler)
{
    if (topic == NULL || topic[0] == '\0' || handler == NULL ||
        max_payload_len == 0U || max_payload_len > NATIVE_OTA_JSON_MAX_LEN) {
        return ESP_ERR_INVALID_ARG;
    }
    if (strlen(topic) >= MQTT_TOPIC_SIZE) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t result = ESP_ERR_NO_MEM;
    portENTER_CRITICAL(&s_registry_lock);
    /* 同名 topic 重复注册时覆盖旧配置，保证注册幂等。 */
    for (size_t i = 0; i < s_registered_topic_count; i++) {
        if (strcmp(s_registered_topics[i].topic, topic) == 0) {
            s_registered_topics[i].max_payload_len = max_payload_len;
            s_registered_topics[i].critical = critical;
            s_registered_topics[i].handler = handler;
            s_registered_topics[i].sub_msg_id = -1;
            portEXIT_CRITICAL(&s_registry_lock);
            return ESP_OK;
        }
    }
    if (s_registered_topic_count < MQTT_MAX_REGISTERED_TOPICS) {
        mqtt_registered_topic_t *slot = &s_registered_topics[s_registered_topic_count];
        strncpy(slot->topic, topic, sizeof(slot->topic) - 1U);
        slot->topic[sizeof(slot->topic) - 1U] = '\0';
        slot->max_payload_len = max_payload_len;
        slot->critical = critical;
        slot->handler = handler;
        slot->sub_msg_id = -1;
        s_registered_topic_count++;
        result = ESP_OK;
    }
    portEXIT_CRITICAL(&s_registry_lock);
    return result;
}

/**
 * @brief 将秒数换算为 FreeRTOS tick，并显式检查 32 位毫秒范围。
 *
 * Kconfig 取值范围已保证常规路径不越界；本函数仍防御毫秒换算溢出，
 * 溢出时记录错误并钳制为 portMAX_DELAY，避免把非法等待值交给任务。
 *
 * @param[in] seconds 等待秒数。
 * @return 换算后的 tick 数，始终大于 0。
 */
static TickType_t mqtt_seconds_to_ticks(uint32_t seconds)
{
    if (seconds > UINT32_MAX / 1000U) {
        ESP_LOGE(TAG, "OTA check delay overflow: %" PRIu32 " s exceeds millisecond range",
                 seconds);
        return portMAX_DELAY;
    }
    uint32_t delay_ms = seconds * 1000U;
    TickType_t ticks = pdMS_TO_TICKS(delay_ms);
    return ticks > 0U ? ticks : 1U;
}

/**
 * @brief 计算下一次 OTA 检查前的等待时间。
 *
 * 在固定检查周期上叠加 0～CONFIG_OTA_CHECK_JITTER_SECONDS 秒随机抖动，避免大量
 * 终端在整点或统一重启后同时请求服务器。加法和毫秒换算都显式检查范围，
 * 不会因抖动把间隔推到 32 位溢出边界。
 *
 * @return 换算后的 FreeRTOS tick 数，始终大于 0。
 */
static TickType_t mqtt_next_check_delay(void)
{
    /* 先取秒级抖动，再统一换算为毫秒/tick，避免每次任务唤醒都固定落在同一时刻。 */
    uint32_t interval_seconds = (uint32_t)CONFIG_OTA_CHECK_INTERVAL_SECONDS;
    uint32_t jitter_seconds = 0;
#if CONFIG_OTA_CHECK_JITTER_SECONDS > 0
    jitter_seconds = esp_random() % (CONFIG_OTA_CHECK_JITTER_SECONDS + 1U);
#endif
    uint32_t total_seconds = interval_seconds +
                             MIN(jitter_seconds, UINT32_MAX - interval_seconds);
    return mqtt_seconds_to_ticks(total_seconds);
}

static TickType_t mqtt_check_retry_delay(unsigned retry)
{
    uint32_t delay_seconds = (uint32_t)CONFIG_OTA_CHECK_RESPONSE_RETRY_BASE_SECONDS;
    while (retry-- > 0U && delay_seconds < (uint32_t)CONFIG_OTA_CHECK_RESPONSE_RETRY_MAX_SECONDS) {
        uint32_t doubled = delay_seconds * 2U;
        delay_seconds = doubled > (uint32_t)CONFIG_OTA_CHECK_RESPONSE_RETRY_MAX_SECONDS ?
                        (uint32_t)CONFIG_OTA_CHECK_RESPONSE_RETRY_MAX_SECONDS : doubled;
    }
    uint32_t jitter_ms = esp_random() % 1001U;
    return mqtt_seconds_to_ticks(delay_seconds) + pdMS_TO_TICKS(jitter_ms);
}

/**
 * @brief 计算恢复模式下第 step 步的等待秒数。
 *
 * 从 CONFIG_OTA_CHECK_RECOVERY_INITIAL_SECONDS 开始逐级翻倍，并钳制到
 * CONFIG_OTA_CHECK_RECOVERY_MAX_SECONDS；达到上限后每一步都返回上限，
 * 保证服务器恢复后设备以固定周期继续检查，而不是无限加速或彻底沉默。
 *
 * @param[in] step 当前恢复步数，从 0 开始。
 * @return 本次恢复等待秒数，范围受 Kconfig 约束。
 */
static uint32_t mqtt_recovery_wait_seconds(unsigned step)
{
    uint32_t delay_seconds = (uint32_t)CONFIG_OTA_CHECK_RECOVERY_INITIAL_SECONDS;
    uint32_t max_seconds = (uint32_t)CONFIG_OTA_CHECK_RECOVERY_MAX_SECONDS;
    if (delay_seconds > max_seconds) {
        delay_seconds = max_seconds;
    }
    while (step-- > 0U && delay_seconds < max_seconds) {
        uint32_t doubled = delay_seconds * 2U;
        delay_seconds = doubled > max_seconds ? max_seconds : doubled;
    }
    return delay_seconds;
}

/** 恢复步数上限；达到后延迟稳定在最大值，只防止计数器无意义增长。 */
#define MQTT_RECOVERY_STEP_CAP 64U

/**
 * @brief 推进恢复步数并返回下一次恢复检查的等待 tick。
 *
 * @param[in,out] step 当前恢复步数；函数返回后前进到下一步。
 * @return 下一次恢复检查前的等待 tick 数。
 */
static TickType_t mqtt_recovery_check_delay(unsigned *step)
{
    uint32_t seconds = mqtt_recovery_wait_seconds(*step);
    if (*step < MQTT_RECOVERY_STEP_CAP) {
        (*step)++;
    }
    ESP_LOGI(TAG, "recovery check scheduled in %" PRIu32 " seconds", seconds);
    return mqtt_seconds_to_ticks(seconds);
}

static void mqtt_check_request_session_reset(void)
{
    portENTER_CRITICAL(&s_check_state_lock);
    s_check_session_reset_requested = true;
    portEXIT_CRITICAL(&s_check_state_lock);
}

/**
 * @brief 由检查任务写回当前是否正在等待 ota_check_response。
 *
 * MQTT 事件任务通过 mqtt_check_is_waiting() 读取该状态，因此写入也经过同一
 * 自旋锁，保证事件回调看到的等待状态不会与检查任务正在切换的状态互相撕裂。
 */
static void mqtt_check_set_waiting(bool waiting)
{
    portENTER_CRITICAL(&s_check_state_lock);
    s_waiting_for_check_response = waiting;
    portEXIT_CRITICAL(&s_check_state_lock);
}

/**
 * @brief 查询检查任务当前是否在等待一个 ota_check_response。
 *
 * 只有等待窗口内的非法响应才设置 REJECTED 位并唤醒短重试；窗口外的垃圾消息
 * 直接丢弃，不能打破正常或恢复检查节奏。
 */
static bool mqtt_check_is_waiting(void)
{
    portENTER_CRITICAL(&s_check_state_lock);
    bool waiting = s_waiting_for_check_response;
    portEXIT_CRITICAL(&s_check_state_lock);
    return waiting;
}

static bool mqtt_check_take_session_reset(void)
{
    portENTER_CRITICAL(&s_check_state_lock);
    bool requested = s_check_session_reset_requested;
    s_check_session_reset_requested = false;
    portEXIT_CRITICAL(&s_check_state_lock);
    return requested;
}

static void mqtt_request_reconnect(const char *reason)
{
    ESP_LOGW(TAG, "MQTT reconnect requested: %s", reason);
    if (s_connection_events != NULL) {
        xEventGroupClearBits(s_connection_events, MQTT_OTA_READY_BIT |
                             MQTT_OTA_RESPONSE_BIT | MQTT_OTA_RESPONSE_REJECTED_BIT);
    }
    mqtt_check_request_session_reset();
    mqtt_reset_sub_msg_ids();
    s_suback_deadline_ms = 0;
    s_reconnect_requested = true;
    if (s_check_task != NULL) {
        xTaskNotifyGive(s_check_task);
    }
}

/**
 * @brief 发布一次包含设备信息和当前固件版本的 OTA 检查请求。
 *
 * 请求由通用 OTA 模块生成，本函数只负责通过配置的公共检查 topic 以 QoS 1、
 * 非 retained 方式发送。非 retained 可避免新上线设备收到过期请求；请求失败由
 * 后续周期检查或重连检查自然恢复。
 *
 * @return ESP_OK 请求已交给 ESP-MQTT 发送队列。
 * @return ESP_FAIL MQTT 发布接口拒绝请求。
 * @return 其他 esp_err_t 请求 JSON 生成失败。
 *
 * @note 由 ota_check_task 调用，可能因 MQTT 内部发送而短暂阻塞，不能在中断调用。
 */
static esp_err_t mqtt_publish_ota_check(void)
{
    /* 请求 JSON 在栈上生成并立即交给 ESP-MQTT 队列，enqueue 返回后不再依赖该缓冲区。 */
    char request[NATIVE_OTA_CHECK_JSON_SIZE];
    size_t request_len = 0;
    esp_err_t err = native_ota_build_check_request(request, sizeof(request), &request_len);
    if (err != ESP_OK) {
        return err;
    }

    int msg_id = esp_mqtt_client_enqueue(s_client, CONFIG_COMM_MQTT_OTA_CHECK_TOPIC,
                                         request, (int)request_len, 1, 0, true);
    if (msg_id < 0) {
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Published OTA check to %s, msg_id=%d",
             CONFIG_COMM_MQTT_OTA_CHECK_TOPIC, msg_id);
    return ESP_OK;
}

/**
 * @brief 在 MQTT 就绪时立即检查版本，并按带抖动的周期重复检查。
 *
 * 任务初始无限等待事件回调通知。专属响应 topic 订阅成功后，事件回调设置
 * MQTT_OTA_READY_BIT 并唤醒本任务；每次请求后进入定时等待。断线事件会清除就绪位
 * 并唤醒任务，使其恢复无限等待，避免离线期间反复调用发布接口。
 *
 * 服务器无响应恢复策略：
 * - 每次检查等待 CONFIG_OTA_CHECK_RESPONSE_TIMEOUT_SECONDS 秒，超时后按配置次数
 *   执行带指数退避的短重试；
 * - 短重试耗尽后进入恢复模式，恢复等待从 CONFIG_OTA_CHECK_RECOVERY_INITIAL_SECONDS
 *   秒逐级翻倍到 CONFIG_OTA_CHECK_RECOVERY_MAX_SECONDS 秒并保持该周期持续检查，
 *   直到收到合法响应（update=false 同样视为恢复成功）；
 * - 合法响应立即清零重试计数和恢复步数，回到正常 6 小时周期；
 * - MQTT 重连并完成两个订阅后立即检查一次；合法 ota_notify 也立即唤醒检查。
 *
 * 并发约束：本任务是全模块唯一的检查发布者，同一时刻至多一个待响应请求；
 * 等待期间收到 ota_notify 时立即发布携带新 request_id 的请求来安全替换旧请求，
 * 旧 request_id 的迟到响应由控制面拒绝，不能结束新请求的等待。
 *
 * @param[in] pv_parameter FreeRTOS 任务参数，本实现未使用。
 *
 * @note 任务由 mqtt_comm_start() 创建并在客户端生命周期内常驻。
 * @note 与 MQTT 事件任务共享 s_connection_events，通过 FreeRTOS 事件组同步。
 */
static void mqtt_ota_check_task(void *pv_parameter)
{
    (void)pv_parameter;
    TickType_t wait_ticks = portMAX_DELAY;
    unsigned response_retries = 0;
    unsigned recovery_step = 0;
    bool recovery_mode = false;
    /* 上一次循环观察到的就绪状态；只用于区分首次连接与断线恢复的日志。 */
    bool was_ready = false;
    bool connection_ever_ready = false;

    while (1) {
        uint32_t notified = ulTaskNotifyTake(pdTRUE, wait_ticks);

        if (mqtt_check_take_session_reset()) {
            /* 新会话不继承旧会话的等待状态、短重试计数和恢复进度。 */
            mqtt_check_set_waiting(false);
            response_retries = 0;
            recovery_step = 0;
            recovery_mode = false;
            ESP_LOGI(TAG, "Reset OTA check retry state for the new MQTT session");
        }

        if (s_reconnect_requested) {
            s_reconnect_requested = false;
            if (s_client != NULL) {
                ESP_LOGW(TAG, "Rebuilding MQTT client connection");
                (void)esp_mqtt_client_stop(s_client);
                esp_err_t restart_err = esp_mqtt_client_start(s_client);
                if (restart_err != ESP_OK) {
                    ESP_LOGE(TAG, "Failed to restart MQTT client: %s",
                             esp_err_to_name(restart_err));
                }
            }
            wait_ticks = portMAX_DELAY;
            continue;
        }

        EventBits_t bits = xEventGroupGetBits(s_connection_events);
        if ((bits & MQTT_OTA_READY_BIT) == 0) {
            was_ready = false;
            int64_t now_ms = esp_timer_get_time() / 1000LL;
            if (s_suback_deadline_ms > now_ms) {
                wait_ticks = pdMS_TO_TICKS((uint32_t)(s_suback_deadline_ms - now_ms));
            } else if (s_suback_deadline_ms != 0) {
                mqtt_request_reconnect("OTA subscription SUBACK timeout");
                wait_ticks = 0;
            } else {
                wait_ticks = portMAX_DELAY;
            }
            continue;
        }

        if (!was_ready) {
            if (connection_ever_ready) {
                /* 断线期间等待 ESP-MQTT 自动重连；两个订阅都恢复后立即检查一次。 */
                ESP_LOGI(TAG, "MQTT session restored; sending immediate OTA check");
            } else {
                ESP_LOGI(TAG, "MQTT connection ready; sending initial OTA check");
                connection_ever_ready = true;
            }
            was_ready = true;
        }

        if ((bits & MQTT_OTA_RESPONSE_BIT) != 0) {
            /* 合法响应使任何旧 REJECTED 位失去意义，一并清除。 */
            xEventGroupClearBits(s_connection_events, MQTT_OTA_RESPONSE_BIT |
                                 MQTT_OTA_RESPONSE_REJECTED_BIT);
            mqtt_check_set_waiting(false);
            response_retries = 0;
            if (recovery_mode) {
                /* update=false 也走此路径，同样视为策略服务器恢复成功。 */
                ESP_LOGI(TAG, "OTA policy server response restored; returning to normal interval");
                recovery_mode = false;
                recovery_step = 0;
            } else {
                ESP_LOGD(TAG, "Valid OTA check response received; next check in %u s",
                         CONFIG_OTA_CHECK_INTERVAL_SECONDS);
            }
            wait_ticks = mqtt_next_check_delay();
            continue;
        }

        if ((bits & MQTT_OTA_RESPONSE_REJECTED_BIT) != 0) {
            xEventGroupClearBits(s_connection_events, MQTT_OTA_RESPONSE_REJECTED_BIT);
            if (s_waiting_for_check_response) {
                mqtt_check_set_waiting(false);
                if (response_retries < CONFIG_OTA_CHECK_RESPONSE_RETRY_COUNT) {
                    response_retries++;
                    ESP_LOGW(TAG, "Invalid OTA check response; scheduling short retry %u/%u",
                             response_retries, CONFIG_OTA_CHECK_RESPONSE_RETRY_COUNT);
                    wait_ticks = mqtt_check_retry_delay(response_retries - 1U);
                } else {
                    /* 服务器持续返回非法响应与无响应同样按未恢复处理。 */
                    ESP_LOGW(TAG, "OTA check response retries exhausted; entering server recovery mode");
                    response_retries = 0;
                    recovery_mode = true;
                    wait_ticks = mqtt_recovery_check_delay(&recovery_step);
                }
                continue;
            }
            /* 事件回调只会在等待窗口内设置该位；此分支仅为防御陈旧位，不触发发布。 */
            continue;
        }

        /* A timeout is distinct from an explicit reconnect/notify wakeup. */
        if (notified == 0 && s_waiting_for_check_response) {
            mqtt_check_set_waiting(false);
            if (response_retries < CONFIG_OTA_CHECK_RESPONSE_RETRY_COUNT) {
                response_retries++;
                ESP_LOGW(TAG, "OTA check response timed out; scheduling short retry %u/%u",
                         response_retries, CONFIG_OTA_CHECK_RESPONSE_RETRY_COUNT);
                wait_ticks = mqtt_check_retry_delay(response_retries - 1U);
                continue;
            }
            /* 短重试耗尽后不再回到 6 小时周期，而是进入递增恢复检查。 */
            ESP_LOGW(TAG, "OTA check response retries exhausted; entering server recovery mode");
            response_retries = 0;
            recovery_mode = true;
            wait_ticks = mqtt_recovery_check_delay(&recovery_step);
            continue;
        }

        /* 走到这里只有两种情形：等待窗口内被 notify 唤醒（安全替换旧请求），
         * 或正常/恢复周期等待结束。两种情况都发布一个携带新 request_id 的请求。 */
        if (s_waiting_for_check_response) {
            ESP_LOGD(TAG, "Replacing pending OTA check after external wakeup");
        }
        esp_err_t err = mqtt_publish_ota_check();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to publish OTA check: %s", esp_err_to_name(err));
            wait_ticks = mqtt_check_retry_delay(0);
            continue;
        }
        mqtt_check_set_waiting(true);
        wait_ticks = mqtt_seconds_to_ticks((uint32_t)CONFIG_OTA_CHECK_RESPONSE_TIMEOUT_SECONDS);
    }
}

/**
 * @brief 清除当前 MQTT 消息的重组状态。
 *
 * 本函数不会擦除缓冲区内容；长度和接收标志被清除后，旧内容不会再被解析。
 * 只能由 ESP-MQTT 事件回调调用，不需要额外互斥锁。
 */
static void mqtt_reset_payload(void)
{
    /* 不清零 s_payload 是有意的：长度和 receiving 标志清零后旧字节不会再次被解析。 */
    s_payload_len = 0;
    s_receiving_payload = false;
    s_active_topic = -1;
}

/**
 * @brief 校验 ota_notify 并只唤醒主动检查任务。
 *
 * 通知不包含清单和 URL，也不直接创建 OTA 下载任务；完整清单仍必须通过下一次
 * ota_check/ota_check_response 交给公共控制面解析器。重复合法通知按时间节流。
 *
 * @param[in] json NUL 结尾或至少包含 json_len 字节的通知 JSON。
 * @param[in] json_len JSON 有效长度，单位为字节，不能超过 MQTT_OTA_NOTIFY_MAX_LEN。
 *
 * @note 函数只解析固定上限的临时 cJSON 对象，并通过任务通知唤醒检查任务；不下载固件。
 */
static void mqtt_handle_notify_json(const char *json, size_t json_len)
{
    if (json == NULL || json_len == 0 || json_len > MQTT_OTA_NOTIFY_MAX_LEN) {
        ESP_LOGW(TAG, "Ignoring oversized or empty ota_notify payload");
        return;
    }

    cJSON *root = cJSON_ParseWithLength(json, json_len);
    if (root == NULL || !cJSON_IsObject(root)) {
        ESP_LOGW(TAG, "Ignoring malformed ota_notify JSON");
        cJSON_Delete(root);
        return;
    }
    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    const cJSON *schema_version = cJSON_GetObjectItemCaseSensitive(root, "schema_version");
    const cJSON *job_id = cJSON_GetObjectItemCaseSensitive(root, "job_id");
    bool valid = cJSON_IsString(type) && strcmp(type->valuestring, "ota_notify") == 0 &&
                 cJSON_IsNumber(schema_version) && schema_version->valueint == 1 &&
                 schema_version->valuedouble == 1.0 &&
                 (job_id == NULL || cJSON_IsString(job_id));
    if (valid && job_id != NULL &&
        (job_id->valuestring == NULL || strlen(job_id->valuestring) >= NATIVE_OTA_JOB_ID_SIZE)) {
        valid = false;
    }
    if (!valid) {
        ESP_LOGW(TAG, "Ignoring ota_notify with invalid type/schema/fields");
        cJSON_Delete(root);
        return;
    }

    /* 节流使用设备启动后的单调时间，不受 NTP 校时或 UTC 回拨影响。 */
    int64_t now_ms = esp_timer_get_time() / 1000LL;
    int64_t throttle_ms = (int64_t)CONFIG_COMM_MQTT_OTA_NOTIFY_THROTTLE_SECONDS * 1000LL;
    if (s_last_notify_ms >= 0 && throttle_ms > 0 &&
        now_ms - s_last_notify_ms < throttle_ms) {
        ESP_LOGI(TAG, "Ignoring throttled duplicate ota_notify");
        cJSON_Delete(root);
        return;
    }
    s_last_notify_ms = now_ms;
    ESP_LOGI(TAG, "Accepted ota_notify; waking the active OTA check task");
    if (s_check_task != NULL) {
        xTaskNotifyGive(s_check_task);
    }
    cJSON_Delete(root);
}

/**
 * @brief 处理一条完整重组的 ota_check_response（注册表回调）。
 *
 * 把完整载荷转交给 OTA 公共入口解析；合法响应唤醒主动检查任务，等待窗口内的
 * 非法响应触发有界短重试。本回调在 MQTT 事件任务上下文中执行，只解析元数据、
 * 至多创建 OTA 任务，绝不在此处下载固件或写 Flash。
 *
 * @param[in] payload     NUL 结尾的完整响应。
 * @param[in] payload_len 响应有效长度，不含末尾 NUL。
 */
static void mqtt_handle_ota_response(const char *payload, size_t payload_len)
{
    esp_err_t err = native_ota_handle_server_json(payload, payload_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA server response rejected: %s", esp_err_to_name(err));
        /* 只有当前请求等待窗口内的非法响应才触发短重试；窗口外的垃圾消息
         * 被丢弃，避免外部消息打破正常或恢复检查节奏。 */
        if (err == ESP_ERR_INVALID_ARG && mqtt_check_is_waiting() &&
            s_connection_events != NULL) {
            xEventGroupSetBits(s_connection_events, MQTT_OTA_RESPONSE_REJECTED_BIT);
            if (s_check_task != NULL) {
                xTaskNotifyGive(s_check_task);
            }
        }
    } else if (s_connection_events != NULL) {
        /* The control plane has checked the current request_id, so a stale
         * response cannot end this wait window. */
        xEventGroupSetBits(s_connection_events, MQTT_OTA_RESPONSE_BIT);
        if (s_check_task != NULL) {
            xTaskNotifyGive(s_check_task);
        }
    }
}

/**
 * @brief 接收并重组一个 MQTT_EVENT_DATA 数据片段。
 *
 * 第一片通过注册表匹配 topic 并建立接收状态，后续片依据 current_data_offset
 * 写入固定缓冲区。最后一片到达后追加 NUL，并把完整载荷交给该 topic 注册的
 * handler。异常偏移或越界会立即丢弃整条消息。
 *
 * @param[in] event ESP-MQTT 数据事件，不允许为 NULL。
 *
 * @note 由 MQTT 客户端任务调用，函数不执行 HTTPS 下载，仅可能创建业务任务。
 */
static void mqtt_handle_data(const esp_mqtt_event_handle_t event)
{
    if (event->current_data_offset == 0) {
        /* 新消息的首片必须重新验证 topic 和总长度，避免沿用上一条分片消息的状态。 */
        mqtt_reset_payload();

        s_active_topic = mqtt_find_registered_topic(event);
        if (s_active_topic < 0) {
            ESP_LOGW(TAG, "Ignoring message from an unexpected topic");
            return;
        }
        const mqtt_registered_topic_t *slot = &s_registered_topics[s_active_topic];
        if (event->total_data_len <= 0 ||
            event->total_data_len > (int)slot->max_payload_len) {
            ESP_LOGE(TAG, "Invalid payload length: %d", event->total_data_len);
            mqtt_reset_payload();
            return;
        }

        s_payload_len = event->total_data_len;
        s_receiving_payload = true;
    }

    if (!s_receiving_payload || event->current_data_offset < 0 || event->data_len < 0 ||
        event->current_data_offset + event->data_len > s_payload_len) {
        ESP_LOGE(TAG, "Invalid MQTT payload fragment");
        mqtt_reset_payload();
        return;
    }

    /* current_data_offset 是 MQTT 原始 payload 偏移，不是本地缓冲区的递增索引。 */
    /* ESP-MQTT 的 offset 允许分片乱序/间隔到达，按原始 offset 写入而不是简单追加。 */
    memcpy(s_payload + event->current_data_offset, event->data, event->data_len);

    if (event->current_data_offset + event->data_len == s_payload_len) {
        /* 只有完整消息到齐后才追加 NUL 并路由，防止把半条消息交给业务模块。
         * handler 与长度必须在 reset 前快照：reset 会清空接收状态。 */
        s_payload[s_payload_len] = '\0';
        mqtt_inbound_handler_t handler = s_registered_topics[s_active_topic].handler;
        size_t payload_len = (size_t)s_payload_len;
        mqtt_reset_payload();
        handler(s_payload, payload_len);
    }
}

/**
 * @brief 处理 ESP-MQTT 客户端事件。
 *
 * CONNECTED 时订阅设备专属响应 topic；SUBSCRIBED 时设置就绪位并唤醒主动检查；
 * DATA 时交给分片重组函数；断线时清除接收状态并暂停周期检查；ERROR 时记录官方
 * 错误句柄提供的 TLS 和 socket 诊断信息。
 *
 * @param[in] handler_args 注册事件时提供的用户参数，本实现未使用。
 * @param[in] base         事件基，正常情况下为 MQTT_EVENTS。
 * @param[in] event_id     esp_mqtt_event_id_t 事件编号。
 * @param[in] event_data   指向 esp_mqtt_event_t 的事件数据。
 *
 * @note 回调运行在 ESP-MQTT 任务上下文，不允许阻塞等待 OTA 完成。
 * @note 与检查任务共享事件组和任务通知，均使用 FreeRTOS 线程安全接口同步。
 */
static void mqtt_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id,
                               void *event_data)
{
    (void)handler_args;
    (void)base;

    esp_mqtt_event_handle_t event = event_data;
    /* 事件回调只推进连接/接收状态；固件下载由 native OTA 任务异步执行。 */
    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED: {
        /* 每次重连都重新订阅全部已注册 topic，并等待全部 critical SUBACK，
         * 防止只连接成功就误发设备专属消息。订阅调用可能阻塞，绝不能在
         * 自旋锁临界区内执行；sub_msg_id 的常规写只发生在 MQTT 事件任务内。 */
        ESP_LOGI(TAG, "MQTT_EVENT_CONNECTED");
        xEventGroupClearBits(s_connection_events, MQTT_OTA_READY_BIT |
                             MQTT_OTA_RESPONSE_BIT | MQTT_OTA_RESPONSE_REJECTED_BIT);
        bool critical_subscribe_failed = false;
        for (size_t i = 0; i < s_registered_topic_count; i++) {
            int msg_id = esp_mqtt_client_subscribe(event->client,
                                                   s_registered_topics[i].topic, 1);
            s_registered_topics[i].sub_msg_id = msg_id;
            ESP_LOGI(TAG, "Subscribing to %s, msg_id=%d",
                     s_registered_topics[i].topic, msg_id);
            if (msg_id < 0 && s_registered_topics[i].critical) {
                /* critical 订阅失败必须重建连接；非 critical topic 下次重连再试。 */
                ESP_LOGW(TAG, "Critical topic subscribe call failed: %s",
                         s_registered_topics[i].topic);
                critical_subscribe_failed = true;
            }
        }
        if (critical_subscribe_failed) {
            mqtt_request_reconnect("Critical topic subscribe call failed");
        } else {
            s_suback_deadline_ms = esp_timer_get_time() / 1000LL +
                                   (int64_t)CONFIG_COMM_MQTT_SUBACK_TIMEOUT_SECONDS * 1000LL;
            if (s_check_task != NULL) {
                xTaskNotifyGive(s_check_task);
            }
        }
        break;
    }
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT_EVENT_DISCONNECTED; client will reconnect automatically");
        mqtt_reset_payload();
        mqtt_status_handle_disconnected();
        mqtt_reset_sub_msg_ids();
        s_suback_deadline_ms = 0;
        xEventGroupClearBits(s_connection_events, MQTT_OTA_READY_BIT |
                             MQTT_OTA_RESPONSE_BIT | MQTT_OTA_RESPONSE_REJECTED_BIT);
        mqtt_check_request_session_reset();
        if (s_check_task != NULL) {
            xTaskNotifyGive(s_check_task);
        }
        break;
    case MQTT_EVENT_SUBSCRIBED: {
        ESP_LOGI(TAG, "MQTT_EVENT_SUBSCRIBED, msg_id=%d", event->msg_id);
        bool matched = false;
        portENTER_CRITICAL(&s_registry_lock);
        for (size_t i = 0; i < s_registered_topic_count; i++) {
            if (event->msg_id >= 0 &&
                s_registered_topics[i].sub_msg_id == event->msg_id) {
                s_registered_topics[i].sub_msg_id = -1;
                matched = true;
                break;
            }
        }
        portEXIT_CRITICAL(&s_registry_lock);
        if (!matched) {
            ESP_LOGW(TAG, "Ignoring unrelated SUBACK, msg_id=%d", event->msg_id);
            break;
        }
        if (s_suback_deadline_ms != 0 && mqtt_all_critical_subscribed()) {
            /* 全部 critical topic 都确认后才同时放行状态 flush 和主动检查任务。 */
            xEventGroupSetBits(s_connection_events, MQTT_OTA_READY_BIT);
            s_suback_deadline_ms = 0;
            /* 只入队持久化事件和最新进度；不在回调里访问 Flash 或等待发送。 */
            (void)native_ota_report_flush_pending();
            if (s_check_task != NULL) {
                xTaskNotifyGive(s_check_task);
            }
            if (s_status_task != NULL) {
                xTaskNotifyGive(s_status_task);
            }
        }
        break;
    }
    case MQTT_EVENT_PUBLISHED:
        mqtt_status_handle_published(event->msg_id);
        break;
    case MQTT_EVENT_DATA:
        mqtt_handle_data(event);
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "MQTT_EVENT_ERROR");
        if (event->error_handle != NULL &&
            event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
            ESP_LOGE(TAG, "esp-tls error: 0x%x, tls stack error: 0x%x, socket errno: %d",
                     event->error_handle->esp_tls_last_esp_err,
                     event->error_handle->esp_tls_stack_err,
                     event->error_handle->esp_transport_sock_errno);
        }
        break;
    default:
        break;
    }
}

/**
 * @brief 按 Kconfig 参数创建主动检查任务、注册事件并启动 ESP-MQTT 客户端。
 *
 * 启动前根据芯片唯一 ID 生成设备专属响应 topic，并创建事件组和周期检查任务。
 * MQTT 使用官方自动重连；初始化中任一步失败都会按创建顺序释放客户端、
 * 任务和事件组，避免留下访问无效句柄的后台任务。
 *
 * @return ESP_OK 客户端与主动检查任务均已启动。
 * @return ESP_ERR_INVALID_SIZE 响应 topic 超出本地缓冲区。
 * @return ESP_ERR_NO_MEM 无法创建事件组或任务。
 * @return ESP_FAIL ESP-MQTT 客户端初始化失败。
 * @return 其他 esp_err_t 设备 ID、事件注册或客户端启动失败的错误码。
 *
 * @note 必须在 NVS、默认事件循环和网络连接初始化完成后调用。
 * @note 本函数不允许在中断上下文中调用。
 */
esp_err_t mqtt_comm_start(void)
{
    portENTER_CRITICAL(&s_start_lock);
    if (s_started) {
        portEXIT_CRITICAL(&s_start_lock);
        return ESP_OK;
    }
    if (s_starting) {
        portEXIT_CRITICAL(&s_start_lock);
        return ESP_ERR_INVALID_STATE;
    }
    s_starting = true;
    portEXIT_CRITICAL(&s_start_lock);

    /* 设备 ID 同时作为 client_id 和专属 topic 尾部，确保响应/通知不会串到其他设备。 */
    char device_id[NATIVE_OTA_DEVICE_ID_SIZE];
    esp_err_t err = native_ota_get_device_id(device_id, sizeof(device_id));
    if (err != ESP_OK) {
        return mqtt_start_finish(err);
    }

    /* topic_len 是 snprintf 生成的有效字符数，不包含末尾 NUL。 */
    int topic_len = snprintf(s_response_topic, sizeof(s_response_topic), "%s/%s",
                             CONFIG_COMM_MQTT_OTA_RESPONSE_TOPIC_PREFIX, device_id);
    if (topic_len <= 0 || (size_t)topic_len >= sizeof(s_response_topic)) {
        return mqtt_start_finish(ESP_ERR_INVALID_SIZE);
    }
    topic_len = snprintf(s_notify_topic, sizeof(s_notify_topic), "%s/%s",
                         CONFIG_COMM_MQTT_OTA_NOTIFY_TOPIC_PREFIX, device_id);
    if (topic_len <= 0 || (size_t)topic_len >= sizeof(s_notify_topic)) {
        return mqtt_start_finish(ESP_ERR_INVALID_SIZE);
    }
    topic_len = snprintf(s_status_topic, sizeof(s_status_topic), "%s/%s",
                         CONFIG_COMM_MQTT_OTA_STATUS_TOPIC_PREFIX, device_id);
    if (topic_len <= 0 || (size_t)topic_len >= sizeof(s_status_topic)) {
        return mqtt_start_finish(ESP_ERR_INVALID_SIZE);
    }
    strncpy(s_client_id, device_id, sizeof(s_client_id) - 1);
    s_client_id[sizeof(s_client_id) - 1] = '\0';

    /* 注册 OTA 专属下行 topic；语音命令等业务 topic 由各业务模块在启动前自行注册。 */
    err = mqtt_comm_register_topic(s_response_topic, NATIVE_OTA_JSON_MAX_LEN, true,
                                   mqtt_handle_ota_response);
    if (err != ESP_OK) {
        return mqtt_start_finish(err);
    }
    err = mqtt_comm_register_topic(s_notify_topic, MQTT_OTA_NOTIFY_MAX_LEN, true,
                                   mqtt_handle_notify_json);
    if (err != ESP_OK) {
        return mqtt_start_finish(err);
    }

    s_connection_events = xEventGroupCreate();
    if (s_connection_events == NULL) {
        return mqtt_start_finish(ESP_ERR_NO_MEM);
    }

    s_status_lock = xSemaphoreCreateMutex();
    s_status_queue = xQueueCreate(CONFIG_OTA_REPORT_QUEUE_DEPTH,
                                  sizeof(native_ota_report_message_t));
    s_status_ack_queue = xQueueCreate(MQTT_STATUS_TRACK_SIZE, sizeof(int));
    if (s_status_lock == NULL || s_status_queue == NULL || s_status_ack_queue == NULL) {
        mqtt_status_cleanup();
        vEventGroupDelete(s_connection_events);
        s_connection_events = NULL;
        return mqtt_start_finish(ESP_ERR_NO_MEM);
    }
    if (xTaskCreate(mqtt_status_task, "ota_status_task", 4096, NULL, 4,
                    &s_status_task) != pdPASS) {
        mqtt_status_cleanup();
        vEventGroupDelete(s_connection_events);
        s_connection_events = NULL;
        return mqtt_start_finish(ESP_ERR_NO_MEM);
    }

    err = native_ota_report_set_transport(mqtt_status_transport, NULL);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "OTA report transport is unavailable: %s", esp_err_to_name(err));
    }

    if (xTaskCreate(mqtt_ota_check_task, "ota_check_task", 4096, NULL, 4,
                    &s_check_task) != pdPASS) {
        (void)native_ota_report_set_transport(NULL, NULL);
        mqtt_status_cleanup();
        vEventGroupDelete(s_connection_events);
        s_connection_events = NULL;
        return mqtt_start_finish(ESP_ERR_NO_MEM);
    }

    /* 凭据只保留指针，来源为 Kconfig 或构建嵌入区，不复制、不打印秘密内容。 */
    const char *mqtt_username = NULL;
    const char *mqtt_password = NULL;
    const char *mqtt_client_cert = NULL;
    const char *mqtt_client_key = NULL;
    err = mqtt_get_device_auth(&mqtt_username, &mqtt_password,
                               &mqtt_client_cert, &mqtt_client_key);
    if (err != ESP_OK) {
        vTaskDelete(s_check_task);
        s_check_task = NULL;
        (void)native_ota_report_set_transport(NULL, NULL);
        mqtt_status_cleanup();
        vEventGroupDelete(s_connection_events);
        s_connection_events = NULL;
        return mqtt_start_finish(err);
    }

    /* keepalive 和自动重连由 ESP-MQTT 执行；本模块只在连接/订阅事件上推进业务状态。 */
    /* 与 WSS/OTA 同一信任锚：内嵌 server_certs/ca_cert.pem（自签服务器，
     * 如 172.20.10.2）；不用 esp_crt_bundle（公网 CA 不信任自签）。 */
    extern const unsigned char ca_cert_pem_start[] asm("_binary_ca_cert_pem_start");
    extern const unsigned char ca_cert_pem_end[] asm("_binary_ca_cert_pem_end");
    const esp_mqtt_client_config_t mqtt_cfg = {
        .broker = {
            .address.uri = CONFIG_COMM_MQTT_BROKER_URI,
            .verification.certificate = (const char *)ca_cert_pem_start,
            .verification.certificate_len = (size_t)(ca_cert_pem_end - ca_cert_pem_start),
            .verification.skip_cert_common_name_check = true,
        },
        .credentials = {
            .client_id = s_client_id,
            .username = mqtt_username,
            .authentication.password = mqtt_password,
            .authentication.certificate = mqtt_client_cert,
            .authentication.key = mqtt_client_key,
        },
        .session = {
            .keepalive = CONFIG_COMM_HEARTBEAT_INTERVAL_SECONDS,
            .disable_keepalive = false,
        },
        .network = {
            .reconnect_timeout_ms = CONFIG_COMM_RECONNECT_INTERVAL_SECONDS * 1000,
            .disable_auto_reconnect = false,
        },
    };

    s_client = esp_mqtt_client_init(&mqtt_cfg);
    if (s_client == NULL) {
        vTaskDelete(s_check_task);
        s_check_task = NULL;
        (void)native_ota_report_set_transport(NULL, NULL);
        mqtt_status_cleanup();
        vEventGroupDelete(s_connection_events);
        s_connection_events = NULL;
        return mqtt_start_finish(ESP_FAIL);
    }

    err = esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    if (err != ESP_OK) {
        esp_mqtt_client_destroy(s_client);
        s_client = NULL;
        vTaskDelete(s_check_task);
        s_check_task = NULL;
        (void)native_ota_report_set_transport(NULL, NULL);
        mqtt_status_cleanup();
        vEventGroupDelete(s_connection_events);
        s_connection_events = NULL;
        return mqtt_start_finish(err);
    }

    err = esp_mqtt_client_start(s_client);
    if (err != ESP_OK) {
        esp_mqtt_client_destroy(s_client);
        s_client = NULL;
        vTaskDelete(s_check_task);
        s_check_task = NULL;
        (void)native_ota_report_set_transport(NULL, NULL);
        mqtt_status_cleanup();
        vEventGroupDelete(s_connection_events);
        s_connection_events = NULL;
    }
    return mqtt_start_finish(err);
}

/**
 * @brief 以 QoS 1、非 retain 方式发布一条 MQTT 消息（通用尽力而为通道）。
 *
 * 直接交给 ESP-MQTT 发送队列，不跟踪 PUBACK，也不做 NVS 持久化。用于音频状态
 * 等辅助上报；OTA 关键生命周期事件走 ota_report 的可靠 transport，不经本函数。
 */
esp_err_t mqtt_comm_publish(const char *topic, const char *data, size_t data_len)
{
    if (topic == NULL || topic[0] == '\0' || data == NULL || data_len == 0U ||
        data_len > (size_t)INT_MAX) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_client == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    int msg_id = esp_mqtt_client_enqueue(s_client, topic, data, (int)data_len, 1, 0, true);
    if (msg_id < 0) {
        ESP_LOGW(TAG, "MQTT publish enqueue failed for topic %s", topic);
        return ESP_FAIL;
    }
    return ESP_OK;
}

/**
 * @brief 网络 IP 就绪回调适配器：转调 mqtt_comm_start()。
 */
esp_err_t mqtt_comm_ip_ready(void *arg)
{
    (void)arg;
    return mqtt_comm_start();
}
