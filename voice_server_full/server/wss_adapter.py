# -*- coding: utf-8 -*-
"""WSS 语音服务适配器:双向实时语音。

- 端点:wss://<host>:<port>/voice,Bearer token 鉴权(RFC6455);
- 上行:二进制帧 = PCM1(16B 头 + 640B,20ms @16kHz);文本命令;
- 文件推送 FILE_SEND:BEGIN FILE <size> <name> → 二进制×N → END <bytes>;
- 下行:文本命令(SPKS/SPKV/SPKE/SPKT/MIC_START…)+ 二进制 PCM16;
全异步,独立模块;语音语义交给 voice_bridge 引擎。
"""
import asyncio
import logging
import re
from pathlib import Path

from common import pcm1_parse
from voice_bridge import COMMANDS

log = logging.getLogger("vs.wss")

MAX_WAV_SIZE = 8 * 1024 * 1024   # 固件协议:单文件 ≤ 8 MiB
FRAME_LIMIT = 1200                # 固件协议:单帧 ≤ 1200 B


def _path(ws):
    req = getattr(ws, "request", None)
    if req is not None and hasattr(req, "path"):
        return req.path
    return getattr(ws, "path", None)


def _headers(ws):
    """兼容 websockets 12(legacy: request_headers)与 12+(request.headers)。"""
    req = getattr(ws, "request", None)
    h = getattr(req, "headers", None) if req is not None else None
    if h is None:
        h = getattr(ws, "request_headers", None)
    return h or {}


def _header_value(ws, name):
    for k, v in _headers(ws).items():
        if k.lower() == name.lower():
            return v
    return ""


def _auth_header(ws):
    return _header_value(ws, "Authorization")


def _map_uri(uri):
    """SD:/x/y → /sdcard/x/y;SPIFFS:/x/y → /spiffs/x/y"""
    if uri.startswith("SD:"):
        return "/sdcard" + uri[3:]
    if uri.startswith("SPIFFS:"):
        return "/spiffs" + uri[7:]
    return uri


class FileReceiver:
    """收集 BEGIN FILE…END 之间的二进制帧。"""

    def __init__(self, size, name, save_dir, ws):
        self.size = int(size)
        self.name = Path(name).name
        self.save_dir = Path(save_dir)
        self.ws = ws
        self.buf = bytearray()
        self.done = False

    def add(self, data):
        self.buf.extend(data)

    def finish(self, reported_bytes):
        ok = (
            Path(self.name).suffix.lower() == ".wav"
            and self.size <= MAX_WAV_SIZE
            and reported_bytes == self.size
            and len(self.buf) == self.size
        )
        if not ok:
            return None, f"size mismatch END={reported_bytes} got={len(self.buf)} size={self.size}"
        self.save_dir.mkdir(parents=True, exist_ok=True)
        target = self.save_dir / self.name
        target.write_bytes(bytes(self.buf))
        self.done = True
        return target, None


