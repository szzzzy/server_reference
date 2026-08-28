# -*- coding: utf-8 -*-
"""真机全链路 A/B 探测:一轮噪声轮 + 一轮恢复轮(同 voice_multiturn_probe 结构)。

用法:
  1) 先启动 run_server.py --voice-mode real(dynamic_floor 按目标配置开启/关闭);
  2) 再运行本脚本(需 voice_server_full/server 已起、证书在 certs/ 下)。

断言:每轮收到 MIC_STOP→(SPKS→PCM→SPKE→MIC_START) 完整序列。
判定结果以服务器 runs/<ts>/virtual_server.log 的 VAD/heal 行为准。
"""
import asyncio
import json
import ssl
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRV = HERE.parent / "server"
sys.path.insert(0, str(SRV))

from common import detect_ip, load_test_wav, pcm1_build, resolve_path, rms_dbfs, wss_connect  # noqa: E402

# 三轮:turn1 纯噪声(喂开机校准) → turn2 噪声+人声(15s 超时→heal) → turn3 同 turn2(恢复轮)
TURN_WAVS = [HERE / "ab_noise.wav", HERE / "ab_turn2.wav", HERE / "ab_turn2.wav"]


async def upload_and_collect(ws, pcm, tag):
    """整段 wav 按 20ms/帧节奏上传,收集下行直到 SPKE(再等 5s 确认 MIC_START)。"""
    seq = 0
    n = len(pcm) // 640
    t0 = time.monotonic()
    for i in range(n):
        chunk = pcm[i * 640:(i + 1) * 640]
        seq += 1
        await ws.send(pcm1_build(seq, chunk, int(rms_dbfs(chunk) * 100)))
        await asyncio.sleep(0.017)         # ≈17ms/帧: 略快于实时(留缓冲余量)但保持校准时间窗不失真
    print(f"[{tag}] 上传完成 {n}帧({n * 0.02:.1f}s)", flush=True)
    texts, pcm_frames = [], 0
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            m = await asyncio.wait_for(ws.recv(), timeout=min(5.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            continue
        if isinstance(m, str):
            texts.append((round(time.monotonic() - t0, 2), m))
            if m == "SPKE":
                try:
                    nxt = await asyncio.wait_for(ws.recv(), timeout=5)
                    if isinstance(nxt, str):
                        texts.append((round(time.monotonic() - t0, 2), nxt))
                except asyncio.TimeoutError:
                    pass
                break
        else:
            pcm_frames += 1
    print(f"[{tag}] 下行序列: {[t for _, t in texts]} | PCM帧={pcm_frames} "
          f"| 上传开始→SPKE {round(time.monotonic() - t0, 1)}s", flush=True)
    return texts, pcm_frames


async def main():
    cfg = json.loads((SRV / "config.json").read_text(encoding="utf-8"))
    addr = cfg["server"]["addr"] if cfg["server"]["addr"] != "auto" else detect_ip()
    ctx = ssl.create_default_context(cafile=str(resolve_path(SRV, "../certs/ca/ca.crt")))
    ws = await wss_connect(f"wss://{addr}:{cfg['wss']['port']}{cfg['wss']['path']}", ctx,
                           headers={"Authorization": f"Bearer {cfg['wss']['token']}"})
    print(f"WSS connected ({addr}:{cfg['wss']['port']})", flush=True)
    await ws.send("MIC_START")
    ok = True
    for i, wav in enumerate(TURN_WAVS, start=1):
        pcm = load_test_wav(wav)
        texts, frames = await upload_and_collect(ws, pcm, f"turn{i}")
        seq = [t for _, t in texts]
        ok &= ("MIC_STOP" in seq and "SPKS 24000" in seq and "SPKE" in seq)
    print("SERVERTEST:", "PASS" if ok else "FAIL")
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
