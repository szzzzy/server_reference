/**
 * @file    ota_control_plane.h
 * @brief   OTA 控制面请求构建与服务器清单解析接口。
 *
 * 本模块只处理 MQTT 控制消息：生成版本检查请求、关联 request_id，
 * 并把服务器 JSON 深拷贝为已校验的 native_ota_manifest_t。它不创建 FreeRTOS
 * 下载任务、不访问 OTA 分区，也不执行 HTTP 固件下载。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#include "native_ota_example.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 解析并校验一条 OTA 服务器响应。
 *
 * 当响应声明 update=false 或目标版本已经运行时，函数返回 ESP_OK 且将
 * download_requested 置为 false；当需要下载时，manifest 返回完整深拷贝。
 *
 * @param[in]  json               JSON 数据首地址，不要求以 NUL 结尾。
 * @param[in]  json_len           JSON 有效长度，单位为字节。
 * @param[out] manifest           接收已校验清单的结构体，不允许为 NULL。
 * @param[out] download_requested 接收是否应创建下载任务的标志，不允许为 NULL。
 * @return ESP_OK 响应有效。
 * @return ESP_ERR_INVALID_ARG JSON、设备身份、清单字段、有效期或 URL 无效。
 * @return ESP_ERR_INVALID_STATE request_id 不匹配或 artifact 已被隔离。
 * @return ESP_ERR_NO_MEM cJSON 临时对象创建失败。
 *
 * @note 函数可在通信事件任务中调用，但会分配 cJSON 临时对象，不能在中断中调用。
 */
esp_err_t ota_control_plane_parse_server_response(const char *json, size_t json_len,
                                                  native_ota_manifest_t *manifest,
                                                  bool *download_requested);

#ifdef __cplusplus
}
#endif
