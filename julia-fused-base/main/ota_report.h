/**
 * @file    ota_report.h
 * @brief   与通信协议解耦的 OTA 生命周期事件和进度上报接口。
 *
 * OTA 主任务只构建统一事件并交给本模块。具体 MQTT 队列和
 * PUBACK 关联由通信模块注册 transport 实现，因此下载任务不会直接调用 MQTT。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "native_ota_example.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 128-bit event_id 的小写十六进制文本长度，包含末尾 NUL。 */
#define NATIVE_OTA_EVENT_ID_SIZE 33

/** OTA 状态名称最大长度，包含末尾 NUL。 */
#define NATIVE_OTA_REPORT_STATE_SIZE 32

/** 单条状态 JSON 的固定容量，包含末尾 NUL。 */
#define NATIVE_OTA_STATUS_JSON_SIZE 1024

/** 固定为 8 条的关键事件持久化队列深度；普通进度不写入该队列。 */
#define NATIVE_OTA_REPORT_PENDING_MAX 8

/**
 * @brief OTA 生命周期状态。
 *
 * 这些值会被序列化为状态 JSON 的 `state` 字段；它们描述的是设备已经
 * 到达的生命周期阶段，不等同于 ota_state_store 中用于断点恢复的阶段。
 */
typedef enum {
    NATIVE_OTA_REPORT_ACCEPTED = 0, /**< 清单已校验并已创建 OTA 任务。 */
    NATIVE_OTA_REPORT_DOWNLOADING, /**< 已开始下载或正在上报下载进度。 */
    NATIVE_OTA_REPORT_VERIFYING, /**< 下载完成，正在校验镜像和摘要。 */
    NATIVE_OTA_REPORT_REBOOTING, /**< 已设置下次启动分区，设备即将重启。 */
    NATIVE_OTA_REPORT_BOOTED_PENDING_VERIFY, /**< 新镜像已启动，仍等待本地验收。 */
    NATIVE_OTA_REPORT_SUCCEEDED, /**< 本地健康检查通过且镜像已确认有效。 */
    NATIVE_OTA_REPORT_FAILED, /**< 本次 OTA 因终端错误失败。 */
    NATIVE_OTA_REPORT_ROLLED_BACK, /**< 新镜像无效，设备已回滚或识别到回滚。 */
    NATIVE_OTA_REPORT_DEFERRED, /**< 提交前条件暂不满足，保留记录等待后续处理。 */
} native_ota_report_state_t;

/**
 * @brief 一次 OTA 任务的稳定关联上下文。
 *
 * 该结构不包含 URL、证书或固件内容；它只用于生成生命周期事件和跨重启恢复。
 */
typedef struct {
    char job_id[NATIVE_OTA_JOB_ID_SIZE]; /**< 可选云端任务 ID；未提供时为空串。 */
    char request_id[NATIVE_OTA_REQUEST_ID_SIZE]; /**< 触发本次升级的检查请求 ID。 */
    char artifact_id[NATIVE_OTA_ARTIFACT_ID_SIZE]; /**< 固件发布物唯一 ID。 */
    char product[NATIVE_OTA_PRODUCT_ID_SIZE]; /**< 本地配置确认过的产品标识。 */
    char hardware_version[NATIVE_OTA_HARDWARE_VERSION_SIZE]; /**< 本地配置确认过的硬件版本。 */
    char current_version[sizeof(((esp_app_desc_t *)0)->version)]; /**< 重启前运行的版本。 */
    char target_version[sizeof(((esp_app_desc_t *)0)->version)]; /**< 待安装镜像版本。 */
    uint32_t image_size; /**< 清单声明的镜像大小，单位为字节。 */
    uint32_t attempt; /**< 本 artifact 的下载尝试序号，从 1 开始。 */
} native_ota_report_context_t;

/**
 * @brief 已序列化的单条事件。
 *
 * transport 必须在返回前复制需要的数据；调用者不会保证 message 的生命周期。
 */
typedef struct {
    bool critical; /**< true 表示必须持久化并等待 QoS 1 PUBACK。 */
    native_ota_report_state_t state; /**< JSON 中对应的 OTA 生命周期状态。 */
    char event_id[NATIVE_OTA_EVENT_ID_SIZE]; /**< 本条事件的幂等关联 ID。 */
    size_t json_len; /**< JSON 有效长度，不包含末尾 NUL，单位为字节。 */
    char json[NATIVE_OTA_STATUS_JSON_SIZE]; /**< 已序列化的 NUL 结尾状态 JSON。 */
} native_ota_report_message_t;

/**
 * @brief 通信实现使用的非阻塞事件提交回调。
 *
 * 回调必须在返回前复制所需内容，且不应等待网络发送完成；返回失败只表示当前
 * transport 没有接收成功，关键事件仍由报告模块保留在 NVS 中。
 */
typedef esp_err_t (*native_ota_report_transport_t)(
    const native_ota_report_message_t *message, void *context);

/**
 * @brief 初始化事件持久化镜像和 PUBACK 处理任务。
 *
 * @return ESP_OK 已初始化或此前已初始化。
 * @return ESP_ERR_NO_MEM FreeRTOS 同步对象或确认任务创建失败。
 * @return 其他 esp_err_t 读取 NVS 状态失败；该错误不会自动擦除已有状态。
 *
 * @note 必须在 nvs_flash_init() 成功后、注册 transport 前调用；不能在中断中调用。
 */
esp_err_t native_ota_report_init(void);

