# ASR–LLM–TTS 代码逻辑详解（逐函数讲解版）

> 配套文档：`ASR_LLM_TTS_解析.md`（架构/文件地图）← 本文件讲"代码到底怎么跑"。
> 阅读顺序建议：先看 §0 的调用关系，再按 §1→§7 顺序读，每节末尾都有"这一节在干嘛"小结。
> 行号基于当前工作目录代码，改动后以实际为准。

---

## 0. 调用关系总图（进程 × 内存线程）

```
进程 A：voice_daemon.py（主程序，.venv_5090_llm）
├─ 主线程：状态机循环（校准→自检→wake/command/dialog 无限循环）
├─ 生成线程 A1：llm.generate()  → 把 token 塞进 TextIteratorStreamer
├─ 切句线程 A2：迭代 streamer → 切句 → 写 request json → 推 segment 队列
└─ 主线程（消费）：segment 队列 → tts_stream_to_board() → 串口推 PCM

进程 B：tts_worker.py（.venv_5090_tts，由 A 用 subprocess 拉起）
└─ 单线程循环：扫 request_*.json → CosyVoice2 合成 → 写 chunk_*.pcm + response json

IPC 通道：磁盘目录 runs/<时间戳>/tts_queue/（request/response/chunk 全是文件）
```

数据从一个"量"变到另一个"量"的五个阶段：

```
PCM 帧流 ──VAD──> 一段完整音频(numpy int16) ──ASR──> 中文文本
   ──LLM流式──> token 流 ──切句──> 句子队列 ──TTS──> 24kHz PCM ──串口──> 板卡出声
```

**五个阶段各自的责任**：
| 阶段 | 谁负责 | 输入 | 输出 | 核心难点 |
|---|---|---|---|---|
| ① 收帧 | `read_frame` | 串口字节流 | 20ms 音频块 | 字节流里找边界、校验 |
| ② VAD | `capture_until_endpoint` | 帧流 | "一段完整的话" | 什么时候算说完 |
| ③ ASR | `recognize` | 一段音频 | 文本 | 流式模型的 cache |
| ④ LLM | `answer` | 文本 | 句子队列 | 边生成边切（线程） |
| ⑤ TTS | `tts_worker` + `tts_stream_to_board` | 句子 | 板卡声音 | 跨进程通信 + 推流节奏 |

---

## 1. 背景知识（30 秒速成）

- **ASR**（语音转文字）：输入波形，输出文字。本项目用 Paraformer 流式版，特点是"边吃音频边出字"，还能保留上下文句子的模糊记忆。
- **LLM**（大语言模型）：输入文字对话，输出文字回答。本项目 Qwen3-4B，回答通过流式接口"一个字一个字"吐出来。
- **TTS**（文字转语音）：输入文字，输出波形。本项目 CosyVoice2 用 **zero-shot（零样本音色克隆）**：给一句参考音频+对应文本，就模仿那个人的音色说话。
- **VAD**（语音活动检测）：判断"人什么时候开始说话、什么时候说完"。本项目是**纯能量 VAD**——只算音量大小，不用模型。

---

## 2. 阶段①：串口字节流 → 20ms 音频帧（`read_frame`）

板卡每 20ms 发一个 656 字节的包：`"PCM1"(4B) + seq(4B) + len(2B) + level(2B) + 保留(3B) + sum8(1B) + PCM(640B)`。

代码逻辑（`board_serial_asr_test.py:87-108`）：

```python
def read_frame(ser):
    magic = b"PCM1"
    window = bytearray()
    while True:
        b = ser.read(1)                    # ① 一字节一字节读
        window.extend(b)
        if len(window) > 4: del window[0]  # ② 只保留最近4字节作滑动窗
        if bytes(window) == magic:         # ③ 找到帧头魔数 → 跳出
            break
    rest = read_exact(ser, 12)             # ④ 读头部剩余12字节
    header = magic + rest
    seq, payload_len, dbfs_x100 = unpack(...)  # ⑤ 解出序号/负载长度/音量
    payload = read_exact(ser, payload_len) # ⑥ 读640字节音频
    if checksum8(payload) != header[15]:   # ⑦ 校验和不对 → 丢帧重新找帧头
        continue
    samples = np.frombuffer(payload, dtype="<i2").copy()  # ⑧ 320个int16采样
    return seq, dbfs_x100/100.0, samples   # 返回 (序号, dBFS, 20ms音频)
```

