/**
 * @file    ota_control_plane.c
 * @brief   OTA 控制面请求、响应关联与清单校验实现。
 *
 * 该文件从主 OTA 例程中抽离项目新增的 MQTT 控制面逻辑。它不参与
 * ESP-IDF 官方 HTTP 下载循环，所有输出清单均由主程序负责调度为 OTA 任务。
 *
 * 模块关系：
 * - 从 mqtt_comm.c 接收一条已经完成 MQTT 分片重组的 JSON 响应；
 * - 使用 native_ota_get_device_id() 和 Kconfig 身份配置校验响应归属；
 * - 查询 ota_state_store.c，拒绝已经被隔离的 artifact；
 * - 向 native_ota_example.c 输出固定大小的 native_ota_manifest_t。
 *
 * 线程安全与限制：
 * - 最近 request_id 由短临界区保护，避免检查任务写入时被响应事件读取到半个 ID；
 * - JSON 解析和 cJSON 分配只能在普通任务/事件任务上下文运行，不允许中断调用；
 * - 本模块不写 Flash、不创建下载任务，也不等待 HTTP 或 OTA 完成。
 */
#include "ota_control_plane.h"

#include <inttypes.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>

#include "freertos/FreeRTOS.h"

#include "esp_app_desc.h"
#include "esp_efuse.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"

#include "cJSON.h"

#include "ota_state_store.h"

/** 本模块日志标签。 */
static const char *TAG = "ota_control_plane";

/** SHA-256 文本采用两个十六进制字符表示一个字节。 */
#define OTA_CONTROL_PLANE_SHA256_HEX_LEN (NATIVE_OTA_SHA256_SIZE * 2U)

/** OTA application versions use exactly three numeric components: major.minor.patch. */
#define OTA_CONTROL_PLANE_VERSION_PARTS 3U

/** Parsed numeric application version used for precedence comparison. */
typedef struct {
    uint32_t part[OTA_CONTROL_PLANE_VERSION_PARTS];
} ota_control_plane_version_t;

/** 保护最近 request_id 的短临界区锁。 */
static portMUX_TYPE s_request_id_lock = portMUX_INITIALIZER_UNLOCKED;

/** 最近一次主动检查使用的 request_id，服务器响应必须严格匹配。 */
static char s_last_request_id[NATIVE_OTA_REQUEST_ID_SIZE];

/**
 * @brief 保存最近一次 OTA 检查请求的 request_id。
 *
 * @param[in] request_id NUL 结尾请求 ID，不允许为 NULL。
 *
 * @note 仅在临界区内复制固定长度字符串，不执行阻塞操作。
 */
static void ota_control_plane_set_last_request_id(const char *request_id)
{
    /* 只保护固定长度的关联 ID；锁内不执行 cJSON、日志或任何可能阻塞的操作。 */
    portENTER_CRITICAL(&s_request_id_lock);
    strncpy(s_last_request_id, request_id, sizeof(s_last_request_id) - 1U);
    s_last_request_id[sizeof(s_last_request_id) - 1U] = '\0';
    portEXIT_CRITICAL(&s_request_id_lock);
}

/**
 * @brief 判断响应中的 request_id 是否对应最近一次主动检查。
 *
 * @param[in] request_id NUL 结尾响应 ID，可为 NULL。
 * @return true ID 存在且完全匹配。
 * @return false ID 为空、尚未发出请求或不匹配。
 */
static bool ota_control_plane_request_id_matches(const char *request_id)
{
    bool matches;

    /* 在同一临界区内完成“读取最近 ID + 比较”，避免请求轮换造成错误关联。 */
    portENTER_CRITICAL(&s_request_id_lock);
    matches = request_id != NULL && s_last_request_id[0] != '\0' &&
              strcmp(request_id, s_last_request_id) == 0;
    portEXIT_CRITICAL(&s_request_id_lock);
    return matches;
}

/**
 * @brief 将一个十六进制字符转换为 0～15 的半字节数值。
 *
 * @param[in] value ASCII 字符。
 * @return 0～15 转换成功。
 * @return -1 不是合法十六进制字符。
 */
