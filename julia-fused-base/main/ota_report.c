/**
 * @file    ota_report.c
 * @brief   OTA 状态 JSON、进度节流和关键事件持久化实现。
 *
 * 主要职责：
 * 1. 将 OTA 任务上下文和生命周期状态序列化为固定上限的状态 JSON；
 * 2. 关键事件先保存到 ota_report NVS blob，确保断电或 MQTT 断线后可以重发；
 * 3. 普通下载进度只保留最新 RAM 槽位，并按百分比步长/时间间隔节流；
 * 4. 通过非阻塞 transport 将消息交给通信模块，并把 PUBACK 交给独立任务处理。
 *
 * 模块关系：
 * - native_ota_example.c 在下载和启动验收阶段提交事件；
 * - mqtt_comm.c 注册 transport，负责 MQTT 队列、msg_id 与 event_id 的关联；
 * - ota_state_store.c 提供共享的 NVS 容量与写入失败诊断；
 * - 本文件不调用 ESP-MQTT，不执行 HTTP/Flash 操作。
 *
 * 并发约束：NVS 内存镜像、transport 和进度基线由 s_lock 保护；MQTT PUBACK 只入
 * 有界队列，实际删除 NVS 事件由 ota_report_ack_task 完成，因此事件回调不会写 Flash。
 */

#include "ota_report.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "cJSON.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "nvs.h"

#include "ota_state_store.h"

/** 本模块统一使用的日志标签。 */
static const char *TAG = "ota_report";

/** ota_report NVS blob 的布局版本，改变结构体布局时必须递增。 */
#define OTA_REPORT_STORE_SCHEMA_VERSION 1U
/** 保存关键状态事件和跨重启上下文的独立 NVS namespace。 */
#define OTA_REPORT_NAMESPACE "ota_report"
/** namespace 内完整状态 blob 的键名。 */
#define OTA_REPORT_KEY "state"
/** PUBACK 事件 ID 的 FreeRTOS 队列深度；队列满时由 NVS 保证重发。 */
#define OTA_REPORT_ACK_QUEUE_DEPTH 8

/**
 * @brief 持久化队列中的单条关键状态事件。
 *
 * JSON 与 event_id 一起保存，PUBACK 到达后按 event_id 删除；reserved 用于保持
 * 结构体对齐并为布局兼容保留空间。
 */
typedef struct {
    uint8_t state; /**< native_ota_report_state_t 的持久化值。 */
    uint8_t reserved[3]; /**< 对齐保留字段，当前不参与事件匹配。 */
    uint32_t json_len; /**< JSON 有效长度，不含末尾 NUL，单位为字节。 */
    char event_id[NATIVE_OTA_EVENT_ID_SIZE]; /**< 云端幂等关联 ID。 */
    char json[NATIVE_OTA_STATUS_JSON_SIZE]; /**< NUL 结尾的完整状态 JSON。 */
} ota_report_pending_item_t;

/**
 * @brief ota_report NVS namespace 中保存的完整内存镜像。
 *
 * 该结构以单个 blob 保存，count 表示 pending 数组前缀中有效元素数量；context 和
 * awaiting_boot 用于重启后继续生成 booted_pending_verify/succeeded/rolled_back 事件。
 */
typedef struct {
    uint32_t schema_version; /**< 持久化布局版本。 */
    uint8_t count; /**< pending 数组中有效事件数，范围为 0～NATIVE_OTA_REPORT_PENDING_MAX。 */
    uint8_t context_active; /**< 是否存在可用于跨重启上报的 OTA 上下文。 */
    uint8_t awaiting_boot; /**< 是否等待新镜像启动验收结果。 */
    uint8_t reserved; /**< 对齐保留字段，当前不参与状态恢复。 */
    native_ota_report_context_t context; /**< 当前 OTA 任务的稳定关联上下文。 */
    ota_report_pending_item_t pending[NATIVE_OTA_REPORT_PENDING_MAX]; /**< 待 PUBACK 关键事件。 */
} ota_report_store_t;

/** ota_report NVS blob 的 RAM 镜像；仅在 s_lock 持有时读写。 */
static ota_report_store_t s_store;
/** 保护 s_store、transport 和进度槽位的互斥锁。 */
static SemaphoreHandle_t s_lock;
/** MQTT PUBACK 事件 ID 的内部接收队列。 */
static QueueHandle_t s_ack_queue;
/** PUBACK 处理任务句柄；仅用于任务生命周期诊断。 */
static TaskHandle_t s_ack_task;
/** 报告模块是否完成同步对象、NVS 镜像和确认任务初始化。 */
static bool s_initialized;

/** 当前通信层提供的非阻塞发送回调；访问时需要持有 s_lock。 */
static native_ota_report_transport_t s_transport;
/** 传给 s_transport 的不透明上下文；访问时需要持有 s_lock。 */
static void *s_transport_context;