**为什么这么写**：
- 串口是**无边界字节流**，可能从包的中间开始读到（比如 5090 开机时板卡已经在发流了），所以必须"滑动窗口找魔数"，找到对齐点才开读。
- `seq` 用于丢帧检测（板卡序号连续）；`sum8` 校验和用于检测传输损坏，坏帧丢弃但不断链。
- `dbfs_x100` 是板卡侧算的音量（dBFS×100），5090 稍后 VAD 里其实会**自己再算一遍**（用 `rms_dbfs`），板的读数主要用于记录和展示。

**这一节小结**：`read_frame` = "从字节流里抠出一个 20ms 的音频块，顺便给你序号和音量"。

---

## 3. 阶段②：能量 VAD（`capture_until_endpoint`，核心算法）

这是整个系统里**最有调参意义**的代码。它回答两个问题：① 人开始说话了吗？② 人说完话了吗？

### 3.1 两个量（先看单位）

```python
def rms_dbfs(samples):            # 把 int16 音频算成音量(分贝)
    x = samples.astype(np.float32) / 32768.0
    rms = sqrt(mean(x²))
    return 20*log10(rms)          # 安静≈-60dB，说话≈-20dB
```

- 开机校准：录 3 秒背景 → `background_dbfs`（比如 **-45.3 dB**）。
- 所有阈值都是**相对背景**的：`开始说话阈值 = 背景 + 3dB`（lab 模式）。

### 3.2 起始检测（"声音大到什么程度、持续多久算开始"）

```python
voice_start_window_ms = 300       # 滑动窗 300ms
start_threshold = background + 3dB # 例：-42.3dB
voice_start_ms = 120              # 窗内"活跃样本"需 ≥120ms

while not speech_started:
    frame_dbfs = rms_dbfs(frame)
    if frame_dbfs > start_threshold:
        active_samples += len(frame)      # 活跃样本统计
    # 每来一帧，把 300ms 滑窗外的最旧帧移出（窗口滑动）
    ...
    if active_samples >= 120ms的样本数:
        speech_started = True
        speech_start_sample = 当前总样本 - 窗长   # ← 关键
```

**关键细节 `speech_start_sample`**：检测到人声时，把录音起点**回退到窗口起点**。因为检测到"连续 120ms 有声"时，其实人已经说了 120ms 了，直接从头录会**切掉第一个字**。回退窗口保证开头完整。

### 3.3 尾端点检测（"沉默多久算说完"）

```python
endpoint_silence_ms = 1000        # lab：说完后 1 秒静音即截断

else:  # 已检测到说话
    if frame_dbfs > endpoint_threshold:      # 还在说话
        last_active_sample = 当前
        trailing_silence -= len(frame) * 4   # ← 关键：活跃帧"抵扣"4倍沉默
    else:                                     # 静音帧
        trailing_silence += len(frame)
        if trailing_silence >= 1秒:            # 真沉默了1秒 → 截断
            endpoint_triggered = True; break
```

**为什么有 `endpoint_active_penalty=4`**：正常说话中会有停顿（换气、"嗯"），如果没有抵扣，一次 1 秒的停顿就会把话截断。抵扣的规则是：**1ms 活跃音频抵 4ms 沉默**——连续说话时沉默计数永远攒不到 1 秒；而孤立一声敲击（20ms 尖峰）最多抵 80ms，不会让端点延迟。这叫"对尖峰免疫的端点"。

### 3.4 兜底

`max_seconds=15`：超过 15 秒不管说不说完都截断（防止用户一直说/环境噪声一直响）。

### 3.5 一个数字例子

背景 -45.3dB，用户说"请介绍这个文物"（约 1.5 秒）：
1. 前几帧音量 < -42.3dB → 不触发；说话到 120ms → `speech_started=True`，录音点回退 300ms；
2. 说话期间每帧活跃，沉默计数被反复抵扣；
3. 说完后静音累计到 1000ms → 截断；
4. 输出：整段音频（含末尾 1s 静音 + 开头 300ms 预卷）+ endpoint 字典（`speech_start_seconds`、`trailing_silence_ms` 等调试信息）。

