/**
 * @file    ota_stability.c
 * @brief   OTA 恢复、完整性与提交安全增强实现。
 *
 * 基于 ESP-IDF 官方 native OTA 流程，提供检查点、镜像预检和提交前校验。
 *
 * 模块关系：
 * - 由 native_ota_example.c 调用，决定是否能够安全恢复、校验和提交 OTA 镜像；
 * - 通过 ota_state_store.c 保存断点和 artifact 隔离状态；
 * - 使用 ESP-IDF 分区/镜像 API、Flash Encryption eFuse 和 PSA Crypto 读取与校验数据；
 * - 通过两个弱符号钩子向板级代码提供电源和关键业务状态检查入口。
 *
 * 线程安全与限制：
 * - 本模块不维护共享任务状态，调用者负责串行化同一 OTA 分区的操作；
 * - 分区读取、SHA-256 和 NVS 检查点均可能阻塞，只能在普通任务上下文调用；
 * - 本模块不创建任务、不访问 MQTT，也不设置启动分区。
 */
#include "ota_stability.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "esp_efuse.h"
#include "esp_flash_encrypt.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_secure_boot.h"

#include "psa/crypto.h"

/** 本模块日志标签。 */
static const char *TAG = "ota_stability";

/** 分区摘要计算每次读取的 Flash 数据量，单位为字节；固定大小避免大栈缓冲区。 */
#define OTA_STABILITY_HASH_BUFFER_SIZE 1024U

/**
 * @brief 返回供日志、NVS 和测试使用的稳定失败原因名称。
 *
 * @param[in] reason 失败原因枚举值，可为未知值。
 * @return 指向静态只读字符串的指针；未知值返回 UNKNOWN。
 */
const char *native_ota_failure_reason_name(native_ota_failure_reason_t reason)
{
    switch (reason) {
    case NATIVE_OTA_FAILURE_NONE: return "NONE";
    case NATIVE_OTA_FAILURE_PRECONDITION_LOW_POWER: return "PRECONDITION_LOW_POWER";
    case NATIVE_OTA_FAILURE_NETWORK_TIMEOUT: return "NETWORK_TIMEOUT";
    case NATIVE_OTA_FAILURE_TLS_VERIFY_FAILED: return "TLS_VERIFY_FAILED";
    case NATIVE_OTA_FAILURE_HTTP_STATUS_INVALID: return "HTTP_STATUS_INVALID";
    case NATIVE_OTA_FAILURE_RANGE_MISMATCH: return "RANGE_MISMATCH";
    case NATIVE_OTA_FAILURE_IMAGE_TOO_LARGE: return "IMAGE_TOO_LARGE";
    case NATIVE_OTA_FAILURE_IMAGE_HEADER_INVALID: return "IMAGE_HEADER_INVALID";
    case NATIVE_OTA_FAILURE_HASH_MISMATCH: return "HASH_MISMATCH";
    case NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED: return "IMAGE_VALIDATE_FAILED";
    case NATIVE_OTA_FAILURE_BOOT_SELF_TEST_FAILED: return "BOOT_SELF_TEST_FAILED";
    case NATIVE_OTA_FAILURE_ROLLBACK_UNAVAILABLE: return "ROLLBACK_UNAVAILABLE";
    case NATIVE_OTA_FAILURE_MANIFEST_INVALID: return "MANIFEST_INVALID";
    case NATIVE_OTA_FAILURE_ARTIFACT_QUARANTINED: return "ARTIFACT_QUARANTINED";
    case NATIVE_OTA_FAILURE_BOOT_PARTITION_SET_FAILED: return "BOOT_PARTITION_SET_FAILED";
    case NATIVE_OTA_FAILURE_NVS_WRITE_FAILED: return "NVS_WRITE_FAILED";
    default: return "UNKNOWN";
    }
}

/**
 * @brief 默认的板级电源提交前检查钩子。
 *
 * @return ESP_OK 当前示例不附加电源限制。
 *
 * @note 产品代码可提供同名强符号覆盖该弱实现。
 */
__attribute__((weak)) esp_err_t native_ota_check_power(void)
{
    /* 示例默认没有板级电源测量；产品固件可用强符号替换此实现。 */
    return ESP_OK;
}

/**
 * @brief 默认的板级关键业务状态检查钩子。
 *
 * @return ESP_OK 当前示例不附加业务状态限制。
 *
 * @note 产品代码可提供同名强符号覆盖该弱实现。
 */
__attribute__((weak)) esp_err_t native_ota_check_business_state(void)
{
    /* 示例默认没有关键业务状态机；产品固件可用强符号替换此实现。 */
    return ESP_OK;
}

