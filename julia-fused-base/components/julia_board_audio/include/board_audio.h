#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 板级音频组件 —— 来自最小包 `mic_test.c` 抽取（融合方案 §8）。
 *
 * 保留（§8.1）：mic_init / speaker_init / scale_sample / mic_dbfs_x100、
 * mic_task 的 I2S 读取、PCM 转换、休眠/预录、speaker 的 start/volume/data/end、
 * playing 半双工标志、PCM1、SPKS/SPKV/SPKD/SPKE/MICS/MICW 语义。
 * 不保留（§8.2）：app_main、esp_log_level_set("*", ESP_LOG_NONE)、usb_init/
 * usb_write_all/usb_read_all、USB 命令无限读取循环 —— 由本组件的 C API 与
 * WSS 上行/下行（voice_service）替换。
 */

/** 一个 20 ms / 320 sample mono PCM16 帧（用于 AFE/WakeNet 路径）。 */
typedef void (*audio_pcm_sink_t)(const int16_t *pcm, size_t samples, void *ctx);

/** 一个完整 WSS binary 载荷（16 B `PCM1` 头 + PCM 正文，通常 656 B）。 */
typedef void (*audio_frame_sink_t)(const uint8_t *frame, size_t bytes, void *ctx);

/** 初始化 I2S0 MIC / I2S1 Speaker 并创建 mic_task/core1-p5 与 speaker_task/core0-p6。 */
esp_err_t board_audio_init(void);

/* ---- MIC 上行 fanout（§8.4） ---- */

/** 设置 AFE sink；每个 20 ms 帧回调一次，MICS/MICW 不影响 AFE 路径。 */
esp_err_t board_audio_set_afe_sink(audio_pcm_sink_t sink, void *ctx);

/** 设置 WSS sink（PCM1 帧）；MIC_START/MIC_STOP 通过 enable_wss_mic 控制。 */
esp_err_t board_audio_set_wss_sink(audio_frame_sink_t sink, void *ctx);

/** 打开/关闭 WSS 上行。关闭时复位触发/预录状态，AFE sink 不受影响。 */
void board_audio_enable_wss_mic(bool enabled);

/** 进入声音触发上传模式（背景 dBFS x100，触发阈值为背景 +5 dB）。 */
void board_audio_mic_sleep(int16_t background_dbfs_x100);

/** 退出休眠，恢复持续上传。 */
void board_audio_mic_wake(void);

/* ---- Speaker 下行（§8.3 / §9.4） ---- */

/** 开始播放（rate=0 时默认 24000），并置 playing。 */
esp_err_t board_audio_speaker_start(uint32_t sample_rate);

/** 写入 mono PCM16（长度非零、偶数、<= 4096 B），自动复制左右声道。 */
esp_err_t board_audio_speaker_write(const uint8_t *mono_pcm, size_t bytes);

/** 停止播放：写静音、清 playing。 */
esp_err_t board_audio_speaker_stop(void);

/** 音量 0-100（越界钳位）。 */
void board_audio_speaker_set_volume(uint8_t percent);

/** 当前是否处于播放（半双工标志）。 */
bool board_audio_speaker_is_playing(void);

/** 扬声器本地自检（440/660/880 Hz 三音）。 */
esp_err_t board_audio_speaker_self_test(void);

#ifdef __cplusplus
}
#endif
