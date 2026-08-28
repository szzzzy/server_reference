# Julia Fused-Base 通信协议（设备侧实际实现，服务器对接用）

> 适用固件：`julia-fused-base`（native_ota_example + 最小包音频 + 本地唤醒词）。
> 本文件逐条对应设备代码实现，服务器按此收发即可联调。
> 与旧版 GitHub 文档的差异（融合后）：**本地唤醒词触发 MIC_START**、WSS 下行 binary PCM 现已接通、AUTH 用 token。

---

## 0. 架构总览

```
设备                                             服务器
├─ WiFi STA → IPv4
├─ MQTT 控制面 (mqtt://<addr>:1883, TLS)
│    ├─ 主动规则检查/语音命令/状态上报
│    └─ 接收 ota_notify / ota_check_response / voice 命令
└─ WSS 语音面 (wss://<addr>:9443/voice, Bearer)
     ├─ 上行: PCM1 帧 (16B头+640B PCM=656B/20ms)、FILE_SEND 文件推送
     └─ 下行: 文本命令 (SPKS/SPKV/SPKE/SPKT/MICS/MICW/FILE_SEND/MIC_START/MIC_STOP)
              + 二进制 PCM (先 SPKS 后写扬声器)
```

---

## 1. WSS 语音面（重点）

### 1.1 连接

| 项 | 值 |
|---|---|
| URL | `wss://<JULIA_SERVER_ADDR>:<WSS_SERVER_PORT>/<WSS_PATH>`；默认端口 `9443`、路径 `/voice` |
| 鉴权 | HTTP 升级请求头 `Authorization: Bearer <token>` ；token 取值：`COMM_DEVICE_AUTH_TOKEN_VALUE` 优先，空则 `WSS_TOKEN` |
| 保活 | 客户端每 **15s** 发 PING 帧；10s 未收到 PONG → 判死重连（重连间隔 **5s**） |
| 帧 | RFC 6455；单帧载荷 **≤1200B**；opcode 0x1=文本、0x2=二进制 |

### 1.2 设备 → 服务器（WSS 上行 binary）

**① MIC 音频流（PCM1 帧）**——`MIC_START` 命令生效后每 20ms 一帧：

```
长度：16B 头 + 640B PCM = 656B（一个 WSS binary 帧，恒定）
头（全部小端）：
  [0..3]   "PCM1"                     魔数
  [4..7]   seq,    uint32 LE          帧序号（每开麦递增，不断帧）
  [8..9]   bytes,  uint16 LE          本帧 PCM 字节数（恒 640）
  [10..11] level,  int16  LE          dBFS×100（如 -6000 = -60.00dBFS）
  [12..14] 保留 = 0
  [15]     sum8,   uint8              PCM 数据逐字节和 mod 256
正文：320 × int16 LE = mono PCM16 @ 16kHz
```

服务器校验建议：`seq` 连续、`[15]` 求和一致、`level` 随环境变化；异常可丢弃该帧不断链。

**② FILE_SEND 文件推送（服务器先发 `FILE_SEND <uri>` 触发）**：

```
设备发送序列：
  文本帧(0x1): "BEGIN FILE <size> <name>"
  二进制帧 ×N(0x2): 文件内容，每帧 ≤1200B
  文本帧(0x1): "END <bytes>"            —— 总字节数确认
失败状态（文本帧 0x1）:
  "ERROR bad_uri" | "ERROR bad_extension" | "ERROR file_open_failed"
  | "ERROR sd_busy" | "ERROR file_size_failed" | "ERROR file_name_too_long"
```

规则：URI `SD:/x/y` → `/sdcard/x/y`、`SPIFFS:/x/y` → `/spiffs/x/y`；**仅 `.wav`**；单文件 ≤ **8MiB**；中途任何失败→断链重连（**绝不发截断的 END**）。

### 1.3 服务器 → 设备（WSS 下行）

**文本命令**（0x1，与 MQTT `vcmd` 同一语法）：

