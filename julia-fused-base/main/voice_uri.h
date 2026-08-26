/**
 * @file    voice_uri.h
 * @brief   FILE_SEND URI 到本地文件系统路径的受控映射。
 *
 * 只允许受控根目录内的相对路径："SD:/x/y" -> "/sdcard/x/y"，
 * "SPIFFS:/x/y" -> "/spiffs/x/y"。包含 "."/".." 段、反斜杠或绝对路径的
 * URI 一律拒绝，防止远端命令越出预期音频目录读取文件。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 把 FILE_SEND URI 换算为受控根目录内的本地文件系统路径。
 *
 * @param[in]  uri      URI 字符串（"SD:/x/y" 或 "SPIFFS:/x/y"），不允许为 NULL。
 * @param[out] path     输出路径缓冲区，不允许为 NULL。
 * @param[in]  path_cap 输出缓冲区容量，必须大于 0。
 *
 * @return true 换算成功，path 已写入 NUL 结尾路径。
 * @return false URI 格式不支持、包含不安全路径段或缓冲区不足。
 *
 * @note 该映射不检查文件是否存在；调用方仍需自行 open。
 */
bool voice_uri_to_path(const char *uri, char *path, size_t path_cap);

#ifdef __cplusplus
}
#endif
