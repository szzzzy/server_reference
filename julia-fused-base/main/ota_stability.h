/**
 * @file    ota_stability.h
 * @brief   OTA 断点恢复、镜像完整性与提交前稳定性检查接口。
 *
 * 基于 ESP-IDF 官方 native OTA 流程，提供恢复记录、HTTP Range 一致性、镜像头
 * 预检、分区摘要和提交前条件检查。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_app_desc.h"
#include "esp_app_format.h"
#include "esp_err.h"
#include "esp_partition.h"

#include "native_ota_example.h"
#include "ota_state_store.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 单次完整镜像头预检所需的最小字节数：镜像头、首个 segment 头和应用描述符的总和。
 */
#define OTA_STABILITY_IMAGE_HEADER_SIZE (sizeof(esp_image_header_t) + \
                                         sizeof(esp_image_segment_header_t) + \
                                         sizeof(esp_app_desc_t))

/**
 * @brief 以当前清单和目标分区初始化下载恢复记录。
 *
 * @param[out] record    待初始化记录，不允许为 NULL。
 * @param[in]  manifest  已校验服务器清单，不允许为 NULL。
 * @param[in]  partition 目标 OTA 分区，不允许为 NULL。
 *
 * @note 仅初始化内存；调用者必须另行调用 ota_state_store_save() 持久化。
 */
void ota_stability_record_init(ota_resume_record_t *record,
                               const native_ota_manifest_t *manifest,
                               const esp_partition_t *partition);

/**
 * @brief 按 Flash Encryption 写入约束向下对齐恢复偏移。
 *
 * @param[in] offset 原始偏移，单位为字节。
 * @return 未启用 Flash Encryption 时返回原值；启用时返回 16 字节对齐值。
 *
 * @note 向下对齐可能丢弃尚未持久化到安全写入边界的尾部数据，调用方应从返回值继续
 *       请求 Range；函数不修改 Flash 内容。
 */
size_t ota_stability_normalize_resume_offset(size_t offset);

/**
 * @brief 按固定间隔更新并保存断点续传检查点。
 *
 * @param[in,out] record 待更新的恢复记录，不允许为 NULL。
 * @param[in]     offset 已写入镜像长度，单位为字节。
 * @param[in]     force  为 true 时立即保存。
 * @return ESP_OK 未到保存间隔或保存成功。
 * @return 其他 esp_err_t NVS 写入失败。
 *
 * @note offset 会先限制到 expected_size；非 force 调用只有跨过
 *       OTA_STATE_STORE_CHECKPOINT_BYTES 才会触发一次 NVS 写入。
 */
esp_err_t ota_stability_save_checkpoint(ota_resume_record_t *record, size_t offset, bool force);

/**
 * @brief 将当前 artifact 标记为终端校验失败并持久化隔离状态。
 *
 * @param[in,out] record 当前恢复记录，可为 NULL。
 * @param[in] reason     终端失败原因。
 *
 * @note 隔离后控制面会拒绝同一 artifact 的后续下载；保存失败只记录日志，不能在此处
 *       伪造隔离成功。
 */
void ota_stability_quarantine_record(ota_resume_record_t *record,
                                     native_ota_failure_reason_t reason);

/**
 * @brief 解析 `Content-Range: bytes start-end/total` 响应头。
 *
 * @param[in]  value       NUL 结尾响应头字符串，可为 NULL。
 * @param[out] range_start 返回响应起点，单位为字节。
 * @param[out] range_end   返回响应终点，包含该字节。
 * @param[out] total_size  返回完整镜像长度，单位为字节。
 * @return true 格式、边界和数值均有效。
 * @return false 参数或格式无效。
 *
 * @note 支持的语法是 `bytes start-end/total`，其中 start/end 为闭区间，total 必须
 *       大于 end；函数不会验证该区间是否与当前清单相符。
 */
bool ota_stability_parse_content_range(const char *value, size_t *range_start,
                                       size_t *range_end, size_t *total_size);

/**
 * @brief 校验网络接收的镜像头与当前服务器清单。
 *
 * @param[in]  header      至少包含 OTA_STABILITY_IMAGE_HEADER_SIZE 字节的缓冲区。
 * @param[in]  header_size 缓冲区实际长度，单位为字节。
 * @param[in]  manifest    已校验服务器清单，不允许为 NULL。
 * @param[out] app_desc    接收镜像应用描述符，不允许为 NULL。
 * @return ESP_OK 头部、芯片、项目、版本和安全版本均匹配。
 * @return 其他 esp_err_t 镜像头或清单不匹配。
 *
 * @note 此检查必须发生在 esp_ota_begin() 前；它只读取网络缓冲区，不擦写目标分区。
 */
esp_err_t ota_stability_validate_image_header(const uint8_t *header, size_t header_size,
                                              const native_ota_manifest_t *manifest,
                                              esp_app_desc_t *app_desc);

/**
 * @brief 读取目标分区头并校验其仍对应当前清单。
 *
 * @param[in]  partition 目标 OTA 分区，不允许为 NULL。
 * @param[in]  manifest  已校验服务器清单，不允许为 NULL。
 * @param[out] app_desc  接收镜像应用描述符，不允许为 NULL。
 * @return ESP_OK 分区前缀可安全用于恢复。
 * @return 其他 esp_err_t Flash 读取或镜像校验失败。
 *
 * @note 仅当恢复记录的 target_partition_subtype 与当前分区一致时才应调用；本函数
 *       本身只验证分区头和清单，不验证已写入长度。
 */
esp_err_t ota_stability_validate_partition_header(const esp_partition_t *partition,
                                                  const native_ota_manifest_t *manifest,
                                                  esp_app_desc_t *app_desc);

/**
 * @brief 计算目标分区中实际写入镜像前缀的 SHA-256。
 *
 * @param[in]  partition 已写入固件的分区，不允许为 NULL。
 * @param[in]  length    有效镜像长度，单位为字节。
 * @param[out] output    接收 NATIVE_OTA_SHA256_SIZE 字节摘要的缓冲区。
 * @return ESP_OK 摘要计算成功。
 * @return 其他 esp_err_t Flash 或 PSA Crypto 操作失败。
 *
 * @note 摘要范围为 [0, length) 的实际镜像前缀，不包含 OTA 分区剩余空间。
 */
esp_err_t ota_stability_calculate_partition_sha256(const esp_partition_t *partition,
                                                   size_t length,
                                                   uint8_t output[NATIVE_OTA_SHA256_SIZE]);

/**
 * @brief 检查镜像切换启动分区前的资源、板级和安全条件。
 *
 * @param[in] manifest  已验证的服务器清单，不允许为 NULL。
 * @param[in] partition 已完整写入的目标 OTA 分区，不允许为 NULL。
 * @return NATIVE_OTA_FAILURE_NONE 可以提交启动分区。
 * @return 其他 native_ota_failure_reason_t 指示应保留记录或拒绝提交的原因。
 *
 * @note 函数只检查条件；调用方仍需在返回成功后调用 esp_ota_set_boot_partition()，
 *       失败时保留 READY_TO_COMMIT 记录以便下次启动继续尝试。
 */
native_ota_failure_reason_t ota_stability_pre_commit_check(
    const native_ota_manifest_t *manifest, const esp_partition_t *partition);

#ifdef __cplusplus
}
#endif