| 命令 | 作用 | 设备行为 |
|---|---|---|
| `FILE_SEND <uri>` | 让设备把 `<uri>` 的 wav 推回服务器 | 见 1.2-② |
| `MIC_START` | 开 MIC 流式上传 | `mic_started` 回执（MQTT vstatus）；PCM1 帧开始 |
| `MIC_STOP` | 关 MIC 流式上传 | `mic_stopped` 回执；停止 PCM1 |
| `MICS <bg>` | 休眠触发上传（bg=-10000~0，dBFS×100） | 仅电平>背景+5dB 才发帧（带预录缓冲） |
| `MICW` | 恢复持续上传 | 退出休眠模式 |
| `SPKS <rate>` | 扬声器开始播放（rate 采样率，如 16000/24000） | **必须最先发**，否则下行 PCM 被丢弃 |
| `SPKV <0-100>` | 音量 | 即时生效，越界钳位 |
| `SPKE` | 停止播放 | 写静音+清标志 |
| `SPKT` | 扬声器本地自检（440/660/880Hz） | 纯本地测试用 |

**二进制 PCM（0x2）**：mono PCM16，长度偶数、**≤1200B/帧**、采样率 16k/24k（与 SPKS rate 一致）。
**关键**：未收到 `SPKS` 前下发的 PCM 全部丢弃（日志 `Dropping x-byte downlink PCM: speaker not started`）。
建议服务器推流顺序：`SPKS 24000`（或16000）→ PCM 帧×N → `SPKE`。

---

## 2. MQTT 控制面

### 2.1 Topic 表

| 方向 | Topic | QoS | 说明 |
|---|---|---|---|
| 设备→服务器 | `/device/ota/check` | 1 非保留 | 版本检查 |
| 服务器→设备 | `/device/ota/response/<device_id>` | 1 | 检查响应（设备订阅） |
| 服务器→设备 | `/device/ota/notify/<device_id>` | 1 | 轻量通知 |
| 设备→服务器 | `/device/ota/status/<device_id>` | 1 | 生命周期状态+进度 |
| 服务器→设备 | `voice/esp32s3/vcmd` | 1 | 语音文本命令（同 1.3 表，不含 SPKS/SPKE/SPKT——那些走 WSS） |
| 设备→服务器 | `voice/esp32s3/vstatus` | 1 best-effort | 语音命令回执 |

### 2.2 MQTT 语音命令（服务器→设备 `vcmd`）

纯文本行，可含换行：
```
FILE_SEND <uri> | MIC_START | MIC_STOP | MICW | MICS <bg> | SPKV <n>
```

### 2.3 MQTT 语音回执（设备→服务器 `vstatus`）

纯文本，取值：
```
mic_started | mic_stopped | mic_wake | mic_sleep | volume_set
| file_send_queued
| error file_send_rejected | error mic_start_rejected | error mic_stop_rejected
| error mic_sleep_invalid | error volume_invalid
```

---

## 3. OTA（MQTT + HTTPS，快速对照）

### 3.1 设备→服务器 `ota_check`

```json
{
  "type": "ota_check",
  "request_id": "8位小写hex",
  "device_id": "esp-xxxxxxxxxxxx",
  "product": "julia-ai-device",
  "hardware_version": "1.0",
  "current_version": "0.0.1"
}
```
触发：MQTT 连接+SUBACK 后立即；之后每 21600s+随机抖动；收到合法 `ota_notify` 立即补发。

### 3.2 服务器→设备 `ota_check_response`

```json
{
  "type": "ota_check_response",
  "update": true,
  "request_id": "<原样回显>",
  "device_id": "<原样>",
  "product": "<原样>",
  "hardware_version": "<原样>",
  "job_id": "job-001",
  "artifact_id": "release-2025-01",
  "version": "0.0.2",
  "url": "https://<JULIA_SERVER_ADDR>/fw/app.bin",
  "sha256": "64位hex",
  "image_size": 1048576,
  "security_version": 1,
  "expires_at": 1893456000,
  "force_update": false
}
```
`update=false` 合法（无清单字段）。`update=true` 时清单字段全必填；`request_id/device_id/product/hardware_version` 必须回显；`url` 主机名须命中白名单（默认 `JULIA_SERVER_ADDR`）；`version` 须高于当前除非 `force_update=true`；`sha256` 恰好 64hex。

### 3.3 服务器→设备 `ota_notify`

```json
{ "type": "ota_notify", "job_id": "job-001" }
```

### 3.4 设备→服务器 `ota_status`（生命周期，持久化到 PUBACK）