class WssAdapter:
    def __init__(self, cfg, hub, engine, run_dir):
        self.cfg = cfg
        self.hub = hub
        self.engine = engine
        self.run_dir = run_dir
        self.ws_server = None
        self.clients = {}

    async def start(self):
        import websockets
        import ssl

        w = self.cfg.get("wss", {})
        host = w.get("host", "0.0.0.0")
        port = int(w.get("port", 9443))
        path = w.get("path", "/voice")
        ctx = None
        if w.get("tls", True):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=self.cfg["paths"]["server_cert"],
                                keyfile=self.cfg["paths"]["server_key"])
        self.ws_server = await websockets.serve(
            self._handler, host, port, ssl=ctx, max_size=16 * 1024 * 1024,
            ping_interval=float(w.get("ping_interval", 60)),
            ping_timeout=float(w.get("ping_timeout", 30)),
        )
        log.info("WSS 服务已启动: wss://%s:%s%s (TLS=%s, ping=%ss/%ss)",
                 host, port, path, ctx is not None,
                 w.get("ping_interval", 60), w.get("ping_timeout", 30))

    async def stop(self):
        if self.ws_server:
            self.ws_server.close()
            await self.ws_server.wait_closed()

    async def send_text(self, ws, text):
        await ws.send(text)
        log.info("WSS -> %s", text)

    async def send_pcm(self, ws, pcm_bytes):
        await ws.send(pcm_bytes)

    # ---------------- 连接处理 ----------------

    async def _handler(self, ws):
        token = self.cfg.get("wss", {}).get("token", "")
        auth = _auth_header(ws)
        if not auth.startswith("Bearer ") or auth[7:].strip() != token:
            log.warning("WSS 鉴权失败: %r", auth[:40])
            await ws.close(code=4401, reason="unauthorized")
            return
        if _path(ws) != self.cfg.get("wss", {}).get("path", "/voice"):
            await ws.close(code=4404, reason="not found")
            return
        ip = getattr(ws, "remote_address", ("?",))[0]
        log.info("WSS 设备接入: %s", ip)
        self.hub.touch("wss-device", ip=str(ip), transport="wss")
        self.clients[ws] = ip
        file_rx = None
        import websockets
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    if file_rx is not None:
                        file_rx.add(message)
                        continue
                    self._dispatch_bytes(message)
                else:
                    file_rx = await self._dispatch_text(ws, message, file_rx)
        except websockets.exceptions.ConnectionClosed as e:
            # 对端(板卡)异常断开:未发 close 帧(掉WiFi/重启/复位),联调常见,仅记录不刷错误
            log.info("WSS 设备异常断开(未收close帧): %s (%s)", ip, e)
        finally:
            self.clients.pop(ws, None)
            log.info("WSS 设备断开: %s", ip)

    def _dispatch_bytes(self, message):
        if len(message) > FRAME_LIMIT:
            self.engine.on_bad_frame(f"frame {len(message)}B > {FRAME_LIMIT}B")
            return
        pcm1 = pcm1_parse(message)
        if pcm1 is None:
            self.engine.on_bad_frame("magic/checksum/len 不合法")
            return
        self.engine.on_frame(pcm1, raw=message)

    async def _dispatch_text(self, ws, text, file_rx):
        t = text.strip()
        if not t:
            return file_rx
        if t.startswith("BEGIN FILE "):
            m = re.match(r"BEGIN FILE (\d+) (\S+)", t)
            if not m:
                await ws.send("ERROR bad_uri")
                return None
            size, name = m.group(1), _map_uri(m.group(2))
            save_dir = self.run_dir / "files_send"
            try:
                return FileReceiver(size, name, save_dir, ws)
            except (ValueError, OSError):
                await ws.send("ERROR file_open_failed")
                return None
        if t.startswith("END "):
            if file_rx is None:
                return None
            target, err = file_rx.finish(int(t.split()[1]) if len(t.split()) > 1 else -1)
            if err:
                log.warning("FILE_SEND 失败: %s", err)
                await ws.send(f"ERROR {err}")
            else:
                log.info("FILE_SEND 完成: %s (%d B)", target, target.stat().st_size)
                self.engine.file_uploads.append(str(target))
                await ws.send("FILE_OK")
            return None
        if t.startswith("ERROR"):
            log.warning("设备上报错误: %s", t)
            return file_rx
        if t.upper() in COMMANDS or t.split()[0].upper() in COMMANDS:
            self.engine.on_command(t)
            return file_rx
        log.debug("WSS 未知文本: %s", t[:80])
        return file_rx

    async def broadcast_text(self, text):
        if not self.clients:
            log.info("WSS 无在线客户端,忽略下发: %s", text)
            return False
        for ws in list(self.clients):
            try:
                await ws.send(text)
            except Exception:
                pass
        log.info("WSS -> 全部客户端: %s", text)
        return True

    async def broadcast_pcm(self, pcm_bytes):
        if not self.clients:
            log.debug("WSS 无在线客户端,忽略 PCM %d B", len(pcm_bytes))
            return False
        for ws in list(self.clients):
            try:
                await ws.send(pcm_bytes)
            except Exception:
                pass
        return True
