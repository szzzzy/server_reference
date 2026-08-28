# -*- coding: utf-8 -*-
"""板卡模拟器:按固件协议扮演设备端,对虚拟服务器做自动化验收(PASS/FAIL)。

场景:
  ota      固件 OTA 全链路(check→response 校验→status 序列→下载/Range/ETag/SHA-256)
  audio    音频素材全链路(check→response→status→下载校验)
  voice    WSS 语音流(MIC_START→PCM1×N→MIC_STOP)+ 服务器状态断言
  filedump FILE_SEND 文件推送(需服务器先发 vcmd,如: run_server --admin-vcmd "FILE_SEND SD:/test.wav")
  all      以上全部(filedump 若无命令则 FAIL,提示如何触发)

用法(先按 README 启动 run_server):
  python board_simulator.py --scenario all
"""
import argparse
import asyncio
import hashlib
import json
import ssl
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 本模块位于 <包根>/server/ 下,一级父目录即包根(参考音频在包根 samples/)
ROOT = HERE.parents[0]
sys.path.insert(0, str(HERE))

from common import (detect_ip, load_test_wav, make_sine_wav, pcm1_build,
                    resolve_path, rms_dbfs, ts_now, wss_connect)

DEVICE_ID = "esp-30c6f7a1b2c3"
PRODUCT = "julia-ai-device"
HW_VERSION = "1.0"


def rnd_hex(n):
    import os as _os
    return _os.urandom(n).hex()


def event_id():
    return rnd_hex(16)


def request_id():
    return rnd_hex(4)


class Assertions:
    def __init__(self):
        self.passed, self.failed = [], []

    def check(self, cond, msg):
        (self.passed if cond else self.failed).append(msg)
        print(f"  {'PASS' if cond else 'FAIL'}  {msg}", flush=True)

    def finish(self, tag):
        print(f"\n[{tag}] passed={len(self.passed)} failed={len(self.failed)}")
        return len(self.failed) == 0


