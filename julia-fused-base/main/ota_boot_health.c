/**
 * @file    ota_boot_health.c
 * @brief   OTA 首次启动验收、健康检查、确认与回滚实现。
 *
 * 本文件承载项目新增的启动维稳检查。它刻意不测试 Wi-Fi 或 MQTT，
 * 使网络暂时不可用不会被误判为新镜像不健康并触发 rollback。
 *
 * 模块关系：
 * - app_main 在确认 PENDING_VERIFY 镜像前调用本模块；
 * - 本模块根据健康检查结果提供 confirm 或 rollback 状态操作；
 * - diagnostic 回调由主程序提供，具体板级 GPIO 语义不由本模块推断；
 * - 本模块不访问 NVS 或网络；确认和拒绝接口只修改 OTA 启动状态。
 *
 * 线程安全与限制：函数在调用方任务中同步执行；GPIO 回调和 Flash/堆查询可能阻塞，
 * 不能在中断上下文调用。
 */
#include "ota_boot_health.h"

#include <inttypes.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "esp_app_desc.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"

/** 本模块日志标签。 */
static const char *TAG = "ota_boot_health";

esp_err_t ota_boot_health_begin(bool *pending_verify)
{
    if (pending_verify == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    *pending_verify = false;
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        ESP_LOGE(TAG, "Cannot find the running application partition");
        return ESP_ERR_NOT_FOUND;
    }

    esp_ota_img_states_t state;
    esp_err_t err = esp_ota_get_state_partition(running, &state);
    if (err == ESP_ERR_NOT_FOUND) {
        return ESP_OK;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read OTA state: %s", esp_err_to_name(err));
        return err;
    }

    *pending_verify = (state == ESP_OTA_IMG_PENDING_VERIFY);
    ESP_LOGI(TAG, "Running image OTA state=%d, pending_verify=%s",
             state, *pending_verify ? "yes" : "no");
    return ESP_OK;
}

esp_err_t ota_boot_health_confirm(void)
{
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to confirm running image: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "Running image marked VALID; rollback cancelled");
    return ESP_OK;
}

esp_err_t ota_boot_health_reject(const char *reason)
{
    ESP_LOGE(TAG, "Rejecting pending image, reason=%s", reason != NULL ? reason : "unspecified");
    if (!esp_ota_check_rollback_is_possible()) {
        ESP_LOGE(TAG, "Rollback is unavailable: no valid previous application exists");
        return ESP_ERR_OTA_ROLLBACK_FAILED;
    }

    esp_err_t err = esp_ota_mark_app_invalid_rollback_and_reboot();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Rollback request failed: %s", esp_err_to_name(err));
    }
    return err;
}

/**
 * @brief Default product acceptance hook, replaceable by product code.
 */
bool __attribute__((weak)) ota_boot_health_product_check(void)
{
#if CONFIG_OTA_TEST_FORCE_BOOT_HEALTH_FAIL
    ESP_LOGE(TAG, "TEST ONLY: CONFIG_OTA_TEST_FORCE_BOOT_HEALTH_FAIL is forcing boot health failure");
    return false;
#else
    return true;
#endif
}

/**
 * @brief 校验当前镜像可运行所需的本地资源，而不依赖网络状态。
 *
 * @param[in] include_gpio_diagnostic 是否调用 GPIO 诊断回调。
 * @param[in] gpio_diagnostic GPIO 诊断回调；不启用时可为 NULL。
 * @return true 分区、Flash、应用描述、堆、队列和可选 GPIO 检查均通过。
 * @return false 任一必要条件失败。
 *
 * @note 创建的测试队列会在返回前删除；GPIO 回调可阻塞，只能在任务上下文调用。
 */
