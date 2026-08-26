/**
 * @file    voice_service.c
 * @brief   语音业务服务实现：命令语法、WSS 文件推送协议与入口装配。
 *
 * 模块关系：
 * - 通过 mqtt_comm_register_topic() 注册语音命令 topic，MQTT 命令与 WSS 服务端
 *   文本命令共用同一套语法解析；
 * - 传输由纯传输层 wss_transport 完成（连接、帧、握手、保活、重连）；
 * - FILE_SEND 的 URI 映射由 voice_uri 完成；
 * - 所有对外接口只做有界入队，实际发送在 WSS 会话任务上下文中执行。
 */

#include "voice_service.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "esp_check.h"
#include "esp_log.h"

#include "board_audio.h"
#include "mqtt_comm.h"
#include "voice_uri.h"
#include "wss_transport.h"

/* SD 文件访问锁钩子（融合方案 §9.6）：底座工程由 sd_card.c 提供强符号
 * julia_wireless_sd_lock/unlock；组件内弱默认实现允许未接线时无锁退化。 */
__attribute__((weak)) bool julia_wireless_sd_lock(uint32_t timeout_ms)
{
    (void)timeout_ms;
    return true;
}
__attribute__((weak)) void julia_wireless_sd_unlock(void) {}

/** 本模块统一使用的日志标签。 */
static const char *TAG = "voice_service";

/** 命令队列深度：与云端基准实现保持一致。 */
#define VOICE_QUEUE_DEPTH 4

/**
 * @brief 命令队列中的一条作业：文件 URI（含 NUL）或一块 MIC 数据。
 *
 * 作业对传输层是纯不透明条目；本模块在 on_queue_item 回调中解释它。
 */
typedef enum {
    VOICE_JOB_SEND_FILE = 0, /**< FILE_SEND：推送一个音频文件。 */
    VOICE_JOB_MIC_START,     /**< MIC_START：开启流式发送状态。 */
    VOICE_JOB_MIC_STOP,      /**< MIC_STOP：关闭流式发送状态。 */
    VOICE_JOB_SEND_CHUNK,    /**< 发送一个 MIC 音频块。 */
} voice_job_type_t;

typedef struct {
    voice_job_type_t type;                  /**< 作业类型。 */
    size_t len;                             /**< data 的有效字节数。 */
    uint8_t data[WSS_TRANSPORT_MAX_PAYLOAD]; /**< URI 或音频块内容。 */
} voice_job_t;

/** MIC 流式发送状态，仅由 WSS 会话任务上下文读写。 */
static bool s_mic_active;

/** FILE_SEND 允许的最大文件大小（8 MiB，融合方案 §9.6 二次限制）。 */
#define VOICE_SEND_MAX_FILE_BYTES (8 * 1024 * 1024)

/**
 * @brief board_audio 的 WSS sink 适配器：完整 PCM1 帧（16 B 头 + PCM）
 * 作为一个二进制 WSS 消息入队。WSS 未启动时静默丢弃，不阻塞 mic_task。
 */
static void voice_service_on_board_audio_frame(const uint8_t *frame, size_t bytes, void *ctx)
{
    (void)ctx;
    (void)voice_service_send_chunk(frame, bytes);
}

/** 是否为允许外发的文件扩展名（当前仅 .wav）。 */
static bool voice_service_path_is_allowed(const char *path)
{
    size_t len = strlen(path);
    if (len < 4) {
        return false;
    }
    return strcasecmp(path + len - 4, ".wav") == 0;
}

/**
 * @brief 向服务端回一条 ERROR 文本帧；写出失败会由传输层标记会话故障。
 *
 * @param[in] text NUL 结尾的错误文本，不允许为 NULL。
 */
static esp_err_t voice_service_send_error(const char *text)
{
    return wss_transport_send_now(0x1, (const uint8_t *)text, strlen(text));
}

/**
 * @brief 推送一个音频文件（仅在 WSS 会话任务上下文中调用）。
 *
 * 打开失败向服务端回复 "ERROR file_open_failed"；成功后按协议发送
 * "BEGIN FILE <size> <name>"、若干 1200 B 二进制帧和 "END <bytes>"。
 * BEGIN、二进制帧、fread 或 END 任一失败都会置位会话故障标志（由传输层
 * 在写失败时设置），会话循环随后关闭并重连，绝不给截断文件发送 END。
 *
 * @param[in] uri NUL 结尾的文件 URI，长度受 VOICE_SERVICE_URI_MAX_LEN 约束。
 * @return ESP_OK 传输成功或命令被安全拒绝（会话仍健康）；
 * @return ESP_FAIL 传输中途失败，会话必须关闭重连。
 */