**这一节小结**：VAD 没用任何模型，就是"相对背景的音量阈值 + 滑动窗起始 + 带抵扣的静音端点"。调优的核心参数都在 `config.json → modes.*` 里。

---

## 4. 阶段③：ASR（`load_paraformer` + `recognize`）

### 4.1 加载（`asr_eval_core.py:94-119`）

```python
def load_paraformer(model_name, device="auto"):
    real_device = "cuda:0" if torch.cuda.is_available() else "cpu"
    model = AutoModel(model=model_name, device=real_device, disable_update=True)
    return model, real_device, 加载耗时
```

- `AutoModel` 是 FunASR 的统一入口：给它模型目录，它把前端/模型/推理全部装配好。
- `disable_update=True`：**不联网检查模型版本**（现场环境无网也能跑）。
- 本地模型是完整的 ModelScope 快照（`models/paraformer-zh-streaming/` 里有 `config.yaml/model.pt/tokens.json`），直接给目录即可，绕开 hub。

### 4.2 识别（`board_serial_asr_test.py:272-296`）

```python
def recognize(model, samples):
    audio = samples.astype(np.float32) / 32768.0   # int16 → [-1,1] float
    chunk_size = [0, 10, 5]                        # FunASR流式参数
    chunk_stride = chunk_size[1] * 960             # = 9600样本 = 600ms
    cache = {}
    parts = []
    for i in range(total_chunks):
        chunk = audio[i*stride : (i+1)*stride]     # 切一个600ms块
        result = model.generate(
            input=chunk, cache=cache,
            is_final=(i == last),
            chunk_size=chunk_size,
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        text = result[0]["text"]
        parts.append(text)                         # 部分结果直接拼接
    return "".join(parts), 首块结果耗时, 总耗时
```

**流式识别的机制**：
- 600ms 音频一块，一块调一次 `generate`；
- `cache` 字典存放模型内部状态（encoder 的隐藏状态等），**跨块传递**，相当于模型的"记忆"——这是流式模型和整段识别的本质区别；
- `chunk_size=[0,10,5]`：FunASR 流式配置，意思是"当前块 10 个单位（每单位 60ms=960 样本），右看 5 个单位"；`encoder_chunk_look_back=4, decoder_chunk_look_back=1` 表示解码时回头看前面 4/1 块，这就是为什么同音字/多音字能靠上下文校正（例如"把"和"吧"）；
- `is_final=True`：通知模型"这是最后一块"，模型会冲刷内部缓冲，输出最后剩余的文本 —— **泄漏的关键**：某些字的识别结果只有在 `is_final` 后才出现，所以最后一块必须传 True；
- 输出 `[{'text': '...'}]`（list 包 dict），`extract_text` 取 `result[0]["text"]`；每块文本直接拼接成全文（块之间本来就是顺序的）。

**注意**：这里"流式"指的是**对已录完的音频做 600ms 块级增量识别**，不是"边说边识别"。因为在 VAD 确认说完之前，系统一直在录音缓存，没喂给 ASR。

**这一节小结**：ASR = 把一段 VAD 切好的音频，按 600ms 切块 + cache 传状态 + 最后一块 is_final，得到完整文本和首块延迟。整个识别是**同步阻塞**的，跑完才出文本。

---

## 5. 阶段④：LLM（`answer()`，线程编排是重点）

代码在 `next_stage/voice_sleep_v5_5090/voice_daemon.py:314-400`。**三个线程**一次问答同时活：

```
生成线程 A1:  llm.generate(...)        # 只负责"产token"，一次调用直到生成完
切句线程 A2:  for sentence in sentence_chunks(streamer): 切句 → 塞队列
主线程(播):   for item in segment_queue: tts_stream_to_board(item) 推板卡
```

### 5.1 拼 prompt

```python
messages = [{"role":"system","content":系统提示词}]
messages += history[-4*2:]          # 最近4轮对话(每轮用户+助手=2条)
messages += [{"role":"user","content":用户话}]
prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
```

- `apply_chat_template` 负责把消息列表变成 Qwen3 认识的格式（`<|im_start|>system\n...`），用 `add_generation_prompt=True` 在末尾加上"该助手说话了"的提示符；
- 历史只保留 4 轮（`history_turns`），这是**显存和语义取舍**：太长会导致回答变短、变重复，同时 KV cache 占显存。

### 5.2 流式生成：`TextIteratorStreamer`

