# -*- coding: utf-8 -*-
# ============================================================================
# ASR/VAD 模块(原:板卡串口 ASR 测试件) —— 语音前端处理公共件
# ----------------------------------------------------------------------------
# 职责:把"一段语音"变成"一段文本"之前的所有信号处理 + 识别入口:
#   ① 帧解析   read_frame()        — 串口/RingBuffer 字节流里抠出 20ms PCM 帧(带帧头校验)
#   ② 能量 VAD capture_until_endpoint() — 判断"是否开始说话/是否说完"(纯信号处理,无模型)
#   ③ 识别     recognize()         — 600ms 块级增量流式识别(调 FunASR model.generate)
#   ④ 工具     rms_dbfs()/save_wav()/capture_seconds()/GpuPeakMonitor ...
# 兼容两种输入源(接口一致):
#   - 真机 ESP32-S3 串口(pyserial):open_serial() 拿到 ser 参数;
#   - 网络服务器 WSS 流(ring_buffer.RingBuffer):实现了 read()/reset_input_buffer() 等
#     serial 同形接口,本文件所有函数无需改动即可复用 —— 这是本包完整链路的关键适配点。
# 调用方: server/real_engine.py(服务器 real 模式)、voice_daemon.py(本地 V5)。
# ============================================================================
import argparse
from collections import deque
import csv
import math
import struct
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import serial
import serial.tools.list_ports

from asr_eval_core import cer, extract_text, gpu_snapshot, load_paraformer


SENTENCES = [
    "请开始进行语音讲解。",
    "停止讲解。",
    "请拍照识别这个文物。",
    "请返回主界面。",
    "请介绍这个文物的历史背景。",
    "这个展品是什么年代的？",
    "请告诉我它的主要用途。",
    "这个文物有什么文化价值？",
]


def rms_dbfs(samples):
    """把 int16 音频样本算成平均音量(分贝满刻度 dBFS)。

    输入: int16 numpy 数组(20ms 帧或任意长度);输出: dBFS 浮点,如 -60.0(安静)~ -20.0(说话)。
    计算: 归一化到 [-1,1] → RMS → 20*log10(RMS);全零/空输入返回 -120.0 保底。
    说明: 这是能量 VAD 的唯一依据 —— 阈值都是"背景 dBFS + 偏移量"的形式。
    """
    if len(samples) == 0:
        return -120.0
    x = samples.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(np.square(x))))
    if rms <= 1e-12:
        return -120.0
    return 20.0 * math.log10(rms)


def active_voice_dbfs(samples, background_dbfs, sr=16000, frame_ms=20, threshold_above_bg=3.0):
    """统计段内"活跃语音"的平均电平和活跃帧占比(供结果记录/SNR 评估用,非 VAD 主逻辑)。

    按 20ms 分帧,帧电平 > 背景+threshold_above_bg 的帧视为活跃帧;
    返回: (活跃帧拼接后的 RMS dBFS, 活跃帧数/总帧数)。
    """
    frame_len = max(1, int(sr * frame_ms / 1000))
    threshold = background_dbfs + threshold_above_bg
    active = []
    frame_count = 0
    active_count = 0
    for start in range(0, len(samples), frame_len):
        frame = samples[start : start + frame_len]
        if len(frame) == 0:
            continue
        frame_count += 1
        if rms_dbfs(frame) > threshold:
            active.append(frame)
            active_count += 1
    active_samples = np.concatenate(active) if active else np.array([], dtype=np.int16)
    return rms_dbfs(active_samples), (active_count / frame_count if frame_count else 0.0)