/* 进度永远只保留最新一条；它不进入 s_store，也不触发 NVS 写入。 */
static native_ota_report_message_t s_latest_progress;
/** s_latest_progress 是否已经构建且尚未交给 transport。 */
static bool s_progress_valid;
/** 上一次提交进度对应的整数百分比。 */
static uint32_t s_last_progress_percent;
/** 上一次提交进度的单调运行时间，单位为 ms。 */
static int64_t s_last_progress_ms;

/**
 * @brief 在有限时间内取得报告模块互斥锁。
 *
 * @return true 成功取得锁。
 * @return false 模块未初始化锁，或 100 ms 内未取得锁。
 *
 * @note 只用于普通任务上下文；失败时调用者不得访问受保护状态。
 */
static bool ota_report_take_lock(void)
{
    return s_lock != NULL && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE;
}

/**
 * @brief 释放报告模块互斥锁。
 *
 * @note 传入的锁可能尚未创建；此时函数不执行任何操作，便于失败清理路径调用。
 */
static void ota_report_give_lock(void)
{
    if (s_lock != NULL) {
        xSemaphoreGive(s_lock);
    }
}

/**
 * @brief 从 ota_report NVS namespace 加载持久化状态到 RAM 镜像。
 *
 * @return ESP_OK 成功加载、namespace/键不存在或已恢复为空状态。
 * @return ESP_ERR_INVALID_VERSION blob 大小、schema 或事件项格式不兼容。
 * @return 其他 esp_err_t NVS 打开或读取失败。
 *
 * @note 调用前不需要 s_lock，因为该函数只在初始化阶段运行；调用方必须先初始化 NVS。
 */
static esp_err_t ota_report_store_load(void)
{
    memset(&s_store, 0, sizeof(s_store));
    s_store.schema_version = OTA_REPORT_STORE_SCHEMA_VERSION;

    nvs_handle_t handle;
    esp_err_t err = nvs_open(OTA_REPORT_NAMESPACE, NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (err != ESP_OK) {
        return err;
    }

    size_t size = sizeof(s_store);
    err = nvs_get_blob(handle, OTA_REPORT_KEY, &s_store, &size);
    nvs_close(handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        memset(&s_store, 0, sizeof(s_store));
        s_store.schema_version = OTA_REPORT_STORE_SCHEMA_VERSION;
        return ESP_OK;
    }
    if (err != ESP_OK || size != sizeof(s_store) ||
        s_store.schema_version != OTA_REPORT_STORE_SCHEMA_VERSION ||
        s_store.count > NATIVE_OTA_REPORT_PENDING_MAX) {
        ESP_LOGW(TAG, "Ignoring invalid OTA report store");
        memset(&s_store, 0, sizeof(s_store));
        s_store.schema_version = OTA_REPORT_STORE_SCHEMA_VERSION;
        return err == ESP_OK ? ESP_ERR_INVALID_VERSION : err;
    }

    /* 每个事件都必须自洽，防止重启后按错误长度读取 JSON 或错误关联 event_id。 */
    for (uint8_t i = 0; i < s_store.count; ++i) {
        ota_report_pending_item_t *item = &s_store.pending[i];
        if (item->json_len == 0 || item->json_len >= NATIVE_OTA_STATUS_JSON_SIZE ||
            item->event_id[0] == '\0' || item->json[item->json_len] != '\0') {
            ESP_LOGW(TAG, "Ignoring malformed OTA report item %u", (unsigned)i);
            memset(&s_store, 0, sizeof(s_store));
            s_store.schema_version = OTA_REPORT_STORE_SCHEMA_VERSION;
            return ESP_ERR_INVALID_VERSION;
        }
    }
    return ESP_OK;
}

/**
 * @brief 在持有 s_lock 时把当前 RAM 镜像写入 NVS。
 *
 * @return ESP_OK blob 写入和 nvs_commit() 均成功。
 * @return 其他 esp_err_t NVS 打开、写入或提交失败。
 *
 * @warning 调用者必须已经持有 s_lock；函数会写 Flash，不能在中断上下文调用。
 */
static esp_err_t ota_report_store_save_locked(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(OTA_REPORT_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ota_state_store_log_nvs_usage(TAG, "opening OTA report store for write", err);
        return err;
    }

    err = nvs_set_blob(handle, OTA_REPORT_KEY, &s_store, sizeof(s_store));
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err != ESP_OK) {
        ota_state_store_log_nvs_usage(TAG, "persisting OTA report state", err);
    }
    return err;
}

/**
 * @brief 在持有 s_lock 时查找待确认事件。
 *
 * @param[in] event_id NUL 结尾事件 ID，不允许为 NULL。
 * @return 0～count-1 对应的数组下标；-1 表示未找到。
 */
