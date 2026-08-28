# -*- coding: utf-8 -*-
"""线上唤醒全链路探测(唤醒一次·持续对话 + 半双工回声免疫版本):
    阶段1: [安静 + "你好小科" + 安静] → 期待 SPKS→PCM→SPKE(应答)→MIC_START
    阶段2: [问题段 + 安静 + **回声段**(模拟扬声器回声)] →
           期待 MIC_STOP→SPKS→PCM→SPKE → MIC_START(续听,无唤醒词);
           **且 SPKE 后 10s 内无第二轮 MIC_STOP**(半双工:回声不被识别,无自问自答)
    阶段3: 静默(停传)超过 timeout_seconds(测试时 config 设 8s) → 期待引擎回待机
    阶段4: 再传一次唤醒词 → 期待再次唤醒应答 + MIC_START(证明超时后回了待机)

用法: 先启动 run_server.py --voice-mode real(voice.real.wake.enabled=true),再运行本脚本。
测试超时前请把 config voice.real.wake.timeout_seconds 临时调小(如 8),测完恢复 60。
"""
import asyncio
import json
import math
import ssl
import struct
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SRV = HERE.parent / "server"
sys.path.insert(0, str(SRV))

from common import detect_ip, load_test_wav, pcm1_build, resolve_path, rms_dbfs, wss_connect  # noqa: E402

SR = 16000
OK = []


def check(name, cond, detail=""):
    OK.append(cond)
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""), flush=True)


def tone(level_db, seconds, f=220.0, phase=0.0):
    amp = 32768.0 * 10 ** (level_db / 20.0) * math.sqrt(2.0)
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t + phase)).astype(np.int16)


def speech_slice(level_db, seconds, offset_s=1.5):
    p = HERE.parent.parent / "samples" / "standard_female_voice_16k_mono_16bit.wav"
    with wave.open(str(p), "rb") as w:
        raw = w.readframes(int((offset_s + seconds) * w.getframerate()))
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)[: int(seconds * SR)]
    target = 32768.0 * 10 ** (level_db / 20.0)
    x *= target / (float(np.sqrt(np.mean(x ** 2))) + 1e-9)
    return np.clip(x, -32768, 32767).astype(np.int16)


def load_wav_pcm(path):
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


async def upload(ws, pcm):
    n = len(pcm) // 320
    for i in range(n):
        chunk = pcm[i * 320:(i + 1) * 320]
        await ws.send(pcm1_build(i + 1, chunk.tobytes(), int(rms_dbfs(chunk.tobytes()) * 100)))
        await asyncio.sleep(0.017)


async def collect_until(ws, stop_text, timeout_s, tag):
    texts, pcm_frames = [], 0
    deadline = time.monotonic() + timeout_s
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        try:
            m = await asyncio.wait_for(ws.recv(), timeout=min(5.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            continue
        if isinstance(m, str):
            texts.append((round(time.monotonic() - t0, 2), m))
            if m == stop_text:
                break
        else:
            pcm_frames += 1
    print(f"[{tag}] 累积 {time.monotonic() - t0:.1f}s texts={[t for _, t in texts]} pcm={pcm_frames}",
          flush=True)
    return texts, pcm_frames


async def main():
    cfg = json.loads((SRV / "config.json").read_text(encoding="utf-8"))
    addr = cfg["server"]["addr"] if cfg["server"]["addr"] != "auto" else detect_ip()
    ctx = ssl.create_default_context(cafile=str(resolve_path(SRV, "../certs/ca/ca.crt")))
    ws = await wss_connect(f"wss://{addr}:{cfg['wss']['port']}{cfg['wss']['path']}", ctx,
                           headers={"Authorization": f"Bearer {cfg['wss']['token']}"})
    print(f"WSS connected ({addr}:{cfg['wss']['port']})", flush=True)

    phase1 = np.concatenate([
        tone(-58.0, 1.5, f=180.0),
        load_wav_pcm(HERE / "wake_hello.wav"),
        tone(-58.0, 0.8, f=180.0),
    ])
    phase2 = np.concatenate([
        speech_slice(-25.0, 2.5, 1.5),                  # 问题段(标准女声 2.5s)
        tone(-58.0, 0.4, f=180.0),
        speech_slice(-25.0, 2.5, 4.0),                  # 回声段:模拟扬声器播报被麦克风录回
        tone(-58.0, 0.6, f=180.0),
    ])

    # ---- 阶段1: 唤醒词 → 应答 + MIC_START ----
    await upload(ws, phase1)
    t1, pcm1 = await collect_until(ws, "MIC_START", 40.0, "phase1")
    seq1 = [t for _, t in t1]
    check("P1 应答序列: SPKS→PCM→SPKE→MIC_START",
          "SPKS 24000" in seq1 and "SPKE" in seq1 and seq1[-1] == "MIC_START" and pcm1 > 0,
          f"seq={seq1} pcm={pcm1}")

    # ---- 阶段2: 问题(不再说唤醒词) → MIC_STOP → 回答 → SPKE → MIC_START(续听) ----
    await upload(ws, phase2)
    t2, pcm2 = await collect_until(ws, "SPKE", 60.0, "phase2")
    seq2 = [t for _, t in t2]
    check("P2 问题序列: MIC_STOP→SPKS→PCM→SPKE",
          "MIC_STOP" in seq2 and "SPKS 24000" in seq2 and seq2[-1] == "SPKE" and pcm2 > 0,
          f"seq={seq2} pcm={pcm2}")
    t2b, _ = await collect_until(ws, "MIC_START", 8.0, "phase2b")
    seq2b = [t for _, t in t2b]
    check("P2b 持续对话: SPKE 后收到 MIC_START(无需重新说唤醒词)",
          seq2b and seq2b[-1] == "MIC_START",
          f"seq={seq2b}")

    # ---- P5 半双工:回声免疫 —— SPKE 后必须无第二轮 MIC_STOP(否则=自问自答) ----
    t5, _ = await collect_until(ws, "MIC_STOP", 10.0, "phase5_echo")
    seq5 = [t for _, t in t5]
    check("P5 回声免疫: 播报回声不被识别为新一轮问题(无第二轮 MIC_STOP)",
          "MIC_STOP" not in seq5 and "SPKS 24000" not in seq5,
          f"seq={seq5}")

    # ---- 阶段3: 静默停传 > timeout(8s),引擎应回待机 ----
    timeout_s = float(cfg.get("voice", {}).get("real", {}).get("wake", {}).get("timeout_seconds", 60))
    print(f"[phase3] 静默 {timeout_s + 3:.0f}s(等待空闲超时)", flush=True)
    await asyncio.sleep(timeout_s + 3.0)
    # 无下行可观测(超时回待机不发命令),从阶段4 的"二次唤醒"验证

    # ---- 阶段4: 再次说唤醒词 → 应再次唤醒(证明已回待机) ----
    await upload(ws, phase1)
    t4, pcm4 = await collect_until(ws, "MIC_START", 40.0, "phase4")
    seq4 = [t for _, t in t4]
    check("P4 超时后二次唤醒: 再次命中唤醒词→应答→MIC_START",
          "SPKS 24000" in seq4 and "SPKE" in seq4 and seq4[-1] == "MIC_START" and pcm4 > 0,
          f"seq={seq4} pcm={pcm4}")

    print("WAKE:", "ALL PASS" if all(OK) else "SOME FAILED", f"({sum(OK)}/{len(OK)})")
    await ws.close()
    sys.exit(0 if all(OK) else 1)


if __name__ == "__main__":
    asyncio.run(main())