static int ota_control_plane_hex_to_nibble(char value)
{
    if (value >= '0' && value <= '9') {
        return value - '0';
    }
    if (value >= 'a' && value <= 'f') {
        return value - 'a' + 10;
    }
    if (value >= 'A' && value <= 'F') {
        return value - 'A' + 10;
    }
    return -1;
}

/**
 * @brief 将 SHA-256 十六进制文本转换为原始摘要。
 *
 * @param[in]  text   64 字符 NUL 结尾文本，可为 NULL。
 * @param[out] output 接收摘要的缓冲区，不允许为 NULL。
 * @return true 格式有效且转换成功。
 * @return false 长度或字符无效。
 */
static bool ota_control_plane_parse_sha256(const char *text,
                                           uint8_t output[NATIVE_OTA_SHA256_SIZE])
{
    if (text == NULL || output == NULL || strlen(text) != OTA_CONTROL_PLANE_SHA256_HEX_LEN) {
        return false;
    }

    /* 每两个文本字符还原一个摘要字节；任一半字节非法都拒绝整条清单。 */
    for (size_t i = 0; i < NATIVE_OTA_SHA256_SIZE; ++i) {
        int high = ota_control_plane_hex_to_nibble(text[i * 2U]);
        int low = ota_control_plane_hex_to_nibble(text[i * 2U + 1U]);
        if (high < 0 || low < 0) {
            return false;
        }
        output[i] = (uint8_t)((high << 4) | low);
    }
    return true;
}

/**
 * @brief 将 JSON 字符串复制到固定容量字段。
 *
 * @param[in]  item        cJSON 字符串节点，可为 NULL。
 * @param[out] output      目标缓冲区，不允许为 NULL。
 * @param[in]  output_size 缓冲区容量，包含 NUL。
 * @return true 已完整复制。
 * @return false 节点或容量无效。
 */
static bool ota_control_plane_copy_string(const cJSON *item, char *output, size_t output_size)
{
    if (!cJSON_IsString(item) || item->valuestring == NULL || output == NULL ||
        output_size == 0U) {
        return false;
    }

    /* 固定字段必须完整放入目标结构体，截断会破坏身份匹配和后续 URL 校验。 */
    size_t length = strlen(item->valuestring);
    if (length == 0U || length >= output_size) {
        return false;
    }

    memcpy(output, item->valuestring, length + 1U);
    return true;
}

/**
 * @brief 解析非负 32 位整数 JSON 字段。
 *
 * @param[in]  item  cJSON 数字节点，可为 NULL。
 * @param[out] value 接收数值，不允许为 NULL。
 * @return true 数值为范围内整数。
 * @return false 类型、小数、范围或参数无效。
 */
static bool ota_control_plane_get_u32(const cJSON *item, uint32_t *value)
{
    /* cJSON 数字以 double 保存，因此额外检查整数性和 UINT32_MAX 边界再转换。 */
    if (!cJSON_IsNumber(item) || value == NULL || item->valuedouble < 0.0 ||
        item->valuedouble > (double)UINT32_MAX ||
        item->valuedouble != floor(item->valuedouble)) {
        return false;
    }

    *value = (uint32_t)item->valuedouble;
    return true;
}

/**
 * @brief 解析可表示 Unix 时间戳的非负 64 位 JSON 整数。
 *
 * @param[in]  item  cJSON 数字节点，可为 NULL。
 * @param[out] value 接收时间戳，单位为秒，不允许为 NULL。
 * @return true 数值可安全转换为 int64_t。
 * @return false 类型、小数、范围或参数无效。
 */
static bool ota_control_plane_get_i64(const cJSON *item, int64_t *value)
{
    /* JSON 数字同样以 double 表示；时间戳只接受非负、无小数且可转换为 int64_t 的值。 */
    const double int64_limit = 9223372036854775808.0;

    if (!cJSON_IsNumber(item) || value == NULL || item->valuedouble < 0.0 ||
        item->valuedouble >= int64_limit || item->valuedouble != floor(item->valuedouble)) {
        return false;
    }

    *value = (int64_t)item->valuedouble;
    return true;
}

