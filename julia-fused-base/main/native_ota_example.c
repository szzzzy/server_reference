/* OTA example

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * @file    native_ota_example.c
 * @brief   基于 ESP-IDF 原生 OTA 接口的固件在线升级示例主程序。
 *
 * 主要职责：
 * 1. 初始化 NVS、网络协议栈，并通过示例连接辅助函数建立 Wi-Fi 或以太网连接；
 * 2. 通过 HTTP(S) 从配置的服务器下载新固件，并按数据块写入下一个 OTA 分区；
 * 3. 在写入前检查固件描述信息，避免重复安装当前版本或上一次启动失败的版本；
 * 4. 完成镜像校验后设置新的启动分区，并重启设备；
 * 5. 设备重启后执行诊断，决定确认新固件或触发回滚。
 *
 * 模块关系：
 * - 依赖 `app_update` 提供的 OTA 分区、镜像状态和写入接口；
 * - 依赖 `esp_http_client` 从固件服务器接收镜像数据；
 * - MQTT 模块主动上报设备版本，并把服务器决策交给本文件解析；
 * - 通过 `ota_control_plane` 校验 type、version、url、sha256，并关联检查请求；
 * - 通过 `ota_stability` 完成断点、Range、镜像头、摘要和提交前条件校验；
 * - 通过 `ota_boot_health` 完成启动验收前的本地健康检查与安全模式处理；
 * - 依赖 `protocol_examples_common` 根据 menuconfig 配置建立网络连接；
 * - 依赖 `server_certs/ca_cert.pem`，该证书由组件构建脚本嵌入最终固件；
 * - 通过 GPIO 诊断输入判断新固件是否满足最基本的启动条件。
 *
 * 运行模型：
 * - `app_main` 完成一次性初始化后，启动 MQTT 客户端；
 * - 通信客户端连接就绪后立即检查版本，此后按带随机抖动的配置周期重复检查；
 * - 服务器返回 `update=true` 且元数据有效时才创建唯一的 `ota_example_task`；
 * - OTA 任务在网络读取、Flash 写入和错误处理过程中运行；
 * - OTA 成功后立即重启，重启后的 `app_main` 负责确认或回滚待验证镜像；
 * - OTA 写缓冲区和写入句柄仅由 OTA 任务使用；任务占用状态由临界区保护；
 * - `diagnostic` 会阻塞约 5 s，因此不能在中断上下文中调用。
 *
 * 注意事项：
 * - 固件 URL、目标版本和 SHA-256 来自服务器响应；设备身份、检查周期、接收超时、诊断 GPIO 和网络类型由项目配置项决定；
 * - 证书内容必须与固件服务器的证书链匹配，不能仅依赖跳过证书名称检查来规避校验；
 * - 本文件只描述 OTA 示例的运行逻辑，不改变分区表、网络参数或硬件配置。
 */
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <errno.h>
#include <sys/param.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_system.h"
#include "esp_app_desc.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"

#include "esp_ota_ops.h"
#include "esp_http_client.h"
#include "esp_partition.h"
#include "esp_tls_errors.h"

#include "nvs_flash.h"

#include "driver/gpio.h"

#include "native_ota_example.h"
#include "network_lifecycle.h"
#include "ota_boot_health.h"
#include "ota_control_plane.h"
#include "ota_engine.h"
#include "ota_report.h"
#include "ota_state_store.h"
#include "ota_stability.h"

#include "mqtt_comm.h"
#include "audio_service.h"
#include "sd_card.h"
#include "voice_service.h"
#include "board_audio.h"
#include "wake_detector.h"
#if CONFIG_VOICE_PUSH_DEMO_ENABLE
#include "voice_push_demo.h"
#endif

#define BUFFSIZE 1024 /**< OTA 下载和 Flash 写入缓冲区大小，单位为字节。 */
#define HASH_LEN 32   /**< SHA-256 摘要长度，单位为字节。 */

/** 兼容官方主流程中的镜像头缓冲区名称，具体定义由稳定性模块提供。 */
#define OTA_IMAGE_HEADER_SIZE OTA_STABILITY_IMAGE_HEADER_SIZE

/** 本文件统一使用的日志标签。 */
static const char *TAG = "native_ota_example";

/** 连续空读的上限；每次空读间隔 10 ms，达到后视为网络无响应。 */
#define OTA_MAX_EMPTY_READS 600U

static uint32_t ota_cooldown_seconds(uint32_t cooldown_count)
{
    uint32_t delay = CONFIG_OTA_DOWNLOAD_COOLDOWN_BASE_SECONDS;
    unsigned shifts = cooldown_count > 0U ? cooldown_count - 1U : 0U;
    while (shifts-- > 0U && delay < CONFIG_OTA_DOWNLOAD_COOLDOWN_MAX_SECONDS) {
        delay = delay > CONFIG_OTA_DOWNLOAD_COOLDOWN_MAX_SECONDS / 2U ?
                CONFIG_OTA_DOWNLOAD_COOLDOWN_MAX_SECONDS : delay * 2U;
    }
    return MIN(delay, (uint32_t)CONFIG_OTA_DOWNLOAD_COOLDOWN_MAX_SECONDS);
}

/** Preserve a reliable TLS handshake/certificate failure instead of merging it
 * into the generic network bucket. DNS, socket and timeout failures remain
 * NETWORK_TIMEOUT when ESP-IDF does not provide TLS verification evidence. */
static native_ota_failure_reason_t ota_http_open_failure_reason(
    esp_http_client_handle_t client)
{
    int tls_code = 0;
    int tls_flags = 0;
    esp_err_t tls_err = esp_http_client_get_and_clear_last_tls_error(
        client, &tls_code, &tls_flags);
    if (tls_flags != 0 || tls_err == ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED ||
        tls_err == ESP_ERR_MBEDTLS_X509_CRT_PARSE_FAILED) {
        ESP_LOGE(TAG, "HTTPS TLS verification/handshake failed: esp_err=0x%x tls=0x%x flags=0x%x",
                 (unsigned)tls_err, (unsigned)tls_code, (unsigned)tls_flags);
        return NATIVE_OTA_FAILURE_TLS_VERIFY_FAILED;
    }
    return NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
}

/**
 * @brief 从 OTA 服务器响应深拷贝得到的单次升级参数。
 *
 * 结构体实例由公共触发入口在堆上创建，OTA 任务启动时复制到自身栈并立即释放
 * 堆内存，从而不依赖 MQTT 事件缓冲区的生命周期。
 */
typedef native_ota_manifest_t ota_request_t;

/** 是否已有 OTA 任务运行；通信事件可能来自不同任务，因此通过临界区访问。 */
static bool s_ota_in_progress;
/** 保护 s_ota_in_progress 的 FreeRTOS 自旋锁，不保护耗时 OTA 操作。 */
static portMUX_TYPE s_ota_state_lock = portMUX_INITIALIZER_UNLOCKED;

/** OTA 任务入口；任务参数为一次服务器响应的堆上深拷贝。 */
static void ota_example_task(void *pvParameter);

/**
 * OTA 数据接收缓冲区。
 *
 * 每次从 HTTP 响应中读取的数据先暂存于此，再交给 `esp_ota_write` 写入
 * OTA 分区。缓冲区由 OTA 任务独占使用，因此当前实现不需要额外的锁保护。
 */
static char ota_write_data[BUFFSIZE + 1] = { 0 };

/**
 * 构建系统嵌入的服务器根证书起始地址和结束地址。
 *
 * `main/CMakeLists.txt` 通过 `EMBED_TXTFILES` 将证书放入最终固件，HTTP 客户端
 * 使用这两个符号确定证书内容范围。证书只读，不由本文件释放或修改。
 */
extern const uint8_t server_cert_pem_start[] asm("_binary_ca_cert_pem_start");
extern const uint8_t server_cert_pem_end[] asm("_binary_ca_cert_pem_end");

