# engine/ —— 大模型推理代码（ASR / LLM / TTS）

本目录是**完整链路的推理层**，由 `server/real_engine.py`（`sys.path` 注入本目录后）直接 import 复用，
与本地 V5 链路（`voice_sleep_v5_5090/`）共用同一套推理实现，代码零功能改动，仅 `realtime_pipeline.py` /
`tts_worker.py` 增加"依赖根可配置"以支持自包含打包。

| 文件 | 职责 | 关键函数 |
|---|---|---|
| `asr_eval_core.py` | **ASR 加载/评估**：FunASR `AutoModel` 装配本地 Paraformer 流式模型快照（`disable_update=True`，离线可用）；另含 CER 计算、文本提取 | `load_paraformer(model_name, device)`、`recognize_streaming(model, audio_path)`、`extract_text`、`cer(ref, hyp)` |
| `board_serial_asr_test.py` | **VAD + 识别 + PCM1 帧解析**（原板卡串口链路公共件）：能量 RMS→dBFS、帧头魔数滑动窗解析、带抵扣的静音端点、600ms 块增量识别 | `rms_dbfs`、`read_frame`、`capture_until_endpoint`、`capture_seconds`、`recognize(model, samples)`、`save_wav` |
| `realtime_pipeline.py` | **编排工具**：路径解析、TTS 角色参考音色查找、拉起 TTS 子进程、按标点切句、串口推流（本地板卡用；服务器版本走 `real_engine._stream_tts_segment`） | `resolve`、`find_reference(config, root)`、`start_tts_worker(config, run_dir)`、`sentence_chunks(streamer)`、`tts_request` |
| `tts_worker.py` | **TTS 子进程**（`.venv_5090_tts` 运行）：CosyVoice2 zero-shot 音色克隆流式合成；入口参数 `--project-root / --cosy-root / --model-dir / --queue-dir / --prompt-wav / --prompt-text`；与主进程以 `tts_queue/` 目录文件 IPC（原子写） | `main()`：轮询 `request_*.json` → `inference_zero_shot(..., stream=True)` → `chunk_N.pcm` → `response_*.json` |

## 调用链（服务器 real 模式）

```
real_engine._load_and_answer_loop
 ├─ load_paraformer(deps/models/paraformer-zh-streaming)        ── ASR 模型
 ├─ start_tts_worker(base_cfg) → subprocess tts_worker.py        ── TTS 子进程(CosyVoice2)
 ├─ AutoModelForCausalLM.from_pretrained(deps/models/Qwen3-...)  ── LLM 模型
 └─ 每轮: capture_until_endpoint(RingBuffer) → recognize(asr)   ── VAD+ASR
         → _answer_qa: llm.generate + TextIteratorStreamer       ── LLM 流式
         → _sentence_chunks 切句 → tts_queue 请求
         → _stream_tts_segment 轮询 chunk → WSS SPKS/PCM/SPKE    ── TTS 推流
```

## 与本地 V5 的关系

- `board_serial_asr_test.py`、`asr_eval_core.py`：与 `voice_sleep_v5_5090/voice_daemon.py` 完全共用；
- `tts_worker.py`：与 `next_stage/full_pipeline_auto_5090/tts_worker.py` 同一文件（MD5 一致）；
- `realtime_pipeline.py`：仅 `find_reference`/`start_tts_worker` 支持外部依赖根与 `--cosy-root`，其余函数与原版一致。
