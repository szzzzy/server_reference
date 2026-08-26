/**
 * @file    network_lifecycle.h
 * @brief   Wi-Fi station lifecycle that never makes application startup depend on AP availability.
 *
 * 网络生命周期只负责 Wi-Fi 连接、IP 就绪通知与服务启动回调的有界退避重试，
 * 不感知任何具体业务（MQTT / WSS / 音频）。业务模块通过
 * network_lifecycle_register_ip_ready() 注册启动回调，取得 IPv4 后由生命周期
 * 任务按注册顺序调用；回调失败会按同一有界退避策略重试，直到成功或 IP 失效。
 */
#pragma once

#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** IP 就绪启动回调：取得 IPv4 后由生命周期任务调用；失败会被有界退避重试。 */
typedef esp_err_t (*network_ip_ready_cb_t)(void *arg);

/** 可注册的 IP 就绪回调数量上限。 */
#define NETWORK_MAX_IP_READY_CALLBACKS 4

/**
 * @brief 注册一个 IP 就绪启动回调。
 *
 * 取得 IPv4 后（包括断线重连后的再次取得），生命周期任务会按注册顺序调用所有
 * 回调。回调返回 ESP_OK 视为启动完成，不再重试；返回其他错误码则按有界指数
 * 退避（带负向抖动）重试，直至成功或 IP 失效。每次新的 GOT_IP 都会重置重试
 * 计数并重新触发全部回调。
 *
 * @param[in] callback 启动回调，不允许为 NULL。
 * @param[in] arg      原样传给回调的上下文参数，可为 NULL。
 *
 * @return ESP_OK 注册成功。
 * @return ESP_ERR_INVALID_ARG callback 为 NULL。
 * @return ESP_ERR_NO_MEM 回调表已满。
 * @return ESP_ERR_INVALID_STATE 网络生命周期已经启动（必须在启动前注册）。
 *
 * @note 不允许在中断上下文中调用。
 */
esp_err_t network_lifecycle_register_ip_ready(network_ip_ready_cb_t callback, void *arg);

/**
 * @brief 启动 Wi-Fi station 生命周期。
 *
 * 初始化 netif、注册事件并启动 Wi-Fi；后台任务永久重连，应用启动不依赖 AP
 * 可用性。取得 IPv4 后按注册表调用 IP 就绪回调。
 *
 * @return ESP_OK 生命周期已启动（重复调用幂等返回）。
 * @return ESP_ERR_NOT_SUPPORTED 构建未启用 Wi-Fi 连接示例。
 * @return 其他 esp_err_t Wi-Fi 初始化失败（部分资源已清理）。
 *
 * @note 不允许在中断上下文中调用。
 */
esp_err_t network_lifecycle_start(void);

#ifdef __cplusplus
}
#endif
