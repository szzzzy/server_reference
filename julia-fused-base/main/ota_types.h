/**
 * @file    ota_types.h
 * @brief   固件 OTA 与音频素材下载共用的公共类型、尺寸常量与失败原因分类。
 *
 * 本头文件只承载跨模块共享的值类型，不包含任何模块私有状态：
 * - 控制面协议尺寸常量（JSON 上限、ID/URL/摘要长度）；
 * - native_ota_manifest_t（固件镜像清单）与 native_audio_manifest_t（音频素材清单）；
 * - native_ota_failure_reason_t 统一失败分类，固件 OTA 与音频下载共用。
 *
 * 兼容性约束：
 * - failure_reason 会以 error_code 序列化进状态事件，也会写入 NVS 断点记录，
 *   因此新增取值只能追加在枚举末尾，不得改变已有数值；
 * - 各 *_SIZE 常量描述含末尾 NUL 的缓冲区容量。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_app_desc.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 单条控制面服务器响应允许的最大字节数，不包含接收缓冲区末尾的 NUL。 */
#define NATIVE_OTA_JSON_MAX_LEN 1024

/** 版本检查请求 JSON 的最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_CHECK_JSON_SIZE 512

/** 设备唯一标识字符串的最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_DEVICE_ID_SIZE 32

/** 控制面响应中的 request_id 最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_REQUEST_ID_SIZE 32

/** 云端 OTA job_id 最大长度，包含末尾 NUL；服务端未提供时保持为空。 */
#define NATIVE_OTA_JOB_ID_SIZE 64

/** OTA artifact_id 最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_ARTIFACT_ID_SIZE 96

/** OTA 产品标识最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_PRODUCT_ID_SIZE 64

/** OTA 硬件版本最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_HARDWARE_VERSION_SIZE 32

/** OTA URL 最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_URL_SIZE 256

/** SHA-256 摘要长度，单位为字节。 */
#define NATIVE_OTA_SHA256_SIZE 32

/** 音频素材唯一 ID 最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_AUDIO_ID_SIZE 96

/** 音频素材版本字符串最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_AUDIO_VERSION_SIZE 32

/** 设备尚未安装任何音频素材时对外报告的版本占位值。 */
#define NATIVE_OTA_AUDIO_VERSION_UNKNOWN "unknown"

/**
 * @brief 下载/升级失败原因的统一分类。
 *
 * 分类用于串口日志、状态事件 error_code、恢复策略和测试断言；具体底层
 * esp_err_t 仍会同时记录。固件 OTA 与音频素材下载共用本枚举。
 */
typedef enum {
    NATIVE_OTA_FAILURE_NONE = 0, /**< 未发生失败。 */
    NATIVE_OTA_FAILURE_PRECONDITION_LOW_POWER, /**< 电源、堆空间或提交前条件不满足。 */
    NATIVE_OTA_FAILURE_NETWORK_TIMEOUT, /**< 网络连接或读取失败。 */
    NATIVE_OTA_FAILURE_TLS_VERIFY_FAILED, /**< TLS/HTTPS 客户端初始化或校验失败。 */
    NATIVE_OTA_FAILURE_HTTP_STATUS_INVALID, /**< HTTP 状态码或完整响应长度不符合预期。 */
    NATIVE_OTA_FAILURE_RANGE_MISMATCH, /**< Range 或 Content-Range 与断点不一致。 */
    NATIVE_OTA_FAILURE_IMAGE_TOO_LARGE, /**< 镜像超过清单长度或目标分区容量。 */
    NATIVE_OTA_FAILURE_IMAGE_HEADER_INVALID, /**< 镜像头、项目名、版本或安全版本不匹配。 */
    NATIVE_OTA_FAILURE_HASH_MISMATCH, /**< 下载内容的 SHA-256 与清单不一致。 */
    NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED, /**< ESP-IDF 镜像完整性或可启动性校验失败。 */
    NATIVE_OTA_FAILURE_BOOT_SELF_TEST_FAILED, /**< 新镜像首次启动的本地健康检查失败。 */
    NATIVE_OTA_FAILURE_ROLLBACK_UNAVAILABLE, /**< 失败时没有可用的旧镜像可回滚。 */
    NATIVE_OTA_FAILURE_MANIFEST_INVALID, /**< 服务器清单格式、身份或有效期校验失败。 */
    NATIVE_OTA_FAILURE_ARTIFACT_QUARANTINED, /**< 同一 artifact 已因终端错误被隔离。 */
    NATIVE_OTA_FAILURE_BOOT_PARTITION_SET_FAILED, /**< 设置下次启动分区失败。 */
    NATIVE_OTA_FAILURE_NVS_WRITE_FAILED, /**< 断点记录写入或提交 NVS 失败。 */
    NATIVE_OTA_FAILURE_STORAGE_UNAVAILABLE, /**< 目标数据/OTA 分区缺失或不可用。 */
} native_ota_failure_reason_t;

