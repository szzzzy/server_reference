# -*- coding: utf-8 -*-
"""最小判定实验: Windows proactor loop 下 transport.pause_reading() 是否
1) 停止 data_received 投递;2) 压住对端发送(客户端 send 阻塞/超时)。"""
import asyncio
import socket
import time


class P:
    def __init__(self):
        self.got = 0
        self.paused = False

    def connection_made(self, tr):
        self.tr = tr
        tr.pause_reading()          # 连接建好即暂停读取
        self.paused_at = time.time()

    def data_received(self, data):
        self.got += len(data)
        print("  [server] data_received +%dB total=%dB (+%.1fs)" %
              (len(data), self.got, time.time() - self.paused_at))

    def connection_lost(self, exc):
        print("  [server] connection_lost", exc)


async def main():
    loop = asyncio.get_running_loop()
    print("event loop policy:", type(loop).__name__)
    server = await loop.create_server(P, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"hello")
    await asyncio.sleep(0.3)
    print("client: 开始灌 4MB, 观察 send 是否阻塞:")
    t0 = time.time()
    try:
        chunk = b"x" * (64 * 1024)
        n = 0
        while n < 4 * 1024 * 1024:
            await asyncio.wait_for(w.write(chunk) or w.drain(), timeout=2.0)
            n += len(chunk)
    except asyncio.TimeoutError:
        print("  [client] send 阻塞超时! 说明背压已压到发送端 (灌了 %d KB / %.1fs)" %
              (n // 1024, time.time() - t0))
    else:
        print("  [client] 4MB 全部发送未阻塞 (%.1fs) → pause_reading 未生效" %
              (time.time() - t0))
    await asyncio.sleep(0.5)
    server.close()


asyncio.run(main())