class NoiseFloorTracker:
    """动态底噪估计器(方案:双时间尺度,把固定背景电平换成随环境变化的 bg_t)。

    设计要点:
      - fast 窗(fast_window_s,默认1.5s):最近*全部真实帧*的 percentile 分位 → 环境变吵快速跟随;
        关键:不套 gate(修正 B)—— 风扇/空调噪声常比底噪高 15~20dB,若按"低于 bg_t+gate_db"
        过滤则噪声帧永远进不了估计器,底噪永不上升;
      - slow 窗(slow_window_s,默认8s):最近"低于 bg_t+gate_db"的候选帧分位 → 稳定跟踪与回落;
      - 语音污染防护:仅 speech_started=False(每轮预语音段)时帧才入 ring 且才允许更新;
        speech_started 置位后本轮冻结 —— 语音结构性进不了估计器;
      - 上升需连续 rise_confirm_updates 次确认 + up_max_db_per_s 限速;下降 down_max_db_per_s
        限速(慢),避免语音间隙被当噪声;整体钳制在 [floor_min_dbfs, floor_max_dbfs]。

    用法(与 VAD 状态机解耦,本地串口 V5 链路亦可复用):
      tracker = NoiseFloorTracker(init_bg_dbfs, cfg_dict)
      for frame in ...:
          tracker.on_frame(rms_dbfs(frame), speech_started)
      threshold = tracker.bg() + offset        # 每帧阈值
      tracker.reset(bg)                        # 会话内成功校准后重设初值
      tracker.diagnostics()                    # bg_initial/final/bg_trajectory...
    """

    def __init__(self, init_bg_dbfs, cfg=None):
        cfg = cfg or {}
        self.cfg = {
            "fast_window_s": float(cfg.get("fast_window_s", 1.5)),
            "slow_window_s": float(cfg.get("slow_window_s", 8.0)),
            "percentile": float(cfg.get("percentile", 10.0)),
            "update_interval_s": float(cfg.get("update_interval_s", 0.5)),
            "up_max_db_per_s": float(cfg.get("up_max_db_per_s", 3.0)),
            "down_max_db_per_s": float(cfg.get("down_max_db_per_s", 0.5)),
            "gate_db": float(cfg.get("gate_db", 12.0)),
            "rise_trigger_db": float(cfg.get("rise_trigger_db", 1.0)),
            "rise_confirm_updates": int(cfg.get("rise_confirm_updates", 2)),
            "snap_trigger_db": float(cfg.get("snap_trigger_db", 6.0)),
            "fast_min_frames": int(cfg.get("fast_min_frames", 20)),
            "slow_min_frames": int(cfg.get("slow_min_frames", 50)),
            "floor_min_dbfs": float(cfg.get("floor_min_dbfs", -80.0)),
            "floor_max_dbfs": float(cfg.get("floor_max_dbfs", -35.0)),
        }
        self._bg = float(init_bg_dbfs)
        self._bg_initial = float(init_bg_dbfs)
        self._fast = deque()            # (ts, dbfs):最近 fast_window_s 的真实帧(不套 gate)
        self._slow = deque()            # (ts, dbfs):最近 slow_window_s 的低电平候选帧(套 gate)
        self._last_update = None
        self._rise_pending = 0
        self._traj = deque(maxlen=1200)  # bg_trajectory 环形保留(防长会话内存膨胀)

    def reset(self, bg_dbfs):
        """重校准:更新初值并清空全部状态(引擎 _calibrate_background 成功后调用)。"""
        self._bg = float(bg_dbfs)
        self._bg_initial = float(bg_dbfs)
        self._fast.clear()
        self._slow.clear()
        self._last_update = None
        self._rise_pending = 0
        self._traj.clear()

    def bg(self):
        """当前底噪估计(dBFS);VAD 每帧阈值 = bg() + offset。"""
        return self._bg

    def on_frame(self, frame_dbfs, speech_started=False, ts=None):
        """每 20ms 帧调用一次(仅预语音段生效;进入语音后本轮冻结)。

        frame_dbfs: 该帧服务器侧 rms_dbfs;speech_started: VAD 是否已判"开始说话"。
        ts: 时间戳(秒),默认 time.monotonic();调试/回放校验时可注入模拟时间。
        -120dB(合成静音帧/无真实数据)直接忽略,避免把 RingBuffer 补的静音当成低噪声。
        """
        if frame_dbfs <= -100.0:
            return                                    # 合成静音帧或静默:无环境信息
        if speech_started:
            return                                    # 冻结:语音帧结构性排除
        ts = time.monotonic() if ts is None else float(ts)
        self._fast.append((ts, frame_dbfs))
        if frame_dbfs < self._bg + self.cfg["gate_db"]:
            self._slow.append((ts, frame_dbfs))
        if self._last_update is None or ts - self._last_update >= self.cfg["update_interval_s"]:
            self._update(ts)

    def _update(self, ts):
        c = self.cfg
        prev_update = self._last_update
        self._last_update = ts
        # 确认计数跨冻结期失效:距上次更新超过 2.5 个更新周期(回合已被语音冻结/隔断),
        # 清零重来,保持"连续 2 次"的语义
        if prev_update is not None and ts - prev_update > c["update_interval_s"] * 2.5:
            self._rise_pending = 0
        self._prune(ts)
        fast = [d for _, d in self._fast]
        slow = [d for _, d in self._slow]
        p_fast = (float(np.percentile(fast, c["percentile"]))
                  if len(fast) >= c["fast_min_frames"] else None)
        p_slow = (float(np.percentile(slow, c["percentile"]))
                  if len(slow) >= c["slow_min_frames"] else None)
        reason = "none"
        if p_fast is not None and p_fast > self._bg + c["rise_trigger_db"]:
            # 上升:连续 2 次确认后进入"持续上升"状态(不重置计数),此后每个周期按限速
            # 继续抬升,直到条件不再满足(由 else 清零);限速防语音污染推动过快
            self._rise_pending += 1
            if self._rise_pending >= c["rise_confirm_updates"]:
                step = min(p_fast - self._bg, c["up_max_db_per_s"] * c["update_interval_s"])
                self._bg = min(max(self._bg + step, c["floor_min_dbfs"]), c["floor_max_dbfs"])
                reason = "rise"
        elif p_slow is not None and p_slow < self._bg:
            # 下降:慢速回落(下降信号可能来自语音间隙/换气,限速保护);
            # snap 静音快速回落:分位显著低于 bg_t(关风机/人员散去等大落差)时直接重锚 ——
            # 0.5dB/s 慢速在"噪声刚停 1~2s 即说轻声"的场景会漏检(数十秒追不上);
            # 仅在预语音段运行(语音期冻结),且以 8s 分位为锚,不会因语音间隙误触
            self._rise_pending = 0
            if c["snap_trigger_db"] > 0 and p_slow < self._bg - c["snap_trigger_db"]:
                self._bg = max(p_slow, c["floor_min_dbfs"])
                reason = "snap"
            else:
                step = min(self._bg - p_slow, c["down_max_db_per_s"] * c["update_interval_s"])
                self._bg = min(max(self._bg - step, c["floor_min_dbfs"]), c["floor_max_dbfs"])
                reason = "fall"
        else:
            self._rise_pending = 0
        self._traj.append({
            "t": round(ts, 3),
            "bg": round(self._bg, 2),
            "p_fast": round(p_fast, 2) if p_fast is not None else None,
            "p_slow": round(p_slow, 2) if p_slow is not None else None,
            "fast_n": len(fast),
            "slow_n": len(slow),
            "reason": reason,
        })

    def _prune(self, ts):
        """按真实时间窗清理(ring 存时间戳而非 maxlen —— 窗口语义是"最近 N 秒")。"""
        c = self.cfg
        while self._fast and ts - self._fast[0][0] > c["fast_window_s"]:
            self._fast.popleft()
        while self._slow and ts - self._slow[0][0] > c["slow_window_s"]:
            self._slow.popleft()

    def diagnostics(self):
        return {
            "dynamic_floor_enabled": True,
            "bg_initial_dbfs": round(self._bg_initial, 2),
            "bg_final_dbfs": round(self._bg, 2),
            "bg_trajectory": list(self._traj),
        }


