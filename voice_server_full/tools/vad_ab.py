# -*- coding: utf-8 -*-
"""P2 固定 PCM A/B 验证工具:固定底噪 vs 动态底噪(+heal)。

场景(S2 核心): 恒噪风扇 -35dB + 正常人声 —— 两种底噪在"本轮"均会被噪声在 ~0.12s 内
误触发起始(120ms 起始判定快于确认+上升的 1.5~2s,帧级自适应救不了本轮,方案 §15);
区别在 heal 之后的**下一轮**:动态底噪经 heal 重锚后阈值=噪声+3dB,噪声不再误触发,
只在人声到达时正确起始;固定底噪则每轮都坏。

场景(S3 已知问题,仅打印): 恒噪后的安静 + 轻声 —— 动态底噪 0.5dB/s 回落太慢,
30s 内阈值仍偏高,轻声(<阈值)漏检;固定底噪反而能识别。→ 归入 P3 调参项
(down_max_db_per_s / 静音快速回落)。

用法:
  .venv_5090_llm\\Scripts\\python.exe voice_server_full\\tools\\vad_ab.py
产物: tools/ab_step.wav(噪声轮 16s)、tools/ab_slow.wav(回落轮 14s),供 board_simulator/真机复现。
"""
import math
import struct
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent      # voice_server_full/
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))

from board_serial_asr_test import NoiseFloorTracker, capture_until_endpoint  # noqa: E402

SR = 16000
MAX_SECONDS = 16.0
PASS = []


def check(name, cond, detail=""):
    PASS.append(cond)
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))


def tone(level_db, seconds, f=220.0, phase=0.0):
    amp = 32768.0 * 10 ** (level_db / 20.0) * math.sqrt(2.0)   # 正弦 rms → 目标 dBFS
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t + phase)).astype(np.int16)


def noisy(level_db, seconds, seed=7):
    """风扇状噪声:白噪声 → 低通平滑 → 归一化到目标 dBFS。"""
    rng = np.random.default_rng(seed)
    x = np.convolve(rng.normal(size=int(seconds * SR)), np.ones(8) / 8.0, mode="same")
    target = 32768.0 * 10 ** (level_db / 20.0)
    x *= target / float(np.sqrt(np.mean(x ** 2)))
    return np.clip(x, -32768, 32767).astype(np.int16)


def speech_slice(level_db, seconds, offset_s=0.0):
    """从库内标准女声样本切一段并归一化到目标 dBFS。"""
    p = ROOT.parent / "samples" / "standard_female_voice_16k_mono_16bit.wav"
    with wave.open(str(p), "rb") as w:
        raw = w.readframes(int((offset_s + seconds) * w.getframerate()))
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)[: int(seconds * SR)]
    target = 32768.0 * 10 ** (level_db / 20.0)
    x *= target / (float(np.sqrt(np.mean(x ** 2))) + 1e-9)
    return np.clip(x, -32768, 32767).astype(np.int16)


def write_wav(path, x):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x.astype("<i2").tobytes())


def pcm1_frame(seq, samples):
    payload = np.asarray(samples, dtype="<i2").tobytes()
    h = (b"PCM1" + struct.pack("<I", seq) + struct.pack("<H", len(payload))
         + struct.pack("<h", 0) + b"\x00\x00\x00" + bytes([sum(payload) & 0xFF]))
    return h + payload