/**
 * @brief 注册或清除通信 transport。
 *
 * @param[in] transport 非阻塞事件提交回调；传入 NULL 暂时禁用发送。
 * @param[in] context 原样传给 transport 的上下文指针，可为 NULL。
 * @return ESP_OK 注册成功。
 * @return ESP_ERR_INVALID_STATE 报告模块尚未初始化。
 * @return ESP_ERR_TIMEOUT 在规定时间内无法取得内部锁。
 *
 * @note 清除 transport 不会删除 NVS 中的关键事件。
 */
esp_err_t native_ota_report_set_transport(native_ota_report_transport_t transport,
                                           void *context);

/**
 * @brief 从已校验 manifest 和当前运行版本构建报告上下文。
 *
 * @param[out] context 输出一次 OTA 任务的稳定关联上下文，不允许为 NULL。
 * @param[in] manifest 已通过控制面校验的清单，不允许为 NULL。
 * @param[in] current_version 当前运行镜像版本字符串，不允许为空。
 * @return ESP_OK 上下文复制成功。
 * @return ESP_ERR_INVALID_ARG 参数缺失或清单关键字段为空。
 *
 * @note 只写入调用者内存，不访问 NVS，也不创建任务。
 */
esp_err_t native_ota_report_context_init(native_ota_report_context_t *context,
                                          const native_ota_manifest_t *manifest,
                                          const char *current_version);

/**
 * @brief 返回状态的稳定协议名称。
 *
 * @param[in] state 生命周期状态枚举值。
 * @return 静态只读字符串；未知值返回 `unknown`。
 */
const char *native_ota_report_state_name(native_ota_report_state_t state);

/**
 * @brief 生成并提交一条生命周期状态事件。
 *
 * 关键状态会先写入有限的 NVS 待发送队列，再调用非阻塞 transport。上报失败不会
 * 改变 OTA 主流程返回值。
 */
esp_err_t native_ota_report_event(const native_ota_report_context_t *context,
                                  native_ota_report_state_t state,
                                  uint32_t bytes_downloaded,
                                  native_ota_failure_reason_t failure_reason);

/**
 * @brief 按百分比/时间策略提交下载进度。
 *
 * 进度只保存在 RAM，满足步长、时间间隔或 100% 任一条件时才提交。
 */
esp_err_t native_ota_report_progress(const native_ota_report_context_t *context,
                                     uint32_t bytes_downloaded);

/**
 * @brief 将持久化关键事件和 RAM 中最新进度交给当前 transport。
 *
 * @return ESP_OK 全部当前可发送内容已交给 transport，或没有待发送内容。
 * @return ESP_ERR_NOT_SUPPORTED 尚未注册 transport。
 * @return 其他 esp_err_t transport 或内部锁操作失败。
 *
 * @note 本函数不会执行 NVS 写入；通常由 MQTT 连接/订阅就绪路径调用。
 */
esp_err_t native_ota_report_flush_pending(void);

/**
 * @brief 记录一条已收到 QoS 1 PUBACK 的事件 ID。
 *
 * @param[in] event_id 已确认事件的 NUL 结尾 ID，长度必须小于
 *                     NATIVE_OTA_EVENT_ID_SIZE。
 *
 * @note 函数只向有界确认队列入队，不在 MQTT 事件回调中写 Flash；队列满时保留 NVS 记录。
 */
void native_ota_report_ack_event(const char *event_id);

/**
 * @brief 新镜像处于 PENDING_VERIFY 时生成 booted_pending_verify 事件。
 *
 * @return ESP_OK 已生成事件，或当前没有跨重启待验收上下文。
 * @return 其他 esp_err_t 报告构建、持久化或 transport 操作失败。
 *
 * @note 必须在 NVS 初始化和报告模块初始化完成后、网络启动前调用。
 */
esp_err_t native_ota_report_boot_pending_verify(void);

/**
 * @brief 本地自检通过并确认镜像后生成 succeeded 事件。
 *
 * @return ESP_OK 已生成事件，或当前没有跨重启待验收上下文。
 * @return 其他 esp_err_t 报告构建、持久化或 transport 操作失败。
 */
esp_err_t native_ota_report_boot_succeeded(void);

/**
 * @brief 自检失败或检测到回滚时生成 rolled_back 事件。
 *
 * @param[in] failure_reason 回滚原因；会序列化为稳定的 error_code 字段。
 * @return ESP_OK 已生成事件，或当前没有跨重启待验收上下文。
 * @return 其他 esp_err_t 报告构建、持久化或 transport 操作失败。
 */
esp_err_t native_ota_report_boot_rolled_back(native_ota_failure_reason_t failure_reason);

/**
 * @brief 在非 PENDING_VERIFY 启动中识别上一次失败升级的回滚。
 *
 * last_invalid_version 应来自 esp_ota_get_last_invalid_partition()；只有它与持久化
 * 目标版本匹配且当前运行版本不同，才会生成 rolled_back。
 *
 * @param[in] running_version 当前实际运行镜像版本，不允许为 NULL。
 * @param[in] last_invalid_version bootloader 最近判定无效的镜像版本，不允许为 NULL。
 * @return ESP_OK 已完成对账、没有匹配的回滚上下文，或已生成回滚事件。
 * @return 其他 esp_err_t 内部锁或报告持久化/提交失败。
 */
esp_err_t native_ota_report_reconcile_rollback(const char *running_version,
                                                const char *last_invalid_version);

#ifdef __cplusplus
}
#endif