static int ota_report_find_pending_locked(const char *event_id)
{
    for (uint8_t i = 0; i < s_store.count; ++i) {
        if (strcmp(s_store.pending[i].event_id, event_id) == 0) {
            return (int)i;
        }
    }
    return -1;
}

/**
 * @brief 判断生命周期状态是否属于终态。
 *
 * @param[in] state 待判断状态。
 * @return true succeeded、failed 或 rolled_back。
 * @return false 仍可能继续推进的状态，包括 deferred。
 */
static bool ota_report_state_is_terminal(native_ota_report_state_t state)
{
    return state == NATIVE_OTA_REPORT_SUCCEEDED ||
           state == NATIVE_OTA_REPORT_FAILED ||
           state == NATIVE_OTA_REPORT_ROLLED_BACK;
}

/**
 * @brief 生成 128-bit 随机事件 ID。
 *
 * @param[out] output 接收 32 个小写十六进制字符和末尾 NUL 的缓冲区，不允许为 NULL。
 *
 * @note 事件 ID 用于重复上报幂等关联，不承载安全认证用途。
 */
static void ota_report_make_event_id(char output[NATIVE_OTA_EVENT_ID_SIZE])
{
    uint32_t random_words[4] = {
        esp_random(), esp_random(), esp_random(), esp_random(),
    };
    (void)snprintf(output, NATIVE_OTA_EVENT_ID_SIZE,
                   "%08" PRIx32 "%08" PRIx32 "%08" PRIx32 "%08" PRIx32,
                   random_words[0], random_words[1], random_words[2], random_words[3]);
}

/**
 * @brief 将生命周期状态转换为协议名称。
 *
 * @param[in] state 状态枚举值。
 * @return 静态只读字符串；未知值返回 `unknown`。
 */
const char *native_ota_report_state_name(native_ota_report_state_t state)
{
    switch (state) {
    case NATIVE_OTA_REPORT_ACCEPTED: return "accepted";
    case NATIVE_OTA_REPORT_DOWNLOADING: return "downloading";
    case NATIVE_OTA_REPORT_VERIFYING: return "verifying";
    case NATIVE_OTA_REPORT_REBOOTING: return "rebooting";
    case NATIVE_OTA_REPORT_BOOTED_PENDING_VERIFY: return "booted_pending_verify";
    case NATIVE_OTA_REPORT_SUCCEEDED: return "succeeded";
    case NATIVE_OTA_REPORT_FAILED: return "failed";
    case NATIVE_OTA_REPORT_ROLLED_BACK: return "rolled_back";
    case NATIVE_OTA_REPORT_DEFERRED: return "deferred";
    default: return "unknown";
    }
}

/**
 * @brief 构建一条完整的 ota_status JSON 消息。
 *
 * @param[in]  context         OTA 稳定关联上下文，不允许为 NULL。
 * @param[in]  state            生命周期状态。
 * @param[in]  bytes_downloaded 已下载字节数；仅 downloading 状态会写入 JSON。
 * @param[in]  failure_reason   失败分类；正常状态应传 NATIVE_OTA_FAILURE_NONE。
 * @param[out] message          接收事件 ID、状态标志和序列化 JSON，不允许为 NULL。
 * @return ESP_OK 消息在固定缓冲区内构建成功。
 * @return ESP_ERR_INVALID_ARG 上下文缺少关联字段。
 * @return ESP_ERR_NO_MEM cJSON 节点创建失败。
 * @return ESP_ERR_INVALID_SIZE JSON 超过固定上限。
 *
 * @note 只构建内存消息，不写 NVS、不调用 transport。
 */
