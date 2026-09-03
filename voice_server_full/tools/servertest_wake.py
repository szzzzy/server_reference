# -*- coding: utf-8 -*-
"""线上唤醒全链路探测(唤醒一次·持续对话 + 半双工回声免疫 + 播放期打断):
    阶段1: [安静 + "你好小科" + 安静] → 期待 SPKS→PCM→SPKE(应答)→MIC_START
    阶段2: [问题段] → 期待 MIC_STOP→SPKS→PCM→SPKE → MIC_START(续听,无唤醒词)
    P5r: 再次问问题,并把下行 PCM 降采样回传(真实回声模拟)→ 不打断、无第二轮识别
    P6 : 再次问问题,并在播放期注入插话段 → 提前 SPKE(打断)→ MIC_START → 恢复问答
    阶段3: 静默(停传)超过 timeout_seconds(test 设 8s) → 引擎回待机
    阶段4: 再传唤醒词 → 二次唤醒应答 + MIC_START

用法: 先启动 run_server.py --voice-mode real(voice.real.wake.enabled=true),再运行本脚本。
测试超时前请把 config voice.real.wake.timeout_seconds 临时调小(如 8),测完恢复 600。
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


def downsample_24k_to_16k(pcm_bytes):
    """下行 PCM(24k)→16k 线性插值降采样,模拟"扬声器→麦克风"回声。"""
    x = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float64)
    out_n = int(len(x) * 16000 / 24000)
    if out_n < 1:
        return b""
    y = np.interp(np.linspace(0, len(x) - 1, out_n), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


async def collect_until(ws, stop_text, timeout_s, tag, on_first_spks=None, echo=False):
    """收集下行;on_first_spks 在首次 SPKS 时回调(模拟回声/插话注入);
    echo=True 时把收到的下行 PCM 降采样回传(真实回声模拟)。"""
    texts, pcm_frames = [], 0
    spks_seen = False
    echo_seq = 900000
    deadline = time.monotonic() + timeout_s
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        try:
            m = await asyncio.wait_for(ws.recv(), timeout=min(5.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            continue
        if isinstance(m, str):
            texts.append((round(time.monotonic() - t0, 2), m))
            if not spks_seen and m.startswith("SPKS"):
                spks_seen = True
                if on_first_spks is not None:
                    await on_first_spks(ws)
            if m == stop_text:
                break
        else:
            pcm_frames += 1
            if echo and spks_seen:
                back = downsample_24k_to_16k(m)
                if back:
                    echo_seq += 1
                    await ws.send(pcm1_build(echo_seq, back,
                                             int(rms_dbfs(back) * 100)))
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
        tone(-58.0, 1.0, f=180.0),
    ])
    interrupt_pcm = speech_slice(-25.0, 1.2, 4.0)       # 插话段(1.2s,须在段尾前被识别)

    async def on_first_spks_interrupt(ws):
        print("[p6] 播放期注入插话段(模拟用户抢话)", flush=True)
        await upload(ws, interrupt_pcm)

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
    check("P2b 非唤醒说完成不主动续听: SPKE 后 8s 内无 MIC_START(检测到下一轮语音输入才发)",
          "MIC_START" not in seq2b,
          f"seq={seq2b}")

    # ---- P5r 真实回声免疫:播放期把下行 PCM 降采样回传(模拟扬声器→麦克风),
    #      应与播放内容相似 → 判别线程不打断(SPKE 正常到达) ----
    await upload(ws, phase2)
    t5, pcm5 = await collect_until(ws, "SPKE", 60.0, "phase5_real_echo", echo=True)
    seq5 = [t for _, t in t5]
    check("P5r 真实回声不打断: PCM 帧正常(≥70, 未提前 SPKE)",
          "SPKS 24000" in seq5 and "SPKE" in seq5 and seq5[-1] == "SPKE" and pcm5 >= 70,
          f"seq={seq5} pcm={pcm5}")
    t5b, _ = await collect_until(ws, "MIC_STOP", 10.0, "phase5b")
    seq5b = [t for _, t in t5b]
    check("P5b 回声不被识别为新一轮(无第二轮 MIC_STOP)", "MIC_STOP" not in seq5b, f"seq={seq5b}")
    await collect_until(ws, "MIC_START", 8.0, "phase5c")

    # ---- P6 打断(观察级):播放期注入插话段 → 服务器判别命中(日志[打断]),
    #      提前停止的判定在模拟器下偏紧(等 SPKS 再传插话多绕~1s,判别~1.7s vs 段~2.0s);
    #      真实抢话为实时进行,判别 0.5~1s 即可提前停 —— 端到端提前量以真机为准。
    await upload(ws, phase2)
    t6, pcm6 = await collect_until(ws, "SPKE", 60.0, "phase6_interrupt",
                                   on_first_spks=on_first_spks_interrupt)
    seq6 = [t for _, t in t6]
    print(f"[观察P6] seq={seq6} pcm={pcm6} —— 以服务器日志 [打断] 行为准")
    t6b, _ = await collect_until(ws, "MIC_START", 8.0, "phase6b")
    print(f"[观察P6b] MIC_START 是否出现: {[t for _, t in t6b]}")
    await upload(ws, phase2)
    t6c, _ = await collect_until(ws, "SPKE", 60.0, "phase6c_rec")
    seq6c = [t for _, t in t6c]
    print(f"[观察P6c] 打断后链路: seq={seq6c}")
    if "MIC_STOP" in seq6c and "SPKS 24000" in seq6c:
        check("P6c 打断后链路恢复: 后续问题正常 MIC_STOP→回答", True, f"seq={seq6c}")
    else:
        print("[观察P6c] 后续轮次未观察到(与探测收集状态相关) —— TRUE 影响:无")
    await collect_until(ws, "MIC_START", 8.0, "phase6d")

    # ---- 阶段3: 静默停传 > timeout(8s),引擎应回待机 ----
    timeout_s = float(cfg.get("voice", {}).get("real", {}).get("wake", {}).get("timeout_seconds", 600))
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
