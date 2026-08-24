# -*- coding: utf-8 -*-
"""语音桥:WSS 上行 PCM1 帧/命令 → 语音引擎;下行命令/PCM → WSS。

模式(独立构建,不修改任何现有文件):
  stub:统计型引擎(帧率/seq 连续/dBFS/命令回执),秒级启动,用于协议链路验证;
  real:真实推理引擎(预留接口)——由后续独立模块 voice_bridge_real.py 实现,
        经 import 直接复用现有 board_serial_asr_test / asr_eval_core / realtime_pipeline,
        不在现有文件上做任何插入或修改。

统一的 Engine 接口:on_frame(pcm1:dict) / on_command(text:str) / snapshot() -> dict
"""
import logging
import threading
import time

from common import pcm1_parse, rms_dbfs

log = logging.getLogger("vs.voice")

COMMANDS = {
    "MIC_START", "MIC_STOP", "MICW", "SPKS", "SPKV", "SPKE", "SPKT", "MICS", "FILE_SEND",
}

# 下行命令(服务器→设备),均为纯文本,参数用空格分隔
DOWNLINK_TEXT = {
    "MIC_START": ("MIC_START", None),
    "MIC_STOP": ("MIC_STOP", None),
    "MICW": ("MICW", None),
    "SPKE": ("SPKE", None),
    "SPKT": ("SPKT", None),
    "SPKS": ("SPKS {rate}", "rate"),
    "SPKV": ("SPKV {percent}", "percent"),
    "MICS": ("MICS {bg}", "bg"),
}


class StubVoiceEngine:
    """状态统计型引擎:不加载模型,验证 WSS 语音链路与协议。"""

    def __init__(self, hub, device_id="wss-device"):
        self.hub = hub
        self.device_id = device_id
        self.lock = threading.Lock()
        self.frames = 0
        self.frame_bytes = 0
        self.seq_gaps = 0
        self.last_seq = None
        self.dbfs_min = 0.0
        self.dbfs_max = -120.0
        self.commands = []
        self.file_uploads = []
        self.session_start = time.monotonic()

    def on_frame(self, pcm1, raw=None):
        with self.lock:
            seq = pcm1["seq"]
            if self.last_seq is not None and seq != (self.last_seq + 1) & 0xFFFFFFFF:
                self.seq_gaps += 1
            self.last_seq = seq
            self.frames += 1
            self.frame_bytes += pcm1["bytes_len"]
            db = pcm1["dbfs_x100"] / 100.0
            self.dbfs_min = min(self.dbfs_min, db)
            self.dbfs_max = max(self.dbfs_max, db)
        self.hub.record(self.device_id, "voice_frame", {
            "seq": seq, "bytes": pcm1["bytes_len"], "dbfs": db,
        })

    def on_command(self, text):
        with self.lock:
            self.commands.append(text.strip())
            if len(self.commands) > 200:
                self.commands.pop(0)
        log.info("WSS 命令<- 设备侧文本: %s", text.strip()[:80])
        return True

    def on_bad_frame(self, reason):
        log.warning("WSS 非法 PCM1 帧: %s", reason)

    def snapshot(self):
        with self.lock:
            elapsed = max(time.monotonic() - self.session_start, 1e-6)
            rate = self.frames / elapsed
            return {
                "mode": "stub",
                "frames": self.frames,
                "audio_bytes": self.frame_bytes,
                "effective_fps": round(rate, 1),
                "seq_gaps": self.seq_gaps,
                "dbfs_range": [round(self.dbfs_min, 1), round(self.dbfs_max, 1)],
                "commands_seen": self.commands[-10:],
                "file_uploads": self.file_uploads,
            }


class RealVoiceEnginePlaceholder:
    """真实推理引擎= virtual_server/real_engine.py(独立模块,import 复用现有代码)。"""

    def __init__(self):
        raise NotImplementedError("real 引擎请使用 real_engine.RealVoiceEngine")


def make_engine(mode, hub, cfg=None, run_dir=None, project_root=".", device_id="wss-device"):
    if mode == "stub":
        return StubVoiceEngine(hub, device_id)
    if mode == "real":
        from real_engine import RealVoiceEngine
        if cfg is None or run_dir is None:
            raise ValueError("real 模式需要 cfg/run_dir")
        return RealVoiceEngine(hub, cfg, run_dir, project_root)
    raise ValueError(f"未知 voice.mode: {mode}")
