/**
 * @file    wss_transport.c
 * @brief   基于 esp-tls 的 WSS（WebSocket over TLS）纯传输层实现。
 *
 * 设计要点：
 * - 使用 IDF v5.x esp-tls API：esp_tls_init() + esp_tls_conn_new_sync()（返回 1/-1，
 *   句柄由调用者持有），不使用旧版返回指针的 esp_tls_conn_new()；
 * - WebSocket 帧按 RFC 6455 手写：客户端帧强制掩码（4 字节随机 key，载荷逐字节
 *   XOR），服务端帧不掩码，长度支持 126/127 扩展，服务端分片消息跨帧重组；
 *   服务端帧违反协议（RSV 位、掩码、控制帧超长、64 位长度最高位、非最短长度
 *   编码、保留操作码）时以 1002 协议错误关闭；文本消息非法 UTF-8 时以 1007
 *   关闭；收到 CLOSE 会先校验载荷（1 字节载荷、非法状态码、非法 UTF-8 reason
 *   均按 1002 处理），合法时回送 CLOSE（RFC 6455 5.5.1）；
 * - 所有 socket 读写收敛到唯一会话任务；外部调用者只把不透明条目放入有界队列，
 *   避免多任务并发访问 mbedTLS 会话；
 * - 升级握手严格校验：状态行 101、Upgrade/Connection 头，并以请求 nonce 计算和
 *   比对 Sec-WebSocket-Accept；HTTP 头之后同一次 TLS 读取带回的首个 WebSocket 帧
 *   字节会被保留并优先消费，绝不丢弃；
 * - HTTP 升级完成后把空闲读取超时收紧到 20 ms（SO_RCVTIMEO）、写超时收紧到
 *   WSS_WRITE_TIMEOUT_MS（SO_SNDTIMEO）并启用 TCP keepalive，会话循环在超时
 *   窗口内处理排队条目；空闲本身不是故障：客户端按
 *   CONFIG_WSS_KEEPALIVE_INTERVAL_SECONDS 周期发送 PING；发出 PING 后只要在
 *   CONFIG_WSS_PONG_TIMEOUT_SECONDS 内收到任意下行帧（PONG 或其他帧都能证明
 *   链路存活）就取消待定探测并重置保活计时，只有该窗口内完全没有下行帧才判定
 *   链路死亡并重连自愈；
 *   写方向超时保证服务端停止读取时不会把会话任务永久卡死；
 * - Bearer token 优先取 COMM_DEVICE_AUTH_TOKEN_VALUE，为空时回退 CONFIG_WSS_TOKEN；
 * - 服务器证书仍由 server_certs/ca_cert.pem 内嵌信任锚校验，不引入新证书。
 *
 * 业务协议（文件推送、MIC 流等）由上层 voice_service 通过回调注入，本文件不含
 * 任何业务语义。
 */

#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "lwip/sockets.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_tls.h"
#include "mbedtls/base64.h"
#include "mbedtls/md.h"
#include "psa/crypto.h"

#include "wss_transport.h"

/** 本模块统一使用的日志标签。 */
static const char *TAG = "wss_transport";

/** WebSocket 帧头最长字节数（2 基础 + 8 扩展长度 + 4 掩码 key）。 */
#define WSS_FRAME_HDR_SIZE 16
/** 握手请求/响应缓冲大小。 */
#define WSS_REQ_BUF_SIZE 512
#define WSS_HANDSHAKE_BUF_SIZE 1024
/** 连接与 TLS 握手的总超时；连接成功后会单独收紧空闲读取超时。 */
#define WSS_CONNECT_TIMEOUT_MS 8000
/** HTTP 升级响应的读取超时。握手不能使用会话级的短超时。 */
#define WSS_HANDSHAKE_READ_TIMEOUT_MS 1000
/**
 * 会话空闲读取超时：同时是排队命令的最长处理延迟。
 *
 * 板级 MIC 每 20 ms 产生一帧 PCM1；命令队列深度只有 4。若在一次空闲
 * recv 上阻塞 1 s，队列会在 80 ms 内写满并丢掉几乎全部上行音频。因此
 * 会话超时必须不大于一帧周期。
 */
#define WSS_READ_TIMEOUT_MS 20
/** 会话写方向超时（SO_SNDTIMEO）：对端停止读取时 send 最多阻塞这么久，
 *  随后 mbedTLS 返回 WANT_WRITE、写路径按链路故障结束会话，避免会话任务
 *  在推送大文件时被永久卡死。 */
#define WSS_WRITE_TIMEOUT_MS 5000
/** 帧内读取允许的连续空闲超时次数，超过即判定链路故障，防止中途死亡挂死。 */
#define WSS_READ_EAGAIN_BUDGET 5
/** HTTP 升级响应读取允许的连续空闲超时次数。 */
#define WSS_HANDSHAKE_EAGAIN_BUDGET 10

/**
 * @brief 内嵌服务器根证书符号，由 main/CMakeLists.txt 的 EMBED_TXTFILES 生成。
 *
 * 证书只读，不由本文件释放或修改。
 */
extern const unsigned char ca_cert_pem_start[] asm("_binary_ca_cert_pem_start");
extern const unsigned char ca_cert_pem_end[] asm("_binary_ca_cert_pem_end");

/** Protects start idempotency; 重复启动绝不分配第二份任务或队列。 */
static portMUX_TYPE s_start_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_started;
static bool s_starting;
/** 唯一会话任务：串行执行连接、帧收发与命令队列。 */
static TaskHandle_t s_session_task;
/** 外部调用者 -> 会话任务的有界命令队列；条目为不透明数据。 */
static QueueHandle_t s_cmd_queue;
/** 当前 TLS 会话句柄，仅由会话任务访问。 */
static esp_tls_t *s_tls;
/** TLS 连接配置；cacert 指针在 wss_transport_start() 中一次性填充。 */
static esp_tls_cfg_t s_tls_cfg;
/** 启动配置副本：回调与队列尺寸。 */
static wss_transport_config_t s_config;
/** 队列条目接收缓冲，容量等于 queue_item_size，由启动时分配。 */
static void *s_queue_item;
/** 会话级"链路已坏"标志：任一路径写失败后置位，会话循环据此退出并重连。 */
static bool s_session_failed;
/**
 * 握手阶段保留下来的"HTTP 头结束之后的余量"：一次 TLS 读取可能同时带回 HTTP
 * 响应和首个 WebSocket 帧，这些字节必须被帧解析优先消费，否则解析错位。
 * 仅由会话任务读写。
 */