static esp_err_t ota_report_build_message(const native_ota_report_context_t *context,
                                          native_ota_report_state_t state,
                                          uint32_t bytes_downloaded,
                                          native_ota_failure_reason_t failure_reason,
                                          native_ota_report_message_t *message)
{
    if (context == NULL || message == NULL || context->request_id[0] == '\0' ||
        context->artifact_id[0] == '\0' || context->target_version[0] == '\0' ||
        context->current_version[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    memset(message, 0, sizeof(*message));
    message->critical = true;
    message->state = state;
    ota_report_make_event_id(message->event_id);

    char device_id[NATIVE_OTA_DEVICE_ID_SIZE];
    esp_err_t err = native_ota_get_device_id(device_id, sizeof(device_id));
    if (err != ESP_OK) {
        return err;
    }

    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }

    bool added = cJSON_AddStringToObject(root, "type", "ota_status") != NULL &&
                 cJSON_AddNumberToObject(root, "schema_version", 1) != NULL &&
                 cJSON_AddStringToObject(root, "event_id", message->event_id) != NULL &&
                 cJSON_AddStringToObject(root, "device_id", device_id) != NULL &&
                 cJSON_AddStringToObject(root, "request_id", context->request_id) != NULL &&
                 cJSON_AddStringToObject(root, "artifact_id", context->artifact_id) != NULL &&
                 cJSON_AddStringToObject(root, "product", context->product) != NULL &&
                 cJSON_AddStringToObject(root, "hardware_version", context->hardware_version) != NULL &&
                 cJSON_AddStringToObject(root, "current_version", context->current_version) != NULL &&
                 cJSON_AddStringToObject(root, "target_version", context->target_version) != NULL &&
                 cJSON_AddStringToObject(root, "state", native_ota_report_state_name(state)) != NULL &&
                 cJSON_AddNumberToObject(root, "attempt", context->attempt == 0 ? 1 : context->attempt) != NULL &&
                 cJSON_AddStringToObject(root, "error_code",
                                         native_ota_failure_reason_name(failure_reason)) != NULL &&
                 cJSON_AddNumberToObject(root, "uptime_ms",
                                         (double)(esp_timer_get_time() / 1000LL)) != NULL;

    if (added && context->job_id[0] != '\0') {
        added = cJSON_AddStringToObject(root, "job_id", context->job_id) != NULL;
    }

    if (added && state == NATIVE_OTA_REPORT_DOWNLOADING) {
        /* 百分比使用整数除法并钳制到 100，避免错误调用导致协议出现超过 100 的进度。 */
        uint32_t image_size = context->image_size;
        uint32_t progress = image_size == 0 ? 0 :
            (uint32_t)(((uint64_t)bytes_downloaded * 100U) / image_size);
        if (progress > 100U) {
            progress = 100U;
        }
        added = cJSON_AddNumberToObject(root, "bytes_downloaded", bytes_downloaded) != NULL &&
                cJSON_AddNumberToObject(root, "image_size", image_size) != NULL &&
                cJSON_AddNumberToObject(root, "progress_percent", progress) != NULL;
    }

    if (!added || !cJSON_PrintPreallocated(root, message->json,
                                           sizeof(message->json), false)) {
        cJSON_Delete(root);
        return added ? ESP_ERR_INVALID_SIZE : ESP_ERR_NO_MEM;
    }
    message->json_len = strlen(message->json);
    if (message->json_len == 0 || message->json_len >= sizeof(message->json)) {
        cJSON_Delete(root);
        return ESP_ERR_INVALID_SIZE;
    }
    cJSON_Delete(root);
    return ESP_OK;
}

/**
 * @brief 将一条已构建消息提交给当前 transport。
 *
 * @param[in] message 已序列化消息，不允许为 NULL。
 * @return ESP_OK transport 已接收。
 * @return ESP_ERR_NOT_SUPPORTED 尚未注册 transport，消息仍保留在本地。
 * @return ESP_ERR_INVALID_STATE 无法取得内部锁或模块状态无效。
 * @return 其他 esp_err_t transport 返回的错误。
 *
 * @note 函数只复制 transport 指针后调用，不在此处写 Flash。
 */
static esp_err_t ota_report_submit(const native_ota_report_message_t *message)
{
    native_ota_report_transport_t transport;
    void *transport_context;
    if (!ota_report_take_lock()) {
        return ESP_ERR_INVALID_STATE;
    }
    transport = s_transport;
    transport_context = s_transport_context;
    ota_report_give_lock();

    if (transport == NULL) {
        ESP_LOGI(TAG, "No OTA report transport is ready; event %s is retained",
                 message->event_id);
        return ESP_ERR_NOT_SUPPORTED;
    }
    return transport(message, transport_context);
}

/**
 * @brief 异步处理 MQTT PUBACK 对应的 event_id 并删除已确认事件。
 *
 * @param[in] parameter FreeRTOS 任务参数，本实现未使用。
 *
 * @note 任务阻塞等待 s_ack_queue；只有 NVS 删除成功才从内存镜像移除事件，删除失败
 *       会恢复数组项，使其能够在重启后重发。
 */
static void ota_report_ack_task(void *parameter)
{
    (void)parameter;
    char event_id[NATIVE_OTA_EVENT_ID_SIZE];
    while (xQueueReceive(s_ack_queue, event_id, portMAX_DELAY) == pdTRUE) {
        if (!ota_report_take_lock()) {
            continue;
        }
        /* 先从 RAM 镜像移除并持久化；保存失败时恢复数组，保持重试语义。 */
        int index = ota_report_find_pending_locked(event_id);
        if (index >= 0) {
            ota_report_pending_item_t removed = s_store.pending[index];
            for (uint8_t i = (uint8_t)index; i + 1U < s_store.count; ++i) {
                s_store.pending[i] = s_store.pending[i + 1U];
            }
            s_store.count--;
            if (ota_report_store_save_locked() != ESP_OK) {
                /* 失败时恢复内存项；NVS 中原记录仍然会在重启后重发。 */
                for (uint8_t i = s_store.count; i > (uint8_t)index; --i) {
                    s_store.pending[i] = s_store.pending[i - 1U];
                }
                s_store.pending[index] = removed;
                s_store.count++;
            }
        }
        ota_report_give_lock();
    }
    vTaskDelete(NULL);
}

/**
 * @brief 初始化报告模块的锁、NVS 镜像、PUBACK 队列和后台确认任务。
 *
 * @return ESP_OK 初始化成功或已经初始化。
 * @return ESP_ERR_NO_MEM FreeRTOS 对象或确认任务创建失败。
 * @return 其他 esp_err_t NVS 状态读取错误（会记录日志并继续使用空镜像）。
 *
 * @note 必须在 nvs_flash_init() 成功后调用；函数不创建 MQTT 连接。
 */
esp_err_t native_ota_report_init(void)
{
    if (s_initialized) {
        return ESP_OK;
    }

    /* 先创建同步对象，再读取 NVS；任何对象创建失败都按相反顺序清理。 */
    s_lock = xSemaphoreCreateMutex();
    s_ack_queue = xQueueCreate(OTA_REPORT_ACK_QUEUE_DEPTH, NATIVE_OTA_EVENT_ID_SIZE);
    if (s_lock == NULL || s_ack_queue == NULL) {
        if (s_ack_queue != NULL) {
            vQueueDelete(s_ack_queue);
            s_ack_queue = NULL;
        }
        if (s_lock != NULL) {
            vSemaphoreDelete(s_lock);
            s_lock = NULL;
        }
        return ESP_ERR_NO_MEM;
    }

    esp_err_t err = ota_report_store_load();
    if (err != ESP_OK && err != ESP_ERR_INVALID_VERSION) {
        ESP_LOGW(TAG, "Could not load OTA report store: %s", esp_err_to_name(err));
    }

    if (xTaskCreate(ota_report_ack_task, "ota_report_ack", 4096, NULL, 3,
                    &s_ack_task) != pdPASS) {
        vQueueDelete(s_ack_queue);
        vSemaphoreDelete(s_lock);
        s_ack_queue = NULL;
        s_lock = NULL;
        return ESP_ERR_NO_MEM;
    }
    s_initialized = true;
    return ESP_OK;
}

/**
 * @brief 设置当前通信 transport。
 *
 * @param[in] transport 非阻塞发送回调，可为 NULL 以暂时停用发送。
 * @param[in] context transport 私有上下文，可为 NULL。
 * @return ESP_OK 设置成功。
 * @return ESP_ERR_INVALID_STATE 报告模块尚未初始化。
 * @return ESP_ERR_TIMEOUT 获取 s_lock 超时。
 */
esp_err_t native_ota_report_set_transport(native_ota_report_transport_t transport,
                                           void *context)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!ota_report_take_lock()) {
        return ESP_ERR_TIMEOUT;
    }
    s_transport = transport;
    s_transport_context = context;
    ota_report_give_lock();
    return ESP_OK;
}

