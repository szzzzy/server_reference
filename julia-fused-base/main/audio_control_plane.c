/**
 * @file    audio_control_plane.c
 * @brief   音频控制面请求、响应关联与清单校验实现。
 *
 * 与 ota_control_plane.c 平行：负责音频检查请求的生成与 audio_check_response
 * 的解析校验。工具函数（十六进制/SHA-256 文本/定长复制/数值解析/URL 白名单）
 * 与 OTA 控制面同规则，保持独立副本以维持模块边界。
 *
 * 线程安全与限制：
 * - 最近 request_id 由短临界区保护；
 * - JSON 解析和 cJSON 分配只能在普通任务/事件任务上下文运行；
 * - 本模块不写 Flash、不创建下载任务，也不等待 HTTP 下载完成。
 */
#include "audio_control_plane.h"

#include <inttypes.h>
#include <math.h>
#include <string.h>
#include <strings.h>
#include <time.h>

#include "freertos/FreeRTOS.h"

#include "esp_efuse.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"

#include "cJSON.h"

#include "native_ota_example.h"

/** 本模块日志标签。 */
static const char *TAG = "audio_control_plane";

/** SHA-256 文本采用两个十六进制字符表示一个字节。 */
#define AUDIO_CONTROL_PLANE_SHA256_HEX_LEN (NATIVE_OTA_SHA256_SIZE * 2U)

/** 保护最近 request_id 的短临界区锁。 */
static portMUX_TYPE s_audio_request_id_lock = portMUX_INITIALIZER_UNLOCKED;

/** 最近一次音频检查使用的 request_id，服务器响应必须严格匹配。 */
static char s_last_request_id[NATIVE_OTA_REQUEST_ID_SIZE];

/** 保存最近一次音频检查请求的 request_id。 */
static void audio_control_plane_set_last_request_id(const char *request_id)
{
    portENTER_CRITICAL(&s_audio_request_id_lock);
    strncpy(s_last_request_id, request_id, sizeof(s_last_request_id) - 1U);
    s_last_request_id[sizeof(s_last_request_id) - 1U] = '\0';
    portEXIT_CRITICAL(&s_audio_request_id_lock);
}

/** 判断响应中的 request_id 是否对应最近一次音频检查。 */
static bool audio_control_plane_request_id_matches(const char *request_id)
{
    bool matches;
    portENTER_CRITICAL(&s_audio_request_id_lock);
    matches = request_id != NULL && s_last_request_id[0] != '\0' &&
              strcmp(request_id, s_last_request_id) == 0;
    portEXIT_CRITICAL(&s_audio_request_id_lock);
    return matches;
}

/** 将一个十六进制字符转换为 0～15 的半字节数值；非法字符返回 -1。 */
static int audio_control_plane_hex_to_nibble(char value)
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

/** 将 SHA-256 十六进制文本转换为原始摘要；格式无效返回 false。 */
static bool audio_control_plane_parse_sha256(const char *text,
                                             uint8_t output[NATIVE_OTA_SHA256_SIZE])
{
    if (text == NULL || output == NULL || strlen(text) != AUDIO_CONTROL_PLANE_SHA256_HEX_LEN) {
        return false;
    }
    for (size_t i = 0; i < NATIVE_OTA_SHA256_SIZE; ++i) {
        int high = audio_control_plane_hex_to_nibble(text[i * 2U]);
        int low = audio_control_plane_hex_to_nibble(text[i * 2U + 1U]);
        if (high < 0 || low < 0) {
            return false;
        }
        output[i] = (uint8_t)((high << 4) | low);
    }
    return true;
}

/** 将 JSON 字符串复制到固定容量字段；节点缺失、过长或为空返回 false。 */
static bool audio_control_plane_copy_string(const cJSON *item, char *output, size_t output_size)
{
    if (!cJSON_IsString(item) || item->valuestring == NULL || output == NULL ||
        output_size == 0U) {
        return false;
    }
    size_t length = strlen(item->valuestring);
    if (length == 0U || length >= output_size) {
        return false;
    }
    memcpy(output, item->valuestring, length + 1U);
    return true;
}

