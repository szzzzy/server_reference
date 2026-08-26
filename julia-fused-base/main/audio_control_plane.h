/**
 * @file    audio_control_plane.h
 * @brief   音频控制面请求构建与音频清单解析接口。
 *
 * 与 ota_control_plane 平行：只处理 MQTT 音频控制消息，生成音频检查请求、
 * 关联 request_id，并把服务器 JSON 深拷贝为已校验的 native_audio_manifest_t。
 * 不创建下载任务、不访问分区、不执行 HTTP 下载。
 *
 * 协议版本不升级：复用现有 MQTT/HTTPS 双通道与 schema_version=1 语义，
 * 仅新增 audio_* 报文类型与独立 topic 前缀。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#include "ota_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 生成包含设备身份、产品标识和当前音频版本的音频检查请求。
 *
 * 每次成功生成都会记录一个新的 request_id，服务器响应必须严格匹配最近
 * 一次请求。设备标识与 OTA 共用 native_ota_get_device_id() 规则。
 *
 * @param[in]  current_audio_version 设备当前已安装音频素材版本；未安装时传
 *                                   NATIVE_OTA_AUDIO_VERSION_UNKNOWN。
 * @param[out] json                  接收 NUL 结尾 JSON 的缓冲区。
 * @param[in]  json_size             缓冲区容量，建议使用 NATIVE_OTA_CHECK_JSON_SIZE。
 * @param[out] json_len              返回 JSON 有效字节数，不含末尾 NUL。
 *
 * @return ESP_OK 请求生成成功。
 * @return ESP_ERR_INVALID_ARG 参数无效或缓冲区不足。
 * @return ESP_ERR_NO_MEM 无法创建临时 cJSON 对象。
 * @return 其他 esp_err_t 读取设备标识失败。
 */
esp_err_t native_audio_build_check_request(const char *current_audio_version,
                                           char *json, size_t json_size, size_t *json_len);

/**
 * @brief 解析并校验一条 audio_check_response。
 *
 * 当响应声明 update=false 时返回 ESP_OK 且将 download_requested 置为 false；
 * 需要下载时输出完整深拷贝的音频清单。
 *
 * @param[in]  json                JSON 数据首地址，不要求以 NUL 结尾。
 * @param[in]  json_len            JSON 有效长度，单位为字节。
 * @param[out] manifest            接收已校验清单的结构体，不允许为 NULL。
 * @param[out] download_requested  接收是否应创建下载任务的标志，不允许为 NULL。
 *
 * @return ESP_OK 响应有效。
 * @return ESP_ERR_INVALID_ARG JSON、设备身份、清单字段、有效期或 URL 无效。
 * @return ESP_ERR_INVALID_STATE request_id 不匹配。
 * @return ESP_ERR_NO_MEM cJSON 临时对象创建失败。
 *
 * @note 可在通信事件任务中调用，但会分配 cJSON 临时对象，不能在中断中调用。
 */
esp_err_t audio_control_plane_parse_audio_response(const char *json, size_t json_len,
                                                   native_audio_manifest_t *manifest,
                                                   bool *download_requested);

#ifdef __cplusplus
}
#endif
