/**
 * @file    ota_state_store.c
 * @brief   OTA 断点续传与 artifact 隔离状态的 NVS 存储实现。
 *
 * 所有读写都使用独立 namespace 和单个 blob 键，避免业务配置迁移或清理
 * 影响 OTA 恢复状态。调用方负责在 nvs_flash_init() 成功后使用本模块。
 */

#include <string.h>

#include "esp_log.h"
#include "nvs.h"

#include "ota_state_store.h"

/** 本模块统一使用的日志标签。 */
static const char *TAG = "ota_state_store";
/** OTA 断点状态使用的独立 NVS namespace，不与业务配置混用。 */
static const char *NAMESPACE = "ota_resume";
/** namespace 内保存完整恢复记录的单一 blob 键名。 */
static const char *RECORD_KEY = "record";

void ota_state_store_log_nvs_usage(const char *tag, const char *operation,
                                   esp_err_t operation_error)
{
    const char *log_tag = tag != NULL ? tag : TAG;
    const char *operation_name = operation != NULL ? operation : "operation";
    nvs_stats_t stats = { 0 };
    esp_err_t stats_err = nvs_get_stats(NULL, &stats);

    if (stats_err != ESP_OK) {
        if (operation_error == ESP_OK) {
            ESP_LOGW(log_tag, "NVS water level unavailable at %s: %s",
                     operation_name, esp_err_to_name(stats_err));
        } else {
            ESP_LOGE(log_tag, "NVS %s failed: %s; water level unavailable: %s",
                     operation_name, esp_err_to_name(operation_error),
                     esp_err_to_name(stats_err));
        }
        return;
    }

    size_t used_percent = 0;
    if (stats.total_entries != 0U) {
        used_percent = (stats.used_entries * 100U + stats.total_entries / 2U) /
                       stats.total_entries;
    }

    if (operation_error == ESP_OK) {
        ESP_LOGI(log_tag,
                 "NVS water level at %s: used=%zu/%zu (%zu%%), available=%zu, free=%zu, namespaces=%zu",
                 operation_name, stats.used_entries, stats.total_entries, used_percent,
                 stats.available_entries, stats.free_entries, stats.namespace_count);
    } else {
        ESP_LOGE(log_tag,
                 "NVS %s failed: %s; water level: used=%zu/%zu (%zu%%), available=%zu, free=%zu, namespaces=%zu",
                 operation_name, esp_err_to_name(operation_error), stats.used_entries,
                 stats.total_entries, used_percent, stats.available_entries, stats.free_entries,
                 stats.namespace_count);
    }
}

/**
 * @brief 检查恢复记录的布局版本和最小字段约束。
 *
 * @param[in] record 待检查的恢复记录，可为 NULL。
 * @return true 记录可以作为 OTA 恢复元数据使用。
 * @return false 记录为空、版本不兼容或关键字段无效。
 *
 * @note 这里只做内存中的一致性检查，不访问 NVS，也不修改 record 内容。
 */
static bool record_is_valid(const ota_resume_record_t *record)
{
    /* 记录以固定 blob 布局保存；不接受未知 schema、空字符串或越过清单的恢复偏移。 */
    return record != NULL &&
           record->schema_version == OTA_STATE_STORE_SCHEMA_VERSION &&
           record->expected_size > 0 &&
           record->verified_offset <= record->expected_size &&
           record->phase >= OTA_RESUME_PHASE_DOWNLOADING &&
           record->phase <= OTA_RESUME_PHASE_COOLING_DOWN &&
           record->artifact_id[0] != '\0' &&
           record->version[0] != '\0' &&
           record->url[0] != '\0';
}

/**
 * @brief 从独立的 ota_resume namespace 读取 OTA 恢复记录。
 *
 * @param[out] record 输出记录；失败时会清零，不允许为 NULL。
 * @return ESP_OK 读取到尺寸和 schema 均有效的记录。
 * @return ESP_ERR_NOT_FOUND 没有保存过记录。
 * @return ESP_ERR_INVALID_VERSION blob 尺寸或 schema 与当前实现不兼容。
 * @return 其他 esp_err_t NVS 打开或读取失败。
 *
 * @note 必须在 nvs_flash_init() 成功后调用；函数只执行一次短暂 NVS 读取，不等待网络。
 */
esp_err_t ota_state_store_load(ota_resume_record_t *record)
{
    if (record == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(record, 0, sizeof(*record));
    /* 读取时先把输出清零，避免没有记录或格式不兼容时调用者误用旧内存内容。 */
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        return err == ESP_ERR_NVS_NOT_FOUND ? ESP_ERR_NOT_FOUND : err;
    }

    /* 以当前结构体大小读取，随后再次检查 NVS blob 的实际大小，拒绝旧布局。 */
    size_t size = sizeof(*record);
    err = nvs_get_blob(handle, RECORD_KEY, record, &size);
    nvs_close(handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        memset(record, 0, sizeof(*record));
        return ESP_ERR_NOT_FOUND;
    }
    if (err != ESP_OK) {
        memset(record, 0, sizeof(*record));
        return err;
    }
    if (size != sizeof(*record) || !record_is_valid(record)) {
        ESP_LOGW(TAG, "Ignoring invalid or incompatible OTA resume record");
        memset(record, 0, sizeof(*record));
        return ESP_ERR_INVALID_VERSION;
    }
    return ESP_OK;
}