/**
 * @brief Parse an exact three-part numeric version such as 1.0.0.
 *
 * Each component is parsed independently as uint32_t. Empty components,
 * signs, suffixes, extra components and integer overflow are rejected.
 */
static bool ota_control_plane_parse_version(const char *text,
                                            ota_control_plane_version_t *version)
{
    if (text == NULL || version == NULL || text[0] == '\0') {
        return false;
    }

    const char *cursor = text;
    for (size_t index = 0; index < OTA_CONTROL_PLANE_VERSION_PARTS; ++index) {
        if (*cursor < '0' || *cursor > '9') {
            return false;
        }

        uint32_t value = 0;
        do {
            uint32_t digit = (uint32_t)(*cursor - '0');
            if (value > (UINT32_MAX - digit) / 10U) {
                return false;
            }
            value = value * 10U + digit;
            ++cursor;
        } while (*cursor >= '0' && *cursor <= '9');

        version->part[index] = value;
        if (index + 1U < OTA_CONTROL_PLANE_VERSION_PARTS) {
            if (*cursor != '.') {
                return false;
            }
            ++cursor;
        } else if (*cursor != '\0') {
            return false;
        }
    }
    return true;
}

/** Return -1, 0 or 1 by major, then minor, then patch precedence. */
static int ota_control_plane_compare_version(const ota_control_plane_version_t *left,
                                             const ota_control_plane_version_t *right)
{
    for (size_t index = 0; index < OTA_CONTROL_PLANE_VERSION_PARTS; ++index) {
        if (left->part[index] < right->part[index]) {
            return -1;
        }
        if (left->part[index] > right->part[index]) {
            return 1;
        }
    }
    return 0;
}

/**
 * @brief 判断 HTTPS URL 主机名是否属于精确允许列表。
 *
 * @param[in] url 待验证 URL，必须以 https:// 开头。
 * @return true URL 合法且主机在允许列表中；空列表仅为开发配置并放行。
 * @return false URL 或主机不合法。
 */
static bool ota_control_plane_url_host_allowed(const char *url)
{
    static const char *scheme = "https://";

    if (url == NULL || strncmp(url, scheme, strlen(scheme)) != 0) {
        return false;
    }

    /* 端口、路径、查询和片段都不属于主机名；允许列表比较必须停在这些分隔符之前。 */
    const char *host_start = url + strlen(scheme);
    /* host_len 是主机名文本长度，单位为字节；它不包含 URL 分隔符。 */
    size_t host_len = strcspn(host_start, "/?#:");
    if (host_len == 0U) {
        return false;
    }

    const char *allowlist = CONFIG_OTA_ALLOWED_URL_HOSTS;
    if (allowlist[0] == '\0') {
        ESP_LOGW(TAG, "OTA URL host allowlist is empty; enable it for production builds");
        return true;
    }

    /* 允许列表是逗号分隔文本，逐项去除空格后做精确主机名匹配，不允许后缀模糊匹配。 */
    const char *cursor = allowlist;
    while (*cursor != '\0') {
        while (*cursor == ',' || *cursor == ' ' || *cursor == '\t') {
            ++cursor;
        }

        const char *token_start = cursor;
        while (*cursor != '\0' && *cursor != ',') {
            ++cursor;
        }
        const char *token_end = cursor;
        while (token_end > token_start &&
               (token_end[-1] == ' ' || token_end[-1] == '\t')) {
            --token_end;
        }
        if ((size_t)(token_end - token_start) == host_len &&
            strncasecmp(token_start, host_start, host_len) == 0) {
            return true;
        }
        if (*cursor == ',') {
            ++cursor;
        }
    }

    return false;
}

/**
 * @brief 获取由基础 MAC 地址生成的稳定设备标识。
 *
 * @param[out] device_id      接收 NUL 结尾标识的缓冲区。
 * @param[in]  device_id_size 缓冲区容量，单位为字节。
 * @return ESP_OK 标识生成成功。
 * @return ESP_ERR_INVALID_ARG 参数无效或缓冲区过小。
 * @return 其他 esp_err_t eFuse 读取失败。
 */
