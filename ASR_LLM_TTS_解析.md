# ASR–LLM–TTS 模块解析（接手文档）

> 适用代码基线：`20260819_224201` 工作目录。本文档面向刚接手该模块的维护者，按"先看整体、再看组件、最后看代码地图与坑"的顺序组织。

---

## 0. 项目一句话定位

这是一个 **ESP32-S3 开发板（带麦克风+扬声器）↔ RTX 5090 电脑** 的**本地语音导游/对话一体机**：

- 板卡只负责采集 PCM 音频（20ms/帧，16kHz）和播放 PCM（24kHz），不做任何 AI 推理；
- 5090 电脑上跑完整链路：**环境 VAD → FunASR(Paraformer-online) → Qwen3-4B → 按标点分句 → CosyVoice2 → 回板卡扬声器**；
- 当前形态是"唤醒词 + 对话状态机"（V5 两级硬件唤醒），另有网络化改造分支（`virtual_server/`，MQTT/HTTPS/WSS 三种通道）。

---

## 1. 全链路数据流（最重要的图）

```
ESP32-S3 开发板                  RTX 5090 电脑
┌────────────────────┐          ┌──────────────────────────────────────────────┐
│ 麦克风 16kHz ADC   │  USB串口  │  ① read_frame()  解析 PCM1 帧(656B/20ms)     │
│ 每20ms打包一帧:     │ 921600baud│  ② capture_until_endpoint() 能量VAD         │
│  16B头+640B PCM     │──────────>│     （背景dBFS相对阈值+滑动窗起始+尾静音端点） │
│ 头: PCM1|seq|len|   │  PCM1帧流 │  ③ recognize()  FunASR 流式识别(600ms块)    │
│     level|sum8     │          │     ── 识别文本 "请介绍这个文物" ──           │
│                    │          │  ④ Qwen3-4B 流式生成(TextIteratorStreamer)   │
│                    │          │  ⑤ sentence_chunks() 按标点切成句子           │
│                    │          │  ⑥ 每句/每段 → tts_worker(CosyVoice2)        │
│                    │          │     文件队列IPC: request_json+chunk_pcm      │
│ 扬声器 I2S 24kHz   │  SPKS/SPKD │  ⑦ tts_stream_to_board() 读chunk→串口推流    │
│                    │<──────────│     SPKS 24000 → SPKD <pcm> ×N → SPKE        │
└────────────────────┘  裸PCM    └──────────────────────────────────────────────┘
```

- **ASR 输入**：板卡 PCM1 帧（16 字节头 + 640 字节 = 320×int16 LE 单声道 16kHz，20ms）。
- **ASR 输出**：中文文本；**LLM 输入**：system prompt + 最近 4 轮对话历史 + 本轮用户话；**LLM 输出**：流式 token；**TTS 输入**：按标点切好的句子；**TTS 输出**：24kHz 单声道 PCM，经串口 SPKD 帧送回板卡播放。

---

## 2. 三大模型组件

| 组件 | 模型 | 目录 | 大小 | 加载/推理方式 |
|---|---|---|---|---|
| ASR | FunASR + Paraformer-online（流式） | `models/paraformer-zh-streaming/`（完整 ModelScope 快照） | ~848MB | `funasr.AutoModel(model=本地快照, device=cuda:0, disable_update=True)` |
| LLM | Qwen3-4B-Instruct-2507 | `models/Qwen3-4B-Instruct-2507/` | ~7.7GB | transformers `AutoModelForCausalLM` + `TextIteratorStreamer`，`device_map="auto"` |
| TTS | CosyVoice2-0.5B | `models/CosyVoice2-0.5B/` | ~5.4GB | `third_party/CosyVoice` 源码，`CosyVoice2` 类，`inference_zero_shot(..., stream=True)`，fp16 |

> 注意：只加载这三个模型，**不考虑 Qwen2.5-1.5B**（`models/` 里还有它，是早期/对照用，当前 config 不引用）。

### 2.1 ASR 细节（`asr_eval_core.py` + `board_serial_asr_test.py`）