/**
 * @brief 从 manifest 和当前版本复制一次 OTA 的跨模块报告上下文。
 *
 * @param[out] context 输出上下文，不允许为 NULL。
 * @param[in] manifest 已校验清单，不允许为 NULL。
 * @param[in] current_version 当前运行版本，不允许为空。
 * @return ESP_OK 初始化成功；ESP_ERR_INVALID_ARG 参数或关联字段无效。
 *
 * @note 只操作调用者内存，初始 attempt 固定为 1。
 */
esp_err_t native_ota_report_context_init(native_ota_report_context_t *context,
                                          const native_ota_manifest_t *manifest,
                                          const char *current_version)
{
    if (context == NULL || manifest == NULL || current_version == NULL ||
        current_version[0] == '\0' || manifest->request_id[0] == '\0' ||
        manifest->artifact_id[0] == '\0' || manifest->version[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    memset(context, 0, sizeof(*context));
    strncpy(context->job_id, manifest->job_id, sizeof(context->job_id) - 1U);
    strncpy(context->request_id, manifest->request_id, sizeof(context->request_id) - 1U);
    strncpy(context->artifact_id, manifest->artifact_id, sizeof(context->artifact_id) - 1U);
    strncpy(context->product, manifest->product, sizeof(context->product) - 1U);
    strncpy(context->hardware_version, manifest->hardware_version,
            sizeof(context->hardware_version) - 1U);
    strncpy(context->current_version, current_version, sizeof(context->current_version) - 1U);
    strncpy(context->target_version, manifest->version, sizeof(context->target_version) - 1U);
    context->image_size = manifest->image_size;
    context->attempt = 1;
    return ESP_OK;
}

/**
 * @brief 生成、持久化并提交一条关键 OTA 生命周期事件。
 *
 * @param[in] context          OTA 关联上下文，不允许为 NULL。
 * @param[in] state            生命周期状态。
 * @param[in] bytes_downloaded 已下载字节数；非 downloading 状态通常传 0。
 * @param[in] failure_reason   错误分类；成功或正常状态传 NONE。
 * @return ESP_OK 事件已保存并提交给 transport，或 transport 尚未就绪但事件已保留。
 * @return 其他 esp_err_t 消息构建、锁、NVS 持久化或 transport 失败。
 *
 * @note 关键事件会写 NVS，因此不能在中断上下文调用；上报失败不改变 OTA 下载结果。
 */
esp_err_t native_ota_report_event(const native_ota_report_context_t *context,
                                  native_ota_report_state_t state,
                                  uint32_t bytes_downloaded,
                                  native_ota_failure_reason_t failure_reason)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }

    native_ota_report_message_t message;
    esp_err_t err = ota_report_build_message(context, state, bytes_downloaded,
                                              failure_reason, &message);
    if (err != ESP_OK) {
        return err;
    }

    esp_err_t store_err = ESP_OK;
    if (!ota_report_take_lock()) {
        return ESP_ERR_TIMEOUT;
    }
    s_store.schema_version = OTA_REPORT_STORE_SCHEMA_VERSION;
    s_store.context = *context;
    s_store.context_active = 1;
    if (state == NATIVE_OTA_REPORT_REBOOTING ||
        state == NATIVE_OTA_REPORT_BOOTED_PENDING_VERIFY) {
        s_store.awaiting_boot = 1;
    } else if (state == NATIVE_OTA_REPORT_SUCCEEDED ||
               state == NATIVE_OTA_REPORT_FAILED ||
               state == NATIVE_OTA_REPORT_ROLLED_BACK) {
        s_store.context_active = 0;
        s_store.awaiting_boot = 0;
    } else if (state == NATIVE_OTA_REPORT_DEFERRED) {
        s_store.awaiting_boot = 0;
    }

    if (s_store.count < NATIVE_OTA_REPORT_PENDING_MAX) {
        ota_report_pending_item_t *item = &s_store.pending[s_store.count++];
        memset(item, 0, sizeof(*item));
        item->state = (uint8_t)state;
        item->json_len = (uint32_t)message.json_len;
        strncpy(item->event_id, message.event_id, sizeof(item->event_id) - 1U);
        memcpy(item->json, message.json, message.json_len + 1U);
    } else if (ota_report_state_is_terminal(state) || state == NATIVE_OTA_REPORT_REBOOTING) {
        /* 关键队列满时保留最新终态/重启事件；普通状态不会挤掉旧关键事件。 */
        memmove(&s_store.pending[0], &s_store.pending[1],
                (NATIVE_OTA_REPORT_PENDING_MAX - 1U) * sizeof(s_store.pending[0]));
        ota_report_pending_item_t *item = &s_store.pending[NATIVE_OTA_REPORT_PENDING_MAX - 1U];
        memset(item, 0, sizeof(*item));
        item->state = (uint8_t)state;
        item->json_len = (uint32_t)message.json_len;
        strncpy(item->event_id, message.event_id, sizeof(item->event_id) - 1U);
        memcpy(item->json, message.json, message.json_len + 1U);
        ESP_LOGW(TAG, "Critical OTA report queue full; retained latest %s event",
                 native_ota_report_state_name(state));
    } else {
        store_err = ESP_ERR_NO_MEM;
    }

    esp_err_t persist_err = ota_report_store_save_locked();
    if (store_err == ESP_OK && persist_err != ESP_OK) {
        store_err = persist_err;
    }
    if (state == NATIVE_OTA_REPORT_DOWNLOADING) {
        /* 新一轮下载从 0 重新建立进度基线，避免沿用上一次任务的节流状态。 */
        s_progress_valid = false;
        s_last_progress_percent = 0;
        s_last_progress_ms = 0;
    }
    ota_report_give_lock();

    /* 即便 transport 失败，关键事件已经在 RAM/NVS 中保留，OTA 继续运行。 */
    err = ota_report_submit(&message);
    if (store_err != ESP_OK) {
        return store_err;
    }
    return err == ESP_ERR_NOT_SUPPORTED ? ESP_OK : err;
}