class FakeStream:
    def __init__(self, samples):
        frames = []
        for i in range(len(samples) // 320):
            frames.append(pcm1_frame(i + 1, samples[i * 320:(i + 1) * 320]))
        self._buf = b"".join(frames)
        self.pos = 0

    def read(self, n):
        data = self._buf[self.pos:self.pos + n]
        self.pos += len(data)
        return data

    def reset_input_buffer(self):
        pass


def run_vad(samples, background, floor=None):
    return capture_until_endpoint(
        FakeStream(samples), max_seconds=MAX_SECONDS, background_dbfs=background,
        endpoint_silence_ms=400, threshold_above_bg=3.0, endpoint_threshold_above_bg=3.0,
        endpoint_active_penalty=4.0, voice_start_ms=120, voice_start_window_ms=300,
        floor_tracker=floor)


def heal_anchor(samples, threshold):
    """引擎级 heal 的等价逻辑:帧电平 10% 分位重锚。"""
    from board_serial_asr_test import rms_dbfs
    n = len(samples) // 320
    rows = np.array(samples[:n * 320], dtype=np.int16).reshape(-1, 320)
    fr = np.array([rms_dbfs(r) for r in rows])
    active_ratio = float(np.mean(fr > threshold))
    return float(np.percentile(fr, 10)), active_ratio


def seconds_of(ep, key):
    v = ep.get(key)
    return float(v) if v not in (None, "") else None


# ================= 场景 S2:恒噪 + heal 跨轮恢复 =================
print("=" * 72)
print("S2 恒噪场景:风扇-35dB 全程 + 第6s起人声-25dB(2.5s) | 两轮对比")
turn1_audio = np.concatenate([
    noisy(-35.0, 6.0, seed=7),      # 0~6s 风扇
    speech_slice(-25.0, 2.5, 0.5),  # 6~8.5s 人声
    noisy(-35.0, MAX_SECONDS - 8.5, seed=9),  # 8.5s~16s 风扇(封顶=超时判据)
])
write_wav(ROOT / "tools" / "ab_step.wav", turn1_audio)

eps = {}
for label, floor in (("fixed", None), ("dynamic", NoiseFloorTracker(-55.0, {}))):
    _, _, ep = run_vad(turn1_audio, -55.0, floor)
    eps[label] = ep
    print(f"  [{label}] 本轮: speech_started={ep['speech_started']} "
          f"start={seconds_of(ep, 'speech_start_seconds')}s endpoint={ep['endpoint_triggered']} "
          f"bg_final={ep['bg_final_dbfs']}")
check("S2-1 本轮两者均被判噪声起始(帧级救不了本轮,故需 heal)",
      eps["fixed"]["speech_started"] and eps["dynamic"]["speech_started"]
      and seconds_of(eps["fixed"], "speech_start_seconds") < 1.0,
      f"fixed={seconds_of(eps['fixed'],'speech_start_seconds')}s")
check("S2-2 本轮两者均无端点(噪声钉死→超时)",
      not eps["fixed"]["endpoint_triggered"] and not eps["dynamic"]["endpoint_triggered"])

heal_bg_expected, _ = heal_anchor(turn1_audio, -52.0)
print(f"  heal 重锚 bg_t ≈ {heal_bg_expected:.1f} dBFS(期望≈噪声-35)")
check("S2-3 heal 锚点落在噪声附近", -38.0 < heal_bg_expected < -33.0,
      f"anchor={heal_bg_expected:.1f}")

# 第二轮(模拟 heal 后的会话继续):风扇继续 + 第2s起人声(补齐到 max_seconds)
turn2_audio = np.concatenate([
    noisy(-35.0, 2.0, seed=11),      # 0~2s 风扇(预语音段)
    speech_slice(-25.0, 2.5, 3.0),   # 2~4.5s 人声
    noisy(-35.0, MAX_SECONDS - 4.5, seed=13),  # 4.5s~16s 风扇(无静音收尾,封顶=超时判据)
])
for label, floor in (("fixed", None), ("dynamic", NoiseFloorTracker(heal_bg_expected, {}))):
    _, _, ep = run_vad(turn2_audio, -55.0, floor)
    eps["turn2_" + label] = ep
    print(f"  [{label}] 第二轮: speech_started={ep['speech_started']} "
          f"start={seconds_of(ep, 'speech_start_seconds')}s endpoint={ep['endpoint_triggered']} "
          f"尾静音={ep.get('trailing_silence_ms')}ms bg_final={ep['bg_final_dbfs']:.1f}")
check("S2-4 固定底噪第二轮仍被噪声起始(每轮都坏)",
      seconds_of(eps["turn2_fixed"], "speech_start_seconds") is not None
      and seconds_of(eps["turn2_fixed"], "speech_start_seconds") < 1.0)
check("S2-5 动态底噪第二轮只在人声处起始(≥1.5s, heal 生效)",
      seconds_of(eps["turn2_dynamic"], "speech_start_seconds") is not None
      and seconds_of(eps["turn2_dynamic"], "speech_start_seconds") >= 1.5,
      f"start={seconds_of(eps['turn2_dynamic'],'speech_start_seconds')}s")
check("S2-6 动态底噪第二轮端点正常触发(噪声不再钉死)",
      bool(eps["turn2_dynamic"]["endpoint_triggered"]))

# ================= 场景 S3:回落过慢 → P3 调参项(仅打印) =================
print("=" * 72)
print("S3 回落场景:heal 后 bg≈-36 → 安静6s(回落) → 轻声-34dB(2.5s) | 仅观察")
turn3_audio = np.concatenate([
    tone(-58.0, 6.0, f=180.0),                       # 0~6s 安静(-58)
    speech_slice(-34.0, 2.5, 1.5),                   # 6~8.5s 轻声
    tone(-58.0, MAX_SECONDS - 8.5, f=180.0),         # 8.5s~16s 安静收尾
])
write_wav(ROOT / "tools" / "ab_slow.wav", turn3_audio)
_, _, ep_f = run_vad(turn3_audio, -55.0, None)
_, _, ep_d = run_vad(turn3_audio, -55.0, NoiseFloorTracker(-36.0, {}))
print(f"  [fixed ] start={seconds_of(ep_f,'speech_start_seconds')}s endpoint={ep_f['endpoint_triggered']}")
print(f"  [dynamic] start={seconds_of(ep_d,'speech_start_seconds')}s endpoint={ep_d['endpoint_triggered']} "
      f"bg_final={ep_d['bg_final_dbfs']:.1f}")
print(f"  → 实测:bg_t {ep_d['bg_initial_dbfs']}→{ep_d['bg_final_dbfs']:.1f}dB(快速回落),"
      f"轻声捕获较 fixed 差距 {abs((seconds_of(ep_d,'speech_start_seconds') or 0) - (seconds_of(ep_f,'speech_start_seconds') or 0)):.2f}s")
print("  → 下一场景 S4 专门验证『噪声刚停即轻声』的快速回落规则(P3)")

# ================= 场景 S4:恒噪刚停即轻声 → P3 调参验证(snap 快速回落) =================
print("=" * 72)
print("S4 快速回落场景:heal 后 bg≈-36 → 风扇-36dB(1s) → 安静2s(预语音段) → 轻声-34.5dB")
turn4_audio = np.concatenate([
    noisy(-36.0, 1.0, seed=21),                      # 0~1s 风扇(继续)
    tone(-58.0, 2.0, f=180.0),                       # 1~3s 安静(预语音段:快落窗口)
    speech_slice(-34.5, 2.5, 1.5),                   # 3~5.5s 轻声(噪声刚停即说)
    tone(-58.0, MAX_SECONDS - 5.5, f=180.0),
])
write_wav(ROOT / "tools" / "ab_quick.wav", turn4_audio)
_, _, ep_old = run_vad(turn4_audio, -55.0, NoiseFloorTracker(-36.0, {"down_max_db_per_s": 0.5, "snap_trigger_db": 0.0}))
_, _, ep_new = run_vad(turn4_audio, -55.0, NoiseFloorTracker(-36.0, {"down_max_db_per_s": 0.5, "snap_trigger_db": 6.0}))
print(f"  [无snap  ] start={seconds_of(ep_old,'speech_start_seconds')}s endpoint={ep_old['endpoint_triggered']} "
      f"bg={ep_old['bg_initial_dbfs']}→{ep_old['bg_final_dbfs']:.1f}")
print(f"  [snap=6dB] start={seconds_of(ep_new,'speech_start_seconds')}s endpoint={ep_new['endpoint_triggered']} "
      f"bg={ep_new['bg_initial_dbfs']}→{ep_new['bg_final_dbfs']:.1f}")
old_start = seconds_of(ep_old, "speech_start_seconds")
new_start = seconds_of(ep_new, "speech_start_seconds")
check("S4-1 快速回落:bg_t 重锚到安静分位(< -45)", float(ep_new["bg_final_dbfs"]) < -45.0,
      f"bg={ep_new['bg_final_dbfs']:.1f} (无snap对照={ep_old['bg_final_dbfs']:.1f})")
check("S4-2 快速回落:噪声刚停即轻声可捕获(起始≤3.4s)",
      new_start is not None and new_start <= 3.4, f"start={new_start}s (无snap对照={old_start}s)")
check("S4-3 快速回落:端点正常", bool(ep_new["endpoint_triggered"]))

print("=" * 72)
print("RESULT:", "ALL PASS" if all(PASS) else "SOME FAILED", f"({sum(PASS)}/{len(PASS)})")
sys.exit(0 if all(PASS) else 1)
