# -*- coding: utf-8 -*-
"""WSS 语音服务适配器:双向实时语音。

- 端点:wss://<host>:<port>/voice,Bearer token 鉴权(RFC6455);
- 上行:二进制帧 = PCM1(16B 头 + 640B,20ms @16kHz);文本命令;
- 文件推送 FILE_SEND:BEGIN FILE <size> <name> → 二进制×N → END <bytes>;
- 下行:文本命令(SPKS/SPKV/SPKE/SPKT/MIC_START…)+ 二进制 PCM16;
全异步,独立模块;语音语义交给 voice_bridge 引擎。
"""
import asyncio
import json
import logging
import re
import socket
import time
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
        # ---- 断联统计与归类(2026-09-02):区分"设备裸断/半开超时/RST"三种亚型 ----
        self._disconnects = 0            # 累计断联次数(全部在线客户端)
        self._last_disconnect_ts = 0.0   # 上次断联时刻(重连耗时报出)
        # ---- 注压测试接口(2026-09-03): 暂停读取上行 → TCP 窗口关闭 → 设备 TX 背压
        #      (WANT_WRITE/EAGAIN),用于验证固件"短暂背压不拆连接"的重试逻辑。
        #      实现要点: 只睡 handler 不够(websockets 协议层会继续读 socket 并缓冲),
        #      必须 pause_reading 暂停底层 transport,数据才会真正压在路径缓冲里。 ----
        self._loop = None                # 主事件循环(stall() 可能从 HTTPS 线程调用)
        self._stall_until = 0.0          # monotonic 时刻;0=未注压
        self._paused = {}                # ws -> True(transport 已被暂停读取)
        self._rcvbuf_orig = {}           # ws -> 原始 SO_RCVBUF(注压结束时恢复)
        self._stall_fut = None           # 当前注压协程的 future(用于取消/覆盖)

    # ---------------- 注压测试接口 ----------------

    def _pause_ws(self, ws):
        """暂停该连接底层读取,并收紧接收缓冲,确保 TCP 窗口尽快关闭、
        背压真实到达对端(设备侧 TLS write 将出现 WANT_WRITE/EAGAIN)。"""
        tr = getattr(ws, "transport", None)
        if tr is None:
            return
        try:
            tr.pause_reading()
            self._paused[ws] = True
        except Exception as e:
            log.warning("WSS 注压: 暂停读取失败: %s", e)
        # 收紧 SO_RCVBUF: 接收窗口收缩 → 对端 sndbuf 快速耗尽,背压尽早出现
        try:
            sock = tr.get_extra_info("socket")
            if sock is not None:
                self._rcvbuf_orig.setdefault(
                    ws, sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        except Exception as e:
            log.debug("WSS 注压: SO_RCVBUF 收紧失败: %s", e)

    def _resume_ws(self, ws):
        if self._paused.pop(ws, None):
            tr = getattr(ws, "transport", None)
            if tr is not None:
                try:
                    tr.resume_reading()
                except Exception as e:
                    log.warning("WSS 注压: 恢复读取失败: %s", e)
        # 恢复接收缓冲
        try:
            orig = self._rcvbuf_orig.pop(ws, None)
            if orig is not None:
                tr = getattr(ws, "transport", None)
                sock = tr.get_extra_info("socket") if tr is not None else None
                if sock is not None:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, orig)
        except Exception as e:
            log.debug("WSS 注压: SO_RCVBUF 恢复失败: %s", e)

    async def _stall_cycle(self, seconds):
        """注压主体(在事件循环里跑): 暂停全部在线连接底层读取 → 等待 → 恢复。"""
        self._stall_until = time.monotonic() + seconds
        for ws in list(self.clients):
            self._pause_ws(ws)
        log.warning("WSS 注压开始: 暂停读取上行 %.2fs(设备 TX 将出现 WANT_WRITE/EAGAIN;"
                    "期间 1006 断联即固件待验行为)", seconds)
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            pass            # 被新注压覆盖/手动取消 → finally 统一恢复
        finally:
            self._stall_until = 0.0
            for ws in list(self._paused):
                self._resume_ws(ws)
            log.info("WSS 注压结束: 恢复全部连接读取(积压帧将突发到达)")

    def stall(self, seconds):
        """注压: 暂停读取全部 WSS 客户端上行 seconds 秒(seconds<=0 = 取消)。

        服务器停止读取 → TCP 窗口关闭 → 设备发送缓冲耗尽 → 设备侧 TLS write 出现
        WANT_WRITE/EAGAIN,与固件此前把 WANT_WRITE 当死连接、直接销毁 WSS 的场景一致。
        注压期间下行(SPKS/PCM/MIC_START 等)与发送保活不受影响。
        注意: 建议注压 5~15s —— 太短(≤4s)可能被路径缓冲吸收不产生 WANT_WRITE,
        太长(≥20s)会触发设备自身 15s/10s 保活(PING 没人应),混淆被测行为。
        """
        seconds = float(seconds or 0)
        if self._loop is None or not self._loop.is_running():
            log.warning("WSS 注压失败: 事件循环未就绪")
            return
        if self._stall_fut is not None:
            self._stall_fut.cancel()
        if seconds <= 0:
            self._stall_until = 0.0
            log.info("WSS 注压取消")
            return
        self._stall_fut = asyncio.run_coroutine_threadsafe(
            self._stall_cycle(seconds), self._loop)

    def stall_remaining(self):
        remain = self._stall_until - time.monotonic()
        return round(remain, 2) if remain > 0 else 0.0

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
        self._loop = asyncio.get_running_loop()
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
        # 连接边界语义: 仅"0→1"(此前无任何客户端)才通知引擎开启新会话。
        # 设备和服务器可能短暂并存新旧两条连接(设备重启时旧连接残留 12s),
        # 若每条新连接都重置会话,会把进行中的唤醒/对话打掉(实测: 设备在线却
        # 被旧连接断联误判 → 唤醒被拒)。重叠连接不打扰进行中的会话。
        was_empty = not self.clients
        self.clients[ws] = ip
        # 断联归类: 若距上次断联很短 → 设备自动重连;与 MQTT CONNACK esp 是否同秒对比可判重启
        if self._last_disconnect_ts:
            log.info("WSS 设备重连: %s (距上次断联 %.1fs)", ip,
                     time.monotonic() - self._last_disconnect_ts)
        if was_empty:
            # 真·新接入: 会话从待机开始(需说唤醒词)
            on_conn = getattr(self.engine, "on_client_connected", None)
            if on_conn is not None:
                try:
                    on_conn(ip)
                except Exception as e:
                    log.warning("引擎 on_client_connected 回调失败: %s", e)
        else:
            log.info("WSS 重叠连接: %s (在线客户端=%d, 不重置会话)",
                     ip, len(self.clients))
        file_rx = None
        conn_started = time.monotonic()
        import websockets
        # ---- 注压测试: 若注压进行中,新连接先暂停底层读取等待注压结束(否则注压期间
        #      新连接会在协议层无阻碍地继续上传,测不到 WANT_WRITE) ----
        if self._stall_until and time.monotonic() < self._stall_until:
            remain = self._stall_until - time.monotonic()
            log.info("WSS 注压中: %s 新连接等待注压结束(%.2fs)", ip, remain)
            self._pause_ws(ws)
            await asyncio.sleep(remain)
            self._resume_ws(ws)
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
            # 对端断连归类(2026-09-02 增强): 打印连接关闭的"类型/code/reason"以区分:
            #   ConnectionClosedError + reason="no close frame received or sent" → 设备裸断
            #     (断电/重启/WiFi闪断,未发 Close 帧就没了 TCP);
            #   ConnectionClosedError + reason 含 "ping timeout" 等 → 半开连接,
            #     由服务器 ping(60s)超时(30s)检测后主动关闭 —— 设备可能早已消失;
            #   ConnectionClosedOK(code=1000) → 正常 Close 握手(目前日志中几乎未出现)。
            log.info("WSS 设备异常断开: %s (type=%s code=%s reason=%r)", ip,
                     type(e).__name__, getattr(e, "code", None), getattr(e, "reason", ""))
        finally:
            self._resume_ws(ws)   # 注压期间断开的连接: 清理暂停状态
            self.clients.pop(ws, None)
            self._disconnects += 1
            self._last_disconnect_ts = time.monotonic()
            # 连接边界语义: 仅"1→0"(再无任何客户端)才通知引擎终止会话;
            # 重叠连接(设备重启后新旧并存)撤离不打扰进行中的会话。
            remain = bool(self.clients)
            log.info("WSS 断联统计: %s 连接时长=%.1fs 在线客户端=%d 累计断联=%d "
                     "(同秒 MQTT esp CONNACK clean=True → 设备重启;仅 WSS 重连 → 单连接掉线)",
                     ip, time.monotonic() - conn_started, len(self.clients), self._disconnects)
            if not remain:
                # 真·全部断开: 会话终止回待机(重连需重新唤醒)+ 清 RingBuffer/历史(见设计文档 §1)
                on_disc = getattr(self.engine, "on_client_disconnected", None)
                if on_disc is not None:
                    try:
                        on_disc(ip)
                    except Exception as e:
                        log.warning("引擎 on_client_disconnected 回调失败: %s", e)
            else:
                log.info("WSS 重叠连接撤离: %s (仍有 %d 个客户端在线, 会话保持)",
                         ip, len(self.clients))

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
        # 设备 FSM 回执(2026-09-02): {"type":"state_ready","interaction_id":"..","state":"S4"}
        # 唤醒协议:wake_detected → 设备完成 S3/S5/S6→S4 迁移后回此消息 → 服务器才播唤醒应答
        if t.startswith("{"):
            try:
                data = json.loads(t)
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("type") == "state_ready":
                cb = getattr(self.engine, "on_state_ready", None)
                if cb is not None:
                    try:
                        cb(t)
                        log.info("WSS 设备状态回执: state=%r", data.get("state"))
                    except Exception as e:
                        log.warning("引擎 on_state_ready 回调失败: %s", e)
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