/**
 * @brief 按百分比步长、时间间隔或完成条件提交一条非关键下载进度。
 *
 * @param[in] context          OTA 关联上下文，image_size 必须大于 0。
 * @param[in] bytes_downloaded 当前已下载字节数，范围为 0～image_size，单位为字节。
 * @return ESP_OK 未达到上报条件，或进度已交给 transport。
 * @return ESP_ERR_INVALID_ARG 参数无效。
 * @return 其他 esp_err_t 锁、JSON 构建或 transport 失败。
 *
 * @note 进度只保存在 RAM，不写 NVS；函数不能在中断上下文调用。
 */
esp_err_t native_ota_report_progress(const native_ota_report_context_t *context,
                                     uint32_t bytes_downloaded)
{
    if (!s_initialized || context == NULL || context->image_size == 0 ||
        bytes_downloaded > context->image_size) {
        return ESP_ERR_INVALID_ARG;
    }

    uint32_t percent = (uint32_t)(((uint64_t)bytes_downloaded * 100U) /
                                  context->image_size);
    if (bytes_downloaded == context->image_size) {
        percent = 100U;
    }
    int64_t now_ms = esp_timer_get_time() / 1000LL;
    bool emit = false;
    if (!ota_report_take_lock()) {
        return ESP_ERR_TIMEOUT;
    }
    /* step/interval 任一条件满足即发送，100% 无条件发送，保证下载完成不会被节流吞掉。 */
    const uint32_t step = CONFIG_OTA_PROGRESS_STEP_PERCENT;
    const int64_t interval_ms = (int64_t)CONFIG_OTA_PROGRESS_INTERVAL_SECONDS * 1000LL;
    emit = !s_progress_valid || percent >= s_last_progress_percent + step ||
           now_ms - s_last_progress_ms >= interval_ms || percent >= 100U;
    ota_report_give_lock();
    if (!emit) {
        return ESP_OK;
    }

    native_ota_report_message_t message;
    esp_err_t err = ota_report_build_message(context, NATIVE_OTA_REPORT_DOWNLOADING,
                                              bytes_downloaded, NATIVE_OTA_FAILURE_NONE,
                                              &message);
    if (err != ESP_OK) {
        return err;
    }
    message.critical = false;

    if (!ota_report_take_lock()) {
        return ESP_ERR_TIMEOUT;
    }
    s_latest_progress = message;
    s_progress_valid = true;
    s_last_progress_percent = percent;
    s_last_progress_ms = now_ms;
    ota_report_give_lock();

    err = ota_report_submit(&message);
    return err == ESP_ERR_NOT_SUPPORTED ? ESP_OK : err;
}

