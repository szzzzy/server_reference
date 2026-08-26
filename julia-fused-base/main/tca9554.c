/**
 * @file    tca9554.c
 * @brief   TCA9554 I2C GPIO 扩展器驱动（板载 Extend IO 控制）。
 *
 * I2C 引脚：SCL=GPIO10、SDA=GPIO11（I2C_NUM_0，400 kHz，内部上拉）。
 * 驱动逻辑与 VOICE DATA BENCHMARK/components/board_hal/tca9554.c 一致
 * （已在目标板验证），仅补充头注释。
 */

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_check.h"
#include "esp_log.h"

#include "tca9554.h"

static const char *TAG = "tca9554";

/* TCA9554 寄存器映射 */
#define TCA9554_REG_INPUT     0x00
#define TCA9554_REG_OUTPUT    0x01
#define TCA9554_REG_POLARITY  0x02
#define TCA9554_REG_CONFIG    0x03

#define TCA9554_I2C_SCL GPIO_NUM_10
#define TCA9554_I2C_SDA GPIO_NUM_11

static i2c_master_bus_handle_t s_bus = NULL;
static i2c_master_dev_handle_t s_dev = NULL;
static SemaphoreHandle_t s_lock = NULL;

static esp_err_t read_reg(uint8_t reg, uint8_t *val)
{
    return i2c_master_transmit_receive(s_dev, &reg, 1, val, 1, 100);
}

static esp_err_t write_reg(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = { reg, val };
    return i2c_master_transmit(s_dev, buf, 2, 100);
}

esp_err_t tca9554_init(void)
{
    if (s_dev) {
        return ESP_OK; /* 幂等：已初始化 */
    }
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = TCA9554_I2C_SDA,
        .scl_io_num = TCA9554_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus_cfg, &s_bus), TAG, "i2c bus init failed");

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = TCA9554_ADDR,
        .scl_speed_hz = 400000,
    };
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(s_bus, &dev_cfg, &s_dev), TAG,
                        "add device failed");

    s_lock = xSemaphoreCreateMutex();
    ESP_LOGI(TAG, "ready at 0x%02x (scl=%d, sda=%d)", TCA9554_ADDR, TCA9554_I2C_SCL,
             TCA9554_I2C_SDA);
    return ESP_OK;
}

esp_err_t tca9554_write_pin(uint8_t pin, bool level)
{
    if (pin > 7 || !s_dev) {
        return ESP_ERR_INVALID_ARG;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    uint8_t cfg = 0, out = 0;
    esp_err_t err = read_reg(TCA9554_REG_CONFIG, &cfg);
    if (err == ESP_OK) {
        err = read_reg(TCA9554_REG_OUTPUT, &out);
    }
    if (err == ESP_OK) {
        /* 先写输出锁存、再切方向：使能瞬间引脚不会输出意外电平。 */
        if (level) {
            out |= (uint8_t)(1u << pin);
        } else {
            out &= (uint8_t)~(1u << pin);
        }
        err = write_reg(TCA9554_REG_OUTPUT, out);
        if (err == ESP_OK) {
            cfg &= (uint8_t)~(1u << pin); /* 方向：输出 */
            err = write_reg(TCA9554_REG_CONFIG, cfg);
        }
    }
    xSemaphoreGive(s_lock);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "write pin %u failed: %s", pin, esp_err_to_name(err));
    }
    return err;
}

esp_err_t tca9554_read_pin(uint8_t pin, bool *level)
{
    if (pin > 7 || !s_dev || !level) {
        return ESP_ERR_INVALID_ARG;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    uint8_t in = 0;
    esp_err_t err = read_reg(TCA9554_REG_INPUT, &in);
    xSemaphoreGive(s_lock);
    if (err == ESP_OK) {
        *level = ((in >> pin) & 1u) != 0;
    }
    return err;
}