/**
 * @brief 在临界区内清除 OTA 任务占用状态。
 *
 * OTA 任务结束、失败或发现无需升级时调用，使后续服务器响应可以再次创建任务。
 * 本函数只修改任务占用标志，不关闭网络、不访问 Flash，也不执行阻塞操作。
 *
 * @note 可由普通任务调用，不能在中断上下文调用；临界区只覆盖一个布尔值的读写。
 */
static void ota_clear_in_progress(void)
{
    portENTER_CRITICAL(&s_ota_state_lock);
    s_ota_in_progress = false;
    portEXIT_CRITICAL(&s_ota_state_lock);
}

/**
 * @brief 查询是否已有固件 OTA 下载任务运行（供上层互斥协调器使用）。
 *
 * @return true 已有 OTA 任务运行；false 空闲。
 *
 * @note 由 ota_engine.h 声明；临界区只覆盖一个布尔值的读取，可在普通任务
 *       或 MQTT 事件任务上下文中调用。
 */
bool ota_engine_is_running(void)
{
    portENTER_CRITICAL(&s_ota_state_lock);
    bool running = s_ota_in_progress;
    portEXIT_CRITICAL(&s_ota_state_lock);
    return running;
}

/**
 * @brief 解析并校验一条服务端 OTA 响应，然后创建唯一的 OTA 下载任务。
 *
 * 响应必须先通过 type、request_id、设备身份、产品、硬件版本和 update 字段校验；
 * `update=false` 只结束本次检查，`update=true` 才继续校验 artifact、版本、HTTPS URL、
 * 镜像长度、SHA-256、安全版本和过期时间。元数据检查在进入临界区前完成，临界区只负责
 * 把“没有 OTA 任务运行”原子地切换为“已有任务运行”，避免 MQTT 事件同时
 * 创建两个任务写入同一 OTA 分区。
 *
 * @param[in] json     JSON 原始数据首地址，不要求以 NUL 结尾。
 * @param[in] json_len JSON 有效字节数，范围为 1～NATIVE_OTA_JSON_MAX_LEN。
 * @return ESP_OK 响应有效；无需升级、版本相同或 OTA 任务创建成功均返回此值。
 * @return ESP_ERR_INVALID_ARG JSON 格式、身份、字段、URL 或镜像元数据无效。
 * @return ESP_ERR_INVALID_STATE request_id 过期、已有 OTA 任务运行或 artifact 已隔离。
 * @return ESP_ERR_NO_MEM 无法分配清单副本或创建 OTA 任务。
 *
 * @note 可由 MQTT 事件任务调用，但不能在中断上下文调用。
 * @note 函数只解析元数据并调度任务，不阻塞等待 HTTPS 下载；任务参数在创建成功后由 OTA
 *       任务取得所有权并释放。
 */
