/**
 * @file    voice_uri.c
 * @brief   FILE_SEND URI 到本地文件系统路径的受控映射实现。
 */

#include "voice_uri.h"

#include <stdio.h>
#include <string.h>

/**
 * @brief 检查受控根目录下的相对路径段是否安全。
 *
 * 拒绝 "."、".." 段与反斜杠：远端 FILE_SEND 命令只允许读取受控根目录
 * （/sdcard、/spiffs）内的音频文件，不允许越出预期目录。
 *
 * @param[in] rel 相对路径（不含 URI 前缀），不允许为 NULL。
 * @return true 路径段安全；false 包含 "."/".." 段或反斜杠。
 */
static bool voice_uri_segments_safe(const char *rel)
{
    const char *p = rel;
    while (*p != '\0') {
        if (*p == '\\') {                   /* 只接受 POSIX 分隔符，防混淆 */
            return false;
        }
        size_t seg_len = strcspn(p, "/");
        if (seg_len == 1 && p[0] == '.') {
            return false;
        }
        if (seg_len == 2 && p[0] == '.' && p[1] == '.') {
            return false;
        }
        p += seg_len;
        while (*p == '/') {
            p++;
        }
    }
    return true;
}

bool voice_uri_to_path(const char *uri, char *path, size_t path_cap)
{
    if (uri == NULL || path == NULL || path_cap == 0U) {
        return false;
    }
    const char *root = NULL;
    const char *rel = NULL;
    if (strncmp(uri, "SD:/", 4) == 0) {
        root = "/sdcard";
        rel = uri + 4;
    } else if (strncmp(uri, "SPIFFS:/", 8) == 0) {
        root = "/spiffs";
        rel = uri + 8;
    } else {
        return false;
    }
    if (rel[0] == '\0' || !voice_uri_segments_safe(rel)) {
        return false;
    }
    int n = snprintf(path, path_cap, "%s/%s", root, rel);
    if (n <= 0 || (size_t)n >= path_cap) {
        return false;
    }
    return true;
}
