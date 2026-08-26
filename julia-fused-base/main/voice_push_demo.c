/**
 * @file    voice_push_demo.c
 * @brief   设备主动推送演示：显式传输项目测试音频到 WSS 服务器。
 *
 * 协议背景：
 * - OTA 检查是设备主动的（设备 publish ota_check 到 MQTT，服务端回响应）；
 * - WSS 语音通道的 FILE_SEND 流程默认由服务端发起（服务端下发 FILE_SEND，
 *   设备回复 BEGIN FILE <size> <name> -> 1200 B 二进制帧 -> END <bytes>）。
 *
 * 本演示展示"设备主动写"：把 测试音频/ 目录下的固定文件列表按
 * "SD:/<文件名>"（映射到 /sdcard/<文件名>）逐一入队，会话任务在 WSS 连接
 * 就绪后按同一协议把文件推给服务器——不等待任何服务端命令。
 *
 * 注意：
 * - 文件必须存在于 SD 卡（/sdcard 挂载由本模块或产品代码负责）；缺失时 WSS
 *   会话会向服务端回复 ERROR file_open_failed，日志同样可证明路径已打通；
 * - 命令只入队不阻塞：断线期间命令滞留在有界队列中，重连后自动执行；
 *   队列满时入队返回 ESP_ERR_NO_MEM，演示会等待后重试；
 * - 主动推送的服务端语义需要服务端配合（接受"不请自来"的 BEGIN FILE 流）。
 */

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"

#include "voice_push_demo.h"
#include "voice_service.h"

#if CONFIG_VOICE_PUSH_DEMO_ENABLE

/** 本模块统一使用的日志标签。 */
static const char *TAG = "voice_push_demo";

/** 客户端未启动（ESP_ERR_INVALID_STATE）或队列满（ESP_ERR_NO_MEM）时的重试间隔。 */
#define DEMO_RETRY_DELAY_MS 500U

/** 保护 start 幂等性的自旋锁；只保护"是否已创建任务"这一个布尔值。 */
static portMUX_TYPE s_demo_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_demo_started;

/**
 * @brief 显式传输的文件列表：与项目 测试音频/ 目录一一对应。
 *
 * 修改文件列表只需增删这里的条目；URI 必须匹配 SD 卡上实际存在的文件名。
 */
static const char *const DEMO_AUDIO_FILES[] = {
    "SD:/BAC009S0002W0122.wav",
    "SD:/BAC009S0002W0123.wav",
    "SD:/BAC009S0002W0124.wav",
    "SD:/BAC009S0002W0125.wav",
    "SD:/BAC009S0002W0126.wav",
    "SD:/BAC009S0002W0127.wav",
    "SD:/BAC009S0002W0128.wav",
};
#define DEMO_AUDIO_FILE_COUNT (sizeof(DEMO_AUDIO_FILES) / sizeof(DEMO_AUDIO_FILES[0]))

/**
 * @brief 阻塞式入队：直到入队成功或遇到不可重试的错误。
 *
 * 语音服务未启动时重试（等 voice_service_ip_ready 生效），队列满时重试（等会话
 * 任务排空一个槽位），从而在多个文件之间自然串行，不会把命令队列撑爆。
 *
 * @param[in] uri 文件 URI，不允许为 NULL。
 * @return ESP_OK 已入队；其他 esp_err_t 不可重试的失败。
 */
static esp_err_t demo_enqueue_blocking(const char *uri)
{
    for (;;) {
        esp_err_t err = voice_service_send_file(uri);
        if (err == ESP_OK || (err != ESP_ERR_INVALID_STATE && err != ESP_ERR_NO_MEM)) {
            return err;
        }
        vTaskDelay(pdMS_TO_TICKS(DEMO_RETRY_DELAY_MS));
    }
}

/**
 * @brief 显式传输整个文件列表一次。
 *
 * @return ESP_OK 全部文件都已入队；ESP_FAIL 中途遇到不可重试错误。
 */
static esp_err_t demo_push_list_once(void)
{
    for (size_t i = 0; i < DEMO_AUDIO_FILE_COUNT; i++) {
        ESP_LOGI(TAG, "Explicit push [%u/%u]: %s",
                 (unsigned)(i + 1U), (unsigned)DEMO_AUDIO_FILE_COUNT, DEMO_AUDIO_FILES[i]);
        esp_err_t err = demo_enqueue_blocking(DEMO_AUDIO_FILES[i]);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Enqueue %s failed: %s", DEMO_AUDIO_FILES[i], esp_err_to_name(err));
            return ESP_FAIL;
        }
    }
    ESP_LOGI(TAG, "All %u files enqueued; WSS session will send BEGIN FILE ... END for each",
             (unsigned)DEMO_AUDIO_FILE_COUNT);
    return ESP_OK;
}

/**
 * @brief 演示任务主体：显式传输一次；CONFIG_VOICE_PUSH_DEMO_INTERVAL_SECONDS
 *        > 0 时按周期重复整个列表。
 */
static void voice_push_demo_task(void *parameter)
{
    (void)parameter;
    ESP_LOGI(TAG, "Explicit audio transfer demo started: %u files, repeat every %d s",
             (unsigned)DEMO_AUDIO_FILE_COUNT, CONFIG_VOICE_PUSH_DEMO_INTERVAL_SECONDS);

    (void)demo_push_list_once();

#if CONFIG_VOICE_PUSH_DEMO_INTERVAL_SECONDS > 0
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS((uint32_t)CONFIG_VOICE_PUSH_DEMO_INTERVAL_SECONDS * 1000U));
        ESP_LOGI(TAG, "Periodic repeat of the explicit file list");
        (void)demo_push_list_once();
    }
#else
    ESP_LOGI(TAG, "Single explicit transfer done; sleeping (enable interval to repeat)");
    vTaskDelay(portMAX_DELAY);
#endif
}

#else /* !CONFIG_VOICE_PUSH_DEMO_ENABLE */
/* 演示关闭：不创建任何任务，voice_push_demo_start() 直接返回不支持。 */
#endif /* CONFIG_VOICE_PUSH_DEMO_ENABLE */

esp_err_t voice_push_demo_start(void)
{
#if CONFIG_VOICE_PUSH_DEMO_ENABLE
    portENTER_CRITICAL(&s_demo_lock);
    if (s_demo_started) {
        portEXIT_CRITICAL(&s_demo_lock);
        return ESP_OK;
    }
    s_demo_started = true;
    portEXIT_CRITICAL(&s_demo_lock);

    if (xTaskCreate(voice_push_demo_task, "voice_push_demo", 3072, NULL, 3,
                    NULL) != pdPASS) {
        portENTER_CRITICAL(&s_demo_lock);
        s_demo_started = false;
        portEXIT_CRITICAL(&s_demo_lock);
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "Voice push demo task created");
    return ESP_OK;
#else
    return ESP_ERR_NOT_SUPPORTED;
#endif
}