static uint8_t s_rx_extra[WSS_HANDSHAKE_BUF_SIZE];
static size_t s_rx_extra_len;
static size_t s_rx_extra_pos;
/** 服务端分片消息重组状态（RFC 6455 允许数据消息跨帧分片），仅由会话任务读写。 */
static uint8_t s_msg_payload[WSS_TRANSPORT_MAX_PAYLOAD + 1];
static size_t s_msg_len;
static uint8_t s_msg_opcode;
static bool s_msg_active;

/** 设置 socket 读取超时。HTTP 升级和实时会话使用不同的时间预算。 */
static void wss_set_receive_timeout(int sockfd, unsigned timeout_ms)
{
    struct timeval timeout = {
        .tv_sec = (time_t)(timeout_ms / 1000U),
        .tv_usec = (suseconds_t)((timeout_ms % 1000U) * 1000U),
    };
    if (setsockopt(sockfd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0) {
        ESP_LOGW(TAG, "setsockopt(SO_RCVTIMEO=%u ms) failed", timeout_ms);
    }
}

/* ------------------------------------------------------------------ */
/* 基础 TLS 字节流读写                                                */
/* ------------------------------------------------------------------ */

/**
 * @brief 循环写出完整数据块。
 *
 * TLS 是字节流，单次 write 可能只完成一部分；本函数保证要么全部写出，
 * 要么返回失败由调用者结束会话。写方向受 SO_SNDTIMEO 约束：服务端停止
 * 读取时 send 超时会让 esp_tls_conn_write 返回 WANT_WRITE（负值）或部分
 * 长度，本函数把负值按链路故障返回，保证会话任务不会被永久阻塞。
 *
 * @param[in] data 数据首地址，不允许为 NULL。
 * @param[in] len  数据长度。
 * @return ESP_OK 全部写出；ESP_FAIL 会话无效或写出失败。
 */
static esp_err_t wss_tls_write_all(const void *data, size_t len)
{
    size_t sent = 0;
    while (sent < len) {
        if (s_tls == NULL) {
            return ESP_FAIL;
        }
        int n = esp_tls_conn_write(s_tls, (const char *)data + sent, len - sent);
        if (n <= 0) {
            return ESP_FAIL;
        }
        sent += (size_t)n;
    }
    return ESP_OK;
}

/**
 * @brief 循环读入完整数据块。
 *
 * 空闲读取超时（EAGAIN）只容忍有限次数：帧中途超时说明对端长时间未补齐数据，
 * 超过预算后按链路故障处理，避免会话任务永久挂起。
 *
 * @param[out] data 接收缓冲区，不允许为 NULL。
 * @param[in]  len  读取长度。
 * @return ESP_OK 已读满；ESP_FAIL 会话无效、EOF 或读取失败。
 */
static esp_err_t wss_tls_read_exact(void *data, size_t len)
{
    size_t got = 0;
    unsigned eagain_budget = WSS_READ_EAGAIN_BUDGET;
    while (got < len) {
        /* 优先消费握手阶段保留下来的首帧余量，再回到 TLS 字节流。 */
        if (s_rx_extra_pos < s_rx_extra_len) {
            size_t avail = s_rx_extra_len - s_rx_extra_pos;
            size_t take = len - got;
            if (take > avail) {
                take = avail;
            }
            memcpy((char *)data + got, s_rx_extra + s_rx_extra_pos, take);
            s_rx_extra_pos += take;
            got += take;
            if (s_rx_extra_pos >= s_rx_extra_len) {
                s_rx_extra_pos = 0;
                s_rx_extra_len = 0;
            }
            continue;
        }
        if (s_tls == NULL) {
            return ESP_FAIL;
        }
        int n = esp_tls_conn_read(s_tls, (char *)data + got, len - got);
        if (n == 0) {
            return ESP_FAIL;
        }
        if (n < 0) {
            if ((errno == EAGAIN || errno == EWOULDBLOCK) && eagain_budget > 0) {
                eagain_budget--;
                continue;
            }
            return ESP_FAIL;
        }
        got += (size_t)n;
        eagain_budget = WSS_READ_EAGAIN_BUDGET;
    }
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/* WebSocket 客户端帧（RFC 6455）                                      */
/* ------------------------------------------------------------------ */

/**
 * @brief 发送一帧 WebSocket 消息。
 *
 * 客户端帧必须掩码：随机 4 字节 key，载荷逐字节 XOR；长度按 126/127 扩展编码。
 * 本函数是发送路径的公共入口（保活 PING、PING 应答、CLOSE 应答与公开的
 * wss_transport_send_now 都经过这里），因此在此统一校验帧合法性：
 * opcode 只能是已定义值；控制帧载荷不得超过 125 字节；载荷非空时必须提供
 * 有效缓冲。
 *
 * @param[in] opcode  帧操作码（0x0 续帧、0x1 文本、0x2 二进制、0x8 CLOSE、
 *                    0x9 PING、0xA PONG；保留值拒绝）。
 * @param[in] payload 载荷首地址，len 为 0 时可为 NULL。
 * @param[in] len     载荷长度，不允许超过 WSS_TRANSPORT_MAX_PAYLOAD；
 *                    控制帧不允许超过 125。
 * @return ESP_OK 发送完成。
 * @return ESP_ERR_INVALID_ARG 保留操作码、控制帧超长、载荷超长或载荷非空但
 *         payload 为 NULL。
 * @return ESP_FAIL 会话无效或写出失败。
 */
static esp_err_t wss_ws_send(uint8_t opcode, const uint8_t *payload, size_t len)
{
    if (s_tls == NULL) {
        return ESP_FAIL;
    }
    switch (opcode) {                   /* RFC 6455 5.2：只允许已定义的操作码 */
    case 0x0:
    case 0x1:
    case 0x2:
    case 0x8:
    case 0x9:
    case 0xA:
        break;
    default:
        ESP_LOGW(TAG, "Refusing to send WS frame with reserved opcode 0x%x", opcode);
        return ESP_ERR_INVALID_ARG;
    }
    if ((opcode & 0x08) != 0 && len > 125) {    /* RFC 6455 5.5：控制帧 ≤125 字节 */
        ESP_LOGW(TAG, "Refusing to send oversized WS control frame (%u bytes)",
                 (unsigned)len);
        return ESP_ERR_INVALID_ARG;
    }
    if (len > WSS_TRANSPORT_MAX_PAYLOAD) {
        ESP_LOGW(TAG, "Refusing to send oversized WS frame (%u bytes)", (unsigned)len);
        return ESP_ERR_INVALID_ARG;
    }
    if (len > 0 && payload == NULL) {
        ESP_LOGW(TAG, "Refusing to send WS frame with NULL payload");
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t hdr[WSS_FRAME_HDR_SIZE];
    size_t h = 0;
    hdr[h++] = 0x80 | opcode;               /* FIN + opcode */

    uint8_t mask_key[4];
    esp_fill_random(mask_key, sizeof(mask_key));

    if (len < 126) {
        hdr[h++] = 0x80 | (uint8_t)len;
    } else if (len <= 0xFFFF) {
        hdr[h++] = 0x80 | 126;
        hdr[h++] = (uint8_t)(len >> 8);
        hdr[h++] = (uint8_t)(len & 0xFF);
    } else {
        hdr[h++] = 0x80 | 127;
        uint64_t l = len;
        for (int i = 7; i >= 0; i--) {
            hdr[h++] = (uint8_t)(l >> (i * 8));
        }
    }
    memcpy(hdr + h, mask_key, sizeof(mask_key)); /* 客户端帧必须掩码 */
    h += sizeof(mask_key);

    if (wss_tls_write_all(hdr, h) != ESP_OK) {
        return ESP_FAIL;
    }
    if (len > 0) {
        static uint8_t masked[WSS_TRANSPORT_MAX_PAYLOAD];
        for (size_t i = 0; i < len; i++) {
            masked[i] = payload[i] ^ mask_key[i % 4];
        }
        if (wss_tls_write_all(masked, len) != ESP_OK) {
            return ESP_FAIL;
        }
    }
    return ESP_OK;
}

/**
 * @brief 发送 CLOSE 帧（RFC 6455 5.5.1）。
 *
 * 客户端帧自动掩码；code 为 0 时发送空载荷 CLOSE（未携带状态码），否则携带
 * 2 字节网络序状态码。
 *
 * @param[in] code 状态码（主机字节序），0 表示不带状态码。
 * @return ESP_OK 发送完成；ESP_FAIL 会话无效或写出失败。
 */
static esp_err_t wss_send_close(uint16_t code)
{
    uint8_t payload[2];
    size_t len = 0;
    if (code != 0) {
        payload[0] = (uint8_t)(code >> 8);
        payload[1] = (uint8_t)(code & 0xFF);
        len = sizeof(payload);
    }
    return wss_ws_send(0x8, payload, len);
}

/**
 * @brief 接收一帧 WebSocket 消息。
 *
 * 服务端帧不得掩码；RFC 6455 分片帧（FIN=0）不再在此处拒绝，由会话循环跨帧
 * 重组。只把帧首字节的空闲超时报告给调用者（idle_out），让会话循环把它当作
 * 队列处理与保活窗口；帧中途失败一律按链路故障返回。握手阶段保留的余量会
 * 被优先消费，因此不会产生空闲超时。
 *
 * @param[out] opcode_out 帧操作码。
 * @param[out] payload    载荷缓冲区，容量 cap 字节。
 * @param[in]  cap        载荷缓冲区容量。
 * @param[out] len_out    载荷长度。
 * @param[out] idle_out   是否只是等待帧首字节的空闲超时。
 * @param[out] fin_out    该帧是否携带 FIN（即是否为消息的最后一帧）。
 * @return ESP_OK 收到一帧（idle_out=false）或空闲超时（idle_out=true）；
 * @return ESP_FAIL 链路故障或非法帧。
 */
static esp_err_t wss_ws_recv(uint8_t *opcode_out, uint8_t *payload, size_t cap,
                             size_t *len_out, bool *idle_out, bool *fin_out)
{
    *idle_out = false;
    *fin_out = false;
    if (opcode_out == NULL || payload == NULL || len_out == NULL || fin_out == NULL ||
        s_tls == NULL) {
        return ESP_FAIL;
    }

    uint8_t first;
    if (s_rx_extra_pos < s_rx_extra_len) {
        /* 握手余量里已有帧字节：直接取用，不走 socket 读（不产生空闲超时）。 */
        first = s_rx_extra[s_rx_extra_pos++];
        if (s_rx_extra_pos >= s_rx_extra_len) {
            s_rx_extra_pos = 0;
            s_rx_extra_len = 0;
        }
    } else {
        int n = esp_tls_conn_read(s_tls, &first, 1);
        if (n == 0) {
            return ESP_FAIL;
        }
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                *idle_out = true;
                return ESP_OK;
            }
            return ESP_FAIL;
        }
    }

    uint8_t hdr[2];
    hdr[0] = first;
    if (wss_tls_read_exact(hdr + 1, 1) != ESP_OK) {
        return ESP_FAIL;
    }
    uint8_t op = hdr[0] & 0x0F;
    *fin_out = (hdr[0] & 0x80) != 0;
    if ((hdr[0] & 0x70) != 0) {             /* RSV1-3：未协商任何扩展 */
        ESP_LOGW(TAG, "WS frame with unsupported RSV extension bits rejected");
        (void)wss_send_close(1002);
        return ESP_FAIL;
    }
    /* 扩展长度先按 uint64_t 解析（ESP32 的 size_t 只有 32 位，直接左移写入
     * size_t 会让恶意超长声明截断，随后按错误长度读取载荷造成协议流错位）。 */
    uint64_t len64 = hdr[1] & 0x7F;
    if (len64 == 126) {
        uint8_t b[2];
        if (wss_tls_read_exact(b, sizeof(b)) != ESP_OK) {
            return ESP_FAIL;
        }
        len64 = ((uint64_t)b[0] << 8) | b[1];
        if (len64 < 126) {                  /* RFC 6455 5.2：长度必须使用最短编码 */
            ESP_LOGW(TAG, "WS frame with non-minimal length encoding rejected");
            (void)wss_send_close(1002);
            return ESP_FAIL;
        }
    } else if (len64 == 127) {
        uint8_t b[8];
        if (wss_tls_read_exact(b, sizeof(b)) != ESP_OK) {
            return ESP_FAIL;
        }
        if ((b[0] & 0x80) != 0) {           /* RFC 6455：64 位长度最高位必须为 0 */
            ESP_LOGW(TAG, "WS frame with high bit set in 64-bit length rejected");
            (void)wss_send_close(1002);
            return ESP_FAIL;
        }
        len64 = 0;
        for (int i = 0; i < 8; i++) {
            len64 = (len64 << 8) | b[i];
        }
        if (len64 <= 0xFFFF) {              /* RFC 6455 5.2：长度必须使用最短编码 */
            ESP_LOGW(TAG, "WS frame with non-minimal length encoding rejected");
            (void)wss_send_close(1002);
            return ESP_FAIL;
        }
    }
    if ((hdr[1] & 0x80) != 0) {             /* RFC 6455：服务端帧不得掩码 */
        ESP_LOGW(TAG, "Masked server WS frame rejected");
        (void)wss_send_close(1002);
        return ESP_FAIL;
    }
    if (op >= 0x8 && len64 > 125) {         /* RFC 6455：控制帧载荷不得超过 125 */
        ESP_LOGW(TAG, "Oversized WS control frame (%" PRIu64 ")", len64);
        (void)wss_send_close(1002);
        return ESP_FAIL;
    }
    /* 读取载荷前先把超长声明拒掉，再安全收窄为 size_t。 */
    if (len64 > cap) {
        ESP_LOGW(TAG, "Oversized WS frame (%" PRIu64 ")", len64);
        (void)wss_send_close(1002);
        return ESP_FAIL;
    }
    size_t len = (size_t)len64;
    if (wss_tls_read_exact(payload, len) != ESP_OK) {
        return ESP_FAIL;
    }
    *opcode_out = op;
    *len_out = len;
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/* TLS 连接 + HTTP 升级握手                                            */
/* ------------------------------------------------------------------ */

/**
 * @brief 解析 WSS Bearer token：优先设备通用 token，为空时回退 WSS 专用配置。
 *
 * @return token 字符串指针，可能为空串。
 */
static const char *wss_token(void)
{
#if defined(CONFIG_COMM_DEVICE_AUTH_TOKEN_VALUE)
    if (CONFIG_COMM_DEVICE_AUTH_TOKEN_VALUE[0] != '\0') {
        return CONFIG_COMM_DEVICE_AUTH_TOKEN_VALUE;
    }
#endif
    return CONFIG_WSS_TOKEN;
}

/** 大小写不敏感的 ASCII 等长比较。 */
static bool wss_ascii_ci_equal(const char *a, const char *b, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        char ca = a[i];
        char cb = b[i];
        if (ca >= 'A' && ca <= 'Z') {
            ca += 'a' - 'A';
        }
        if (cb >= 'A' && cb <= 'Z') {
            cb += 'a' - 'A';
        }
        if (ca != cb) {
            return false;
        }
    }
    return true;
}

/** 判断头部行的字段名是否等于给定名称（大小写不敏感，容忍名字后空白）。 */
static bool wss_header_name_is(const char *line, size_t name_len, const char *name)
{
    while (name_len > 0 && (line[name_len - 1] == ' ' || line[name_len - 1] == '\t')) {
        name_len--;
    }
    return name_len == strlen(name) && wss_ascii_ci_equal(line, name, name_len);
}

/**
 * @brief 判断头部值中是否含有给定逗号分隔 token（大小写不敏感）。
 *
 * 例如 Connection: keep-alive, Upgrade 必须视为包含 Upgrade。
 *
 * @param[in] value     头部值首地址。
 * @param[in] value_len 头部值长度（不含行尾 CRLF）。
 * @param[in] token     目标 token，不允许为 NULL。
 * @return true 值中存在该 token；false 不存在。
 */
static bool wss_value_has_token(const char *value, size_t value_len, const char *token)
{
    size_t token_len = strlen(token);
    size_t pos = 0;
    while (pos < value_len) {
        while (pos < value_len && (value[pos] == ',' || value[pos] == ' ' || value[pos] == '\t')) {
            pos++;
        }
        size_t start = pos;
        while (pos < value_len && value[pos] != ',') {
            pos++;
        }
        size_t tok_len = pos - start;
        while (tok_len > 0 &&
               (value[start + tok_len - 1] == ' ' || value[start + tok_len - 1] == '\t')) {
            tok_len--;
        }
        if (tok_len == token_len && wss_ascii_ci_equal(value + start, token, tok_len)) {
            return true;
        }
    }
    return false;
}

/**
 * @brief 以请求 nonce 计算并比对 Sec-WebSocket-Accept。
 *
 * accept = base64(SHA1(nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))，
 * 与响应头逐字节比较，杜绝任何未认证的伪造升级响应。
 *
 * @param[in] key_b64   请求中发送的 Sec-WebSocket-Key（base64 文本）。
 * @param[in] value     响应头 Sec-WebSocket-Accept 的值。
 * @param[in] value_len 响应头值的长度。
 * @return true 计算值与响应一致；false 不一致或计算失败。
 */
static bool wss_ws_accept_matches(const char *key_b64, const char *value, size_t value_len)
{
    static const char magic[] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    size_t key_len = strlen(key_b64);
    char input[64 + sizeof(magic)];
    if (key_len >= sizeof(input) - sizeof(magic) + 1) {
        return false;
    }
    memcpy(input, key_b64, key_len);
    memcpy(input + key_len, magic, sizeof(magic) - 1);

    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA1);
    if (info == NULL) {
        return false;
    }
    uint8_t digest[20];
    /* PSA 可能尚未初始化；psa_crypto_init() 幂等，重复调用代价可忽略。 */
    (void)psa_crypto_init();
    if (mbedtls_md(info, (const unsigned char *)input,
                   key_len + sizeof(magic) - 1, digest) != 0) {
        return false;
    }
    uint8_t b64[32];
    size_t out_len = 0;
    if (mbedtls_base64_encode(b64, sizeof(b64), &out_len, digest, sizeof(digest)) != 0) {
        return false;
    }
    return out_len == value_len && memcmp(b64, value, value_len) == 0;
}

