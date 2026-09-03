# -*- coding: utf-8 -*-
"""真实语音引擎(RealVoiceEngine):WSS PCM1 → 线上唤醒/VAD/ASR → Qwen3 问答 → TTS → WSS 下行。

完整链路编排(本文件是"编排层",具体推理实现都在 engine/ 目录被 import 复用):
  上行: 设备/WSS 客户端(新版固件 WSS 认证后持续上传 PCM1) → WssAdapter → on_frame()
        → RingBuffer(字节流兼容层,语音停流自动补静音帧)
        → board_serial_asr_test.capture_until_endpoint()(能量 VAD,选出"一段完整的话")
        → board_serial_asr_test.recognize()(FunASR Paraformer 流式,0.5s 内出文本)
  推理: Qwen3-4B 流式生成(_answer_qa,后台线程 + TextIteratorStreamer)
        → _sentence_chunks 按标点切句 → 每句一个 request_*.json 交给 TTS 子进程
  下行: _stream_tts_segment 轮询 chunk_*.pcm → SPKS <rate> + PCM(≤1200B/帧,逐帧匀速 ≈1.0×实时) + SPKE
        + SPKE,经 set_sink 注册的回调广播到所有在线 WSS 客户端(线程安全调度)。

会话形态(2026-09-01 修正):
  - 待机态: 服务器流式 ASR 判定唤醒词"你好小科"(同音容错,600ms 块级),命中→应答→MIC_START;
  - 唤醒态: 唤醒一次·持续对话 —— 每轮 SPKE 后自动 MIC_START 续听,普通对话轮不要求重新唤醒;
  - 回待机(需重新唤醒)仅三个约束: ① 600s 空闲超时 ② 意图轮结束(睡眠/拒绝/敷衍超限,dismiss)
    ③ 设备断联(掉 WiFi/重启/正常关闭→重连后必须重新说唤醒词);
  - 播放期: 半双工(不回采回声)+ 语义回放免疫打断(识别内容≠播放文本 → 提前 SPKE+MIC_START);
  - 动态底噪: 双窗估计器自适应阈值(默认开),启用时跳过开机静态校准。
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


def _echo_similarity(short, long_text):
    """子串容忍的"回声相似度":播放回声的前缀/截断残片(如"一二"、"听起")是
    播放文本的子串 → 相似度≈1;真实插话与播文几乎无公共子串 → ≈0。

    实现: difflib 匹配块长度和(近似 LCS)除以较短长度。
    用途: 替换整串 ratio() —— 整串比对会把"前缀截断的回声"误判为不同文本
    → 假打断 → 短回答被截断 → 接住作答答的是回声 → 自问自答(2026-09-02 实测)。
    """
    if not short or not long_text:
        return 0.0
    from difflib import SequenceMatcher
    blocks = SequenceMatcher(None, short, long_text).get_matching_blocks()
    matched = sum(b.size for b in blocks)
    return matched / max(1, min(len(short), len(long_text)))


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


class _InterruptDetector(threading.Thread):
    """播放期"抢话检测"线程（零依赖·语义回放免疫）。

    原理：服务器自知道正在播放的 TTS 文本（参考信号）；播放期间独立消费上行帧，
    流式 ASR（600ms 块）增量识别，与播放文本做相似度比对 —— 识别出与播放内容明显
    不同的内容且字数足够 → 判定用户插话（set hit_event）。不需要 AEC 库/声学路径
    估计；回声（≈播放内容）相似度高，不会误打断。

    生命周期：_playback_begin(text) 启动、_playback_end() 停止；daemon 线程，
    停止依赖"设备持续上传 → read 立即返回"（新版固件满足）。
    """

    def __init__(self, stream, model, play_text, hit_event, min_chars, min_similarity):
        super().__init__(daemon=True)
        self._stream = stream
        self._model = model
        self._play_text = _normalize_wake(play_text)
        self._hit = hit_event
        self._min_chars = max(2, int(min_chars))
        self._min_sim = float(min_similarity)
        self._stop_flag = threading.Event()   # 注意: 不能叫 _stop(覆盖 Thread._stop 内部方法)
        self.hit_text = ""                    # v2: 命中时保留的用户插话文本(供"打断接住"直接作答)
        self._part_marks = []                 # [(块文本, 与播文相似度)] — 用于从混音结果切出人声段

    def stop(self):
        self._stop_flag.set()

    @staticmethod
    def _hit_text_from_marks(marks, min_sim, fallback):
        """从逐块标记 [(块文本, 与播文相似度)] 中切出用户插话文本:
        取"最后一个回声相似块(相似度≥min_sim)"之后的全部文本,前段回声被剔除;
        无任何相似块(异常)时用整段文本兜底。"""
        last_echo = -1
        for i, (_, pr) in enumerate(marks):
            if pr >= min_sim:
                last_echo = i
        text = "".join(pt for pt, _ in marks[last_echo + 1:])
        return text or fallback

    def run(self):
        try:
            import numpy as np
            from difflib import SequenceMatcher
            from board_serial_asr_test import read_frame
            asr = StreamingAsr(self._model)
            buf = []
            while not self._stop_flag.is_set():
                try:
                    _, _, frame = read_frame(self._stream)
                except Exception:
                    break                     # 流关闭/停止:退出
                buf.append(frame)
                if len(buf) >= 15:            # 300ms 一块(15×320=4800 样本):判别粒度小,命中更快
                    chunk = np.concatenate(buf[:15])
                    buf = buf[15:]
                    n_before = len(asr.parts)
                    asr.on_chunk(chunk)
                    # 逐块标记: 本次新识别文本与播放文本的相似度(回声≈高,人声≈低)
                    # —— 用子串容忍的 _echo_similarity(回声残片是播文子串)
                    for part in asr.parts[n_before:]:
                        pt = _normalize_wake(part)
                        if pt:
                            self._part_marks.append(
                                (pt, _echo_similarity(pt, self._play_text)))
                    text = _normalize_wake("".join(asr.parts))
                    if len(text) >= self._min_chars:
                        if _echo_similarity(text, self._play_text) < self._min_sim:
                            # 与播放内容明显不同 → 用户插话;前段"回声相似块"被剔除
                            self.hit_text = self._hit_text_from_marks(
                                self._part_marks, self._min_sim, text)
                            self._hit.set()   # 与播放内容明显不同 → 用户插话
                            return
        except Exception:
            pass                              # 判别失败不致命(等价于未打断)


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
        self.mqtt = None        # 由 run_server 注入(MqttAdapter);意图语义结果经其发 vcmd topic
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
        self._playback_until = 0.0          # 半双工: 该时刻前不听/丢弃上行(扬声器回声余震)
        # ---- 播放期抢话检测(零依赖·语义回放免疫,见 _InterruptDetector) ----
        # 2026-09-02 晚: 无 AEC 时"回声 ASR 文本"比对播文本质不可靠(截断前缀/听错回声
        # 两个失效面)→ 自问自答链。默认关闭(纯半双工);方案A(参考信号声学相关)落地后
        # 再恢复 enabled=true。
        _icfg = self.real_cfg.get("interrupt") or {}
        self._interrupt_enabled = bool(_icfg.get("enabled", False))
        self._interrupt_min_chars = int(_icfg.get("min_chars", 2))
        self._interrupt_min_similarity = float(_icfg.get("min_similarity", 0.5))
        self._interrupt_hit = threading.Event()   # 播放期被 set = 用户抢话(打断)
        self._pending_interrupt_text = None       # v2 打断接住: 命中时保留的插话文本,主循环直接作答
        self._speech_detector = None              # 前置语音检测器(fsmn-vad;None=纯能量 VAD)
        self._nonspeech_count = 0                 # 被前置检测跳过的非语音段计数(观测)
        # ---- 设备 FSM 对齐(2026-09-02): 唤醒"先 S4 后表达" —— wake_detected → state_ready(S4)
        #      服务器收到 state_ready 后才播唤醒应答;设备播完停留 S4(WAKE_ACK 语义)。 ----
        self._wake_ready_event = threading.Event()   # state_ready 到达信号
        self._wake_ready_id = None                   # 期望的 interaction_id
        self._wake_ready_state = ""                  # 收到的 state(如 "S4")
        self._session_interaction_id = None          # 当前会话 interaction_id(唤醒时产生,语义回执携带)
        self._mic_started = False                 # 设备当前是否在 LISTEN(非唤醒说完成后置 False,
                                                  # 检测到下一轮合法语音起始再发 MIC_START)
        self._last_spke_at = 0.0                  # 最近一次 SPKE 时刻(MIC_START 最小间隔用)
        self._detector = None                     # 当前播放期的判别线程
        self._asr_model = None                    # 判别线程用的 ASR 模型(加载后赋值)
        self._first_spks_at = None          # 本轮首块音频下发的时刻(端点→首块计时用)
        self._turn_first_frame_at = None    # 本轮第一帧到达时刻(完整链路计时起点)
        self._history = []                  # 多轮对话历史[{user},{assistant}...],按 history_turns 截取
        # ---- 线上唤醒态(在 __init__ 初始化,on_client_connected/disconnected 可能先于
        #      _load_and_answer_loop 到达;该循环内也有一次同名初始化,语义一致) ----
        self._awake = False                 # True=唤醒态(可续听问答);False=待机(等唤醒词)
        self._awake_at = 0.0                # 最后一次交互时刻(空闲超时判定用)
        self._online = False                # 是否有 WSS 设备在线(断联立即回待机)
        self._noreply_streak = 0            # 连续敷衍疑似计数(会话级;前N-1次仅计数按正常对话,
                                            # 第N次确认dismiss;断联/重唤醒清零)

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

    def on_client_connected(self, ip=""):
        """WSS 设备接入:新连接一律从待机态开始(必须先说唤醒词)。

        覆盖"旧连接尚未被服务器发现断开(如 TCP 半开,ping 超时最迟 ~90s)设备已重连"
        的窗口,保证任何新接入的设备都从待机开始 —— 会话状态跟连接走,不沿用旧会话。
        """
        with self.lock:
            self._online = True
            self._awake = False
            self._awake_at = 0.0
            self._mic_started = False
            self._session_interaction_id = None   # 新会话: 旧 interaction_id 作废
            # 新会话:清掉上一会话残留的历史(重连后首段不能被旧数据污染)
            self._history = []
            self._noreply_streak = 0
        self._remember(f"CONNECT {ip or '?'} (回待机)")
        self.stream.reset_input_buffer()
        self._real_bytes_consumed = self.stream.real_bytes_total
        log.info("WSS 设备接入(%s): 会话从待机开始,需说唤醒词", ip)

    def on_client_disconnected(self, ip=""):
        """WSS 设备断开(掉 WiFi/重启/正常关闭):立即终止本会话回待机。

        设计文档 §1"WSS 断链/设备断开 → 服务器终止本会话任务,清 RingBuffer 与 LLM
        history":断联后即使重新接入,也必须重新说唤醒词,不再沿用断开前的唤醒态。
        注意: 在 WSS 事件循环线程调用,只做非阻塞操作(判别线程只置停止标志,不 join)。
        """
        with self.lock:
            self._online = False
            self._awake = False
            self._awake_at = 0.0
            self._mic_started = False
            self._session_interaction_id = None   # 会话结束: interaction_id 作废
            self._history = []
            self._noreply_streak = 0
        self._remember(f"DISCONNECT {ip or '?'} (回待机)")
        # 播放期抢话判别线程:只置停止标志,线程在下次读帧时自行退出(不 join 阻塞事件循环)
        if self._detector is not None:
            self._detector.stop()
        # 清掉断开前残留的上行字节(含合成静音帧),避免重连后首段被旧数据污染
        self.stream.reset_input_buffer()
        self._real_bytes_consumed = self.stream.real_bytes_total
        log.info("WSS 设备断开(%s): 会话终止回待机,重连后需重新唤醒", ip)

    def on_state_ready(self, payload=None):
        """设备状态回执: WSS 文本 {"type":"state_ready","interaction_id":"...","state":"S4"}。

        唤醒协议(2026-09-02): 服务器发 wake_detected → 设备完成 S3/S5/S6→S4 迁移后回此消息
        → 服务器等 state_ready=S4 后才播唤醒应答(设备 FSM 异步队列,发完 wake_detected
        立即跟 SPKS 会在设备尚在 S5/S6 时到达 → 状态对不上)。
        异常/旧固件不回执 → 引擎按 ready_timeout_s 超时仍播应答(降级兼容)。
        """
        try:
            data = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
        except Exception:
            data = {}
        got_id = str(data.get("interaction_id") or "")
        if self._wake_ready_id is None or got_id != self._wake_ready_id:
            # 无待唤醒(旧回执/误发)或 id 不匹配(上一轮迟到的 state_ready)→ 忽略
            log.warning("state_ready 忽略: 当前期望 id=%s,收到 id=%r state=%r",
                        self._wake_ready_id, got_id, data.get("state"))
            return
        self._wake_ready_state = str(data.get("state", ""))
        self._wake_ready_event.set()
        log.info("设备状态回执: state=%r (interaction_id=%s)", self._wake_ready_state, got_id)

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
                "online": self._online,
                "awake": self._awake,
                "non_speech_segments": self._nonspeech_count,
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
        """引擎主循环:加载三大模型 → 常驻 → 线上唤醒状态机(待机态↔唤醒态)无限循环。

        加载顺序(考虑显存与依赖):
          ① ASR(FunASR Paraformer,GPU 主进程内);
          ② TTS 子进程(CosyVoice2,独立 venv,经 start_tts_worker 拉起,等 ready.json);
          ③ LLM(Qwen3-4B,transformers,GPU 主进程内)。
        此后所有模型常驻显存,循环里只做推理,不再重载。

        主循环流程(while not stop;新版固件 WSS 认证后持续上传 PCM1):
          - 空闲超时检查(唤醒态 600s 无活动 → 回待机);
          - 待机态(voice.real.wake.enabled 且未唤醒): 唤醒参数 VAD 判一段 → 仅"判了起始"
            的段做流式 ASR + finish 冲刷(600ms 块级,命中 early-stop)→ 同音容错匹配
            "你好小科" → 命中: TTS 应答("我在，请讲。") → 丢弃应答期上行(回声) →
            MIC_START(设备→LISTEN) → 进入唤醒态;未命中继续守听(不下发命令);
          - 唤醒态(= 原有"听—想—说"): capture_until_endpoint 判一句
            → MIC_STOP(设备→THINKING) → recognize → _answer_qa
              (SPKS<PCM>SPKE 下行;SPKE 后自动 MIC_START 续听 —— 唤醒一次·持续对话)
            → 记一轮指标 → 清残留;
          - 播放期: 半双工(SPKS→SPKE 不消费上行 + SPKE 后 0.6s 余震丢弃)+
            语义回放免疫打断(_InterruptDetector 300ms 块判别,抢话即提前 SPKE+MIC_START);
          - 动态底噪启用时跳过开机静态校准(bg 由估计器预语音段自学)。
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
        from intent import decide_intent

        models = self.real_cfg.get("models", {})
        tts_base = self._load_base_tts_config()

        # ---- 加载 ASR ----
        self.status = "loading_asr"
        asr, _, _ = load_paraformer(str(resolve(deps, models["asr"])), "auto")
        self._asr_model = asr               # 供播放期抢话判别线程复用
        log.info("ASR 就绪")
        # ---- 前置语音检测(可插拔): 模型判"此段有语音"才允许进轮,噪声从源头不进入;
        #      模型缺失/加载失败 → None(纯能量 VAD 行为不变,见 engine/speech_detector.py) ----
        from speech_detector import load_speech_detector
        self._speech_detector = load_speech_detector(
            self.real_cfg.get("vad", {}).get("speech_detector"), deps)

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
        wk_timeout_s = float(wk_cfg.get("timeout_seconds", 600.0) or 0.0)
        # 待机判定参数(唤醒词判定要"灵敏":起声 20ms/窗口 500ms、门限默认 3dB ——
        # 唤醒只探测"有人说话",宁可多判候选 ASR 也不能漏掉唤醒词;
        # 唤醒后的对话态由 voice.real.vad 控制(默认 5dB/120ms,严格) —— 见 config)
        wk_listen_s = float(wk_cfg.get("listen_seconds", 8.0))
        wk_endpoint_ms = float(wk_cfg.get("endpoint_silence_ms", 500))
        wk_start_ms = int(wk_cfg.get("start_active_ms", 20))
        wk_window_ms = int(wk_cfg.get("start_window_ms", 500))
        wk_start_above = float(wk_cfg.get("start_above_db", 3.0))
        wk_end_above = float(wk_cfg.get("end_above_db", 3.0))
        self._awake = False
        self._awake_at = 0.0
        if self._wake_enabled:
            log.info("线上唤醒: 启用 (词=%s 应答=%r 空闲超时=%.0fs)",
                     self._wake_words, self._wake_prompt, wk_timeout_s)
        # ---- 意图决策层 v1(规则):睡眠/拒绝/不插话短路 + 共情标记注入,见 engine/intent.py ----
        it_cfg = self.real_cfg.get("intent")
        it_cfg = it_cfg if isinstance(it_cfg, dict) else {}
        self._intent_enabled = bool(it_cfg.get("enabled", False))
        self._intent_cfg = it_cfg
        self._noreply_tolerance = int(it_cfg.get("no_reply_tolerance_rounds", 3))
        self._noreply_streak = 0        # 连续敷衍疑似计数(前N-1次"嗯嗯"仅疑似:按正常对话,
                                        # 连续第N次才确认敷衍 → dismiss)
        if self._intent_enabled:
            log.info("意图层: 启用 (sleep_ack=%r decline_ack=%r)",
                     it_cfg.get("sleep_ack", ""), it_cfg.get("decline_ack", ""))
        # 语音流停止后,在端点静音窗口内自动补静音帧,让现有 VAD 端点逻辑生效
        self.stream.enable_auto_silence(endpoint_ms / 1000.0 + 0.4)

        audio_dir = self.run_dir / "audio"
        tts_dir = self.run_dir / "tts"
        result_csv = self.run_dir / "voice_qa_results.csv"
        turn = 0

        while not self._stop.is_set():
            self._short_sleep(0.2)
            # ---- 打断接住(含唤醒回应/播放期抢话): text 直接作答,不等新一轮 VAD ----
            # 上限 interrupt_max_burst 次,防"抢话-作答-再抢话"无限循环。
            # 放主循环最前: 唤醒路径 continue 后也会立即被消费。
            burst = 0
            while self._pending_interrupt_text:
                text = self._pending_interrupt_text
                self._pending_interrupt_text = None
                burst += 1
                if burst > int(self._intent_cfg.get("interrupt_max_burst", 3)):
                    log.warning("打断接住超过上限(%d),丢弃: %r", burst - 1, text[:40])
                    break
                turn += 1
                print(f"[打断] 接住抢话(第{burst}次)直接作答: {text[:40]!r}", flush=True)
                self._remember(f"Q{turn}(打断): {text[:40]}")
                # 设备 FSM(2026-09-02): DIALOG_REPLY 只在 S2.2 接受;上轮回答播完设备已到 S1。
                # 接住作答前必须补"话语循环": MIC_START(→听/S2.1) → MIC_STOP(→S2.2),
                # 否则 SPKS 落在 S1 → 设备上报 ERROR playback_state(实测 11:21:36 等 ×3)。
                self._send_mic_start("打断接住·前置")
                self._push_text("MIC_STOP")
                time.sleep(0.05)
                try:
                    self.last_result = self._answer_qa(
                        turn, text, tokenizer, llm, streamer_cls=TextIteratorStreamer,
                        sentence_chunks=self._sentence_chunks, queue=queue, tts_dir=tts_dir,
                        tts_base=tts_base, result_csv=result_csv,
                        asr_seconds=0.0, input_wav="", endpoint_wall=time.monotonic(),
                    )
                except Exception:
                    log.exception("第%d轮(打断接住)问答异常", turn)
                    if self._board_spks_active:
                        self._push_text("SPKE")
                        self._board_spks_active = False
                        self._playback_end(0.3)
                        print("[下行] SPKE(打断接住异常收尾)", flush=True)
                self._turn_first_frame_at = None
            # ---- 半双工(无 AEC):SPKE 后 0.6s 内不听 —— 扬声器回声余震;
            #      播放期间(SPKS→SPKE)积累的上行也在这里一次性清掉,否则设备会把
            #      "我刚播的内容"录回去,服务器再识别 → 自问自答。
            #      判别线程未退出时不清(避免与另一读者并发操作流)。
            if self._detector is None and time.monotonic() < self._playback_until:
                self.stream.reset_input_buffer()
                self._real_bytes_consumed = self.stream.real_bytes_total
                self._short_sleep(0.1)
                continue
            # ---- 唤醒态空闲超时(唯一退出条件): 持续对话时 600s 无活动段 → 回待机等下次唤醒词。
            #     放在最外层(等字节之前):即使设备停传/无新字节也要计时;不发任何下行命令
            #     (设备侧 LISTEN/IDLE 由固件 5 分钟远场待机自愈,PCM 不受影响)。
            if (self._wake_enabled and self._awake and wk_timeout_s > 0
                    and time.monotonic() - self._awake_at > wk_timeout_s):
                print(f"[唤醒] 空闲 {wk_timeout_s:.0f}s 无交互 → 回待机(等下次唤醒词)", flush=True)
                self._remember("WAKE- timeout")
                self._awake = False
                self._mic_started = False
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
                # 流式增量唤醒判定(600ms 块粒度,检测频率高):边说边识,命中即 early-stop;
                # 未命中再整段识别兜底(块边界跨字等罕见情形)。段上限 wk_listen_s 默认 3s。
                w_asr = StreamingAsr(asr)
                w_hit = threading.Event()

                def _wake_chunk(block):
                    w_asr.on_chunk(block)
                    if any(n in _normalize_wake("".join(w_asr.parts))
                           for n in self._wake_needles):
                        w_hit.set()

                try:
                    w_samples, _, w_ep = capture_until_endpoint(
                        self.stream, max_seconds=wk_listen_s,
                        background_dbfs=background_dbfs,
                        endpoint_silence_ms=wk_endpoint_ms,
                        threshold_above_bg=wk_start_above,
                        endpoint_threshold_above_bg=wk_end_above,
                        endpoint_active_penalty=active_penalty,
                        voice_start_ms=wk_start_ms,
                        voice_start_window_ms=wk_window_ms,
                        floor_tracker=self._floor,
                        on_chunk=_wake_chunk,
                        stop_event=w_hit,
                    )
                except TimeoutError:
                    self._real_bytes_consumed = self.stream.real_bytes_total
                    continue
                self._real_bytes_consumed = self.stream.real_bytes_total
                if not w_ep.get("speech_started") or len(w_samples) < 1600:
                    continue                      # 环境静音/极短段:不识别,继续守听
                # 前置语音检测同样作用于唤醒候选: 噪声段不判唤醒词(从源头不进)
                if self._speech_detector is not None:
                    ok_sp, sp_ratio, sp_ms = self._speech_detector.is_speech(w_samples)
                    if not ok_sp:
                        self._nonspeech_count += 1
                        print(f"[语音检测] 唤醒候选段非语音(占比={sp_ratio:.2f}) → 继续守听",
                              flush=True)
                        continue
                if w_hit.is_set():
                    # 流式快速路:parts 已提前含唤醒词 → 不等段尾,立即结束并冲刷确认
                    w_crop = self._crop_speech_window(w_samples, w_ep)
                    wake_text, _, _ = (recognize(asr, w_crop) if w_crop is not None
                                       else w_asr.finish(w_samples))
                    wake_text = (wake_text or "").strip()
                    self._remember(f"WAKE? {wake_text or '[空]'}")
                    print(f"[唤醒] 流式命中(段={round(len(w_samples) / 16000, 2)}s): "
                          f"{wake_text[:40]!r}", flush=True)
                    hit = True
                else:
                    # 常规路径:finish 冲刷尾巴(is_final)拼出完整文本 —— on_chunk 非 final
                    # 块文本不完整(词尾滞后),必须 flush;同块边界下与 recognize() 等价。
                    # 段内静默过多时同样裁剪后重识(防唤醒词被稀释判空)。
                    w_crop = self._crop_speech_window(w_samples, w_ep)
                    if w_crop is not None:
                        wake_text, _, wk_asr_s = recognize(asr, w_crop)
                    else:
                        wake_text, _, wk_asr_s = w_asr.finish(w_samples)
                    wake_text = (wake_text or "").strip()
                    value = _normalize_wake(wake_text)
                    self._remember(f"WAKE? {wake_text or '[空]'}")
                    print(f"[唤醒] 候选识别({wk_asr_s:.2f}s): {wake_text!r}", flush=True)
                    hit = any(n in value for n in self._wake_needles)
                if hit:
                    interaction_id = f"wake_{int(time.time() * 1000)}"
                    self._session_interaction_id = interaction_id
                    print(f"[唤醒] 命中唤醒词 → WAKE_DETECTED({interaction_id})", flush=True)
                    self._remember(f"WAKE+ {wake_text}")
                    # 设备 FSM(2026-09-02 定稿): 状态与表达解耦 —— S3/S5/S6 被唤醒先进入 S4,
                    # 再播放唤醒应答(表达在 S4 内)。服务器: 发 wake_detected → 等设备
                    # state_ready=S4(设备 FSM 异步队列,不能假设迁移已完成)→ 才播应答
                    # (WAKE_ACK);应答播完设备停留 S4,不再主动 MIC_START —— 实际话语开始时
                    # 由 _on_speech_start 发 MIC_START(在 S4 内仅标记本轮话语开始)。
                    # 旧实现"应答播完→MIC_START"导致设备在收到唤醒词后 2s+ 仍停在 S5/S6。
                    self.stream.reset_input_buffer()
                    self._real_bytes_consumed = self.stream.real_bytes_total
                    self._push_text(json.dumps(
                        {"type": "wake_detected", "interaction_id": interaction_id},
                        ensure_ascii=False))
                    self._wake_ready_event.clear()
                    self._wake_ready_id = interaction_id
                    wk_ready_timeout = float(wk_cfg.get("ready_timeout_s", 2.0))
                    if self._wake_ready_event.wait(timeout=wk_ready_timeout):
                        print(f"[唤醒] 设备 state_ready={self._wake_ready_state!r} → 播应答",
                              flush=True)
                        if self._wake_prompt:
                            reply_ok, reply_txt = self._speak_prompt(
                                self._wake_prompt, tts_base, queue, tts_dir,
                                tag=f"wake_{int(time.time())}")
                            if not reply_ok and reply_txt:
                                # 唤醒回应期间用户抢话(固件: MIC_START 取消 WAKE_REPLY /
                                # 保持 S4 / listening) —— 插话文本即"实际话语",直接进入对话
                                self._pending_interrupt_text = reply_txt
                                print(f"[打断] 唤醒回应被抢话({reply_txt[:32]!r}) → 待接住作答",
                                      flush=True)
                    else:
                        # 设备未回 state_ready(启动中/旧固件)→ 不在 S4,此时发 SPKS 会被
                        # 设备报 ERROR playback_state(实测 11:20:26)。改为跳过应答:
                        # 实际话语开始时的 MIC_START 会使设备 S3/S5/S6→S4(固件修正 #1),
                        # 会话照常;仅少了"我在,请讲"提示音。
                        log.warning("wake_detected 后 %.1fs 未收到 state_ready(设备未就绪/旧固件)"
                                    " → 跳过唤醒应答,等实际话语(MIC_START 驱动 S4)",
                                    wk_ready_timeout)
                        print("[唤醒] 跳过应答(设备未确认 S4),等实际话语", flush=True)
                    # 应答播完: 设备停留 S4 并保持 listening;MIC_START 留给实际话语
                    self._mic_started = False
                    print("[下行] 唤醒应答播完(WAKE_ACK) → 设备停留 S4,等实际话语", flush=True)
                    # 应答播放(约 1~2s)期间设备可能已断联:断联回调会把 _online/_awake
                    # 置 False,这里在锁内复核,避免把"断联后"的过期命中重新置为唤醒态
                    with self.lock:
                        if self._online:
                            self._awake = True
                            self._awake_at = time.monotonic()   # 持续对话: 最后交互时刻
                            self._noreply_streak = 0            # 新会话: 敷衍计数清零
                        else:
                            log.info("唤醒命中但设备已断联 → 不回唤醒态(等重连重新唤醒)")
                else:
                    print("[唤醒] 未命中 → 继续待机", flush=True)
                continue
            # 流式 ASR:每轮全新上下文(与 recognize 语义一致),VAD 收集期间边收边识别
            asr_stream = StreamingAsr(asr)

            def _on_speech_start():
                """检测到本轮合法语音起始 → 才发 MIC_START(设备→LISTEN)。

                设计(2026-09-01): 非唤醒"说"完成(SPKE)后不主动续听 —— 设备回 IDLE 等待;
                服务器持续上传流上做 VAD,检测到下一轮合法语音输入起始后再发 MIC_START,
                设备才切换 LISTEN。唤醒应答场景除外(唤醒路径主动发,见 _mic_started)。
                """
                if self._wake_enabled and not self._mic_started:
                    self._send_mic_start("语音起始")
                    print("[下行] MIC_START(检测到本轮语音起始,设备→LISTEN)", flush=True)

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
                    on_start=_on_speech_start,
                )
            except TimeoutError:
                self._real_bytes_consumed = self.stream.real_bytes_total
                continue
            self._real_bytes_consumed = self.stream.real_bytes_total
            # ---- 段首"长停顿"治理(2026-09-02): 400ms 端点只在"开始说话后"生效,
            #      段首(起始检测模式)无停顿判断 —— 用户短提问前静止 5.5s 也进段。
            #      这里把 samples 裁剪到 [语音开始-300ms, 段尾](预卷保留防切字):
            #      录音/噪声判决/轮次日志/识别全部基于干净段。不放入 VAD 内核 ——
            #      on_chunk 的 600ms 块边界依赖完整 samples 坐标,裁剪会破坏流式对齐。
            seg_trimmed = False
            sss_val = endpoint.get("speech_start_seconds")
            if isinstance(sss_val, (int, float)) and sss_val > 0.3:
                from_pos = max(0, int(sss_val * 16000.0) - 4800)
                samples = samples[from_pos:]
                seg_trimmed = True
                print(f"[VAD] 段首惰性 {sss_val:.1f}s → 裁剪预卷(保留 300ms),"
                      f"段长 {len(samples) / 16000:.2f}s", flush=True)
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
            #      交互时刻刷新放到"噪声判决"之后 —— 噪声段不算交互(防噪声续命 600s) ----
            if self._wake_enabled and not endpoint.get("speech_started"):
                continue
            # ---- 前置语音检测(噪声不进入轮次): 能量 VAD 判"疑似开始"后,模型确认
            #      "这段有语音"才继续进 ASR/意图;非语音 → 整段跳过(无 MIC_STOP/无空播报/
            #      不刷新交互),噪声从源头不进轮。模型异常时自动放行(宁可多轮,不可漏话) ----
            if self._speech_detector is not None:
                ok_sp, sp_ratio, sp_ms = self._speech_detector.is_speech(samples)
                if not ok_sp:
                    self._nonspeech_count += 1
                    print(f"[语音检测] 段{round(len(samples) / 16000, 2)}s 非语音"
                          f"(占比={sp_ratio:.2f} {sp_ms:.0f}ms) → 跳过(不进轮)", flush=True)
                    self._remember(f"NONSPEECH {round(len(samples) / 16000, 2)}s")
                    continue
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
                # 流式 ASR:说话期间已逐块识别,这里只冲刷尾巴(不足 600ms 自动回退整段识别)。
                # 段被裁剪(前滚被裁)后流式块坐标已错位 → 一律走整段 recognize(干净窗);
                # 未裁剪的正常段保持原流式路径。
                cropped = self._crop_speech_window(samples, endpoint)
                if seg_trimmed or (cropped is not None and len(cropped) >= 1600):
                    asr_in = cropped if cropped is not None else samples
                    recognized, first_partial, asr_seconds = recognize(asr, asr_in)
                    recognized = (recognized or "").strip()
                    print(f"[识别] 第{turn}问({asr_seconds:.2f}s): "
                          f"干净语音窗 {len(asr_in) / 16000:.2f}s → {recognized or '[空]'}",
                          flush=True)
                else:
                    recognized, first_partial, asr_seconds = asr_stream.finish(samples)
                    recognized = (recognized or "").strip()
                    print(f"[识别] 第{turn}问({asr_seconds:.2f}s): {recognized or '[空]'}", flush=True)
            else:                            # <0.1s:无有效语音,直接兜底
                recognized, first_partial, asr_seconds = "", "", 0.0
                print(f"[识别] 第{turn}问: 音频<0.1s,无有效语音", flush=True)
            self._remember(f"Q{turn}: {recognized or '[空]'}")
            # ---- 段级"语音性"判决(外围补充层):不动能量 VAD 参数,只补"这段像不像人声" ----
            # 判据(voice.real.vad.noise_gate): ①时长异常(≥8s 或无端点) ②占空比≥0.9
            # ③识别文本过短(空或≤4字)。三者全中 → 噪声: 重锚 bg(他调好的估计器参数不动,
            # 只用 10% 分位重锚)/ 不刷新交互时刻 / 文本丢弃走空轮。
            # 计算为本地 numpy 统计(<1ms),不增加链路时延;ASR 流式早已并行出字。
            is_noise, active_ratio = self._judge_noise_segment(
                samples, endpoint, recognized, background_dbfs)
            if self._wake_enabled and not is_noise:
                self._awake_at = time.monotonic()          # 噪声段不算交互
            if is_noise and self._floor is not None:
                th = float(endpoint.get("vad_threshold_dbfs") or background_dbfs)
                n = len(samples) // 320
                rows = np.array(samples[:n * 320], dtype=np.int16).reshape(-1, 320)
                fr = np.array([rms_dbfs(row) for row in rows])
                new_bg = float(np.percentile(fr, 10))
                # 钳制到估计器语义范围(防残余帧/异常帧把锚点拉到 -120 级病态值)
                new_bg = min(max(new_bg, float(self._floor.cfg.get("floor_min_dbfs", -80.0))),
                             float(self._floor.cfg.get("floor_max_dbfs", -35.0)))
                log.warning("底噪判决: 噪声段(占比=%.0f%% 时长=%.1fs ASR=%s) → bg_t %.1f→%.1fdB",
                            active_ratio * 100, len(samples) / 16000,
                            "空" if not recognized else recognized[:12],
                            self._floor.bg(), new_bg)
                self._floor.reset(new_bg)
                if recognized:
                    recognized = ""                        # 噪声段文本丢弃,走空轮收尾
                    print("[识别] 噪声型轮次: 丢弃文本,走空轮收尾", flush=True)
            if is_noise:
                print(f"[判决] 第{turn}段判为噪声(时长={len(samples)/16000:.1f}s "
                      f"占比={active_ratio:.0f}%) → 不刷新交互/重锚底噪", flush=True)
                try:
                    save_wav(audio_dir / f"noise_{turn:03d}_{int(time.time())}.wav", samples)
                except Exception:
                    pass
            # ---- 观测: 全部轮次落盘(判决/调参依据,噪声与空轮此前无记录) ----
            self._append_row(self.run_dir / "voice_rounds_log.csv", {
                "time": datetime.now().isoformat(timespec="seconds"),
                "turn": turn,
                "duration_s": round(len(samples) / 16000, 2),
                "active_ratio": round(active_ratio, 3),
                "endpoint": bool(endpoint.get("endpoint_triggered")),
                "recognized": (recognized or "")[:80],
                "verdict": "noise" if is_noise
                           else ("empty" if not recognized else "speech"),
            })
            if not recognized:
                # 空识别(或无有效语音):不做任何语义内容,但按协议完成收尾,
                # 避免设备停在 THINKING —— "空播报":SPKS → 0.12s 静音 PCM → SPKE。
                # 非唤醒说完成不主动续听: MIC_START 等检测到下一轮合法语音起始再由
                # _on_speech_start 发(空轮本身已被检测为语音起始,该轮开始时已发过)。
                if self._wake_enabled:
                    self._send_empty_round(mic_start=False)
                    self._mic_started = False
                else:
                    self._send_empty_round(
                        mic_start=bool(self.r.get("mic_restart_after_answer", False)))
                if self._turn_first_frame_at is not None:
                    print(f"[链路] 第{turn}问(空轮): 首帧→收尾 "
                          f"{time.monotonic() - self._turn_first_frame_at:.2f}s", flush=True)
                self._turn_first_frame_at = None
                self.stream.reset_input_buffer()
                continue

            # ---- 意图决策层 v1(规则):睡眠/拒绝 → 收尾回话+结束会话(需重新唤醒);
            #      不插话(no_reply)两级判定: 单次"嗯嗯/知道了"无法断定是敷衍(可能只是应和/
            #      没观点要表达)→ 前 N-1 次仅"疑似":记录计数并按正常对话处理(LLM 回答);
            #      连续第 N 次才确认敷衍 → 结束会话(dismiss)。
            #      会话结束约束(重新唤醒): ① 600s 空闲超时 ② 设备断联重连 ③ 意图层轮次(睡眠/拒绝/敷衍超限) ----
            suspected_noreply = False
            decision = {"intent": "question", "matched": "", "inject": ""}
            if self._intent_enabled:
                dec = decide_intent(recognized, self._intent_cfg)
                intent = dec["intent"]
                # 长段"敷衍词"防护: 段长 ≥ no_reply_max_duration_s(默认2s) 时,≤4 字的
                # no_reply 不可能是敷衍(如 9.64s 噪声/长句被误识别成"嗯嗯")→ 按正常问答
                # 处理(不进敷衍计数,防"噪声 3 段误结束会话")
                ng = self.real_cfg.get("vad", {}).get("noise_gate") or {}
                if (intent == "no_reply"
                        and (len(samples) / 16000.0) >= float(
                            ng.get("no_reply_max_duration_s", 2.0) if isinstance(ng, dict)
                            else 2.0)):
                    print(f"[意图] no_reply 但段长 {len(samples)/16000.0:.1f}s ≥ 2s → 按 question 处理",
                          flush=True)
                    intent = "question"
                if intent in ("sleep", "decline"):
                    print(f"[意图] {intent} 命中({dec['matched']!r}): {recognized[:32]!r}", flush=True)
                    self._remember(f"{intent.upper()} {recognized}")
                    # 固件语义(2026-09-02 定稿): S4 收 dismiss/goodnight = 直接进 S5/S6,
                    # **无表达** —— 服务器不再播 ack、不再发 SPKS;幂等 MIC_STOP 已在
                    # VAD 端点后发送过(设备不会因此离开 S5/S6)。sleep_ack/decline_ack
                    # 配置保留但不再使用("晚安…"类回话改为 S4 语义直达)。
                    self._publish_intent_result(intent)
                    # 意图轮结束会话: 回待机,需重新说唤醒词(与 600s 空闲/断联同为会话结束约束)
                    self._awake = False
                    self._awake_at = 0.0
                    self._mic_started = False
                    self._noreply_streak = 0   # 会话级计数: 结束即清零(下次唤醒重新累计)
                    round_s = round(time.monotonic() - self._turn_first_frame_at, 2) \
                        if self._turn_first_frame_at is not None else None
                    self._turn_first_frame_at = None
                    self.stream.reset_input_buffer()
                    print(f"[意图] {intent} → 结束会话回待机(等下次唤醒词,无语音回话)"
                          + (f" · 首帧→收尾 {round_s}s" if round_s is not None else ""),
                          flush=True)
                    continue
                if intent == "no_reply":
                    self._noreply_streak += 1
                    print(f"[意图] no_reply 命中({dec['matched']!r}) 连续{self._noreply_streak}"
                          f"/{self._noreply_tolerance}轮", flush=True)
                    self._remember(f"NOREPLY {recognized}")
                    if self._noreply_streak < self._noreply_tolerance:
                        # 前 N-1 次(默认前2次)只能"疑似":单次"嗯嗯"可能是应和、可能只是
                        # 没观点要表达,无法断定是敷衍 → 仅保留计数与记录(NOREPLY 观测),
                        # 其余按正常对话处理: 落入下方 _answer_qa(LLM 结合上下文回答),
                        # 不空播报、不结束会话;计数不重置 —— 连续累计,第 N 次才确认。
                        suspected_noreply = True
                    else:
                        # 连续第 N 次(≥no_reply_tolerance_rounds,默认第3轮)确认敷衍 →
                        # 结束会话(dismiss),不再续听。
                        # 2026-09-02: 终止语义后不再发送 SPKS/空播报(设备直接进 S5,S5 无表达,
                        # 无需协议收尾 —— 设备 FSM 对 dismiss 会自行清理 listening/busy)。
                        print(f"[意图] no_reply 连续{self._noreply_streak}轮 ≥ {self._noreply_tolerance}"
                              f" → 结束会话(dismiss)", flush=True)
                        self._publish_intent_result("dismiss")
                        self._awake = False
                        self._awake_at = 0.0
                        self._mic_started = False
                        self._noreply_streak = 0   # 会话级计数: 结束即清零
                        self._turn_first_frame_at = None
                        self.stream.reset_input_buffer()
                        print("[意图] no_reply 超限 → 回待机(等下次唤醒词)", flush=True)
                        continue
                # question(共情功能已移除,负面情绪归此)与"no_reply 疑似未确认"轮:
                # 均走正常问答 + 常驻状态 normal;question 会打断连续敷衍计数(重置),
                # 疑似 no_reply 轮保留计数(连续多次才累计为敷衍)。
                if not suspected_noreply:
                    self._noreply_streak = 0
                print("[意图] " + ("no_reply 疑似(未确认) → 按正常对话处理" if suspected_noreply
                                   else "question → 正常问答"), flush=True)
                self._publish_intent_result("normal")

            self._endpoint_wall = t_endpoint      # 供 TTS 首块计时
            self._first_spks_at = None
            try:
                answered = self._answer_qa(
                    turn, recognized, tokenizer, llm, streamer_cls=TextIteratorStreamer,
                    sentence_chunks=self._sentence_chunks, queue=queue, tts_dir=tts_dir,
                    tts_base=tts_base, result_csv=result_csv,
                    asr_seconds=asr_seconds, input_wav=str(input_wav),
                    endpoint_wall=t_endpoint, llm_inject=decision.get("inject", ""),
                )
                self.last_result = answered
            except Exception:
                # 单轮异常保护:引擎线程常驻,异常只结束本轮并强制 SPKE 收尾
                log.exception("第%d轮问答异常", turn)
                self.last_result = {"turn": turn, "error": "exception"}
                if self._board_spks_active:
                    self._push_text("SPKE")
                    self._board_spks_active = False
                    self._playback_end(0.3)   # 半双工
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

    def _judge_noise_segment(self, samples, endpoint, recognized, background_dbfs):
        """段级"语音性"判决(外围补充层,不动能量 VAD 参数)。

        判据(全可配 `voice.real.vad.noise_gate`,默认保守,只判"明显的噪声段"):
          ① 时长异常: 段长 ≥ min_duration_s(8s) 或 无端点(endpoint_triggered=False);
          ② 占空比:   段内帧电平超阈值占比 ≥ active_ratio(0.9);
          ③ 识别过短: 归一化文本为空 或 ≤ max_chars(4) 字。
        三者同时命中 → 判噪声段。设计取舍(信"前同学参数",只补必要层):
          - 不动 VAD 起点/端点/penalty/滑窗等一系列已调参规则;
          - 有字(>4 字)的段一律视为说话,哪怕形态像噪声(混合轮保留,对应
            "0.977 < 0.98 不丢文本"的教训);
          - 纯本地 numpy 统计(<1ms),零新依赖,不增加链路时延。
        返回 (is_noise: bool, active_ratio: float)。
        """
        gate = self.real_cfg.get("vad", {}).get("noise_gate") or {}
        gate = gate if isinstance(gate, dict) else {}
        if not gate.get("enabled", False):
            return False, 0.0
        import numpy as np
        from board_serial_asr_test import rms_dbfs
        duration_s = len(samples) / 16000.0
        n = len(samples) // 320
        active_ratio = 0.0
        if n > 0:
            rows = np.array(samples[:n * 320], dtype=np.int16).reshape(-1, 320)
            fr = np.array([rms_dbfs(row) for row in rows])
            th = float(endpoint.get("vad_threshold_dbfs") or background_dbfs)
            active_ratio = float(np.mean(fr > th))
        min_dur = float(gate.get("min_duration_s", 8.0))
        too_long_or_no_endpoint = (duration_s >= min_dur
                                   or not endpoint.get("endpoint_triggered"))
        too_short_text = len(_normalize_wake(recognized or "")) <= int(gate.get("max_chars", 4))
        is_noise = (min_dur > 0 and too_long_or_no_endpoint
                    and active_ratio >= float(gate.get("active_ratio", 0.9))
                    and too_short_text)
        return bool(is_noise), round(active_ratio, 3)

    @staticmethod
    def _crop_speech_window(samples, endpoint, sr=16000, pre_roll_ms=300,
                            tail_pad_ms=300, tail_inert_ms=600):
        """段窗口裁剪: 短提问未被及时"截断"时,段内混入大量静默 → 整段 ASR 判空。

        利用 VAD 诊断已给出的语音窗口(speech_start_seconds/last_active_seconds),
        裁剪到 [语音开始-300ms, 最后活跃+300ms];仅当"惰性前缀 > 300ms"或
        "惰性尾 > 600ms"时才裁剪(正常段不动,保持既有流式识别路径)。
        返回裁剪后样本;无需裁剪/窗口不可用返回 None。
        """
        import numpy as np
        sss = endpoint.get("speech_start_seconds")
        las = endpoint.get("last_active_seconds")
        if not (isinstance(sss, (int, float)) and isinstance(las, (int, float))
                and 0 <= sss < las):
            return None
        a = max(0, int(sss * sr) - int(pre_roll_ms / 1000.0 * sr))
        b = min(len(samples), int(las * sr) + int(tail_pad_ms / 1000.0 * sr))
        inert_prefix = a
        inert_tail = len(samples) - b
        if (inert_prefix > int(0.3 * sr) or inert_tail > int(tail_inert_ms / 1000.0 * sr)) \
                and (b - a) >= int(0.3 * sr):
            return samples[a:b]
        return None

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
                   asr_seconds, input_wav, endpoint_wall, llm_inject=""):
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
        # 意图层动态注入(共情等):临时附加到 system 提示,不污染基础提示词
        if llm_inject and llm_inject.strip():
            system_prompt = (system_prompt + " " + llm_inject).strip()
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
                segment_queue.put((request_id, stream_dir, text))

            for sentence in sentence_chunks(streamer, fast_cut=fast_cut):
                if self._interrupt_hit.is_set():
                    break                    # 用户抢话:停止生成/切句/合成
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
        interrupted = False
        interrupt_text = ""
        while True:
            if self._interrupt_hit.is_set():
                if self._board_spks_active:
                    # 播放中命中:由 _stream_tts_segment 打断出口完成 SPKE,这里收尾
                    interrupted = True
                    interrupt_text = (self._detector.hit_text
                                      if self._detector is not None else "") or interrupt_text
                    self._interrupt_hit.clear()   # 消费本命中(避免"残留命中"杀死后续回答)
                    print("[打断] 回答被用户抢话中断 → 停止播放,进 LISTEN", flush=True)
                else:
                    # 残留命中(命中晚于段尾收尾):播放已完成,不算打断,不重复发命令
                    self._interrupt_hit.clear()
                    print("[打断] 残留命中(已播完) → 忽略", flush=True)
                break
            try:
                item = segment_queue.get(timeout=0.5)
            except thread_queue.Empty:
                continue                     # queue.Empty(threading) → import 包装
            if item is None:
                break
            request_id, stream_dir, seg_text = item
            seg_index += 1
            seg_started = time.monotonic()
            tts = self._stream_tts_segment(
                queue, request_id, stream_dir,
                start_board=(not board_started or not last_ok), end_board=False,
                play_text=seg_text)
            board_started = True
            last_ok = bool(tts.get("ok", False))
            if tts.get("error") == "interrupted":
                interrupted = True
                interrupt_text = str(tts.get("interrupt_text") or "") or interrupt_text
                print("[打断] 用户抢话(段内) → 停止播放"
                      + (f",接住文本: {interrupt_text[:32]!r}" if interrupt_text else ""),
                      flush=True)
                break
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
            self._playback_end(0.6)   # 半双工: 播报回声余震期不听
            print("[下行] 全部段落播完 · SPKE", flush=True)
        # 连续对话模式(协议多轮要求):SPKE 后重发 MIC_START,设备重新进入 LISTENING
        # 继续下一轮;关闭时设备回 IDLE 等本地唤醒。
        # 在线唤醒·持续对话(wake.enabled): 同样在 SPKE 后发 MIC_START 续听 —— 唤醒一次后
        # 不再要求重新说唤醒词,直到空闲超时由引擎回待机。
        # 打断(flexible)或正常续听都发 MIC_START(播放循环中断出口已确保 SPKE 先行)。
        # 非唤醒"说"完成(SPKE 后): 不主动发 MIC_START —— 设备回 IDLE 等待;
        # 服务器在持续上传流上 VAD,检测到下一轮合法语音起始时由 _on_speech_start 发
        # MIC_START(设备→LISTEN)。唤醒应答场景除外(唤醒路径主动发,见 _mic_started=True)。
        # v2 插话接住: 有插话文本时把文本交给主循环先作答(作答完成后同样不主动续听)。
        if self._wake_enabled:
            self._mic_started = False
            if interrupted and interrupt_text and not self._pending_interrupt_text:
                self._pending_interrupt_text = interrupt_text.strip()
                print(f"[打断] 插话文本待主循环接住作答: {interrupt_text[:40]!r}", flush=True)
        else:
            if interrupted or self.r.get("mic_restart_after_answer", False):
                self._send_mic_start("打断进LISTEN" if interrupted else "续听")
                print("[下行] MIC_START(打断进 LISTEN)" if interrupted
                      else "[下行] MIC_START(连续对话模式,设备→LISTENING)", flush=True)

        response = "".join(response_pieces).strip()
        self._remember(f"A{turn}: {response[:80]}")
        # 多轮历史入库(供下一轮 context);断联后会话已终止(历史被清),
        # 旧轮次不再写入新会话 —— 避免重连后上下文残留上一会话内容
        if self._online:
            self._history.append({"role": "user", "content": user_text})
            self._history.append({"role": "assistant", "content": response})
        total_sec = round(time.perf_counter() - started, 3)
        print(f"[计时] 第{turn}问: 端点→播完全部回答 {total_sec:.1f}s (LLM+合成 {seg_index} 段, 首字 {first_token_seconds}s, "
              f"首块音频 {tts_wall}s)", flush=True)
        row = {
            "time": _dt.now().isoformat(timespec="seconds"), "turn": turn,
            "recognized_text": user_text, "qwen_response": response,
            "asr_seconds": round(asr_seconds, 3),
            # 打断接住轮(无 input_wav)没有真实 VAD 端点,计时字段留空(端点为虚拟时刻)
            "endpoint_to_first_token_seconds": None if not input_wav else first_token_seconds,
            "endpoint_to_first_audio_seconds": None if not input_wav else tts_wall,
            "total_seconds": total_sec,
            "segments": seg_index, "input_wav": input_wav,
            "interrupted": bool(interrupted), "interrupt_text": interrupt_text,
        }
        self._append_row(result_csv, row)
        return row

    # ---------------- 播放期抢话检测(零依赖语义回放免疫) ----------------

    def _playback_begin(self, text):
        """SPKS 发出前调用:登记播放文本并启动抢话判别线程(若有文本且开启)。"""
        self._interrupt_hit.clear()
        if self._detector is not None:
            self._detector.stop()
            self._detector = None
        if self._interrupt_enabled and self._asr_model is not None and str(text or "").strip():
            self._detector = _InterruptDetector(
                self.stream, self._asr_model, text, self._interrupt_hit,
                self._interrupt_min_chars, self._interrupt_min_similarity)
            self._detector.start()

    def _playback_end(self, safety_s=0.6):
        """SPKE 发出后调用:等待判别线程真正退出(≤1s,read 有 0.25s 超时)
        + 半双工余震窗口(该时刻前不听)。保证播放期结束后流上只有一个读者(主循环)。"""
        if self._detector is not None:
            d = self._detector
            self._detector = None
            d.stop()
            d.join(timeout=1.0)
        self._playback_until = time.monotonic() + safety_s

    def _publish_intent_result(self, intent):
        """固件侧语义契约(2026-09-01 约定):仅对有状态语义的意图发 MQTT JSON 到 vcmd topic。

        配置 voice.real.intent.intent_result = {意图: 消息值};空字符串/未配置 = 不发送。
          {"type":"intent_result","intent":"goodnight"}  → 用户说晚安,固件切睡眠状态
          {"type":"intent_result","intent":"dismiss"}    → 用户结束对话,固件退出对话
        纯文本命令保持兼容;其余固件状态收到会忽略。发布失败仅告警,不影响语音链路。
        """
        if self.mqtt is None:
            return
        rules = self._intent_cfg.get("intent_result")
        rules = rules if isinstance(rules, dict) else {}
        value = str(rules.get(intent, "") or "")
        if not value:
            return
        payload = json.dumps({"type": "intent_result", "intent": value,
                              "interaction_id": self._session_interaction_id or ""},
                             ensure_ascii=False)
        try:
            self.mqtt.send_vcmd(payload)
            log.info("MQTT -> vcmd: %s", payload)
        except Exception as exc:
            log.warning("意图结果 MQTT 发布失败(%s): %r", intent, exc)

    def _send_empty_round(self, mic_start=True):
        """空播报收尾:SPKS → 0.12s 静音 PCM → SPKE(设备 THINKING→IDLE,绝不卡状态)。

        mic_start=True  → 空轮后发 MIC_START 续听(不插话/正常空轮);
        mic_start=False → 空轮后保持(结束会话路径,由调用方负责回待机)。
        """
        print("[下行] 空播报收尾(SPKS+静音+SPKE)", flush=True)
        self._push_text("SPKS 24000")
        silence = b"\x00\x00" * 2880          # 2880 样本 = 0.12s @24kHz, 偶数字节
        # 按实时节奏推(5 帧 × 25ms ≈ 125ms):旧实现瞬时推完 → SPKS/SPKE 同毫秒,
        # 设备"刚开播即停"状态抖动(UI 闪 speak),转出对不上;按节奏能让设备
        # 真实经历 0.12s 静音(与协议字面一致,状态转换平顺)
        frame_sec = (1200 / 2) / 24000.0
        for off in range(0, len(silence), 1200):
            self._push_pcm(silence[off:off + 1200])
            time.sleep(frame_sec)
        self._push_text("SPKE")
        self._playback_end(0.3)               # 半双工: 零声播报后短暂不听
        if mic_start:
            self._send_mic_start("空轮续听")
            print("[下行] MIC_START(空轮后继续下一轮)", flush=True)

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
        self._playback_begin(text)                 # 应答也是播放:进行中可被用户抢话打断
        self._push_text(f"SPKS {rate}")
        self._board_spks_active = True
        # 逐帧匀速(≈1.0×实时):旧实现 sleep(0.02)=1.25×实时,同样会把设备播放
        # FIFO 越喂越满 → ERROR playback_overflow(与问答段同一机理,一并修复)
        frame_sec = (1200 / 2) / rate
        for i, off in enumerate(range(0, len(pcm), 1200)):
            if self._interrupt_hit.is_set():
                self._interrupt_hit.clear()       # 消费本命中(防残留命中杀死后续回答)
                # 保留判别线程的插话文本(用户在唤醒回应期间抢话 = 实际话语开始)
                hit_text = (self._detector.hit_text if self._detector is not None else "")
                self._push_text("SPKE")
                self._board_spks_active = False
                self._playback_end(0.3)
                print("[打断] 唤醒应答被用户抢话 → 停止应答,进 LISTEN", flush=True)
                return False, hit_text or ""
            self._push_pcm(pcm[off:off + 1200])
            time.sleep(0.012 if i < 6 else frame_sec)   # 前 6 帧(150ms)快速填充,防起步欠载
        self._push_text("SPKE")
        self._board_spks_active = False
        self._playback_end(0.6)                     # 半双工: 应答回声余震期不听
        print(f"[下行] 唤醒应答播报完成: {text[:24]} ({len(pcm) / 2 / rate:.1f}s)", flush=True)
        return True, ""

    def _stream_tts_segment(self, queue, request_id, stream_dir, start_board=True,
                            end_board=True, play_text=""):
        """把 TTS 子进程合成的一段(S 句)流式下发 WSS:SPKS <rate> + PCM 帧(≤1200B) + SPKE。

        与 TTS 子进程的通信协议(全是落盘文件,无共享内存):
          主进程等待 stream_dir/chunk_N.pcm 出现 → 读取后删除 → N+1;
          子进程最终写 queue/response_<id>.json {ok, stream_chunks, ...} 表示整段完成。

        下行协议细节(与固件 PROTOCOL.md 一致):
          - SPKS <rate>:开播命令,必须在任何 PCM 之前发出(固件没收到 SPKS 会丢弃 PCM);
          - PCM:每帧 ≤1200B(约 25ms @24kHz),**逐帧匀速 ≈1.0×实时** 下发
            (前 150ms 快速填充给设备开播余量):旧实现"整块突发 + 0.88 系数"
            = 1.14×实时累积 → 设备播放 FIFO 溢出(ERROR playback_overflow,
            实测每次真实回答必现) —— 已修复;
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
                    self._playback_begin(play_text)       # 登记播放文本 + 启动抢话判别
                    self._push_text(f"SPKS {rate}")
                    self._board_spks_active = True
                    if self._first_spks_at is None:
                        self._first_spks_at = time.monotonic()
                if self._interrupt_hit.is_set():
                    # 用户抢话:立即停播(SPKE),设备收 MIC_START 后停止扬声器。
                    # v2 打断接住: 带回判别线程保留的插话文本,由 _answer_qa 直接作答(用户无需重说)。
                    interrupt_text = (self._detector.hit_text
                                      if self._detector is not None else "")
                    self._interrupt_hit.clear()          # 消费本命中,防"残留命中"杀死后续回答
                    self._push_text("SPKE")
                    self._board_spks_active = False
                    self._playback_end(0.3)
                    print("[打断] 用户抢话 → 停止播放(SPKE 提前),等待进入 LISTEN", flush=True)
                    return {"ok": False, "error": "interrupted",
                            "interrupt_text": interrupt_text or ""}
                if self.sink_pcm:
                    data = pcm if len(pcm) % 2 == 0 else pcm[:-1]
                    # 逐帧匀速(≈1.0×实时)推流:旧实现"整块突发 + sleep(0.88×时长)"
                    # = 突发灌入 + 1.14×实时累积 → 设备播放 FIFO 溢出,实测每次真实
                    # 回答必报 ERROR playback_overflow(SPKS 后 0.9~4s)。匀速后设备
                    # 缓冲恒定不涨;仅前 150ms 快速填充(设备开播缓冲余量,防起步欠载)。
                    frame_sec = (1200 / 2) / rate          # 每帧(≤1200B)时长,如 25ms @24kHz
                    for i, off in enumerate(range(0, len(data), 1200)):
                        self._push_pcm(data[off:off + 1200])
                        time.sleep(0.012 if i < 6 else frame_sec)
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
                    self._playback_end(0.6)   # 半双工: 段末独播收尾
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
            self._playback_end(0.6)   # 半双工
            log.warning("TTS 段超时,已发 SPKE: %s", request_id)
        else:
            log.warning("TTS 段超时: %s", request_id)
        return {"ok": False, "error": "stream timeout"}

    # ---------------- 工具 ----------------

    def _push_text(self, text):
        if text == "SPKE":
            self._last_spke_at = time.monotonic()   # MIC_START 与上一 SPKE 最小间隔用
        if self.sink_text:
            self.sink_text(text)

    def _send_mic_start(self, tag=""):
        """MIC_START 统一出口:与上一 SPKE 强制最小间隔(默认 0.3s,可配 mic_start_min_delay_s)。

        实测(10:21:41,628)SPKE 与 MIC_START 同毫秒时,设备扬声器 64KB 缓冲 + I2S 尾音
        尚未排空 → 固件判定"仍在播放"→ 投递 EVT_INTERRUPT 而非 EVT_WAKEUP
        (S6 不接受 EVT_INTERRUPT)→ 唤醒状态切换丢失、S6 不跳转。此间隔在设备排空
        缓冲后 MIC_START 才会被识别为"唤醒/进入听音"。
        """
        gap = float(self.r.get("mic_start_min_delay_s", 0.3))
        wait = gap - (time.monotonic() - self._last_spke_at)
        if wait > 0:
            time.sleep(wait)
        self._push_text("MIC_START")
        self._mic_started = True
        if tag:
            self._remember(f"MIC_START({tag})")

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