- **加载** `load_paraformer()`：`device="auto"` → 有 CUDA 用 `cuda:0`；`disable_update=True` 避免连 ModelScope 网络。若传入的恰好是别名 `paraformer-zh-streaming` 且缓存快照齐全，会自动改用 ~/.cache/modelscope 下的快照；流水线里传入的是**本地目录路径**，直接使用本地快照。
- **识别** `recognize(model, samples)`：先归一化 int16→float，然后按流式 chunk 切分：
  - `chunk_size = [0, 10, 5]` → 步长 `10 × 960 = 9600` 样本 = **600ms/块**；
  - `cache` 字典跨块传递状态；`is_final=True` 只对最后一块；
  - `encoder_chunk_look_back=4, decoder_chunk_look_back=1`；
  - 每块结果取 `result[0]["text"]` 拼接 → 返回 `(全文, 首个部分结果的秒数, 总耗时)`。
- **注意**：这里"流式"是指对**已录完的整段音频**按 600ms 块做流式推理（不是边录边识）；ASR 在 VAD 判定说完后才开始。

### 2.2 LLM 细节（`realtime_pipeline.py` / `voice_daemon.py` 内联）

- 对话构造：`[system] + history[-history_turns*2:] + [user]`，用 `tokenizer.apply_chat_template(..., add_generation_prompt=True)` 手拼 prompt（不传 messages，而是先拼成字符串再 tokenize）。
- 生成：**后台线程**里 `llm.generate(...)` 带 `TextIteratorStreamer(skip_prompt=True)`；主线程消费 streamer 迭代器，实现"边生成边切句边送 TTS"。
- 参数：`do_sample=False`（贪心）、`max_new_tokens=96`（V4 联调版）/ **180**（V5）、`history_turns=4`。
- V5 自检时用 `max_new_tokens=2` 快速验证 Qwen 可用。

### 2.3 TTS 细节（`tts_worker.py` + `third_party/CosyVoice`）

- **独立子进程**（`.venv_5090_tts`）运行，进程内 `CosyVoice2(model_dir, load_jit=False, load_trt=False, load_vllm=False, fp16=True)`。
- 合成方式：**zero-shot 音色克隆**，参考音取自 `tts_roles_5090/results/reference_manifest.json` 的 `museum_female`（博物馆女声），参考文本同文件；找不到就退回 `third_party/CosyVoice/asset/zero_shot_prompt.wav` + `"希望你以后能够做的比我还好呦。"`。
- `inference_zero_shot(text, prompt_text, prompt_wav, stream=True)` 逐块产出 `result["tts_speech"]`（24kHz 单声道 tensor），即时转 int16 PCM 写 `chunk_XXXXXX.pcm`——这就是"**首块音频不用等全文生成完**"的实现基础。
- 采样率：`model.sample_rate`（CosyVoice2 为 24000）。

### 2.4 主进程 ↔ TTS 子进程 IPC（文件队列，无 socket）

TTS 工作目录：`next_stage/full_pipeline_5090/runs/<时间戳>/tts_queue/`（V5 用 `next_stage/voice_sleep_v5_5090/runs/.../tts_queue/`）。

| 文件 | 作用 |
|---|---|
| `ready.json` | 子进程启动完成（模型加载完）写一次，主进程轮询等待 |
| `request_<id>.json` | 主进程写请求：`{id, text, output_wav, stream_dir?}` |
| `stream_<id>/chunk_000000.pcm` | 子进程流式产出音频块（24kHz int16），按序号递增 |
| `response_<id>.json` | 子进程完成写：`{ok, first_audio_seconds, total_seconds, audio_seconds, sample_rate, stream_chunks}` |
| `request_shutdown.json` | 退出信号：`{id:"shutdown", command:"shutdown"}` |

主进程两个消费者函数：
- `tts_request()`（非流式，等完整 wav）；
- `tts_stream_to_board()`（流式：轮询 chunk 文件→串口 SPKD，直到 response 到达并确认 chunk 数齐）。

**为什么分两个 venv**：`.venv_5090_llm`（funasr + transformers + torch）和 `.venv_5090_tts`（torchaudio + CosyVoice 全家）依赖栈冲突，CosyVoice 要求 torchaudio.load 被替换成 soundfile 实现（`load_audio_compat`——见 tts_worker.py 顶部 monkey-patch）。

