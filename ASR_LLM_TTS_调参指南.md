# ASR–LLM–TTS 调参指南

> 前提：先读 `ASR_LLM_TTS_解析.md`（结构）与 `ASR_LLM_TTS_代码详解.md`（逻辑），本文件只讲"改什么、怎么改、改完什么变化"。
> 所有"当前值"基于 `next_stage/voice_sleep_v5_5090/config.json`（V5 当前部署版）与 `board_audio.c` 固件常量。

---

## 0. 最重要的一课：参数分三层，别改错地方

| 层 | 位置 | 例子 | 改法代价 |
|---|---|---|---|
| **L1 配置层** | `next_stage/voice_sleep_v5_5090/config.json` | VAD 阈值、TTF 分段字数、token 上限、提示词 | 改文件 → 重启 daemon，零风险 |
| **L2 代码常量层** | `*.py` 里的字面量 | 首段 ≥18 字、推流 0.88 倍速、`voice_start_ms=20`（唤醒分支）、600ms 安静确认、180s 超时 | 改代码 → 重启 daemon，注意同步部署副本（根目录 `voice_sleep_v5_5090/` 与 `next_stage/` 各一份） |
| **L3 固件层** | `julia-fused-base/components/julia_board_audio/board_audio.c` | 一级触发 +5dB / 连续120ms / 预录500ms | 改 C → **重新编译+烧录**，风险最高 |

⚠️ **特别警告**：`config.json` 里的 `voice_control.hardware_trigger_above_background_db / hardware_trigger_active_ms / hardware_preroll_ms` **只是文档镜像**。固件真正读的是：

```c
#define SLEEP_THRESHOLD_DELTA_X100 500   // +5dB
#define SLEEP_TRIGGER_FRAMES 6           // 6帧×20ms = 120ms
#define SLEEP_PREROLL_FRAMES 25          // 25帧×20ms = 500ms
```

想改"一级触发灵敏度"只改 config **是无效的**，必须改 C 并重新烧录。这一点接手人最容易踩。

---

## 1. 症状 → 调参速查表（先对号入座）

| 症状 | 首选参数 | 方向 |
|---|---|---|
| 唤醒总不触发 | L3 固件 `SLEEP_THRESHOLD_DELTA_X100` | ↓（如 500→400） |
| 环境吵，人声以外的响声也唤醒 | L3 同上 / L1 `wake_words` 加长 | ↑ 或换更独特的唤醒词 |
| 唤醒后没听清"开启对话" | L1 `wake_endpoint_silence_ms` / `command_listen_seconds` | ↑（500→700；6→8） |
| 说话开头被切 / 第一个字丢失 | L1 `start_threshold_db`↓、`start_active_ms`↓；L2 唤醒分支 `voice_start_ms=20` 已是低值 | — |
| 话说到一半被截断（尤其说得慢的人） | L1 `endpoint_silence_ms` / `endpoint_active_penalty` | ↑（800→1000；4→6） |
| 说完要等很久才识别 | L1 `endpoint_silence_ms` | ↓（但↑截断风险，谨慎） |
| 回答太长（40s+），用户等得烦 | L1 `max_new_tokens` / `system_prompt` 字数要求 | ↓（180→60~120） |
| 首响太慢 | L1 `first_tts_segment_chars` / `max_new_tokens` | ↓（28→20；180→80） |
| 说话像蹦字、一顿一顿 | L1 `later_tts_segment_chars` | ↑（60→80~100） |
| 播放大段有拖音/破音 | L2 推流 0.88 系数 / L1 `stream_chunk_bytes` | 0.88→0.92 / 1024→2048 |
| 对话中说"休眠""退出"等词误退出 | L1 `dialog_exit_words` | **缩短词表**（只留最明确的短语） |
| 音色不对/像换了一个人 | L1 `tts.role` + `reference_manifest.json` | 检查 manifest 指向的 wav；或换参考音 |
| 历史记不住前文、回答跑题 | L1 `history_turns` | ↑（4→6），注意显存 |
| 回答每次都一样，太死板 | L2 `do_sample=False` → `True`+`temperature` | 需改代码 |
| 环境一吵就乱触发 | L1 `start_threshold_db`↑；V4 用 noise 模式 | ↑（3→5~6） |
| 背景校准录进了说话声 | L1 `background_seconds`（3s） | 保证绝对安静地重启 |