```python
streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
generation = dict(**inputs, streamer=streamer,
                  max_new_tokens=180, do_sample=False)
thread = threading.Thread(target=llm.generate, kwargs=generation)
thread.start()          # 生成在后台线程跑
```

- `llm.generate` 本来是一阻塞的同步调用，直到生成完才返回；
- `streamer` 是个"管道"：生成过程中，每产出一个新 token 就塞进 streamer，**另一个线程迭代它就能实时拿到文本**；
- `skip_prompt=True`：只给新生成的部分（不含输入的 prompt 文本）；
- `skip_special_tokens=True`：去掉 `<|im_end|>` 等格式 token，只留正文；
- `do_sample=False`：贪心解码（每次都取概率最高的 token），回答稳定可复现——**代价是没有多样性**。要更"活"一点可改 `do_sample=True`+`temperature=0.7`。

### 5.3 切句：`sentence_chunks`（`realtime_pipeline.py:280-305`）

```python
def sentence_chunks(streamer):
    buffer = ""
    for piece in streamer:                    # piece≈一个字或几个字
        buffer += piece
        while True:
            m = re.search(r"[。！？!?；;\n]", buffer)   # 遇到句号类标点
            if not m: break
            yield buffer[:m.end()].strip()            # 切出一个完整句子
            buffer = buffer[m.end():]
        if len(buffer) >= 48:                          # 长句无标点保护
            split_at = max(rfind(mark,0,48) for mark in "，、：,")
            if split_at < 20: split_at = 48            # 前20字没逗号→硬切
            else: split_at += 1
            yield buffer[:split_at]; buffer = buffer[split_at:]
    if buffer.strip(): yield buffer.strip()            # 最后剩下的
```

**为什么切句**：LLM 一次会说一大段，如果等整段合成，用户响响应会等待很久。按句切，第一句先合成播放，其余继续生成——这就是"**边想边说**"。

### 5.4 V5 的分段策略（daemon 内 `submit_tts_segments`）

第 1 段：凑够 **≥18 字且 ≥1 个完整句** 就送 TTS（首响最快）；
后续段：凑够 **60 字** 或 **2 句** 再送（句子长一点，语音更连贯，不易"断句感"）。

每段：写一个 `request_<id>.json` + 建 `stream_<id>/` 目录 → 放入 `segment_queue` → 主线程依次 `tts_stream_to_board(段)`。

**这一节小结**：LLM = 一个 4B 模型在后台线程吐 token → 另一个线程按标点切成段 → 队列交给主线程逐段推进播放。三线程的时序是"生成"（随时可能超前）和"播放"（实时 1 倍速）之间的**先后解耦**。

---

## 6. 阶段⑤：TTS（子进程 `tts_worker.py` + 主进程 `tts_stream_to_board`）

### 6.1 为什么是单独子进程

`tts_worker.py` 用 **`.venv_5090_tts`** 的 Python 运行。原因：CosyVoice2 依赖的 torchaudio 版本/补丁与 FunASR 环境冲突（worker 顶部还有 `torchaudio.load = load_audio_compat` 的 monkey-patch，把 torchaudio 加载改成 soundfile 实现）。**拆进程=拆依赖，互不污染**。主程序用 `subprocess.Popen` 拉起它，传 4 个参数（模型目录/队列目录/参考音频/参考文本）。

### 6.2 worker 主循环（`tts_worker.py:34-110`）

```python
model = CosyVoice2(model_dir, load_jit=False, load_trt=False, load_vllm=False, fp16=True)
atomic_json(queue/"ready.json", {"ready": True})     # 就绪信号

while True:
    requests = sorted(queue.glob("request_*.json"))
    if not requests: time.sleep(0.05); continue       # 轮询，无任务就睡
    request = json.loads(第一个请求)
    if request.get("command") == "shutdown": return   # 退出指令

    for result in model.inference_zero_shot(
            request["text"], args.prompt_text, args.prompt_wav, stream=True):
        tensor = result["tts_speech"].cpu()           # 合成出的一块音频
        chunks.append(tensor)
        pcm = (tensor→int16).tobytes()
        atomic_bytes(stream_dir/f"chunk_{index:06d}.pcm", pcm)   # 落盘
        index += 1
    # 全部合完：拼接 → 写完整 wav → 写 response_<id>.json {ok, first_audio_seconds, total_seconds, audio_seconds, sample_rate, stream_chunks}
```

