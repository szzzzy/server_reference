/**
 * @file    audio_engine.c
 * @brief   音频素材下载引擎实现。
 *
 * 主要职责：
 * 1. 通过公共下载器 http_downloader 从 HTTPS 服务器下载音频素材；
 * 2. 数据写入独立的音频数据分区（esp_partition_write，不写 OTA 槽）；
 * 3. 按固定间隔保存 NVS 断点检查点，支持 Range 断点续传；
 * 4. 下载完成后从分区回读计算 SHA-256 并与清单比对；
 * 5. 通过通信层通用发布接口 mqtt_comm_publish() 上报 audio_status 生命周期事件，
 *    通过弱钩子 native_audio_on_ready() 通知上层（如播放栈）。
 *
 * 模块关系：
 * - 传输核心由 http_downloader 完成（连接、Range、ETag、读循环）；
 * - 摘要计算复用 ota_stability 的分区回读路径，与固件 OTA 同一校验语义；
 * - 不解析控制面 JSON、不依赖 MQTT 客户端句柄。
 */
#include "audio_engine.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_partition.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "cJSON.h"

#include "http_downloader.h"
#include "mqtt_comm.h"
#include "ota_stability.h"

/** 本模块统一使用的日志标签。 */
static const char *TAG = "audio_engine";

/** 音频断点记录布局版本；改变布局时必须递增。 */
#define AUDIO_ENGINE_STORE_SCHEMA_VERSION 1U

/** NVS 断点检查点之间的最小写入间隔，单位为字节。 */
#define AUDIO_ENGINE_CHECKPOINT_BYTES (16U * 1024U)

/** 断点记录中的 ETag 文本容量，包含末尾 NUL。 */
#define AUDIO_ENGINE_ETAG_SIZE 128

/** 音频 NVS 命名空间，与 ota_resume / ota_report 天然隔离。 */
#define AUDIO_ENGINE_NVS_NAMESPACE "audio_resume"

/** 是否已有音频任务运行；通信事件可能来自不同任务，因此通过临界区访问。 */
static bool s_audio_in_progress;
/** 保护 s_audio_in_progress 的 FreeRTOS 自旋锁，不保护耗时下载操作。 */
static portMUX_TYPE s_audio_state_lock = portMUX_INITIALIZER_UNLOCKED;

/**
 * @brief 可跨重启恢复的音频下载断点记录。
 *
 * verified_offset 表示已写入并通过 NVS 检查点的数据前缀长度；恢复时必须对
 * 清单、URL、摘要和 HTTP Content-Range 再次校验。
 */
typedef struct {
    uint32_t schema_version; /**< 记录布局版本，必须等于 AUDIO_ENGINE_STORE_SCHEMA_VERSION。 */
    uint32_t expected_size; /**< 清单声明的完整文件长度，单位为字节。 */
    uint32_t verified_offset; /**< 已写入并持久化检查点的前缀长度，单位为字节。 */
    uint8_t sha256[NATIVE_OTA_SHA256_SIZE]; /**< 清单中的原始 SHA-256 摘要。 */
    char audio_id[NATIVE_OTA_AUDIO_ID_SIZE]; /**< 服务端音频素材唯一 ID。 */
    char version[NATIVE_OTA_AUDIO_VERSION_SIZE]; /**< 音频素材版本字符串。 */
    char url[NATIVE_OTA_URL_SIZE]; /**< 目标音频 HTTPS URL。 */
    char etag[AUDIO_ENGINE_ETAG_SIZE]; /**< 服务器 ETag，用于确认续传内容仍是同一版本。 */
} audio_resume_record_t;

/** 固定记录布局：全部字段为定长标量/字符数组，无指针、无 padding。 */
_Static_assert(sizeof(audio_resume_record_t) ==
               4U + 4U + 4U + NATIVE_OTA_SHA256_SIZE + NATIVE_OTA_AUDIO_ID_SIZE +
               NATIVE_OTA_AUDIO_VERSION_SIZE + NATIVE_OTA_URL_SIZE + AUDIO_ENGINE_ETAG_SIZE,
               "audio_resume_record_t layout must stay fixed for NVS compatibility");

