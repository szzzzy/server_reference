# -*- coding: utf-8 -*-
"""意图决策层端到端测试 v3(2026-09-03 规则): 睡眠/敷衍两级判定/拒绝。

流程:
  E1 唤醒 → E2 睡眠(goodnight+回话+不续听) → E3 再唤醒
  E4a-b 连续敷衍×2(前2轮"疑似": 记录计数,按正常对话应答)
  E4c 第3轮敷衍(确认超限: dismiss→无表达、不续听)
  E5 再唤醒 → E6 拒绝(回话+不续听,dismiss)
"""
import argparse
import asyncio
import json
import ssl
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SRV = HERE.parent / "server"
sys.path.insert(0, str(SRV))

from common import detect_ip, pcm1_build, resolve_path, rms_dbfs, wss_connect  # noqa: E402

SR = 16000
OK = []


def check(name, cond, detail=""):
    OK.append(cond)
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""), flush=True)


def load_wav_16k(path):
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if rate != SR:
        out_n = int(len(x) * SR / rate)
        x = np.interp(np.linspace(0, len(x) - 1, out_n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16)


async def upload(ws, pcm):
    n = len(pcm) // 320
    for i in range(n):
        chunk = pcm[i * 320:(i + 1) * 320]
        await ws.send(pcm1_build(i + 1, chunk.tobytes(), int(rms_dbfs(chunk.tobytes()) * 100)))
        await asyncio.sleep(0.017)


async def collect_until(ws, stop_text, timeout_s, tag):
    texts, pcm = [], b""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        if isinstance(msg, str):
            texts.append(msg)
            print(f"  [{tag}] 下行<- {msg}", flush=True)
            if msg == stop_text:
                break
        else:
            pcm += bytes(msg)
    return texts, pcm, time.monotonic() - t0


def tone(level_db, seconds, f=180.0):
    amp = 32768.0 * 10 ** (level_db / 20.0) * np.sqrt(2.0)
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.int16)


def wrap(path):
    return np.concatenate([tone(-58.0, 1.0), load_wav_16k(path), tone(-58.0, 0.8)])


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleep-wav", required=True)
    ap.add_argument("--noreply-wav", required=True)
    ap.add_argument("--decline-wav", required=True)
    args = ap.parse_args()

    cfg = json.loads((SRV / "config.json").read_text(encoding="utf-8"))
    addr = cfg["server"]["addr"] if cfg["server"]["addr"] != "auto" else detect_ip()
    ctx = ssl.create_default_context(cafile=str(resolve_path(SRV, "../certs/ca/ca.crt")))
    ws = await wss_connect(f"wss://{addr}:{cfg['wss']['port']}{cfg['wss']['path']}", ctx,
                           headers={"Authorization": f"Bearer {cfg['wss']['token']}"})
    print(f"WSS connected ({addr}:{cfg['wss']['port']})", flush=True)

    wake_pcm = np.concatenate([tone(-58.0, 1.5), load_wav_16k(HERE / "wake_hello.wav"),
                               tone(-58.0, 0.8)])
    sleep_pcm = wrap(args.sleep_wav)
    noreply_pcm = wrap(args.noreply_wav)
    decline_pcm = wrap(args.decline_wav)

    # ---- E1 唤醒 ----
    await upload(ws, wake_pcm)
    texts, pcm, el = await collect_until(ws, "MIC_START", 40.0, "wake")
    check("E1 唤醒应答+MIC_START", "SPKS 24000" in texts and pcm and texts[-1] == "MIC_START",
          f"seq={texts} pcm={len(pcm)} 上传->MIC_START={el:.2f}s")

    # ---- E2 睡眠: 收尾回话 + 不续听 ----
    await upload(ws, sleep_pcm)
    texts, pcm, el = await collect_until(ws, "SPKE", 60.0, "sleep")
    check("E2 sleep: 收尾回话(SPKS→PCM→SPKE)", "SPKS 24000" in texts and "SPKE" in texts and pcm,
          f"seq={texts} pcm={len(pcm)} 上传->SPKE={el:.2f}s")
    texts2, _, _ = await collect_until(ws, "", 3.0, "sleep-after")
    check("E2b sleep: 结束后不续听(无 MIC_START)", "MIC_START" not in texts2, f"after={texts2}")

    # ---- E3 再唤醒 ----
    async def rewake(tag):
        await upload(ws, wake_pcm)
        return await collect_until(ws, "MIC_START", 40.0, tag)

    texts, pcm, el = await rewake("rewake1")
    check("E3 睡眠后可再次唤醒", "SPKS 24000" in texts and texts[-1] == "MIC_START",
          f"seq={texts} 上传->MIC_START={el:.2f}s")

    # ---- E4 连续敷衍: 前2轮"疑似"(no_reply_tolerance_rounds=3,第3轮确认超限) ----
    #      疑似轮: 单次"嗯嗯"无法断定敷衍 → 仅记录,按正常对话应答(SPKS→真实PCM→SPKE);
    #      应答后不主动续听(下一轮语音起始时由服务器发 MIC_START,见 §11 编排)。
    for i in range(2):
        await upload(ws, noreply_pcm)
        texts, pcm, el = await collect_until(ws, "SPKE", 60.0, f"noreply_{i}")
        check(f"E4.{i + 1} 敷衍嫌疑轮: 按正常对话应答(PCM非空)",
              "SPKS 24000" in texts and "SPKE" in texts and pcm and any(pcm),
              f"seq={texts} pcm={len(pcm)} 上传->SPKE={el:.2f}s")
    # ---- E4.3 第3轮敷衍 → 确认超限结束会话(dismiss): 无表达(不再 SPKS/SPKE),不续听。
    #      MIC_START 允许出现在本轮语音起始(设备→LISTEN 协议点),dismiss 后不得再发。
    await upload(ws, noreply_pcm)
    texts, pcm, el = await collect_until(ws, "", 8.0, "noreply_x")
    texts2, _, _ = await collect_until(ws, "", 3.0, "noreply_x_after")
    check("E4.3 敷衍第3轮: 确认超限结束会话(无 SPKS/SPKE,dismiss 后无 MIC_START)",
          "MIC_STOP" in texts and "SPKS" not in texts and "SPKE" not in texts
          and "MIC_START" not in texts2,
          f"seq={texts} after={texts2}")

    # ---- E5 再唤醒 → E6 拒绝: 收尾回话 + 不续听 ----
    texts, pcm, el = await rewake("rewake2")
    check("E5 敷衍结束后可再次唤醒", "SPKS 24000" in texts and texts[-1] == "MIC_START",
          f"seq={texts}")
    await upload(ws, decline_pcm)
    texts, pcm, el = await collect_until(ws, "SPKE", 60.0, "decline")
    check("E6 decline: 收尾回话(SPKS→PCM→SPKE)", "SPKS 24000" in texts and "SPKE" in texts and pcm,
          f"seq={texts} pcm={len(pcm)} 上传->SPKE={el:.2f}s")
    texts2, _, _ = await collect_until(ws, "", 3.0, "decline-after")
    check("E6b decline: 结束后不续听", "MIC_START" not in texts2, f"after={texts2}")

    print(f"\n意图端到端v2: passed={sum(OK)} failed={len(OK) - sum(OK)} / {len(OK)}", flush=True)
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