esp_err_t native_ota_get_device_id(char *device_id, size_t device_id_size)
{
    if (device_id == NULL || device_id_size < NATIVE_OTA_DEVICE_ID_SIZE) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 基础 MAC 是设备生命周期内稳定的硬件身份，不使用会变化的网络接口地址。 */
    uint8_t mac[6];
    esp_err_t err = esp_efuse_mac_get_default(mac);
    if (err != ESP_OK) {
        return err;
    }

    int written = snprintf(device_id, device_id_size,
                           "esp-%02x%02x%02x%02x%02x%02x",
                           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return written > 0 && (size_t)written < device_id_size ? ESP_OK : ESP_ERR_INVALID_ARG;
}

/**
 * @brief 生成 OTA 主动版本检查请求。
 *
 * @param[out] json      接收 NUL 结尾 JSON 的缓冲区。
 * @param[in]  json_size 缓冲区容量，单位为字节。
 * @param[out] json_len  接收 JSON 有效长度，不含末尾 NUL。
 * @return ESP_OK 请求生成成功。
 * @return ESP_ERR_INVALID_ARG 参数或输出容量无效。
 * @return ESP_ERR_NO_MEM cJSON 临时对象创建失败。
 * @return 其他 esp_err_t 设备标识读取失败。
 */
esp_err_t native_ota_build_check_request(char *json, size_t json_size, size_t *json_len)
{
    if (json == NULL || json_size == 0U || json_len == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    char device_id[NATIVE_OTA_DEVICE_ID_SIZE];
    esp_err_t err = native_ota_get_device_id(device_id, sizeof(device_id));
    if (err != ESP_OK) {
        return err;
    }

    /* 32 位随机数编码为 8 个小写十六进制字符，足够作为本次请求的短期关联 ID。 */
    char request_id[9];
    snprintf(request_id, sizeof(request_id), "%08" PRIx32, esp_random());

    /* 请求携带当前镜像版本，让服务器决定是否升级；设备仍会在响应侧再次拦截同版本。 */
    const esp_app_desc_t *app_desc = esp_app_get_description();
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }

    bool added = cJSON_AddStringToObject(root, "type", "ota_check") != NULL &&
                 cJSON_AddStringToObject(root, "request_id", request_id) != NULL &&
                 cJSON_AddStringToObject(root, "device_id", device_id) != NULL &&
                 cJSON_AddStringToObject(root, "product", CONFIG_OTA_PRODUCT_ID) != NULL &&
                 cJSON_AddStringToObject(root, "hardware_version", CONFIG_OTA_HARDWARE_VERSION) != NULL &&
                 cJSON_AddStringToObject(root, "current_version", app_desc->version) != NULL;
    if (!added) {
        cJSON_Delete(root);
        return ESP_ERR_NO_MEM;
    }

    if (!cJSON_PrintPreallocated(root, json, json_size, false)) {
        cJSON_Delete(root);
        return ESP_ERR_INVALID_ARG;
    }

    *json_len = strlen(json);
    ota_control_plane_set_last_request_id(request_id);
    cJSON_Delete(root);
    return ESP_OK;
}

/**
 * @brief 将服务器响应转换为可由 OTA 下载任务持有的已校验清单。
 *
 * @param[in] json 原始 JSON 数据，不要求 NUL 结尾。
 * @param[in] json_len 有效数据长度，单位为字节。
 * @param[out] manifest 接收深拷贝清单。
 * @param[out] download_requested 接收是否应调度下载的结果。
 * @return ESP_OK 响应有效；其他值表示协议、身份、有效期或隔离状态失败。
 *
 * @note 不分配输出清单；内部 cJSON 临时对象在返回前释放，不启动 OTA 任务。
 */
esp_err_t ota_control_plane_parse_server_response(const char *json, size_t json_len,
                                                  native_ota_manifest_t *manifest,
                                                  bool *download_requested)
{
    if (json == NULL || json_len == 0U || json_len > NATIVE_OTA_JSON_MAX_LEN ||
        manifest == NULL || download_requested == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    *download_requested = false;
    memset(manifest, 0, sizeof(*manifest));

    /* 统一从 cleanup 释放 cJSON；任何身份或清单失败都会清零输出，防止调用方误用半成品。 */
    esp_err_t result = ESP_OK;
    cJSON *root = cJSON_ParseWithLength(json, json_len);
    if (root == NULL || !cJSON_IsObject(root)) {
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    /* 先取出控制面身份字段，后续所有下载字段都建立在这组关联校验成功之上。 */
    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    const cJSON *update = cJSON_GetObjectItemCaseSensitive(root, "update");
    const cJSON *request_id = cJSON_GetObjectItemCaseSensitive(root, "request_id");
    const cJSON *device_id = cJSON_GetObjectItemCaseSensitive(root, "device_id");
    const cJSON *product = cJSON_GetObjectItemCaseSensitive(root, "product");
    const cJSON *hardware_version = cJSON_GetObjectItemCaseSensitive(root, "hardware_version");
    const cJSON *job_id = cJSON_GetObjectItemCaseSensitive(root, "job_id");
    if (!cJSON_IsString(type) ||
        (strcmp(type->valuestring, "ota_check_response") != 0 &&
         strcmp(type->valuestring, "ota") != 0) ||
        !cJSON_IsBool(update) || !cJSON_IsString(request_id) ||
        !cJSON_IsString(device_id) || !cJSON_IsString(product) ||
        !cJSON_IsString(hardware_version) ||
        (job_id != NULL && !cJSON_IsString(job_id))) {
        ESP_LOGW(TAG, "OTA response envelope has missing or invalid type/update/identity fields");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    /* 复制 request_id 后再进入临界区比较，避免依赖 cJSON 节点的短生命周期。 */
    char response_request_id[NATIVE_OTA_REQUEST_ID_SIZE];
    if (!ota_control_plane_copy_string(request_id, response_request_id,
                                       sizeof(response_request_id)) ||
        !ota_control_plane_request_id_matches(response_request_id)) {
        ESP_LOGW(TAG, "Ignoring OTA response with stale or mismatched request_id");
        result = ESP_ERR_INVALID_STATE;
        goto cleanup;
    }

    char expected_device_id[NATIVE_OTA_DEVICE_ID_SIZE];
    if (native_ota_get_device_id(expected_device_id, sizeof(expected_device_id)) != ESP_OK ||
        strcmp(device_id->valuestring, expected_device_id) != 0) {
        ESP_LOGW(TAG, "OTA response device_id does not match this device");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (strcmp(product->valuestring, CONFIG_OTA_PRODUCT_ID) != 0 ||
        strcmp(hardware_version->valuestring, CONFIG_OTA_HARDWARE_VERSION) != 0) {
        ESP_LOGW(TAG, "OTA response product or hardware_version does not match");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    /* update=false 是合法的“无需升级”结果，不要求服务器提供清单字段。 */
    if (!cJSON_IsTrue(update)) {
        ESP_LOGI(TAG, "OTA server reports that no update is required");
        goto cleanup;
    }

    memcpy(manifest->request_id, response_request_id, sizeof(manifest->request_id));
    if (job_id != NULL &&
        !ota_control_plane_copy_string(job_id, manifest->job_id, sizeof(manifest->job_id))) {
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    /* update=true 时一次性校验完整清单，任何字段失败都不会输出可下载 manifest。 */
    const cJSON *artifact_id = cJSON_GetObjectItemCaseSensitive(root, "artifact_id");
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(root, "version");
    const cJSON *url = cJSON_GetObjectItemCaseSensitive(root, "url");
    const cJSON *sha256 = cJSON_GetObjectItemCaseSensitive(root, "sha256");
    const cJSON *image_size = cJSON_GetObjectItemCaseSensitive(root, "image_size");
    const cJSON *security_version = cJSON_GetObjectItemCaseSensitive(root, "security_version");
    const cJSON *expires_at = cJSON_GetObjectItemCaseSensitive(root, "expires_at");
    if (!ota_control_plane_copy_string(artifact_id, manifest->artifact_id,
                                       sizeof(manifest->artifact_id))) {
        ESP_LOGW(TAG, "OTA manifest field artifact_id is missing, empty or too long");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!ota_control_plane_copy_string(version, manifest->version, sizeof(manifest->version))) {
        ESP_LOGW(TAG, "OTA manifest field version is missing, empty or too long");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!ota_control_plane_copy_string(url, manifest->url, sizeof(manifest->url)) ||
        !ota_control_plane_url_host_allowed(manifest->url)) {
        ESP_LOGW(TAG, "OTA manifest URL is missing, too long, non-HTTPS or not allowed");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!ota_control_plane_copy_string(product, manifest->product,
                                       sizeof(manifest->product)) ||
        !ota_control_plane_copy_string(hardware_version, manifest->hardware_version,
                                       sizeof(manifest->hardware_version))) {
        ESP_LOGW(TAG, "OTA manifest product identity fields cannot be copied");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!ota_control_plane_parse_sha256(cJSON_IsString(sha256) ? sha256->valuestring : NULL,
                                        manifest->sha256)) {
        ESP_LOGW(TAG, "OTA manifest SHA-256 must contain exactly 64 hexadecimal characters");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!ota_control_plane_get_u32(image_size, &manifest->image_size) ||
        manifest->image_size == 0U) {
        ESP_LOGW(TAG, "OTA manifest image_size must be a positive uint32 integer");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!ota_control_plane_get_u32(security_version, &manifest->security_version)) {
        ESP_LOGW(TAG, "OTA manifest security_version must be a non-negative uint32 integer");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!ota_control_plane_get_i64(expires_at, &manifest->expires_at)) {
        ESP_LOGW(TAG, "OTA manifest expires_at must be a non-negative integer timestamp");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    /* force_update 为可选布尔字段，缺失视为 false；出现但非布尔值是协议错误，严格拒绝。 */
    const cJSON *force_update = cJSON_GetObjectItemCaseSensitive(root, "force_update");
    if (force_update != NULL && !cJSON_IsBool(force_update)) {
        ESP_LOGW(TAG, "OTA manifest force_update must be boolean when present");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    manifest->force_update = (force_update != NULL) && cJSON_IsTrue(force_update);

    /* 设备时间尚未同步时 now 可能不可用；此时跳过过期比较，但不放宽字段格式校验。 */
    /* now 的单位为 Unix 秒；时间无效/尚未同步时通常返回非正值。 */
    time_t now = time(NULL);
    if (now > 0 && manifest->expires_at <= (int64_t)now) {
        ESP_LOGW(TAG, "OTA artifact %s has expired", manifest->artifact_id);
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    const esp_app_desc_t *running_app = esp_app_get_description();
    ota_control_plane_version_t target_version;
    ota_control_plane_version_t current_version;
    if (!ota_control_plane_parse_version(manifest->version, &target_version) ||
        !ota_control_plane_parse_version(running_app->version, &current_version)) {
        ESP_LOGW(TAG, "OTA versions must use major.minor.patch numeric format: target=%s current=%s",
                 manifest->version, running_app->version);
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

#ifndef CONFIG_EXAMPLE_SKIP_VERSION_CHECK
    int version_order = ota_control_plane_compare_version(&target_version, &current_version);
    if (version_order <= 0 && !manifest->force_update) {
        ESP_LOGI(TAG, "Target version %s is not newer than running version %s; skipping firmware download",
                 manifest->version, running_app->version);
        goto cleanup;
    }
    if (version_order <= 0) {
        ESP_LOGW(TAG, "Forced update: version check bypassed (target %s, running %s)",
                 manifest->version, running_app->version);
    }
#endif

    /* 隔离状态按 artifact、版本、URL、大小和摘要匹配，避免误伤新的发布物。 */
    ota_resume_record_t previous_record;
    if (ota_state_store_load(&previous_record) == ESP_OK &&
        previous_record.phase == OTA_RESUME_PHASE_QUARANTINED &&
        ota_state_store_matches_manifest(&previous_record, manifest)) {
        ESP_LOGW(TAG, "Artifact %s is quarantined after a terminal validation failure",
                 manifest->artifact_id);
        result = ESP_ERR_INVALID_STATE;
        goto cleanup;
    }

    *download_requested = true;

cleanup:
    cJSON_Delete(root);
    if (result != ESP_OK) {
        memset(manifest, 0, sizeof(*manifest));
        *download_requested = false;
    }
    return result;
}
