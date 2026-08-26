/**
 * @file    ota_boot_health.h
 * @brief   OTA 首次启动验收、健康检查、确认与回滚接口。
 *
 * 此模块补充 ESP-IDF 官方 native OTA 例程的 GPIO 诊断：在确认 PENDING_VERIFY
 * 镜像前检查分区、物理 Flash、应用描述、堆和基础 FreeRTOS 队列。网络可用性
 * 不属于镜像健康条件，因此不在本模块中检查。
 */
#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief GPIO 诊断回调。
 *
 * @return true 板级诊断通过；false 表示应拒绝当前 PENDING_VERIFY 镜像。
 *
 * @note 回调由普通任务同步调用，可以访问板级 GPIO，但不能依赖网络可用性。
 */
typedef bool (*ota_boot_health_gpio_diagnostic_t)(void);

/** 读取当前运行镜像是否处于 PENDING_VERIFY。 */
esp_err_t ota_boot_health_begin(bool *pending_verify);

/** 将通过健康检查的运行镜像标记为 VALID。 */
esp_err_t ota_boot_health_confirm(void);

/** 检查回滚可行性并拒绝当前待验收镜像。 */
esp_err_t ota_boot_health_reject(const char *reason);

/**
 * @brief Product acceptance hook for a PENDING_VERIFY image.
 *
 * Product code may provide a non-weak definition of this function to initialize and
 * verify its critical local services. Return true only after those services are ready;
 * returning false prevents the image from being marked VALID and requests rollback.
 * The hook must not depend on Wi-Fi, DNS, MQTT, or other remote availability.
 *
 * The example supplies a weak default that succeeds. Test builds can enable
 * CONFIG_OTA_TEST_FORCE_BOOT_HEALTH_FAIL to force this hook to fail.
 */
bool ota_boot_health_product_check(void);

/**
 * @brief 运行不依赖网络的 OTA 本地健康检查。
 *
 * @param[in] include_gpio_diagnostic 是否执行 GPIO 诊断。
 * @param[in] gpio_diagnostic         GPIO 诊断回调；不执行诊断时可为 NULL。
 * @return true 全部本地检查通过。
 * @return false 必要资源、配置或诊断不符合要求。
 *
 * @note 若启用 GPIO 诊断，函数可阻塞；只能在普通任务上下文调用。
 * @note 检查顺序为运行/目标分区、物理 Flash、应用描述、产品配置、堆、FreeRTOS
 *       队列，最后执行可选 GPIO 诊断；不会检查 Wi-Fi、DNS 或 MQTT。
 */
bool ota_boot_health_check(bool include_gpio_diagnostic,
                           ota_boot_health_gpio_diagnostic_t gpio_diagnostic);

/**
 * @brief 进入不启动通信客户端的永久安全模式。
 *
 * @param[in] reason 用于日志的原因字符串，可为 NULL。
 *
 * @note 函数不返回、不重启、不擦除 NVS；仅保持当前任务可调度并等待人工处理或复位。
 * @note 进入安全模式后不得继续调用 mqtt_comm_start() 或其他网络启动接口。
 */
void ota_boot_health_enter_safe_mode(const char *reason) __attribute__((noreturn));

#ifdef __cplusplus
}
#endif
