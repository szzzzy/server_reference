/**
 * @file    audio_service.c
 * @brief   音频服务层实现。
 *
 * 模块关系：
 * - 从 mqtt_comm.c 接收一条已完成 MQTT 分片重组的 audio_check_response；
 * - 使用 audio_control_plane 解析校验，得到 native_audio_manifest_t；
 * - 与固件 OTA 下载互斥：OTA 下载进行中时拒绝启动音频下载（OTA 优先），
 *   音频下载自身也拒绝并发；
 * - 将下载请求交给 audio_engine。
 */
#include "audio_service.h"

#include <stdbool.h>

#include "esp_log.h"

#include "audio_control_plane.h"
#include "audio_engine.h"
#include "ota_engine.h"
#include "ota_types.h"

/** 本模块统一使用的日志标签。 */
static const char *TAG = "audio_service";

esp_err_t audio_service_init(void)
{
    /* 当前服务层没有自有状态；保留初始化点与 OTA 服务层保持一致。 */
    return ESP_OK;
}

esp_err_t audio_service_handle_response(const char *json, size_t json_len)
{
    if (json == NULL || json_len == 0U || json_len > NATIVE_OTA_JSON_MAX_LEN) {
        return ESP_ERR_INVALID_ARG;
    }

    native_audio_manifest_t manifest;
    bool download_requested = false;
    esp_err_t err = audio_control_plane_parse_audio_response(json, json_len,
                                                             &manifest,
                                                             &download_requested);
    if (err != ESP_OK) {
        return err;
    }
    if (!download_requested) {
        return ESP_OK;
    }

    /* 固件 OTA 与音频下载共用网络/Flash 资源，v1 策略：OTA 优先，互斥执行。 */
    if (ota_engine_is_running() || audio_engine_is_running()) {
        ESP_LOGW(TAG, "Audio download rejected: OTA or another audio download is running");
        return ESP_ERR_INVALID_STATE;
    }

    return audio_engine_start(&manifest);
}
