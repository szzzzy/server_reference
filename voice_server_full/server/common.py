# -*- coding: utf-8 -*-
"""虚拟测试服务器:公共工具(配置/日志/校验/PCM1 帧编解码)。"""
import hashlib
import io
import json
import logging
import math
import socket
import struct
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def project_root():
    # 独立目录:network/server 位于项目根下两级
    return HERE.parents[1]


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def resolve_path(config_root, p):
    if not p:
        return None
    p = Path(p)
    return p if p.is_absolute() else (config_root / p).resolve()


def detect_ip():
    """取"默认路由对应的出口 IPv4"(多网卡/残留旧地址时不选错)。
    优先 UDP 探测路由(不实际发包),失败则回退 getaddrinfo。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        finally:
            s.close()
    except OSError:
        pass
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if ":" not in addr and not addr.startswith("127."):
                ips.append(addr)
    except OSError:
        pass
    return ips[0] if ips else "127.0.0.1"


def new_run_dir(base):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    rd = base / datetime.now().strftime("%Y%m%d_%H%M%S")
    rd.mkdir(parents=True, exist_ok=True)
    return rd


def setup_logging(run_dir, name="virtual_server", level=logging.INFO):
    handlers = [logging.StreamHandler()]
    try:
        fh = logging.FileHandler(run_dir / f"{name}.log", encoding="utf-8")
        handlers.append(fh)
    except OSError:
        pass
    logging.basicConfig(
        level=level, handlers=handlers,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    return logging.getLogger(name)


def append_jsonl(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def ts_now():
    return int(time.time())


def iso_now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------- 版本比较 ------------------

def version_tuple(v):
    parts = str(v).strip().split(".")
    out = []
    for p in parts:
        if not p.isdigit():
            raise ValueError(f"版本号须为 x.y.z 纯数字: {v!r}")
        out.append(int(p))
    return tuple(out + [0] * (3 - len(out)))[:3]


def version_gt(a, b):
    return version_tuple(a) > version_tuple(b)


# ---------------- PCM1 帧(与固件/串口版一致)-----------------

PCM1_MAGIC = b"PCM1"
PCM1_HEADER_LEN = 16
PCM1_FRAME_MS = 20
PCM1_SAMPLE_RATE = 16000


def sum8(data):
    return sum(data) & 0xFF


def pcm1_build(seq, samples, dbfs_x100, sample_rate=PCM1_SAMPLE_RATE):
    """samples: bytes(PCM16 LE);返回完整 PCM1 帧(16B 头 + payload)。"""
    payload = bytes(samples)
    header = (
        PCM1_MAGIC
        + struct.pack("<I", seq & 0xFFFFFFFF)
        + struct.pack("<H", len(payload))
        + struct.pack("<h", int(dbfs_x100))
        + b"\x00\x00\x00"
        + bytes([sum8(payload)])
    )
    assert len(header) == PCM1_HEADER_LEN
    return header + payload


def pcm1_parse(frame):
    """解析 PCM1 帧;返回 dict(seq, bytes_len, dbfs_x100, payload) 或 None(非法)。"""
    if len(frame) < PCM1_HEADER_LEN:
        return None
    if frame[0:4] != PCM1_MAGIC:
        return None
    seq, nbytes, dbfs = struct.unpack_from("<IHh", frame, 4)
    expected = frame[15]
    payload = frame[PCM1_HEADER_LEN:]
    if nbytes != len(payload) or sum8(payload) != expected:
        return None
    return {"seq": seq, "bytes_len": nbytes, "dbfs_x100": dbfs, "payload": payload}


def rms_dbfs(byte_samples):
    """纯 Python RMS→dBFS(避免 numpy 依赖)。"""
    if not byte_samples:
        return -120.0
    import array
    samples = array.array("h")
    samples.frombytes(byte_samples)
    if samples.itemsize == 2:
        # Windows/Linux x86 均为小端,PCM 16k LE 直接可用
        pass
    ss = 0.0
    for v in samples:
        x = v / 32768.0
        ss += x * x
    n = len(samples)
    if n == 0:
        return -120.0
    rms = (ss / n) ** 0.5
    if rms <= 1e-12:
        return -120.0
    return 20.0 * math.log10(rms)


def sha256_hex(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_test_wav(path, sample_rate=16000):
    """读取 16k/mono/16bit wav;返回字节串。非 16k 时抛错(测试样本均为 16k)。"""
    import wave
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1, "需要单声道"
        assert w.getsampwidth() == 2, "需要 16bit"
        if w.getframerate() != sample_rate:
            raise ValueError(f"采样率 {w.getframerate()} != {sample_rate}")
        data = w.readframes(w.getnframes())
    return data


async def wss_connect(uri, ssl_ctx, headers=None, max_size=16 * 1024 * 1024):
    """websockets 12(legacy: extra_headers)与 17+(new: additional_headers)兼容。"""
    import websockets
    major = int(websockets.__version__.split(".")[0])
    kwargs = {"ssl": ssl_ctx, "max_size": max_size}
    if headers:
        if major >= 13:
            kwargs["additional_headers"] = headers
        else:
            kwargs["extra_headers"] = headers
    return await websockets.connect(uri, **kwargs)


def make_sine_wav(path, seconds=2.0, freq=440.0, sample_rate=16000):
    import wave
    import struct as st
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * freq * i / sample_rate))
            frames += st.pack("<h", v)
        w.writeframes(bytes(frames))
    return path
