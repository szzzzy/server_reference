# -*- coding: utf-8 -*-
"""发布清单(固件/音频素材)管理与 OTA/音频应答规则。"""
import logging
from pathlib import Path

from common import version_gt, save_json

log = logging.getLogger("vs.manifest")

# 按固件协议 §2.3:非法状态视为隔离
QUARANTINE_ERROR_CODES = {
    "IMAGE_VALIDATE_FAILED", "HASH_MISMATCH", "IMAGE_HEADER_INVALID",
    "MANIFEST_INVALID", "ARTIFACT_QUARANTINED", "STORAGE_UNAVAILABLE",
    "BOOT_PARTITION_SET_FAILED", "NVS_WRITE_FAILED",
}


class ManifestStore:
    """manifest.json:
    {
      "device": {"product": "julia-ai-device", "hardware_version": "1.0"},
      "firmware": { "artifact_id":..., "version":..., "url":..., "sha256":...,
                    "image_size":..., "security_version":..., "expires_at":...,
                    "force_update": false, "job_id": "..." },
      "audio":    { "audio_id":..., "version":..., "url":..., "sha256":...,
                    "file_size":..., "expires_at":... }
    }
    """

    def __init__(self, path, defaults=None):
        self.path = Path(path)
        self.defaults = defaults or {}
        data = dict(defaults or {})
        if self.path.exists():
            data.update(_load(self.path))
        self.data = data

    def device(self):
        return self.data.get("device", {})

    def firmware(self):
        return self.data.get("firmware", {})

    def audio(self):
        return self.data.get("audio", {})

    def add_quarantine(self, artifact_id, reason):
        q = self.data.setdefault("quarantined", {})
        q[artifact_id] = reason
        save_json(self.path, self.data)
        log.warning("ARTIFACT QUARANTINED: %s (%s)", artifact_id, reason)

    def is_quarantined(self, artifact_id):
        if not artifact_id:
            return False
        q = self.data.get("quarantined", {})
        if artifact_id in q:
            log.warning("artifact %s 已被隔离,拒绝发布: %s", artifact_id, q[artifact_id])
            return True
        return False


def _load(path):
    import json
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_ota_response(req, store, hub):
    """收到 /device/ota/check → 构造 ota_check_response(payload dict)。"""
    default = {
        "type": "ota_check_response", "update": False,
        "request_id": req.get("request_id", ""),
        "device_id": req.get("device_id", ""),
        "product": req.get("product", ""),
        "hardware_version": req.get("hardware_version", ""),
    }
    dev = store.device()
    fw = store.firmware()
    need = (req.get("product") == dev.get("product")
            and req.get("hardware_version") == dev.get("hardware_version")
            and fw)
    if not need:
        log.info("ota_check: 产品/硬件版本不匹配或无发布物 → update=false")
        return default

    current = req.get("current_version", "0.0.0")
    target = fw.get("version", "0.0.0")
    force = bool(fw.get("force_update", False))
    can_update = force or version_gt(target, current)
    if not can_update:
        log.info("ota_check: %s 已是目标版本 %s → update=false", req.get("device_id"), current)
        return default
    if store.is_quarantined(fw.get("artifact_id")):
        return default

    resp = {
        "type": "ota_check_response", "update": True,
        "request_id": req.get("request_id", ""),
        "device_id": req.get("device_id", ""),
        "product": req.get("product", ""),
        "hardware_version": req.get("hardware_version", ""),
        "job_id": fw.get("job_id", ""),
        "artifact_id": fw.get("artifact_id", ""),
        "version": target,
        "url": fw.get("url", ""),
        "sha256": fw.get("sha256", ""),
        "image_size": fw.get("image_size", 0),
        "security_version": fw.get("security_version", 0),
        "expires_at": fw.get("expires_at", 0),
    }
    if "force_update" in fw:
        resp["force_update"] = bool(fw["force_update"])
    log.info("ota_check → update=true version=%s for %s", target, req.get("device_id"))
    return resp


def build_audio_response(req, store):
    default = {
        "type": "audio_check_response", "update": False,
        "request_id": req.get("request_id", ""),
        "device_id": req.get("device_id", ""),
        "product": req.get("product", ""),
    }
    aud = store.audio()
    if not aud:
        return default
    if store.is_quarantined(aud.get("audio_id")):
        return default
    if req.get("current_audio_version") == aud.get("version"):
        return default
    resp = {
        "type": "audio_check_response", "update": True,
        "request_id": req.get("request_id", ""),
        "device_id": req.get("device_id", ""),
        "product": req.get("product", ""),
        "audio_id": aud.get("audio_id", ""),
        "version": aud.get("version", ""),
        "url": aud.get("url", ""),
        "sha256": aud.get("sha256", ""),
        "file_size": aud.get("file_size", 0),
        "expires_at": aud.get("expires_at", 0),
    }
    log.info("audio_check → update=true version=%s for %s", aud.get("version"), req.get("device_id"))
    return resp