---

## 2. VAD 调参详解（L1，`config.json → modes.lab`）

VAD 是**纯能量**的，全部参数的含义与调试思路：

| 参数 | 当前值 | 含义 | 调法 |
|---|---|---|---|
| `start_threshold_db` | 3 | 开始说话判定：背景+3dB | 环境噪声大 → 5~6；收音远/近？麦克风灵敏度差异大时调到 2 试试 |
| `end_threshold_db` | 3 | 说话结束判定阈值（一般=start 或更低） | 若环境有持续低噪但人声更大，可略升 |
| `start_active_ms` | 120 | 窗内活跃样本≥120ms 才算"开始" | 太灵敏（咳嗽触发）→ 160~200；说话软绵绵 → 80~100 |
| `start_window_ms` | 300 | 起始检测滑动窗 | 一般不动（它决定"回退录音起点"的长度，也决定头字是否被切） |
| `endpoint_silence_ms` | 1000（lab） | 说后静音多久算说完 | **体验影响最大的参数**：说了话要等 1s 才识别，想快 → 800；切句子 → 1000+ |
| `endpoint_active_penalty` | 4 | 1ms 活跃抵 4ms 静音 | 击键声/急促键盘导致误判说完 → ↑；正常说话被打断 → 保持 |
| `max_record_seconds` | 15 | 单轮上限 | 长讲解 → 20；短问答 → 10 |

**联动说明**：V5 中对话态用 `dialog_endpoint_silence_ms=800`、唤醒/command 态用 `wake_endpoint_silence_ms=500`，这两个优先于 `mode.endpoint_silence_ms`（代码里 `fast_endpoint` 覆盖）。`modes` 里保留的三个档位（quiet/lab/noise）只在 `full_pipeline_5090` 联调版生效，V5 只用 `default_mode` 指定的一档。

**验证方法**：`runs/<时间戳>/voice_daemon_results.csv`（或联调版 `pipeline_results.csv`）里有 `vad_speech_started / vad_endpoint_triggered / audio_seconds / recognized_text`；`capture_until_endpoint` 返回的 endpoint 字典里还有 `speech_start_seconds / trailing_silence_ms / post_speech_wait_seconds` 可直接打印观察。

---

## 3. 唤醒两级调参

### 3.1 一级（板卡，L3 固件）

| 常量 | 当前 | 说明 |
|---|---|---|
| `SLEEP_THRESHOLD_DELTA_X100` | 500（5dB） | 越低越灵敏（电视声/说话都触发），越高越省（大字声才触发） |
| `SLEEP_TRIGGER_FRAMES` | 6（120ms） | 触发需要持续高于阈值；短促噪声（关门声）↑到 8~10 可滤掉 |
| `SLEEP_PREROLL_FRAMES` | 25（500ms） | 预录缓冲；唤醒词说太快/第一个字是"你"时，↑到 30（600ms）更稳 |

固件触发后**持续上传直到 MICW/MICS 切换**，所以 5090 侧一定能收到"说完后的静音"来判端点——不用担心收不到尾静音。

### 3.2 二级（5090 ASR 校验，L1 + L2）

| 参数 | 当前 | 说明 |
|---|---|---|
| `wake_words` | 你好导游/你好小游/小游小游 | **子串匹配**（`normalize(word) in value`），词越独特越不易误判；3 个词 = 3 个入口，想更严只留 1 个 |
| `wake_listen_seconds` | 8 | 触发后最多听 8s；超时且无完整语音 → 直接回休眠（打印"一级触发后未形成完整唤醒语音"） |
| `wake_endpoint_silence_ms` | 500 | 唤醒词说完后静音 500ms 即截断；说话含明显停顿的人 → 600~700 |
| L2 `voice_start_ms=20` / `voice_start_window_ms=500` | 唤醒分支**硬编码** | 比对话态更灵敏（随时可能只有半句唤醒词？）。修改点为 `voice_daemon.py` `run_board_session` 的 wake 分支 |