esp_err_t native_ota_handle_server_json(const char *json, size_t json_len)
{
    ota_request_t manifest;
    bool download_requested = false;
    esp_err_t err = ota_control_plane_parse_server_response(json, json_len, &manifest,
                                                            &download_requested);
    if (err != ESP_OK || !download_requested) {
        return err;
    }

    ota_request_t *request = calloc(1, sizeof(*request));
    if (request == NULL) {
        return ESP_ERR_NO_MEM;
    }
    *request = manifest;

    portENTER_CRITICAL(&s_ota_state_lock);
    if (s_ota_in_progress) {
        portEXIT_CRITICAL(&s_ota_state_lock);
        free(request);
        return ESP_ERR_INVALID_STATE;
    }
    s_ota_in_progress = true;
    portEXIT_CRITICAL(&s_ota_state_lock);

    ESP_LOGI(TAG, "Creating OTA task: artifact=%s, version=%s, size=%" PRIu32 ", force_update=%d",
             request->artifact_id, request->version, request->image_size, (int)request->force_update);
    if (xTaskCreate(ota_example_task, "ota_example_task", 12288, request, 5, NULL) != pdPASS) {
        ota_clear_in_progress();
        free(request);
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

/**
 * @brief 兼容原有调用方的 OTA JSON 入口。
 *
 * 新通信模块使用 native_ota_handle_server_json()；保留本包装函数可避免现有测试
 * 或外部模块在协议升级后立即失效。
 *
 * @param[in] json      原有 OTA JSON 首地址，不允许为 NULL。
 * @param[in] json_len  JSON 有效字节数，范围为 1～NATIVE_OTA_JSON_MAX_LEN。
 * @return ESP_OK 响应有效或 OTA 任务创建成功。
 * @return 其他 esp_err_t 由 native_ota_handle_server_json() 原样返回。
 *
 * @note 本函数不执行下载，只转发到统一解析入口，不允许在中断上下文调用。
 */
esp_err_t native_ota_trigger_json(const char *json, size_t json_len)
{
    return native_ota_handle_server_json(json, json_len);
}

/**
 * @brief 将固定长度的 SHA-256 摘要转换为十六进制字符串并输出。
 *
 * @param[in] image_hash 指向至少 `HASH_LEN` 字节摘要数据的指针，不允许为 NULL。
 * @param[in] label      日志前缀字符串，不允许为 NULL。
 *
 * @note 本函数只读取摘要并输出日志，不修改输入数据，也不返回计算结果。
 * @note 函数使用栈上的临时字符串，运行在任务上下文中，不应在中断中调用。
 */
static void print_sha256 (const uint8_t *image_hash, const char *label)
{
    char hash_print[HASH_LEN * 2 + 1];
    hash_print[HASH_LEN * 2] = 0;
    for (int i = 0; i < HASH_LEN; ++i) {
        sprintf(&hash_print[i * 2], "%02x", image_hash[i]);
    }
    ESP_LOGI(TAG, "%s: %s", label, hash_print);
}


/**
 * @brief 在官方 OTA 主流程中计算目标分区已写入内容的 SHA-256。
 *
 * @param[in]  partition 目标 OTA 分区，不允许为 NULL。
 * @param[in]  length    已写入镜像长度，单位为字节。
 * @param[out] output    接收 HASH_LEN 字节摘要的缓冲区。
 * @return ESP_OK 摘要计算成功；其他值由稳定性模块返回。
 *
 * @note 具体的分块 Flash 读取和 PSA Crypto 实现位于 ota_stability.c；本包装保留
 *       官方 OTA 主流程中的调用位置和顺序。
 */
static esp_err_t calculate_partition_sha256(const esp_partition_t *partition, size_t length,
                                            uint8_t output[HASH_LEN])
{
    return ota_stability_calculate_partition_sha256(partition, length, output);
}

/**
 * @brief 根据当前 manifest 和目标分区创建初始恢复记录。
 *
 * @param[out] record 输出的 NVS 恢复记录，不允许为 NULL。
 * @param[in] request 当前服务器清单，不允许为 NULL。
 * @param[in] partition 本次写入的 OTA 目标分区，不允许为 NULL。
 *
 * @note 函数只初始化内存，不执行 NVS 写入；调用者必须随后调用
 *       ota_state_store_save() 持久化记录。
 */
static void ota_record_init(ota_resume_record_t *record, const ota_request_t *request,
                            const esp_partition_t *partition)
{
    ota_stability_record_init(record, request, partition);
}

/**
 * @brief 按 Flash Encryption 的写入约束规范化恢复偏移。
 *
 * @param[in] offset 原始恢复偏移，单位为字节。
 * @return 未启用 Flash Encryption 时返回原值；启用时返回向下对齐到 16 字节的值。
 *
 * @note 只做整数运算和读取 eFuse 配置，不访问分区、不修改全局状态。
 */
static size_t ota_normalize_resume_offset(size_t offset)
{
    return ota_stability_normalize_resume_offset(offset);
}

/**
 * @brief 按固定数据量间隔保存 OTA 恢复检查点。
 *
 * @param[in,out] record 待更新并写入 NVS 的恢复记录，不允许为 NULL。
 * @param[in] offset 当前已写入镜像长度，单位为字节。
 * @param[in] force true 时忽略检查点间隔，立即保存；正常下载中应传 false。
 * @return ESP_OK 未达到保存间隔或 NVS 保存成功。
 * @return 其他 esp_err_t NVS 写入/提交失败。
 *
 * @note 启用 Flash Encryption 时会先按 16 字节边界向下对齐，避免保存一个不能安全
 *       作为后续写入起点的偏移；函数可能写 Flash，不能在中断上下文调用。
 */
static esp_err_t ota_save_checkpoint(ota_resume_record_t *record, size_t offset, bool force)
{
    return ota_stability_save_checkpoint(record, offset, force);
}

/**
 * @brief 将终端校验失败的 artifact 持久化为隔离状态。
 *
 * @param[in,out] record 当前 artifact 的恢复记录，可为 NULL。
 * @param[in] reason 终端失败分类，保存到 failure_reason 供后续诊断。
 *
 * @note 隔离按 artifact 元数据而不是仅按版本号生效，防止同一坏镜像在重启后重复下载；
 *       NVS 保存失败只记录日志，不改变当前调用流程。
 */
static void ota_quarantine_record(ota_resume_record_t *record,
                                  native_ota_failure_reason_t reason)
{
    ota_stability_quarantine_record(record, reason);
}

/**
 * @brief 解析 `Content-Range: bytes start-end/total` 响应头。
 *
 * @param[in] value        NUL 结尾的 Content-Range 字符串，可为 NULL。
 * @param[out] range_start 返回本次响应的起点，单位为字节。
 * @param[out] range_end   返回本次响应的结束位置，单位为字节，包含该字节。
 * @param[out] total_size  返回完整镜像长度，单位为字节。
 * @return true 格式、顺序和边界均有效。
 * @return false 参数为空、格式错误、溢出或 end 不在 total 范围内。
 *
 * @note 该函数只解析字符串，不执行网络访问；调用者仍需把结果与 manifest 和恢复偏移
 *       比较，单独的格式正确不足以证明响应属于当前 artifact。
 */
#if CONFIG_ESP_HTTP_CLIENT_SAVE_RESPONSE_HEADERS
static bool ota_parse_content_range(const char *value, size_t *range_start,
                                    size_t *range_end, size_t *total_size)
{
    return ota_stability_parse_content_range(value, range_start, range_end, total_size);
}
#endif

/**
 * @brief 校验网络收到的 ESP 应用头和应用描述信息。
 *
 * @param[in] header      至少包含 OTA_IMAGE_HEADER_SIZE 字节的镜像头缓冲区。
 * @param[in] header_size header 实际可读长度，单位为字节。
 * @param[in] request     当前服务器清单，用于比较项目、版本和 secure_version。
 * @param[out] app_desc   输出复制后的应用描述符，不允许为 NULL。
 * @return ESP_OK magic、芯片、项目名、版本和安全版本均符合要求。
 * @return ESP_ERR_INVALID_ARG 参数为空或头部长度不足。
 * @return ESP_ERR_INVALID_VERSION 芯片、项目名、版本或 secure_version 不匹配。
 * @return ESP_ERR_OTA_VALIDATE_FAILED magic 或应用描述符格式无效。
 *
 * @note 函数只读 header 和 request，先复制应用描述符再读取，避免调用者后续复用网络缓冲区
 *       时影响输出；不访问 Flash，也不启动 OTA 写入。
 */
static esp_err_t ota_validate_image_header(const uint8_t *header, size_t header_size,
                                           const ota_request_t *request,
                                           esp_app_desc_t *app_desc)
{
    return ota_stability_validate_image_header(header, header_size, request, app_desc);
}

/**
 * @brief 从目标 OTA 分区读取并校验已存在的镜像头，用于断点续传。
 *
 * @param[in] partition 断点所在的 OTA 目标分区，不允许为 NULL。
 * @param[in] request 当前服务器清单，不允许为 NULL。
 * @param[out] app_desc 输出镜像应用描述符，不允许为 NULL。
 * @return ESP_OK 分区头可读且与当前清单一致。
 * @return 其他 esp_err_t 分区读取或镜像头校验失败。
 *
 * @note 函数同步读取 Flash，只允许在任务上下文调用；它不会创建或恢复 OTA 写入句柄。
 */
static esp_err_t ota_validate_partition_header(const esp_partition_t *partition,
                                               const ota_request_t *request,
                                               esp_app_desc_t *app_desc)
{
    return ota_stability_validate_partition_header(partition, request, app_desc);
}

/**
 * @brief 在切换 boot partition 前执行板级电源、业务和资源前置检查。
 *
 * @param[in] request 当前服务器清单，不允许为 NULL。
 * @param[in] partition 已完成镜像写入的目标分区，不允许为 NULL。
 * @return NATIVE_OTA_FAILURE_NONE 所有提交前条件满足。
 * @return 其他 native_ota_failure_reason_t 对应的条件未满足或镜像不可提交。
 *
 * @note 本函数只决定是否允许切换启动分区，不执行切换、不重启；板级弱钩子可能访问产品
 *       资源，必须在普通任务上下文调用。失败时保留恢复记录供后续处理。
 */
static native_ota_failure_reason_t ota_pre_commit_check(const ota_request_t *request,
                                                        const esp_partition_t *partition)
{
    return ota_stability_pre_commit_check(request, partition);
}

/**
 * @brief 将下载失败原因映射为对云端可见的报告状态。
 *
 * @param[in] failure_reason OTA 任务记录的稳定失败原因。
 * @return NATIVE_OTA_REPORT_DEFERRED 电源或业务条件暂时不满足，可保留 READY_TO_COMMIT。
 * @return NATIVE_OTA_REPORT_FAILED 其他终端错误，不应按暂缓处理。
 *
 * @note 该映射只影响状态上报，不改变恢复记录和 OTA 资源清理策略。
 */
static native_ota_report_state_t ota_report_state_for_failure(
    native_ota_failure_reason_t failure_reason)
{
    if (failure_reason == NATIVE_OTA_FAILURE_PRECONDITION_LOW_POWER ||
        failure_reason == NATIVE_OTA_FAILURE_BOOT_SELF_TEST_FAILED) {
        return NATIVE_OTA_REPORT_DEFERRED;
    }
    return NATIVE_OTA_REPORT_FAILED;
}

/**
 * @brief 执行带完整响应校验、NVS 检查点和 HTTP Range 恢复的 OTA 下载任务。
 *
 * @param[in] pvParameter 指向堆上 native OTA manifest；任务取得所有权后立即释放，不能为 NULL。
 *
 * 任务先复用与清单匹配的断点记录，再建立 HTTPS 连接；Range、HTTP 状态、Content-Length、
 * Content-Range、镜像头、完整接收状态和 SHA-256 必须全部通过，才会调用
 * esp_ota_end() 及 esp_ota_set_boot_partition()。网络类失败保留 DOWNLOADING 检查点，
 * 镜像长度、头部或摘要等终端校验失败则将 artifact 隔离。
 *
 * @note 任务可能阻塞在网络、Flash 和 NVS 操作中，不能在中断上下文调用。
 * @note 所有退出路径汇聚到 cleanup，确保 HTTP 客户端、OTA 句柄、恢复记录和任务占用状态
 *       按当前资源是否已建立分别清理；成功切换启动分区后通过 esp_restart() 重启。
 */
static void ota_example_task(void *pvParameter)
{
    ota_request_t request = *(ota_request_t *)pvParameter;
    free(pvParameter);

    /*
     * 下面的标志描述 cleanup 阶段需要释放的资源和最终结果：
     * - ota_handle_active/client_open：仅在对应 API 成功后置位，避免对未建立资源重复清理；
     * - reboot：已经成功设置下次启动分区，退出时应立即重启；
     * - terminal_failure/keep_record：分别控制 artifact 隔离和恢复记录保留策略；
     * - record_active：表示 NVS 中已有或已创建本次 artifact 的恢复记录。
     */
    /* err 保存最近一次底层操作结果，仅用于日志和失败报告；失败原因单独决定恢复策略。 */
    esp_err_t err = ESP_OK;
    /* 只有 esp_ota_begin()/esp_ota_resume() 成功后才置为 active，cleanup 据此决定 abort。 */
    esp_ota_handle_t update_handle = 0;
    bool ota_handle_active = false;
    bool client_open = false;
    bool reboot = false;
    bool terminal_failure = false;
    bool keep_record = false;
    esp_http_client_handle_t client = NULL;
    native_ota_failure_reason_t failure_reason = NATIVE_OTA_FAILURE_NONE;
    ota_resume_record_t record;
    bool record_active = false;
    bool resume = false;
    size_t resume_offset = 0;
    /* 已成功写入 OTA 分区的镜像长度，单位为字节；它同时是摘要和进度的有效边界。 */
    size_t binary_file_length = 0;
    const esp_partition_t *update_partition = NULL;
    native_ota_report_context_t report_context;
    bool report_context_valid = false;

    /* 报告上下文复制当前版本，供重启后把 bootloader 结果关联回本次 artifact。 */
    const esp_app_desc_t *running_app = esp_app_get_description();
    if (running_app != NULL &&
        native_ota_report_context_init(&report_context, &request, running_app->version) == ESP_OK) {
        report_context_valid = true;
    }

    ESP_LOGI(TAG, "Starting OTA example task: artifact=%s, version=%s",
             request.artifact_id, request.version);

    if (report_context_valid) {
        esp_err_t report_err = native_ota_report_event(&report_context,
                                                       NATIVE_OTA_REPORT_ACCEPTED,
                                                       0, NATIVE_OTA_FAILURE_NONE);
        if (report_err != ESP_OK) {
            ESP_LOGW(TAG, "Could not enqueue OTA accepted report: %s",
                     esp_err_to_name(report_err));
        }
    }

    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        failure_reason = NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
        terminal_failure = true;
        goto cleanup;
    }
    update_partition = esp_ota_get_next_update_partition(NULL);
    if (update_partition == NULL) {
        failure_reason = NATIVE_OTA_FAILURE_IMAGE_TOO_LARGE;
        terminal_failure = true;
        goto cleanup;
    }
    if (request.image_size == 0 || request.image_size > update_partition->size) {
        ESP_LOGE(TAG, "image_size=%" PRIu32 " exceeds update partition size=%" PRIu32,
                 request.image_size, update_partition->size);
        failure_reason = NATIVE_OTA_FAILURE_IMAGE_TOO_LARGE;
        terminal_failure = true;
        goto cleanup;
    }

    /* 只有 artifact 元数据完全一致时才允许复用 Flash 前缀和 HTTP Range 检查点。 */
    err = ota_state_store_load(&record);
    if (err == ESP_OK) {
        /* same_artifact 同时比较 ID、版本、URL、大小和 SHA-256，防止串用旧镜像状态。 */
        bool same_artifact = ota_state_store_matches_manifest(&record, &request);
        if (same_artifact && record.phase == OTA_RESUME_PHASE_COOLING_DOWN) {
            record_active = true;
            uint32_t cooldown_seconds = ota_cooldown_seconds(record.cooldown_count);
            ESP_LOGW(TAG, "Artifact %s resumes persisted network cooldown: failures=%" PRIu32
                     " cooldown_count=%" PRIu32 " delay=%" PRIu32 " s",
                     request.artifact_id, record.retry_count, record.cooldown_count,
                     cooldown_seconds);
            vTaskDelay(pdMS_TO_TICKS((uint64_t)cooldown_seconds * 1000U));
            record.phase = OTA_RESUME_PHASE_DOWNLOADING;
            record.retry_count = 0;
            err = ota_state_store_save(&record);
            if (err != ESP_OK) {
                failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
                goto cleanup;
            }
        } else if (same_artifact && record.phase == OTA_RESUME_PHASE_DOWNLOADING &&
                   record.retry_count >= OTA_STATE_STORE_NETWORK_RETRY_THRESHOLD) {
            record_active = true;
            uint32_t cooldown_seconds = ota_cooldown_seconds(record.cooldown_count + 1U);
            ESP_LOGW(TAG, "Artifact %s network cooldown: failures=%" PRIu32
                     " cooldown_count=%" PRIu32 " delay=%" PRIu32 " s",
                     request.artifact_id, record.retry_count, record.cooldown_count,
                     cooldown_seconds);
            record.phase = OTA_RESUME_PHASE_COOLING_DOWN;
            if (record.cooldown_count != UINT32_MAX) {
                record.cooldown_count++;
            }
            err = ota_state_store_save(&record);
            if (err != ESP_OK) {
                failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
                goto cleanup;
            }
            vTaskDelay(pdMS_TO_TICKS((uint64_t)cooldown_seconds * 1000U));
            record.phase = OTA_RESUME_PHASE_DOWNLOADING;
            record.retry_count = 0;
            err = ota_state_store_save(&record);
            if (err != ESP_OK) {
                failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
                goto cleanup;
            }
        }
        if (same_artifact &&
            record.target_partition_subtype == update_partition->subtype &&
            record.phase == OTA_RESUME_PHASE_DOWNLOADING &&
            record.verified_offset < request.image_size &&
            record.retry_count < OTA_STATE_STORE_NETWORK_RETRY_THRESHOLD) {
            record_active = true;
            resume_offset = ota_normalize_resume_offset(record.verified_offset);
            resume = resume_offset > 0;
            ESP_LOGI(TAG, "Resuming artifact %s from offset=%zu", request.artifact_id,
                     resume_offset);
        } else if (same_artifact &&
                   record.phase == OTA_RESUME_PHASE_READY_TO_COMMIT &&
                   record.verified_offset == request.image_size &&
                   record.target_partition_subtype == update_partition->subtype) {
            /* 之前已经完成镜像校验但提交前掉电，先复用已验证的分区。 */
            /* READY_TO_COMMIT 记录可能来自掉电恢复，重新计算分区摘要防止提交被篡改的前缀。 */
            uint8_t downloaded_sha256[HASH_LEN];
            err = calculate_partition_sha256(update_partition, request.image_size,
                                             downloaded_sha256);
            if (err == ESP_OK && memcmp(downloaded_sha256, request.sha256, HASH_LEN) == 0) {
                failure_reason = ota_pre_commit_check(&request, update_partition);
                if (failure_reason == NATIVE_OTA_FAILURE_NONE) {
                    err = esp_ota_set_boot_partition(update_partition);
                    if (err == ESP_OK) {
                        if (report_context_valid) {
                            esp_err_t report_err = native_ota_report_event(
                                &report_context, NATIVE_OTA_REPORT_REBOOTING, 0,
                                NATIVE_OTA_FAILURE_NONE);
                            if (report_err != ESP_OK) {
                                ESP_LOGW(TAG, "Could not enqueue OTA rebooting report: %s",
                                         esp_err_to_name(report_err));
                            }
                        }
                        reboot = true;
                        goto cleanup;
                    }
                    failure_reason = NATIVE_OTA_FAILURE_BOOT_PARTITION_SET_FAILED;
                    keep_record = true;
                    goto cleanup;
                } else {
                    keep_record = true;
                    goto cleanup;
                }
            }
            ESP_LOGW(TAG, "READY_TO_COMMIT record no longer matches partition; restarting download");
            (void)ota_state_store_clear();
        } else {
            ESP_LOGI(TAG, "OTA resume record belongs to another artifact; starting from zero");
            (void)ota_state_store_clear();
        }
    } else if (err != ESP_ERR_NOT_FOUND && err != ESP_ERR_INVALID_VERSION) {
        ESP_LOGE(TAG, "Failed to load OTA resume record: %s", esp_err_to_name(err));
        failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
        goto cleanup;
    }

    if (report_context_valid) {
        report_context.attempt = record_active ? record.retry_count + 1U : 1U;
    }

    if (!record_active) {
        ota_record_init(&record, &request, update_partition);
        err = ota_state_store_save(&record);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Cannot persist initial OTA resume record: %s", esp_err_to_name(err));
            failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
            goto cleanup;
        }
        record_active = true;
    }

    /* 最多允许一次“服务器忽略 Range 后从零重试”，避免把完整镜像追加到旧偏移。 */
    /* 仅允许一次从 Range 退回全量下载；超过后不会无限循环消耗网络和 Flash。 */
    for (unsigned http_attempt = 0; http_attempt < 2; ++http_attempt) {
        /* 每次重试都创建新的 HTTP 客户端，确保上一次连接的响应状态不会被复用。 */
        esp_http_client_config_t config = {
            .url = request.url,
            .cert_pem = (char *)server_cert_pem_start,
            .timeout_ms = CONFIG_EXAMPLE_OTA_RECV_TIMEOUT,
            .keep_alive_enable = true,
        };
#ifdef CONFIG_EXAMPLE_SKIP_COMMON_NAME_CHECK
        config.skip_cert_common_name_check = true;
#endif

        client = esp_http_client_init(&config);
        if (client == NULL) {
            failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
            goto cleanup;
        }
        if (resume) {
            /* Range 起点必须等于已经持久化的镜像前缀长度，服务器返回的数据才能直接追加。 */
            char range_header[64];
            int range_len = snprintf(range_header, sizeof(range_header), "bytes=%zu-",
                                     resume_offset);
            if (range_len <= 0 || (size_t)range_len >= sizeof(range_header) ||
                esp_http_client_set_header(client, "Range", range_header) != ESP_OK) {
                failure_reason = NATIVE_OTA_FAILURE_RANGE_MISMATCH;
                terminal_failure = true;
                goto cleanup;
            }
        }

        err = esp_http_client_open(client, 0);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to open HTTP connection: %s", esp_err_to_name(err));
            failure_reason = ota_http_open_failure_reason(client);
            goto cleanup;
        }
        client_open = true;

        /* content_length 为服务器声明的 body 长度；0 表示未知，常见于 chunked 响应。 */
        int64_t content_length = esp_http_client_fetch_headers(client);
        if (content_length < 0) {
            ESP_LOGE(TAG, "Failed to fetch HTTP headers: %" PRId64, content_length);
            failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
            goto cleanup;
        }
        if (content_length == 0) {
            ESP_LOGW(TAG, "HTTP response has no Content-Length; manifest size and complete-body checks will be used");
        }
        /* status_code 与 content_length 共同决定当前响应是完整下载还是可续传响应。 */
        int status_code = esp_http_client_get_status_code(client);
        /* ETag 用于确认断点对应的服务器对象没有在两次请求之间被替换。 */
        char *response_etag = NULL;
#if CONFIG_ESP_HTTP_CLIENT_SAVE_RESPONSE_HEADERS
        (void)esp_http_client_get_response_header(client, "ETag", &response_etag);
#endif

        if (resume && record.etag[0] != '\0' &&
            (response_etag == NULL || strcmp(record.etag, response_etag) != 0)) {
            ESP_LOGW(TAG, "HTTP ETag changed while resuming; restarting from zero");
            (void)esp_http_client_close(client);
            (void)esp_http_client_cleanup(client);
            client = NULL;
            client_open = false;
            resume = false;
            resume_offset = 0;
            ota_record_init(&record, &request, update_partition);
            err = ota_state_store_save(&record);
            if (err != ESP_OK) {
                failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
                goto cleanup;
            }
            continue;
        }
        if (response_etag != NULL && response_etag[0] != '\0') {
            strncpy(record.etag, response_etag, sizeof(record.etag) - 1);
            record.etag[sizeof(record.etag) - 1] = '\0';
            err = ota_state_store_save(&record);
            if (err != ESP_OK) {
                failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
                goto cleanup;
            }
        }

        if (resume) {
            if (status_code == 200 || status_code == 416) {
                ESP_LOGW(TAG, "Server cannot honor Range (status=%d); restarting from zero", status_code);
                (void)esp_http_client_close(client);
                (void)esp_http_client_cleanup(client);
                client = NULL;
                client_open = false;
                resume = false;
                resume_offset = 0;
                ota_record_init(&record, &request, update_partition);
                err = ota_state_store_save(&record);
                if (err != ESP_OK) {
                    failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
                    goto cleanup;
                }
                continue;
            }
            if (status_code != 206) {
                ESP_LOGW(TAG, "Range request returned transient/invalid HTTP status=%d", status_code);
                failure_reason = NATIVE_OTA_FAILURE_HTTP_STATUS_INVALID;
                goto cleanup;
            }
            if (content_length > 0 &&
                content_length != (int64_t)(request.image_size - resume_offset)) {
                ESP_LOGE(TAG, "Range response invalid: status=%d length=%" PRId64
                         " expected=%" PRIu32, status_code, content_length,
                         request.image_size - (uint32_t)resume_offset);
                failure_reason = NATIVE_OTA_FAILURE_RANGE_MISMATCH;
                terminal_failure = true;
                goto cleanup;
            }
#if CONFIG_ESP_HTTP_CLIENT_SAVE_RESPONSE_HEADERS
            /* Content-Range 进一步确认响应覆盖的区间和完整镜像大小。 */
            char *content_range = NULL;
            (void)esp_http_client_get_response_header(client, "Content-Range", &content_range);
            size_t range_start = 0;
            size_t range_end = 0;
            size_t range_total = 0;
            if (!ota_parse_content_range(content_range, &range_start, &range_end, &range_total) ||
                range_start != resume_offset || range_end != request.image_size - 1U ||
                range_total != request.image_size) {
                ESP_LOGE(TAG, "Content-Range does not match resume offset");
                failure_reason = NATIVE_OTA_FAILURE_RANGE_MISMATCH;
                terminal_failure = true;
                goto cleanup;
            }
#else
            ESP_LOGE(TAG, "Range resume requires saved HTTP response headers");
            failure_reason = NATIVE_OTA_FAILURE_RANGE_MISMATCH;
            terminal_failure = true;
            goto cleanup;
#endif
        } else if (status_code != 200) {
            ESP_LOGW(TAG, "Full OTA request returned transient/invalid HTTP status=%d", status_code);
            failure_reason = NATIVE_OTA_FAILURE_HTTP_STATUS_INVALID;
            goto cleanup;
        } else if (content_length > 0 && content_length != (int64_t)request.image_size) {
            ESP_LOGE(TAG, "Full OTA response invalid: status=%d length=%" PRId64
                     " expected=%" PRIu32, status_code, content_length, request.image_size);
            failure_reason = NATIVE_OTA_FAILURE_HTTP_STATUS_INVALID;
            terminal_failure = true;
            goto cleanup;
        }
        break;
    }

    if (client == NULL) {
        failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
        goto cleanup;
    }

    if (report_context_valid) {
        esp_err_t report_err = native_ota_report_event(&report_context,
                                                       NATIVE_OTA_REPORT_DOWNLOADING,
                                                       (uint32_t)resume_offset,
                                                       NATIVE_OTA_FAILURE_NONE);
        if (report_err != ESP_OK) {
            ESP_LOGW(TAG, "Could not enqueue OTA downloading report: %s",
                     esp_err_to_name(report_err));
        }
    }

    /* 续传时目标分区头已在 Flash 中校验过；全量下载必须先跨 read 收齐完整镜像头。 */
    /* 续传前缀已从目标分区校验；全量模式必须跨多个 read 收齐镜像头后才能写 Flash。 */
    bool image_header_checked = resume;
    size_t header_bytes = resume ? OTA_IMAGE_HEADER_SIZE : 0;
    uint8_t image_header[OTA_IMAGE_HEADER_SIZE];
    esp_app_desc_t new_app_info;
    if (resume) {
        /* 先确认 Flash 中的前缀仍属于当前 artifact，再让 ESP-IDF 从该偏移继续写入。 */
        err = ota_validate_partition_header(update_partition, &request, &new_app_info);
        if (err != ESP_OK) {
            failure_reason = NATIVE_OTA_FAILURE_IMAGE_HEADER_INVALID;
            terminal_failure = true;
            goto cleanup;
        }
        err = esp_ota_resume(update_partition, OTA_WITH_SEQUENTIAL_WRITES,
                             resume_offset, &update_handle);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_resume failed: %s", esp_err_to_name(err));
            failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
            goto cleanup;
        }
        ota_handle_active = true;
        binary_file_length = resume_offset;
    }

    /* 网络暂时没有数据时允许短暂重试，但连续空读超过约 6 s 即判定连接失效。 */
    /* 仅统计连续无数据 read；一旦收到真实数据就清零，容忍短暂 EAGAIN/0 字节。 */
    unsigned empty_reads = 0;
    while (1) {
        int data_read = esp_http_client_read(client, ota_write_data, BUFFSIZE);
        if (data_read == -ESP_ERR_HTTP_EAGAIN) {
            if (++empty_reads > OTA_MAX_EMPTY_READS) {
                failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
                goto cleanup;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        if (data_read < 0) {
            ESP_LOGE(TAG, "HTTPS data read failed: %d", data_read);
            failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
            goto cleanup;
        }
        if (data_read == 0) {
            if (esp_http_client_is_complete_data_received(client)) {
                break;
            }
            if (++empty_reads > OTA_MAX_EMPTY_READS) {
                failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
                goto cleanup;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        empty_reads = 0;

        /* data_offset 表示本次网络块中已被复制到 image_header 的前缀长度。 */
        size_t data_offset = 0;
        if (!image_header_checked) {
            size_t header_needed = OTA_IMAGE_HEADER_SIZE - header_bytes;
            size_t header_part = MIN((size_t)data_read, header_needed);
            memcpy(image_header + header_bytes, ota_write_data, header_part);
            header_bytes += header_part;
            data_offset = header_part;
            /* 镜像头可能跨多个 HTTPS read；未收齐前不能开始 Flash 写入或版本判断。 */
            if (header_bytes < OTA_IMAGE_HEADER_SIZE) {
                continue;
            }

            err = ota_validate_image_header(image_header, sizeof(image_header), &request,
                                            &new_app_info);
            if (err != ESP_OK) {
                failure_reason = (err == ESP_ERR_INVALID_VERSION) ?
                                 NATIVE_OTA_FAILURE_IMAGE_HEADER_INVALID :
                                 NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
                terminal_failure = true;
                goto cleanup;
            }
            /* 只有镜像头校验通过后才擦写目标分区，避免无效 artifact 先破坏恢复槽位。 */
            err = esp_ota_begin(update_partition, OTA_WITH_SEQUENTIAL_WRITES, &update_handle);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
                failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
                goto cleanup;
            }
            ota_handle_active = true;
            err = esp_ota_write(update_handle, image_header, sizeof(image_header));
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to write image header: %s", esp_err_to_name(err));
                failure_reason = NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
                terminal_failure = true;
                goto cleanup;
            }
            binary_file_length = sizeof(image_header);
            image_header_checked = true;
        }

        if (data_offset < (size_t)data_read) {
            /* 头部已被单独处理，剩余数据按原顺序追加到同一个 OTA 写入会话。 */
            size_t write_length = (size_t)data_read - data_offset;
            if (binary_file_length > request.image_size ||
                write_length > request.image_size - binary_file_length) {
                ESP_LOGE(TAG, "Downloaded image exceeds manifest image_size");
                failure_reason = NATIVE_OTA_FAILURE_IMAGE_TOO_LARGE;
                terminal_failure = true;
                goto cleanup;
            }
            err = esp_ota_write(update_handle, ota_write_data + data_offset, write_length);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to write OTA data: %s", esp_err_to_name(err));
                failure_reason = NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
                terminal_failure = true;
                goto cleanup;
            }
            binary_file_length += write_length;
        }

        /* 检查点按固定字节间隔落盘，兼顾掉电恢复能力和 NVS 擦写次数。 */
        err = ota_save_checkpoint(&record, binary_file_length, false);
        if (err != ESP_OK) {
            failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
            goto cleanup;
        }
        if (report_context_valid) {
            (void)native_ota_report_progress(&report_context,
                                             (uint32_t)binary_file_length);
        }
    }

    /* body 完整、长度匹配是摘要和 esp_ota_end() 之前的必要条件。 */
    bool complete_body = esp_http_client_is_complete_data_received(client);
    if (!image_header_checked || binary_file_length != request.image_size || !complete_body) {
        ESP_LOGE(TAG, "Received image is incomplete: got=%zu expected=%" PRIu32,
                 binary_file_length, request.image_size);
        if (complete_body) {
            failure_reason = NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
            terminal_failure = true;
        } else {
            failure_reason = NATIVE_OTA_FAILURE_NETWORK_TIMEOUT;
        }
        goto cleanup;
    }

    if (report_context_valid) {
        /* 100% 进度必须先进入 RAM/发送队列，再进入验证阶段。 */
        (void)native_ota_report_progress(&report_context, request.image_size);
        esp_err_t report_err = native_ota_report_event(&report_context,
                                                       NATIVE_OTA_REPORT_VERIFYING,
                                                       0, NATIVE_OTA_FAILURE_NONE);
        if (report_err != ESP_OK) {
            ESP_LOGW(TAG, "Could not enqueue OTA verifying report: %s",
                     esp_err_to_name(report_err));
        }
    }

    /* 从目标分区实际写入的 bin 长度重新计算摘要，避免把分区剩余空间计入 SHA-256。 */
    /* 摘要覆盖实际镜像长度，不覆盖 OTA 槽中未使用的尾部空间。 */
    uint8_t downloaded_sha256[HASH_LEN];
    err = calculate_partition_sha256(update_partition, binary_file_length, downloaded_sha256);
    if (err != ESP_OK) {
        failure_reason = NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
        goto cleanup;
    }
    print_sha256(downloaded_sha256, "SHA-256 for downloaded firmware");
    if (memcmp(downloaded_sha256, request.sha256, HASH_LEN) != 0) {
        ESP_LOGE(TAG, "Downloaded firmware SHA-256 does not match the OTA response");
        failure_reason = NATIVE_OTA_FAILURE_HASH_MISMATCH;
        terminal_failure = true;
        goto cleanup;
    }

    /* esp_ota_end() 无论成功失败都会释放句柄，调用后不能再 abort。 */
    err = esp_ota_end(update_handle);
    ota_handle_active = false;
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Image validation failed in esp_ota_end: %s", esp_err_to_name(err));
        failure_reason = NATIVE_OTA_FAILURE_IMAGE_VALIDATE_FAILED;
        terminal_failure = true;
        goto cleanup;
    }

    /* 先把“已校验但尚未切换”状态持久化，掉电后可再次核对并直接提交，避免重下镜像。 */
    record.phase = OTA_RESUME_PHASE_READY_TO_COMMIT;
    record.verified_offset = request.image_size;
    err = ota_state_store_save(&record);
    if (err != ESP_OK) {
        failure_reason = NATIVE_OTA_FAILURE_NVS_WRITE_FAILED;
        keep_record = true;
        goto cleanup;
    }
    failure_reason = ota_pre_commit_check(&request, update_partition);
    if (failure_reason != NATIVE_OTA_FAILURE_NONE) {
        keep_record = true;
        goto cleanup;
    }

    err = esp_ota_set_boot_partition(update_partition);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition failed: %s", esp_err_to_name(err));
        failure_reason = NATIVE_OTA_FAILURE_BOOT_PARTITION_SET_FAILED;
        keep_record = true;
        goto cleanup;
    }
    if (report_context_valid) {
        esp_err_t report_err = native_ota_report_event(&report_context,
                                                       NATIVE_OTA_REPORT_REBOOTING,
                                                       0, NATIVE_OTA_FAILURE_NONE);
        if (report_err != ESP_OK) {
            ESP_LOGW(TAG, "Could not enqueue OTA rebooting report: %s",
                     esp_err_to_name(report_err));
        }
    }
    reboot = true;