static esp_err_t voice_service_push_file(const char *uri)
{
    char path[VOICE_SERVICE_URI_MAX_LEN + 16];
    if (!voice_uri_to_path(uri, path, sizeof(path))) {
        (void)voice_service_send_error("ERROR bad_uri");
        ESP_LOGW(TAG, "Unsupported FILE_SEND uri: %s", uri);
        return ESP_OK;
    }
    if (!voice_service_path_is_allowed(path)) {
        (void)voice_service_send_error("ERROR bad_extension");
        ESP_LOGW(TAG, "FILE_SEND extension rejected: %s", path);
        return ESP_OK;
    }

    /* 文件访问必须持有 SD 锁（融合方案 §9.6）。 */
    if (!julia_wireless_sd_lock(3000)) {
        (void)voice_service_send_error("ERROR sd_busy");
        ESP_LOGW(TAG, "FILE_SEND rejected: SD lock busy");
        return ESP_OK;
    }
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        julia_wireless_sd_unlock();
        (void)voice_service_send_error("ERROR file_open_failed");
        ESP_LOGW(TAG, "Cannot open %s", path);
        return ESP_OK;
    }

    /* 先取文件大小并回到开头：fseek/ftell 任一失败都不能启动传输。 */
    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        julia_wireless_sd_unlock();
        (void)voice_service_send_error("ERROR file_size_failed");
        ESP_LOGW(TAG, "Cannot seek to end of %s", path);
        return ESP_OK;
    }
    long size = ftell(f);
    if (size < 0 || size > VOICE_SEND_MAX_FILE_BYTES || fseek(f, 0, SEEK_SET) != 0) {
        fclose(f);
        julia_wireless_sd_unlock();
        (void)voice_service_send_error("ERROR file_size_failed");
        ESP_LOGW(TAG, "Cannot determine size of %s (or exceeds limit)", path);
        return ESP_OK;
    }
    const char *base = strrchr(path, '/');
    base = (base != NULL) ? base + 1 : path;

    char begin[VOICE_SERVICE_URI_MAX_LEN + 64];
    int n = snprintf(begin, sizeof(begin), "BEGIN FILE %ld %s", size, base);
    if (n <= 0 || (size_t)n >= sizeof(begin)) {
        fclose(f);
        julia_wireless_sd_unlock();
        (void)voice_service_send_error("ERROR file_name_too_long");
        ESP_LOGW(TAG, "BEGIN frame too long for %s", path);
        return ESP_OK;
    }
    if (wss_transport_send_now(0x1, (const uint8_t *)begin, (size_t)n) != ESP_OK) {
        ESP_LOGE(TAG, "BEGIN frame send failed for %s", path);
        fclose(f);
        julia_wireless_sd_unlock();
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Pushing %s (%ld bytes)", path, size);

    static uint8_t buf[WSS_TRANSPORT_MAX_PAYLOAD];
    uint64_t total = 0;
    bool ok = true;
    for (;;) {
        size_t got = fread(buf, 1, sizeof(buf), f);
        if (got == 0) {
            if (ferror(f)) {
                ESP_LOGE(TAG, "fread failed for %s", path);
                ok = false;
            }
            break;
        }
        if (wss_transport_send_now(0x2, buf, got) != ESP_OK) {
            ESP_LOGE(TAG, "Binary frame send failed for %s at offset %" PRIu64, path, total);
            ok = false;
            break;
        }
        total += (uint64_t)got;
    }
    fclose(f);
    julia_wireless_sd_unlock();

    /* 仅当完整读出且每个字节都确认写出（total == 预取 size）时才发 END；
     * 任何失败都置位会话故障，把截断文件伪装成已结束传输。 */
    if (!ok || total != (uint64_t)size) {
        ESP_LOGW(TAG, "Aborting push of %s: %" PRIu64 "/%ld bytes transferred", path, total, size);
        return ESP_FAIL;
    }
    char end[48];
    n = snprintf(end, sizeof(end), "END %" PRIu64, total);
    if (n <= 0 || (size_t)n >= sizeof(end) ||
        wss_transport_send_now(0x1, (const uint8_t *)end, (size_t)n) != ESP_OK) {
        ESP_LOGE(TAG, "END frame send failed for %s", path);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Pushed %s (%" PRIu64 " bytes)", path, total);
    return ESP_OK;
}

/**
 * @brief 处理服务端下发的完整文本帧命令（wss_transport on_text 回调）。
 *
 * 在会话任务上下文中执行：当前只有 FILE_SEND 需要立即推送。
 *
 * @param[in] text 文本载荷，不要求以 NUL 结尾。
 * @param[in] len  文本长度。
 */
static void voice_service_on_server_text(const uint8_t *text, size_t len)
{
    ESP_LOGI(TAG, "Server cmd: %.*s", (int)len, (const char *)text);
    if (len > strlen("FILE_SEND ") && strncmp((const char *)text, "FILE_SEND ", 10) == 0) {
        char uri[VOICE_SERVICE_URI_MAX_LEN];
        size_t uri_len = len - 10;
        if (uri_len >= sizeof(uri)) {
            ESP_LOGW(TAG, "Server FILE_SEND uri too long");
            return;
        }
        memcpy(uri, text + 10, uri_len);
        uri[uri_len] = '\0';
        (void)voice_service_push_file(uri);
        return;
    }

    /* ---- 板级音频下行控制命令（融合方案 §9.4，与最小包 USB 命令同语义） ---- */
    if (len == 4 && memcmp(text, "SPKE", 4) == 0) {
        (void)board_audio_speaker_stop();
        ESP_LOGI(TAG, "SPKE: speaker stop");
    } else if (len == 4 && memcmp(text, "SPKT", 4) == 0) {
        (void)board_audio_speaker_self_test();
        ESP_LOGI(TAG, "SPKT: local tone test");
    } else if (len > 5 && memcmp(text, "SPKS ", 5) == 0) {
        char buf[16];
        size_t n = len - 5;
        if (n >= sizeof(buf)) n = sizeof(buf) - 1;
        memcpy(buf, text + 5, n);
        buf[n] = '\0';
        char *end = NULL;
        long rate = strtol(buf, &end, 10);
        if (end != buf && board_audio_speaker_start((uint32_t)rate) == ESP_OK) {
            ESP_LOGI(TAG, "SPKS: speaker start rate=%ld", rate);
        } else {
            ESP_LOGW(TAG, "Invalid SPKS rate: %.*s", (int)n, buf);
        }
    } else if (len > 5 && memcmp(text, "SPKV ", 5) == 0) {
        char buf[16];
        size_t n = len - 5;
        if (n >= sizeof(buf)) n = sizeof(buf) - 1;
        memcpy(buf, text + 5, n);
        buf[n] = '\0';
        char *end = NULL;
        long volume = strtol(buf, &end, 10);
        if (end != buf && volume >= 0 && volume <= 100) {
            board_audio_speaker_set_volume((uint8_t)volume);
            ESP_LOGI(TAG, "SPKV: volume=%ld", volume);
        } else {
            ESP_LOGW(TAG, "Invalid SPKV volume: %.*s", (int)n, buf);
        }
    } else if (len > 5 && memcmp(text, "MICS ", 5) == 0) {
        char buf[16];
        size_t n = len - 5;
        if (n >= sizeof(buf)) n = sizeof(buf) - 1;
        memcpy(buf, text + 5, n);
        buf[n] = '\0';
        char *end = NULL;
        long bg = strtol(buf, &end, 10);
        if (end != buf && bg >= -10000 && bg <= 0) {
            board_audio_mic_sleep((int16_t)bg);
            ESP_LOGI(TAG, "MICS: sleep trigger bg=%ld", bg);
        } else {
            ESP_LOGW(TAG, "Invalid MICS bg: %.*s", (int)n, buf);
        }
    } else if (len == 4 && memcmp(text, "MICW", 4) == 0) {
        board_audio_mic_wake();
        ESP_LOGI(TAG, "MICW: wake, continuous upload");
    } else {
        ESP_LOGW(TAG, "Ignoring unknown server text command");
    }
}

/**
 * @brief WSS 下行二进制（服务端 PCM）→ 板级扬声器（融合方案 §9.4）。
 * 只在已 SPKS 开始播放时写入；未开始直接丢弃（§9.6 保护）。
 */
static void voice_service_on_binary(const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0U || (len & 1U) != 0U) {
        return;
    }
    if (!board_audio_speaker_is_playing()) {
        ESP_LOGW(TAG, "Dropping %u-byte downlink PCM: speaker not started", (unsigned)len);
        return;
    }
    esp_err_t err = board_audio_speaker_write(data, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Downlink PCM write failed: %s", esp_err_to_name(err));
    }
}

