/*
 * board_audio.c - 板级音频组件（来自最小包 mic_test.c 抽取，融合方案 §8）
 *
 * 保留（§8.1）：
 *   mic_init / speaker_init / scale_sample / mic_dbfs_x100
 *   mic_task 的 I2S 读取、PCM 转换、休眠/预录
 *   speaker 的 start/volume/data/end 核心
 *   playing 半双工标志、PCM1、SPKS/SPKV/SPKD/SPKE/MICS/MICW 语义
 *
 * 不保留（§8.2）：
 *   app_main、esp_log_level_set("*", ESP_LOG_NONE)、usb_init/usb_write_all/
 *   usb_read_all、USB 命令无限读取循环
 *   （由本组件的 C API 与 WSS 上行/下行层替换）
 */

#include "board_audio.h"

#include <math.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define TAG "board_audio"

#define MIC_BCLK GPIO_NUM_15
#define MIC_WS GPIO_NUM_2
#define MIC_DIN GPIO_NUM_39
#define SPK_BCLK GPIO_NUM_48
#define SPK_WS GPIO_NUM_38
#define SPK_DOUT GPIO_NUM_47
#define MIC_SAMPLES 320
#define MAX_SPK_BYTES 4096
#define SLEEP_PREROLL_FRAMES 25
#define SLEEP_TRIGGER_FRAMES 6
#define SLEEP_THRESHOLD_DELTA_X100 500

static i2s_chan_handle_t mic_rx, spk_tx;
static volatile bool playing;
static volatile TickType_t last_speaker_activity;
static volatile uint32_t speaker_volume = 100;
static int32_t mic_raw[MIC_SAMPLES];
static int16_t mic_pcm[MIC_SAMPLES];
static int16_t sleep_preroll[SLEEP_PREROLL_FRAMES][MIC_SAMPLES];
static volatile bool mic_sleeping;
static volatile bool mic_wake_triggered;
static volatile int16_t sleep_background_dbfs_x100 = -6000;
static size_t sleep_preroll_write;
static size_t sleep_preroll_count;
static uint32_t sleep_active_frames;
static int16_t spk_stereo[MAX_SPK_BYTES];

/* Speaker 串行化：start/write/stop/self_test 整体持锁，
 * 防止 WSS 下行、本地 TTS、文件播放交叉配置/交叉写 PCM。 */
static SemaphoreHandle_t s_spk_lock;

/* PCM1 帧缓冲：16 B 头 + MIC_SAMPLES*2 B PCM = 656 B（WSS 单个 binary）。 */
static uint8_t s_pcm1_frame[16 + MIC_SAMPLES * 2];
static audio_pcm_sink_t s_afe_sink;
static void *s_afe_ctx;
static audio_frame_sink_t s_wss_sink;
static void *s_wss_ctx;
static volatile bool s_wss_mic_enabled;

static uint8_t sum8(const uint8_t *p, size_t n)
{
    uint32_t s = 0;
    while (n--) s += *p++;
    return (uint8_t)s;
}

static esp_err_t mic_init(void)
{
    i2s_chan_config_t c = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_RETURN_ON_ERROR(i2s_new_channel(&c, NULL, &mic_rx), TAG, "new MIC channel");
    i2s_std_config_t s = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                       I2S_SLOT_MODE_MONO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = MIC_BCLK, .ws = MIC_WS,
                     .dout = I2S_GPIO_UNUSED, .din = MIC_DIN},
    };
    s.slot_cfg.slot_mask = I2S_STD_SLOT_RIGHT;
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(mic_rx, &s), TAG, "init MIC std mode");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(mic_rx), TAG, "enable MIC");
    return ESP_OK;
}