cleanup:
    /*
     * 统一释放顺序：先关闭 HTTP，再在句柄仍处于活动状态时 abort OTA，最后处理 NVS
     * 记录和任务占用标志。ota_handle_active 在 esp_ota_end() 后清零，避免对已结束句柄
     * 再次调用 esp_ota_abort()。
     */
    if (client != NULL) {
        if (client_open) {
            (void)esp_http_client_close(client);
        }
        (void)esp_http_client_cleanup(client);
    }
    if (ota_handle_active) {
        (void)esp_ota_abort(update_handle);
    }
    if (record_active && !reboot && !keep_record) {
        /* 终端故障隔离 artifact；网络故障只增加重试次数，成功重启则保留 READY 记录。 */
        if (terminal_failure) {
            ota_quarantine_record(&record, failure_reason);
        } else if (failure_reason != NATIVE_OTA_FAILURE_NONE &&
                   record.phase == OTA_RESUME_PHASE_DOWNLOADING) {
            if (record.retry_count != UINT32_MAX) {
                record.retry_count++;
            }
            record.failure_reason = (uint32_t)failure_reason;
            err = ota_state_store_save(&record);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to persist retry/cooldown diagnostics: %s",
                         esp_err_to_name(err));
            } else {
                ESP_LOGW(TAG, "Artifact %s network failure: failures=%" PRIu32
                         " threshold=%u cooldown_count=%" PRIu32,
                         record.artifact_id, record.retry_count,
                         OTA_STATE_STORE_NETWORK_RETRY_THRESHOLD, record.cooldown_count);
            }
        }
    }
    if (!reboot && failure_reason != NATIVE_OTA_FAILURE_NONE && report_context_valid) {
        native_ota_report_state_t report_state = ota_report_state_for_failure(failure_reason);
        esp_err_t report_err = native_ota_report_event(&report_context, report_state,
                                                       (uint32_t)binary_file_length,
                                                       failure_reason);
        if (report_err != ESP_OK) {
            ESP_LOGW(TAG, "Could not enqueue OTA %s report: %s",
                     native_ota_report_state_name(report_state),
                     esp_err_to_name(report_err));
        }
    }
    ota_clear_in_progress();
    if (failure_reason != NATIVE_OTA_FAILURE_NONE) {
        ESP_LOGE(TAG, "OTA task finished with reason=%s (%s)",
                 native_ota_failure_reason_name(failure_reason), esp_err_to_name(err));
    }
    if (reboot) {
        ESP_LOGI(TAG, "Prepare to restart system!");
        esp_restart();
    }
    vTaskDelete(NULL);
    while (1) {
        vTaskDelay(portMAX_DELAY);
    }
}