/**
 * @brief 处理命令队列中的一条作业（wss_transport on_queue_item 回调）。
 *
 * 在会话任务上下文中执行；FILE_SEND 直接推送文件，MIC 块仅在流式状态
 * 开启且会话有效时发送。
 *
 * @param[in] item      队列条目，不允许为 NULL。
 * @param[in] item_size 条目大小，必须等于 sizeof(voice_job_t)。
 */
static void voice_service_on_queue_item(void *item, size_t item_size)
{
    if (item == NULL || item_size != sizeof(voice_job_t)) {
        ESP_LOGW(TAG, "Malformed queued voice job");
        return;
    }
    voice_job_t *job = (voice_job_t *)item;
    switch (job->type) {
    case VOICE_JOB_SEND_FILE:
        job->data[VOICE_SERVICE_URI_MAX_LEN - 1] = '\0';
        (void)voice_service_push_file((const char *)job->data);
        break;
    case VOICE_JOB_MIC_START:
        s_mic_active = true;
        /* 打开板级 WSS 上行（mic_task 才开始组 PCM1 帧，见融合方案 §9.3）。 */
        board_audio_enable_wss_mic(true);
        ESP_LOGI(TAG, "MIC streaming enabled");
        break;
    case VOICE_JOB_MIC_STOP:
        s_mic_active = false;
        board_audio_enable_wss_mic(false);
        ESP_LOGI(TAG, "MIC streaming disabled");
        break;
    case VOICE_JOB_SEND_CHUNK:
        if (!s_mic_active) {
            ESP_LOGW(TAG, "Dropping %u-byte MIC chunk: streaming is not active",
                     (unsigned)job->len);
        } else if (wss_transport_send_now(0x2, job->data, job->len) != ESP_OK) {
            ESP_LOGW(TAG, "Failed to send %u-byte MIC chunk", (unsigned)job->len);
        }
        break;
    default:
        ESP_LOGW(TAG, "Unknown queued voice job type %d", (int)job->type);
        break;
    }
}

