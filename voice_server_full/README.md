# 语音服务器完整链路包（voice_server_full）

从原项目 `network/server`（2026-08-26 网络化重构版）整体归纳而成的**自包含服务器包**：
一台机器即可跑通 **设备/客户端 → WSS 语音 → VAD/ASR → Qwen3 大模型推理 → CosyVoice2 语音合成 → WSS 下行播报** 的完整链路。
所有网络服务（MQTT broker + HTTPS 文件 + WSS 语音 + MQTT 控制面）与大模型推理代码都在本目录内；大体积外部依赖（模型权重 / venv / CosyVoice 源码）通过 `deps_root` 配置指向原项目根（也可以搬进本包实现完全自包含，见 §7）。

---

## 1. 完整链路图

```
 ┌──────────── 客户端(板卡模拟器 board_simulator.py / 真机 ESP32-S3 / VR 侧 WSS 客户端) ────────────┐
 │  上行: 二进制 PCM1 帧(16B 头 + 640B, 20ms @16kHz) + 文本命令                                    │
 │  下行: 文本命令(SPKS <rate>/SPKV/SPKE/...) + 二进制 PCM16(@24000)                               │
 └───────────────┬───────────────────────────────────────────────────────────────────────────────┘
                 │ WSS  wss://<IP>:9443/voice  (Bearer token 鉴权)
 ┌───────────────▼───────────────────────────────────────────────────────────────────────────────┐
 │  server/run_server.py                ← 主入口(单进程, asyncio)                                  │
 │  ├─ broker.py  + mqtt_pure.py        本地 MQTT broker      tcp://<IP>:1883 (可 8883 TLS)        │
 │  ├─ https_file_server.py             HTTPS 文件服务        https://<IP>:8443 (固件/音频/状态台)   │
 │  ├─ wss_adapter.py                   WSS 语音服务(鉴权/PCM1 解析/文件推收/下行广播)               │
 │  ├─ mqtt_adapter.py                  MQTT 控制面(OTA/音频检查应答、vcmd、vstatus)                │
 │  ├─ session_hub.py / manifest_store.py  设备会话中心 / 发布清单(OTA/音频应答规则)                │
 │  └─ voice_bridge.make_engine(...)    stub=统计引擎; real=真实推理引擎 ↓                          │
 │            └─ real_engine.RealVoiceEngine   (engine/ 大模型推理代码)                            │
 │                 ├─ ① ring_buffer.py  WSS 字节流 → serial 同形接口(语音停流自动补静音帧)           │
 │                 ├─ ② engine/board_serial_asr_test.py  VAD(capture_until_endpoint) + 帧解析      │
 │                 ├─ ③ engine/asr_eval_core.py           ASR 加载(load_paraformer)                │
 │                 │     识别: board_serial_asr_test.recognize  (FunASR Paraformer 流式, 600ms 块) │
 │                 ├─ ④ LLM: Qwen3-4B  transformers TextIteratorStreamer 流式生成(后台线程)         │
 │                 │     切句: _sentence_chunks (遇 。！？; 切句 / 长句无标点 48 字保护)             │
 │                 └─ ⑤ engine/tts_worker.py (子进程, .venv_5090_tts)  CosyVoice2 zero-shot 流式    │
 │                        IPC: tts_queue/request_*.json → chunk_N.pcm → response_*.json            │
 │                   ⑥ 下行: _stream_tts_segment → SPKS + PCM(≤1200B/帧, ×0.88 播放节奏) + SPKE    │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
```

一句话数据流：**PCM1 帧 → RingBuffer → VAD 端点 → ASR 文本 → Qwen3 token 流 → 切句 → TTS 子进程 PCM → WSS 下行**。

---

## 2. 目录结构