/**
 * @brief 对升级后首次启动的固件执行最小 GPIO 诊断。
 *
 * @return true 诊断 GPIO 等待 5 s 后为高电平，当前实现视为诊断通过。
 * @return false 诊断 GPIO 为低电平，当前实现视为诊断失败。
 *
 * 函数将配置的 GPIO 设置为输入并开启内部上拉，等待外部电路稳定后读取电平，最后恢复
 * GPIO 默认状态。它用于 OTA 的 PENDING_VERIFY 流程，执行期间阻塞约 5 s；外部硬件必须
 * 遵循“高电平表示通过”的约定。
 *
 * @note 只能在普通 FreeRTOS 任务上下文调用，不能在中断上下文调用。
 */
static bool diagnostic(void)
{
    /* 诊断输入使用上拉，避免外部信号悬空时读到不确定电平。 */
    gpio_config_t io_conf;
    io_conf.intr_type    = GPIO_INTR_DISABLE;
    io_conf.mode         = GPIO_MODE_INPUT;
    io_conf.pin_bit_mask = (1ULL << CONFIG_EXAMPLE_GPIO_DIAGNOSTIC);
    io_conf.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io_conf.pull_up_en   = GPIO_PULLUP_ENABLE;
    gpio_config(&io_conf);

    ESP_LOGI(TAG, "Diagnostics (5 sec)...");
    /* 给外部诊断电路留出稳定时间，避免刚重启时的瞬态导致误回滚。 */
    vTaskDelay(5000 / portTICK_PERIOD_MS);

    /* 当前实现把高电平作为诊断成功条件。 */
    bool diagnostic_is_ok = gpio_get_level(CONFIG_EXAMPLE_GPIO_DIAGNOSTIC);

    /* 诊断结束后释放 GPIO 配置，避免长期占用该引脚的输入/上拉设置。 */
    gpio_reset_pin(CONFIG_EXAMPLE_GPIO_DIAGNOSTIC);
    return diagnostic_is_ok;
}

