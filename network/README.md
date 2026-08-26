# network/ —— 网络侧（远程语音服务器）目录

> 2026-08-26 从项目根目录重构而来：**网络侧独立成目录，与固件侧（`julia-fused-base/`）和本地链路（`next_stage/`、`voice_sleep_v5_5090/`）分离**。
> 远程部署形态：板卡 WiFi 接入（MQTT 控制面 + WSS 语音面 + HTTPS 文件），本目录是**唯一的服务器侧代码**。

## 目录结构

```
network/
├─ server/                     ← 原 virtual_server/ 全量迁移（含 runs/ 历史数据）
│  ├─ run_server.py            ← 主入口（broker + HTTPS + WSS + MQTT 单进程）
│  ├─ real_engine.py           ← 真实语音引擎（WSS PCM1→VAD/ASR→Qwen→TTS→下行）
│  ├─ ring_buffer.py           ← WSS 帧流 ↔ serial 兼容层（VAD 复用的关键）
│  ├─ voice_bridge.py          ← 引擎工厂（stub/real）
│  ├─ mqtt_pure.py / mqtt_adapter.py / wss_adapter.py / session_hub.py / manifest_store.py
│  ├─ https_file_server.py / board_simulator.py / make_test_artifacts.py
│  ├─ config.json              ← 端口/token/VAD/引擎参数（路径相对本目录解析）
│  ├─ releases/                ← OTA/音频发布物 + manifest（隔离记录在此，gitignore）
│  ├─ start_virtual_server.cmd ← 一键启动
│  └─ README.md / 真机联调指南.md / 语音工作流设计.md
├─ certs/                      ← 证书工具包（CA/服务器/客户端；私钥 gitignore）
├─ PROTOCOL.md                 ← 板卡-服务器通信协议（设备侧实现逐条对应）
├─ 网络服务器改造方案.md         ← 网络化方案文档（历史）
└─ 变更统计.md                  ← 网络化改造统计（历史：基准 8bc337a→HEAD；文中旧路径已过时）
```

## 启动（远程联调）

```
network\server\start_virtual_server.cmd --voice-mode real --tty
# 或手动：
.venv_5090_llm\Scripts\python.exe network\server\run_server.py --voice-mode real --tty
```

> `--voice-mode real` 自动用 `.venv_5090_llm`（有 torch/transformers/funasr）；stub 模式用 `.venv_vs`。

## 路径解析约定（重构后已统一）

| 相对谁 | 解析到 |
|---|---|
| `server/config.json` 的路径（`../certs/...`、`releases/...`） | `network/` 下（`network/certs/`、`network/server/releases/`） |
| 引擎 `project_root`（`parents[1]`，run_server.py:49） | **项目根**：`models/`、`next_stage/full_pipeline_auto_5090/`（TTS 复用件）、`next_stage/voice_sleep_v5_5090/config.json`（TTS 基础配置） |
| 模拟器 `ROOT`（`parents[1]`） | 项目根：`samples/`（标准参考音频） |

## 与项目其它部分的关系

- **复用（import，不改动）**：`asr_eval_core.py`（ASR 加载）、`board_serial_asr_test.py`（VAD/识别/电平）、`realtime_pipeline.py`（工具 + `start_tts_worker`）、`next_stage/full_pipeline_auto_5090/tts_worker.py`（TTS 子进程，与本地 V5 共用一个文件）、三个本地模型、`third_party/CosyVoice`。
- **不在此目录**：固件 `julia-fused-base/`（含自身的 docs/PROTOCOL.md 副本）、本地链路 `next_stage/`、`voice_sleep_v5_5090/`。

## 变更记录

- 2026-08-26：`virtual_server/` → `network/server/`；`certs/` → `network/certs/`；`PROTOCOL.md`、`网络服务器改造方案.md`、`变更统计.md` 移入 `network/`；路径解析修正（`project_root=parents[1]`、`common.project_root()`、`board_simulator.ROOT`）；`start_virtual_server.cmd` / `.gitignore` 路径同步。
- 同日：`real_engine.py` 更新——TTS 分段预提交（V5 式生产者/消费者）、链路内背景校准、VAD 参数 V5 化（`start/end_above_db=3`）、每轮端点诊断日志。
