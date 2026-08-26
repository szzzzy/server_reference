/**
 * @file    tca9554.h
 * @brief   TCA9554 I2C GPIO 扩展器驱动（板载 Extend IO 控制）。
 *
 * 本板（Waveshare ESP32-S3-LCD-1.85）的 SD 卡 D3/CS 信号经 TCA9554 的 P2
 * 引脚（Extend_IO3）路由，因此操作 SD 卡前必须先初始化扩展器并保持 CS 为高，
 * 使卡始终处于 SDMMC(SD) 模式而不是误入 SPI 模式。
 *
 * 来源：VOICE DATA BENCHMARK/components/board_hal/tca9554.c（已在目标板验证）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/** TCA9554 的 7 位 I2C 从机地址。 */
#define TCA9554_ADDR 0x20

/** P2 = Extend_IO3：SD 卡 D3/CS。SDMMC(SD) 模式下必须保持为高。 */
#define TCA9554_PIN_SD_CS 2

/**
 * @brief 初始化 I2C 主总线并添加 TCA9554 设备（幂等）。
 *
 * @return ESP_OK 就绪；其他 esp_err_t 总线或设备初始化失败。
 */
esp_err_t tca9554_init(void);

/**
 * @brief 把引脚配置为推挽输出并设置电平。
 *
 * 先写输出锁存、再切方向，保证使能瞬间引脚不会出现意外电平。
 *
 * @param[in] pin   引脚号 0～7。
 * @param[in] level true 高电平；false 低电平。
 * @return ESP_OK 成功；ESP_ERR_INVALID_ARG 引脚号非法或设备未初始化。
 */
esp_err_t tca9554_write_pin(uint8_t pin, bool level);

/**
 * @brief 读取引脚电平（输入或输出均可读）。
 *
 * @param[in]  pin   引脚号 0～7。
 * @param[out] level 输出当前电平。
 * @return ESP_OK 成功；ESP_ERR_INVALID_ARG 参数非法。
 */
esp_err_t tca9554_read_pin(uint8_t pin, bool *level);