/**
 * @brief 严格校验 RFC 6455 升级响应。
 *
 * 状态行必须恰为 "HTTP/1.x 101"；Upgrade: websocket、Connection 含 Upgrade
 * token；Sec-WebSocket-Accept 必须与请求 nonce 的计算值一致。
 *
 * @param[in] resp      响应头部首地址。
 * @param[in] resp_len  响应头部长度（含末尾 "\r\n\r\n"）。
 * @param[in] key_b64   请求中的 Sec-WebSocket-Key。
 * @return true 升级响应合法；false 任何一项不满足。
 */
static bool wss_ws_validate_response(const char *resp, size_t resp_len, const char *key_b64)
{
    static const char status_1_1[] = "HTTP/1.1 101";
    static const char status_1_0[] = "HTTP/1.0 101";
    if (resp_len < sizeof(status_1_1) ||
        !((memcmp(resp, status_1_1, sizeof(status_1_1) - 1) == 0 &&
           (resp[sizeof(status_1_1) - 1] == ' ' || resp[sizeof(status_1_1) - 1] == '\r')) ||
          (memcmp(resp, status_1_0, sizeof(status_1_0) - 1) == 0 &&
           (resp[sizeof(status_1_0) - 1] == ' ' || resp[sizeof(status_1_0) - 1] == '\r')))) {
        return false;
    }
    const char *line_end = strstr(resp, "\r\n");
    if (line_end == NULL) {
        return false;
    }

    bool has_upgrade = false;
    bool has_connection = false;
    bool has_accept = false;
    bool accept_ok = false;
    const char *p = line_end + 2;
    while (p < resp + resp_len) {
        const char *eol = strstr(p, "\r\n");
        if (eol == NULL) {
            return false;
        }
        size_t line_len = (size_t)(eol - p);
        if (line_len == 0) {
            break;                          /* 空行：头部结束 */
        }
        const char *colon = memchr(p, ':', line_len);
        if (colon == NULL) {
            return false;                   /* 非法的头部行 */
        }
        size_t name_len = (size_t)(colon - p);
        const char *val = colon + 1;
        while (val < eol && (*val == ' ' || *val == '\t')) {
            val++;
        }
        size_t val_len = (size_t)(eol - val);
        while (val_len > 0 && (val[val_len - 1] == ' ' || val[val_len - 1] == '\t')) {
            val_len--;
        }
        if (wss_header_name_is(p, name_len, "Upgrade")) {
            has_upgrade = val_len == strlen("websocket") &&
                          wss_ascii_ci_equal(val, "websocket", val_len);
        } else if (wss_header_name_is(p, name_len, "Connection")) {
            has_connection = wss_value_has_token(val, val_len, "Upgrade");
        } else if (wss_header_name_is(p, name_len, "Sec-WebSocket-Accept")) {
            has_accept = true;
            accept_ok = wss_ws_accept_matches(key_b64, val, val_len);
        }
        p = eol + 2;
    }
    return has_upgrade && has_connection && has_accept && accept_ok;
}