static esp_err_t speaker_init(uint32_t rate)
{
    if (spk_tx != NULL) {
        i2s_std_clk_config_t clk = I2S_STD_CLK_DEFAULT_CONFIG(rate);
        ESP_RETURN_ON_ERROR(i2s_channel_disable(spk_tx), TAG, "disable SPK");
        ESP_RETURN_ON_ERROR(i2s_channel_reconfig_std_clock(spk_tx, &clk), TAG, "reconfig SPK clock");
        ESP_RETURN_ON_ERROR(i2s_channel_enable(spk_tx), TAG, "enable SPK");
        return ESP_OK;
    }
    i2s_chan_config_t c = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    c.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&c, &spk_tx, NULL), TAG, "new SPK channel");
    i2s_std_config_t s = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(rate),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                       I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = SPK_BCLK, .ws = SPK_WS,
                     .dout = SPK_DOUT, .din = I2S_GPIO_UNUSED},
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(spk_tx, &s), TAG, "init SPK std mode");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(spk_tx), TAG, "enable SPK");
    return ESP_OK;
}

static int16_t scale_sample(int16_t sample)
{
    int32_t value = (int32_t)sample * (int32_t)speaker_volume / 100;
    if (value > 32767) value = 32767;
    if (value < -32768) value = -32768;
    return (int16_t)value;
}

static int16_t mic_dbfs_x100(const int16_t *x, size_t count)
{
    double ss = 0;
    for (size_t i = 0; i < count; i++) {
        double v = x[i] / 32768.0;
        ss += v * v;
    }
    return (int16_t)lrint(2000 * log10(sqrt(ss / count) + 1e-12));
}

/* WSS 上行：组 16 B PCM1 头 + PCM，整帧交给 WSS sink（取代原 usb_write_all）。 */
static void send_pcm1(uint32_t seq, const int16_t *x, size_t count)
{
    uint16_t bytes = (uint16_t)(count * 2);
    int16_t db = mic_dbfs_x100(x, count);
    uint8_t h[16] = {'P', 'C', 'M', '1'};
    h[4] = (uint8_t)seq; h[5] = (uint8_t)(seq >> 8);
    h[6] = (uint8_t)(seq >> 16); h[7] = (uint8_t)(seq >> 24);
    h[8] = (uint8_t)bytes; h[9] = (uint8_t)(bytes >> 8);
    h[10] = (uint8_t)db; h[11] = (uint8_t)(((uint16_t)db) >> 8);
    h[15] = sum8((const uint8_t *)x, bytes);
    memcpy(s_pcm1_frame, h, sizeof(h));
    memcpy(s_pcm1_frame + sizeof(h), x, bytes);
    if (s_wss_sink != NULL) {
        s_wss_sink(s_pcm1_frame, sizeof(h) + bytes, s_wss_ctx);
    }
}

static void mic_task(void *arg)
{
    uint32_t seq = 0;
    while (1) {
        size_t got = 0;
        if (i2s_channel_read(mic_rx, mic_raw, sizeof(mic_raw), &got, portMAX_DELAY) != ESP_OK ||
            !got) {
            continue;
        }
        /* 半双工：Speaker 播放中暂停整条 MIC 处理（750 ms 活动兜底），与最小包一致。 */
        if (playing) {
            if ((int32_t)(xTaskGetTickCount() - last_speaker_activity) >
                pdMS_TO_TICKS(750)) {
                playing = false;
            } else {
                continue;
            }
        }
        size_t count = got / 4;
        if (count > MIC_SAMPLES) count = MIC_SAMPLES;
        for (size_t i = 0; i < count; i++) {
            int32_t v = mic_raw[i] >> 14;
            if (v > 32767) v = 32767;
            if (v < -32768) v = -32768;
            mic_pcm[i] = (int16_t)v;
        }
        /* fanout 路径 1：AFE（Julia 业务状态通过 sink 侧决定是否使用）。 */
        if (s_afe_sink != NULL) {
            s_afe_sink(mic_pcm, count, s_afe_ctx);
        }
        /* fanout 路径 2：WSS 上行（MIC_START 才启用；MICS/MICW 控制休眠预录）。 */
        if (!s_wss_mic_enabled) {
            continue;
        }
        if (!mic_sleeping) {
            send_pcm1(seq++, mic_pcm, count);
            continue;
        }

        memcpy(sleep_preroll[sleep_preroll_write], mic_pcm, count * sizeof(int16_t));
        if (count < MIC_SAMPLES) {
            memset(&sleep_preroll[sleep_preroll_write][count], 0,
                   (MIC_SAMPLES - count) * sizeof(int16_t));
        }
        sleep_preroll_write = (sleep_preroll_write + 1) % SLEEP_PREROLL_FRAMES;
        if (sleep_preroll_count < SLEEP_PREROLL_FRAMES) sleep_preroll_count++;

        if (!mic_wake_triggered) {
            int16_t db = mic_dbfs_x100(mic_pcm, count);
            int16_t threshold = sleep_background_dbfs_x100 + SLEEP_THRESHOLD_DELTA_X100;
            if (db > threshold) {
                sleep_active_frames++;
            } else {
                sleep_active_frames = 0;
            }
            if (sleep_active_frames >= SLEEP_TRIGGER_FRAMES) {
                mic_wake_triggered = true;
                size_t start = (sleep_preroll_write + SLEEP_PREROLL_FRAMES -
                                sleep_preroll_count) % SLEEP_PREROLL_FRAMES;
                for (size_t i = 0; i < sleep_preroll_count; i++) {
                    send_pcm1(seq++, sleep_preroll[(start + i) % SLEEP_PREROLL_FRAMES],
                              MIC_SAMPLES);
                }
            }
        } else {
            send_pcm1(seq++, mic_pcm, count);
        }
    }
}