/**
 * @brief 执行不依赖网络连接的本地基础设施和资源健康检查。
 *
 * @param[in] include_gpio_diagnostic true 时额外执行配置的 GPIO 诊断；通常仅在
 *                                    PENDING_VERIFY 启动阶段传入 true。
 * @return true 当前运行分区、物理 Flash、应用描述、堆空间和基础队列检查均通过。
 * @return false 任一必要检查失败，调用者不得继续确认 pending 镜像或启动通信。
 *
 * 检查顺序先确认运行/目标分区和物理 Flash 容量，再检查应用描述、产品配置、堆空间和
 * FreeRTOS 队列收发；最后按参数选择 GPIO 诊断。函数不连接 Wi-Fi 或 MQTT，
 * 从而不会把网络暂时不可用误判为镜像健康。
 *
 * @note 创建的测试队列会在函数返回前删除；GPIO 诊断可能阻塞约 5 s，不能在中断调用。
 */
static bool ota_local_health_check(bool include_gpio_diagnostic)
{
    return ota_boot_health_check(include_gpio_diagnostic, diagnostic);
}

/**
 * @brief 进入不启动通信客户端的 OTA 安全模式。
 *
 * @param[in] reason 进入安全模式的诊断原因，可为 NULL。
 *
 * @note 本函数不返回、不重启，也不擦除 NVS；通过每秒延时保持任务可调度，等待人工处理
 *       或外部复位。只能在普通任务上下文调用。
 */
