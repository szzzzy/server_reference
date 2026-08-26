/**
 * @file    ota_state_store.h
 * @brief   OTA 断点续传与 artifact 隔离状态的 NVS 存储。
 *
 * 状态保存在独立的 ota_resume namespace 中，不与业务配置共用键名。
 * 检查点按固定数据量写入，避免每个网络包都触发 Flash 擦写。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "native_ota_example.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 当前状态结构的版本；改变布局时必须递增。 */
#define OTA_STATE_STORE_SCHEMA_VERSION 2U

/** NVS 状态检查点之间的最小写入间隔，单位为字节。 */
#define OTA_STATE_STORE_CHECKPOINT_BYTES (16U * 1024U)

/** 下载恢复最多允许的网络类失败次数；达到后由 OTA 任务隔离 artifact。 */
#define OTA_STATE_STORE_NETWORK_RETRY_THRESHOLD CONFIG_OTA_DOWNLOAD_RETRY_THRESHOLD

/** 输出默认 NVS 分区水位，并关联最近一次存储操作结果。 */
void ota_state_store_log_nvs_usage(const char *tag, const char *operation,
                                   esp_err_t operation_error);

/** OTA 恢复记录的阶段。 */
typedef enum {
    OTA_RESUME_PHASE_EMPTY = 0, /**< 未建立有效恢复记录，仅作为保留值。 */
    OTA_RESUME_PHASE_DOWNLOADING = 1, /**< 已写入部分镜像，可按检查点尝试续传。 */
    OTA_RESUME_PHASE_READY_TO_COMMIT = 2, /**< 镜像已完整校验，等待切换启动分区。 */
    OTA_RESUME_PHASE_QUARANTINED = 3, /**< 终端校验失败，禁止同一 artifact 自动重试。 */
} ota_resume_phase_t;

/** Persistent network retry cooldown. This intentionally never means quarantined. */
#define OTA_RESUME_PHASE_COOLING_DOWN ((ota_resume_phase_t)4)

/**
 * @brief 可跨重启恢复的 OTA 元数据记录。
 *
 * verified_offset 表示已经成功写入并完成 NVS 检查点的镜像前缀长度；
 * 恢复时必须对 manifest、URL、SHA、目标分区和 HTTP Content-Range 再次校验。
 */
typedef struct {
    uint32_t schema_version; /**< 记录布局版本，必须等于 OTA_STATE_STORE_SCHEMA_VERSION。 */
    uint32_t expected_size; /**< 清单声明的完整镜像长度，单位为字节。 */
    uint32_t verified_offset; /**< 已写入并持久化检查点的镜像前缀长度，单位为字节。 */
    uint32_t retry_count; /**< 网络类失败后的重试次数，不包含终端校验失败。 */
    uint8_t sha256[NATIVE_OTA_SHA256_SIZE]; /**< 清单中的原始 SHA-256 摘要。 */
    char artifact_id[NATIVE_OTA_ARTIFACT_ID_SIZE]; /**< 服务端发布物唯一 ID。 */
    char version[sizeof(((esp_app_desc_t *)0)->version)]; /**< 目标应用版本字符串。 */
    char url[NATIVE_OTA_URL_SIZE]; /**< 目标固件 HTTPS URL。 */
    char etag[128]; /**< 服务器 ETag，用于确认续传内容仍是同一版本。 */
    uint8_t target_partition_subtype; /**< 目标 OTA 分区 subtype，防止恢复到错误槽位。 */
    uint8_t phase; /**< ota_resume_phase_t 的持久化值，范围为 DOWNLOADING～QUARANTINED。 */
    uint16_t reserved; /**< 保留字段，用于维持布局对齐和后续扩展空间，当前不参与恢复匹配。 */
    uint32_t failure_reason; /**< 隔离时保存的 native_ota_failure_reason_t 数值；未失败时为 0。 */
    uint32_t cooldown_count;
} ota_resume_record_t;

/**
 * @brief 读取 OTA 恢复记录。
 *
 * @param[out] record 输出记录，不允许为 NULL。
 * @return ESP_OK 读取到有效记录。
 * @return ESP_ERR_NOT_FOUND 没有记录。
 * @return 其他 esp_err_t NVS 读取或 schema 校验失败。
 *
 * @note 失败时会清零输出结构体；必须在 nvs_flash_init() 成功后、普通任务上下文调用。
 */
esp_err_t ota_state_store_load(ota_resume_record_t *record);

/**
 * @brief 原子地保存一条 OTA 恢复记录。
 *
 * @param[in] record 待保存记录，不允许为 NULL。
 * @return ESP_OK 保存并提交成功。
 * @return 其他 esp_err_t NVS 写入或提交失败。
 *
 * @note 调用会执行 nvs_commit() 并写 Flash；不能在中断上下文调用。
 */
esp_err_t ota_state_store_save(const ota_resume_record_t *record);

/**
 * @brief 删除 OTA 恢复记录。
 *
 * @return ESP_OK 删除成功或记录本来不存在。
 * @return 其他 esp_err_t NVS 操作失败。
 *
 * @note 删除会提交 NVS；它只清理 ota_resume/record，不影响 ota_report 状态。
 */
esp_err_t ota_state_store_clear(void);

/**
 * @brief 判断记录是否对应同一个 artifact。
 *
 * @param[in] record 已加载记录，不允许为 NULL。
 * @param[in] manifest 当前服务器清单，不允许为 NULL。
 * @return true artifact_id、版本、大小、URL 和 SHA-256 全部一致。
 * @return false 任一字段不同或参数无效。
 *
 * @note 不比较 job_id、安全版本或过期时间；这些字段不参与断点对象身份判定。
 */
bool ota_state_store_matches_manifest(const ota_resume_record_t *record,
                                      const native_ota_manifest_t *manifest);

/**
 * @brief 用当前运行版本清理已经提交并成功启动的旧记录。
 *
 * @param[in] running_version 当前运行镜像版本，不允许为 NULL。
 * @return ESP_OK 清理完成或无需清理。
 * @return 其他 esp_err_t NVS 读取/删除失败。
 *
 * @note 只删除 phase 为 READY_TO_COMMIT 且版本等于当前运行版本的记录，避免误删
 *       其他 artifact 的下载断点或隔离记录。
 */
esp_err_t ota_state_store_reconcile_running_version(const char *running_version);

#ifdef __cplusplus
}
#endif
