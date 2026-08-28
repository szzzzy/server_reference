# -*- coding: utf-8 -*-
"""线上唤醒全链路探测:模拟"持续上传固件"的完整一轮(唤醒词→应答→MIC_START→问题→回答)。

阶段1: 上传 [安静1.5s + "你好小科"(TTS合成) + 安静0.8s] → 等待下行
        期待 [SPKS 24000 → PCM×N → SPKE(唤醒应答) → MIC_START]
阶段2: 收到 MIC_START 后上传 [问题段(标准女声切片) + 安静] → 等待下行
        期待 [MIC_STOP → SPKS 24000 → PCM×M → SPKE],且 SPKE 后 3s 内无 MIC_START(回待机)

用法: 先启动 run_server.py --voice-mode real(voice.real.wake.enabled=true),再运行本脚本。
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
CHECKED = []


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
    """按 ≈17ms/帧(略快于实时)上传整段 PCM,返回发送结束时刻。"""
    n = len(pcm) // 320
    t0 = time.monotonic()
    for i in range(n):
        chunk = pcm[i * 320:(i + 1) * 320]
        await ws.send(pcm1_build(i + 1, chunk.tobytes(), int(rms_dbfs(chunk.tobytes()) * 100)))
        await asyncio.sleep(0.017)
    return t0


async def collect_until(ws, stop_text, timeout_s, tag):
    """收下行直到出现 stop_text(或超时)；返回 (文本序列[(t,text)], PCM帧数)。"""
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
        load_wav_pcm(HERE / "wake_hello.wav"),          # "你好小科"(TTS 合成 1.68s)
        tone(-58.0, 0.8, f=180.0),
    ])
    phase2 = np.concatenate([
        speech_slice(-25.0, 2.5, 1.5),                  # 问题段(标准女声 2.5s)
        tone(-58.0, 1.0, f=180.0),
    ])

    # ---- 阶段1: 唤醒词 → 应答 + MIC_START ----
    await upload(ws, phase1)
    t1, pcm1 = await collect_until(ws, "MIC_START", 40.0, "phase1")
    seq1 = [t for _, t in t1]
    check("P1 应答序列: SPKS 24000→PCM→SPKE→MIC_START",
          "SPKS 24000" in seq1 and "SPKE" in seq1 and seq1[-1] == "MIC_START" and pcm1 > 0,
          f"seq={seq1} pcm={pcm1}")

    # ---- 阶段2: 问题 → MIC_STOP → 回答 → SPKE(之后回待机,无 MIC_START) ----
    await upload(ws, phase2)
    t2, pcm2 = await collect_until(ws, "SPKE", 60.0, "phase2")
    seq2 = [t for _, t in t2]
    check("P2 问答序列: MIC_STOP→SPKS 24000→PCM→SPKE",
          "MIC_STOP" in seq2 and "SPKS 24000" in seq2 and seq2[-1] == "SPKE" and pcm2 > 0,
          f"seq={seq2} pcm={pcm2}")
    extra = await collect_until(ws, "__never__", 3.0, "post")
    seqx = [t for _, t in extra[0]]
    check("P3 回待机: SPKE 后 3s 内无 MIC_START", "MIC_START" not in seqx and not extra[1],
          f"seq={seqx}")

    print("WAKE:", "ALL PASS" if all(OK) else "SOME FAILED", f"({sum(OK)}/{len(OK)})")
    await ws.close()
    sys.exit(0 if all(OK) else 1)


if __name__ == "__main__":
    asyncio.run(main())
