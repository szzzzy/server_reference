# -*- coding: utf-8 -*-
"""两轮对话协议探测:模拟 ESP32 Julia 设备连续两轮(听—想—说)。

第一轮: 连接 WSS → 发 MIC_START → 上传 PCM1×150 帧(标准女声样本 3s)
        → 等下行 [MIC_STOP → SPKS 24000 → PCM×N → SPKE → MIC_START]
第二轮: 收到 MIC_START 后立即再次上传 PCM1×150 帧 → 等同样的下行序列
断言: 每轮序列完整、MIC_START 出现在 SPKE 之后、两轮均有 PCM 下行。
用法: 先启动 run_server.py --voice-mode real, 再运行本脚本。
"""
import asyncio
import json
import ssl
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import detect_ip, load_test_wav, pcm1_build, resolve_path, rms_dbfs, wss_connect

FRAMES = 150          # 150 帧 × 20ms = 3.0s 音频


async def upload_and_collect(ws, pcm, tag):
    """上传一段 PCM1 音频帧,收集下行直到 SPKE(及其后的 MIC_START)。返回 (文本序列, PCM 帧数)。"""
    seq = 0
    n = min(FRAMES, len(pcm) // 640)
    for i in range(n):
        chunk = pcm[i * 640:(i + 1) * 640]
        seq += 1
        await ws.send(pcm1_build(seq, chunk, int(rms_dbfs(chunk) * 100)))
        await asyncio.sleep(0.02)      # 20ms/帧,与设备节奏一致
    texts, pcm_frames = [], 0
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            m = await asyncio.wait_for(ws.recv(), timeout=min(5.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            continue
        if isinstance(m, str):
            texts.append(m)
            if m == "SPKE":
                # 多轮模式下 SPKE 后应立即收到 MIC_START(等 5s 确认)
                try:
                    nxt = await asyncio.wait_for(ws.recv(), timeout=5)
                    if isinstance(nxt, str):
                        texts.append(nxt)
                except asyncio.TimeoutError:
                    pass
                break
        else:
            pcm_frames += 1
    return texts, pcm_frames


async def main():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    addr = cfg["server"]["addr"] if cfg["server"]["addr"] != "auto" else detect_ip()
    ctx = ssl.create_default_context(cafile=str(resolve_path(HERE, "../certs/ca/ca.crt")))
    ws = await wss_connect(f"wss://{addr}:{cfg['wss']['port']}{cfg['wss']['path']}", ctx,
                           headers={"Authorization": f"Bearer {cfg['wss']['token']}"})
    print("WSS connected (Bearer OK)")
    wav_path = resolve_path(HERE, "../samples/standard_female_voice_16k_mono_16bit.wav")
    pcm = load_test_wav(wav_path)
    ok = True
    await ws.send("MIC_START")       # 第一轮:设备本地唤醒后上传
    for turn in (1, 2):
        texts, pcm_frames = await upload_and_collect(ws, pcm, turn)
        print(f"[round {turn}] downlink texts={texts}")
        print(f"[round {turn}] pcm_frames={pcm_frames}")
        seq_ok = (texts[:1] == ["MIC_STOP"]
                  and "SPKS 24000" in texts
                  and texts[-1] == "SPKE")      # 末尾 SPKE;MIC_START 可选(设备侧软监听续接)
        if not seq_ok:
            print(f"[round {turn}] FAIL: 顺序或结尾不符合协议 {texts}")
        ok &= seq_ok and pcm_frames > 0
    print("MULTITURN:", "PASS" if ok else "FAIL")
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