```
voice_server_full/
├─ README.md                  ★ 本文件(链路/链接/启动/配置)
├─ start_server.cmd            一键启动(stub 模式:不加载模型,验证网络链路)
├─ start_server_real.cmd       一键启动(real 模式:完整 ASR→LLM→TTS 推理)
├─ server/                    ← 网络服务端(全部代码)
│  ├─ run_server.py            主入口(broker + HTTPS + WSS + MQTT,单进程)
│  ├─ config.json              唯一配置(端口/token/主题/模型依赖根/提示词)
│  ├─ real_engine.py           真实语音引擎(编排 VAD/ASR/LLM/TTS + 下行)
│  ├─ voice_bridge.py          引擎工厂(stub/real)
│  ├─ wss_adapter.py           WSS 语音服务(鉴权/PCM1/命令/FILE_SEND/广播)
│  ├─ mqtt_adapter.py          MQTT 控制面(OTA/audio 应答、vcmd)
│  ├─ mqtt_pure.py             纯标准库 MQTT 客户端/broker
│  ├─ broker.py                本地 MQTT broker 启动
│  ├─ https_file_server.py     HTTPS 文件服务(Range/ETag + /__status + /__debug 状态台)
│  ├─ session_hub.py           设备会话中心(状态/事件去重)
│  ├─ manifest_store.py        发布清单与 OTA/audio 应答(含隔离)
│  ├─ ring_buffer.py           WSS 帧流 ↔ serial 兼容层(VAD 复用关键)
│  ├─ board_simulator.py       板卡模拟器(OTA/audio/voice/filedump 自动化验收)
│  ├─ make_test_artifacts.py   生成测试发布物(fw/app.bin + audio/greeting.wav + manifest)
│  ├─ mqtt_selftest.py         MQTT 协议自测
│  ├─ releases/                发布物+manifest(已含测试产物)
│  ├─ runs/                    运行日志/结果(每次启动新建)
│  └─ README.md / 真机联调指南.md / 语音工作流设计.md
├─ engine/                    ← 大模型推理代码(ASR/LLM/TTS)
│  ├─ asr_eval_core.py         ASR: load_paraformer / recognize_streaming / cer
│  ├─ board_serial_asr_test.py VAD+识别: rms_dbfs / read_frame / capture_until_endpoint / recognize
│  ├─ realtime_pipeline.py     工具: resolve / find_reference / start_tts_worker / sentence_chunks
│  ├─ tts_worker.py            TTS 子进程: CosyVoice2 zero-shot 流式合成(文件 IPC)
│  └─ README.md                推理代码逐文件说明
├─ certs/                      ← 证书工具包(CA/服务器/客户端 + generate_certs.py)
├─ samples/                    板卡模拟器标准参考音频(16k 女声)
├─ deps/README.md             ← 外部大依赖说明(models/venv/CosyVoice 如何放置或指向)
└─ docs/                       ← PROTOCOL.md(板卡-服务器协议) / 网络服务器改造方案.md / 变更统计.md
```

---

## 3. 服务器链接（客户端接入地址）

启动后按以下地址连接（`<IP>` = 服务器本机 IP，启动日志会打印；换网络后可用 `server.config.json → server.addr` 指定，或让它 `auto` 自动检测）：

| 服务 | 链接地址 | 说明 |
|---|---|---|
| **WSS 语音面** | `wss://<IP>:9443/voice` | 二进制帧=PCM1(20ms/16kHz)；文本=命令；`Authorization: Bearer <token>`(见 config `wss.token`，默认 `julia-test-token-2026`) |
| **HTTPS 文件面** | `https://<IP>:8443/` | 固件 `/fw/app.bin`、音频 `/audio/greeting.wav`；支持 Range/ETag/SHA-256 |
| **状态台(浏览器)** | `https://<IP>:8443/__debug` | 实时设备状态 + 语音引擎快照(2s 自动刷新)，JSON 版在 `/__status` |
| **MQTT 控制面** | `tcp://<IP>:1883`(明文) / `8883`(TLS 可选) | 主题: `/device/ota/*`、`/device/audio/*`、`voice/esp32s3/vcmd`、`voice/esp32s3/vstatus` |
| 状态查询 | `GET https://<IP>:8443/__status` | JSON: 设备/引擎/最近事件/发布清单 |

> 客户端侧完整协议(帧格式/命令集/OTA 流程)见 `docs/PROTOCOL.md`；无真机时用 `server/board_simulator.py --scenario all` 作客户端联调。

---

## 4. 启动

### 4.1 快速验证网络链路(不加载大模型,秒级启动)

```
start_server.cmd
```

### 4.2 完整推理链路(加载 ASR+Qwen3+TTS,约 20–30 s,需 NVIDIA GPU)

```
start_server_real.cmd
```

或手动(以原项目根 venv 为例):

```
.venv_5090_llm\Scripts\python.exe server\run_server.py --voice-mode real --tty
.venv_vs\Scripts\python.exe server\run_server.py                        # stub
```