/** 解析非负 32 位整数 JSON 字段；类型、小数或范围无效返回 false。 */
static bool audio_control_plane_get_u32(const cJSON *item, uint32_t *value)
{
    if (!cJSON_IsNumber(item) || value == NULL || item->valuedouble < 0.0 ||
        item->valuedouble > (double)UINT32_MAX ||
        item->valuedouble != floor(item->valuedouble)) {
        return false;
    }
    *value = (uint32_t)item->valuedouble;
    return true;
}

/** 解析可表示 Unix 时间戳的非负 64 位 JSON 整数。 */
static bool audio_control_plane_get_i64(const cJSON *item, int64_t *value)
{
    const double int64_limit = 9223372036854775808.0;
    if (!cJSON_IsNumber(item) || value == NULL || item->valuedouble < 0.0 ||
        item->valuedouble >= int64_limit || item->valuedouble != floor(item->valuedouble)) {
        return false;
    }
    *value = (int64_t)item->valuedouble;
    return true;
}

/**
 * @brief 判断 HTTPS URL 主机名是否属于精确允许列表（与 OTA 共用配置）。
 *
 * 允许列表为空仅限开发；主机名比较精确匹配、不允许后缀模糊匹配。
 */
static bool audio_control_plane_url_host_allowed(const char *url)
{
    static const char *scheme = "https://";

    if (url == NULL || strncmp(url, scheme, strlen(scheme)) != 0) {
        return false;
    }

    const char *host_start = url + strlen(scheme);
    size_t host_len = strcspn(host_start, "/?#:");
    if (host_len == 0U) {
        return false;
    }

    const char *allowlist = CONFIG_OTA_ALLOWED_URL_HOSTS;
    if (allowlist[0] == '\0') {
        ESP_LOGW(TAG, "Audio URL host allowlist is empty; enable it for production builds");
        return true;
    }

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

esp_err_t native_audio_build_check_request(const char *current_audio_version,
                                           char *json, size_t json_size, size_t *json_len)
{
    if (json == NULL || json_size == 0U || json_len == NULL ||
        current_audio_version == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    char device_id[NATIVE_OTA_DEVICE_ID_SIZE];
    esp_err_t err = native_ota_get_device_id(device_id, sizeof(device_id));
    if (err != ESP_OK) {
        return err;
    }

    /* 32 位随机数编码为 8 个小写十六进制字符，与 OTA 检查同一关联规则。 */
    char request_id[9];
    snprintf(request_id, sizeof(request_id), "%08" PRIx32, esp_random());

    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }

    bool added = cJSON_AddStringToObject(root, "type", "audio_check") != NULL &&
                 cJSON_AddStringToObject(root, "request_id", request_id) != NULL &&
                 cJSON_AddStringToObject(root, "device_id", device_id) != NULL &&
                 cJSON_AddStringToObject(root, "product", CONFIG_OTA_PRODUCT_ID) != NULL &&
                 cJSON_AddStringToObject(root, "current_audio_version",
                                         current_audio_version) != NULL;
    if (!added) {
        cJSON_Delete(root);
        return ESP_ERR_NO_MEM;
    }

    if (!cJSON_PrintPreallocated(root, json, json_size, false)) {
        cJSON_Delete(root);
        return ESP_ERR_INVALID_ARG;
    }

    *json_len = strlen(json);
    audio_control_plane_set_last_request_id(request_id);
    cJSON_Delete(root);
    return ESP_OK;
}

esp_err_t audio_control_plane_parse_audio_response(const char *json, size_t json_len,
                                                   native_audio_manifest_t *manifest,
                                                   bool *download_requested)
{
    if (json == NULL || json_len == 0U || json_len > NATIVE_OTA_JSON_MAX_LEN ||
        manifest == NULL || download_requested == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    *download_requested = false;
    memset(manifest, 0, sizeof(*manifest));

    esp_err_t result = ESP_OK;
    cJSON *root = cJSON_ParseWithLength(json, json_len);
    if (root == NULL || !cJSON_IsObject(root)) {
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    const cJSON *update = cJSON_GetObjectItemCaseSensitive(root, "update");
    const cJSON *request_id = cJSON_GetObjectItemCaseSensitive(root, "request_id");
    const cJSON *device_id = cJSON_GetObjectItemCaseSensitive(root, "device_id");
    const cJSON *product = cJSON_GetObjectItemCaseSensitive(root, "product");
    if (!cJSON_IsString(type) || strcmp(type->valuestring, "audio_check_response") != 0 ||
        !cJSON_IsBool(update) || !cJSON_IsString(request_id) ||
        !cJSON_IsString(device_id) || !cJSON_IsString(product)) {
        ESP_LOGW(TAG, "Audio response envelope has missing or invalid type/update/identity fields");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    /* 复制 request_id 后再进入临界区比较，避免依赖 cJSON 节点的短生命周期。 */
    char response_request_id[NATIVE_OTA_REQUEST_ID_SIZE];
    if (!audio_control_plane_copy_string(request_id, response_request_id,
                                         sizeof(response_request_id)) ||
        !audio_control_plane_request_id_matches(response_request_id)) {
        ESP_LOGW(TAG, "Ignoring audio response with stale or mismatched request_id");
        result = ESP_ERR_INVALID_STATE;
        goto cleanup;
    }

    char expected_device_id[NATIVE_OTA_DEVICE_ID_SIZE];
    if (native_ota_get_device_id(expected_device_id, sizeof(expected_device_id)) != ESP_OK ||
        strcmp(device_id->valuestring, expected_device_id) != 0) {
        ESP_LOGW(TAG, "Audio response device_id does not match this device");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (strcmp(product->valuestring, CONFIG_OTA_PRODUCT_ID) != 0) {
        ESP_LOGW(TAG, "Audio response product does not match");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    /* update=false 是合法的"无需更新"结果，不要求服务器提供清单字段。 */
    if (!cJSON_IsTrue(update)) {
        ESP_LOGI(TAG, "Audio server reports that no update is required");
        goto cleanup;
    }

    memcpy(manifest->request_id, response_request_id, sizeof(manifest->request_id));
    /* update=true 时一次性校验完整清单，任何字段失败都不会输出可下载清单。 */
    const cJSON *audio_id = cJSON_GetObjectItemCaseSensitive(root, "audio_id");
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(root, "version");
    const cJSON *url = cJSON_GetObjectItemCaseSensitive(root, "url");
    const cJSON *sha256 = cJSON_GetObjectItemCaseSensitive(root, "sha256");
    const cJSON *file_size = cJSON_GetObjectItemCaseSensitive(root, "file_size");
    const cJSON *expires_at = cJSON_GetObjectItemCaseSensitive(root, "expires_at");
    if (!audio_control_plane_copy_string(audio_id, manifest->audio_id,
                                         sizeof(manifest->audio_id))) {
        ESP_LOGW(TAG, "Audio manifest field audio_id is missing, empty or too long");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!audio_control_plane_copy_string(version, manifest->version,
                                         sizeof(manifest->version))) {
        ESP_LOGW(TAG, "Audio manifest field version is missing, empty or too long");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!audio_control_plane_copy_string(url, manifest->url, sizeof(manifest->url)) ||
        !audio_control_plane_url_host_allowed(manifest->url)) {
        ESP_LOGW(TAG, "Audio manifest URL is missing, too long, non-HTTPS or not allowed");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!audio_control_plane_parse_sha256(cJSON_IsString(sha256) ? sha256->valuestring : NULL,
                                          manifest->sha256)) {
        ESP_LOGW(TAG, "Audio manifest SHA-256 must contain exactly 64 hexadecimal characters");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!audio_control_plane_get_u32(file_size, &manifest->file_size) ||
        manifest->file_size == 0U || manifest->file_size > CONFIG_AUDIO_MAX_FILE_SIZE) {
        ESP_LOGW(TAG, "Audio manifest file_size must be a positive uint32 within the configured cap");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }
    if (!audio_control_plane_get_i64(expires_at, &manifest->expires_at)) {
        ESP_LOGW(TAG, "Audio manifest expires_at must be a non-negative integer timestamp");
        result = ESP_ERR_INVALID_ARG;
        goto cleanup;
    }

    /* 设备时间尚未同步时 now 可能不可用；此时跳过过期比较，但不放宽字段格式校验。 */
    time_t now = time(NULL);
    if (now > 0 && manifest->expires_at <= (int64_t)now) {
        ESP_LOGW(TAG, "Audio artifact %s has expired", manifest->audio_id);
        result = ESP_ERR_INVALID_ARG;
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
