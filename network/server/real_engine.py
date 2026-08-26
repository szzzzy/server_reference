# -*- coding: utf-8 -*-
"""真实语音引擎(R1–R3):WSS PCM1 → 现有 VAD/ASR → Qwen3 一问一答 → TTS → WSS 下行。

- 不修改任何现有文件:通过 import 复用 board_serial_asr_test / asr_eval_core / realtime_pipeline;
- 一问一答:无唤醒词、无对话状态机、无角色提示词(全部可配置,默认如此);
- 运行要求:本模块与整个 run_server 需用 .venv_5090_llm 的 Python(有 torch/transformers/funasr)。
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


class RealVoiceEngine:
    def __init__(self, hub, cfg, run_dir, project_root_desc="."):
        self.hub = hub
        self.cfg = cfg
        self.run_dir = Path(run_dir)
        self.project_root = Path(project_root_desc)
        self.r = cfg.get("voice", {})
        self.real_cfg = self.r.get("real", {})
        self.stream = RingBuffer()

        self.lock = threading.Lock()
        self.status = "initializing"
        self.frames = 0
        self.frame_bytes = 0
        self.seq_gaps = 0
        self.last_seq = None
        self.bad_frames = 0
        self.commands = []
        self.file_uploads = []
        self.last_texts = []          # 最近识别/回答文本(供状态页)
        self.last_result = None       # 最近一轮指标

        self.sink_text = None         # async: text → WSS
        self.sink_pcm = None          # async: bytes → WSS
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._real_bytes_consumed = 0
        self._background_dbfs = None        # None=未校准;校准成功为实测背景
        self._calibration_done = False
        self._board_spks_active = False     # 下行 SPKS 是否已开(跨段连续播)

    # ---------------- 引擎接口(与 stub 一致)----------------

    def set_sink(self, text_fn, pcm_fn):
        self.sink_text = text_fn
        self.sink_pcm = pcm_fn

    def on_frame(self, pcm1, raw=None):
        with self.lock:
            seq = pcm1["seq"]
            if self.last_seq is not None and seq != (self.last_seq + 1) & 0xFFFFFFFF:
                self.seq_gaps += 1
            self.last_seq = seq
            self.frames += 1
            self.frame_bytes += pcm1["bytes_len"]
        if raw:
            self.stream.append(raw)     # 原始 PCM1 帧字节 → 现有 read_frame 直接工作
        self._wake_worker()

    def on_command(self, text):
        with self.lock:
            self.commands.append(text.strip())
            if len(self.commands) > 200:
                self.commands.pop(0)
        log.info("WSS 命令<- 设备: %s", text.strip()[:80])
        return True

    def on_bad_frame(self, reason):
        with self.lock:
            self.bad_frames += 1
        log.warning("WSS 非法 PCM1 帧: %s", reason)

    def snapshot(self):
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
        self._worker.start()

    def stop(self):
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
        root = self.project_root
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / "next_stage" / "full_pipeline_auto_5090"))

        import numpy as np
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer
        from asr_eval_core import load_paraformer
        from board_serial_asr_test import (
            capture_seconds, capture_until_endpoint, recognize, rms_dbfs, save_wav,
        )
        from realtime_pipeline import resolve, start_tts_worker

        models = self.real_cfg.get("models", {})
        tts_base = self._load_base_tts_config()

        # ---- 加载 ASR ----
        self.status = "loading_asr"
        asr, _, _ = load_paraformer(str(resolve(root, models["asr"])), "auto")
        log.info("ASR 就绪")

        # ---- TTS 子进程 ----
        self.status = "loading_tts"
        tts_process, queue = start_tts_worker(tts_base, self.run_dir)
        log.info("TTS worker 就绪")

        # ---- 加载 Qwen3 ----
        self.status = "loading_llm"
        tokenizer = AutoTokenizer.from_pretrained(
            str(resolve(root, models["llm"])), trust_remote_code=True, local_files_only=True)
        llm = AutoModelForCausalLM.from_pretrained(
            str(resolve(root, models["llm"])), torch_dtype="auto", device_map="auto",
            trust_remote_code=True, local_files_only=True)
        log.info("Qwen3 就绪")

        self.status = "ready"
        vad = self.real_cfg.get("vad", {})
        max_seconds = float(vad.get("max_seconds", 15))
        endpoint_ms = float(vad.get("endpoint_ms", 1000))
        default_background = float(vad.get("background_dbfs", -60))
        background_seconds = float(vad.get("background_seconds", 2.5))
        start_above = float(vad.get("start_above_db", 3))
        end_above = float(vad.get("end_above_db", 3))
        # 语音流停止后,在端点静音窗口内自动补静音帧,让现有 VAD 端点逻辑生效
        self.stream.enable_auto_silence(endpoint_ms / 1000.0 + 0.4)

        audio_dir = self.run_dir / "audio"
        tts_dir = self.run_dir / "tts"
        result_csv = self.run_dir / "voice_qa_results.csv"
        turn = 0

        while not self._stop.is_set():
            self._short_sleep(0.2)
            if self.stream.real_bytes_total <= self._real_bytes_consumed:
                continue
            if not self._calibration_done:
                self._calibrate_background(self.stream, background_seconds)
            background_dbfs = (self._background_dbfs if self._background_dbfs is not None
                               else default_background)
            try:
                samples, _, endpoint = capture_until_endpoint(
                    self.stream, max_seconds=max_seconds,
                    background_dbfs=background_dbfs,
                    endpoint_silence_ms=endpoint_ms,
                    threshold_above_bg=start_above,
                    endpoint_threshold_above_bg=end_above,
                    endpoint_active_penalty=float(vad.get("active_penalty", 4.0)),
                    voice_start_ms=120, voice_start_window_ms=300,
                )
            except TimeoutError:
                self._real_bytes_consumed = self.stream.real_bytes_total
                continue
            self._real_bytes_consumed = self.stream.real_bytes_total
            log.info("VAD: 端点触发=%s 起始=%.2fs 最后活跃=%.2fs 尾静音=%sms 阈值=%.1fdB 音频=%.2fs",
                     bool(endpoint.get("endpoint_triggered")),
                     float(endpoint.get("speech_start_seconds") or 0.0),
                     float(endpoint.get("last_active_seconds") or 0.0),
                     int(endpoint.get("trailing_silence_ms") or 0),
                     float(endpoint.get("vad_threshold_dbfs") or background_dbfs),
                     round(len(samples) / 16000, 2))
            if len(samples) < 1600:      # < 0.1s,忽略
                self.stream.reset_input_buffer()
                continue

            t_endpoint = time.monotonic()   # 计时原点:VAD 判定"说完了"
            turn += 1
            input_wav = audio_dir / f"qa_{turn:03d}_input.wav"
            save_wav(input_wav, samples)
            recognized, first_partial, asr_seconds = recognize(asr, samples)
            recognized = (recognized or "").strip()
            print(f"[识别] 第{turn}问({asr_seconds:.2f}s): {recognized or '[空]'}", flush=True)
            self._remember(f"Q{turn}: {recognized or '[空]'}")
            if not recognized:
                print("[识别] 空文本,本轮跳过", flush=True)
                continue

            self._endpoint_wall = t_endpoint      # 供 TTS 首块计时
            self._first_spks_at = None
            answered = self._answer_qa(
                turn, recognized, tokenizer, llm, streamer_cls=TextIteratorStreamer,
                sentence_chunks=self._sentence_chunks, queue=queue, tts_dir=tts_dir,
                tts_base=tts_base, result_csv=result_csv,
                asr_seconds=asr_seconds, input_wav=str(input_wav),
                endpoint_wall=t_endpoint,
            )
            self.last_result = answered
            self.stream.reset_input_buffer()   # 丢弃回合间残留的补静音帧

        if tts_process:
            try:
                tts_process.terminate()
            except Exception:
                pass

    def _calibrate_background(self, stream, seconds):
        """借鉴 V5 开机校准:取流起始的 seconds 秒音频,用帧电平 10% 分位数估计背景。
        窗口内即使有零星人声,分位数仍贴近噪声底;整窗都是人声或音频过短则保留默认背景。
        每次引擎运行只尝试一次(会话内固定,与 V5"每插板校准一次"语义一致)。"""
        self._calibration_done = True
        try:
            samples, _ = capture_seconds(stream, seconds)
        except TimeoutError:
            self._real_bytes_consumed = stream.real_bytes_total
            log.warning("背景校准超时,使用默认背景(dBFS=cfg)")
            return
        self._real_bytes_consumed = stream.real_bytes_total
        if len(samples) < 6400:      # 少于 0.4s 的窗口不可信
            log.warning("背景校准音频过短(%d 样本),使用默认背景", len(samples))
            return
        n = len(samples) // 320
        rows = np.array(samples[:n * 320], dtype=np.int16).reshape(-1, 320)
        bg = float(np.percentile([rms_dbfs(row) for row in rows], 10))
        if -85.0 < bg < -40.0:
            self._background_dbfs = bg
            log.info("背景校准: %.1f dBFS (%.1fs 音频, 帧电平10%%分位)", bg, seconds)
        else:
            log.warning("背景校准窗口疑似含人声(%.1f dBFS),使用默认背景", bg)

    # ---------------- 分句(重建:原版 sentence_chunks 含面向控制台的 print,
    # 在 GBK 控制台遇到 emoji 会炸;本实现逻辑一致、无副作用)----------------

    @staticmethod
    def _sentence_chunks(streamer):
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
        # 问答形态/提示词不作调整;本轮仅按 V5 编排重构 TTS 分段:
        # 切句线程边切边预提交全部段(TTS 合成与上一段播放重叠),播放线程只消费,
        # 消除"一句播完才合成下一句"造成的段间 2-4s 空洞。
        import queue as thread_queue
        from datetime import datetime as _dt

        messages = [{"role": "user", "content": user_text}]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
        streamer = streamer_cls(tokenizer, skip_prompt=True, skip_special_tokens=True)
        started = time.perf_counter()
        generation_thread = threading.Thread(target=llm.generate, kwargs=dict(
            **inputs, streamer=streamer,
            max_new_tokens=int(self.real_cfg.get("max_new_tokens", 180)),
            do_sample=False,
        ))
        generation_thread.start()

        response_pieces = []
        segment_queue = thread_queue.Queue()
        first_token_seconds = None

        def submit_tts_segments():
            nonlocal first_token_seconds
            for sentence in sentence_chunks(streamer):
                response_pieces.append(sentence)
                if first_token_seconds is None:
                    first_token_seconds = round(time.monotonic() - endpoint_wall, 3)
                    print(f"[计时] 端点→LLM首字: {first_token_seconds*1000:.0f} ms", flush=True)
                seg_index = len(response_pieces)
                request_id = f"turn_{turn:03d}_{seg_index:03d}"
                wav = tts_dir / f"{request_id}.wav"
                stream_dir = queue / f"stream_{request_id}"
                stream_dir.mkdir(parents=True, exist_ok=True)
                (queue / f"request_{request_id}.json").write_text(json.dumps({
                    "id": request_id, "text": sentence, "output_wav": str(wav),
                    "stream_dir": str(stream_dir),
                }, ensure_ascii=False), encoding="utf-8")
                print(f"[TTS] 段{seg_index}: {sentence[:40]}", flush=True)
                segment_queue.put((request_id, stream_dir))
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

        response = "".join(response_pieces).strip()
        self._remember(f"A{turn}: {response[:80]}")
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

    def _stream_tts_segment(self, queue, request_id, stream_dir, start_board=True,
                            end_board=True):
        """轮询 TTS 子进程流式输出并下发 WSS:SPKS <rate> + PCM 帧(≤1200B) + SPKE。
        跨段连续播:仅首段 start_board=True 发 SPKS,全部段播完后由调用方统一 SPKE。"""
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
        """只读引用 V5 配置:取其 tts/模型段(start_tts_worker 需要完整 config)。"""
        base = self.real_cfg.get("base_config", "")
        path = self.project_root / base if base and not Path(base).is_absolute() else Path(base)
        cfg = {"project_root": str(self.project_root)}
        if path.exists():
            try:
                base_cfg = json.loads(path.read_text(encoding="utf-8"))
                cfg.update(base_cfg)
            except Exception as exc:
                log.warning("读取基础配置失败(%s),TTS 参数使用默认", exc)
        cfg.setdefault("models", {})
        cfg["models"].update({k: str(self.project_root / v)
                              for k, v in self.real_cfg.get("models", {}).items()})
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