启动脚本的 Python 探测顺序:环境变量 `VSF_REAL_PY`(或 `VSF_STUB_PY`) → 包根 `.venv_*` → 包上级项目根 `.venv_*`。
(`--voice-mode real` 必须用含 torch/transformers/funasr 的 `.venv_5090_llm`。)

### 4.3 常见启动参数

| 参数 | 作用 |
|---|---|
| `--voice-mode real` | real 推理引擎(默认 config 为 stub 时用) |
| `--tty` | 启用 stdin 控制台:输入文本作为 vcmd 下发 |
| `--no-broker` | 连外部 MQTT broker(改 config `mqtt.host`) |
| `--admin-vcmd "FILE_SEND SD:/x.wav" --admin-vcmd-delay 6` | 启动后延迟下发一条命令 |
| `--config <path>` | 换配置 |

---

## 5. 大模型推理链路(ASR–LLM–TTS)程序对应

| 环节 | 代码位置 | 关键入口 | 模型 |
|---|---|---|---|
| VAD | `engine/board_serial_asr_test.py` | `capture_until_endpoint()`(能量阈值+滑动窗起始+带抵扣静音端点)、`rms_dbfs()` | 无(纯信号处理) |
| **ASR** | `engine/asr_eval_core.py` + `engine/board_serial_asr_test.py` | `load_paraformer()`(FunASR AutoModel, 本地快照, `disable_update`) → `recognize()`(600ms 块 + cache + is_final) | `models/paraformer-zh-streaming` |
| **LLM** | `server/real_engine.py` | `_answer_qa()`: `apply_chat_template` → `TextIteratorStreamer` 后台线程 `llm.generate`(贪心, max_new_tokens 可配) → `_sentence_chunks()` 切句(标点优先,长句 48 字兜底) | `models/Qwen3-4B-Instruct-2507` |
| **TTS** | `engine/tts_worker.py`(子进程) + `engine/realtime_pipeline.py::start_tts_worker` | `CosyVoice2.inference_zero_shot(prompt_text, prompt_wav, stream=True)` → 24kHz 张量 → int16 PCM chunk 落盘 | `models/CosyVoice2-0.5B` + `third_party/CosyVoice` 源码 |
| 下行推流 | `server/real_engine.py::_stream_tts_segment` | 轮询 `chunk_N.pcm` → `SPKS <rate>` + PCM(≤1200B/帧, ≈1.14×实时) + `SPKE` | — |

进程模型:主进程加载 ASR+Qwen3(GPU)并编排;`tts_worker.py` 用独立 venv 跑 CosyVoice2(依赖隔离),两侧仅通过 `runs/<时间戳>/tts_queue/` 目录文件通信(原子写 + 轮询)。

---

## 6. 配置要点(server/config.json —— 服务器实际使用的唯一配置文件)

面向**线上唤醒版语音链路**(设备端无本地唤醒词,固件 WSS 认证后持续上传)。`run_server.py`
默认加载的就是这份;启动后所有语音参数修改都在这里。

**ASR / LLM / TTS 三段:**

| 段 | 关键键 | 说明 |
|---|---|---|
| `voice.real.models.asr` | `models/paraformer-zh-streaming` | ASR 模型目录(相对 `deps_root`) |
| `voice.real.models.llm` | `models/Qwen3-4B-Instruct-2507` | LLM 模型(要换速可一行改成 Qwen2.5-1.5B-Instruct) |
| `voice.real.models.tts` | `models/CosyVoice2-0.5B` | TTS 模型目录 |
| `conversation` | `system_prompt` / `max_new_tokens=60` / `history_turns=10` | **LLM 参数**:通用语音助手提示词/回答字数上限/历史轮数 |
| `wake`(在 `voice.real.wake`) | `words=["你好小科"]`、`prompt`、`timeout_seconds=60`、`listen_seconds=8` | **线上唤醒**:词表(默认仅"你好小科",同音容错内置)/应答文本/空闲超时/待机段上限 |
| `interrupt`(在 `voice.real.interrupt`) | `enabled / min_chars=2 / min_similarity=0.5` | **播放期打断**(语义回放免疫):识别内容与播放文本相似度低于阈值且 ≥2 字即抢话 |
| `vad`(在 `voice.real.vad`) | `start_above_db=5`、`end_above_db=3`、`start_active_ms=120`、`endpoint_ms=400`、`max_seconds=15` + `dynamic_floor`(默认开) | **对话态 VAD**(滞回:开始严/结束松)+ **动态底噪**(双窗自适应阈值,启用时跳过开机静态校准) |
| `tts` | `role`/`reference_manifest`/`fallback_reference_*` | **TTS 音色**(零样本克隆参考音频+文本);音色换这里 |
| `voice_control` | `first_tts_fast_cut_chars=10`、`first_tts_segment_chars=10`、`later_tts_segment_chars=40` | **切句/分段**:首段快切与送 TTS 字数 |