def checksum8(data):
    """字节和校验(单字节):帧头 sum8 字段与 payload 的一致性校验。"""
    return sum(data) & 0xFF


def list_ports():
    """列出本机全部串口设备名(如 ['COM5', 'COM7'])。"""
    return [p.device for p in serial.tools.list_ports.comports()]


def open_serial(port, baud):
    """打开开发板串口(read_timeout=2s:read_frame 读不到帧时抛 TimeoutError)。"""
    return serial.Serial(port=port, baudrate=baud, timeout=2)


def discard_buffered_audio(ser, settle_seconds=0.15):
    """清零串口接收缓冲(录音前调用),避免把上一次的残留帧混进本次采集。

    先清一次 → 等 settle 秒(让仍在传输的帧到达并被丢弃)→ 再清一次。
    """
    ser.reset_input_buffer()
    time.sleep(settle_seconds)
    ser.reset_input_buffer()


def read_exact(ser, n):
    """从串口/兼容流读取恰好 n 字节;超时读到少于 n 字节即抛 TimeoutError(整帧放弃重对齐)。"""
    data = bytearray()
    while len(data) < n:
        chunk = ser.read(n - len(data))
        if not chunk:
            raise TimeoutError("serial read timeout")
        data.extend(chunk)
    return bytes(data)