/**
 * @brief WSS 会话结束回调：链路关闭后复位会话级 MIC 流式状态。
 */
static void voice_service_on_session_end(void)
{
    s_mic_active = false;
    board_audio_enable_wss_mic(false);
    (void)board_audio_speaker_stop();
}

/**
 * @brief 把一条作业复制到有界命令队列。
 *
 * @param[in] type 作业类型。
 * @param[in] data 作业数据首地址，len 为 0 时可为 NULL。
 * @param[in] len  数据长度，不超过队列块数据容量。
 * @return ESP_OK 已入队；ESP_ERR_NO_MEM 队列已满；ESP_ERR_INVALID_STATE 尚未启动。
 */
static esp_err_t voice_service_enqueue(voice_job_type_t type, const uint8_t *data, size_t len)
{
    if (len > WSS_TRANSPORT_MAX_PAYLOAD) {
        return ESP_ERR_INVALID_SIZE;
    }
    voice_job_t job;
    memset(&job, 0, sizeof(job));
    job.type = type;
    job.len = len;
    if (data != NULL && len > 0U) {
        memcpy(job.data, data, len);
    }
    return wss_transport_enqueue(&job, sizeof(job));
}

/**
 * @brief 处理一条完整重组的 MQTT 语音命令（通信层注册表回调）。
 *
 * 语音命令是纯文本行：FILE_SEND <uri>、MIC_START、MIC_STOP。命令只入队，
 * 不在此处访问网络，也不影响 OTA 状态。
 *
 * @param[in] cmd     NUL 结尾的命令文本，不允许为 NULL。
 * @param[in] cmd_len 命令有效长度，范围为 1～VOICE_SERVICE_CMD_MAX_LEN。
 */
