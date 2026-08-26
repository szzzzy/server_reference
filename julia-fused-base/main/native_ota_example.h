/**
 * @file    native_ota_example.h
 * @brief   native OTA 版本检查协议和公共触发接口。
 *
 * 本头文件向通信模块暴露设备标识、版本检查请求生成和服务器响应处理接口，
 * 不公开 HTTP、Flash 分区或 OTA 写入句柄。MQTT 模块因此共享
 * 同一套按需升级协议、下载、校验和启动分区切换流程。
 *
 * 模块关系：
 * - mqtt_comm.c 调用请求构建和响应处理入口；
 * - ota_control_plane.c 负责 JSON 身份/清单校验；
 * - native_ota_example.c 负责唯一下载任务、HTTPS、Flash 写入和重启；
 * - ota_report.c 接收生命周期事件，但本头文件不暴露 MQTT 发送细节。
 *
 * @note 接口在 ESP-IDF 事件任务上下文中调用，不允许在中断上下文中调用。
 */
#pragma once

#include <stdint.h>
#include <stddef.h>

#include "esp_err.h"

#include "ota_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 将统一失败原因转换为稳定的日志字符串。
 *
 * @param[in] reason 失败原因枚举值。
 * @return 以 NUL 结尾的静态字符串；未知值返回 UNKNOWN。
 */
const char *native_ota_failure_reason_name(native_ota_failure_reason_t reason);

/**
 * @brief 板级电源/电压提交前检查钩子。
 *
 * 默认实现返回 ESP_OK；产品板级代码可以提供同名强符号覆盖它。
 *
 * @return ESP_OK 电源满足切换条件；其他值表示应暂缓切换。
 *
 * @note 由 OTA 任务在设置下次启动分区前同步调用；可读取板级电源监控，但不应启动网络。
 */
esp_err_t native_ota_check_power(void);

/**
 * @brief 板级关键业务状态提交前检查钩子。
 *
 * 默认实现返回 ESP_OK；产品板级代码可以提供同名强符号覆盖它。
 *
 * @return ESP_OK 当前允许切换启动分区；其他值表示设备仍处于关键业务状态。
 *
 * @note 由 OTA 任务在普通任务上下文调用；返回非 ESP_OK 时会保留 READY_TO_COMMIT 记录。
 */
esp_err_t native_ota_check_business_state(void);

/**
 * @brief 获取由芯片基础 MAC 地址生成的稳定设备标识。
 *
 * @param[out] device_id      接收 NUL 结尾设备标识的缓冲区，不允许为 NULL。
 * @param[in]  device_id_size 缓冲区容量，必须至少为 NATIVE_OTA_DEVICE_ID_SIZE 字节。
 *
 * @return ESP_OK 设备标识生成成功。
 * @return ESP_ERR_INVALID_ARG 输出参数无效或缓冲区过小。
 * @return 其他 esp_err_t 读取芯片基础 MAC 地址失败。
 */
esp_err_t native_ota_get_device_id(char *device_id, size_t device_id_size);

/**
 * @brief 生成包含设备身份、硬件信息和当前固件版本的 OTA 检查请求。
 *
 * @param[out] json      接收 NUL 结尾 JSON 的缓冲区，不允许为 NULL。
 * @param[in]  json_size 缓冲区容量，建议使用 NATIVE_OTA_CHECK_JSON_SIZE。
 * @param[out] json_len  返回 JSON 有效字节数，不包含末尾 NUL，不允许为 NULL。
 *
 * @return ESP_OK 请求生成成功。
 * @return ESP_ERR_INVALID_ARG 输出参数无效或缓冲区不足。
 * @return ESP_ERR_NO_MEM 无法创建临时 cJSON 对象。
 * @return 其他 esp_err_t 读取设备标识失败。
 */
esp_err_t native_ota_build_check_request(char *json, size_t json_size, size_t *json_len);

/**
 * @brief 处理服务器返回的 OTA 检查响应。
 *
 * `update=false` 时只记录“无需升级”并返回；`update=true` 时校验 version、url 和
 * sha256，确认目标版本不同于当前版本后创建 OTA 下载任务。为兼容原有测试工具，
 * 本接口仍接受 type 为 `ota` 的旧格式命令。
 *
 * @param[in] json      JSON 数据首地址，不允许为 NULL，不要求以 NUL 结尾。
 * @param[in] json_len  JSON 有效长度，范围为 1～NATIVE_OTA_JSON_MAX_LEN。
 *
 * @return ESP_OK 响应有效；可能无需升级，也可能已创建 OTA 任务。
 * @return ESP_ERR_INVALID_ARG JSON 格式或字段不符合协议。
 * @return ESP_ERR_INVALID_STATE 已有 OTA 任务正在运行。
 * @return ESP_ERR_NO_MEM 参数分配或任务创建失败。
 */
esp_err_t native_ota_handle_server_json(const char *json, size_t json_len);

/**
 * @brief 兼容旧协议，解析一条 OTA JSON 并按统一规则处理。
 *
 * JSON 缓冲区不要求以 NUL 结尾；函数会在返回前复制 OTA 任务需要的全部参数，
 * 因而调用者可在返回后立即复用通信接收缓冲区。
 *
 * @param[in] json      JSON 数据首地址，不允许为 NULL，不要求以 NUL 结尾。
 * @param[in] json_len  JSON 有效长度，单位为字节，范围为 1～NATIVE_OTA_JSON_MAX_LEN。
 *
 * @return ESP_OK JSON 有效；可能无需升级，也可能已创建 OTA 任务。
 * @return ESP_ERR_INVALID_ARG JSON 格式、字段、HTTPS URL 或 SHA-256 格式无效。
 * @return ESP_ERR_INVALID_STATE 已有 OTA 任务正在运行。
 * @return ESP_ERR_NO_MEM 无法分配参数副本或创建 OTA 任务。
 *
 * @note 本函数只创建任务，不在调用者所在事件回调中执行固件下载。
 */
esp_err_t native_ota_trigger_json(const char *json, size_t json_len);

#ifdef __cplusplus
}
#endif