/**
 * @brief 初始化下载 artifact 的可恢复 NVS 记录。
 *
 * @param[out] record    待写入的记录。
 * @param[in]  manifest  当前已校验清单。
 * @param[in]  partition 当前目标 OTA 分区。
 *
 * @note 只改写调用者提供的内存，不提交 NVS。
 */
void ota_stability_record_init(ota_resume_record_t *record,
                               const native_ota_manifest_t *manifest,
                               const esp_partition_t *partition)
{
    /* 先清零，保证保留字段、ETag、失败原因和偏移从已知初始值开始。 */
    memset(record, 0, sizeof(*record));
    record->schema_version = OTA_STATE_STORE_SCHEMA_VERSION;
    record->expected_size = manifest->image_size;
    record->target_partition_subtype = partition->subtype;
    record->phase = OTA_RESUME_PHASE_DOWNLOADING;
    memcpy(record->sha256, manifest->sha256, sizeof(record->sha256));
    strncpy(record->artifact_id, manifest->artifact_id, sizeof(record->artifact_id) - 1U);
    strncpy(record->version, manifest->version, sizeof(record->version) - 1U);
    strncpy(record->url, manifest->url, sizeof(record->url) - 1U);
}

/**
 * @brief 将恢复偏移规范化为当前 Flash 写入模式允许的边界。
 *
 * @param[in] offset 原始偏移，单位为字节。
 * @return 可传给 esp_ota_resume() 的向下对齐偏移。
 */
size_t ota_stability_normalize_resume_offset(size_t offset)
{
    /* Flash Encryption 的 OTA 恢复写入要求 16 字节边界；向下取整才能从安全前缀继续。
     * IDF 5.5 的 API 是 esp_flash_encryption_enabled()（6.x 的
     * esp_efuse_is_flash_encryption_enabled() 已移除）。 */
    if (esp_flash_encryption_enabled()) {
        return offset & ~(size_t)0x0f;
    }
    return offset;
}

/**
 * @brief 以受限频率提交下载检查点。
 *
 * @param[in,out] record 恢复记录。
 * @param[in] offset 当前已写入长度，单位为字节。
 * @param[in] force 是否绕过最小检查点间隔。
 * @return ESP_OK 无需保存或保存成功；其他值表示 NVS 写入失败。
 */
esp_err_t ota_stability_save_checkpoint(ota_resume_record_t *record, size_t offset, bool force)
{
    /* 网络读取长度可能暂时超过清单，先截断到清单长度，避免持久化无效偏移。 */
    size_t normalized = ota_stability_normalize_resume_offset(offset);
    if (normalized > record->expected_size) {
        normalized = record->expected_size;
    }
    /* 以固定字节间隔写 NVS，减少掉电恢复收益与 Flash 擦写次数之间的冲突。 */
    if (!force && normalized < (size_t)record->verified_offset +
                              OTA_STATE_STORE_CHECKPOINT_BYTES) {
        return ESP_OK;
    }

    record->verified_offset = (uint32_t)normalized;
    return ota_state_store_save(record);
}

/**
 * @brief 将不可重试的 artifact 保存为隔离状态。
 *
 * @param[in,out] record 当前恢复记录，可为 NULL。
 * @param[in] reason 对应的终端校验失败原因。
 *
 * @note NVS 保存失败只记录日志，调用者仍按原失败路径清理资源。
 */
void ota_stability_quarantine_record(ota_resume_record_t *record,
                                     native_ota_failure_reason_t reason)
{
    if (record == NULL) {
        return;
    }

    /* 终端校验错误不能通过普通网络重试修复，因此阻止同一 artifact 无限下载。 */
    record->phase = OTA_RESUME_PHASE_QUARANTINED;
    record->failure_reason = (uint32_t)reason;
    esp_err_t err = ota_state_store_save(record);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to persist quarantined artifact %s: %s",
                 record->artifact_id, esp_err_to_name(err));
    }
}

/**
 * @brief 严格解析 HTTP Range 恢复响应的 Content-Range 字段。
 *
 * @param[in] value 响应头文本。
 * @param[out] range_start、range_end、total_size 接收已解析的字节边界。
 * @return true 格式及数值范围有效；false 表示不能安全续传。
 */