static void __attribute__((noreturn)) ota_enter_safe_mode(const char *reason)
{
    ota_boot_health_enter_safe_mode(reason);
}

/**
 * @brief 读取最近一次被 bootloader 判定无效的镜像版本，用于回滚结果关联。
 *
 * @param[out] version 接收版本字符串的缓冲区；函数失败时写入空串。
 * @param[in] version_size 缓冲区容量，单位为字节，必须大于 0。
 * @return true 找到无效分区且成功读取非空版本。
 * @return false 参数无效、没有无效分区或分区描述读取失败。
 *
 * @note 只读 bootloader/分区描述信息，不修改 OTA 状态；调用方负责与持久化报告上下文
 *       比较版本后再决定是否上报 rolled_back。
 */
static bool ota_get_last_invalid_version(char *version, size_t version_size)
{
    if (version == NULL || version_size == 0) {
        return false;
    }
    version[0] = '\0';
    const esp_partition_t *invalid = esp_ota_get_last_invalid_partition();
    if (invalid == NULL) {
        return false;
    }
    esp_app_desc_t invalid_desc;
    if (esp_ota_get_partition_description(invalid, &invalid_desc) != ESP_OK ||
        invalid_desc.version[0] == '\0') {
        return false;
    }
    strncpy(version, invalid_desc.version, version_size - 1U);
    version[version_size - 1U] = '\0';
    return true;
}

