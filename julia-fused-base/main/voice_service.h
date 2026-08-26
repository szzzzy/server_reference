/**
 * @file    voice_service.h
 * @brief   语音业务服务：FILE_SEND/MIC 命令语法、WSS 文件推送协议与入口装配。
 *
 * 本模块是语音命令语法的唯一解析点，两条入口共用同一套处理：
 * - MQTT 语音命令 topic（FILE_SEND <uri> / MIC_START / MIC_STOP）：由
 *   voice_service_init() 注册到通信层 topic 注册表；
 * - WSS 服务端文本帧（FILE_SEND <uri>）：作为 wss_transport 的 on_text 回调执行。
 *
 * 文件推送协议（BEGIN FILE <size> <name> -> 1200 B 二进制帧 -> END <bytes>）
 * 在本模块内实现，传输交给纯传输层 wss_transport；URI 到本地路径的受控映射
 * 由 voice_uri 提供。本模块不采集音频、不做编解码。
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 语音命令（含 FILE_SEND 的 URI 部分）的最大长度，含末尾 NUL。 */
#define VOICE_SERVICE_CMD_MAX_LEN 128

/** FILE_SEND / 命令 URI 的最大长度（含末尾 NUL）。 */
#define VOICE_SERVICE_URI_MAX_LEN 128

/**
 * @brief 板级音频接入：把 board_audio 的 PCM1 帧挂到 WSS sink，
 * 并接管 WSS 下行 SPKS/SPKV/SPKE/SPKT/MICS/MICW 命令 → board_audio_* API。
 *
 * @note 须在 board_audio_init() 之后、voice_service_ip_ready() 之前调用。
 * @note WSS 下行的二进制 PCM 帧（voice_service_on_binary）由此路由到扬声器。
 */
esp_err_t voice_service_init_board_audio(void);

/**
 * @brief 初始化语音服务并注册 MQTT 语音命令 topic。
 *
 * 把 CONFIG_COMM_MQTT_VOICE_CMD_TOPIC 注册到通信层注册表（非 critical：
 * 语音订阅不影响 OTA 就绪判定）。WSS 传输客户端不在此启动，而是在取得 IPv4
 * 后由 voice_service_ip_ready() 启动。
 *
 * @return ESP_OK 初始化成功。
 * @return 其他 esp_err_t 通信层 topic 注册失败。
 *
 * @note 必须在 mqtt_comm_start() 生效前（即网络启动前）调用；幂等。
 */
esp_err_t voice_service_init(void);

/**
 * @brief 网络 IP 就绪回调适配器：启动 WSS 传输客户端。
 *
 * 供 network_lifecycle 的 ip_ready 回调注册使用；失败会由网络生命周期任务
 * 按有界退避重试。
 *
 * @param[in] arg 回调参数，本实现未使用。
 * @return wss_transport_start() 的返回值。
 */
esp_err_t voice_service_ip_ready(void *arg);

/**
 * @brief 请求通过 WSS 推送一个音频文件（对应 FILE_SEND 语音命令）。
 *
 * @param[in] uri 文件 URI："SD:/x/y" 映射到 /sdcard/x/y，"SPIFFS:/x/y" 映射到
 *                /spiffs/x/y；非法格式以 ERROR bad_uri 拒绝。
 *
 * @return ESP_OK 命令已入队，由会话任务在连接就绪后执行。
 * @return ESP_ERR_INVALID_ARG uri 为空。
 * @return ESP_ERR_INVALID_SIZE uri 超出上限。
 * @return ESP_ERR_NO_MEM 命令队列已满。
 * @return ESP_ERR_INVALID_STATE WSS 客户端尚未启动。
 *
 * @note 可被任意普通任务（如 MQTT 事件任务）调用；只入队不阻塞。
 */
esp_err_t voice_service_send_file(const char *uri);

/**
 * @brief 发送一个 MIC 音频块（流式预留接口）。
 *
 * 每个块封装为一个二进制 WebSocket 帧；仅在 WSS 会话已建立且
 * voice_service_mic_start() 已生效时真正发送，否则该块被丢弃并记录日志。
 *
 * @param[in] buf 音频数据首地址，不允许为 NULL。
 * @param[in] len 数据长度，1～1200 字节。
 *
 * @return ESP_OK 数据块已入队。
 * @return ESP_ERR_INVALID_ARG buf 为空或 len 为 0。
 * @return ESP_ERR_INVALID_SIZE len 超过单帧载荷上限。
 * @return ESP_ERR_NO_MEM 命令队列已满。
 * @return ESP_ERR_INVALID_STATE WSS 客户端尚未启动。
 */
esp_err_t voice_service_send_chunk(const uint8_t *buf, size_t len);

/**
 * @brief 开启 MIC 流式发送状态（对应 MIC_START 语音命令）。
 *
 * @return ESP_OK 命令已入队；ESP_ERR_NO_MEM 命令队列已满；ESP_ERR_INVALID_STATE 尚未启动。
 */
esp_err_t voice_service_mic_start(void);

/**
 * @brief 关闭 MIC 流式发送状态（对应 MIC_STOP 语音命令）。
 *
 * @return ESP_OK 命令已入队；ESP_ERR_NO_MEM 命令队列已满；ESP_ERR_INVALID_STATE 尚未启动。
 */
esp_err_t voice_service_mic_stop(void);

#ifdef __cplusplus
}
#endif