/**
 * @brief 在已建立的 TLS 连接上完成 WebSocket HTTP 升级握手。
 *
 * 随机 16 字节 Sec-WebSocket-Key 经 base64 编码，请求头携带
 * Authorization: Bearer <token>；按 RFC 6455 严格校验升级响应，并把
 * 响应头之后同一读取带回的首帧字节保留给帧解析。
 *
 * @return ESP_OK 升级成功；ESP_FAIL 请求构造、写出或响应校验失败。
 */
static esp_err_t wss_ws_handshake(void)
{
    if (s_tls == NULL) {
        return ESP_FAIL;
    }

    uint8_t rnd[16];
    uint8_t keyb64[32];
    size_t outlen = 0;
    esp_fill_random(rnd, sizeof(rnd));
    if (mbedtls_base64_encode(keyb64, sizeof(keyb64), &outlen, rnd, sizeof(rnd)) != 0 ||
        outlen >= sizeof(keyb64)) {
        return ESP_FAIL;
    }
    keyb64[outlen] = '\0';

    const char *token = wss_token();
    if (token[0] == '\0') {
        ESP_LOGW(TAG, "WSS token is empty; server may reject the upgrade");
    }

    char req[WSS_REQ_BUF_SIZE];
    int n = snprintf(req, sizeof(req),
                     "GET %s HTTP/1.1\r\n"
                     "Host: %s:%d\r\n"
                     "Upgrade: websocket\r\n"
                     "Connection: Upgrade\r\n"
                     "Sec-WebSocket-Key: %s\r\n"
                     "Sec-WebSocket-Version: 13\r\n"
                     "Authorization: Bearer %s\r\n"
                     "\r\n",
                     CONFIG_WSS_PATH, CONFIG_WSS_SERVER_HOST, CONFIG_WSS_SERVER_PORT,
                     keyb64, token);
    if (n <= 0 || (size_t)n >= sizeof(req)) {
        ESP_LOGE(TAG, "WSS upgrade request too long");
        return ESP_FAIL;
    }
    if (wss_tls_write_all(req, (size_t)n) != ESP_OK) {
        return ESP_FAIL;
    }

    char buf[WSS_HANDSHAKE_BUF_SIZE];
    size_t got = 0;
    unsigned eagain_budget = WSS_HANDSHAKE_EAGAIN_BUDGET;
    const char *hdr_end = NULL;
    while (got < sizeof(buf) - 1) {
        int r = esp_tls_conn_read(s_tls, buf + got, sizeof(buf) - 1 - got);
        if (r == 0) {
            return ESP_FAIL;
        }
        if (r < 0) {
            if ((errno == EAGAIN || errno == EWOULDBLOCK) && eagain_budget > 0) {
                eagain_budget--;
                continue;
            }
            return ESP_FAIL;
        }
        got += (size_t)r;
        buf[got] = '\0';
        hdr_end = strstr(buf, "\r\n\r\n");
        if (hdr_end != NULL) {
            break;
        }
    }
    if (hdr_end == NULL) {
        ESP_LOGE(TAG, "WSS handshake failed: response headers incomplete or too long");
        return ESP_FAIL;
    }
    size_t header_len = (size_t)(hdr_end - buf) + 4;
    /* 一次 TLS 读取可能同时带回 HTTP 头与首个 WebSocket 帧：头结束后的余量
     * 必须保留给帧解析，丢弃会导致后续解析错位并断线。 */
    if (got > header_len) {
        memcpy(s_rx_extra, buf + header_len, got - header_len);
        s_rx_extra_len = got - header_len;
        s_rx_extra_pos = 0;
    }
    if (!wss_ws_validate_response(buf, header_len, (const char *)keyb64)) {
        ESP_LOGE(TAG, "WSS handshake failed: invalid RFC 6455 upgrade response: %.*s",
                 (int)header_len, buf);
        return ESP_FAIL;
    }
    return ESP_OK;
}

