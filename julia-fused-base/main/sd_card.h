/**
 * @file    sd_card.h
 * @brief   SD 卡（FAT）挂载，供 voice_service 的 "SD:/<name>" 文件推送使用。
 *
 * 挂载点为 /sdcard，与 voice_uri.c 中 "SD:/x" -> "/sdcard/x" 的映射一致。
 *
 * 板级接法（Waveshare ESP32-S3-LCD-1.85，与 VOICE DATA BENCHMARK 参考工程
 * 一致，已在目标板验证可写入）：
 * - SDMMC 1-bit：CLK=IO14、CMD=IO17、D0=IO16；
 * - D3/CS 经 TCA9554 P2（Extend_IO3）控制，初始化时先拉高，卡保持 SD 模式；
 * - TCA9554 在 I2C_NUM_0：SCL=IO10、SDA=IO11，地址 0x20，400 kHz。
 *
 * 所有引脚都可通过 Kconfig（SD_CARD_PIN_* / SD_CARD_I2C_*）调整。
 */
#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 挂载 SD 卡（同步执行一次；不依赖网络，可在启动早期调用）。
 *
 * 先初始化 TCA9554 并把 CS 拉高，再以 SDMMC 1-bit 模式挂载 FAT 到 /sdcard。
 * 缺卡或挂载失败不阻断应用：返回错误码，调用方仅记录日志；之后 voice_service
 * 的 FILE_SEND 会以 ERROR file_open_failed 呈现。
 *
 * @return ESP_OK 已挂载。
 * @return ESP_ERR_NOT_SUPPORTED SD 卡功能未启用（CONFIG_SD_CARD_ENABLE=n）。
 * @return 其他 esp_err_t 扩展器初始化或挂载失败。
 */
esp_err_t sd_card_start(void);

/**
 * @brief 查询 SD 卡是否已成功挂载。
 *
 * @return true 已挂载（/sdcard 可用）；false 未挂载或功能未启用。
 */
bool sd_card_is_mounted(void);

#ifdef __cplusplus
}
#endif