def read_frame(ser):
    """从字节流中解析出一个完整的 20ms PCM 音频帧。

    板卡每 20ms 发一包,总长 656 字节,格式:
      "PCM1"(4B 魔数) + seq(4B 小端序号) + payload_len(2B,恒 640)
      + dbfs_x100(2B,板卡侧音量,单位 0.01dB) + 保留(3B) + sum8(1B, payload 字节和)
      + payload(640B = 320 个 int16 样本 @16kHz = 20ms)

    算法:
      1) 单字节滑动窗口找魔数 "PCM1"(串口无边界,可能从包中间开始读;
         窗口只保留最近 4 字节,找到对齐点才继续);
      2) 读头部剩余 12 字节,解出 seq/payload_len/dbfs;
      3) 读 payload_len 字节音频,校验 sum8 —— 校验不过说明帧损坏,丢弃并回到步骤 1
         (不抛异常、不断链,等待下一帧重新对齐);
      4) int16 样本复制为独立数组(避免与底层缓冲共享内存)。
    返回: (seq, dbfs_x100/100.0, samples int16 数组);超时无数据抛 TimeoutError。
    """
    magic = b"PCM1"
    window = bytearray()
    while True:
        b = ser.read(1)
        if not b:
            raise TimeoutError("waiting for PCM1 frame")
        window.extend(b)
        if len(window) > 4:
            del window[0]
        if bytes(window) == magic:
            rest = read_exact(ser, 12)
            header = magic + rest
            seq = struct.unpack_from("<I", header, 4)[0]
            payload_len = struct.unpack_from("<H", header, 8)[0]
            dbfs_x100 = struct.unpack_from("<h", header, 10)[0]
            expected_sum = header[15]
            payload = read_exact(ser, payload_len)
            if checksum8(payload) != expected_sum:
                continue
            samples = np.frombuffer(payload, dtype="<i2").copy()
            return seq, dbfs_x100 / 100.0, samples


def capture_seconds(ser, seconds, sr=16000):
    """连续采集指定秒数的音频(用于开机/自检时的背景校准)。

    循环 read_frame 直到攒够 target 样本数;总时长超过 seconds+10s 兜底退出(防挂死)。
    返回: (拼接后的 int16 数组, 每帧板卡侧 dBFS 列表)。"""
    samples = []
    dbfs_frames = []
    target = int(seconds * sr)
    start = time.perf_counter()
    while sum(len(x) for x in samples) < target:
        _, dbfs, frame = read_frame(ser)
        samples.append(frame)
        dbfs_frames.append(dbfs)
        if time.perf_counter() - start > seconds + 10:
            break
    if not samples:
        return np.array([], dtype=np.int16), []
    return np.concatenate(samples)[:target], dbfs_frames