bool ota_boot_health_check(bool include_gpio_diagnostic,
                           ota_boot_health_gpio_diagnostic_t gpio_diagnostic)
{
    /* 运行分区和下一 OTA 分区都必须存在，后续健康结论才有明确目标。 */
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        ESP_LOGE(TAG, "Health check failed: running partition is unavailable");
        return false;
    }

    const esp_partition_t *update_partition = esp_ota_get_next_update_partition(NULL);
    if (update_partition == NULL) {
        ESP_LOGE(TAG, "Health check failed: next OTA partition is unavailable");
        return false;
    }

    /* 分区表的末地址必须落在实物 Flash 容量内，否则继续运行可能读写越界。 */
    uint32_t flash_size = 0;
    if (esp_flash_get_size(NULL, &flash_size) != ESP_OK) {
        ESP_LOGE(TAG, "Health check failed: cannot determine physical flash size");
        return false;
    }
    /* required_flash_end 是分区表要求的最小物理容量上界，单位为字节。 */
    uint64_t required_flash_end = (uint64_t)update_partition->address + update_partition->size;
    ESP_LOGI(TAG, "Physical flash=%" PRIu32
             " bytes; next OTA partition end=0x%08" PRIx64,
             flash_size, required_flash_end);
    if ((uint64_t)flash_size < required_flash_end) {
        ESP_LOGE(TAG, "Health check failed: physical flash is smaller than the configured OTA table");
        return false;
    }

    /* 应用描述至少要有 project_name/version，避免把损坏镜像误当作健康镜像。 */
    esp_app_desc_t app_desc;
    if (esp_ota_get_partition_description(running, &app_desc) != ESP_OK ||
        app_desc.project_name[0] == '\0' || app_desc.version[0] == '\0') {
        ESP_LOGE(TAG, "Health check failed: running app description is invalid");
        return false;
    }
    if (CONFIG_OTA_PRODUCT_ID[0] == '\0' || CONFIG_OTA_HARDWARE_VERSION[0] == '\0') {
        ESP_LOGE(TAG, "Health check failed: OTA product/hardware configuration is empty");
        return false;
    }
    if (heap_caps_get_free_size(MALLOC_CAP_8BIT) < CONFIG_OTA_MIN_FREE_HEAP) {
        ESP_LOGE(TAG, "Health check failed: free heap is below configured minimum");
        return false;
    }

    /* 用一个最小队列验证 FreeRTOS 的动态对象创建、发送和接收路径均可用。 */
    QueueHandle_t health_queue = xQueueCreate(1, sizeof(uint32_t));
    if (health_queue == NULL) {
        ESP_LOGE(TAG, "Health check failed: cannot create a core queue");
        return false;
    }

    /* 固定标记用于确认取出的不是未初始化数据或错误长度的队列项。 */
    uint32_t marker = 0x4f54414a;
    bool queue_ok = xQueueSend(health_queue, &marker, 0) == pdTRUE;
    uint32_t received = 0;
    queue_ok = queue_ok && xQueueReceive(health_queue, &received, 0) == pdTRUE &&
               received == marker;
    vQueueDelete(health_queue);
    if (!queue_ok) {
        ESP_LOGE(TAG, "Health check failed: core queue operation failed");
        return false;
    }

    /* 记录诊断上下文，便于区分首次启动验收和普通启动的资源问题。 */
    ESP_LOGI(TAG, "Health check: project=%s version=%s free_heap=%u reset_reason=%d",
             app_desc.project_name, app_desc.version,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT), esp_reset_reason());

#if CONFIG_OTA_ENABLE_GPIO_DIAGNOSTIC
    if (include_gpio_diagnostic &&
        (gpio_diagnostic == NULL || !gpio_diagnostic())) {
        ESP_LOGE(TAG, "Health check failed: GPIO diagnostic returned low");
        return false;
    }
#else
    (void)include_gpio_diagnostic;
    (void)gpio_diagnostic;
#endif

    return true;
}

/**
 * @brief 在不能继续启动 OTA 通信时保留可诊断的安全状态。
 *
 * @param[in] reason 日志原因，可为 NULL。
 *
 * @note 不返回、不重启、不修改 NVS；每秒让出 CPU，等待人工处理或外部复位。
 */
void ota_boot_health_enter_safe_mode(const char *reason)
{
    ESP_LOGE(TAG, "Entering OTA safe mode: %s", reason != NULL ? reason : "unknown");
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
