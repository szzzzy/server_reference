/*
 * wake_detector.c - 本地唤醒词检测（WakeNet "你好小智"，wn9_nihaoxiaozhi_tts）
 *
 * 来源：fused 工程 julia_voice.c 的 AFE/WakeNet 初始化与 feed/detect 任务，
 * 裁剪为纯"唤醒检测"形态：
 *   - 只做本地唤醒（不接 ASR/LLM/TTS/FSM——那些在服务器侧或后续接入）；
 *   - 检测到唤醒词 → 调 voice_service_mic_start()（开麦推 PCM1 流），
 *     服务器收到后负责 ASR/LLM/TTS，回推 PCM 由 voice_service 播报。
 *
 * 与最小包/board_audio 的衔接：mic_task 的 afe_sink fanout（路径 1）在
 * board_audio_set_afe_sink() 后每 20 ms 回调一次，本模块把它喂给 AFE。
 */

#include "wake_detector.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_wn_iface.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "model_path.h"

#include "board_audio.h"
#include "voice_service.h"

#define TAG "WAKE"

/* 唤醒词模型：与 fused wake_word_config.h 一致（量产模型 wn9_nihaoxiaozhi_tts）。 */
#define WAKE_WORD_MODEL_NAME "wn9_nihaoxiaozhi_tts"
#define WAKE_WORD_DISPLAY_TEXT "你好小智"
#define MODEL_PARTITION "model"

static const esp_afe_sr_iface_t *s_afe;
static esp_afe_sr_data_t *s_afe_data;
/** AFE 要求的单通道 feed 块大小（由运行时接口查询，不能假定为 20 ms）。 */
static size_t s_feed_chunk_samples;
/** 将板级 20 ms（320 sample）帧拼成 AFE 所要求大小的缓冲区。 */
static size_t s_feed_samples;
static int16_t *s_feed_buffer;
static volatile bool s_ready;

/* AFE feed 回调（board_audio mic_task 上下文，20 ms 一帧）。 */
static void wake_afe_feed(const int16_t *pcm, size_t samples, void *ctx)
{
    (void)ctx;
    if (s_afe_data == NULL || s_feed_buffer == NULL || pcm == NULL || samples == 0) {
        return;
    }
    while (samples > 0U) {
        size_t copy = s_feed_chunk_samples - s_feed_samples;
        if (copy > samples) {
            copy = samples;
        }
        memcpy(s_feed_buffer + s_feed_samples, pcm, copy * sizeof(*pcm));
        s_feed_samples += copy;
        pcm += copy;
        samples -= copy;
        if (s_feed_samples == s_feed_chunk_samples) {
            (void)s_afe->feed(s_afe_data, s_feed_buffer);
            s_feed_samples = 0;
        }
    }
}

/* 唤醒检测任务：从 AFE fetch 结果中找 WakeNet 事件。 */
static void wake_detect_task(void *arg)
{
    (void)arg;
    while (true) {
        afe_fetch_result_t *result = s_afe->fetch(s_afe_data);
        if (result == NULL || result->ret_value == ESP_FAIL) {
            continue;
        }
        if (result->wakeup_state == WAKENET_DETECTED) {
            ESP_LOGI(TAG, "Wake word detected [%s]", WAKE_WORD_DISPLAY_TEXT);
            /* 形态 1：本地唤醒 → 自动开麦推流，服务器负责 ASR/LLM/TTS。 */
            esp_err_t err = voice_service_mic_start();
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "MIC start after wake failed: %s", esp_err_to_name(err));
            }
            /* 清除识别窗口，防止同一次唤醒的残留结果再次触发。 */
            (void)s_afe->reset_buffer(s_afe_data);
            /* 防抖：短暂静音窗口，避免同一句话连续触发。 */
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
    }
}

esp_err_t wake_detector_init(void)
{
    ESP_RETURN_ON_FALSE(CONFIG_USE_WAKENET, ESP_ERR_INVALID_STATE, TAG,
                        "CONFIG_USE_WAKENET is not enabled");
    srmodel_list_t *models = esp_srmodel_init(MODEL_PARTITION);
    ESP_RETURN_ON_FALSE(models, ESP_ERR_NOT_FOUND, TAG,
                        "speech model partition unavailable (flash 'model' partition?)");
    char *wake_model = esp_srmodel_filter(models, ESP_WN_PREFIX, WAKE_WORD_MODEL_NAME);
    ESP_RETURN_ON_FALSE(wake_model, ESP_ERR_NOT_FOUND, TAG, "WakeNet model unavailable");

    afe_config_t config = AFE_CONFIG_DEFAULT();
    config.aec_init = false; config.se_init = false;
    config.vad_init = true; config.wakenet_init = true;
    config.vad_mode = VAD_MODE_0;
    config.wakenet_model_name = wake_model;
    config.afe_ringbuf_size = 50;
    config.wakenet_mode = DET_MODE_95;
    config.pcm_config.total_ch_num = 1;
    config.pcm_config.mic_num = 1;
    config.pcm_config.ref_num = 0;
    s_afe = &ESP_AFE_SR_HANDLE;

    /* 默认配置偏向 PSRAM；当前 sdkconfig 未启用 PSRAM 时必须使用内部 RAM，
     * 否则 AFE 创建会因不可用的 MALLOC_CAP_SPIRAM 失败。 */
    if (heap_caps_get_total_size(MALLOC_CAP_SPIRAM) == 0U) {
        config.memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_INTERNAL;
        ESP_LOGW(TAG, "PSRAM is unavailable; WakeNet will allocate from internal RAM");
    }
    s_afe_data = s_afe->create_from_config(&config);
    ESP_RETURN_ON_FALSE(s_afe_data, ESP_ERR_NO_MEM, TAG, "create AFE failed");

    int channels = s_afe->get_total_channel_num(s_afe_data);
    int chunk = s_afe->get_feed_chunksize(s_afe_data);
    if (channels != 1 || chunk <= 0) {
        ESP_LOGE(TAG, "Unexpected AFE input layout: channels=%d chunk=%d", channels, chunk);
        s_afe->destroy(s_afe_data);
        s_afe_data = NULL;
        return ESP_ERR_INVALID_STATE;
    }
    s_feed_chunk_samples = (size_t)chunk;
    s_feed_buffer = heap_caps_malloc(s_feed_chunk_samples * sizeof(*s_feed_buffer),
                                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (s_feed_buffer == NULL) {
        s_afe->destroy(s_afe_data);
        s_afe_data = NULL;
        return ESP_ERR_NO_MEM;
    }

    /* 挂板级麦克风 sink：mic_task 每帧回调 wake_afe_feed。 */
    ESP_RETURN_ON_ERROR(board_audio_set_afe_sink(wake_afe_feed, NULL), TAG, "set AFE sink");

    /* AFE feed 需要 CPU 0；detect 放核心 1（与 fused 一致）。 */
    if (xTaskCreatePinnedToCore(wake_detect_task, "wake_detect", 6144, NULL, 5,
                                NULL, 1) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    s_ready = true;
    ESP_LOGI(TAG, "ready model=%s wake_word=%s afe_chunk=%u",
             wake_model, WAKE_WORD_DISPLAY_TEXT, (unsigned)s_feed_chunk_samples);
    return ESP_OK;
}

bool wake_detector_is_ready(void) { return s_ready; }