/**
 * @brief 建立 TLS 连接并完成 WebSocket 升级握手。
 *
 * @return true 连接并升级成功，s_tls 已指向有效会话；
 * @return false 连接、握手或升级失败，资源已释放。
 */
static bool wss_connect(void)
{
    esp_tls_t *tls = esp_tls_init();
    if (tls == NULL) {
        ESP_LOGE(TAG, "esp_tls_init failed");
        return false;
    }
    const char *host = CONFIG_WSS_SERVER_HOST;
    int ret = esp_tls_conn_new_sync(host, (int)strlen(host), CONFIG_WSS_SERVER_PORT,
                                    &s_tls_cfg, tls);
    if (ret != 1) {
        ESP_LOGW(TAG, "WSS TLS connect failed (%d)", ret);
        (void)esp_tls_conn_destroy(tls);
        return false;
    }

    /* TCP 与 TLS 握手已完成。HTTP 升级仍使用较长读取超时；升级成功后再
     * 收紧到一帧周期，避免实时 MIC 队列在 recv 中积压。 */
    int sockfd = -1;
    if (esp_tls_get_conn_sockfd(tls, &sockfd) == ESP_OK && sockfd >= 0) {
        wss_set_receive_timeout(sockfd, WSS_HANDSHAKE_READ_TIMEOUT_MS);
        /* 写方向同样要有界：服务端停止读取时 send 只阻塞到 SO_SNDTIMEO，
         * 随后写路径按链路故障结束会话并重连，避免推送大文件时永久挂死。 */
        struct timeval wtv;
        wtv.tv_sec = WSS_WRITE_TIMEOUT_MS / 1000;
        wtv.tv_usec = (WSS_WRITE_TIMEOUT_MS % 1000) * 1000;
        if (setsockopt(sockfd, SOL_SOCKET, SO_SNDTIMEO, &wtv, sizeof(wtv)) != 0) {
            ESP_LOGW(TAG, "setsockopt(SO_SNDTIMEO) failed; writes may block longer");
        }
        /* TCP keepalive 与应用层 Ping/Pong 互补：半开链路可被底层探测提前发现。 */
        int keepalive = 1;
        if (setsockopt(sockfd, SOL_SOCKET, SO_KEEPALIVE, &keepalive, sizeof(keepalive)) != 0) {
            ESP_LOGW(TAG, "setsockopt(SO_KEEPALIVE) failed");
        }
    }

    /* 新会话开始前清空上一会话可能残留的握手余量。 */
    s_rx_extra_len = 0;
    s_rx_extra_pos = 0;

    s_tls = tls;
    if (wss_ws_handshake() != ESP_OK) {
        (void)esp_tls_conn_destroy(s_tls);
        s_tls = NULL;
        return false;
    }
    if (sockfd >= 0) {
        wss_set_receive_timeout(sockfd, WSS_READ_TIMEOUT_MS);
    }
    ESP_LOGI(TAG, "WSS connected & authenticated (wss://%s:%d%s)",
             CONFIG_WSS_SERVER_HOST, CONFIG_WSS_SERVER_PORT, CONFIG_WSS_PATH);
    return true;
}