**误触发成本提示**：一级触发不花钱；二级失败 = 1 次 ASR（~0.5s）+ 65KB 串口流量 + 5090 与板卡间一次 MICS 重置。若现场经常有游客对话，考虑把 `SLEEP_THRESHOLD_DELTA_X100` 升到 600。

---

## 4. LLM 调参（L1 配置 + L2 代码）

| 参数 | 当前 | 说明 | 调法 |
|---|---|---|---|
| `system_prompt` | 博物馆导游提示词（4–6 句/100–140 字/20s 朗读） | **产品效果的最大杠杆** | 改这个就能改回答风格、长度、是否结巴。要求"适合直接朗读"很重要——CosyVoice 对口语化文本合成更自然 |
| `max_new_tokens` | 180 | 实测 46s 音频，太长 | 一问一答 60~120；先改 prompt 再兜底改 token 上限 |
| `history_turns` | 4 | 历史轮数（×2 条消息） | 长对话/连续追问 → 6；显存紧或回答变短 → 3。注意：历史太长会让 Qwen 回答变短（训得爱总结） |
| `do_sample` | `False`（L2，`voice_daemon.py` 硬编码） | 贪心解码 | 要多样性 → `do_sample=True, temperature=0.7, top_p=0.9`（需要小改代码） |
| `torch_dtype`/`device_map` | auto | 显存分载 | 5090 32G 无压力；换小卡可改 `torch_dtype=torch.float16` 或量化加载 |

**最佳实践**：让 prompt 承担大部分长度控制（"只回答2到3个短句""总长度控制在100字以内"），`max_new_tokens` 当保险丝（防止模型失控输出长篇）。

---

## 5. TTS 调参

### 5.1 分段策略（L1 + L2）

| 参数 | 当前 | 说明 |
|---|---|---|
| `first_tts_segment_chars` | 28 | 首段目标字数；但代码里**硬编码 `chars >= 18` 且 ≥1 个完整句**（`voice_daemon.py:344`）才送——实际首段 ≈18~28 字。再想快 → 把 18 改小（有蹦字风险） |
| `later_tts_segment_chars` | 60 | 后续段目标；**60 字大约 6~8s 语音**，韵律连贯的关键。↑到 80~100 → 更连贯但首段后停顿变长（因为要攒） |
| L2 `sentence_chunks` 48 字守护 | 无标点 48 字强切 | 中文回答带 emoji/英文时切分行为要注意 |

**权衡本质**：首段字符数 ↓ → 第一声快但"字蹦感"强；↑ → 自然但慢。推荐先保持 28/60，实测主观感受再动。

### 5.2 音色与合成（L1）

| 参数 | 当前 | 说明 |
|---|---|---|
| `tts.role` | museum_female | 从 `reference_manifest.json` 选角色；**参考音频质量决定了 zero-shot 音质的 80%**（要 5~10s、单声道、清晰、无混响） |
| `reference_manifest` | `tts_roles_5090/results/...` | 注意 `museum_female_reference.mp3` 是 0 字节（在线参考没下载成功），实际用的是 wav |
| `stream_chunk_bytes` | 1024 | SPKD 拆分块；≤2048（固件 `MAX_SPK_BYTES=4096`，但串口 921600 波特率下 2048 更稳）。改大 → 每块要 22ms 左右传完，块太大有节奏影响 |
| `board_volume_percent` | 100 | 音量；板卡音量瞬时生效 |

### 5.3 性能选项（L2，改动需谨慎）

