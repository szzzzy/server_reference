/**
 * @file    audio_service.h
 * @brief   音频服务层：MQTT 音频控制面与音频下载引擎之间的业务协调入口。
 *
 * 本模块把已通过音频控制面校验的 audio_check_response 转换为音频下载动作：
 * - 解析并校验音频清单（转调 audio_control_plane）；
 * - 与固件 OTA / 已有音频下载互斥（OTA 优先，运行中拒绝并发下载）；
 * - 将下载请求交给 audio_engine。
 *
 * 本模块不持有音频运行状态：运行中判断统一转调 audio_engine。
 */
#pragma once

#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 初始化音频服务层。
 *
 * @return ESP_OK 初始化成功。
 *
 * @note 幂等；必须在 NVS 初始化完成后、网络启动前调用。
 * @note 当前服务层没有自有状态，本函数为装配点保留；不允许在中断上下文调用。
 */
esp_err_t audio_service_init(void);

/**
 * @brief 处理服务器返回的完整 audio_check_response。
 *
 * 完整 JSON 响应由 MQTT 模块完成分片重组后调用本函数。`update=false` 时只
 * 记录并返回 ESP_OK；需要下载时经互斥检查后交给音频引擎创建唯一下载任务。
 *
 * @param[in] json     JSON 数据首地址，不允许为 NULL，不要求以 NUL 结尾。
 * @param[in] json_len JSON 有效长度，范围为 1～NATIVE_OTA_JSON_MAX_LEN。
 *
 * @return ESP_OK 响应有效；可能无需下载，也可能已创建音频下载任务。
 * @return ESP_ERR_INVALID_ARG JSON 格式、字段、身份、清单或有效期无效。
 * @return ESP_ERR_INVALID_STATE request_id 过期，或已有 OTA/音频下载任务运行。
 * @return ESP_ERR_NO_MEM 参数分配或任务创建失败。
 *
 * @note 可由 MQTT 事件任务调用；函数只解析元数据并调度任务，不在调用者
 *       上下文中执行下载。不允许在中断上下文调用。
 */
esp_err_t audio_service_handle_response(const char *json, size_t json_len);

#ifdef __cplusplus
}
#endif