static void speaker_task(void *arg)
{
    /* 原最小包通过 USB 等待命令。融合后改为 API 驱动（board_audio_speaker_*），
     * 任务保留用于独占 core0-p6 上的 I2S 资源与 750 ms 兜底心跳。 */
    (void)arg;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(250));
        if (playing &&
            (int32_t)(xTaskGetTickCount() - last_speaker_activity) > pdMS_TO_TICKS(750)) {
            playing = false;
        }
    }
}

esp_err_t board_audio_init(void)
{
    ESP_RETURN_ON_ERROR(mic_init(), TAG, "mic init");
    ESP_RETURN_ON_ERROR(speaker_init(24000), TAG, "speaker init");
    if (s_spk_lock == NULL) {
        s_spk_lock = xSemaphoreCreateMutex();
        if (s_spk_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (xTaskCreatePinnedToCore(mic_task, "board_mic", 4096, NULL, 5, NULL, 1) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreatePinnedToCore(speaker_task, "board_spk", 3072, NULL, 6, NULL, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "ready mic=I2S0(15/2/39) spk=I2S1(48/38/47)");
    return ESP_OK;
}

esp_err_t board_audio_set_afe_sink(audio_pcm_sink_t sink, void *ctx)
{
    s_afe_sink = sink;
    s_afe_ctx = ctx;
    return ESP_OK;
}

esp_err_t board_audio_set_wss_sink(audio_frame_sink_t sink, void *ctx)
{
    s_wss_sink = sink;
    s_wss_ctx = ctx;
    return ESP_OK;
}

void board_audio_enable_wss_mic(bool enabled)
{
    s_wss_mic_enabled = enabled;
    if (!enabled) {
        /* 关闭上行：清触发/预录状态，防止重开后发送陈旧预录（等价 MICW 复位）。 */
        mic_wake_triggered = false;
        sleep_active_frames = 0;
        sleep_preroll_write = 0;
        sleep_preroll_count = 0;
    }
}

void board_audio_mic_sleep(int16_t background_dbfs_x100)
{
    sleep_background_dbfs_x100 = background_dbfs_x100;
    mic_sleeping = true;
    mic_wake_triggered = false;
    sleep_active_frames = 0;
    sleep_preroll_write = 0;
    sleep_preroll_count = 0;
}

void board_audio_mic_wake(void)
{
    mic_sleeping = false;
    mic_wake_triggered = false;
    sleep_active_frames = 0;
}

static esp_err_t board_audio_speaker_stop_locked(void);

esp_err_t board_audio_speaker_start(uint32_t sample_rate)
{
    uint32_t rate = sample_rate ? sample_rate : 24000;
    esp_err_t err = ESP_OK;
    if (s_spk_lock != NULL && xSemaphoreTake(s_spk_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    /* 串行化：若已有其他源在播放，先停止旧源，新源接管（后开优先）。 */
    if (playing) {
        (void)board_audio_speaker_stop_locked();
    }
    err = speaker_init(rate);
    if (err == ESP_OK) {
        playing = true;
        last_speaker_activity = xTaskGetTickCount();
    }
    if (s_spk_lock != NULL) xSemaphoreGive(s_spk_lock);
    return err;
}

static esp_err_t board_audio_speaker_stop_locked(void)
{
    if (!playing) return ESP_OK;
    int16_t z[256] = {0};
    size_t w = 0;
    (void)i2s_channel_write(spk_tx, z, sizeof(z), &w, portMAX_DELAY);
    vTaskDelay(pdMS_TO_TICKS(50));
    playing = false;
    last_speaker_activity = xTaskGetTickCount();
    return ESP_OK;
}

esp_err_t board_audio_speaker_write(const uint8_t *mono_pcm, size_t bytes)
{
    if (mono_pcm == NULL || bytes == 0 || bytes > MAX_SPK_BYTES || (bytes & 1)) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ESP_OK;
    if (s_spk_lock != NULL && xSemaphoreTake(s_spk_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    if (!playing) {
        err = ESP_ERR_INVALID_STATE;
    } else {
        last_speaker_activity = xTaskGetTickCount();
        const int16_t *m = (const int16_t *)mono_pcm;
        size_t count = bytes / 2;
        for (size_t i = 0; i < count; i++) {
            int16_t value = scale_sample(m[i]);
            spk_stereo[2 * i] = value;
            spk_stereo[2 * i + 1] = value;
        }
        size_t w = 0;
        if (i2s_channel_write(spk_tx, spk_stereo, count * 4, &w, portMAX_DELAY) != ESP_OK) {
            err = ESP_FAIL;
        } else {
            last_speaker_activity = xTaskGetTickCount();
        }
    }
    if (s_spk_lock != NULL) xSemaphoreGive(s_spk_lock);
    return err;
}

esp_err_t board_audio_speaker_stop(void)
{
    esp_err_t err = ESP_OK;
    if (s_spk_lock != NULL && xSemaphoreTake(s_spk_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    err = board_audio_speaker_stop_locked();
    if (s_spk_lock != NULL) xSemaphoreGive(s_spk_lock);
    return err;
}

void board_audio_speaker_set_volume(uint8_t percent)
{
    speaker_volume = percent > 100 ? 100 : percent;
}

bool board_audio_speaker_is_playing(void)
{
    return playing;
}

esp_err_t board_audio_speaker_self_test(void)
{
    const uint32_t rate = 24000;
    const int frequencies[3] = {440, 660, 880};
    esp_err_t err = ESP_OK;
    if (s_spk_lock != NULL && xSemaphoreTake(s_spk_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    err = speaker_init(rate);
    playing = true;
    last_speaker_activity = xTaskGetTickCount();
    for (int tone = 0; err == ESP_OK && tone < 3; tone++) {
        for (int block = 0; err == ESP_OK && block < 12; block++) {
            for (int i = 0; i < 512; i++) {
                double phase = 2.0 * M_PI * frequencies[tone] * (block * 512 + i) / rate;
                int16_t value = scale_sample((int16_t)(sin(phase) * 26000.0));
                spk_stereo[2 * i] = value;
                spk_stereo[2 * i + 1] = value;
            }
            size_t written = 0;
            if (i2s_channel_write(spk_tx, spk_stereo, 512 * 4, &written, portMAX_DELAY) != ESP_OK)
                err = ESP_FAIL;
        }
        int16_t silence[1024] = {0};
        size_t written = 0;
        if (i2s_channel_write(spk_tx, silence, sizeof(silence), &written, portMAX_DELAY) != ESP_OK)
            err = ESP_FAIL;
    }
    if (err == ESP_OK) err = board_audio_speaker_stop_locked();
    else playing = false;
    if (s_spk_lock != NULL) xSemaphoreGive(s_spk_lock);
    return err;
}