bool ota_stability_parse_content_range(const char *value, size_t *range_start,
                                       size_t *range_end, size_t *total_size)
{
    if (value == NULL || range_start == NULL || range_end == NULL || total_size == NULL ||
        strncmp(value, "bytes ", 6) != 0) {
        return false;
    }

    /* 使用无符号解析并逐段检查分隔符，避免把部分合法数字当成完整 Range。 */
    char *end = NULL;
    unsigned long long start = strtoull(value + 6, &end, 10);
    if (end == value + 6 || *end != '-') {
        return false;
    }
    unsigned long long last = strtoull(end + 1, &end, 10);
    if (end == NULL || *end != '/') {
        return false;
    }
    unsigned long long total = strtoull(end + 1, &end, 10);
    if (*end != '\0' || start > SIZE_MAX || last < start || total == 0U ||
        total > SIZE_MAX || last >= total) {
        return false;
    }

    /* 只在所有边界检查通过后写出结果，调用者不会看到半解析状态。 */
    /* 三个输出值的单位均为字节；range_end 是闭区间终点，total 是完整对象长度。 */
    *range_start = (size_t)start;
    *range_end = (size_t)last;
    *total_size = (size_t)total;
    return true;
}

/**
 * @brief 在任何 Flash 擦写前检查网络镜像头是否符合当前 artifact。
 *
 * @param[in] header 网络接收的镜像前缀。
 * @param[in] header_size 前缀长度，单位为字节。
 * @param[in] manifest 当前清单。
 * @param[out] app_desc 接收复制出的应用描述符。
 * @return ESP_OK 成功；其他值表示芯片、项目、版本或安全版本不匹配。
 */
esp_err_t ota_stability_validate_image_header(const uint8_t *header, size_t header_size,
                                              const native_ota_manifest_t *manifest,
                                              esp_app_desc_t *app_desc)
{
    if (header == NULL || manifest == NULL || app_desc == NULL ||
        header_size < OTA_STABILITY_IMAGE_HEADER_SIZE) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 先验证 magic/chip，再读取紧随其后的应用描述符，避免把任意网络数据解释为版本。 */
    const esp_image_header_t *image_header = (const esp_image_header_t *)header;
    if (image_header->magic != ESP_IMAGE_HEADER_MAGIC) {
        ESP_LOGE(TAG, "OTA image header has invalid magic byte 0x%02x", image_header->magic);
        return ESP_ERR_OTA_VALIDATE_FAILED;
    }
#ifdef CONFIG_IDF_FIRMWARE_CHIP_ID
    if (image_header->chip_id != CONFIG_IDF_FIRMWARE_CHIP_ID) {
        ESP_LOGE(TAG, "OTA image chip id %u does not match this target", image_header->chip_id);
        return ESP_ERR_INVALID_VERSION;
    }
#endif

    const size_t app_desc_offset = sizeof(esp_image_header_t) +
                                   sizeof(esp_image_segment_header_t);
    memcpy(app_desc, header + app_desc_offset, sizeof(*app_desc));
    if (app_desc->magic_word != ESP_APP_DESC_MAGIC_WORD) {
        ESP_LOGE(TAG, "OTA image application descriptor is invalid");
        return ESP_ERR_OTA_VALIDATE_FAILED;
    }
    if (strncmp(app_desc->project_name, CONFIG_OTA_IMAGE_PROJECT_NAME,
                sizeof(app_desc->project_name)) != 0) {
        ESP_LOGE(TAG, "OTA project_name %s does not match expected %s",
                 app_desc->project_name, CONFIG_OTA_IMAGE_PROJECT_NAME);
        return ESP_ERR_INVALID_VERSION;
    }
    if (strncmp(app_desc->version, manifest->version, sizeof(app_desc->version)) != 0) {
        ESP_LOGE(TAG, "Manifest version %s does not match image version %s",
                 manifest->version, app_desc->version);
        return ESP_ERR_INVALID_VERSION;
    }
    if (app_desc->secure_version < manifest->security_version) {
        ESP_LOGE(TAG, "Image secure_version %" PRIu32
                 " is below manifest requirement %" PRIu32,
                 app_desc->secure_version, manifest->security_version);
        return ESP_ERR_OTA_SMALL_SEC_VER;
    }

    return ESP_OK;
}

/**
 * @brief 从已写入的 OTA 分区重读镜像头，验证断点可安全恢复。
 *
 * @param[in] partition 目标 OTA 分区。
 * @param[in] manifest 当前清单。
 * @param[out] app_desc 接收应用描述符。
 * @return ESP_OK 分区前缀仍对应当前 artifact；其他值表示不能续传。
 */
esp_err_t ota_stability_validate_partition_header(const esp_partition_t *partition,
                                                  const native_ota_manifest_t *manifest,
                                                  esp_app_desc_t *app_desc)
{
    uint8_t header[OTA_STABILITY_IMAGE_HEADER_SIZE];
    esp_err_t err = esp_partition_read(partition, 0, header, sizeof(header));
    if (err != ESP_OK) {
        return err;
    }

    return ota_stability_validate_image_header(header, sizeof(header), manifest, app_desc);
}