```json
{
  "type": "ota_status",
  "schema_version": 1,
  "event_id": "32位hex幂等ID",
  "device_id": "esp-...",
  "request_id": "8hex",
  "artifact_id": "release-2025-01",
  "product": "julia-ai-device",
  "hardware_version": "1.0",
  "current_version": "0.0.1",
  "target_version": "0.0.2",
  "state": "accepted",
  "attempt": 1,
  "error_code": "NONE",
  "uptime_ms": 123456,
  "job_id": "job-001"
}
```
`state`: `accepted|downloading|verifying|rebooting|booted_pending_verify|succeeded|failed|rolled_back|deferred`
`error_code`（**字符串**）: `NONE|PRECONDITION_LOW_POWER|NETWORK_TIMEOUT|TLS_VERIFY_FAILED|HTTP_STATUS_INVALID|RANGE_MISMATCH|IMAGE_TOO_LARGE|IMAGE_HEADER_INVALID|HASH_MISMATCH|IMAGE_VALIDATE_FAILED|BOOT_SELF_TEST_FAILED|ROLLBACK_UNAVAILABLE|MANIFEST_INVALID|ARTIFACT_QUARANTINED|BOOT_PARTITION_SET_FAILED|NVS_WRITE_FAILED|STORAGE_UNAVAILABLE`
进度事件（仅 `state=downloading`，best-effort）：追加
`"bytes_downloaded": 524288, "image_size": 1048576, "progress_percent": 50`

### 3.5 HTTPS 固件下载

| 场景 | 服务器要求 |
|---|---|
| 首次 | `GET <url>` → 200，`Content-Length == image_size` |
| 断点续传 | `GET` + `Range: bytes=<offset>-` → 206 + `Content-Range: bytes start-end/total`；**ETag 稳定**（变化→设备从零重下，只允许一次 200 回退） |
| 校验 | SHA-256==清单；镜像头 project_name=`julia-ai` |

---

## 4. 公共常量（Kconfig 默认值）

| 项 | 默认 |
|---|---|
| `JULIA_SERVER_ADDR` | 必须配置真实域名（空/占位符时 MQTT/WSS 拒绝启动） |
| `COMM_MQTT_OTA_CHECK_TOPIC` | `/device/ota/check` |
| `COMM_MQTT_OTA_RESPONSE_NOTIFY_STATUS_PREFIX` | `/device/ota/{response,notify,status}` |
| `COMM_MQTT_VOICE_CMD_TOPIC` / `VOICE_STATUS_TOPIC` | `voice/esp32s3/vcmd` / `voice/esp32s3/vstatus` |
| `WSS_SERVER_PORT` / `WSS_PATH` | 9443 / `/voice` |
| `WSS_KEEPALIVE_INTERVAL_SECONDS` | 15 |
| `WSS_PONG_TIMEOUT_SECONDS` | 10 |
| `WSS_RECONNECT_INTERVAL_SECONDS` | 5 |
| `OTA_CHECK_INTERVAL_SECONDS` | 21600（抖动 0~1800s） |
| `OTA_CHECK_RESPONSE_TIMEOUT_SECONDS` | 15 |

---

## 5. 服务器最小验证顺序（联调指引）

1. **WSS 建连**：验证 Bearer 鉴权 → PING→PONG；
2. **语音上行**：发 `MIC_START` → 设备回 `mic_started` → 收 PCM1 帧流（校验魔数/seq/sum8）→ `MIC_STOP` → `mic_stopped`；
3. **语音下行**：`SPKS 24000` + PCM 帧 → 设备出声；`SPKV`/`SPKE` 生效；
4. **线上唤醒**（新版固件：无本地唤醒词）：设备 WSS 认证后**持续上传 PCM1** → 服务器待机态
   用普通 ASR 判定"你好小科"（含同音容错）→ 下发 MIC_START（设备→LISTEN，UI 闭眼）；
   服务器 VAD 判说完 → MIC_STOP（结束本轮 LISTEN，不关 PCM）→ ASR/LLM/TTS 下行
   （SPKS→PCM→SPKE，设备播完回 IDLE，PCM 继续上传）→ 服务器回待机，第二轮需重新说唤醒词。
5. **OTA**：`ota_check`→`ota_check_response`→`ota_status` 序列 + HTTPS 下载（Range/ETag）。