/* ------------------------------------------------------------------ */
/* 会话任务与命令队列                                                  */
/* ------------------------------------------------------------------ */

/**
 * @brief 排空外部命令队列（只负责把不透明条目交给上层回调）。
 *
 * 每条条目在会话任务上下文中执行；上层回调内可直接调用 wss_transport_send_now()。
 */
static void wss_drain_queue(void)
{
    while (xQueueReceive(s_cmd_queue, s_queue_item, 0) == pdTRUE) {
        s_config.on_queue_item(s_queue_item, s_config.queue_item_size);
        if (s_session_failed) {
            break;
        }
    }
}

/**
 * @brief 校验文本载荷是否为合法 UTF-8（RFC 3629，RFC 6455 8.1 的要求）。
 *
 * 拒绝：孤立续字节、过短/过长编码、U+D800-DFFF 代理区、超过 U+10FFFF 的码点。
 *
 * @param[in] data 载荷首地址。
 * @param[in] len  载荷长度。
 * @return true 合法 UTF-8；false 非法。
 */
static bool wss_text_is_valid_utf8(const uint8_t *data, size_t len)
{
    size_t i = 0;
    while (i < len) {
        uint8_t b = data[i];
        if (b < 0x80) {
            i++;
        } else if ((b & 0xE0) == 0xC0) {
            /* 2 字节序列：0xC2-0xDF + 续字节（0xC0/0xC1 是过短编码） */
            if (b < 0xC2 || i + 1 >= len || (data[i + 1] & 0xC0) != 0x80) {
                return false;
            }
            i += 2;
        } else if ((b & 0xF0) == 0xE0) {
            /* 3 字节序列：排除过短（E0 80-9F）与代理区（ED A0-BF） */
            if (i + 2 >= len ||
                (data[i + 1] & 0xC0) != 0x80 || (data[i + 2] & 0xC0) != 0x80) {
                return false;
            }
            if ((b == 0xE0 && data[i + 1] < 0xA0) || (b == 0xED && data[i + 1] >= 0xA0)) {
                return false;
            }
            i += 3;
        } else if ((b & 0xF8) == 0xF0) {
            /* 4 字节序列：排除过短（F0 80-8F）、F5-FF 与超过 U+10FFFF（F4 90-BF） */
            if (b > 0xF4 || i + 3 >= len ||
                (data[i + 1] & 0xC0) != 0x80 || (data[i + 2] & 0xC0) != 0x80 ||
                (data[i + 3] & 0xC0) != 0x80) {
                return false;
            }
            if ((b == 0xF0 && data[i + 1] < 0x90) || (b == 0xF4 && data[i + 1] >= 0x90)) {
                return false;
            }
            i += 4;
        } else {
            return false;                   /* 0x80-0xBF 孤立续字节 */
        }
    }
    return true;
}