/**
 * @brief 原子地保存一条 OTA 恢复记录。
 *
 * @param[in] record 已填充且通过 record_is_valid() 的记录，不允许为 NULL。
 * @return ESP_OK blob 写入并 nvs_commit() 成功。
 * @return ESP_ERR_INVALID_ARG 记录不满足当前 schema 的最小约束。
 * @return 其他 esp_err_t NVS 写入或提交失败。
 *
 * @note 函数会触发 NVS 写入，不能在中断上下文调用；调用者负责控制检查点写入频率。
 */
esp_err_t ota_state_store_save(const ota_resume_record_t *record)
{
    if (!record_is_valid(record)) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 单个 blob + nvs_commit() 让检查点成为一次完整更新，避免只写入部分字段。 */
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ota_state_store_log_nvs_usage(TAG, "opening OTA resume store for write", err);
        return err;
    }

    err = nvs_set_blob(handle, RECORD_KEY, record, sizeof(*record));
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err != ESP_OK) {
        ota_state_store_log_nvs_usage(TAG, "saving OTA resume record", err);
    }
    return err;
}

/**
 * @brief 删除 OTA 恢复记录。
 *
 * @return ESP_OK 记录删除成功、记录不存在或 namespace 尚未创建。
 * @return 其他 esp_err_t NVS 擦除或提交失败。
 *
 * @note 函数会写入 Flash，不能在中断上下文调用。
 */
esp_err_t ota_state_store_clear(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NAMESPACE, NVS_READWRITE, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (err != ESP_OK) {
        ota_state_store_log_nvs_usage(TAG, "opening OTA resume store for erase", err);
        return err;
    }

    /* 只擦除 OTA 恢复键，不删除 namespace 中未来可能加入的其他诊断项。 */
    err = nvs_erase_key(handle, RECORD_KEY);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        err = ESP_OK;
    } else if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err != ESP_OK) {
        ota_state_store_log_nvs_usage(TAG, "clearing OTA resume record", err);
    }
    return err;
}

/**
 * @brief 判断恢复记录是否对应当前服务器清单。
 *
 * @param[in] record 已加载的恢复记录，不允许为 NULL。
 * @param[in] manifest 当前服务器清单，不允许为 NULL。
 * @return true artifact_id、版本、大小、URL 和 SHA-256 全部一致。
 * @return false 任一字段不同、记录无效或参数无效。
 *
 * @note 本函数只比较内存，不访问 NVS，也不修改任一输入对象。
 */
bool ota_state_store_matches_manifest(const ota_resume_record_t *record,
                                      const native_ota_manifest_t *manifest)
{
    if (record == NULL || manifest == NULL || !record_is_valid(record) ||
        manifest->artifact_id[0] == '\0' || manifest->url[0] == '\0' ||
        manifest->image_size == 0) {
        return false;
    }

    /* 这些字段共同定义“同一个可续传对象”；任一变化都必须从零开始。 */
    return record->expected_size == manifest->image_size &&
           strcmp(record->artifact_id, manifest->artifact_id) == 0 &&
           strcmp(record->version, manifest->version) == 0 &&
           strcmp(record->url, manifest->url) == 0 &&
           memcmp(record->sha256, manifest->sha256, NATIVE_OTA_SHA256_SIZE) == 0;
}

/**
 * @brief 用当前运行版本清理已经提交并成功启动的旧恢复记录。
 *
 * @param[in] running_version 当前运行镜像版本字符串，不允许为 NULL 或空串。
 * @return ESP_OK 清理完成、没有记录或记录不属于已提交的当前版本。
 * @return 其他 esp_err_t NVS 读取或删除失败。
 *
 * @note 必须在当前镜像完成启动验收后调用；不参与网络连接和 rollback 决策。
 */
esp_err_t ota_state_store_reconcile_running_version(const char *running_version)
{
    if (running_version == NULL || running_version[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    ota_resume_record_t record;
    esp_err_t err = ota_state_store_load(&record);
    if (err == ESP_ERR_NOT_FOUND || err == ESP_ERR_INVALID_VERSION) {
        return ESP_OK;
    }
    if (err != ESP_OK) {
        return err;
    }

    /* READY_TO_COMMIT 表示该版本已写入并校验；当前版本真正运行后才安全清理。 */
    if (record.phase == OTA_RESUME_PHASE_READY_TO_COMMIT &&
        strcmp(record.version, running_version) == 0) {
        ESP_LOGI(TAG, "Clearing committed OTA record for running version %s", running_version);
        return ota_state_store_clear();
    }
    return ESP_OK;
}
