#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 初始化本地唤醒词检测（WakeNet "你好小智"，wn9_nihaoxiaozhi_tts）。
 *
 * 职责：从板级 mic_task（经 board_audio_set_afe_sink 挂接）取 20 ms PCM 帧喂给
 * AFE，AFE 在设备上本地推理；检测到唤醒词后回调 on_wake（默认实现：调用
 * voice_service_mic_start() 开启 WSS MIC 推流）。
 *
 * @note 必须在 board_audio_init() 与 voice_service_init() 之后调用。
 * @note 依赖 "model" 分区已烧录（构建时 esp-sr 自动打包 srmodels.bin）。
 */
esp_err_t wake_detector_init(void);

/** 返回检测器是否就绪。 */
bool wake_detector_is_ready(void);

#ifdef __cplusplus
}
#endif