/**
 * @brief 判断 CLOSE 帧状态码是否合法（RFC 6455 7.4 与 IANA WebSocket 注册表）。
 *
 * 合法：1000-1014 中除保留码 1004（保留）、1005（无状态码）、1006（异常关闭）
 * 之外的已注册码，以及 3000-4999（应用/私有用途）。其余值（<1000、
 * 1016-2999 未注册段、1015）视为非法，收到时必须按协议错误关闭。
 *
 * @param[in] code 状态码。
 * @return true 合法；false 非法。
 */
static bool wss_close_code_is_valid(uint16_t code)
{
    if (code >= 3000 && code <= 4999) {
        return true;
    }
    if (code >= 1000 && code <= 1014) {
        return code != 1004 && code != 1005 && code != 1006;
    }
    return false;
}

/**
 * @brief 运行一次完整的 WSS 会话，直到链路故障、对端关闭或保活超时。
 *
 * 循环结构：先排空命令队列，再接收一帧；帧首字节的空闲超时是队列处理与
 * 保活窗口。空闲本身不代表断线：服务端静默达到
 * CONFIG_WSS_KEEPALIVE_INTERVAL_SECONDS 秒后客户端主动发 PING；发出 PING 后
 * 只要在 CONFIG_WSS_PONG_TIMEOUT_SECONDS 内收到任意下行帧（PONG 或其他帧都
 * 证明链路存活）就取消待定探测并重置保活计时，只有该窗口内完全没有下行帧才
 * 判定链路死亡。服务端分片消息（RFC 6455）跨帧重组后再交给上层回调。
 */
static void wss_run_session(void)
{
    /* 会话级状态复位：写失败标志、分片重组进度与保活计时。 */
    s_session_failed = false;
    s_msg_active = false;
    s_msg_len = 0;
    int64_t last_rx_us = esp_timer_get_time();
    int64_t last_ping_us = last_rx_us;
    bool pong_pending = false;

    while (s_tls != NULL) {
        wss_drain_queue();
        if (s_session_failed) {
            ESP_LOGW(TAG, "WSS write failure during queued item handling; closing session");
            break;
        }

        uint8_t op = 0;
        static uint8_t frame_payload[WSS_TRANSPORT_MAX_PAYLOAD + 1]; /* 大缓冲留在静态区 */
        size_t len = 0;
        bool idle = false;
        bool fin = true;
        if (wss_ws_recv(&op, frame_payload, sizeof(frame_payload) - 1,
                        &len, &idle, &fin) != ESP_OK) {
            ESP_LOGW(TAG, "WSS receive failed; closing session");
            break;
        }

        if (idle) {
            /* 空闲窗口：只做主动保活，不把空闲当作断线。 */
            int64_t now_us = esp_timer_get_time();
            if (pong_pending) {
                int64_t pong_timeout_us = (int64_t)CONFIG_WSS_PONG_TIMEOUT_SECONDS * 1000000LL;
                if (now_us - last_ping_us >= pong_timeout_us) {
                    ESP_LOGW(TAG, "WSS keepalive: no downlink frame within %d s after PING; reconnecting",
                             CONFIG_WSS_PONG_TIMEOUT_SECONDS);
                    break;
                }
            } else {
                int64_t keepalive_us = (int64_t)CONFIG_WSS_KEEPALIVE_INTERVAL_SECONDS * 1000000LL;
                if (now_us - last_rx_us >= keepalive_us) {
                    if (wss_ws_send(0x9, NULL, 0) != ESP_OK) {
                        ESP_LOGW(TAG, "WSS keepalive PING send failed; reconnecting");
                        break;
                    }
                    last_ping_us = now_us;
                    pong_pending = true;
                }
            }
            continue;
        }

        /* 有下行帧：链路确认存活；取消待定 PONG 探测并重置保活计时。RFC 6455
         * 只要求对 PING 回 PONG，但任何下行帧都能证明链路可用，因此不限于 PONG。 */
        last_rx_us = esp_timer_get_time();
        pong_pending = false;

        if (op >= 0x8) {                    /* 控制帧：CLOSE/PING/PONG */
            if (!fin || len > 125) {
                ESP_LOGW(TAG, "Invalid WS control frame (fragmented or oversized)");
                (void)wss_send_close(1002);
                break;
            }
            if (op == 0x9) {                /* PING -> PONG */
                if (wss_ws_send(0xA, frame_payload, len) != ESP_OK) {
                    break;
                }
                continue;
            }
            if (op == 0x8) {                /* CLOSE：先校验载荷，再回送 CLOSE（RFC 6455 5.5.1） */
                uint16_t close_code = 0;
                if (len == 1) {             /* RFC 6455 5.5.1：CLOSE 载荷要么为空要么 ≥2 字节 */
                    ESP_LOGW(TAG, "Invalid WS CLOSE frame (1-byte payload); closing with 1002");
                    (void)wss_send_close(1002);
                    break;
                }
                if (len >= 2) {
                    close_code = (uint16_t)(((uint16_t)frame_payload[0] << 8) |
                                            frame_payload[1]);
                    if (!wss_close_code_is_valid(close_code)) {
                        ESP_LOGW(TAG, "Invalid WS CLOSE status code 0x%04X; closing with 1002",
                                 close_code);
                        (void)wss_send_close(1002);
                        break;
                    }
                    /* RFC 6455 5.5.1：状态码之后的 reason 必须是合法 UTF-8 */
                    if (!wss_text_is_valid_utf8(frame_payload + 2, len - 2)) {
                        ESP_LOGW(TAG, "WS CLOSE reason is not valid UTF-8; closing with 1002");
                        (void)wss_send_close(1002);
                        break;
                    }
                }
                ESP_LOGI(TAG, "WSS server sent CLOSE (code 0x%04X); replying CLOSE", close_code);
                (void)wss_send_close(close_code);   /* 回显状态码；无状态码则发空载荷 */
                break;
            }
            if (op != 0xA) {                /* 保留控制操作码 0xB-0xF：协议错误（RFC 6455 5.5） */
                ESP_LOGW(TAG, "Reserved WS control opcode 0x%x rejected", op);
                (void)wss_send_close(1002);
                break;
            }
            /* PONG（0xA）：待定探测已在上方统一清除。 */
            continue;
        }

        /* 数据帧：RFC 6455 允许分片，跨帧重组后再处理完整消息。 */
        if (s_msg_active) {
            if (op != 0x0) {                /* 重组过程中不允许新消息帧 */
                ESP_LOGW(TAG, "New data frame during fragmented message; closing session");
                (void)wss_send_close(1002);
                break;
            }
        } else {
            if (op == 0x0) {
                ESP_LOGW(TAG, "Continuation frame without a message start; closing session");
                (void)wss_send_close(1002);
                break;
            }
            if (op != 0x1 && op != 0x2) {   /* 保留数据操作码 0x3-0x7：协议错误（RFC 6455 5.5） */
                ESP_LOGW(TAG, "Reserved WS data opcode 0x%x rejected", op);
                (void)wss_send_close(1002);
                break;
            }
            s_msg_active = true;
            s_msg_opcode = op;
            s_msg_len = 0;
        }
        if (len > WSS_TRANSPORT_MAX_PAYLOAD - s_msg_len) {
            ESP_LOGW(TAG, "Reassembled WS message exceeds payload cap");
            (void)wss_send_close(1002);
            break;
        }
        memcpy(s_msg_payload + s_msg_len, frame_payload, len);
        s_msg_len += len;
        if (!fin) {
            continue;                       /* 等待后续分片 */
        }

        uint8_t msg_opcode = s_msg_opcode;
        size_t msg_len = s_msg_len;
        s_msg_active = false;
        s_msg_len = 0;
        if (msg_opcode == 0x1) {
            /* RFC 6455 8.1：文本消息必须携带合法 UTF-8，否则以 1007 失败连接。 */
            if (!wss_text_is_valid_utf8(s_msg_payload, msg_len)) {
                ESP_LOGW(TAG, "WS text message is not valid UTF-8; closing with 1007");
                (void)wss_send_close(1007);
                break;
            }
            if (s_config.on_text != NULL) {
                /* 完整文本消息交给上层业务回调。 */
                s_config.on_text(s_msg_payload, msg_len);
                if (s_session_failed) {
                    break;
                }
            }
        } else if (msg_opcode == 0x2) {
            /* 服务端二进制消息（下行 PCM 等）交给上层业务回调。 */
            if (s_config.on_binary != NULL) {
                s_config.on_binary(s_msg_payload, msg_len);
                if (s_session_failed) {
                    break;
                }
            }
        }
        /* 服务端其他数据帧无下行用途，直接忽略。 */
    }

    (void)esp_tls_conn_destroy(s_tls);
    s_tls = NULL;
    s_msg_active = false;
    s_msg_len = 0;
    if (s_config.on_session_end != NULL) {
        s_config.on_session_end();
    }
    ESP_LOGW(TAG, "WSS session ended; reconnecting in %d s",
             CONFIG_WSS_RECONNECT_INTERVAL_SECONDS);
}