def capture_until_endpoint(
    ser,
    max_seconds,
    background_dbfs,
    endpoint_silence_ms,
    threshold_above_bg=6.0,
    endpoint_threshold_above_bg=None,
    endpoint_active_penalty=4.0,
    voice_start_ms=100,
    voice_start_window_ms=None,
    sr=16000,
    on_chunk=None,
    floor_tracker=None,
    stop_event=None,
):
    """能量 VAD 主函数:边收帧边判定"人开始说话了吗 / 人说完了吗",输出整段语音。

    参数(除 ser 外全部可调,对应 config.json voice_control/modes):
      max_seconds                   兜底上限:超过就截断(防一直说/环境噪声一直响)
      background_dbfs               开机校准得到的背景电平,如 -45.3dB
      endpoint_silence_ms           说完后沉默多久判为"说完了"(默认 1000ms,对话框 800ms)
      threshold_above_bg            起始阈值 = 背景 + 该偏移(如 +3dB)
      endpoint_threshold_above_bg   端点阈值(= 起始阈值;说话中停顿低于此算"静音帧")
      endpoint_active_penalty       活跃帧对沉默计数的抵扣倍率(默认 4:1ms 活跃抵 4ms 沉默)
      voice_start_ms                起始滑动窗内活跃样本需达到的毫秒数(防单次尖峰误触发)
      voice_start_window_ms         起始滑窗长度(默认=voice_start_ms;检测到说话时录音起点回退
                                    到窗口起点,保证第一个字不被切掉 —— 关键预处理)
      floor_tracker                 NoiseFloorTracker 实例(动态底噪,默认 None=固定底噪,行为不变)。
                                    非 None 时每帧喂给估计器,start/end 阈值随 bg_t 变化:
                                    speech_started 前估计器持续更新,判定开始后本轮冻结。
      stop_event                    threading.Event(可选):置位后下一帧即提前退出(early-stop),
                                    用于"检测到目标即可不再等段尾"的场景(流式唤醒命中即时响应)。

    起始检测:读帧 → 帧内活跃样本计数 → 300ms 滑窗(窗口滚出则扣除) →
      活跃样本 ≥ 120ms → speech_started=True,并把 speech_start_sample 回退到窗口起点。
    端点检测:说话中,每帧活跃则把"尾静音计数"扣减 penalty 倍(连续说话时计数永远攒不到
      1 秒,换气/停顿不会截断;而孤立 20ms 尖峰最多抵 80ms,不会拖延端点 —— 对尖峰免疫);
      静音帧则累加计数,攒够 endpoint_silence_ms 即 endpoint_triggered=True。
    返回: (captured int16 数组[整段含末尾静音+开头预卷], 每帧板卡侧 dBFS 列表,
           endpoint 诊断字典[speech_started/endpoint_triggered/各阈值/
           动态底噪 bg_initial/bg_final/bg_trajectory/...供日志与调参])
    """
    # 流式 ASR(可选):on_chunk 非 None 时,每凑满 600ms(9600 样本=30 帧)切一块回调,
    # 供"边说边识别";默认 None 行为与旧版完全一致(本地 V5 等调用者零影响)。
    samples = []
    dbfs_frames = []
    max_samples = int(max_seconds * sr)
    asr_blocks_done = 0                   # 已切出并回调的 600ms 块数
    endpoint_samples = int(endpoint_silence_ms * sr / 1000)
    voice_start_samples = int(voice_start_ms * sr / 1000)
    if voice_start_window_ms is None:
        voice_start_window_ms = voice_start_ms
    voice_start_window_samples = int(voice_start_window_ms * sr / 1000)
    start_threshold_dbfs = background_dbfs + threshold_above_bg
    if endpoint_threshold_above_bg is None:
        endpoint_threshold_above_bg = threshold_above_bg
    endpoint_threshold_dbfs = background_dbfs + endpoint_threshold_above_bg
    total_samples = 0
    active_samples = 0
    start_activity_window = []
    start_activity_window_samples = 0
    trailing_silence_samples = 0
    speech_started = False
    endpoint_triggered = False
    speech_start_sample = None
    last_active_sample = None
    start = time.perf_counter()
    # 动态底噪音频时钟:帧数驱动(epoch + 样本数/sr),实时链路等价(帧按 50fps 到达),
    # 回放/回归确定性;跨轮单调(epoch 取真实时钟),保持"超时清零确认计数"的语义
    floor_epoch = time.monotonic()

    while total_samples < max_samples:
        # early-stop 挂点:on_chunk 回调(如流式唤醒判定命中)置位后,下一帧即提前结束 ——
        # 用于"检测到目标即可不再等段尾"的场景(唤醒命中即时响应)。
        if stop_event is not None and stop_event.is_set():
            break
        _, board_dbfs, frame = read_frame(ser)
        remaining = max_samples - total_samples
        frame = frame[:remaining]
        if len(frame) == 0:
            break
        samples.append(frame)
        frame_dbfs = rms_dbfs(frame)
        dbfs_frames.append(board_dbfs)
        total_samples += len(frame)
        # ---- 动态底噪(可选):每帧喂给估计器,speech_started 前持续更新; ------------
        # 按最新 bg_t 重算本帧阈值(两次加法,零额外成本);floor_tracker=None 时
        # bg_t 恒为 background_dbfs,与旧版行为完全一致。
        if floor_tracker is not None:
            floor_tracker.on_frame(frame_dbfs, speech_started,
                                   ts=floor_epoch + total_samples / sr)
            bg_now = floor_tracker.bg()
            start_threshold_dbfs = bg_now + threshold_above_bg
            endpoint_threshold_dbfs = bg_now + endpoint_threshold_above_bg
        # ---- 流式 ASR 挂点:每凑满 600ms(9600 样本 = 30 帧)切一块,立即回调增量识别 ----
        # 块边界与 recognize() 完全一致(从第一帧起每 9600 样本);on_chunk=None 时零开销。
        if on_chunk is not None:
            while total_samples // 9600 > asr_blocks_done:
                asr_blocks_done += 1
                block = np.concatenate(
                    samples[(asr_blocks_done - 1) * 30: asr_blocks_done * 30])
                on_chunk(block)

        if not speech_started:
            frame_active_samples = len(frame) if frame_dbfs > start_threshold_dbfs else 0
            start_activity_window.append((len(frame), frame_active_samples))
            start_activity_window_samples += len(frame)
            active_samples += frame_active_samples
            while start_activity_window and start_activity_window_samples > voice_start_window_samples:
                old_total, old_active = start_activity_window.pop(0)
                start_activity_window_samples -= old_total
                active_samples -= old_active
            if active_samples >= voice_start_samples:
                speech_started = True
                speech_start_sample = max(0, total_samples - start_activity_window_samples)
                last_active_sample = total_samples
                trailing_silence_samples = 0
        else:
            if frame_dbfs > endpoint_threshold_dbfs:
                last_active_sample = total_samples
                # ---- 活跃帧(仍在说话)对"尾静音计数"做抵扣 ----
                # 为什么:说话中总会有换气/短停顿,如果一停顿就累加静音计数,
                # 一次 1 秒的停顿就会把话截断。
                # 规则:1ms 活跃音频抵 penalty(4)ms 沉默 —— 连续说话时计数永远
                # 攒不到"说完了",而孤立 20ms 尖峰最多抵 80ms,不会拖延端点
                # (对尖峰/敲击声免疫)。
                penalty = int(len(frame) * endpoint_active_penalty)
                trailing_silence_samples = max(0, trailing_silence_samples - penalty)
            else:
                trailing_silence_samples += len(frame)
                if trailing_silence_samples >= endpoint_samples:
                    endpoint_triggered = True
                    break

        if time.perf_counter() - start > max_seconds + 10:
            break

    captured = np.concatenate(samples) if samples else np.array([], dtype=np.int16)
    diag = {
        "endpoint_triggered": endpoint_triggered,
        "speech_started": speech_started,
        "vad_threshold_dbfs": start_threshold_dbfs,
        "vad_start_threshold_dbfs": start_threshold_dbfs,
        "vad_endpoint_threshold_dbfs": endpoint_threshold_dbfs,
        "vad_endpoint_threshold_above_bg_db": endpoint_threshold_above_bg,
        "vad_endpoint_active_penalty": endpoint_active_penalty,
        "vad_voice_start_window_ms": voice_start_window_ms,
        "trailing_silence_ms": round(trailing_silence_samples * 1000 / sr),
        "speech_start_seconds": round(speech_start_sample / sr, 3) if speech_start_sample is not None else "",
        "last_active_seconds": round(last_active_sample / sr, 3) if last_active_sample is not None else "",
        "post_speech_wait_seconds": (
            round((total_samples - last_active_sample) / sr, 3)
            if last_active_sample is not None else ""
        ),
    }
    if floor_tracker is not None:
        diag.update(floor_tracker.diagnostics())
    else:
        diag["dynamic_floor_enabled"] = False
        diag["bg_initial_dbfs"] = round(background_dbfs, 2)
        diag["bg_final_dbfs"] = round(background_dbfs, 2)
        diag["bg_trajectory"] = []
    return captured, dbfs_frames, diag