class SimBoard:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.device_id = args.device_id or DEVICE_ID
        self.a = Assertions()
        self.mq = None
        self.ws = None
        self.responses = asyncio.Queue()
        self.vcmd_q = asyncio.Queue()

    # ---------------- MQTT(纯标准库客户端)----------------

    async def mqtt_connect(self):
        from mqtt_pure import MqttPClient
        m = self.cfg["mqtt"]
        self.mq = MqttPClient(self.device_id, m["host"], int(m["port"]),
                              keepalive=60, clean_session=False,
                              on_message=self._on_mqtt)
        await self.mq.connect(timeout=10)
        await self.mq.subscribe([
            (self.cfg["topics"]["ota_response"] + "/" + self.device_id, 1),
            (self.cfg["topics"]["ota_notify"] + "/" + self.device_id, 1),
            (self.cfg["topics"]["audio_response"] + "/" + self.device_id, 1),
            (self.cfg["topics"]["vcmd"], 1),
        ])
        print("  MQTT 已连接并订阅(响应/通知/vcmd)", flush=True)

    def _on_mqtt(self, topic, payload, qos):
        text = payload.decode("utf-8", errors="replace")
        if topic == self.cfg["topics"]["vcmd"]:
            self.vcmd_q.put_nowait(text.strip())
            return
        try:
            obj = json.loads(text)
        except Exception:
            obj = {"text": text}
        obj["_topic"] = topic
        self.responses.put_nowait(obj)

    async def publish(self, topic, obj, qos=1):
        await self.mq.publish(topic, json.dumps(obj, ensure_ascii=False).encode("utf-8"), qos=qos)

    async def wait_msg(self, topic_frag, timeout=10.0, exact=False):
        """exact=False:子串匹配(exact=True:主题完全相等)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(
                    self.responses.get(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                return None
            got = msg.get("_topic", "")
            if (got == topic_frag) if exact else (topic_frag in got):
                return msg
        return None

    async def wait_vcmd(self, timeout=30.0):
        try:
            return await asyncio.wait_for(self.vcmd_q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ---------------- HTTP ----------------

    def http_get(self, url, headers=None, expect_code=None):
        import urllib.request
        ctx = ssl.create_default_context(cafile=str(
            resolve_path(HERE, self.cfg["paths"]["ca_file"])))
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def fetch_status(self):
        addr = self.cfg["server"]["addr"]
        _, _, body = self.http_get(
            f"https://{addr}:{self.cfg['file_server']['port']}/__status")
        try:
            return json.loads(body)
        except Exception:
            return {}

    # ---------------- OTA 场景 ----------------

    async def scenario_ota(self):
        print("\n=== OTA 场景 ===")
        await self.mqtt_connect()
        ck = {
            "type": "ota_check", "request_id": request_id(),
            "device_id": self.device_id, "product": PRODUCT,
            "hardware_version": HW_VERSION, "current_version": self.args.current_version,
        }
        await self.publish(self.cfg["topics"]["ota_check"], ck)
        resp = await self.wait_msg(
            f"{self.cfg['topics']['ota_response']}/{self.device_id}", exact=True)
        self.a.check(resp is not None and resp["_topic"] ==
                     f"{self.cfg['topics']['ota_response']}/{self.device_id}",
                     "收到 ota_check_response")
        if resp is None:
            return False
        self.a.check(resp.get("type") == "ota_check_response", "type=ota_check_response")
        self.a.check(resp.get("request_id") == ck["request_id"], "request_id 原样回显")
        self.a.check(resp.get("device_id") == self.device_id, "device_id 回显")
        self.a.check(resp.get("product") == ck["product"], "product 回显")
        self.a.check(resp.get("hardware_version") == ck["hardware_version"], "hardware_version 回显")
        if not self.args.need_update:
            self.a.check(resp.get("update") is False, "update=false(无需更新)")
            return True
        self.a.check(resp.get("update") is True, "update=true")
        for field in ("artifact_id", "version", "url", "sha256", "image_size",
                      "security_version", "expires_at"):
            self.a.check(field in resp and resp[field] not in (None, "", 0), f"清单字段 {field}")
        self.a.check(len(str(resp.get("sha256", ""))) == 64, "sha256 恰 64 hex")
        self.a.check(str(resp.get("url", "")).startswith("https://"), "url 以 https:// 开头")
        self.a.check(resp.get("expires_at", 0) > ts_now(), "expires_at 为未来时间")
        self.a.check(isinstance(resp.get("image_size"), int) and resp["image_size"] > 0,
                     "image_size 为正整数")
        fw_path = resolve_path(HERE, self.cfg["paths"]["fw_bin"])
        expected_sha = hashlib.sha256(fw_path.read_bytes()).hexdigest()
        self.a.check(resp["sha256"] == expected_sha, "清单 sha256 == 实际固件")

        base = {"device_id": self.device_id, "request_id": ck["request_id"],
                "artifact_id": resp["artifact_id"], "product": PRODUCT,
                "hardware_version": HW_VERSION,
                "current_version": ck["current_version"],
                "target_version": resp["version"],
                "job_id": resp.get("job_id", "")}
        await self._status_event(base, "accepted")

        status, headers, data = self.http_get(resp["url"])
        self.a.check(status == 200, f"GET 全量 → 200(实际 {status})")
        self.a.check(int(headers.get("Content-Length", 0)) == resp["image_size"],
                     "Content-Length == image_size")
        etag_full = headers.get("ETag", "")
        self.a.check(bool(etag_full), "响应带 ETag")
        self.a.check(hashlib.sha256(data).hexdigest() == resp["sha256"], "下载 SHA-256 一致")

        status2, headers2, data2 = self.http_get(
            resp["url"], headers={"Range": "bytes=1000-1999"})
        self.a.check(status2 == 206, f"Range 请求 → 206(实际 {status2})")
        self.a.check(headers2.get("Content-Range", "").startswith("bytes 1000-1999/"),
                     "Content-Range 正确")
        self.a.check(len(data2) == 1000, "Range 长度为 1000")
        self.a.check(headers2.get("ETag") == etag_full, "Range 响应 ETag 不变")
        status3, _, _ = self.http_get(
            resp["url"], headers={"Range": "bytes=0-99", "If-Range": '"deadbeef"'})
        self.a.check(status3 == 200, f"If-Range 不匹配 → 200 全量(实际 {status3})")

        await self._status_event(base, "downloading", extra={
            "bytes_downloaded": resp["image_size"] // 2, "image_size": resp["image_size"],
            "progress_percent": 50,
        })
        await self._status_event(base, "verifying")
        await self._status_event(base, "rebooting")
        await self._status_event(base, "booted_pending_verify")
        await self._status_event(base, "succeeded")
        self.a.check(True, "状态序列 accepted/downloading/verifying/rebooting/booted_pending_verify/succeeded 已上报")

        ck2 = dict(ck, request_id=request_id(), current_version=resp["version"])
        await self.publish(self.cfg["topics"]["ota_check"], ck2)
        resp2 = await self.wait_msg(
            f"{self.cfg['topics']['ota_response']}/{self.device_id}", exact=True)
        self.a.check(resp2 is not None and resp2.get("update") is False
                     and resp2.get("request_id") == ck2["request_id"],
                     "同版本二次检查 → update=false")
        return True

    async def _status_event(self, base, state, extra=None):
        ev = dict(base, type="ota_status", schema_version=1, event_id=event_id(),
                  state=state, attempt=1, error_code="NONE",
                  uptime_ms=int(time.monotonic() * 1000))
        if extra:
            ev.update(extra)
        await self.publish(f"{self.cfg['topics']['ota_status']}/{self.device_id}", ev)
        await asyncio.sleep(0.2)

    # ---------------- Audio 场景 ----------------

    async def scenario_audio(self):
        print("\n=== Audio 场景 ===")
        if self.mq is None:
            await self.mqtt_connect()
        ck = {"type": "audio_check", "request_id": request_id(), "device_id": self.device_id,
              "product": PRODUCT, "current_audio_version": self.args.current_audio_version}
        await self.publish(self.cfg["topics"]["audio_check"], ck)
        resp = await self.wait_msg(
            f"{self.cfg['topics']['audio_response']}/{self.device_id}", exact=True)
        self.a.check(resp is not None, "收到 audio_check_response")
        if resp is None:
            return False
        self.a.check(resp.get("type") == "audio_check_response", "type=audio_check_response(无旧别名)")
        self.a.check(resp.get("request_id") == ck["request_id"], "request_id 回显")
        if not self.args.need_audio_update:
            self.a.check(resp.get("update") is False, "update=false(版本一致)")
            return True
        self.a.check(resp.get("update") is True, "update=true")
        for field in ("audio_id", "version", "url", "sha256", "file_size", "expires_at"):
            self.a.check(field in resp and resp[field] not in (None, "", 0), f"字段 {field}")
        self.a.check(resp["file_size"] <= 4 * 1024 * 1024, "file_size ≤ 4 MiB")
        self.a.check(len(resp["sha256"]) == 64, "sha256 64 hex")
        status, headers, data = self.http_get(resp["url"])
        self.a.check(status == 200, f"音频 GET → 200(实际 {status})")
        self.a.check(int(headers.get("Content-Length", 0)) == resp["file_size"],
                     "Content-Length == file_size")
        self.a.check(hashlib.sha256(data).hexdigest() == resp["sha256"], "音频 SHA-256 一致")
        for st, prog in (("accepted", 0), ("downloading", 37), ("verifying", 0), ("ready", 100)):
            await self.publish(f"{self.cfg['topics']['audio_status']}/{self.device_id}", {
                "type": "audio_status", "schema_version": 1, "device_id": self.device_id,
                "audio_id": resp["audio_id"], "state": st, "progress": prog, "error_code": 0,
            })
            await asyncio.sleep(0.15)
        self.a.check(True, "状态序列 accepted→downloading→verifying→ready 已上报")
        return True

    # ---------------- Voice 场景 ----------------

    async def scenario_voice(self):
        print("\n=== Voice 场景 ===")
        w = self.cfg["wss"]
        addr = self.cfg["server"]["addr"]
        ctx = ssl.create_default_context(cafile=str(resolve_path(HERE, self.cfg["paths"]["ca_file"])))
        try:
            self.ws = await wss_connect(
                f"wss://{addr}:{w['port']}{w['path']}", ctx,
                headers={"Authorization": f"Bearer {w['token']}"})
        except Exception as e:
            self.a.check(False, f"WSS 连接失败: {e}")
            return False
        self.a.check(True, "WSS 连接成功(Bearer 鉴权)")
        await self.ws.send("MIC_START")
        wav_path = resolve_path(HERE, self.cfg["paths"]["voice_wav"])
        pcm = load_test_wav(wav_path)
        n_frames = min(int(self.args.stream_seconds * 16000 / 320), len(pcm) // 640)
        seq = 0
        for i in range(n_frames):
            chunk = pcm[i * 640:(i + 1) * 640]
            if len(chunk) < 640:
                break
            seq += 1
            frame = pcm1_build(seq, chunk, int(rms_dbfs(chunk) * 100))
            await self.ws.send(frame)
            await asyncio.sleep(0.02)
        await self.ws.send("MIC_STOP")
        self.a.check(seq >= 50, f"已发送 {seq} 帧 PCM1(≥50)")

        # ---- 下行采集(real 模式:SPKS + PCM + SPKE)----
        downlink_texts, pcm_frames = [], 0
        if self.args.expect_downlink:
            deadline = time.monotonic() + self.args.downlink_wait
            got_spke = False
            while time.monotonic() < deadline and not got_spke:
                try:
                    m = await asyncio.wait_for(
                        self.ws.recv(), timeout=min(5.0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break
                if isinstance(m, str):
                    downlink_texts.append(m)
                    if m == "SPKE":
                        got_spke = True
                else:
                    pcm_frames += 1
            self.a.check(any(t.startswith("SPKS") for t in downlink_texts),
                         f"下行含 SPKS(实际 {downlink_texts[:5]})")
            self.a.check("SPKE" in downlink_texts, f"下行含 SPKE(实际 {downlink_texts})")
            self.a.check(pcm_frames > 0, f"下行 PCM 帧数: {pcm_frames}")
        await asyncio.sleep(0.3)
        await self.ws.close()

        st = self.fetch_status()
        dev = st.get("hub", {}).get("wss-device", {})
        eng = st.get("engine", {})
        seen = max(dev.get("pcm_frames", 0), eng.get("frames", 0))
        self.a.check(seen >= seq,
                     f"服务器收到 PCM1 ≥ {seq} 帧(stub 侧 {dev.get('pcm_frames')} / real 侧 {eng.get('frames')})")
        cmds = " ".join(st.get("engine", {}).get("commands_seen", []))
        self.a.check("MIC_START" in cmds and "MIC_STOP" in cmds, "服务器记录 MIC_START/MIC_STOP 命令")
        return True

    # ---------------- FILE_SEND 场景 ----------------

    async def scenario_filedump(self):
        print("\n=== FILE_SEND 场景 ===")
        if self.mq is None:
            await self.mqtt_connect()
        cmd = await self.wait_vcmd(timeout=self.args.vcmd_timeout)
        if cmd is None:
            self.a.check(False, "等待 vcmd 超时(需服务器下发 FILE_SEND)")
            print("  提示:另开终端运行 run_server.py --admin-vcmd \"FILE_SEND SD:/test.wav\" --admin-vcmd-delay 6")
            return False
        if not cmd.startswith("FILE_SEND "):
            self.a.check(False, f"vcmd 不是 FILE_SEND: {cmd}")
            return False
        uri = cmd[len("FILE_SEND "):].strip()
        if uri.startswith("SD:"):
            local = HERE / "sim_data" / uri[3:].lstrip("/")
        else:
            local = HERE / "sim_data" / uri.lstrip("/")
        make_sine_wav(local, seconds=1.0)
        size = local.stat().st_size
        self.a.check(size <= 8 * 1024 * 1024 and local.suffix.lower() == ".wav",
                     f"本地 wav 就绪: {local.name} ({size} B)")
        if self.ws is None or getattr(self.ws, "closed", True):
            w = self.cfg["wss"]
            ctx = ssl.create_default_context(cafile=str(resolve_path(HERE, self.cfg["paths"]["ca_file"])))
            self.ws = await wss_connect(
                f"wss://{self.cfg['server']['addr']}:{w['port']}{w['path']}", ctx,
                headers={"Authorization": f"Bearer {w['token']}"})
        await self.ws.send(f"BEGIN FILE {size} {local.name}")
        data = local.read_bytes()
        for i in range(0, len(data), 1200):
            await self.ws.send(data[i:i + 1200])
        await self.ws.send(f"END {len(data)}")
        try:
            reply = await asyncio.wait_for(self.ws.recv(), timeout=8)
        except asyncio.TimeoutError:
            reply = ""
        self.a.check(reply == "FILE_OK", f"服务器回 FILE_OK(实际 {reply!r})")
        st = self.fetch_status()
        uploads = st.get("engine", {}).get("file_uploads", [])
        self.a.check(any(str(u).endswith(local.name) for u in uploads), f"服务器落盘确认: {uploads}")
        await self.ws.close()
        return True

    # ---------------- 入口 ----------------

    async def run(self):
        ok = True
        for sc in [s.strip() for s in self.args.scenario.split(",") if s.strip()]:
            if sc == "ota":
                ok &= await self.scenario_ota()
            elif sc == "audio":
                ok &= await self.scenario_audio()
            elif sc == "voice":
                ok &= await self.scenario_voice()
            elif sc == "filedump":
                ok &= await self.scenario_filedump()
            elif sc == "all":
                ok &= await self.scenario_ota()
                ok &= await self.scenario_audio()
                ok &= await self.scenario_voice()
                ok &= await self.scenario_filedump()
            else:
                print(f"未知场景: {sc}")
                ok = False
        if self.mq:
            await self.mq.close()
        print("\n" + "=" * 60)
        print(f"总体: passed={len(self.a.passed)} failed={len(self.a.failed)}")
        for f in self.a.failed:
            print(f"  FAILED: {f}")
        return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--device-id", default=DEVICE_ID)
    ap.add_argument("--current-version", default="1.0.0")
    ap.add_argument("--need-update", action="store_true", default=True)
    ap.add_argument("--no-update", action="store_true",
                    help="OTA 场景断言 update:false(同版本,不触发)")
    ap.add_argument("--current-audio-version", default="unknown")
    ap.add_argument("--need-audio-update", action="store_true", default=True)
    ap.add_argument("--stream-seconds", type=float, default=3.0)
    ap.add_argument("--vcmd-timeout", type=float, default=30.0)
    ap.add_argument("--fw-size", type=int, default=262144)
    ap.add_argument("--expect-downlink", action="store_true",
                    help="real 模式:断言下行 SPKS/SPKE/PCM 帧")
    ap.add_argument("--downlink-wait", type=float, default=120.0,
                    help="等待下行应答的秒数")
    args = ap.parse_args()
    if args.no_update:
        args.need_update = False

    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    if cfg["server"]["addr"] == "auto":
        cfg["server"]["addr"] = detect_ip()
    cfg["paths"].update({
        "ca_file": str(resolve_path(HERE, "../certs/ca/ca.crt")),
        "fw_bin": "releases/files/fw/app.bin",
        "voice_wav": str(ROOT / "samples/standard_female_voice_16k_mono_16bit.wav"),
    })
    sim = SimBoard(cfg, args)
    ok = asyncio.run(sim.run())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
