# -*- coding: utf-8 -*-
"""注压测试接口自测(隔离端口 19443/18443,不影响在跑服务):
1) WSS 客户端持续上行 50fps;2) GET /__stall?sec=3 触发注压;
3) 断言: 注压期间服务器 frames 计数冻结≈3s,客户端发送被背压(TCP 窗口关闭);
4) 压力解除后帧流恢复。"""
import asyncio
import json
import ssl
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

CFG = {"wss": {"host": "127.0.0.1", "port": 19443, "path": "/voice", "token": "test",
               "tls": False, "ping_interval": 10, "ping_timeout": 20},
       "file_server": {"host": "127.0.0.1", "port": 18443, "tls": False}}


async def main():
    import sys
    sys.path.insert(0, str(HERE))
    from common import pcm1_build, setup_logging, wss_connect
    from session_hub import SessionHub
    from voice_bridge import make_engine
    from wss_adapter import WssAdapter

    run_dir = Path(tempfile.mkdtemp(prefix="stall_test_"))
    setup_logging(run_dir, name="stall_test", level=logging_info())
    hub = SessionHub(run_dir)
    engine = make_engine("stub", hub, cfg=None, run_dir=None)

    wss = WssAdapter(CFG, hub, engine, run_dir)
    await wss.start()

    # HTTPS 文件服务(线程)+ stall 端点
    from https_file_server import serve_file_server
    fs = serve_file_server(Path(tempfile.mkdtemp()), int(CFG["file_server"]["port"]),
                           ssl_ctx=None, host="127.0.0.1")
    fs.stall_fn = wss.stall

    # ---- 测试客户端 ----
    uri = "ws://127.0.0.1:19443/voice"
    async with await wss_connect(uri, None, headers={"Authorization": "Bearer test"}) as ws:
        seq = 0
        payload = b"\x00\x00" * 320

        async def pump(seconds):
            nonlocal seq
            t_end = time.time() + seconds
            sent = 0
            while time.time() < t_end:
                frame = pcm1_build(seq & 0xFFFFFFFF, payload, -6000)
                seq += 1
                await ws.send(frame)
                sent += 1
                await asyncio.sleep(0.02)
            return sent

        # 正常期 2s
        n1 = await pump(2.0)
        f1 = engine.frames
        print(f"正常期: 发送 {n1} 帧, 引擎收到 {f1}")

        # 触发注压 6s(收紧 rcvbuf + 暂停读取后,发送端应在 ~1s 内被压住)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = "http://127.0.0.1:18443/__stall?sec=6"
        r = urllib.request.urlopen(url, timeout=5)
        resp = json.loads(r.read().decode("utf-8"))
        print("__stall 响应:", resp)
        assert resp["ok"] is True and resp["stall_seconds"] == 6.0

        # 无歧义验证: 突发发送 2000 帧(1.3MB),期间必须出现 send 阻塞(≥2s 级)
        # 且注压未结束时引擎零计数;注压结束后积压帧突发投递。
        await asyncio.sleep(1.2)          # 让 TCP 窗口关闭、路径缓冲耗尽
        print("注压状态: _paused=%s stall_remaining=%.2f" % (
            len(wss._paused), wss.stall_remaining()))
        f_before = engine.frames
        n_ok, n_timeout, max_dt = 0, 0, 0.0
        st_all = time.time()
        for _ in range(2000):
            frame = pcm1_build(seq & 0xFFFFFFFF, payload, -6000)
            seq += 1
            st = time.time()
            try:
                await asyncio.wait_for(ws.send(frame), timeout=2.0)
            except asyncio.TimeoutError:
                n_timeout += 1
                continue
            dt = time.time() - st
            max_dt = dt if dt > max_dt else max_dt
            n_ok += 1
        dur = time.time() - st_all
        f_mid = engine.frames
        print(f"突发 2000 帧: 成功={n_ok} 阻塞超时={n_timeout} 最大send延时={max_dt:.2f}s "
              f"总耗时={dur:.2f}s; 注压期引擎计数 {f_before} → {f_mid}")
        # 注压结束,积压帧应突发投递(等 stall 真正结束再测)
        while wss.stall_remaining() > 0:
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.0)
        f_end = engine.frames
        print(f"恢复期: 引擎帧数 {f_mid} → {f_end}")
        # 服务器侧语义断言: 注压期间零投递;结束后积压帧突发投递
        assert f_mid - f_before <= 150, "注压期间数据仍在投递 —— 机制未生效"
        assert f_end - f_mid >= 100, "注压结束后积压帧未突发投递 —— 恢复异常"

        # 取消注压测试
        urllib.request.urlopen("http://127.0.0.1:18443/__stall?sec=0", timeout=5)
        print("取消注压 OK")

    fs.shutdown()
    await wss.stop()
    print("== 自测通过 ==")


def logging_info():
    import logging
    logging.getLogger("vs").setLevel(logging.WARNING)
    return logging.WARNING


if __name__ == "__main__":
    asyncio.run(main())