static void voice_service_on_mqtt_command(const char *cmd, size_t cmd_len)
{
    if (cmd == NULL || cmd_len == 0U || cmd_len > VOICE_SERVICE_CMD_MAX_LEN) {
        ESP_LOGW(TAG, "Ignoring oversized or empty voice command");
        return;
    }
    /* 去掉发布端可能附加的换行与空白；通信层尾部 NUL 保证 cmd 可读。 */
    while (cmd_len > 0U && (cmd[cmd_len - 1] == '\r' || cmd[cmd_len - 1] == '\n' ||
                            cmd[cmd_len - 1] == ' ' || cmd[cmd_len - 1] == '\t')) {
        cmd_len--;
    }
    if (cmd_len == 0U) {
        ESP_LOGW(TAG, "Ignoring blank voice command");
        return;
    }
    ESP_LOGI(TAG, "Voice command: %.*s", (int)cmd_len, cmd);

    if (cmd_len > strlen("FILE_SEND ") && strncmp(cmd, "FILE_SEND ", 10) == 0) {
        /* 复制到独立缓冲区保证 NUL 结尾，供 WSS 会话任务异步使用。 */
        char uri[VOICE_SERVICE_CMD_MAX_LEN];
        size_t uri_len = cmd_len - 10;
        if (uri_len >= sizeof(uri)) {
            ESP_LOGW(TAG, "Voice FILE_SEND uri too long");
            return;
        }
        memcpy(uri, cmd + 10, uri_len);
        uri[uri_len] = '\0';
        esp_err_t err = voice_service_send_file(uri);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Voice FILE_SEND rejected: %s", esp_err_to_name(err));
        }
    } else if (cmd_len == strlen("MIC_START") && memcmp(cmd, "MIC_START", cmd_len) == 0) {
        esp_err_t err = voice_service_mic_start();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Voice MIC_START rejected: %s", esp_err_to_name(err));
        }
    } else if (cmd_len == strlen("MIC_STOP") && memcmp(cmd, "MIC_STOP", cmd_len) == 0) {
        esp_err_t err = voice_service_mic_stop();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Voice MIC_STOP rejected: %s", esp_err_to_name(err));
        }
    } else {
        ESP_LOGW(TAG, "Ignoring unknown voice command: %.*s", (int)cmd_len, cmd);
    }
}

/* ------------------------------------------------------------------ */
/* 公共接口                                                            */
/* ------------------------------------------------------------------ */

esp_err_t voice_service_init(void)
{
    /* 语音 topic 非 critical：语音订阅失败不影响 OTA 连接就绪判定。 */
    return mqtt_comm_register_topic(CONFIG_COMM_MQTT_VOICE_CMD_TOPIC,
                                    VOICE_SERVICE_CMD_MAX_LEN, false,
                                    voice_service_on_mqtt_command);
}

esp_err_t voice_service_init_board_audio(void)
{
    /* 板级 MIC：把 PCM1 帧（16B 头 + PCM）路由到 WSS binary（voice_service_send_chunk）。
     * 未启动 MIC_START 前 mic_task 不组帧，无需其他门控。 */
    ESP_RETURN_ON_ERROR(board_audio_set_wss_sink(voice_service_on_board_audio_frame, NULL),
                        TAG, "set WSS sink");
    ESP_LOGI(TAG, "board audio wired: PCM1 uplink -> WSS, SPKS/SPKE downlink -> speaker");
    return ESP_OK;
}

esp_err_t voice_service_ip_ready(void *arg)
{
    (void)arg;
    static const wss_transport_config_t transport_cfg = {
        .on_text = voice_service_on_server_text,
        .on_binary = voice_service_on_binary,
        .on_queue_item = voice_service_on_queue_item,
        .on_session_end = voice_service_on_session_end,
        .queue_item_size = sizeof(voice_job_t),
        .queue_depth = VOICE_QUEUE_DEPTH,
    };
    return wss_transport_start(&transport_cfg);
}

esp_err_t voice_service_send_file(const char *uri)
{
    if (uri == NULL || uri[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    size_t len = strlen(uri);
    if (len >= VOICE_SERVICE_URI_MAX_LEN) {
        return ESP_ERR_INVALID_SIZE;
    }
    /* 连同末尾 NUL 一起入队，会话任务侧可直接当作字符串使用。 */
    return voice_service_enqueue(VOICE_JOB_SEND_FILE, (const uint8_t *)uri, len + 1U);
}

esp_err_t voice_service_send_chunk(const uint8_t *buf, size_t len)
{
    if (buf == NULL || len == 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len > WSS_TRANSPORT_MAX_PAYLOAD) {
        return ESP_ERR_INVALID_SIZE;
    }
    return voice_service_enqueue(VOICE_JOB_SEND_CHUNK, buf, len);
}

esp_err_t voice_service_mic_start(void)
{
    return voice_service_enqueue(VOICE_JOB_MIC_START, NULL, 0);
}

esp_err_t voice_service_mic_stop(void)
{
    return voice_service_enqueue(VOICE_JOB_MIC_STOP, NULL, 0);
}
