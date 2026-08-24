# -*- coding: utf-8 -*-
"""生成虚拟测试产物:固件镜像 + 音频素材,并写出 releases/manifest.json。

用法(在 virtual_server 目录):
  python make_test_artifacts.py                    # 自动取本机 IP
  python make_test_artifacts.py --addr myhost.com  # 指定服务器地址(须与固件 CONFIG_JULIA_SERVER_ADDR 一致)
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import detect_ip, make_sine_wav, sha256_hex

FW_VERSION = "1.1.0"
FW_SIZE = 262144                    # 256 KiB,uint32 正整数,可改
AUDIO_SECONDS = 2.0


def make_firmware(path, version=FW_VERSION, size=FW_SIZE):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"julia-ai\nversion={version}\nproject=julia-ai\n".encode("utf-8")
    body = bytearray(header)
    body.extend(b"\xab" * max(0, size - len(body)))
    body = body[:size]
    path.write_bytes(bytes(body))
    return path, len(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="", help="服务器地址(域名或 IP),默认自动取本机 IP")
    ap.add_argument("--fw-version", default=FW_VERSION)
    ap.add_argument("--fw-size", type=int, default=FW_SIZE)
    ap.add_argument("--audio-seconds", type=float, default=AUDIO_SECONDS)
    args = ap.parse_args()
    addr = args.addr or detect_ip()

    fw_path, fw_size = make_firmware(HERE / "releases/files/fw/app.bin", args.fw_version, args.fw_size)
    wav_path = make_sine_wav(HERE / "releases/files/audio/greeting.wav", seconds=args.audio_seconds)
    wav_size = wav_path.stat().st_size

    manifest = {
        "device": {"product": "julia-ai-device", "hardware_version": "1.0"},
        "firmware": {
            "artifact_id": "release-2025-01",
            "version": args.fw_version,
            "url": f"https://{addr}:8443/fw/app.bin",
            "sha256": sha256_hex(fw_path),
            "image_size": fw_size,
            "security_version": 1,
            "expires_at": int(time.time()) + 90 * 86400,
            "force_update": False,
            "job_id": "job-001",
        },
        "audio": {
            "audio_id": "greeting-2025-01",
            "version": "greet-v1.2",
            "url": f"https://{addr}:8443/audio/greeting.wav",
            "sha256": sha256_hex(wav_path),
            "file_size": wav_size,
            "expires_at": int(time.time()) + 90 * 86400,
        },
    }
    out = HERE / "releases/manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"固件: {fw_path} ({fw_size} B) sha256={manifest['firmware']['sha256'][:16]}…")
    print(f"音频: {wav_path} ({wav_size} B) sha256={manifest['audio']['sha256'][:16]}…")
    print(f"清单: {out}")
    print(f"地址: {addr}  (必须与固件 CONFIG_JULIA_SERVER_ADDR 一致;OTA URL 主机名精确匹配白名单)")


if __name__ == "__main__":
    main()