/**
 * @brief 计算目标分区实际写入范围的 SHA-256。
 *
 * @param[in] partition 已写入镜像的分区。
 * @param[in] length 有效镜像长度，单位为字节。
 * @param[out] output 接收固定长度摘要。
 * @return ESP_OK 成功；其他值表示 Flash 或 PSA Crypto 操作失败。
 *
 * @note 以固定块同步读取 Flash，可能阻塞，只能由 OTA 任务调用。
 */
esp_err_t ota_stability_calculate_partition_sha256(const esp_partition_t *partition,
                                                   size_t length,
                                                   uint8_t output[NATIVE_OTA_SHA256_SIZE])
{
    if (partition == NULL || output == NULL || length == 0U) {
        return ESP_ERR_INVALID_ARG;
    }

    /* PSA operation 只覆盖目标分区的有效镜像前缀，不把分区剩余擦除区域计入摘要。 */
    psa_hash_operation_t operation = PSA_HASH_OPERATION_INIT;
    uint8_t read_buffer[OTA_STABILITY_HASH_BUFFER_SIZE];
    size_t output_len = 0;
    size_t offset = 0;

    psa_status_t status = psa_hash_setup(&operation, PSA_ALG_SHA_256);
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "Failed to set up PSA SHA-256, status=%d", (int)status);
        return ESP_FAIL;
    }

    /* 分块读取限制栈占用，并使每次 Flash 读取都能在失败时明确终止 hash。 */
    while (offset < length) {
        size_t chunk_len = length - offset;
        if (chunk_len > sizeof(read_buffer)) {
            chunk_len = sizeof(read_buffer);
        }

        esp_err_t err = esp_partition_read(partition, offset, read_buffer, chunk_len);
        if (err != ESP_OK) {
            psa_hash_abort(&operation);
            return err;
        }

        status = psa_hash_update(&operation, read_buffer, chunk_len);
        if (status != PSA_SUCCESS) {
            ESP_LOGE(TAG, "Failed to update PSA SHA-256, status=%d", (int)status);
            psa_hash_abort(&operation);
            return ESP_FAIL;
        }
        offset += chunk_len;
    }

    status = psa_hash_finish(&operation, output, NATIVE_OTA_SHA256_SIZE, &output_len);
    if (status != PSA_SUCCESS || output_len != NATIVE_OTA_SHA256_SIZE) {
        ESP_LOGE(TAG, "Failed to finish PSA SHA-256, status=%d, length=%u",
                 (int)status, (unsigned int)output_len);
        psa_hash_abort(&operation);
        return ESP_FAIL;
    }

    return ESP_OK;
}

/**
 * @brief 判断经过完整性校验的镜像是否满足切换启动分区的条件。
 *
 * @param[in] manifest 当前清单。
 * @param[in] partition 完整写入的目标分区。
 * @return NATIVE_OTA_FAILURE_NONE 可以提交；其他值说明应推迟或拒绝提交。
 *
 * @note 会读取堆、Secure Boot 和可覆盖的板级钩子，但不设置启动分区或重启。
 */
native_ota_failure_reason_t ota_stability_pre_commit_check(
    const native_ota_manifest_t *manifest, const esp_partition_t *partition)
{
    /* 提交前再次检查分区容量，防止调用方绕过下载阶段直接提交超大清单。 */
    if (manifest == NULL || partition == NULL || manifest->image_size == 0U ||
        manifest->image_size > partition->size) {
        return NATIVE_OTA_FAILURE_IMAGE_TOO_LARGE;
    }
    /* 镜像切换会触发后续重启；保持最低可用堆，避免在提交临界点进入资源枯竭状态。 */
    if (heap_caps_get_free_size(MALLOC_CAP_8BIT) < CONFIG_OTA_MIN_FREE_HEAP) {
        ESP_LOGE(TAG, "Free heap is below OTA commit threshold");
        return NATIVE_OTA_FAILURE_PRECONDITION_LOW_POWER;
    }
    if (native_ota_check_power() != ESP_OK) {
        ESP_LOGW(TAG, "Board power check requested VERIFIED_WAIT_COMMIT");
        return NATIVE_OTA_FAILURE_PRECONDITION_LOW_POWER;
    }
    if (native_ota_check_business_state() != ESP_OK) {
        ESP_LOGW(TAG, "Board business-state check deferred OTA commit");
        return NATIVE_OTA_FAILURE_BOOT_SELF_TEST_FAILED;
    }
#if CONFIG_OTA_REQUIRE_SIGNED_IMAGE
    if (!esp_secure_boot_enabled()) {
        ESP_LOGE(TAG, "Signed OTA images are required but Secure Boot is not enabled");
        return NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
    }
#endif

    return NATIVE_OTA_FAILURE_NONE;
}