---

## 3. 板卡串口协议（5090 侧视角）

- 上行帧（板卡→5090）：`PCM1` 魔数 + seq(uint32 LE) + bytes(uint16 LE, 恒640) + level(int16 LE, dBFS×100) + 保留3B + sum8 = **16B 头 + 640B PCM = 656B/20ms**。校验：seq 连续、sum8 一致（不一致则丢帧重找）。
- 下行命令（5090→板卡，纯字节流，无帧头魔数，靠 4 字节命令字）：

| 命令字 | 载荷 | 作用 |
|---|---|---|
| `SPKS` | uint32 LE 采样率(24000) | **必须最先发**，否则板卡丢弃 PCM |
| `SPKV` | uint32 LE 0-100 | 音量 |
| `SPKD` | uint32 LE 长度 + PCM | 音频数据块（≤2048B） |
| `SPKE` | uint32 LE 0 | 停止播放 |
| `MICS` | uint32 LE 背景 dBFS×100 | 进入硬件休眠（一级触发阈值=背景+5dB，连续120ms） |
| `MICW` | uint32 LE 0 | 恢复持续上传 |

> 完整协议（含 WSS/MQTT/OTA 版本）见根目录 `PROTOCOL.md`。5090 侧推流节奏：`ser.flush()` 后 `sleep(len/2/rate × 0.88)`，用 88% 倍速填板卡 I2S 缓冲，防止欠载拖音。

---

## 4. VAD（软件端点检测，`capture_until_endpoint`）

纯能量 VAD（无深度模型），全部参数在 config 的 `modes.*` 里：

- **起始**：300ms 滑动窗内，活跃样本（帧 RMS > 背景+start_threshold_db）累计 ≥ start_active_ms（120–160ms）才判定"人说话开始"；记录 `speech_start_sample`（会把窗口最前的音频纳入，保证不切头）。
- **端点**：说完后连续 1200ms（quiet）/1000ms（lab）/800ms（对话态）静音即截断；静音计数可被活跃音频"抵扣"（`endpoint_active_penalty=4`：1ms 活跃抵 4ms 静音），防止单个 20ms 尖峰/敲击误判端点。
- **兜底**：`max_record_seconds=15` 硬上限。
- 背景校准：开机时录 3–4 秒静音，算 RMS dBFS，作为所有阈值的基准（`calibration` 环节）。

---

## 5. 唤醒与对话状态机（V5 两级唤醒，核心卖点）

> 状态定义见 `next_stage/voice_sleep_v5_5090/voice_daemon.py` 的 `run_board_session()`。

```
[hardware sleep]  MICS：板卡本地算能量，不上传PCM/不录音/不跑ASR
      │  任何人声>背景+5dB 且持续120ms → 板卡把含前500ms预卷的临时音频上传
      ▼
[ASR 唤醒词校验]  5090 收流(VAD端点500ms, 上限8s) → ASR → 含"你好导游/你好小游/小游小游"？
      │ 是 → MICW 恢复持续上传，播提示音"我在..." → state=command
      │ 否 → 再发 MICS 回硬件休眠（零功耗等待）
      ▼
[command]  持续上传；用户说"开启对话/开始对话/…"
      │ 是 → 播"已开启对话"，state=dialog；否/无语音 → 回休眠
      ▼
[dialog]  VAD端点800ms → ASR → Qwen → 分句TTS流式播放
      │ 说"进入休眠/退出对话/…" → 播结束语，清历史，state=wake
      ▼  (回到硬件休眠)
```

**为什么分两级**：传统方案（V4）让板卡**持续**上传 PCM，5090 常驻 VAD+ASR，耗电/占带宽。V5 让板卡本地只做能量检测，只在疑似人声时才上传 **500ms 预卷 + 当前语音**，5090 只在这种时候跑一次 ASR 验证唤醒词。省电、省显存、减少无效推理。**前提是烧录配套 V5 固件**（`MICS` 命令在老固件上不会停流）。