**网络与资源:**

| 键 | 说明 |
|---|---|
| `wss.port/path/token` | 语音连接:端口 9443/路径 /voice/令牌 —— 客户端连 `wss://<IP>:9443/voice`(Bearer token) |
| `deps_root`(顶层 + `voice.real`) | 模型/音色/venv/CosyVoice 源码所在根(当前指向原项目根;自包含时改为本包根) |
| `file_server.port` | 状态台 `https://<IP>:8443/__debug` |
| `mqtt.enabled` | `false`(默认禁用 OTA/命令控制面;需要烧录/升级控制时改 `true`) |
| `voice.mic_restart_after_answer` | `true`(SPKE 后重发 MIC_START 连续对话;线上唤醒模式下即"唤醒一次·持续对话") |
| `paths.server_cert/server_key` | TLS 证书(指向 `../certs/server/…`) |

## 7. 听—想—说 协议适配(ESP32 Julia 设备)

新版固件无本地唤醒词,WSS 认证后**持续上传 PCM1**;服务器线上判定唤醒词,之后一次唤醒持续对话:

```
设备(持续上传) → WSS 上传 PCM1 帧 ──> 服务器(待机态)
                                      ├─ 流式 ASR(600ms 块)+ 同音容错:判定"你好小科"
                                      ├─ 命中 → TTS 应答"我在，请讲。" → 丢弃应答期上行(回声)
                                      ├─ MIC_START(设备→LISTEN)
                                      ├─ VAD 能量端点:判定"用户说完了" → MIC_STOP(设备→THINKING)
                                      ├─ ASR 最终识别 → Qwen3 生成(流式切句)
                                      ├─ SPKS 24000(开播,采样率=TTS 实际 24000)
                                      ├─ WSS 二进制帧:mono PCM16(设备播报+嘴型,UI→SPEAKING)
                                      ├─ SPKE(播报结束)
                                      └─ MIC_START(续听——唤醒一次·持续对话;60s 空闲超时回待机)
```

- 文本帧(MIC_START/MIC_STOP/SPKS/SPKE)均为独立精确帧;下行 PCM 只出现在 SPKS 之后、SPKE 之前;
- **播放期打断**:播放中用户抢话 → 服务器停止旧 TTS + SPKE + MIC_START(设备 EVT_INTERRUPT 进 LISTEN),
  由 `voice.real.interrupt` 控制;无 AEC 时 v1 不保留插话全文;
- **半双工**:SPKS→SPKE 期间不回采上行,SPKE 后 0.6s 余震丢弃,防自问自答(回声免疫);
- 实测(2026-08-31): 隔离端口全链路 6/6(唤醒应答/无唤醒词问题轮/续听/真实回声不打断/超时二次唤醒/打断后链路),
  动态底噪真机 A/B 恢复轮 2.16s 起始 / 4.92s 端点;验证记录 `server/runs/20260831_*`。

---

## 7. 外部依赖与完全自包含两种模式

**模式 A(默认,当前机器即跑):** `deps_root` 指向原项目根,`models/`(数 GB)、两个 venv、`third_party/CosyVoice`(数 GB)原样复用,本包只装代码/证书/发布物。

**模式 B(完全独立):** 把外部依赖搬入本包,并把 `deps_root` 改为 `"."`:

```
voice_server_full/
├─ models/                      ← 从 deps_root 拷入 models/ 三个模型目录
├─ .venv_5090_llm/  .venv_5090_tts/   ← 从 deps_root 拷入(或重新 pip 安装)
├─ third_party/CosyVoice/       ← CosyVoice 源码克隆
├─ tts_roles_5090/              ← 角色音色(可选;缺省用 CosyVoice 自带 zero_shot_prompt.wav)
└─ next_stage/voice_sleep_v5_5090/config.json  ← TTS 基础配置(可选)
```

