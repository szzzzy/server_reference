# -*- coding: utf-8 -*-
"""纯标准库 MQTT 3.1.1 子集:broker + client。

覆盖本虚拟测试所需子集:CONNECT/CONNACK、SUBSCRIBE/SUBACK、PUBLISH(QoS0/1)+PUBACK、
PINGREQ/PINGRESP、DISCONNECT、keepalive;wildcard(+/#)订阅匹配;clean_session=false 会话恢复。
不实现:retain 语义、QoS2、will、LWT 自定义(需要时另加)。
"""
import asyncio
import logging
import struct
import time

log = logging.getLogger("vs.mqtt_pure")

# 控制包类型
CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
SUBSCRIBE = 8
SUBACK = 9
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14


class MqttError(Exception):
    pass


def enc_str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def dec_str(buf, off):
    l = struct.unpack_from(">H", buf, off)[0]
    return buf[off + 2: off + 2 + l].decode("utf-8", errors="replace"), off + 2 + l


def enc_remaining(n):
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n:
            d |= 0x80
        out.append(d)
        if not n:
            return bytes(out)


def topic_match(filt, topic):
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part == "+":
            continue
        if part != t[i]:
            return False
    return len(f) == len(t)


# ---------------- Wire 读写的底层封装 ----------------

class PacketIO:
    """按包读写 TCP 流(固定头 + varint 剩余长度 + payload)。"""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def read_packet(self):
        first = await self.reader.readexactly(1)
        if not first:
            return None
        b0 = first[0]
        mult, rem = 1, 0
        for _ in range(4):
            b = (await self.reader.readexactly(1))[0]
            rem += (b & 0x7F) * mult
            if not (b & 0x80):
                break
            mult *= 128
        payload = await self.reader.readexactly(rem) if rem else b""
        return b0, payload

    async def write_packet(self, b0, payload=b""):
        self.writer.write(bytes([b0]) + enc_remaining(len(payload)) + payload)
        await self.writer.drain()

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


# ---------------- Broker ----------------

class BrokerSession:
    def __init__(self, client_id):
        self.client_id = client_id
        self.subs = []            # [(filter, qos)]
        self.conn = None
        self.last = time.monotonic()


class MqttBroker:
    def __init__(self, host="0.0.0.0", port=1883, ssl_ctx=None):
        self.host, self.port, self.ssl_ctx = host, port, ssl_ctx
        self.sessions = {}        # client_id -> BrokerSession
        self.server = None
        self._pids = 0
        self.on_device_connect = None   # 可选回调(client_id, clean): 设备 MQTT 重连(重启)事件

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, self.host, self.port, ssl=self.ssl_ctx, limit=1 << 20)
        log.info("broker: %s:%s (TLS=%s)", self.host, self.port, self.ssl_ctx is not None)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def _next_pid(self):
        self._pids = (self._pids % 65535) + 1
        return self._pids

    # ---------- 连接 ----------

    async def _handle(self, reader, writer):
        io = PacketIO(reader, writer)
        sess = None
        try:
            b0, payload = await io.read_packet()
            if b0 >> 4 != CONNECT:
                await io.write_packet(CONNACK << 4, struct.pack(">BB", 0, 4))
                return
            # 解析 CONNECT
            proto, off = dec_str(payload, 0)
            level, flags, keepalive = struct.unpack_from(">BBH", payload, off)
            off += 4
            client_id, off = dec_str(payload, off)
            clean = bool(flags & 0x02)
            # username/password 可存在,忽略
            sess = self.sessions.get(client_id)
            if sess is None:
                sess = BrokerSession(client_id)
                self.sessions[client_id] = sess
            if clean and sess.subs:
                sess.subs = []
            sess.conn = io
            sess.last = time.monotonic()
            await io.write_packet(CONNACK << 4, struct.pack(">BB", 0, 0))
            log.info("broker: CONNACK %s (clean=%s, keepalive=%s)", client_id, clean, keepalive)
            # 设备(MQTT 客户端 esp*)重新 CONNECT = 设备网络栈/整机重启(旧 WSS 连接
            # 可能在服务器侧残留) → 通知上层"设备重启,会话应重置为待机(需重新唤醒)"
            if self.on_device_connect is not None and client_id.lower().startswith("esp"):
                try:
                    self.on_device_connect(client_id, clean)
                except Exception as exc:
                    log.warning("on_device_connect 回调失败: %s", exc)
            while True:
                b0, payload = await io.read_packet()
                if b0 is None:
                    break
                sess.last = time.monotonic()
                await self._dispatch(sess, b0, payload)
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        except MqttError as e:
            log.warning("broker 协议错误: %s", e)
        finally:
            if sess is not None and sess.conn is io:
                sess.conn = None
        log.info("broker: 连接断开 %s", sess.client_id if sess else "?")

    async def _dispatch(self, sess, b0, payload):
        ptype = b0 >> 4
        if ptype == PUBLISH:
            flags = (b0 & 0x0F)
            qos = (flags >> 1) & 0x03
            topic, off = dec_str(payload, 0)
            pid = None
            if qos >= 1:
                pid = struct.unpack_from(">H", payload, off)[0]
                off += 2
            body = payload[off:]
            if qos == 2:
                raise MqttError("QoS2 未实现")
            if qos == 1:
                await sess.conn.write_packet(PUBACK << 4, struct.pack(">H", pid))
            await self._route(topic, body, max(qos, 0))
        elif ptype == SUBSCRIBE:
            pid = struct.unpack_from(">H", payload, 0)[0]
            off, codes = 2, []
            while off < len(payload):
                f, off = dec_str(payload, off)
                s_qos = payload[off]
                off += 1
                sess.subs = [s for s in sess.subs if s[0] != f] + [(f, s_qos & 0x03)]
                codes.append(s_qos & 0x03)
            await sess.conn.write_packet(SUBACK << 4, struct.pack(">H", pid) + bytes(codes))
            log.info("broker: SUBACK %s %s", sess.client_id, codes)
        elif ptype == PINGREQ:
            await sess.conn.write_packet(PINGRESP << 4)
        elif ptype == DISCONNECT:
            sess.conn.close()
        else:
            log.debug("broker: 忽略类型 %d", ptype)

    async def _route(self, topic, body, qos):
        for sess in self.sessions.values():
            if sess.conn is None:
                continue
            for f, s_qos in sess.subs:
                if topic_match(f, topic):
                    d_qos = min(qos, s_qos)
                    if d_qos == 0:
                        await sess.conn.write_packet(PUBLISH << 4, enc_str(topic) + body)
                    else:
                        pid = self._next_pid()
                        hdr = enc_str(topic) + struct.pack(">H", pid)
                        b0 = (PUBLISH << 4) | 0x02
                        await sess.conn.write_packet(b0, hdr + body)
                    log.info("broker 路由: %s (%d B, qos=%d→%d) → %s",
                             topic, len(body), qos, d_qos, sess.client_id)
                    break
        log.debug("broker: 路由 %s (%d B) → %s", topic, len(body),
                  [s.client_id for s in self.sessions.values() if s.conn])