启动时序（daemon 主流程）：
1. 先加载 3 个模型（等板卡插入前完成，**登录自启动**版本在 Windows 登录时就预加载）；
2. 预热缓存固定提示音（menu/startup_begin/startup_ready，用 TTS 生成后 `prompt_cache/` 持久化复用，避免每轮都合成）；
3. 等待板卡 → `connect_calibrate_and_self_check()`：连串口 → 录 3 秒背景 → 真实跑一遍 ASR/Qwen(2 token)/TTS 自检 → 播"自检完成" → 进状态机；
4. **外层死循环**：任何板卡拔插/串口错误只结束当前板卡会话，模型常驻内存，重新插上自动重校准+自检，不重启模型。

---

## 6. LLM 输出 → TTS 的分句策略（`sentence_chunks` + `submit_tts_segments`）

- `sentence_chunks(streamer)`：累积 token 流，遇到 `。！？!?；;\n` 立即切句；连续 48 字无句号则在 `，、：,` 处强行切（≥20 字），保证长句也能及时发声。
- V5 的 `submit_tts_segments`（在 daemon 内）：第 1 段攒够 **18 字以上且≥1 个完整句**就送 TTS（首响最快）；后续段攒到 **60 字**或 ≥2 句再送（语音韵律更连贯，减少停顿感）。
- 每段一个独立 `request_<id>.json` + 独立 `stream_<id>/`；主进程逐个消费（`board_started` 标志保证 SPKS 只发一次，段间不断播，最后统一 SPKE）。

---

## 7. 代码地图（按重要性）

| 文件 | 作用 | 关键函数/类 |
|---|---|---|
| `next_stage/voice_sleep_v5_5090/voice_daemon.py` | **V5 主程序（当前部署版）**：模型预加载、自检、两级唤醒状态机、Qwen 流式+分句 TTS | `main`, `run_board_session`, `answer`, `wait_for_hardware_sound_trigger`, `startup_self_check` |
| `next_stage/full_pipeline_auto_5090/realtime_pipeline.py` | V5 复用的公共工具库（被 daemon import 为 `rp`） | `resolve`, `start_tts_worker`, `tts_request`, `tts_stream_to_board`, `sentence_chunks`, `board_speaker_*`, `detect_board_port` |
| `next_stage/full_pipeline_5090/realtime_pipeline.py` | **V4 交互式联调版**：菜单自检、人工触发每轮对话、结果写 CSV | `main`, `self_check` |
| `next_stage/full_pipeline_5090/tts_worker.py` | TTS 子进程：CosyVoice2 加载、文件队列服务、流式写 chunk | `main`, `load_audio_compat` |
| `asr_eval_core.py` | ASR 加载/流式识别/工具（CER、GPU 快照、`load_audio_16k_mono`） | `load_paraformer`, `recognize_streaming`, `cer`, `extract_text` |
| `board_serial_asr_test.py` | **串口/VAD 的核心实现**：帧解析、能量 VAD、WAV 保存、识别封装 | `read_frame`, `capture_until_endpoint`, `capture_seconds`, `rms_dbfs`, `recognize`, `open_serial` |
| `next_stage/voice_sleep_v5_5090/config.json` | V5 配置（含 voice_control） | — |
| `config.json` / `next_stage/full_pipeline_5090/config.json` | V4 联调配置（含三模式 VAD） | — |
| `tts_roles_5090/results/reference_manifest.json` | TTS 音色参考（角色→wav+文本） | — |
| `virtual_server/real_engine.py` | **网络化版本**的真实语音引擎（WSS 一问一答，复用同一套 ASR/LLM/TTS） | `RealVoiceEngine` |

版本关系：

```
V4 单级连续上传:  voice_daemon_5090/voice_daemon.py（旧，16.9KB）
V5 两级硬件唤醒:  next_stage/voice_sleep_v5_5090/voice_daemon.py（当前主力，25.3KB）
     ├─ 联调交互版(旧): next_stage/full_pipeline_5090/*（00_启动完整语音对话.bat 用）
     ├─ 稳定快照:       next_stage/full_pipeline_5090_cli_stable_20260823/*
     └─ 被 V5 import:   next_stage/full_pipeline_auto_5090/realtime_pipeline.py
网络化改造:        virtual_server/*（真机联调前先用板卡模拟器验证协议）
```