/**
 * @brief 会话任务主体：连接 -> 会话 -> 退避 -> 重连，永不退出。
 *
 * @param[in] parameter FreeRTOS 任务参数，本实现未使用。
 */
static void wss_session_task(void *parameter)
{
    (void)parameter;
    for (;;) {
        if (wss_connect()) {
            wss_run_session();
        }
        vTaskDelay(pdMS_TO_TICKS((uint32_t)CONFIG_WSS_RECONNECT_INTERVAL_SECONDS * 1000U));
    }
}

/* ------------------------------------------------------------------ */
/* 公共接口                                                            */
/* ------------------------------------------------------------------ */

esp_err_t wss_transport_start(const wss_transport_config_t *config)
{
    if (config == NULL || config->on_queue_item == NULL ||
        config->queue_item_size == 0U || config->queue_depth == 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_start_lock);
    if (s_started) {
        portEXIT_CRITICAL(&s_start_lock);
        return ESP_OK;
    }
    if (s_starting) {
        portEXIT_CRITICAL(&s_start_lock);
        return ESP_ERR_INVALID_STATE;
    }
    s_starting = true;
    portEXIT_CRITICAL(&s_start_lock);

    /* 信任锚与超时配置只需填充一次；每次重连复用同一配置。
     * 自签服务器（如 172.20.10.2）：证书链由 ca_cert.pem 校验，
     * 服务器证书 CN 是 "Voice Robot Local CA"，与主机名不匹配，
     * 跳过 CN（Identity)检查——身份已由信任锚链绑定。
     * 若服务器证书含该 IP 的 SAN，可移除本行。 */
    s_tls_cfg.cacert_buf = ca_cert_pem_start;
    s_tls_cfg.cacert_bytes = (unsigned int)(ca_cert_pem_end - ca_cert_pem_start);
    s_tls_cfg.skip_common_name = true;
    s_tls_cfg.timeout_ms = WSS_CONNECT_TIMEOUT_MS;
    s_config = *config;

    esp_err_t err = ESP_OK;
    if (s_queue_item == NULL) {
        s_queue_item = malloc(config->queue_item_size);
        if (s_queue_item == NULL) {
            err = ESP_ERR_NO_MEM;
            goto finish;
        }
    }
    if (s_cmd_queue == NULL) {
        s_cmd_queue = xQueueCreate(config->queue_depth, config->queue_item_size);
    }
    if (s_cmd_queue == NULL) {
        err = ESP_ERR_NO_MEM;
        goto finish;
    }
    if (xTaskCreate(wss_session_task, "wss_transport", (uint32_t)CONFIG_WSS_TASK_STACK_SIZE,
                    NULL, 4, &s_session_task) != pdPASS) {
        vQueueDelete(s_cmd_queue);
        s_cmd_queue = NULL;
        err = ESP_ERR_NO_MEM;
        goto finish;
    }
    ESP_LOGI(TAG, "WSS transport client started");

finish:
    portENTER_CRITICAL(&s_start_lock);
    s_starting = false;
    s_started = (err == ESP_OK);
    portEXIT_CRITICAL(&s_start_lock);
    return err;
}

esp_err_t wss_transport_enqueue(const void *item, size_t item_size)
{
    if (item == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_cmd_queue == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (item_size != s_config.queue_item_size) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (xQueueSend(s_cmd_queue, item, 0) != pdTRUE) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t wss_transport_send_now(uint8_t opcode, const uint8_t *payload, size_t len)
{
    esp_err_t err = wss_ws_send(opcode, payload, len);
    if (err != ESP_OK) {
        /* 只有会话级写失败才标记链路故障并重连；参数非法不是会话故障。 */
        if (err == ESP_FAIL) {
            s_session_failed = true;
        }
        return err;
    }
    return ESP_OK;
}
