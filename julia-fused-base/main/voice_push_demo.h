/**
 * @file    voice_push_demo.h
 * @brief   设备主动推送演示：显式传输项目测试音频到 WSS 服务器。
 *
 * 与 OTA 检查（设备主动 publish ota_check）不同，WSS 语音通道的协议默认是
 * 服务端驱动（FILE_SEND/MIC_START）。本模块演示设备主动写入：WSS 客户端
 * 启动后，把 voice_push_demo.c 内固定的测试音频列表（测试音频/ 目录）按
 * "SD:/<文件名>" 逐一入队，由 voice_service 的 WSS 会话在连接就绪后推给
 * 服务器，全程不需要任何服务端命令。
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 启动设备主动推送（显式传输）演示任务。
 *
 * 行为由配置项决定：
 * - CONFIG_VOICE_PUSH_DEMO_INTERVAL_SECONDS == 0：WSS 客户端启动后把文件列表
 *   显式传输一次；
 * - > 0：每 N 秒重复传输整个列表。
 *
 * @return ESP_OK 演示任务已创建。
 * @return ESP_ERR_NOT_SUPPORTED 演示未启用（CONFIG_VOICE_PUSH_DEMO_ENABLE=n）。
 * @return ESP_ERR_NO_MEM 演示任务创建失败。
 *
 * @note 幂等：重复调用不会创建第二个任务。
 * @note 本函数不阻塞；演示任务在后台执行，不允许在中断上下文调用。
 */
esp_err_t voice_push_demo_start(void);

#ifdef __cplusplus
}
#endif