> ⚠️ 根目录 `voice_sleep_v5_5090/`（有 start_v5_visible.cmd 等 bat）是**启动器目录**，里面的 `voice_daemon.py`/`config.json` 是更老的副本；`start_v5_visible.cmd` 实际运行的是 `next_stage/voice_sleep_v5_5090/voice_daemon.py`。改代码请改 `next_stage` 下的，不要改根目录副本。

---

## 8. 配置参数速查（V5 `next_stage/voice_sleep_v5_5090/config.json`）

| 配置 | 默认值 | 含义 |
|---|---|---|
| `serial.port` | `"auto"` | 自动探测 ESP32-S3（VID 0x303A/PID 0x1001） |
| `serial.baud` | 921600 | 串口波特率 |
| `models.*` | 本地三模型路径 | ASR/LLM/TTS |
| `conversation.system_prompt` | 博物馆导游提示词 | 要求 4–6 短句、100–140 字、直接朗读、不编造 |
| `conversation.max_new_tokens` | 180 | V5 下回答偏长（服务器实测 46s），一问一答建议 60–120 |
| `conversation.history_turns` | 4 | 保留最近 4 轮（8 条消息） |
| `voice_control.wake_words` | 你好导游/你好小游/小游小游 | 二级唤醒词（ASR 验证） |
| `voice_control.dialog_exit_words` | 进入休眠/退出对话/结束对话… | 对话退出触发词（子串匹配） |
| `voice_control.default_mode` | lab | 只用 lab 一种 VAD 模式（V4 有 quiet/lab/noise 三档） |
| `voice_control.background_seconds` | 3 | 开机背景校准时长 |
| `voice_control.wake_listen_seconds` | 8 | 一级触发后收听唤醒词的最长时长 |
| `voice_control.command_listen_seconds` | 6 | command 态收听"开启对话"的最长时长 |
| `voice_control.wake_endpoint_silence_ms` | 500 | 唤醒词/命令的尾静音端点 |
| `voice_control.dialog_endpoint_silence_ms` | 800 | 对话的尾静音端点 |
| `voice_control.hardware_trigger_*` | +5dB / 120ms / 500ms | 固件侧一级触发阈值（需与固件一致） |
| `voice_control.first_tts_segment_chars` | 28 | 首段 TTS 目标字数（实际 18 字起） |
| `voice_control.later_tts_segment_chars` | 60 | 后续段目标字数 |
| `tts.role` | museum_female | 音色角色（见 reference_manifest） |
| `tts.board_volume_percent` | 100 | 板卡音量 |
| `tts.stream_chunk_bytes` | 1024 | 每 SPKD 块字节数（≤2048 上限） |

---

## 9. 运行入口

| 入口 | 用途 |
|---|---|
| `00_menu.bat` | 主测试菜单（ASR 专项：4/4p/4a–4h/4s） |
| `00_启动完整语音对话.bat`（= 根目录同一份的菜单） | **V4 联调**：自检→选安静/实验室/噪声→插板→人工触发问答→结果 CSV |
| `00_RUN_V5_TWO_STAGE_WAKE.bat` | **V5 可见测试**（当前推荐） |
| `voice_sleep_v5_5090/install_autostart.cmd` / `start_voice_daemon.cmd` | V5 随 Windows 登录自启（vbs 隐藏窗口 + status 文件） |
| `voice_sleep_v5_5090/check_status.cmd` | 看 daemon 状态（`voice_daemon.status.txt`：starting/loading_asr/loading_tts/loading_qwen/models_ready_waiting_for_board/calibrating_background/sleeping_waiting_for_wake_word/...） |
| `virtual_server/start_virtual_server.cmd` | 网络化服务器（板卡经 MQTT/HTTPS/WSS 连接，替代串口） |
| 单个测试：`04_test_funasr_asr.bat`、`05_test_qwen3_http.bat`、`06_test_cosyvoice2_http.bat`、`07_test_pipeline_http.bat` | 分组件独立验证 |