/** 音频下载任务入口；任务参数为一次服务器响应的堆上深拷贝。 */
static void audio_download_task(void *pvParameter);

/* ------------------------------------------------------------------------- */
/* NVS 辅助                                                                    */
/* ------------------------------------------------------------------------- */

static esp_err_t audio_nvs_open(nvs_handle_t *handle, nvs_open_mode_t mode)
{
    return nvs_open(AUDIO_ENGINE_NVS_NAMESPACE, mode, handle);
}

/** 读取音频断点记录；无记录返回 ESP_ERR_NOT_FOUND，布局不符返回 ESP_ERR_INVALID_VERSION。 */
static esp_err_t audio_record_load(audio_resume_record_t *record)
{
    if (record == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t handle;
    esp_err_t err = audio_nvs_open(&handle, NVS_READONLY);
    if (err != ESP_OK) {
        return err;
    }
    size_t length = sizeof(*record);
    err = nvs_get_blob(handle, "record", record, &length);
    nvs_close(handle);
    if (err != ESP_OK) {
        return err;
    }
    if (length != sizeof(*record) ||
        record->schema_version != AUDIO_ENGINE_STORE_SCHEMA_VERSION) {
        return ESP_ERR_INVALID_VERSION;
    }
    return ESP_OK;
}

/** 原子地保存音频断点记录。 */
static esp_err_t audio_record_save(const audio_resume_record_t *record)
{
    if (record == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t handle;
    esp_err_t err = audio_nvs_open(&handle, NVS_READWRITE);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(handle, "record", record, sizeof(*record));
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err;
}

/** 删除音频断点记录；记录本不存在时按成功处理。 */
static esp_err_t audio_record_clear(void)
{
    nvs_handle_t handle;
    esp_err_t err = audio_nvs_open(&handle, NVS_READWRITE);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_erase_key(handle, "record");
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        err = ESP_OK;
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err;
}

/** 保存当前已安装音频素材版本。 */
static esp_err_t audio_current_version_save(const char *version)
{
    if (version == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t handle;
    esp_err_t err = audio_nvs_open(&handle, NVS_READWRITE);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(handle, "current_version", version);
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err;
}

/** 读取当前已安装音频素材版本；未安装返回 ESP_ERR_NOT_FOUND。 */
static esp_err_t audio_current_version_load(char *version, size_t version_size)
{
    if (version == NULL || version_size == 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t handle;
    esp_err_t err = audio_nvs_open(&handle, NVS_READONLY);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_get_str(handle, "current_version", version, &version_size);
    nvs_close(handle);
    return err;
}

/* ------------------------------------------------------------------------- */
/* 记录初始化与匹配                                                            */
/* ------------------------------------------------------------------------- */

/** 以当前清单初始化音频断点记录（只初始化内存，不写 NVS）。 */
static void audio_record_init(audio_resume_record_t *record,
                              const native_audio_manifest_t *manifest)
{
    memset(record, 0, sizeof(*record));
    record->schema_version = AUDIO_ENGINE_STORE_SCHEMA_VERSION;
    record->expected_size = manifest->file_size;
    memcpy(record->sha256, manifest->sha256, sizeof(record->sha256));
    strncpy(record->audio_id, manifest->audio_id, sizeof(record->audio_id) - 1U);
    strncpy(record->version, manifest->version, sizeof(record->version) - 1U);
    strncpy(record->url, manifest->url, sizeof(record->url) - 1U);
}

/** 判断断点记录是否对应当前音频清单（ID、大小、URL 和摘要全部一致）。 */
static bool audio_record_matches(const audio_resume_record_t *record,
                                 const native_audio_manifest_t *manifest)
{
    return record->schema_version == AUDIO_ENGINE_STORE_SCHEMA_VERSION &&
           record->expected_size == manifest->file_size &&
           memcmp(record->sha256, manifest->sha256, sizeof(record->sha256)) == 0 &&
           strcmp(record->audio_id, manifest->audio_id) == 0 &&
           strcmp(record->url, manifest->url) == 0;
}

/* ------------------------------------------------------------------------- */
/* 状态上报                                                                    */
/* ------------------------------------------------------------------------- */

/**
 * @brief 构造并发布一条尽力而为的 audio_status 事件（QoS 1，不持久化）。
 *
 * @param[in] manifest        当前音频清单，不允许为 NULL。
 * @param[in] state           协议状态名（accepted/downloading/verifying/ready/failed）。
 * @param[in] progress        进度百分比 0～100。
 * @param[in] failure_reason  失败原因；无失败传 NATIVE_OTA_FAILURE_NONE。
 * @return ESP_OK 已交给 MQTT 发送队列；其他值表示 MQTT 未就绪或构造失败。
 */
static esp_err_t audio_report_status(const native_audio_manifest_t *manifest,
                                     const char *state, uint32_t progress,
                                     native_ota_failure_reason_t failure_reason)
{
    if (manifest == NULL || state == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    char device_id[NATIVE_OTA_DEVICE_ID_SIZE] = { 0 };
    (void)native_ota_get_device_id(device_id, sizeof(device_id));

    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }
    bool added = cJSON_AddStringToObject(root, "type", "audio_status") != NULL &&
                 cJSON_AddNumberToObject(root, "schema_version", 1) != NULL &&
                 cJSON_AddStringToObject(root, "device_id", device_id) != NULL &&
                 cJSON_AddStringToObject(root, "audio_id", manifest->audio_id) != NULL &&
                 cJSON_AddStringToObject(root, "state", state) != NULL &&
                 cJSON_AddNumberToObject(root, "progress", (double)progress) != NULL &&
                 cJSON_AddNumberToObject(root, "error_code", (double)failure_reason) != NULL;
    if (!added) {
        cJSON_Delete(root);
        return ESP_ERR_NO_MEM;
    }

    char json[512];
    bool printed = cJSON_PrintPreallocated(root, json, sizeof(json), false);
    cJSON_Delete(root);
    if (!printed) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 音频状态 topic 为 <prefix>/<device_id>，与 OTA 状态 topic 同一拼装规则；
     * 通过通信层通用发布接口发送，不依赖任何 OTA 报告内部状态。 */
    char topic[192];
    int topic_len = snprintf(topic, sizeof(topic), "%s/%s",
                             CONFIG_COMM_MQTT_AUDIO_STATUS_TOPIC_PREFIX, device_id);
    if (topic_len <= 0 || (size_t)topic_len >= sizeof(topic)) {
        return ESP_ERR_INVALID_SIZE;
    }
    return mqtt_comm_publish(topic, json, strlen(json));
}

/* ------------------------------------------------------------------------- */
/* 公共下载器 sink / restart                                                   */
/* ------------------------------------------------------------------------- */

/**
 * @brief 音频下载 sink 上下文：把下载器网络块写入音频数据分区并保存检查点。
 */
typedef struct {
    const native_audio_manifest_t *manifest; /**< 当前清单，只读。 */
    const esp_partition_t *partition; /**< 目标音频数据分区。 */
    audio_resume_record_t *record; /**< 当前断点记录，sink 更新检查点。 */
    size_t offset; /**< 已写入分区长度（含断点前缀），同时是摘要边界。 */
    size_t last_checkpoint; /**< 上次 NVS 检查点对应的偏移。 */
} audio_engine_sink_ctx_t;

/**
 * @brief 将下载器网络块写入音频数据分区（公共下载器 sink 回调）。
 *
 * @return ESP_OK 数据已写入；其他值中止下载并透传给调用方分类。
 */
static esp_err_t audio_engine_sink(void *ctx, const uint8_t *data, size_t len)
{
    audio_engine_sink_ctx_t *s = (audio_engine_sink_ctx_t *)ctx;
    if (s == NULL || data == NULL || len == 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    /* 即使服务端采用 chunked 编码而未提供 Content-Length，也绝不能让
     * 单个异常响应写过清单声明的素材边界。 */
    if (s->manifest == NULL || s->offset > s->manifest->file_size ||
        len > (size_t)s->manifest->file_size - s->offset) {
        ESP_LOGE(TAG, "Audio response exceeds manifest size");
        return ESP_ERR_INVALID_SIZE;
    }
    esp_err_t err = esp_partition_write(s->partition, s->offset, data, len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write audio partition: %s", esp_err_to_name(err));
        return err;
    }
    s->offset += len;
    if (s->offset - s->last_checkpoint >= AUDIO_ENGINE_CHECKPOINT_BYTES) {
        s->record->verified_offset = (uint32_t)s->offset;
        err = audio_record_save(s->record);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to persist audio checkpoint: %s", esp_err_to_name(err));
            return err;
        }
        s->last_checkpoint = s->offset;
    }
    return ESP_OK;
}

/**
 * @brief 公共下载器断点重置回调：重建断点记录并清零写入偏移。
 *
 * @return ESP_OK 允许从零开始全量下载；其他值中止下载。
 */
static esp_err_t audio_engine_restart(void *ctx)
{
    audio_engine_sink_ctx_t *s = (audio_engine_sink_ctx_t *)ctx;
    if (s == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    s->offset = 0;
    s->last_checkpoint = 0;
    audio_record_init(s->record, s->manifest);
    return audio_record_save(s->record);
}

/* ------------------------------------------------------------------------- */
/* 下载任务                                                                    */
/* ------------------------------------------------------------------------- */

/**
 * @brief 执行带断点续传、分区写入和摘要校验的音频下载任务。
 *
 * @param[in] pvParameter 指向堆上 native_audio_manifest_t；任务取得所有权后
 *                        立即释放，不能为 NULL。
 *
 * 任务先复用与清单匹配的断点记录，再经公共下载器下载；Range、HTTP 状态、
 * Content-Length、Content-Range 与完整接收校验由下载器完成，分区写入与
 * 检查点由 sink 完成。摘要校验失败或分区不可用时清除断点记录；网络类失败
 * 保留断点供下次续传。
 */
static void audio_download_task(void *pvParameter)
{
    native_audio_manifest_t manifest = *(native_audio_manifest_t *)pvParameter;
    free(pvParameter);

    esp_err_t err = ESP_OK;
    native_ota_failure_reason_t failure_reason = NATIVE_OTA_FAILURE_NONE;
    /* 网络类失败保留断点记录；校验类失败清除，下次从零下载。 */
    bool keep_record = false;
    size_t resume_offset = 0;
    bool resume = false;
    const esp_partition_t *partition = NULL;
    audio_resume_record_t record;
    bool record_active = false;
    audio_engine_sink_ctx_t sink_ctx = { 0 };

    ESP_LOGI(TAG, "Starting audio download task: audio_id=%s, version=%s, size=%" PRIu32,
             manifest.audio_id, manifest.version, manifest.file_size);

    (void)audio_report_status(&manifest, "accepted", 0, NATIVE_OTA_FAILURE_NONE);

    partition = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY,
                                         CONFIG_AUDIO_STORAGE_PARTITION);
    if (partition == NULL) {
        ESP_LOGE(TAG, "Audio storage partition '%s' not found", CONFIG_AUDIO_STORAGE_PARTITION);
        failure_reason = NATIVE_OTA_FAILURE_STORAGE_UNAVAILABLE;
        goto cleanup;
    }
    if (manifest.file_size > partition->size) {
        ESP_LOGE(TAG, "Audio file_size=%" PRIu32 " exceeds partition size=%" PRIu32,
                 manifest.file_size, partition->size);
        failure_reason = NATIVE_OTA_FAILURE_IMAGE_TOO_LARGE;
        goto cleanup;
    }

    /* 只有清单元数据完全一致时才允许复用 Flash 前缀和 HTTP Range 检查点。 */
    err = audio_record_load(&record);
    if (err == ESP_OK) {
        if (audio_record_matches(&record, &manifest) &&
            record.verified_offset > 0U &&
            record.verified_offset < manifest.file_size) {
            record_active = true;
            resume_offset = ota_stability_normalize_resume_offset(record.verified_offset);
            resume = resume_offset > 0U;
            ESP_LOGI(TAG, "Resuming audio artifact %s from offset=%zu",
                     manifest.audio_id, resume_offset);
        } else {
            ESP_LOGI(TAG, "Audio resume record belongs to another artifact; starting from zero");
            (void)audio_record_clear();
        }
    } else if (err != ESP_ERR_NOT_FOUND && err != ESP_ERR_INVALID_VERSION) {
        ESP_LOGE(TAG, "Failed to load audio resume record: %s", esp_err_to_name(err));
        failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
        goto cleanup;
    }

    if (!record_active) {
        audio_record_init(&record, &manifest);
        err = audio_record_save(&record);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Cannot persist initial audio resume record: %s", esp_err_to_name(err));
            failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
            goto cleanup;
        }
        record_active = true;
    }

    sink_ctx.manifest = &manifest;
    sink_ctx.partition = partition;
    sink_ctx.record = &record;
    sink_ctx.offset = resume_offset;
    sink_ctx.last_checkpoint = resume_offset;

    (void)audio_report_status(&manifest, "downloading", 0, NATIVE_OTA_FAILURE_NONE);

    http_downloader_config_t dl_config = {
        .url = manifest.url,
        .cert_pem = NULL, /* NULL 使用构建嵌入的 ca_cert.pem。 */
        .timeout_ms = CONFIG_EXAMPLE_OTA_RECV_TIMEOUT,
#ifdef CONFIG_EXAMPLE_SKIP_COMMON_NAME_CHECK
        .skip_cert_common_name_check = true,
#endif
        .expected_size = manifest.file_size,
        .resume_offset = resume ? resume_offset : 0,
        .expected_etag = resume ? record.etag : NULL,
        .sink = audio_engine_sink,
        .sink_ctx = &sink_ctx,
        .restart_cb = audio_engine_restart,
        .restart_ctx = &sink_ctx,
    };
    http_downloader_result_t dl_result;
    err = http_downloader_run(&dl_config, &dl_result);

    /* 传输层失败直接使用下载器分类；sink 返回的业务错误按错误码映射。 */
    if (err != ESP_OK) {
        if (dl_result.failure_reason != NATIVE_OTA_FAILURE_NONE) {
            failure_reason = dl_result.failure_reason;
        } else if (err >= ESP_ERR_NVS_BASE && err < ESP_ERR_NVS_BASE + 0x10) {
            failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
        } else {
            failure_reason = NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
        }
        /* 网络类失败保留断点记录供下次续传；其余错误清除。 */
        keep_record = (failure_reason == NATIVE_OTA_FAILURE_NETWORK_TIMEOUT ||
                       failure_reason == NATIVE_OTA_FAILURE_TLS_VERIFY_FAILED ||
                       failure_reason == NATIVE_OTA_FAILURE_HTTP_STATUS_INVALID);
        goto cleanup;
    }

    /* 首次获得服务器 ETag 时立即持久化，供下次断点续传确认对象身份。 */
    if (dl_result.etag[0] != '\0' && record.etag[0] == '\0') {
        strncpy(record.etag, dl_result.etag, sizeof(record.etag) - 1U);
        record.etag[sizeof(record.etag) - 1U] = '\0';
        err = audio_record_save(&record);
        if (err != ESP_OK) {
            failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
            goto cleanup;
        }
    }

    /* body 完整、长度匹配是摘要校验之前的必要条件。 */
    if (!dl_result.complete || sink_ctx.offset != manifest.file_size) {
        ESP_LOGE(TAG, "Received audio is incomplete: got=%zu expected=%" PRIu32,
                 sink_ctx.offset, manifest.file_size);
        failure_reason = dl_result.complete ?
                         NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED :
                         NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
        keep_record = !dl_result.complete;
        goto cleanup;
    }

    (void)audio_report_status(&manifest, "verifying", 0, NATIVE_OTA_FAILURE_NONE);

    /* 从分区实际写入的内容重新计算摘要，与固件 OTA 同一校验路径。 */
    uint8_t downloaded_sha256[NATIVE_OTA_SHA256_SIZE];
    err = ota_stability_calculate_partition_sha256(partition, manifest.file_size,
                                                   downloaded_sha256);
    if (err != ESP_OK) {
        failure_reason = NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
        goto cleanup;
    }
    if (memcmp(downloaded_sha256, manifest.sha256, sizeof(downloaded_sha256)) != 0) {
        ESP_LOGE(TAG, "Downloaded audio SHA-256 does not match the audio response");
        failure_reason = NATIVE_OTA_FAILURE_HASH_MISMATCH;
        goto cleanup;
    }

    /* 摘要校验通过：记录当前素材版本并清除断点，随后通知上层播放。 */
    err = audio_current_version_save(manifest.version);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Failed to persist current audio version: %s", esp_err_to_name(err));
    }
    (void)audio_record_clear();
    record_active = false;

    (void)audio_report_status(&manifest, "ready", 100, NATIVE_OTA_FAILURE_NONE);
    esp_err_t play_err = native_audio_on_ready(&manifest, sink_ctx.offset);
    if (play_err != ESP_OK) {
        ESP_LOGW(TAG, "Audio playback handoff reported: %s", esp_err_to_name(play_err));
    }
    failure_reason = NATIVE_OTA_FAILURE_NONE;

cleanup:
    if (failure_reason != NATIVE_OTA_FAILURE_NONE) {
        ESP_LOGE(TAG, "Audio task finished with reason=%s",
                 native_ota_failure_reason_name(failure_reason));
        (void)audio_report_status(&manifest, "failed", 0, failure_reason);
        if (record_active && !keep_record) {
            (void)audio_record_clear();
        }
    }
    portENTER_CRITICAL(&s_audio_state_lock);
    s_audio_in_progress = false;
    portEXIT_CRITICAL(&s_audio_state_lock);
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------------- */
/* 公共接口                                                                    */
/* ------------------------------------------------------------------------- */

esp_err_t audio_engine_start(const native_audio_manifest_t *manifest)
{
    if (manifest == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    native_audio_manifest_t *request = calloc(1, sizeof(*request));
    if (request == NULL) {
        return ESP_ERR_NO_MEM;
    }
    *request = *manifest;

    portENTER_CRITICAL(&s_audio_state_lock);
    if (s_audio_in_progress) {
        portEXIT_CRITICAL(&s_audio_state_lock);
        free(request);
        return ESP_ERR_INVALID_STATE;
    }
    s_audio_in_progress = true;
    portEXIT_CRITICAL(&s_audio_state_lock);

    ESP_LOGI(TAG, "Creating audio task: audio_id=%s, version=%s, size=%" PRIu32,
             request->audio_id, request->version, request->file_size);
    if (xTaskCreate(audio_download_task, "audio_task", 12288, request, 5, NULL) != pdPASS) {
        portENTER_CRITICAL(&s_audio_state_lock);
        s_audio_in_progress = false;
        portEXIT_CRITICAL(&s_audio_state_lock);
        free(request);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

bool audio_engine_is_running(void)
{
    portENTER_CRITICAL(&s_audio_state_lock);
    bool running = s_audio_in_progress;
    portEXIT_CRITICAL(&s_audio_state_lock);
    return running;
}

esp_err_t audio_engine_get_current_version(char *version, size_t version_size)
{
    if (version == NULL || version_size < NATIVE_OTA_AUDIO_VERSION_SIZE) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = audio_current_version_load(version, version_size);
    if (err != ESP_OK) {
        strncpy(version, NATIVE_OTA_AUDIO_VERSION_UNKNOWN, version_size - 1U);
        version[version_size - 1U] = '\0';
    }
    return ESP_OK;
}

__attribute__((weak)) esp_err_t native_audio_on_ready(const native_audio_manifest_t *manifest,
                                                      size_t stored_size)
{
    ESP_LOGI(TAG, "Audio artifact %s ready: %zu bytes stored (default hook, no player attached)",
             manifest != NULL ? manifest->audio_id : "?", stored_size);
    return ESP_OK;
}