# ---------------- Client ----------------

class MqttPClient:
    """异步 MQTT 客户端(QoS0/1)。on_message(topic, payload, qos) 在 reader 任务中同步调用。"""

    def __init__(self, client_id, host="127.0.0.1", port=1883, keepalive=60,
                 clean_session=False, on_message=None, ssl_ctx=None):
        self.client_id = client_id
        self.host, self.port = host, port
        self.keepalive, self.clean = keepalive, clean_session
        self.on_message = on_message
        self.ssl_ctx = ssl_ctx
        self.reader = self.writer = self._io = None
        self._pid = 0
        self._pubacks = {}
        self._subacks = {}
        self._connected = asyncio.Event()
        self._close_evt = asyncio.Event()
        self._tasks = []

    def _next_pid(self):
        self._pid = (self._pid % 65535) + 1
        return self._pid

    @property
    def connected(self):
        return self._connected.is_set()

    async def connect(self, timeout=10):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=self.ssl_ctx), timeout=timeout)
        self._io = PacketIO(self.reader, self.writer)
        flags = 0x02 if self.clean else 0x00
        payload = enc_str("MQTT") + struct.pack(">BBH", 4, flags, self.keepalive) + enc_str(self.client_id)
        await self._io.write_packet(CONNECT << 4, payload)
        self._reader_task = asyncio.create_task(self._read_loop())
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        self._tasks.append(asyncio.create_task(self._ping_loop()))
        return self

    async def _read_loop(self):
        try:
            while True:
                b0, payload = await self._io.read_packet()
                if b0 is None:
                    break
                ptype = b0 >> 4
                if ptype == CONNACK:
                    rc = payload[1]
                    if rc != 0:
                        raise MqttError(f"CONNACK rc={rc}")
                    self._connected.set()
                elif ptype == PUBLISH:
                    qos = (b0 & 0x06) >> 1
                    topic, off = dec_str(payload, 0)
                    pid = None
                    if qos >= 1:
                        pid = struct.unpack_from(">H", payload, off)[0]
                        off += 2
                    body = payload[off:]
                    if qos == 1:
                        await self._io.write_packet(PUBACK << 4, struct.pack(">H", pid))
                    if self.on_message:
                        self.on_message(topic, body, qos)
                elif ptype == PUBACK:
                    pid = struct.unpack_from(">H", payload, 0)[0]
                    ev = self._pubacks.pop(pid, None)
                    if ev:
                        ev.set_result(True)
                elif ptype == SUBACK:
                    pid = struct.unpack_from(">H", payload, 0)[0]
                    ev = self._subacks.pop(pid, None)
                    if ev:
                        ev.set_result(list(payload[2:]))
                elif ptype == PINGRESP:
                    pass
                elif ptype == DISCONNECT:
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        finally:
            self._connected.clear()
            self._close_evt.set()

    async def _ping_loop(self):
        try:
            while not self._close_evt.is_set():
                await asyncio.sleep(self.keepalive / 2)
                if self._io is not None:
                    try:
                        await self._io.write_packet(PINGREQ << 4)
                    except OSError:
                        pass
        except asyncio.CancelledError:
            pass

    async def subscribe(self, topics):
        """topics: [(filter, qos), ...] → 返回 granted qos 列表"""
        pid = self._next_pid()
        payload = struct.pack(">H", pid) + b"".join(enc_str(f) + bytes([q]) for f, q in topics)
        await self._io.write_packet(SUBSCRIBE << 4 | 0x02, payload)
        ev = asyncio.get_running_loop().create_future()
        self._subacks[pid] = ev
        return await asyncio.wait_for(ev, timeout=10)

    async def publish(self, topic, payload, qos=1):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if qos == 0:
            await self._io.write_packet(PUBLISH << 4, enc_str(topic) + payload)
            return None
        pid = self._next_pid()
        ev = asyncio.get_running_loop().create_future()
        self._pubacks[pid] = ev
        b0 = (PUBLISH << 4) | 0x02
        await self._io.write_packet(b0, enc_str(topic) + struct.pack(">H", pid) + payload)
        await asyncio.wait_for(ev, timeout=10)
        return pid

    async def close(self):
        self._close_evt.set()
        if self._io is not None:
            try:
                await self._io.write_packet(DISCONNECT << 4)
            except (OSError, asyncio.IncompleteReadError):
                pass
            self._io.close()
        for t in getattr(self, "_tasks", []):
            t.cancel()
        self._connected.clear()
