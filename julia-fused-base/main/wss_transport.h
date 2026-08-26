/**
 * @file    wss_transport.h
 * @brief   WSS（WebSocket over TLS）纯传输层客户端接口。
 *
 * 本模块只负责传输：esp-tls 连接、RFC 6455 帧编解码、HTTP 升级握手严格校验、
 * 会话保活（客户端 PING/PONG + TCP keepalive）与断线自愈重连。业务协议
 * （FILE_SEND / MIC 命令、BEGIN FILE/END 推送）不属于本模块，由上层通过回调
 * 注入：
 * - on_text：服务端一条完整的文本消息（会话任务上下文，同步执行）；
 * - on_queue_item：命令队列中的一条不透明数据（会话任务上下文，同步执行）。
 *
 * 线程模型：
 * - 所有 socket/TLS 读写收敛到唯一会话任务；
 * - 外部调用者（MQTT 事件任务等）只通过 wss_transport_enqueue() 入队；
 * - 两个回调都在会话任务上下文中同步执行，可以在回调内直接调用
 *   wss_transport_send_now() 发送帧。
 *
 * 证书复用构建内嵌的 server_certs/ca_cert.pem 信任锚；本模块不访问 OTA 分区、
 * 不写 NVS、不打开文件。
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 单帧载荷上限：也是命令队列条目数据区的通用容量，与云端基准实现保持一致。 */
#define WSS_TRANSPORT_MAX_PAYLOAD 1200

/**
 * @brief 服务端完整文本消息回调。
 *
 * @param[in] data 文本载荷，不保证以 NUL 结尾。
 * @param[in] len  文本长度，单位为字节。
 *
 * @note 在会话任务上下文中同步执行；回调返回后缓冲区失效。
 */
/** 服务端文本消息回调。 */
typedef void (*wss_transport_text_cb_t)(const uint8_t *data, size_t len);

/**
 * @brief 服务端二进制消息回调（下行 PCM 等）。
 *
 * 与 on_text 同上下文（会话任务）同步执行；回调返回后缓冲区失效。
 * 分片（FIN=0）会先重组为完整消息再回调。
 */
typedef void (*wss_transport_binary_cb_t)(const uint8_t *data, size_t len);

/**
 * @brief 命令队列条目回调。
 *
 * @param[in] item      队列条目内容，由上层定义，仅在回调返回前有效。
 * @param[in] item_size 条目大小，等于启动时配置的 queue_item_size。
 *
 * @note 在会话任务上下文中同步执行；回调内可直接调用 wss_transport_send_now()。
 */
typedef void (*wss_transport_queue_item_cb_t)(void *item, size_t item_size);

/**
 * @brief 会话结束回调：链路关闭、故障或保活超时后、重连等待之前调用。
 *
 * @note 在会话任务上下文中同步执行；供上层复位会话级业务状态（如 MIC 流）。
 */
typedef void (*wss_transport_session_end_cb_t)(void);

/**
 * @brief 传输层启动配置。
 */
typedef struct {
    wss_transport_text_cb_t on_text; /**< 服务端文本消息回调，可为 NULL。 */
    wss_transport_binary_cb_t on_binary; /**< 服务端二进制消息回调，可为 NULL。 */
    wss_transport_queue_item_cb_t on_queue_item; /**< 命令队列条目回调，不允许为 NULL。 */
    wss_transport_session_end_cb_t on_session_end; /**< 会话结束回调，可为 NULL。 */
    size_t queue_item_size; /**< 单条队列条目的大小，必须大于 0。 */
    unsigned queue_depth; /**< 命令队列深度，必须大于 0。 */
} wss_transport_config_t;

/**
 * @brief 启动 WSS 传输客户端。
 *
 * 创建唯一会话任务：连接失败或会话结束后按 CONFIG_WSS_RECONNECT_INTERVAL_SECONDS
 * 周期重连，链路中断无需外部干预即可自愈。
 *
 * @param[in] config 启动配置，不允许为 NULL；on_queue_item 必须有效。
 *
 * @return ESP_OK 客户端已启动。
 * @return ESP_ERR_INVALID_ARG 配置无效。
 * @return ESP_ERR_NO_MEM 队列条目缓冲、命令队列或会话任务创建失败。
 * @return ESP_ERR_INVALID_STATE 已有一次启动正在进行。
 *
 * @note 幂等：重复调用直接复用已创建的会话任务，不会重复分配。
 * @note 必须取得 IPv4 后调用；本函数不允许在中断上下文中调用。
 */
esp_err_t wss_transport_start(const wss_transport_config_t *config);

/**
 * @brief 把一条不透明命令放入有界队列。
 *
 * @param[in] item      条目内容首地址，不允许为 NULL；内容会被复制。
 * @param[in] item_size 条目大小，必须等于启动时配置的 queue_item_size。
 *
 * @return ESP_OK 已入队。
 * @return ESP_ERR_INVALID_ARG item 为 NULL。
 * @return ESP_ERR_INVALID_SIZE item_size 与队列条目大小不匹配。
 * @return ESP_ERR_NO_MEM 命令队列已满。
 * @return ESP_ERR_INVALID_STATE 客户端尚未启动。
 *
 * @note 可被任意普通任务（如 MQTT 事件任务）调用；只入队不阻塞。
 */
esp_err_t wss_transport_enqueue(const void *item, size_t item_size);

/**
 * @brief 在会话任务上下文中直接发送一帧 WebSocket 消息。
 *
 * 发送前按 RFC 6455 校验：opcode 只允许已定义值（0x0 续帧、0x1 文本、0x2 二进制、
 * 0x8 CLOSE、0x9 PING、0xA PONG）；控制帧载荷不得超过 125 字节；载荷非空时
 * payload 不得为 NULL；载荷不得超过 WSS_TRANSPORT_MAX_PAYLOAD。
 *
 * @param[in] opcode  帧操作码（0x1 文本、0x2 二进制、0x9 PING、0xA PONG 等）。
 * @param[in] payload 载荷首地址，len 为 0 时可为 NULL。
 * @param[in] len     载荷长度，不允许超过 WSS_TRANSPORT_MAX_PAYLOAD；
 *                    控制帧不允许超过 125。
 *
 * @return ESP_OK 发送完成。
 * @return ESP_ERR_INVALID_ARG 保留操作码、控制帧超长、载荷超长或载荷非空但
 *         payload 为 NULL；不标记会话故障。
 * @return ESP_FAIL 会话无效或写出失败；写失败同时标记会话故障，
 *         会话循环将关闭链路并重连。
 *
 * @note 只能在 on_text / on_queue_item 回调（会话任务上下文）中调用；
 *       多任务并发调用不是线程安全的。
 */
esp_err_t wss_transport_send_now(uint8_t opcode, const uint8_t *payload, size_t len);

#ifdef __cplusplus
}
#endif