/**
 * @brief 将 NVS 中的关键事件和 RAM 中最新进度逐条交给 transport。
 *
 * @return ESP_OK 当前可发送内容均已提交。
 * @return ESP_ERR_INVALID_STATE 模块尚未初始化或无法取得锁。
 * @return ESP_ERR_NOT_SUPPORTED 尚未注册 transport。
 * @return 其他 esp_err_t transport 拒绝某条消息。
 *
 * @note 不在本函数中删除 NVS 事件；删除仅由 PUBACK 路径触发。
 */
esp_err_t native_ota_report_flush_pending(void)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }

    native_ota_report_transport_t transport;
    void *transport_context;
    if (!ota_report_take_lock()) {
        return ESP_ERR_TIMEOUT;
    }
    transport = s_transport;
    transport_context = s_transport_context;
    ota_report_give_lock();
    if (transport == NULL) {
        return ESP_ERR_NOT_SUPPORTED;
    }

    /* 逐条复制，避免在 MQTT 事件回调栈上放置整个持久化结构。 */
    for (uint8_t i = 0; ; ++i) {
        native_ota_report_message_t message;
        if (!ota_report_take_lock()) {
            return ESP_ERR_TIMEOUT;
        }
        if (i >= s_store.count) {
            ota_report_give_lock();
            break;
        }
        const ota_report_pending_item_t *item = &s_store.pending[i];
        memset(&message, 0, sizeof(message));
        message.critical = true;
        message.state = (native_ota_report_state_t)item->state;
        strncpy(message.event_id, item->event_id, sizeof(message.event_id) - 1U);
        message.json_len = item->json_len;
        memcpy(message.json, item->json, item->json_len + 1U);
        ota_report_give_lock();
        esp_err_t err = transport(&message, transport_context);
        if (err != ESP_OK) {
            return err;
        }
    }

    if (ota_report_take_lock()) {
        if (s_progress_valid) {
            native_ota_report_message_t progress = s_latest_progress;
            ota_report_give_lock();
            (void)transport(&progress, transport_context);
        } else {
            ota_report_give_lock();
        }
    }
    return ESP_OK;
}

/**
 * @brief 接收通信层传来的 PUBACK 事件 ID。
 *
 * @param[in] event_id 已确认事件 ID，可为 NULL（此时忽略）。
 *
 * @note 只做参数检查和零等待入队；队列满时不丢失 NVS 中的原事件。
 */
void native_ota_report_ack_event(const char *event_id)
{
    if (!s_initialized || s_ack_queue == NULL || event_id == NULL ||
        event_id[0] == '\0' || strlen(event_id) >= NATIVE_OTA_EVENT_ID_SIZE) {
        return;
    }
    char id[NATIVE_OTA_EVENT_ID_SIZE] = { 0 };
    strncpy(id, event_id, sizeof(id) - 1U);
    (void)xQueueSend(s_ack_queue, id, 0);
}

