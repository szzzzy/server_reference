/**
 * @file    http_downloader.h
 * @brief   公共 HTTPS 数据面下载器：Range 断点续传、ETag 与响应一致性校验。
 *
 * 本模块从 ota_engine.c 的 HTTP 下载循环中抽取，供固件 OTA 与音频素材下载
 * 共用同一套传输核心：连接建立、TLS 失败分类、Range 续传与 200/416 回退、
 * HTTP 状态码 / Content-Length / Content-Range / ETag 校验、带空读超时的
 * 读取循环与完整 body 判定。
 *
 * 业务差异（写入目标、镜像/素材校验、断点检查点、进度上报）通过 sink 回调
 * 注入；下载器决定放弃断点从头重试（ETag 变化或服务器忽略 Range）时，通过
 * restart 回调通知调用方重建持久化断点记录。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "ota_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 下载结果中的 ETag 文本容量，包含末尾 NUL。 */
#define HTTP_DOWNLOADER_ETAG_SIZE 128

/**
 * @brief 数据接收回调，下载器每读到一个网络块就同步调用一次。
 *
 * @param[in] ctx  调用方提供的上下文，不允许为 NULL。
 * @param[in] data 本次网络块数据，仅在回调返回前有效，不允许为 NULL。
 * @param[in] len  数据长度，单位为字节，大于 0。
 *
 * @return ESP_OK 继续下载。
 * @return 其他 esp_err_t 中止下载；错误码原样返回给 http_downloader_run()，
 *         且 result->failure_reason 保持 NATIVE_OTA_FAILURE_NONE，由调用方
 *         自行分类业务错误。
 *
 * @note 回调在普通任务上下文同步执行，可以写 Flash/NVS，但不能阻塞过久；
 *       不能保留 data 指针。
 */
typedef esp_err_t (*http_downloader_sink_t)(void *ctx, const uint8_t *data, size_t len);

/**
 * @brief 断点重置回调：服务器忽略 Range（返回 200/416）或 ETag 变化时调用。
 *
 * @param[in] ctx 调用方提供的上下文，不允许为 NULL。
 *
 * @return ESP_OK 允许从零开始全量下载。
 * @return 其他 esp_err_t 中止下载并原样返回。
 *
 * @note 回调内应重建持久化断点记录并清零内部偏移状态；此时下载器尚未开始
 *       读取新连接的 body，调用方可安全重置写目标。
 */
typedef esp_err_t (*http_downloader_restart_cb_t)(void *ctx);

/**
 * @brief 一次下载任务的传输参数。
 *
 * @note cert_pem 为 NULL 时使用构建嵌入的 ca_cert.pem；expected_size 为 0
 *       表示服务器可能使用 chunked 编码（无 Content-Length），此时只依赖
 *       complete 标志判定收尾，且 resume_offset 必须为 0。
 */
typedef struct {
    const char *url; /**< HTTPS 下载地址，不允许为 NULL。 */
    const char *cert_pem; /**< 服务器根证书 PEM 文本；NULL 使用构建嵌入证书。 */
    int timeout_ms; /**< esp_http_client 接收超时，单位为毫秒。 */
    bool skip_cert_common_name_check; /**< 跳过服务器证书 CN 校验（仅开发）。 */
    size_t expected_size; /**< 清单声明的完整对象长度；0 表示未知。 */
    size_t resume_offset; /**< 断点偏移；0 表示全量下载。 */
    const char *expected_etag; /**< 断点对应的服务器 ETag；NULL 或空串不校验。 */
    http_downloader_sink_t sink; /**< 数据接收回调，不允许为 NULL。 */
    void *sink_ctx; /**< 原样传给 sink 的上下文。 */
    http_downloader_restart_cb_t restart_cb; /**< 断点重置回调，可为 NULL。 */
    void *restart_ctx; /**< 原样传给 restart_cb 的上下文。 */
} http_downloader_config_t;

/**
 * @brief 一次下载任务的传输结果。
 *
 * @note failure_reason 仅在传输层失败时设置（网络/TLS/HTTP 状态/Range 一致
 *       性）；sink 或 restart 回调返回的业务错误通过返回值透传，本字段保持
 *       NATIVE_OTA_FAILURE_NONE。
 */
typedef struct {
    int status_code; /**< 最终会话的 HTTP 状态码。 */
    size_t bytes_received; /**< 本次调用实际交给 sink 的字节数，不含断点前缀。 */
    size_t total_size; /**< Content-Range total 或 Content-Length；未知时为 0。 */
    char etag[HTTP_DOWNLOADER_ETAG_SIZE]; /**< 最终会话的服务器 ETag，可空。 */
    bool ranged; /**< 最终会话以 206 响应（断点续传）。 */
    bool restarted; /**< 本次调用发生过一次断点重置并全量重试。 */
    bool complete; /**< body 已完整接收（esp_http_client_is_complete_data_received）。 */
    native_ota_failure_reason_t failure_reason; /**< 传输层失败分类。 */
} http_downloader_result_t;

/**
 * @brief 执行一次带断点续传能力的 HTTPS 数据面下载。
 *
 * 内部最多允许一次“服务器忽略 Range 后从零重试”；ETag 变化或 200/416 响应
 * 都会先调用 restart_cb 再重新建立连接全量下载。所有网络块按顺序同步交给
 * sink，sink 返回非 ESP_OK 时立即中止。
 *
 * @param[in]  config 下载参数，不允许为 NULL。
 * @param[out] result 接收传输结果，不允许为 NULL。
 *
 * @return ESP_OK 传输层完成（是否完整由 result->complete 与调用方的字节数
 *         校验共同判定）。
 * @return ESP_ERR_INVALID_ARG 参数无效。
 * @return 其他 esp_err_t 传输失败或 sink/restart 回调返回的错误。
 *
 * @note 只能在普通任务上下文调用；函数阻塞到下载结束或失败。
 */
esp_err_t http_downloader_run(const http_downloader_config_t *config,
                              http_downloader_result_t *result);

#ifdef __cplusplus
}
#endif