**几个机制**：
- `inference_zero_shot(text, 参考文本, 参考wav, stream=True)`：CosyVoice2 的零样本接口是"参考音色 + 目标文本"。`stream=True` 表示**逐步产出**（内部每合成一段就 yield 一次），让主进程能边合成边播放；
- 每一块 `result["tts_speech"]` 是一个 tensor（1×N 采样，**24kHz**）；worker 转成 int16 PCM 写文件；
- 请求文件处理完就删（`finally: request_path.unlink()`），response 也由主进程消费后删；
- 所有写文件都是**先写 .tmp 再 rename**（`atomic_*`），防止主进程读到半截文件——文件没有锁机制，靠"原子写 + 轮询存在性"达成一致性。

### 6.3 主进程侧：`tts_stream_to_board`（`realtime_pipeline.py:223-277`）

对每个段（尤其第一段）做以下循环：

```python
request = {id, text, output_wav, stream_dir}
while True:
    if chunk_N.pcm exists:                        # ①有新音频块
        if not started:
            SPKV <vol>; SPKS 24000;               # ② 首次：开播命令(必须!)
            started = True; 记 first_chunk_seconds
        board_speaker_chunk(ser, pcm, 24000, ...) # ③ 拆≤2048B块→SPKD发送
        delete chunk_N; N++
    if response.json exists:                      # ④ worker说合成完了
        if N < stream_chunks: sleep(10ms)         # 还有chunk没发完→等
        else: SPKE; return {延迟统计}              # ⑤ 收尾
    sleep(10ms)
```

**必须 SPKS 先行的原因**（`PROTOCOL.md` 明确）：板卡没收到 `SPKS` 前，下发的所有 PCM 都被丢弃——这是固件端刻意的保护（防止未开播的杂散数据）。

### 6.4 推流节奏（`board_speaker_chunk`，`realtime_pipeline.py:206-215`）

```python
for part in chunks(pcm, 2048):
    serial_write_all(ser, b"SPKD" + len(part) + part)   # 发2048字节音频
    ser.flush()
    time.sleep(len(part)/2 / 24000 * 0.88)  # 睡"这段音频时长"的88%
```

- `len/2`=采样数，`/24000`=秒。**睡 88% 意味着以 1.14 倍速发送**——比实时稍快，让板卡缓冲里始终攒一点音频；
- 为什么不是 100% 或更快：100% 重传会因抖动造成欠载（拖音），过快会溢出板卡缓冲。88% 是实测折中。

**这一节小结**：TTS = 独立进程慢慢合成（stream 产出），主进程一边轮询产物一边按实时速率推到板卡。进程间没有共享内存，全部靠目录里的 JSON + PCM 文件。

---

## 7. 全部串起来：V5 状态机主循环（`run_board_session`）

### 7.1 启动序列（daemon `main()`）

```
写 pid/status 文件
→ load_paraformer（ASR 进显存）
→ subprocess 拉起 tts_worker，等 ready.json（最多180s）
→ 加载 Qwen3（约20-30s）
→ 预热缓存提示音(menu/startup_begin/startup_ready)：已存在就用，不存在就 TTS 生成一次
→ 等板卡：detect_board_port(VID=0x303A) + open_serial(921600)
→ 校准：capture_seconds(3s) → rms_dbfs → background_dbfs
→ 自检：serial/mic + 真跑ASR + Qwen(2token) + TTS("语音合成自测") → 播"自检完成"
→ run_board_session(background_dbfs, "command")
```

> 为什么"状态=command 而不是 wake"：刚开机自检完，板上已经播了"自检完成，有什么需要帮助的吗？"，此时系统直接监听用户的话（说"开启对话"进入对话），**不需要先喊唤醒词**。自检失败才退回 wake（硬件休眠等唤醒词）。

### 7.2 状态循环（`run_board_session`）