/**
 * @brief 在锁保护下复制跨重启待验收上下文。
 *
 * @param[out] context 接收上下文，不允许为 NULL。
 * @return ESP_OK 存在 awaiting_boot 上下文并复制成功。
 * @return ESP_ERR_NOT_FOUND 当前没有待验收上下文。
 * @return ESP_ERR_INVALID_ARG 参数无效或无法取得锁。
 */
static esp_err_t ota_report_get_boot_context(native_ota_report_context_t *context)
{
    if (context == NULL || !ota_report_take_lock()) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_store.context_active || !s_store.awaiting_boot) {
        ota_report_give_lock();
        return ESP_ERR_NOT_FOUND;
    }
    *context = s_store.context;
    ota_report_give_lock();
    return ESP_OK;
}

/**
 * @brief 生成新镜像首次启动时的 booted_pending_verify 事件。
 *
 * @return ESP_OK 已生成事件，或没有需要关联的待验收上下文。
 * @return 其他 esp_err_t 报告处理失败。
 *
 * @note 应在本地健康检查前、网络连接前调用。
 */
esp_err_t native_ota_report_boot_pending_verify(void)
{
    native_ota_report_context_t context;
    esp_err_t err = ota_report_get_boot_context(&context);
    if (err != ESP_OK) {
        return err == ESP_ERR_NOT_FOUND ? ESP_OK : err;
    }
    return native_ota_report_event(&context, NATIVE_OTA_REPORT_BOOTED_PENDING_VERIFY,
                                   0, NATIVE_OTA_FAILURE_NONE);
}

/**
 * @brief 生成本地验收成功后的 succeeded 事件。
 *
 * @return ESP_OK 已生成事件，或没有需要关联的待验收上下文。
 * @return 其他 esp_err_t 报告处理失败。
 */
esp_err_t native_ota_report_boot_succeeded(void)
{
    native_ota_report_context_t context;
    esp_err_t err = ota_report_get_boot_context(&context);
    if (err != ESP_OK) {
        return err == ESP_ERR_NOT_FOUND ? ESP_OK : err;
    }
    return native_ota_report_event(&context, NATIVE_OTA_REPORT_SUCCEEDED,
                                   0, NATIVE_OTA_FAILURE_NONE);
}

/**
 * @brief 生成自检失败或检测到 bootloader 回滚后的 rolled_back 事件。
 *
 * @param[in] failure_reason 回滚失败分类。
 * @return ESP_OK 已生成事件，或没有需要关联的待验收上下文。
 * @return 其他 esp_err_t 报告处理失败。
 */
esp_err_t native_ota_report_boot_rolled_back(native_ota_failure_reason_t failure_reason)
{
    native_ota_report_context_t context;
    esp_err_t err = ota_report_get_boot_context(&context);
    if (err != ESP_OK) {
        return err == ESP_ERR_NOT_FOUND ? ESP_OK : err;
    }
    return native_ota_report_event(&context, NATIVE_OTA_REPORT_ROLLED_BACK,
                                   0, failure_reason);
}

/**
 * @brief 将非 PENDING_VERIFY 启动识别为上一次 OTA 回滚并生成终态事件。
 *
 * @param[in] running_version 当前运行镜像版本，不允许为 NULL。
 * @param[in] last_invalid_version bootloader 最近判定无效的版本，不允许为 NULL。
 * @return ESP_OK 无匹配上下文或回滚事件已生成。
 * @return 其他 esp_err_t 锁、事件构建、持久化或 transport 失败。
 *
 * @note 只有“持久化目标版本 == last_invalid_version 且不等于当前版本”时才上报，
 *       避免把历史无效分区误关联到新的 OTA 任务。
 */
esp_err_t native_ota_report_reconcile_rollback(const char *running_version,
                                                const char *last_invalid_version)
{
    if (!s_initialized || running_version == NULL || last_invalid_version == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    native_ota_report_context_t context;
    if (!ota_report_take_lock()) {
        return ESP_ERR_TIMEOUT;
    }
    bool matches = s_store.context_active && s_store.awaiting_boot &&
                   strcmp(s_store.context.target_version, last_invalid_version) == 0 &&
                   strcmp(s_store.context.target_version, running_version) != 0;
    if (matches) {
        context = s_store.context;
    }
    ota_report_give_lock();
    if (!matches) {
        return ESP_OK;
    }
    return native_ota_report_event(&context, NATIVE_OTA_REPORT_ROLLED_BACK,
                                   0, NATIVE_OTA_FAILURE_BOOT_SELF_TEST_FAILED);
}
