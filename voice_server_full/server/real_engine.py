# -*- coding: utf-8 -*-
"""真实语音引擎(RealVoiceEngine,R1–R3):WSS PCM1 → VAD/ASR → Qwen3 一问一答 → TTS → WSS 下行。

完整链路编排(本文件是"编排层",具体推理实现都在 engine/ 目录被 import 复用):
  上行: 设备/WSS 客户端 → WssAdapter(二进制 PCM1 帧) → on_frame()
        → RingBuffer(字节流兼容层,语音停流自动补静音帧)
        → board_serial_asr_test.capture_until_endpoint()(能量 VAD,选出"一段完整的话")
        → board_serial_asr_test.recognize()(FunASR Paraformer 流式,0.5s 内出文本)
  推理: Qwen3-4B 流式生成(_answer_qa,后台线程 + TextIteratorStreamer)
        → _sentence_chunks 按标点切句 → 每句一个 request_*.json 交给 TTS 子进程
  下行: _stream_tts_segment 轮询 chunk_*.pcm → SPKS <rate> + PCM(≤1200B/帧,≈1.14×实时节奏)
        + SPKE,经 set_sink 注册的回调广播到所有在线 WSS 客户端(线程安全调度)。

问答形态: 默认一问一答(无唤醒词、无对话状态机、无历史);voice.real.system_prompt 可配。
运行要求: 本模块与整个 run_server 需用 .venv_5090_llm 的 Python(有 torch/transformers/funasr)。
"""
import csv
import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from ring_buffer import RingBuffer

log = logging.getLogger("vs.real")


def _normalize_wake(text):
    """唤醒词匹配归一化(与串口版 V5 一致):小写、去空白与中英文标点。"""
    return "".join(ch for ch in str(text).lower().strip()
                   if ch not in " ，。！？,.!?；;：:、\t\r\n")


# 唤醒词同音容错:唤醒词走"普通语音识别"(Paraformer),实测把"你好小科"识别成"你好小柯",
# 因此匹配时给每个字生成"替换一个同音字"的候选(仅录常见的同音误识,不做开放拼音匹配)。
_WAKE_HOMOPHONES = {
    "你": "尼泥", "好": "昊号", "小": "晓筱", "科": "柯棵颗课嗑",
    "智": "芝之", "导": "岛到道", "游": "尤由邮",
}


def _wake_variants(word):
    """由主词生成同音候选集:原文 + 每个字替换一个同音字(一次一处)。"""
    out = {word}
    for i, ch in enumerate(word):
        for alt in _WAKE_HOMOPHONES.get(ch, ""):
            out.add(word[:i] + alt + word[i + 1:])
    return out


class StreamingAsr:
    """流式 ASR 协调器("边说边识",延迟优化项)。

    与 recognize() 等价性的来源:同一模型、同一 600ms 块边界(9600 样本)、同一 cache
    跨块传递、同一 chunk_size/look_back、末块 is_final=True —— 只是把"识别"从
    "VAD 收完再跑"提前到"说话期间边收边跑"。

    用法:
      asr_stream = StreamingAsr(model)
      capture_until_endpoint(..., on_chunk=asr_stream.on_chunk)   # 说话期间增量识别
      text, first_partial, seconds = asr_stream.finish(samples)   # VAD 结束后冲刷尾巴
    """

    CHUNK = 9600        # 600ms @16kHz
    SR = 16000

    def __init__(self, model):
        self.model = model
        self.cache = {}              # FunASR 跨块状态(与 recognize 相同)
        self.parts = []              # 增量识别文本片段
        self.blocks = 0              # 已识别块数(用于定位"尾巴")
        self.first_partial = ""      # 首块出字耗时
        self.elapsed = 0.0
        self._started = None

    def on_chunk(self, block, is_final=False):
        """VAD 每凑满 600ms 调一次(by capture_until_endpoint.on_chunk)。"""
        import time as _t
        import numpy as np
        from asr_eval_core import extract_text
        if self._started is None:
            self._started = _t.perf_counter()
        audio = block.astype(np.float32) / 32768.0
        result = self.model.generate(
            input=audio, cache=self.cache, is_final=is_final,
            chunk_size=[0, 10, 5], encoder_chunk_look_back=4, decoder_chunk_look_back=1,
            disable_pbar=True,          # 关掉逐块识别的 tqdm/rtf_avg 进度条(日志降噪)
        )
        text = extract_text(result)
        if text:
            if not self.first_partial:
                self.first_partial = round(_t.perf_counter() - self._started, 3)
            self.parts.append(text)
        self.blocks += 1

    def finish(self, samples):
        """VAD 结束后调用:冲刷最后不足 600ms 的尾巴(is_final),拼出全文。

        回退:整个说话期间没有任何提前识别块(说话 <600ms)时,退化为整段
        recognize() —— 与旧行为完全一致。
        """
        from board_serial_asr_test import recognize
        if not self.parts:
            return recognize(self.model, samples)
        t0 = time.perf_counter()
        tail = samples[self.blocks * self.CHUNK:]
        if len(tail) >= 320:
            self.on_chunk(tail, is_final=True)
        text = "".join(self.parts).strip()
        self.elapsed = round(time.perf_counter() - t0, 3)   # 仅统计"识别耗时"(尾巴块)
        return text, self.first_partial, self.elapsed


