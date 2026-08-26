/**
 * @file    mqtt_comm.h
 * @brief   MQTT 通信层的公共接口：topic→handler 注册、通用发布与启动。
 *
 * 通信层不感知任何业务（OTA / 语音 / 音频状态）。业务模块在启动前通过
 * mqtt_comm_register_topic() 注册自己的下行 topic 与处理回调，连接建立后通信层
 * 统一订阅并完成分片重组，再把完整载荷交给对应 handler。
 *
 * 模块职责边界：
 * - 负责 MQTT 连接、已注册 topic 订阅、主动 OTA 检查任务和状态异步发送；
 * - 将完整载荷按注册表路由给业务 handler；
 * - 不执行 HTTPS 下载、OTA 分区写入或业务 NVS 清理。
 *
 * @note 所有 topic 必须在 mqtt_comm_start() 生效前注册（app_main 装配阶段）；
 *       启动后注册的新 topic 不会随已建立的会话订阅。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 已注册 topic 的完整载荷处理回调。
 *
 * 载荷以 NUL 结尾（通信层在重组完成后追加），长度不含末尾 NUL。回调在
 * ESP-MQTT 事件任务上下文中同步执行：只应解析元数据、入队或创建任务，
 * 不得阻塞等待下载完成，也不得访问 Flash。
 *
 * @param[in] payload     完整消息载荷首地址，不要求调用方释放。
 * @param[in] payload_len 载荷有效长度，单位为字节。
 */
typedef void (*mqtt_inbound_handler_t)(const char *payload, size_t payload_len);

/**
 * @brief 注册一个下行 MQTT topic 及其完整载荷处理回调。
 *
 * @param[in] topic          要订阅的完整 topic，不允许为 NULL；字符串会被复制。
 * @param[in] max_payload_len 该 topic 单条消息允许的最大长度（不含末尾 NUL），
 *                            不得超过 NATIVE_OTA_JSON_MAX_LEN。
 * @param[in] critical       为 true 时，该 topic 的 SUBACK 计入连接就绪判定；
 *                            任一 critical topic 订阅失败或超时会触发重建连接。
 * @param[in] handler        完整载荷处理回调，不允许为 NULL。
 *
 * @return ESP_OK 注册成功（同名 topic 重复注册时覆盖旧配置）。
 * @return ESP_ERR_INVALID_ARG 参数无效或 max_payload_len 超出重组缓冲区容量。
 * @return ESP_ERR_NO_MEM 注册表已满。
 *
 * @note 必须在 mqtt_comm_start() 之前调用；不允许在中断上下文中调用。
 */
esp_err_t mqtt_comm_register_topic(const char *topic, size_t max_payload_len,
                                   bool critical, mqtt_inbound_handler_t handler);

/**
 * @brief 以 QoS 1、非 retain 方式发布一条 MQTT 消息。
 *
 * 通用尽力而为发布：消息直接交给 ESP-MQTT 发送队列，不跟踪 PUBACK。用于
 * 音频状态等辅助上报通道；OTA 关键生命周期事件请继续使用 ota_report 模块
 * 的可靠 transport。
 *
 * @param[in] topic     完整发布 topic，不允许为 NULL。
 * @param[in] data      消息内容首地址，不允许为 NULL。
 * @param[in] data_len  消息长度，单位为字节，大于 0 且不超过 INT_MAX。
 *
 * @return ESP_OK 消息已交给 ESP-MQTT 发送队列。
 * @return ESP_ERR_INVALID_ARG 参数无效。
 * @return ESP_ERR_INVALID_STATE MQTT 客户端尚未创建。
 * @return ESP_FAIL ESP-MQTT 发布接口拒绝请求。
 *
 * @note 可在任意普通任务上下文中调用；不允许在中断上下文中调用。
 */
esp_err_t mqtt_comm_publish(const char *topic, const char *data, size_t data_len);

/**
 * @brief 初始化并启动 MQTT 客户端。
 *
 * 客户端启动后由 ESP-MQTT 自有任务维持连接，并由模块内部任务在连接就绪后立即
 * 上报当前版本、随后周期检查。每次连接成功都会订阅所有已注册 topic，异常断线
 * 后使用官方客户端的自动重连机制恢复连接与版本检查。
 *
 * @return ESP_OK 客户端成功启动。
 * @return ESP_FAIL ESP-MQTT 客户端初始化失败。
 * @return 其他 esp_err_t 事件注册或客户端启动失败的错误码。
 *
 * @note 必须在 NVS、默认事件循环初始化完成且已获得 IP 后调用。重复调用是幂等的：
 *       已启动的客户端、任务和事件组会被复用，不会重复创建。
 * @note 本函数不允许在中断上下文中调用。
 */
esp_err_t mqtt_comm_start(void);

/**
 * @brief 网络 IP 就绪回调适配器：转调 mqtt_comm_start()。
 *
 * 供 network_lifecycle 的 ip_ready 回调注册使用；失败会由网络生命周期任务
 * 按有界退避重试。
 *
 * @param[in] arg 回调参数，本实现未使用。
 * @return mqtt_comm_start() 的返回值。
 */
esp_err_t mqtt_comm_ip_ready(void *arg);

#ifdef __cplusplus
}
#endif