```
state=wake（硬件休眠，省电）
 │  set_hardware_sleep():  先等600ms静音→清缓冲→发 MICS<背景dBFS>
 │  wait_for_hardware_sound_trigger():
 │     板卡本地能量检测(>背景+5dB 且连续120ms) → 板卡上传500ms预卷+新音频
 │     5090 收到第一帧 → capture_until_endpoint(端点500ms, 上限8s)
 │     ASR 识别 → 含"你好导游/你好小游/小游小游"?
 │        ├─ 是 → MICW(恢复持续上传) → 播"我在，有什么需要帮助的吗?" → state=command
 │        └─ 否 → 重新 MICS 回休眠（不做任何后续推理）
 ▼
state=command（持续上传，听"开启对话"）
 │  record_utterance(端点500ms, 上限6s)
 │  ├─ 无语音 → 回 wake
 │  ├─ 内容≠开启对话/开始对话/... → 回 wake
 │  └─ 是 → 播"已开启对话，请开始讲话" → state=dialog
 ▼
state=dialog（自由问答）
 │  record_utterance(端点800ms, 上限15s)
 │  → ASR → 含"进入休眠/退出对话/结束对话..."? 
 │      ├─ 是 → 播"已进入休眠" → 清历史 → state=wake
 │      └─ 否 → answer(text)  ← §5 的三线程问答
 ▼  （回到 wake，循环）
```

### 7.3 拔插容错（外层 while True）

任何异常（串口超时、拔线、自检失败）→ 关闭串口 → 外层循环回到"等待插入板卡"→ **模型不重载**（常驻显存），重新插上自动重新校准+自检。这就是日志里"开发板会话结束，等待重新插入"的含义。

---

## 8. 一次完整对话的时序（时间轴，数字为量级估计）

```
t=0s        用户喊"你好导游"（板卡本地能量检测，不经过5090）
t=120ms     板卡触发 → 上传[500ms预卷+后续音频]
t≈0.5s      5090 收流，VAD 500ms静音端点
t≈1.5s      ASR 识别"你好导游"（0.5s内出文本）
t≈2s        MICW + 播"我在，有什么需要帮助的吗?"（缓存提示音，立即播放）
t≈3s        用户说"开启对话"（VAD 500ms端点）
t≈4.5s      ASR → 是"开启对话" → 播"已开启对话"
t≈5s        用户说"请介绍这个展品"（VAD 800ms端点）
t≈6.5s      ASR 出全文
t≈7.5s      Qwen 首 token（流式开始）
t≈8s        第一句切出（≥18字）→ TTS 开始合成（约2-3s首块）
t≈11s       第一块 PCM 到板卡出声  ← 从"说完了"到"听到第一声"约4-5s
   ...       后续句子边生成边合边播（TTS 每段约3-5s，与播放并行）
t≈30s       回答播完（4-6句/100-140字）
```

---

## 9. 高频问题自查表

| 症状 | 看哪里 |
|---|---|
| 提示音反复变/换音色 | `prompt_cache/` 缓存版本名（`prompt_path` 的 versioned dict）、manifest 是否指向 wav |
| 唤醒总不触发 | 固件是否 V5 固件（支持 MICS）；`hardware_trigger_*` 与固件 Kconfig 是否一致 |
| 首响慢 | `first_tts_segment_chars`/`max_new_tokens`；TTS 是否命中缓存提示音 |
| 说话被截断 | `endpoint_silence_ms` 偏小；`endpoint_active_penalty` 需调大 |
| 收音太灵敏误触发 | `start_threshold_db`/`voice_start_ms` 调大 |
| 板卡音量异常 | `board_speaker_volume`/`board_volume_percent`；SPKS 是否先发 |
| 播放大段有拖音 | `board_speaker_chunk` 的 0.88 系数；串口波特率；`stream_chunk_bytes` |
| 自检失败 | `voice_daemon.status.txt`（最后写入的阶段）；`runs/<时间戳>/` 日志 |

---

## 10. 建议精读顺序（按"理解成本"排序）

1. `board_serial_asr_test.py` 的 `rms_dbfs` + `read_frame`（60 行）→ 建立"帧"概念；
2. 同一文件 `capture_until_endpoint`（100 行）→ VAD 全部逻辑；
3. `asr_eval_core.py` 的 `load_paraformer` + 同文件 `recognize_streaming`（对比理解 ASR 流式）；
4. `realtime_pipeline.py` 的 `sentence_chunks` + `tts_stream_to_board`（切句与推流）；
5. `tts_worker.py` 全文件（110 行，IPC 协议）；
6. `voice_daemon.py` 的 `run_board_session` + `answer`（状态机与线程编排）。