输出位置：
- V5 每会话：`next_stage/voice_sleep_v5_5090/runs/<时间戳>/`（audio/、tts/、voice_daemon_results.csv）；
- V4 联调：`next_stage/full_pipeline_5090/runs/<时间戳>/pipeline_results.csv`；
- ASR 专项：`results/`、`results_5090/`；录音在 `recordings/`。

---

## 10. 已测性能（RTX 5090，来自 `变更统计.md` 实测）

- 模型加载：约 20–30s（三个模型都在显存里常驻）；
- ASR：3s 语音识别 0.54s；
- 首句 TTS 下行：约 10s（含 Qwen 首 token 延迟 + 首段合成）；
- Qwen 默认 180 token 回答约 46s 音频（**偏长**，联调时按场景调 `max_new_tokens`）；
- 板卡→5090 延迟：20ms/帧持续传输，VAD 端点后即开始 ASR。

---

## 11. 已知坑（接手必读）

1. **V5 必须配 V5 固件**：老固件不认 `MICS`，板卡不会停止上传，硬件休眠形同虚设。改固件协议前先读 `PROTOCOL.md`（与固件逐条对应）和 `julia-fused-base/`。
2. **根目录 vs next_stage 双副本**：`voice_sleep_v5_5090/`（根）和 `next_stage/voice_sleep_v5_5090/` 各有一份 daemon，后者才是启动器实际运行的。改完要同步部署副本，否则"改了没生效"。
3. **GBK 控制台**：`全角/emoji` 打印可能崩（`变更统计.md` 提到原 `sentence_chunks` 的 print 问题）；bat 普遍 `chcp 65001`，新代码避免在 Windows 控制台打印非 GBK 字符。
4. **TTS 参考音**：`museum_female_reference.mp3`（在线参考）为 0 字节——在线角色参考没下载成功，实际用的是 `outputs/references/museum_female_reference.wav`（manifest 里指向 wav 即可；若 manifest 缺失会退回 zero_shot_prompt.wav，音色会变）。
5. **Qwen 长回答**：system prompt 要求 100–140 字（约 20s 朗读），与 `max_new_tokens=180` 要配合调；对话历史只有 4 轮，长会话会失忆。
6. **串口驱动**：`detect_board_port` 认 VID 0x303A；插拔后 daemon 会自己重连（V5 外层 while 循环），但自检/R 校准会重新跑一遍（约 1 分钟）。
7. **TTf 子进程退出**：`tts_worker` 遇到合成异常会写 `response ok:false` 并继续服务，但若 CosyVoice 崩了（OOM 等），`start_tts_worker` 的 180s 等待和主进程重试逻辑要留意。
8. **网络化版本**（`virtual_server/`）是另一条线：一问一答、无唤醒词状态机，靠 WSS Bearer token + 板卡设备 ID；真机联调前先用 `board_simulator.py` 验收（`变更统计.md` 第 6 节：OTA 28 项、Audio 16 项、Voice 4 项 PASS）。
9. **文件队列 IPC 的落盘**：request/response 通过文件存在性轮询，`run_dir` 在 `runs/<时间戳>`，跑久了会积累大量 wav/pcm，注意磁盘回收。
10. **性能权衡**：V5 是"省电优先"；如果要更低的对话延迟（唤醒→开口），方向是缩短 `first_tts_segment_chars`（但会牺牲韵律）、或预唤醒词缓存提示音（已做）、或把 LLM 换小模型（Qwen2.5-1.5B 在 models/ 里备着）。

---

## 12. 建议的掌握顺序（一周内可完成）

1. 跑一遍 `00_RUN_V5_TWO_STAGE_WAKE.bat`，观察 status 文件变化与日志，对应第 1/5 节流程；
2. 读 `board_serial_asr_test.py`（VAD 核心）→ `asr_eval_core.py`（ASR）→ `tts_worker.py`（TTS 子进程）→ `realtime_pipeline.py`（工具库）→ `voice_daemon.py`（状态机）；
3. 用 `04*.bat` 单独测 ASR 准确率，用 `07_test_pipeline_http.bat` 看 HTTP 接口（若走服务器线）；
4. 改任何配置前先读 `PROTOCOL.md` 确认固件行为；改固件前把 `julia-fused-base` 里对应实现确认一遍。
