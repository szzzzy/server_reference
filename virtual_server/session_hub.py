# -*- coding: utf-8 -*-
"""统一会话中心:设备注册/状态追踪/事件去重/隔离记录(独立模块,不依赖现有代码)。"""
import logging
import threading
import time

from common import append_jsonl, iso_now, ts_now

log = logging.getLogger("vs.hub")

OTA_STATES = {
    "accepted", "downloading", "verifying", "rebooting",
    "booted_pending_verify", "succeeded", "failed", "rolled_back", "deferred",
}
AUDIO_STATES = {"accepted", "downloading", "verifying", "ready", "failed"}


class DeviceSession:
    def __init__(self, device_id, events_path):
        self.device_id = device_id
        self.events_path = events_path
        self.first_seen = ts_now()
        self.last_seen = ts_now()
        self.last_ip = ""
        self.last_transport = ""
        self.lock = threading.Lock()
        self.ota_state = ""
        self.ota_last_event_id = ""          # 幂等去重
        self.ota_events = []
        self.audio_state = ""
        self.audio_events = []
        self.vstatus_lines = []
        self.ota_progress = 0
        self.total_audio_bytes_up = 0
        self.total_pcm_frames = 0
        self.voice_seq_errors = 0

    def touch(self, ip="", transport=""):
        self.last_seen = ts_now()
        if ip:
            self.last_ip = ip
        if transport:
            self.last_transport = transport

    def record_event(self, kind, payload):
        with self.lock:
            self.touch()
            row = dict(payload)
            row["_kind"] = kind
            row["_time"] = iso_now()
            append_jsonl(self.events_path, row)
            if kind == "ota_status":
                self._handle_ota_status(row)
            elif kind == "audio_status":
                self._handle_audio_status(row)
            elif kind == "vstatus":
                self.vstatus_lines.append(row.get("text", ""))
                if len(self.vstatus_lines) > 50:
                    self.vstatus_lines.pop(0)
            elif kind == "voice_frame":
                self.total_pcm_frames += 1
                self.total_audio_bytes_up += row.get("bytes", 0)

    def _handle_ota_status(self, row):
        event_id = row.get("event_id", "")
        if event_id and event_id == self.ota_last_event_id:
            return  # 重复事件(设备重连重发),已处理
        if event_id:
            self.ota_last_event_id = event_id
        state = row.get("state", "")
        if state in OTA_STATES:
            self.ota_state = state
            if state == "downloading":
                self.ota_progress = row.get("progress_percent", self.ota_progress)
            self.ota_events.append({"state": state, "t": iso_now(), "attempt": row.get("attempt")})
            if len(self.ota_events) > 100:
                self.ota_events.pop(0)

    def _handle_audio_status(self, row):
        state = row.get("state", "")
        if state in AUDIO_STATES:
            self.audio_state = state
            self.audio_events.append({"state": state, "t": iso_now()})
            if len(self.audio_events) > 100:
                self.audio_events.pop(0)

    def summary(self):
        return {
            "device_id": self.device_id,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "ip": self.last_ip, "transport": self.last_transport,
            "ota_state": self.ota_state, "ota_progress": self.ota_progress,
            "audio_state": self.audio_state,
            "pcm_frames": self.total_pcm_frames, "audio_bytes_up": self.total_audio_bytes_up,
            "ota_events": self.ota_events, "audio_events": self.audio_events,
        }


class SessionHub:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.events_path = run_dir / "events.jsonl"
        self.devices = {}
        self.lock = threading.Lock()

    def get(self, device_id):
        with self.lock:
            dev = self.devices.get(device_id)
            if dev is None:
                dev = DeviceSession(device_id, self.events_path)
                self.devices[device_id] = dev
                log.info("设备会话建立: %s", device_id)
            return dev

    def touch(self, device_id, ip="", transport=""):
        self.get(device_id).touch(ip, transport)

    def record(self, device_id, kind, payload):
        self.get(device_id).record_event(kind, payload)

    def snapshot(self):
        with self.lock:
            return {d: s.summary() for d, s in self.devices.items()}

    def summary_text(self):
        lines = []
        for d, s in sorted((d, s.summary()) for d, s in self.devices.items()):
            lines.append(
                f"{d}  ota={s['ota_state']}({s['ota_progress']}%)  audio={s['audio_state']}  "
                f"pcm_frames={s['pcm_frames']}  ip={s['ip']}  @{s['last_seen']}"
            )
        return "\n".join(lines) if lines else "(尚无设备在线)"