def save_wav(path, samples, sr=16000):
    """把 int16 样本保存为 16k/单声道/16bit wav(调试留证:每次问答的输入音频都落盘)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(samples.astype("<i2").tobytes())


def query_gpu_used_mb():
    """查询当前 GPU 显存占用(MB),用于峰值监控;失败返回 None(不抛异常)。"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
        return int(out.splitlines()[0].strip())
    except Exception:
        return None


class GpuPeakMonitor:
    """后台线程定时采样 nvidia-smi 显存占用,记录峰值(测量模型加载/推理的显存上界)。

    用法: with GpuPeakMonitor() as m: ... ;之后读 m.peak_mb。
    """

    def __init__(self, interval=0.1):
        self.interval = interval
        self.peak_mb = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            used = query_gpu_used_mb()
            if used is not None and (self.peak_mb is None or used > self.peak_mb):
                self.peak_mb = used
            time.sleep(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join(timeout=2)


def recognize(model, samples):
    """ASR 识别入口(服务器/本地链路实际调用):对 VAD 切好的一段音频做流式识别。

    与 asr_eval_core.recognize_streaming 的机制完全一致,区别仅在于输入是
    直接的内存数组(已归一化),不需要从 wav 文件读取 —— 因此服务器与本地
    板卡链路都用这个版本(音频来自 RingBuffer/串口,而非磁盘文件)。

    流程:
      chunk_size=[0,10,5] → 一块 600ms(=10×960 样本),chunk_stride=9600;
      每块调一次 model.generate(input=chunk, cache=cache, is_final=..., ...),
      cache 跨块传递模型内部状态(流式记忆),最后一块 is_final=True 冲刷缓冲;
      每块取 result[0]["text"] 直接拼接(块间顺序即时间顺序)。
    返回: (完整文本, 首块出字耗时秒或 "", 总耗时秒)。
    """
    audio = samples.astype(np.float32) / 32768.0
    chunk_size = [0, 10, 5]
    chunk_stride = chunk_size[1] * 960
    cache = {}
    parts = []
    first_partial = ""
    start = time.perf_counter()
    total_chunks = int((len(audio) - 1) / chunk_stride + 1)
    for i in range(total_chunks):
        chunk = audio[i * chunk_stride : (i + 1) * chunk_stride]
        result = model.generate(
            input=chunk,
            cache=cache,
            is_final=i == total_chunks - 1,
            chunk_size=chunk_size,
            disable_pbar=True,          # 关掉 FunASR 每次调用的 tqdm/rft_avg 进度条(日志降噪)
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        text = extract_text(result)
        if text:
            if first_partial == "":
                first_partial = round(time.perf_counter() - start, 3)
            parts.append(text)
    return "".join(parts), first_partial, time.perf_counter() - start


def write_row(path, row):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out.exists()
    with out.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="ESP32-S3 board microphone serial PCM to Paraformer ASR test.")
    parser.add_argument("--port", default="")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--distance", default="1m")
    parser.add_argument("--angle", default="0")
    parser.add_argument("--environment", default="未记录")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--record-seconds", type=float, default=5.0)
    parser.add_argument("--background-seconds", type=float, default=10.0)
    parser.add_argument("--endpoint-silence-ms", type=int, default=700)
    parser.add_argument("--vad-threshold-above-bg", type=float, default=6.0)
    parser.add_argument("--vad-end-threshold-above-bg", type=float, default=None)
    parser.add_argument("--vad-end-active-penalty", type=float, default=4.0)
    parser.add_argument("--voice-start-ms", type=int, default=100)
    parser.add_argument("--voice-start-window-ms", type=int, default=None)
    parser.add_argument("--pre-capture-settle-ms", type=int, default=150)
    parser.add_argument("--output", default="results/board_mic_asr_results.csv")
    parser.add_argument("--model", default="paraformer-zh-streaming")
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()

    if not args.port:
        ports = list_ports()
        print("Available ports:", ", ".join(ports) if ports else "none")
        args.port = input("Input ESP32 COM port, for example COM5: ").strip()
    if not args.port:
        raise RuntimeError("COM port is required.")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_dir = Path("recordings") / f"board_{run_id}"
    print(f"Opening board serial {args.port} @ {args.baud}...")
    ser = open_serial(args.port, args.baud)
    time.sleep(1)
    ser.reset_input_buffer()

    print("Loading Paraformer-online...")
    with GpuPeakMonitor() as monitor:
        model, device, load_seconds = load_paraformer(args.model, args.device)
        print(f"Model loaded on {device}, load_seconds={load_seconds:.3f}")
        print(f"GPU after load: {gpu_snapshot()}")

        input("Press Enter, then keep quiet near the board for background recording...")
        discard_buffered_audio(ser)
        bg, _ = capture_seconds(ser, args.background_seconds)
        background_dbfs = rms_dbfs(bg)
        save_wav(audio_dir / "background.wav", bg)
        print(f"Background dBFS_avg: {background_dbfs:.2f}")

        case_index = 0
        for sentence_id, sentence in enumerate(SENTENCES, start=1):
            for repeat in range(1, args.repeats + 1):
                case_index += 1
                print()
                print(f"[{case_index}] Distance={args.distance}, angle={args.angle}, repeat={repeat}")
                print(f"Read to the board: {sentence}")
                input("Press Enter, then speak when 'NOW SPEAK' appears...")
                discard_buffered_audio(ser, settle_seconds=max(0, args.pre_capture_settle_ms) / 1000.0)
                print("NOW SPEAK / 现在请讲话")
                samples, frame_dbfs, endpoint = capture_until_endpoint(
                    ser,
                    max_seconds=args.record_seconds,
                    background_dbfs=background_dbfs,
                    endpoint_silence_ms=args.endpoint_silence_ms,
                    threshold_above_bg=args.vad_threshold_above_bg,
                    endpoint_threshold_above_bg=args.vad_end_threshold_above_bg,
                    endpoint_active_penalty=args.vad_end_active_penalty,
                    voice_start_ms=args.voice_start_ms,
                    voice_start_window_ms=args.voice_start_window_ms,
                )
                wav_path = audio_dir / f"{args.distance}_{args.angle}_s{sentence_id:02d}_r{repeat}.wav"
                save_wav(wav_path, samples)
                all_dbfs = rms_dbfs(samples)
                voice_dbfs, active_ratio = active_voice_dbfs(samples, background_dbfs)
                snr = voice_dbfs - background_dbfs
                text, first_partial, infer_seconds = recognize(model, samples)
                row_cer = cer(sentence, text)
                row = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "run_id": run_id,
                    "source": "ESP32-S3 board microphone serial PCM",
                    "port": args.port,
                    "baud": args.baud,
                    "distance": args.distance,
                    "angle": args.angle,
                    "environment": args.environment,
                    "sentence_id": sentence_id,
                    "repeat": repeat,
                    "expected": sentence,
                    "audio_file": str(wav_path),
                    "record_seconds": round(len(samples) / 16000, 3),
                    "max_record_seconds": args.record_seconds,
                    "background_dbfs_avg": round(background_dbfs, 2),
                    "all_segment_dbfs_avg_reference": round(all_dbfs, 2),
                    "active_voice_dbfs_avg": round(voice_dbfs, 2),
                    "snr_db": round(snr, 2),
                    "active_frame_ratio": round(active_ratio, 4),
                    "first_partial_seconds": first_partial,
                    "final_result_seconds": round(infer_seconds, 3),
                    "recognized_text": text,
                    "cer": row_cer,
                    "keyword_ok": "是" if row_cer != "" and row_cer <= 0.35 else "否",
                    "acceptable": "是" if row_cer != "" and row_cer <= 0.35 else "否",
                    "asr_window_ms": 600,
                    "upload_packet_ms": 20,
                    "vad_endpoint_silence_ms": args.endpoint_silence_ms,
                    "vad_threshold_above_bg_db": args.vad_threshold_above_bg,
                    "vad_threshold_dbfs": round(endpoint["vad_threshold_dbfs"], 2),
                    "vad_start_threshold_above_bg_db": args.vad_threshold_above_bg,
                    "vad_start_threshold_dbfs": round(endpoint["vad_start_threshold_dbfs"], 2),
                    "vad_endpoint_threshold_above_bg_db": endpoint["vad_endpoint_threshold_above_bg_db"],
                    "vad_endpoint_threshold_dbfs": round(endpoint["vad_endpoint_threshold_dbfs"], 2),
                    "vad_endpoint_active_penalty": endpoint["vad_endpoint_active_penalty"],
                    "vad_voice_start_ms": args.voice_start_ms,
                    "vad_voice_start_window_ms": endpoint["vad_voice_start_window_ms"],
                    "pre_capture_settle_ms": args.pre_capture_settle_ms,
                    "vad_speech_started": "是" if endpoint["speech_started"] else "否",
                    "vad_endpoint_triggered": "是" if endpoint["endpoint_triggered"] else "否",
                    "vad_trailing_silence_ms": endpoint["trailing_silence_ms"],
                    "vad_speech_start_seconds": endpoint["speech_start_seconds"],
                    "vad_last_active_seconds": endpoint["last_active_seconds"],
                    "vad_post_speech_wait_seconds": endpoint["post_speech_wait_seconds"],
                    "gpu_peak_mb_observed": monitor.peak_mb if monitor.peak_mb is not None else "",
                    "gpu_after_case": gpu_snapshot(),
                }
                write_row(args.output, row)
                print(f"Recognized: {text}")
                print(f"SNR={snr:.2f} dB, CER={row_cer}, final={infer_seconds:.3f}s")
                print(
                    f"VAD endpoint={'triggered' if endpoint['endpoint_triggered'] else 'not triggered'}, "
                    f"audio={len(samples) / 16000:.3f}s, silence={endpoint['trailing_silence_ms']}ms"
                )
                print(f"Saved row: {args.output}")

    ser.close()
    print()
    print("Done.")
    print(f"Board mic recordings saved in: {audio_dir}")
    print(f"Results saved in: {args.output}")


if __name__ == "__main__":
    main()