/**
 * @brief ESP-IDF 应用入口，按 rollback 安全顺序初始化本地基础设施和通信。
 *
 * 顺序固定为：读取 PENDING_VERIFY、初始化 NVS/netif/event loop、本地自检、确认或回滚、
 * 清理已确认版本的恢复记录、连接网络、启动 MQTT。网络连通性不参与镜像验收，
 * 只有本地检查和 pending 镜像确认完成后才允许启动通信客户端。
 *
 * @note 该函数由 ESP-IDF 启动框架调用，不接收参数且不返回；失败路径进入安全模式或由
 *       ESP_ERROR_CHECK 触发异常处理。函数会初始化 NVS、网络接口和默认事件循环。
 */
void app_main(void)
{
    ESP_LOGI(TAG, "OTA example app_main start");

    bool pending_verify = false;
    esp_err_t err = ota_boot_health_begin(&pending_verify);
    if (err != ESP_OK) {
        ota_enter_safe_mode("cannot read OTA boot state");
    }

    err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        if (pending_verify) {
            ESP_LOGE(TAG, "NVS format is incompatible during PENDING_VERIFY; refusing to erase NVS");
            err = ota_boot_health_reject("NVS format incompatible");
            ota_enter_safe_mode(err == ESP_ERR_OTA_ROLLBACK_FAILED ?
                                "rollback unavailable after NVS failure" :
                                "rollback failed after NVS failure");
        }
        ESP_LOGW(TAG, "Erasing NVS because no image is pending verification");
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        ota_enter_safe_mode("NVS initialization failed");
    }
    ota_state_store_log_nvs_usage(TAG, "startup", ESP_OK);

    err = native_ota_report_init();
    if (err != ESP_OK) {
        /* 状态上报不可用不能阻断已有 OTA/回滚主流程。 */
        ESP_LOGW(TAG, "OTA report initialization failed: %s", esp_err_to_name(err));
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    if (pending_verify) {
        err = native_ota_report_boot_pending_verify();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Could not report booted_pending_verify: %s", esp_err_to_name(err));
        }
    }

    if (!ota_local_health_check(pending_verify)) {
        if (pending_verify) {
            native_ota_failure_reason_t rollback_reason =
                esp_ota_check_rollback_is_possible() ?
                NATIVE_OTA_FAILURE_BOOT_SELF_TEST_FAILED :
                NATIVE_OTA_FAILURE_ROLLBACK_UNAVAILABLE;
            err = native_ota_report_boot_rolled_back(rollback_reason);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "Could not report rolled_back: %s", esp_err_to_name(err));
            }
            err = ota_boot_health_reject("boot self-test failed");
            ota_enter_safe_mode(err == ESP_ERR_OTA_ROLLBACK_FAILED ?
                                "rollback unavailable after self-test" :
                                "rollback failed after self-test");
        }
        ota_enter_safe_mode("local health check failed");
    }

    /* A product override must finish critical local business initialization before a
     * PENDING_VERIFY image can become VALID. Remote connectivity is intentionally not
     * an acceptance condition. */
    if (pending_verify && !ota_boot_health_product_check()) {
        native_ota_failure_reason_t rollback_reason =
            esp_ota_check_rollback_is_possible() ?
            NATIVE_OTA_FAILURE_BOOT_SELF_TEST_FAILED :
            NATIVE_OTA_FAILURE_ROLLBACK_UNAVAILABLE;
        err = native_ota_report_boot_rolled_back(rollback_reason);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Could not report rolled_back after product health failure: %s",
                     esp_err_to_name(err));
        }
        err = ota_boot_health_reject("product boot health check failed");
        ota_enter_safe_mode(err == ESP_ERR_OTA_ROLLBACK_FAILED ?
                            "rollback unavailable after product health check" :
                            "rollback failed after product health check");
    }

    if (pending_verify) {
        err = ota_boot_health_confirm();
        if (err != ESP_OK) {
            native_ota_failure_reason_t rollback_reason =
                esp_ota_check_rollback_is_possible() ?
                NATIVE_OTA_FAILURE_BOOT_SELF_TEST_FAILED :
                NATIVE_OTA_FAILURE_ROLLBACK_UNAVAILABLE;
            (void)native_ota_report_boot_rolled_back(rollback_reason);
            err = ota_boot_health_reject("cannot confirm healthy image");
            ota_enter_safe_mode("cannot confirm or rollback image");
        }
        err = native_ota_report_boot_succeeded();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Could not report succeeded: %s", esp_err_to_name(err));
        }
    }

    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running != NULL) {
        esp_app_desc_t running_desc;
        if (esp_ota_get_partition_description(running, &running_desc) == ESP_OK) {
            ESP_LOGI(TAG, "Running firmware version: %s", running_desc.version);
            char last_invalid_version[sizeof(running_desc.version)] = { 0 };
            const char *invalid_version =
                ota_get_last_invalid_version(last_invalid_version, sizeof(last_invalid_version)) ?
                last_invalid_version : NULL;
            if (!pending_verify && invalid_version != NULL) {
                err = native_ota_report_reconcile_rollback(running_desc.version,
                                                           invalid_version);
                if (err != ESP_OK) {
                    ESP_LOGW(TAG, "Could not reconcile OTA rollback report: %s",
                             esp_err_to_name(err));
                }
            }
            err = ota_state_store_reconcile_running_version(running_desc.version);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "Failed to reconcile OTA resume record: %s", esp_err_to_name(err));
            }
        }
    }

    /* 语音服务装配：注册 MQTT 语音命令 topic（非 critical，不影响 OTA 就绪）。
     * WSS 客户端不在此启动：取得 IPv4 后由下方注册的 ip_ready 回调启动。 */
    err = voice_service_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Voice service init failed: %s", esp_err_to_name(err));
    }
    /* 板级音频（最小包 mic_test.c 抽取）：MIC 走 WSS 上行、SPKS/SPKE 下行控制。 */
    err = board_audio_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Board audio init failed: %s", esp_err_to_name(err));
    } else {
        err = voice_service_init_board_audio();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Voice-board audio wiring failed: %s", esp_err_to_name(err));
        }
    }
    /* 本地唤醒词（WakeNet "你好小智"）：检测到后自动 MIC_START 推流。
     * 依赖 "model" 分区（构建时 esp-sr 自动打包 srmodels.bin）。 */
    err = wake_detector_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Wake detector init failed: %s", esp_err_to_name(err));
    }
    /* 音频服务装配点（当前无自有状态，与 OTA 服务层保持一致的初始化顺序）。 */
    err = audio_service_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Audio service init failed: %s", esp_err_to_name(err));
    }

    /* SD 卡（SPI，FAT 挂载到 /sdcard）：供 voice_service 的 "SD:/<name>" 文件
     * 推送与显式传输演示使用；不依赖网络，挂载失败会自动重试。 */
    err = sd_card_start();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SD card monitor not started: %s", esp_err_to_name(err));
    }

    /* 取得 IPv4 后按注册顺序启动 MQTT 与 WSS 语音服务；任一启动失败都由网络
     * 生命周期任务按独立的有界退避重试，服务之间互不干扰。 */
    err = network_lifecycle_register_ip_ready(mqtt_comm_ip_ready, NULL);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "MQTT IP-ready callback registration failed: %s",
                 esp_err_to_name(err));
    }
    err = network_lifecycle_register_ip_ready(voice_service_ip_ready, NULL);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Voice IP-ready callback registration failed: %s",
                 esp_err_to_name(err));
    }

    /* 网络不可用绝不能阻断本地应用或影响 pending 镜像验收。Wi-Fi 管理器在后台
     * 永久重连；只有取得 IPv4 后才会调用已注册的服务启动回调。 */
    ESP_LOGI(TAG, "Starting background Wi-Fi lifecycle");
    err = network_lifecycle_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Background Wi-Fi lifecycle is unavailable: %s; local application remains active",
                 esp_err_to_name(err));
    }

    /* 设备主动推送演示（默认关闭）：WSS 会话默认由云端驱动（服务端发
     * FILE_SEND，设备推送）；启用演示后设备才会主动推测试音频列表。 */
#if CONFIG_VOICE_PUSH_DEMO_ENABLE
    err = voice_push_demo_start();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Voice push demo not started: %s", esp_err_to_name(err));
    }
#endif
}
