# -*- coding: utf-8 -*-
"""字节流兼容层:把 WSS 推送的 PCM1 帧转成"串口式"字节流,
使现有 board_serial_asr_test.read_frame / capture_until_endpoint 可直接复用。

接口对齐 pyserial:read(n) / reset_input_buffer() / flush() / is_open / close()。
"""
import threading
import time


class RingBuffer:
    def __init__(self, max_bytes=512 * 1024, read_timeout=0.25):
        self.max_bytes = max_bytes
        self.read_timeout = read_timeout
        self._buf = bytearray()
        self._closed = False
        self._cond = threading.Condition()
        self.log = None
        self._last_real = time.monotonic()
        self._silence_window_s = 0.0     # 0 = 不合成静音
        self._silence_seq = 0
        self._real_bytes = 0

    def enable_auto_silence(self, window_s):
        """真实帧停止后,在 window_s 秒窗口内补合成静音 PCM1 帧,
        使现有 capture_until_endpoint 的"尾部静音"端点逻辑依然生效。"""
        self._silence_window_s = float(window_s)

    def _maybe_silence(self):
        if self._silence_window_s <= 0:
            return False
        gap = time.monotonic() - self._last_real
        if gap > self._silence_window_s or gap < 0.08:
            # 窗口外不补;0.08s 内说明真实帧仍在以 20ms 节奏到达,不插帧
            return False
        from common import pcm1_build
        self._silence_seq += 1
        self._buf.extend(pcm1_build(self._silence_seq, b"\x00" * 640, -12000))
        return True

    # ---------------- 生产者(WSS 任务)----------------

    def append(self, data, real=True):
        if not data:
            return
        with self._cond:
            if self._closed:
                return
            self._buf.extend(data)
            if real:
                self._last_real = time.monotonic()
                self._real_bytes += len(data)
            if len(self._buf) > self.max_bytes:
                del self._buf[:len(self._buf) - self.max_bytes]
            self._cond.notify_all()

    @property
    def real_bytes_total(self):
        with self._cond:
            return self._real_bytes

    # ---------------- 消费者(采集线程,兼容 serial)----------------

    def read(self, n):
        """阻塞直到凑满 n 字节或 read_timeout 到期;超时返回已有数据(可能为空)。"""
        if n <= 0:
            return b""
        deadline = time.monotonic() + self.read_timeout
        with self._cond:
            while True:
                if self._closed:
                    return bytes(self._buf)
                if len(self._buf) >= n:
                    out = bytes(self._buf[:n])
                    del self._buf[:n]
                    return out
                if not self._buf:
                    # 真实帧停流后的窗口内,补合成静音帧(端点判定需要)
                    self._maybe_silence()
                    if len(self._buf) >= n:
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    out = bytes(self._buf)
                    del self._buf[:]
                    return out
                self._cond.wait(timeout=min(remaining, 0.05))

    def read1(self):
        return self.read(1)

    def reset_input_buffer(self):
        with self._cond:
            self._buf.clear()

    def flush(self):
        pass

    @property
    def is_open(self):
        return not self._closed

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def buffered_bytes(self):
        with self._cond:
            return len(self._buf)