/**
 * @brief 控制面返回的完整固件镜像清单。
 *
 * 清单由 MQTT 事件任务解析后复制到下载任务自己的堆对象中，
 * 因此下载任务不依赖通信模块接收缓冲区的生命周期。
 */
typedef struct {
    char request_id[NATIVE_OTA_REQUEST_ID_SIZE]; /**< 与本次检查请求对应的关联 ID。 */
    char job_id[NATIVE_OTA_JOB_ID_SIZE]; /**< 可选的云端任务 ID；未提供时为空。 */
    char artifact_id[NATIVE_OTA_ARTIFACT_ID_SIZE]; /**< 服务端发布物的唯一 ID。 */
    char product[NATIVE_OTA_PRODUCT_ID_SIZE]; /**< 目标产品/型号标识。 */
    char hardware_version[NATIVE_OTA_HARDWARE_VERSION_SIZE]; /**< 目标硬件版本。 */
    char version[sizeof(((esp_app_desc_t *)0)->version)]; /**< 镜像内的应用版本字符串。 */
    char url[NATIVE_OTA_URL_SIZE]; /**< 固件 HTTPS 下载地址，包含末尾 NUL。 */
    uint8_t sha256[NATIVE_OTA_SHA256_SIZE]; /**< 固件 bin 的原始 SHA-256 摘要。 */
    uint32_t image_size; /**< 固件 bin 的实际长度，单位为字节。 */
    uint32_t security_version; /**< 镜像允许的最低 secure_version，无单位的单调安全版本号。 */
    int64_t expires_at; /**< 清单过期时间，Unix 时间戳，单位为秒；当前时间可用时必须晚于它。 */
    bool force_update; /**< 服务器强制更新标志：true 时跳过“目标版本必须更新”检查（紧急回退）。 */
} native_ota_manifest_t;

/**
 * @brief 音频控制面返回的完整音频素材清单。
 *
 * 由音频控制面深拷贝生成，音频下载任务取得所有权后即可安全使用；
 * 目标写入位置是音频数据分区，而不是任何 OTA 应用槽。
 */
typedef struct {
    char request_id[NATIVE_OTA_REQUEST_ID_SIZE]; /**< 与本次音频检查请求对应的关联 ID。 */
    char audio_id[NATIVE_OTA_AUDIO_ID_SIZE]; /**< 服务端音频素材唯一 ID。 */
    char version[NATIVE_OTA_AUDIO_VERSION_SIZE]; /**< 音频素材版本字符串。 */
    char url[NATIVE_OTA_URL_SIZE]; /**< 音频 HTTPS 下载地址，包含末尾 NUL。 */
    uint8_t sha256[NATIVE_OTA_SHA256_SIZE]; /**< 音频文件的原始 SHA-256 摘要。 */
    uint32_t file_size; /**< 音频文件实际长度，单位为字节。 */
    int64_t expires_at; /**< 清单过期时间，Unix 时间戳，单位为秒。 */
} native_audio_manifest_t;

#ifdef __cplusplus
}
#endif
