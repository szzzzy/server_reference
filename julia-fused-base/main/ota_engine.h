/**
 * @file    ota_engine.h
 * @brief   固件 OTA 引擎的轻量状态查询接口。
 *
 * 本头文件只暴露“是否已有固件 OTA 下载任务运行”这一查询，供上层协调器
 * （如 audio_service 的 OTA 优先互斥策略）使用。真正的下载任务创建入口仍是
 * native_ota_example.h 中的 native_ota_handle_server_json()。
 *
 * @note 运行状态只保存在 OTA 引擎内部，由短临界区保护；本函数不允许在中断
 *       上下文中调用。
 */
#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 查询是否已有固件 OTA 下载任务正在运行。
 *
 * @return true 已有 OTA 任务运行；false 空闲。
 */
bool ota_engine_is_running(void);

#ifdef __cplusplus
}
#endif
