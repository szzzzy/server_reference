/**
 * @file    audio_engine.h
 * @brief   音频素材下载引擎公共接口。
 *
 * 本模块拥有唯一的音频下载任务及全部下载资源：HTTPS 传输（复用公共下载器
 * http_downloader）、数据分区写入句柄、流式 SHA-256 上下文与 NVS 断点记录。
 * 下载完成并通过摘要校验后，通过弱钩子 native_audio_on_ready() 通知上层
 * （如 JULIA 播放栈）播放，并上报 audio_status 生命周期事件。
 *
 * 模块边界：
 * - 不依赖 MQTT；状态上报经通信层通用发布接口 mqtt_comm_publish() 发送到
 *   音频状态 topic；
 * - 不解析控制面 JSON；清单由 audio_service 校验后传入；
 * - 运行中判断以本模块为准，调用方不得另存副本。
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
 * @brief 以已校验音频清单启动唯一的音频下载任务。
 *
 * 函数在堆上深拷贝清单并创建下载任务，调用者可在返回后立即复用输入缓冲区。
 * 已有任务运行或固件 OTA 任务运行时拒绝创建新任务。
 *
 * @param[in] manifest 已通过音频控制面校验的清单，不允许为 NULL。
 *
 * @return ESP_OK 任务创建成功。
 * @return ESP_ERR_INVALID_ARG manifest 为 NULL。
 * @return ESP_ERR_INVALID_STATE 已有音频或 OTA 下载任务正在运行。
 * @return ESP_ERR_NO_MEM 清单副本分配或任务创建失败。
 *
 * @note 只创建任务，不在调用者上下文中执行下载；不允许在中断上下文调用。
 */
esp_err_t audio_engine_start(const native_audio_manifest_t *manifest);

/**
 * @brief 查询是否已有音频下载任务正在运行。
 *
 * @return true 已有音频任务运行；false 空闲。
 *
 * @note 运行状态只保存在本模块内部，由短临界区保护。
 */
bool audio_engine_is_running(void);

/**
 * @brief 读取设备当前已安装的音频素材版本。
 *
 * @param[out] version      接收 NUL 结尾版本字符串的缓冲区。
 * @param[in]  version_size 缓冲区容量，必须至少为 NATIVE_OTA_AUDIO_VERSION_SIZE。
 *
 * @return ESP_OK 版本读取成功；未安装过素材时输出 NATIVE_OTA_AUDIO_VERSION_UNKNOWN。
 * @return ESP_ERR_INVALID_ARG 参数无效。
 *
 * @note 实现读 NVS audio_resume/current_version；NVS 不可用时按未安装处理。
 */
esp_err_t audio_engine_get_current_version(char *version, size_t version_size);

/**
 * @brief 音频素材下载完成且摘要校验通过后的播放通知弱钩子。
 *
 * 默认实现只记录日志；产品板级代码（如 JULIA 播放栈）可提供同名强符号覆盖，
 * 从音频数据分区读取 stored_size 字节并播放。
 *
 * @param[in] manifest    本次已下载清单，只读，回调返回后失效。
 * @param[in] stored_size 音频分区中已写入的有效字节数，等于清单 file_size。
 *
 * @return ESP_OK 播放接管成功；其他值仅记录日志，不改变下载结果。
 */
esp_err_t native_audio_on_ready(const native_audio_manifest_t *manifest, size_t stored_size);

#ifdef __cplusplus
}
#endif