具体清单与体积预估见 `deps/README.md`。模型/环境版本与来源:
`paraformer-zh-streaming`(FunASR)、`Qwen3-4B-Instruct-2507`(transformers)、`CosyVoice2-0.5B`(third_party/CosyVoice)。

---

## 8. 链路验收

| 命令 | 验证内容 |
|---|---|
| `server\board_simulator.py --scenario all` | 客户端侧自动化:OTA(7 项)/audio(16 项)/voice(7 项)/filedump |
| `server\board_simulator.py --scenario voice --expect-downlink` | real 模式:模拟器播放人声 → 断言下行 SPKS+PCM+SPKE |
| `server\mqtt_selftest.py` | MQTT 协议自测 |
| 浏览器开 `https://<IP>:8443/__debug` | 观察设备/引擎状态与每轮识别/回答/指标 |

---

## 9. 来源与变更

- 本包归纳自原项目 `network/server`(2026-08-26 重构版),代码零功能改动,仅做**路径适配**:
  - `server/real_engine.py`: 新增 `deps_root`(模型/venv/third_party/基础配置统一解析),推理代码固定从包内 `engine/` 导入(包根=server 目录父级,`parents[0]`),`system_prompt` 生效;
  - `server/run_server.py`、`server/board_simulator.py`: 包根计算由 `parents[1]`(原布局"项目根/network/server"深两层)改为 `parents[0]`(本包仅深一层),避免把上级项目根误当包根;
  - `engine/realtime_pipeline.py`: `find_reference`/`start_tts_worker` 支持依赖根覆盖,worker 增加 `--cosy-root`,模块根改为 `HERE.parents[0]`;
  - `engine/tts_worker.py`: 新增可选 `--cosy-root`(默认兼容原 `--project-root`);
  - `server/config.json`: 增加 `voice.real.deps_root`,并**把本地 V5 完整参数体系并入**
    (`conversation`/`voice_control`/`modes`/`tts` 全段落,与 V5 同构);
  - `server/real_engine.py`: VAD 参数改为 modes/voice_control 优先(vad 兜底,消除 120/300ms 硬编码)、
    LLM 提示词/字数改 conversation 优先、TTS 配置支持本 config 覆盖 base_config、
    `_sentence_chunks` 新增首段快切(fast_cut)、切句线程改 V5 式分段缓冲
    (**首段完整一句即送** + **生成结束残留缓冲强制提交** —— 修复"只有首句/没说完"的
    短回答丢失 bug:原实现"你好。"等短句凑不满字数阈值,末尾内容被直接丢弃);
  - **降延迟默认值**: endpoint_ms 1000→600、max_new_tokens 180→80、首段 12 字快切、
     提示词限短句;`models.llm` 已切换为 **Qwen2.5-1.5B-Instruct**(本地现成,首字再降 ~0.8s;
     对答质量要求高可一行改回 Qwen3-4B-Instruct-2507)。
- **在线实测(2026-08-27, real 模式, Qwen2.5-1.5B)**: 两轮模拟器 voice 场景 7/7 PASS;
  - 第 1 轮: 端点→LLM 首字 **0.77s**, 端点→首块音频 **2.97s**, 整轮 **6.8s**;
  - 第 2 轮: 端点→LLM 首字 **0.41s**, 端点→首块音频 **1.88s**, 整轮 **8.0s**;
  - 对比修复前基线(4B): 首字 1.61s / 首块 4.47s / 整轮 18.7s。
  - 其余源码(网络服务、协议、证书、发布物、模拟器)原样收录。
- **在线诊断记录(2026-08-27, real 模式实机跑通)**: 板卡模拟器 WSS 接入(150 帧 PCM1)→ 背景校准(-70.7 dBFS)→ VAD(1.5s 音频)→ ASR "风十一度"(0.58s)→ Qwen3 回答 3 段(首字 1.609s)→ CosyVoice2 逐句合成 → 下行 SPKS+614 PCM帧+SPKE,端点→首块音频 4.468s,整轮 18.65s;模拟器断言 7/7 PASS,结果见 `server/runs/20260827_111149/voice_qa_results.csv`。
- 原项目根的网络版说明文档一并收录于 `docs/` 与 `server/`(README/真机联调指南/语音工作流设计/网络服务器改造方案/变更统计)。