`tts_worker.py` 中 `CosyVoice2(..., load_jit=False, load_trt=False, load_vllm=False, fp16=True)`：
- `load_vllm=True`：需要装 vllm 且其环境与 CosyVoice 兼容，**速度可显著提升**，但当前离线环境不一定能装；
- 0.5B 本身在 5090 上合成速度已接近实时（实测首块 ~2-3s 内），一般不用动。

---

## 6. 串口/播放调参（L2 代码常量）

| 常量 | 位置 | 当前 | 说明 |
|---|---|---|---|
| 推流倍速 0.88 | `realtime_pipeline.py:board_speaker_chunk` | sleep(音频时长×0.88) | ↓ 0.80 → 每块间更缓冲，欠载拖音更少，但板卡缓冲更深、中断响应更慢；↑ 0.95 → 更实时但更易欠载。**先试 0.92** |
| `max_bytes`（SPKD 块） | 同一函数 | 2048 | 与 `stream_chunk_bytes` 取小值；改串口波特率时必须复核 |
| 串口波特率 | config `serial.baud` | 921600 | 理论 656B/20ms ≈ 32.8KB/s，921600 波特率余量充足；低波特率时改 0.88 和块大小 |
| 超时（180s） | `start_tts_worker` / `tts_request` / `tts_stream_to_board` | 180 | 模型加载慢或显存吃紧时可能超时 → 改 300 |

**半双工提示**：板卡播放时麦克风暂不采集（无 barge-in），所以"打断"功能需要固件层改造（`board_audio.c:playing` 逻辑），不是调参能解决的。

---

## 7. 资源/运行调参

| 项 | 当前 | 说明 |
|---|---|---|
| `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` | V5 代码 94 行设置 | 减少显存碎片；显存紧可保留 |
| 模型常驻 | V5 设计如此 | 加载 20~30s 一次性；板卡拔插不重载 |
| `background_seconds` | 3 | 校准时长；环境稳定取 3，波动大取 5（会拖慢启动） |
| 提示音缓存 | `prompt_cache/` + `prompt_path()` 版本字典 | **改了 `prompt_texts` 必须同步改 versioned 字典中的文件名**（如 `menu_auto_v1.wav` → `v2`），否则旧的缓存音频还会被复用 |
| 日志/产物清理 | `runs/<时间戳>/` | 一段对话产生几十 MB（wav+pcm+csv），注意磁盘；无自动清理 |

---

## 8. 调参方法论（重要！）

1. **一次只改一个参数**，其他全部保持——能量 VAD 的参数互相耦合（改 `endpoint_silence_ms` 会掩盖 `endpoint_active_penalty` 的问题）。
2. **用数据说话**：`runs/<时间戳>/voice_daemon_results.csv` 的列（`vad_seconds / asr_seconds / qwen_first_text_seconds / qwen_tts_first_audio_seconds / tts_audio_seconds`）就是指标仪表盘；联调版 `pipeline_results.csv` 更全。
3. **先调"体验最痛"的三处**（新手推荐顺序）：
   - `endpoint_silence_ms`（回答等待 × 截断风险）→ 800±200 试
   - `first_tts_segment_chars` / `later_tts_segment_chars`（首响 × 连贯性）
   - `max_new_tokens` + `system_prompt`（回答长度与节奏）
4. **联动测试脚本**：`04d_realtime_param_sweep.bat`（ASR 侧 20/40ms 包 × 300/600/900ms 窗口扫描）和 `04b/04c` 做稳定性/基线；改了 VAD 后先跑 `04b`（5 次重复稳定性）再上真机。
5. **改 L2/L3 前的强制动作**：
   - L2：`git diff` 确认改动点，同步两份目录（`next_stage/voice_sleep_v5_5090/` 与根目录 `voice_sleep_v5_5090/` 的部署副本）；
   - L3：看完 `PROTOCOL.md` 再动固件，改完必须烧录，**先测 MICS/MICW 行为**（`board_simulator.py` 可做协议验证）。
6. **回归清单**：每轮调参后验证 ①唤醒真实命中 ②对话中说"进入休眠"能退出 ③拨插板卡自动重连 ④自检全部通过。