class RealVoiceEngine:
    """真实推理引擎,实现与 StubVoiceEngine 相同的引擎接口(可整体替换):

      on_frame(pcm1:dict, raw:bytes|None) — WSS 收到合法 PCM1 帧时回调
      on_command(text:str)              — WSS 收到设备文本命令时回调
      on_bad_frame(reason:str)          — WSS 收到非法帧时回调
      snapshot() -> dict                — 状态页数据(帧统计/最近问答/每轮指标)
      set_sink(text_fn, pcm_fn)         — 注册下行通道(主进程把重活调度到事件循环)
      start() / stop()                  — 生命周期(后台线程跑 _load_and_answer_loop)
    """

    def __init__(self, hub, cfg, run_dir, project_root_desc="."):
        self.hub = hub
        self.cfg = cfg
        self.run_dir = Path(run_dir)
        self.project_root = Path(project_root_desc)
        self.r = cfg.get("voice", {})
        self.real_cfg = self.r.get("real", {})
        self.stream = RingBuffer()
        # 依赖根:模型 weights/venv/third_party/CosyVoice/角色音色/基础配置 的解析基准。
        # 默认 = project_root(本包根);可经 voice.real.deps_root 配置为绝对路径或相对 project_root。
        deps_value = self.real_cfg.get("deps_root") or project_root_desc
        self.deps_root = Path(deps_value)
        if not self.deps_root.is_absolute():
            self.deps_root = Path(project_root_desc) / self.deps_root

        self.lock = threading.Lock()          # 状态统计/文本记录共享锁(on_frame 与状态页并发)
        self.status = "initializing"          # initializing→loading_asr→loading_tts→loading_llm→ready→error
        self.frames = 0                       # 收到的 PCM1 帧数(上行总量统计)
        self.frame_bytes = 0
        self.seq_gaps = 0                     # 序号跳变次数(丢帧检测)
        self.last_seq = None
        self.bad_frames = 0                   # 非法帧(魔数/长度/校验和不过)数
        self.commands = []
        self.file_uploads = []
        self.last_texts = []          # 最近识别/回答文本(供状态页)
        self.last_result = None       # 最近一轮指标

        self.sink_text = None         # async: text → WSS
        self.sink_pcm = None          # async: bytes → WSS
        self._stop = threading.Event()        # 停止信号(stop() 置位)
        self._worker = threading.Thread(target=self._run, daemon=True)   # 引擎主循环线程
        self._real_bytes_consumed = 0         # 已消费的真实字节数(校准/识别循环的记录游标)
        self._background_dbfs = None        # None=未校准;校准成功为实测背景
        self._calibration_done = False
        self._floor = None                  # 动态底噪估计器(会话级;None=未启用/固定底噪)
        self._board_spks_active = False     # 下行 SPKS 是否已开(跨段连续播)
        self._first_spks_at = None          # 本轮首块音频下发的时刻(端点→首块计时用)
        self._turn_first_frame_at = None    # 本轮第一帧到达时刻(完整链路计时起点)
        self._history = []                  # 多轮对话历史[{user},{assistant}...],按 history_turns 截取

    # ---------------- 引擎接口(与 stub 一致)----------------

    def set_sink(self, text_fn, pcm_fn):
        """注册下行通道(run_server 启动时调用):
        text_fn(text) → 主进程异步广播文本命令;pcm_fn(bytes) → 异步广播音频帧。
        引擎工作线程通过这两个回调把数据"安全地"送进 asyncio 事件循环(WSS 广播)。"""
        self.sink_text = text_fn
        self.sink_pcm = pcm_fn

    def on_frame(self, pcm1, raw=None):
        """WSS 收到一个合法 PCM1 帧时由 WssAdapter 调用(上行入口)。

        pcm1: 已解析的帧信息 dict(seq/bytes_len/dbfs_x100/payload);
        raw:  原始帧字节(整个 PCM1 包)——只有带 raw 时才追加到 RingBuffer,
              这样现有 read_frame 的帧内解析(魔数/校验和)可以原样复用。
        """
        with self.lock:
            seq = pcm1["seq"]
            if self.last_seq is not None and seq != (self.last_seq + 1) & 0xFFFFFFFF:
                self.seq_gaps += 1
            self.last_seq = seq
            self.frames += 1
            if self._turn_first_frame_at is None:
                self._turn_first_frame_at = time.monotonic()   # 本轮第一帧到达时刻(链路计时起点)
            self.frame_bytes += pcm1["bytes_len"]
        if raw:
            self.stream.append(raw)     # 原始 PCM1 帧字节 → 现有 read_frame 直接工作
        self._wake_worker()

    def on_command(self, text):
        """WSS 设备文本命令(如 MIC_START/MIC_STOP/SPKV...)记录进状态(供状态页/断言)。"""
        with self.lock:
            self.commands.append(text.strip())
            if len(self.commands) > 200:
                self.commands.pop(0)
        log.info("WSS 命令<- 设备: %s", text.strip()[:80])
        return True

    def on_bad_frame(self, reason):
        """WSS 非法帧(超长/魔数或校验和不对)计数并记日志 —— 频繁出现说明链路有噪声。"""
        with self.lock:
            self.bad_frames += 1
        log.warning("WSS 非法 PCM1 帧: %s", reason)

    def snapshot(self):
        """状态页数据:上行统计 + 引擎状态 + 最近 4 条识别/回答 + 最近一轮指标。"""
        with self.lock:
            return {
                "mode": "real",
                "status": self.status,
                "frames": self.frames,
                "audio_bytes": self.frame_bytes,
                "seq_gaps": self.seq_gaps,
                "bad_frames": self.bad_frames,
                "buffered_bytes": self.stream.buffered_bytes,
                "commands_seen": self.commands[-10:],
                "last_texts": self.last_texts[-4:],
                "last_result": self.last_result,
                "file_uploads": self.file_uploads,
            }

    # ---------------- 生命周期 ----------------

    def start(self):
        """启动后台引擎线程(加载模型后进入"采集→识别→问答"循环)。"""
        self._worker.start()

    def stop(self):
        """停止:置停止信号并关闭 RingBuffer(阻塞中的 read 会立即返回/退出循环)。"""
        self._stop.set()
        self.stream.close()

    def _wake_worker(self):
        # 采集线程阻塞在 stream.read;append 已触发 notify,无需额外动作
        pass

    # ---------------- 主流程(后台线程)----------------

    def _run(self):
        # 控制台可能是 GBK 编码,emoji 等字符会让 print/日志崩掉引擎 → 一律替换不报错
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
        try:
            self._load_and_answer_loop()
        except Exception:
            log.exception("real 引擎终止")
            self.status = "error"

    def _load_and_answer_loop(self):
        """引擎主循环:加载三大模型 → 常驻 → "采集→VAD→MIC_STOP→ASR→问答→下行" 无限循环。

        加载顺序(考虑显存与依赖):
          ① ASR(FunASR Paraformer,GPU 主进程内);
          ② TTS 子进程(CosyVoice2,独立 venv,经 start_tts_worker 拉起,等 ready.json);
          ③ LLM(Qwen3-4B,transformers,GPU 主进程内)。
        此后所有模型常驻显存,循环里只做推理,不再重载。

        每轮流程(while not stop),协议适配 ESP32 Julia "听—想—说":
          等新字节 → 首轮先做背景校准(_calibrate_background)
          → capture_until_endpoint:能量 VAD 判定"用户说完了"(设备本地 WakeNet 已唤醒,
            自行上传 PCM1;服务端不识别唤醒词)
          → 立即下发 MIC_STOP 文本帧(设备→THINKING,必须发送)
          → recognize(ASR 最终识别)
          → _answer_qa: Qwen3 流式回答 + TTS 合成
             (SPKS <24000> 开播 → PCM16 二进制帧 → SPKE 收尾;
              可选连续对话模式: SPKE 后重发 MIC_START,默认关闭,设备回 IDLE)
          → 记一轮指标 → 清残留静音帧。
        """
        # 包根 = 本文件(server/)的父目录;推理代码固定从包内 engine/ 导入,
        # 不依赖调用方传入的 project_root(避免被外部同名模块劫持)。
        package_root = Path(__file__).resolve().parent.parent
        deps = self.deps_root
        sys.path.insert(0, str(package_root))
        sys.path.insert(0, str(package_root / "engine"))

        import numpy as np
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer
        from asr_eval_core import load_paraformer
        from board_serial_asr_test import (
            NoiseFloorTracker, capture_seconds, capture_until_endpoint, recognize,
            rms_dbfs, save_wav,
        )
        from realtime_pipeline import resolve, start_tts_worker, tts_request

        models = self.real_cfg.get("models", {})
        tts_base = self._load_base_tts_config()

        # ---- 加载 ASR ----
        self.status = "loading_asr"
        asr, _, _ = load_paraformer(str(resolve(deps, models["asr"])), "auto")
        log.info("ASR 就绪")

        # ---- TTS 子进程 ----
        self.status = "loading_tts"
        tts_process, queue = start_tts_worker(tts_base, self.run_dir)
        log.info("TTS worker 就绪")

        # ---- 加载 Qwen3 ----
        self.status = "loading_llm"
        tokenizer = AutoTokenizer.from_pretrained(
            str(resolve(deps, models["llm"])), trust_remote_code=True, local_files_only=True)
        llm = AutoModelForCausalLM.from_pretrained(
            str(resolve(deps, models["llm"])), torch_dtype="auto", device_map="auto",
            trust_remote_code=True, local_files_only=True)
        log.info("Qwen3 就绪")

        self.status = "ready"
        # ---- VAD 参数解析:本地 V5 参数体系(config.modes + config.voice_control)优先,
        #      voice.real.vad 作为兼容兜底(旧配置只有 vad 段也能跑) ----
        vad = self.real_cfg.get("vad", {})
        vc = self.cfg.get("voice_control") or {}
        modes = self.cfg.get("modes") or {}
        mode = modes.get(vc.get("default_mode", "lab")) or {}
        start_above = float(mode.get("start_threshold_db", vad.get("start_above_db", 3)))
        end_above = float(mode.get("end_threshold_db", vad.get("end_above_db", 3)))
        endpoint_ms = float(
            vc.get("dialog_endpoint_silence_ms",
                   mode.get("endpoint_silence_ms", vad.get("endpoint_ms", 1000))))
        max_seconds = float(mode.get("max_record_seconds", vad.get("max_seconds", 15)))
        active_penalty = float(mode.get("endpoint_active_penalty",
                                        vad.get("active_penalty", 4.0)))
        voice_start_ms = int(mode.get("start_active_ms", vad.get("start_active_ms", 120)))
        voice_start_window_ms = int(mode.get("start_window_ms", vad.get("start_window_ms", 300)))
        default_background = float(vad.get("background_dbfs", -60))
        background_seconds = float(
            vc.get("background_seconds", vad.get("background_seconds", 2.5)))
        # ---- 动态底噪:双窗估计器(默认关闭;enabled=false 时 _floor=None,完全走原路径) ----
        df_cfg = vad.get("dynamic_floor")
        df_cfg = df_cfg if isinstance(df_cfg, dict) else {}
        if df_cfg.get("enabled", False):
            self._floor = NoiseFloorTracker(default_background, df_cfg)
            log.info("动态底噪: 启用 (fast=%.1fs slow=%.1fs gate=%.1fdB 升%.1fdB/s 降%.1fdB/s 触发%.1fdB)",
                     float(df_cfg.get("fast_window_s", 1.5)),
                     float(df_cfg.get("slow_window_s", 8.0)),
                     float(df_cfg.get("gate_db", 12.0)),
                     float(df_cfg.get("up_max_db_per_s", 3.0)),
                     float(df_cfg.get("down_max_db_per_s", 0.5)),
                     float(df_cfg.get("rise_trigger_db", 1.0)))
        else:
            self._floor = None
        # ---- 线上唤醒(2026-08-28 新版语音链路):设备端已去除本地 WakeNet,固件 WSS 认证后
        #      持续上传 PCM1;服务器待机态用普通 ASR 判定唤醒词(N 个字/一段),命中才进一轮问答。
        #      参考串口版 V5 两级唤醒的第二级(硬件触发后的 ASR 判定)。默认打开
        #      (词表仅"你好小科";旧固件本地唤醒模式请设 enabled=false)。 ----
        wk_cfg = self.real_cfg.get("wake")
        wk_cfg = wk_cfg if isinstance(wk_cfg, dict) else {}
        self._wake_enabled = bool(wk_cfg.get("enabled", False))
        self._wake_words = [str(w).strip() for w in (wk_cfg.get("words") or ["你好小科"])
                            if str(w).strip()]
        # 展开同音候选(一次一处替换),匹配时任一命中即唤醒
        self._wake_needles = set()
        for _w in self._wake_words:
            self._wake_needles |= _wake_variants(_normalize_wake(_w))
        self._wake_prompt = str(wk_cfg.get("prompt", "我在，请讲。") or "")
        # 唤醒一次·持续对话: 唤醒后不回待机,仅空闲超时(无"判了起始"的段)才回待机
        wk_timeout_s = float(wk_cfg.get("timeout_seconds", 60.0) or 0.0)
        # 待机判定参数(串口版 V5 唤醒段同款:起声快、端点短、段上限小)
        wk_listen_s = float(wk_cfg.get("listen_seconds", 8.0))
        wk_endpoint_ms = float(wk_cfg.get("endpoint_silence_ms", 500))
        wk_start_ms = int(wk_cfg.get("start_active_ms", 20))
        wk_window_ms = int(wk_cfg.get("start_window_ms", 500))
        self._awake = False
        self._awake_at = 0.0
        if self._wake_enabled:
            log.info("线上唤醒: 启用 (词=%s 应答=%r 空闲超时=%.0fs)",
                     self._wake_words, self._wake_prompt, wk_timeout_s)
        # 语音流停止后,在端点静音窗口内自动补静音帧,让现有 VAD 端点逻辑生效
        self.stream.enable_auto_silence(endpoint_ms / 1000.0 + 0.4)

        audio_dir = self.run_dir / "audio"
        tts_dir = self.run_dir / "tts"
        result_csv = self.run_dir / "voice_qa_results.csv"
        turn = 0

        while not self._stop.is_set():
            self._short_sleep(0.2)
            # ---- 唤醒态空闲超时(唯一退出条件): 持续对话时 60s 无活动段 → 回待机等下次唤醒词。
            #     放在最外层(等字节之前):即使设备停传/无新字节也要计时;不发任何下行命令
            #     (设备侧 LISTEN/IDLE 由固件 5 分钟远场待机自愈,PCM 不受影响)。
            if (self._wake_enabled and self._awake and wk_timeout_s > 0
                    and time.monotonic() - self._awake_at > wk_timeout_s):
                print(f"[唤醒] 空闲 {wk_timeout_s:.0f}s 无交互 → 回待机(等下次唤醒词)", flush=True)
                self._remember("WAKE- timeout")
                self._awake = False
            if self.stream.real_bytes_total <= self._real_bytes_consumed:
                continue
            if not self._calibration_done and self._floor is None:
                self._calibrate_background(self.stream, background_seconds)
            elif not self._calibration_done:
                # 动态底噪启用:跳过开机静态校准 —— 校准窗口会"消费"持续上传流的开头音频
                # (实测唤醒词前半段被吃掉 → 只剩"小科"),且 3 次重试造成启动后 ~24s 哑巴期;
                # bg_t 由估计器在每轮预语音段自学(初值 = 配置默认)。
                self._calibration_done = True
                log.info("动态底噪已启用: 跳过开机静态校准(bg 由估计器自学, 初值 %.1fdB)",
                         default_background)
            background_dbfs = (self._background_dbfs if self._background_dbfs is not None
                               else default_background)
            # ---- 线上唤醒:待机态(未唤醒)只做唤醒词判定,不进入问答编排 ----
            # 固件持续上传 → 每(≤8s)一段,VAD 判定;仅对"判了起始"的段做普通 ASR;
            # 命中"你好小科" → 播唤醒应答 → 丢弃应答期间上行(含扬声器回声,无AEC) →
            # MIC_START(设备→LISTEN) → 进入唤醒态;未命中继续守听(不发任何下行命令)。
            if self._wake_enabled and not self._awake:
                try:
                    w_samples, _, w_ep = capture_until_endpoint(
                        self.stream, max_seconds=wk_listen_s,
                        background_dbfs=background_dbfs,
                        endpoint_silence_ms=wk_endpoint_ms,
                        threshold_above_bg=start_above,
                        endpoint_threshold_above_bg=end_above,
                        endpoint_active_penalty=active_penalty,
                        voice_start_ms=wk_start_ms,
                        voice_start_window_ms=wk_window_ms,
                        floor_tracker=self._floor,
                    )
                except TimeoutError:
                    self._real_bytes_consumed = self.stream.real_bytes_total
                    continue
                self._real_bytes_consumed = self.stream.real_bytes_total
                if not w_ep.get("speech_started") or len(w_samples) < 1600:
                    continue                      # 环境静音/极短段:不识别,继续守听
                # 只识别"起始回退后→段尾"的有效语音段 —— 整段喂 ASR 时,开头长静音会被
                # 流式切块吞掉前缀字(实测"你好小科"只识别出"小科");与串口版
                # "保留触发首帧/pre-roll 作为唤醒段一部分"的做法等价。
                w_si = int(float(w_ep.get("speech_start_seconds") or 0.0) * 16000)
                w_seg = w_samples[w_si:] if 0 < w_si < len(w_samples) else w_samples
                wake_text, _, wk_asr_s = recognize(asr, w_seg)
                wake_text = (wake_text or "").strip()
                value = _normalize_wake(wake_text)
                self._remember(f"WAKE? {wake_text or '[空]'}")
                print(f"[唤醒] 候选识别({wk_asr_s:.2f}s): {wake_text!r}", flush=True)
                if any(n in value for n in self._wake_needles):
                    print(f"[唤醒] 命中唤醒词 → 应答: {self._wake_prompt!r}", flush=True)
                    self._remember(f"WAKE+ {wake_text}")
                    if self._wake_prompt:
                        self._speak_prompt(self._wake_prompt, tts_base, queue, tts_dir,
                                           tag=f"wake_{int(time.time())}")
                    # 应答播放期间设备仍在上传(含扬声器回声)——无 AEC,丢弃这段上行,
                    # 否则 MIC_START 后的第一句会把"我 在,请讲"的残影当问题听
                    self.stream.reset_input_buffer()
                    self._real_bytes_consumed = self.stream.real_bytes_total
                    self._push_text("MIC_START")
                    print("[下行] MIC_START(线上唤醒→LISTEN)", flush=True)
                    self._awake = True
                    self._awake_at = time.monotonic()     # 持续对话: 最后交互时刻
                else:
                    print("[唤醒] 未命中 → 继续待机", flush=True)
                continue
            # 流式 ASR:每轮全新上下文(与 recognize 语义一致),VAD 收集期间边收边识别
            asr_stream = StreamingAsr(asr)
            try:
                samples, _, endpoint = capture_until_endpoint(
                    self.stream, max_seconds=max_seconds,
                    background_dbfs=background_dbfs,
                    endpoint_silence_ms=endpoint_ms,
                    threshold_above_bg=start_above,
                    endpoint_threshold_above_bg=end_above,
                    endpoint_active_penalty=active_penalty,
                    voice_start_ms=voice_start_ms,
                    voice_start_window_ms=voice_start_window_ms,
                    on_chunk=asr_stream.on_chunk,
                    floor_tracker=self._floor,
                )
            except TimeoutError:
                self._real_bytes_consumed = self.stream.real_bytes_total
                continue
            self._real_bytes_consumed = self.stream.real_bytes_total
            bg_extra = (f" | 动态底噪bg={float(endpoint.get('bg_final_dbfs') or background_dbfs):.1f}dB"
                        if self._floor is not None else "")
            log.info("VAD: 端点触发=%s 起始=%.2fs 最后活跃=%.2fs 尾静音=%sms 阈值=%.1fdB 音频=%.2fs%s",
                     bool(endpoint.get("endpoint_triggered")),
                     float(endpoint.get("speech_start_seconds") or 0.0),
                     float(endpoint.get("last_active_seconds") or 0.0),
                     int(endpoint.get("trailing_silence_ms") or 0),
                     float(endpoint.get("vad_threshold_dbfs") or background_dbfs),
                     round(len(samples) / 16000, 2),
                     bg_extra)
            # ---- 在线唤醒·持续对话:静音段不作为轮次(不 MIC_STOP/不空轮),保持唤醒等用户说话;
            #      任何"判了起始"的段都刷新最后交互时刻(空闲超时判定用) ----
            if self._wake_enabled:
                if not endpoint.get("speech_started"):
                    continue
                self._awake_at = time.monotonic()
            # ---- 协议适配(ESP32 Julia "听—想—说"):
            # VAD 判定本轮输入结束 → 立即回发独立的 MIC_STOP 文本帧(不能省略),
            # 设备收到后结束上传、UI 进入 THINKING;然后服务器才做 ASR/LLM/TTS。
            # 任何情况下本轮都必须以 SPKS→PCM→SPKE 收尾(空识别走兜底播报),
            # 否则设备永远停在 THINKING。
            self._push_text("MIC_STOP")
            print("[下行] MIC_STOP(结束本轮输入,设备→THINKING)", flush=True)

            t_endpoint = time.monotonic()   # 计时原点:VAD 判定"说完了"
            turn += 1
            input_wav = audio_dir / f"qa_{turn:03d}_input.wav"
            save_wav(input_wav, samples)
            if len(samples) >= 1600:        # ≥0.1s 才值得识别
                # 流式 ASR:说话期间已逐块识别,这里只冲刷尾巴(不足 600ms 自动回退整段识别)
                recognized, first_partial, asr_seconds = asr_stream.finish(samples)
                recognized = (recognized or "").strip()
                print(f"[识别] 第{turn}问({asr_seconds:.2f}s): {recognized or '[空]'}", flush=True)
            else:                            # <0.1s:无有效语音,直接兜底
                recognized, first_partial, asr_seconds = "", "", 0.0
                print(f"[识别] 第{turn}问: 音频<0.1s,无有效语音", flush=True)
            self._remember(f"Q{turn}: {recognized or '[空]'}")
            # ---- 动态底噪 heal(P2):无端点轮 → 重锚 bg_t,防止突变噪声"每轮都坏" ----
            # 依据:开了动态底噪 + 本轮"判了起始但从未触发端点"(被持续高电平钉死)→
            # 说明底噪估计失效(无论本轮是否混有可识别人声):
            #   ① 一律用本轮音频帧电平 10% 分位重锚 bg_t(reset 清空窗口,下轮预语音段重新学习);
            #   ② 仅当确认为"纯噪声轮"(空识别 或 活跃占比≈100%)时才丢弃文本走空轮收尾;
            #      若 ASR 已识别出有效人声(噪声+人声混合轮),仍正常作答 —— 教训:实测量
            #      活跃占比 0.977 < 0.98 门槛,若把重锚绑在"丢弃文本"上会漏掉修复。
            if (self._floor is not None and endpoint.get("speech_started")
                    and not endpoint.get("endpoint_triggered") and len(samples) >= 16000 * 2):
                th = float(endpoint.get("vad_threshold_dbfs") or background_dbfs)
                n = len(samples) // 320
                rows = np.array(samples[:n * 320], dtype=np.int16).reshape(-1, 320)
                fr = np.array([rms_dbfs(row) for row in rows])
                active_ratio = float(np.mean(fr > th))
                new_bg = float(np.percentile(fr, 10))
                # 钳制到估计器语义范围(防残余帧/异常帧把锚点拉到 -120 级病态值)
                new_bg = min(max(new_bg, float(self._floor.cfg.get("floor_min_dbfs", -80.0))),
                             float(self._floor.cfg.get("floor_max_dbfs", -35.0)))
                log.warning("动态底噪 heal: 无端点轮(活跃占比=%.0f%% ASR=%s) → bg_t %.1f→%.1fdB",
                            active_ratio * 100, "空" if not recognized else "有字",
                            self._floor.bg(), new_bg)
                self._floor.reset(new_bg)
                if not recognized or active_ratio >= 0.98:
                    if recognized:
                        recognized = ""
                        print("[识别] 噪声型轮次: 丢弃文本,走空轮收尾", flush=True)
            if not recognized:
                # 空识别(或无有效语音):不做任何语义内容,但按协议完成收尾,
                # 避免设备停在 THINKING —— "空播报":SPKS → 0.12s 静音 PCM → SPKE,
                # 多轮模式下再发 MIC_START 进入下一轮(设备即将收到的只是"无声的一轮")。
                print("[识别] 空文本 → 空播报收尾(SPKS+静音+SPKE)", flush=True)
                self._push_text("SPKS 24000")
                silence = b"\x00\x00" * 2880          # 2880 样本 = 0.12s @24kHz, 偶数字节
                for off in range(0, len(silence), 1200):
                    self._push_pcm(silence[off:off + 1200])
                self._push_text("SPKE")
                print("[下行] SPKE(空播报结束)", flush=True)
                # 在线唤醒·持续对话:空轮后同样续听(不回待机);非唤醒模式按 mic_restart 决定
                if self._wake_enabled or self.r.get("mic_restart_after_answer", False):
                    self._push_text("MIC_START")
                    print("[下行] MIC_START(空轮后继续下一轮)", flush=True)
                if self._turn_first_frame_at is not None:
                    print(f"[链路] 第{turn}问(空轮): 首帧→收尾 "
                          f"{time.monotonic() - self._turn_first_frame_at:.2f}s", flush=True)
                self._turn_first_frame_at = None
                self.stream.reset_input_buffer()
                continue

            self._endpoint_wall = t_endpoint      # 供 TTS 首块计时
            self._first_spks_at = None
            try:
                answered = self._answer_qa(
                    turn, recognized, tokenizer, llm, streamer_cls=TextIteratorStreamer,
                    sentence_chunks=self._sentence_chunks, queue=queue, tts_dir=tts_dir,
                    tts_base=tts_base, result_csv=result_csv,
                    asr_seconds=asr_seconds, input_wav=str(input_wav),
                    endpoint_wall=t_endpoint,
                )
                self.last_result = answered
            except Exception:
                # 单轮异常保护:引擎线程常驻,异常只结束本轮并强制 SPKE 收尾
                log.exception("第%d轮问答异常", turn)
                self.last_result = {"turn": turn, "error": "exception"}
                if self._board_spks_active:
                    self._push_text("SPKE")
                    self._board_spks_active = False
                    print("[下行] SPKE(异常收尾)", flush=True)
            # ---- 完整链路计时(首帧上传 → SPKE 播完)----
            if self._turn_first_frame_at is not None:
                talk_s = t_endpoint - self._turn_first_frame_at      # 上传开始→VAD判说完(含尾静音)
                if isinstance(answered, dict):
                    ft = answered.get("endpoint_to_first_token_seconds")
                    fa = answered.get("endpoint_to_first_audio_seconds")
                    tot = answered.get("total_seconds")
                    fmt = lambda v: f"{v:.2f}s" if isinstance(v, (int, float)) else "--"
                    print(f"[链路] 第{turn}问: 首帧→判完 {talk_s:.2f}s | 端点→首字 {fmt(ft)} | "
                          f"端点→首块 {fmt(fa)} | 端点→播完 {fmt(tot)} | "
                          f"首帧→播完 {time.monotonic() - self._turn_first_frame_at:.2f}s",
                          flush=True)
            self._turn_first_frame_at = None
            self.stream.reset_input_buffer()   # 丢弃回合间残留的补静音帧

        if tts_process:
            try:
                tts_process.terminate()
            except Exception:
                pass

    def _calibrate_background(self, stream, seconds):
        """借鉴 V5 开机校准:用流起始的 seconds 秒音频,按帧电平 10% 分位数估计背景。
        要求窗口内是真实帧(合成静音帧为 -120dB,不计);整窗都是人声/静音视为失败并重试,
        3 次未成功才回退配置默认值。"""
        # 延迟导入:项目根已由 _load_and_answer_loop 插入 sys.path;
        # 此处为独立方法,不能复用其函数内局部导入。
        import numpy as np
        from board_serial_asr_test import capture_seconds, rms_dbfs

        need_bytes = int(seconds * 656 * 50)      # 656B/帧 × 50帧/s
        for attempt in range(1, 4):
            # 等足够真实字节到达(留 1.2 倍余量;合成静音帧不增加 real_bytes)
            deadline = time.monotonic() + 8
            while (stream.real_bytes_total - self._real_bytes_consumed
                   < int(need_bytes * 1.2) and time.monotonic() < deadline):
                self._short_sleep(0.2)
            if stream.real_bytes_total - self._real_bytes_consumed < int(need_bytes * 1.2):
                log.info("背景校准: 第%d次真实帧不足,稍后重试", attempt)
                self._short_sleep(1.0)
                continue
            try:
                samples, _ = capture_seconds(stream, seconds)
            except TimeoutError:
                self._real_bytes_consumed = stream.real_bytes_total
                self._short_sleep(1.0)
                continue
            self._real_bytes_consumed = stream.real_bytes_total
            n = len(samples) // 320
            if n < 20:
                self._short_sleep(1.0)
                continue
            rows = np.array(samples[:n * 320], dtype=np.int16).reshape(-1, 320)
            dbfs = np.array([rms_dbfs(row) for row in rows])
            silence_ratio = float(np.mean(dbfs < -100.0))   # 合成静音帧占比
            bg = float(np.percentile(dbfs, 10))
            if silence_ratio > 0.3:
                log.info("背景校准: 第%d次窗口多为合成静音(%.0f%%),继续等待真实帧",
                         attempt, silence_ratio * 100)
                self._short_sleep(1.5)
                continue
            if -85.0 < bg < -40.0:
                self._background_dbfs = bg
                self._calibration_done = True
                # 动态底噪:校准结果作为 bg_t 初值,此后由估计器持续修正
                if self._floor is not None:
                    self._floor.reset(bg)
                log.info("背景校准: %.1f dBFS (%.1fs 真实音频, 帧电平10%%分位, 第%d次)",
                         bg, seconds, attempt)
                return
            log.warning("背景校准: 第%d次窗口疑似持续人声(%.1f dBFS),稍后重试", attempt, bg)
            self._short_sleep(2.0)
        self._calibration_done = True
        log.warning("背景校准 3 次未成功,使用配置默认背景(dBFS=cfg)")

    # ---------------- 分句(LLM 流式输出 → 完整句子;重建版:原版 sentence_chunks
    # 含面向控制台的 print,在 GBK 控制台遇到 emoji 会炸;本实现逻辑一致、无副作用)----------------

    @staticmethod
    def _sentence_chunks(streamer, fast_cut=0):
        """流式消费 TextIteratorStreamer,按句产出 LLM 回答(生成器,yield 完整句子)。

        切句规则(决定"边想边说"的粒度):
          ① 优先按标点切:遇到 。！？!?；; 或换行 \n 即切出一句(含该标点);
          ② 首段快切:fast_cut > 0 且缓冲 ≥ fast_cut 字时,遇逗号/顿号/冒号也切
             —— LLM 第一句往往很长,不拆首句就要等整句出来 TTS 才开口;
             快切使首段提前 0.5~1.3s 送合成(代价:首句语气稍碎,演示可接受,
             fast_cut=12 时再短断句点不够时自动等下一标点);
          ③ 长句保护:缓冲 ≥48 字仍无标点 → 在 20~48 字之间最后出现的 ,、：, 处切
             (前 20 字没有就地硬切 48 字),防止长时间不出声;
          ④ 收尾:生成结束后剩余缓冲非空则作为最后一句 yield。
        注意: 这里的"句"是交给 TTS 的一段文本(一段 = 一个 request_*.json)。
        """
        import re as _re
        buffer = ""
        for piece in streamer:
            buffer += piece
            while True:
                match = _re.search(r"[。！？!?；;\n]", buffer)
                if not match:
                    break
                end = match.end()
                sentence = buffer[:end].strip()
                buffer = buffer[end:]
                if sentence:
                    yield sentence
            if fast_cut and len(buffer) >= fast_cut:
                # 首段快切:逗号级标点出现即切(至少留 6 个字,避免太碎的短段)
                m2 = _re.search(r"[，、：,]", buffer)
                if m2 and m2.end() >= 6:
                    sentence = buffer[:m2.end()].strip()
                    buffer = buffer[m2.end():]
                    if sentence:
                        yield sentence
                    continue
            if len(buffer) >= 48:
                split_at = max(buffer.rfind(mark, 0, 48) for mark in "，、：,")
                if split_at < 20:
                    split_at = 48
                else:
                    split_at += 1
                sentence = buffer[:split_at].strip()
                buffer = buffer[split_at:]
                if sentence:
                    yield sentence
        if buffer.strip():
            yield buffer.strip()

    # ---------------- 一键一答 ----------------

    def _answer_qa(self, turn, user_text, tokenizer, llm, streamer_cls,
                   sentence_chunks, queue, tts_dir, tts_base, result_csv,
                   asr_seconds, input_wav, endpoint_wall):
        """一轮"问—答(LLM)—切句—合成—下行"的完整编排(LLM 模块核心)。

        线程编排(一次问答同时有 3 个执行体):
          生成线程:    llm.generate(...) 阻塞式产出 token,通过 streamer 管道输出;
          切句线程:    submit_tts_segments() 迭代 streamer → _sentence_chunks 切句
                       → 每句立即写 request_*.json 并预提交(不等上一段播完再合成,
                       消除段间 2~4s 的"先播完再合成"空洞)→ 推入 segment_queue;
          播放主循环:  本线程(real_engine._load_and_answer_loop 的调用方)从队列
                       逐段 _stream_tts_segment() 推送到 WSS(与合成/生成并行)。

        LLM 细节:
          - 提示词:默认一问一答(无 system);voice.real.system_prompt 非空时作为 system 消息;
          - 流式:TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True),
            只给新生成正文、去掉 <|im_end|> 等格式 token;
          - 生成参数: 贪心解码(do_sample=False,稳定可复现),max_new_tokens 可配(默认 180)。
        计时: 以 VAD 判定"说完了"(endpoint_wall)为原点,记录
          端点→LLM 首字(first_token_seconds)、端点→首块音频下发(tts_wall)、整轮 total_seconds。
        返回: 本轮指标 dict(同时写入 result_csv 与 self.last_result)。
        """
        # 问答形态/提示词:默认一问一答(无 system),可经 voice.real.system_prompt 配置;
        # 本轮按 V5 编排重构 TTS 分段:
        # 切句线程边切边预提交全部段(TTS 合成与上一段播放重叠),播放线程只消费,
        # 消除"一句播完才合成下一句"造成的段间 2-4s 空洞。
        # 参数来源:config.conversation(完整体系)优先,voice.real 兜底(旧配置兼容)。
        import queue as thread_queue
        from datetime import datetime as _dt

        conversation = self.cfg.get("conversation") or self.real_cfg
        messages = []
        system_prompt = str(conversation.get("system_prompt")
                            or self.real_cfg.get("system_prompt") or "").strip()
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        # 多轮上下文:取最近 history_turns 轮(每轮 = user + assistant 两条)。
        # 历史保存在 self._history(引擎生命周期内常驻),第二轮起"那它呢"这类
        # 指代性提问才能被 LLM 正确理解 —— 多轮对话的必要条件。
        history_turns = int(conversation.get("history_turns", 0) or 0)
        if history_turns > 0 and self._history:
            messages.extend(self._history[-history_turns * 2:])
        messages.append({"role": "user", "content": user_text})
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
        streamer = streamer_cls(tokenizer, skip_prompt=True, skip_special_tokens=True)
        started = time.perf_counter()
        generation_thread = threading.Thread(target=llm.generate, kwargs=dict(
            **inputs, streamer=streamer,
            max_new_tokens=int(conversation.get("max_new_tokens", 80)),
            do_sample=False,
        ))
        generation_thread.start()

        response_pieces = []
        segment_queue = thread_queue.Queue()
        first_token_seconds = None

        def submit_tts_segments():
            nonlocal first_token_seconds
            # V5 式分段缓冲:首段"完整一句话立即送"(首响最快,不等攒长度);
            # 后续段凑够 later_chars 字或攒满 2 句再送(句长一点,语气更连贯)。
            # 首段快切 fast_cut:LLM 第一句被逗号拆短,不用等整句出来 TTS 才开口。
            vc = self.cfg.get("voice_control") or {}
            first_chars = int(vc.get("first_tts_segment_chars", 12))   # 兼容保留(见下注)
            later_chars = int(vc.get("later_tts_segment_chars", 40))
            fast_cut = int(vc.get("first_tts_fast_cut_chars", 12))
            buffered = []
            chars = 0
            seg_index = 0

            def submit(text):
                """把一段文本提交给 TTS 子进程(写 request + 排队等待播发)。"""
                nonlocal seg_index
                seg_index += 1
                request_id = f"turn_{turn:03d}_{seg_index:03d}"
                wav = tts_dir / f"{request_id}.wav"
                stream_dir = queue / f"stream_{request_id}"
                stream_dir.mkdir(parents=True, exist_ok=True)
                (queue / f"request_{request_id}.json").write_text(json.dumps({
                    "id": request_id, "text": text, "output_wav": str(wav),
                    "stream_dir": str(stream_dir),
                }, ensure_ascii=False), encoding="utf-8")
                print(f"[TTS] 段{seg_index}: {text[:40]}", flush=True)
                segment_queue.put((request_id, stream_dir))

            for sentence in sentence_chunks(streamer, fast_cut=fast_cut):
                response_pieces.append(sentence)
                if first_token_seconds is None:
                    first_token_seconds = round(time.monotonic() - endpoint_wall, 3)
                    print(f"[计时] 端点→LLM首字: {first_token_seconds*1000:.0f} ms", flush=True)
                buffered.append(sentence)
                chars += len(sentence)
                # 首段:完整一句立即送(不再等攒满 first_chars —— 短句"你好。"
                # 会被立即送;长句则由 fast_cut 提前拆短);
                # 后续段:2 句或攒够 later_chars 字再送。
                ready = (seg_index == 0 and len(buffered) >= 1
                         or len(buffered) >= 2 or chars >= later_chars)
                if not ready:
                    continue
                submit("".join(buffered).strip())
                buffered, chars = [], 0
            # ★ 关键修复:生成结束后提交残留缓冲。修复前直接丢弃,症状就是
            #   短回答(如只回"你好。")整段没声、或回答末尾一句丢失。
            if buffered:
                submit("".join(buffered).strip())
            generation_thread.join()
            segment_queue.put(None)

        producer = threading.Thread(target=submit_tts_segments)
        producer.start()

        print(f"[LLM] 开始生成回答(第{turn}问): {user_text[:40]}", flush=True)
        tts_wall = None
        seg_times = []
        seg_index = 0
        board_started = False
        last_ok = True
        while True:
            item = segment_queue.get()
            if item is None:
                break
            request_id, stream_dir = item
            seg_index += 1
            seg_started = time.monotonic()
            tts = self._stream_tts_segment(
                queue, request_id, stream_dir,
                start_board=(not board_started or not last_ok), end_board=False)
            board_started = True
            last_ok = bool(tts.get("ok", False))
            seg_sec = round(time.monotonic() - seg_started, 2)
            seg_times.append(seg_sec)
            if tts_wall is None and self._first_spks_at is not None:
                tts_wall = round(self._first_spks_at - endpoint_wall, 3)
                print(f"[计时] 端点→首块音频下发: {tts_wall*1000:.0f} ms", flush=True)
            print(f"[计时] 段{seg_index} 合成+下发: {seg_sec:.2f}s", flush=True)
        producer.join()
        if self._board_spks_active:
            self._push_text("SPKE")
            self._board_spks_active = False
            print("[下行] 全部段落播完 · SPKE", flush=True)
        # 连续对话模式(协议多轮要求):SPKE 后重发 MIC_START,设备重新进入 LISTENING
        # 继续下一轮;关闭时设备回 IDLE 等本地唤醒。
        # 在线唤醒·持续对话(wake.enabled): 同样在 SPKE 后发 MIC_START 续听 —— 唤醒一次后
        # 不再要求重新说唤醒词,直到空闲超时由引擎回待机。
        if self._wake_enabled or self.r.get("mic_restart_after_answer", False):
            self._push_text("MIC_START")
            print("[下行] MIC_START(连续对话模式,设备→LISTENING)", flush=True)

        response = "".join(response_pieces).strip()
        self._remember(f"A{turn}: {response[:80]}")
        # 多轮历史入库(供下一轮 context)
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": response})
        total_sec = round(time.perf_counter() - started, 3)
        print(f"[计时] 第{turn}问: 端点→播完全部回答 {total_sec:.1f}s (LLM+合成 {seg_index} 段, 首字 {first_token_seconds}s, "
              f"首块音频 {tts_wall}s)", flush=True)
        row = {
            "time": _dt.now().isoformat(timespec="seconds"), "turn": turn,
            "recognized_text": user_text, "qwen_response": response,
            "asr_seconds": round(asr_seconds, 3),
            "endpoint_to_first_token_seconds": first_token_seconds,
            "endpoint_to_first_audio_seconds": tts_wall,
            "total_seconds": total_sec,
            "segments": seg_index, "input_wav": input_wav,
        }
        self._append_row(result_csv, row)
        return row

    def _speak_prompt(self, text, tts_base, queue, tts_dir, tag):
        """单段 TTS 播报(唤醒应答/提示语):非流式合成完整 wav → SPKS + PCM ≤1200B/帧 + SPKE。

        与 _answer_qa 的流式切段不同——应答是单句,直接 tts_request 拿到完整 wav 再下发,
        简单且不会与其他播放交错;播放期间设备仍在上传(含回声),调用方负责丢弃该段上行。
        """
        request_id = f"prompt_{tag}"
        wav = tts_dir / f"{request_id}.wav"
        from realtime_pipeline import tts_request
        tts_request(queue, request_id, text, wav)
        import wave
        with wave.open(str(wav), "rb") as w:
            pcm = w.readframes(w.getnframes())
            rate = w.getframerate()
        if not pcm:
            raise RuntimeError("唤醒应答 TTS 产物为空")
        self._push_text(f"SPKS {rate}")
        self._board_spks_active = True
        for off in range(0, len(pcm), 1200):
            self._push_pcm(pcm[off:off + 1200])
            time.sleep(0.02)
        self._push_text("SPKE")
        self._board_spks_active = False
        print(f"[下行] 唤醒应答播报完成: {text[:24]} ({len(pcm) / 2 / rate:.1f}s)", flush=True)

    def _stream_tts_segment(self, queue, request_id, stream_dir, start_board=True,
                            end_board=True):
        """把 TTS 子进程合成的一段(S 句)流式下发 WSS:SPKS <rate> + PCM 帧(≤1200B) + SPKE。

        与 TTS 子进程的通信协议(全是落盘文件,无共享内存):
          主进程等待 stream_dir/chunk_N.pcm 出现 → 读取后删除 → N+1;
          子进程最终写 queue/response_<id>.json {ok, stream_chunks, ...} 表示整段完成。

        下行协议细节(与固件 PROTOCOL.md 一致):
          - SPKS <rate>:开播命令,必须在任何 PCM 之前发出(固件没收到 SPKS 会丢弃 PCM);
          - PCM:每帧 ≤1200B(约 25ms @24kHz),按播放时长 ×0.88 的节奏下发
            (≈1.14×实时,让设备端缓冲始终攒一点音频;100% 会因抖动欠载拖音,
            过快会溢出缓冲)—— 与本地 V5 串口推流的 0.88 系数一致;
          - SPKE:结束命令。跨段连续播时仅首段发 SPKS、全部段播完由 _answer_qa 统一发 SPKE,
            避免段间出现"开播-停止"的咔哒声。
        返回: response 字典(ok 等);超时/出错返回 {"ok": False, "error": "stream timeout"}。
        """
        rate = int(self.r.get("downlink_rate", 24000))
        deadline = time.monotonic() + 180
        next_index = 0
        spoke = False
        while time.monotonic() < deadline and not self._stop.is_set():
            chunk_path = stream_dir / f"chunk_{next_index:06d}.pcm"
            pcm = None
            for _ in range(6):
                try:
                    if chunk_path.exists():
                        pcm = chunk_path.read_bytes()
                    break
                except PermissionError:      # TTS 子进程原子替换暂锁
                    time.sleep(0.02)
            if pcm is not None:
                if not spoke and start_board:
                    spoke = True
                    self._push_text(f"SPKS {rate}")
                    self._board_spks_active = True
                    if self._first_spks_at is None:
                        self._first_spks_at = time.monotonic()
                if self.sink_pcm:
                    data = pcm if len(pcm) % 2 == 0 else pcm[:-1]
                    for off in range(0, len(data), 1200):
                        self._push_pcm(data[off:off + 1200])
                    # 按播放节奏(≈1.14×实时)推流:避免突发灌满板卡TCP缓冲,
                    # 降低播放期设备 PONG 失联/重启概率(与本地V5 0.88 系数一致)
                    time.sleep((len(data) / 2) / rate * 0.88)
                else:
                    log.warning("无下行 sink,丢弃 %d B TTS 音频", len(pcm))
                chunk_path.unlink(missing_ok=True)
                next_index += 1
                continue
            response = queue / f"response_{request_id}.json"
            if response.exists():
                try:
                    data = json.loads(response.read_text(encoding="utf-8"))
                except PermissionError:
                    time.sleep(0.02)
                    continue
                response.unlink(missing_ok=True)
                if end_board and self._board_spks_active:
                    self._push_text("SPKE")
                    self._board_spks_active = False
                print(f"[下行] 段完成 · {next_index} 块音频"
                      + (f" · SPKE(段末)" if end_board and not self._board_spks_active else ""),
                      flush=True)
                if not data.get("ok"):
                    log.error("TTS 失败: %s", data.get("error"))
                return data
            time.sleep(0.01)
        if self._board_spks_active:
            self._push_text("SPKE")
            self._board_spks_active = False
            log.warning("TTS 段超时,已发 SPKE: %s", request_id)
        else:
            log.warning("TTS 段超时: %s", request_id)
        return {"ok": False, "error": "stream timeout"}

    # ---------------- 工具 ----------------

    def _push_text(self, text):
        if self.sink_text:
            self.sink_text(text)

    def _push_pcm(self, pcm_bytes):
        if self.sink_pcm:
            self.sink_pcm(pcm_bytes)

    def _remember(self, text):
        with self.lock:
            self.last_texts.append(text)
            if len(self.last_texts) > 20:
                self.last_texts.pop(0)

    def _short_sleep(self, sec):
        self._stop.wait(timeout=sec)

    def _load_base_tts_config(self):
        """只读引用 V5 配置:取其 tts/模型段(start_tts_worker 需要完整 config)。
        所有相对路径按 deps_root(依赖根)解析,并注入 TTS 环境/CosyVoice 源码的绝对路径。"""
        deps = self.deps_root
        cfg = {"project_root": str(deps)}
        # base_config 已不再必需:网络版 TTS 参数全部由本 config 的 tts 段提供;
        # 保留读取仅为兼容旧配置(指向 V5 文件时仍会合并其同名段)。
        base = self.real_cfg.get("base_config", "")
        if base:
            path = deps / base if not Path(base).is_absolute() else Path(base)
            if path.exists():
                try:
                    base_cfg = json.loads(path.read_text(encoding="utf-8"))
                    cfg.update(base_cfg)
                except Exception as exc:
                    log.warning("读取基础配置失败(%s),TTS 参数使用默认", exc)
        cfg.setdefault("models", {})
        cfg["models"].update({k: str(deps / v)
                              for k, v in self.real_cfg.get("models", {}).items()})
        # 本 config 的 tts 段覆盖 base_config 的同名段(音色/音量等统一在此管理,
        # base_config 只作为缺省来源,不需要改 V5 原文件)
        if self.cfg.get("tts"):
            cfg.setdefault("tts", {})
            cfg["tts"].update(self.cfg["tts"])
        cfg.setdefault("venvs", {})
        cfg["venvs"].setdefault("tts", str(deps / ".venv_5090_tts" / "Scripts" / "python.exe"))
        cfg.setdefault("third_party", {})
        cfg["third_party"].setdefault("cosy_voice_root", str(deps / "third_party" / "CosyVoice"))
        return cfg

    @staticmethod
    def _append_row(path, row):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        with path.open("a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)
