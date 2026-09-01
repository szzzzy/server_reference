# -*- coding: utf-8 -*-
"""MQTT 控制面适配器:OTA/音频素材检查、状态回执、语音命令(vcmd)。

纯独立模块:transport 用本目录 mqtt_pure.MqttPClient(标准库,MQTT QoS0/1)。
主题(与固件协议一致,可经 config 覆盖):
  订阅: /device/ota/check /device/ota/status/+ /device/audio/check
        /device/audio/status/+ voice/esp32s3/vstatus
  发布: /device/ota/response/<id> /device/ota/notify/<id>
        /device/audio/response/<id> voice/esp32s3/vcmd
"""
import asyncio
import json
import logging

from common import iso_now
from manifest_store import QUARANTINE_ERROR_CODES, build_audio_response, build_ota_response
from mqtt_pure import MqttPClient

log = logging.getLogger("vs.mqtt")


class MqttAdapter:
    def __init__(self, cfg, hub, store, run_dir, on_vcmd=None):
        self.cfg = cfg
        self.hub = hub
        self.store = store
        self.run_dir = run_dir
        self.on_vcmd = on_vcmd or (lambda text: asyncio.sleep(0))
        self.client = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._loop = None          # 主事件循环(引擎线程等跨线程发布须经其调度)

    # ---------------- 生命周期 ----------------

    async def start(self):
        self._loop = asyncio.get_running_loop()   # 记住主 loop,供跨线程(引擎)发布
        m = self.cfg.get("mqtt", {})
        host = m.get("host", "127.0.0.1")
        port = int(m.get("port", 1883))
        self.client = MqttPClient(
            m.get("server_client_id", "virtual-server"), host, port,
            keepalive=30, clean_session=False, on_message=self._on_message,
        )
        self._conn_task = asyncio.create_task(self._connect_loop())
        log.info("MQTT 适配器启动中 → %s:%s (client_id=%s)", host, port,
                 m.get("server_client_id", "virtual-server"))

    async def _connect_loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.client.connect(timeout=10)
                granted = await self.client.subscribe(self._sub_topics())
                self._ready.set()
                backoff = 1.0
                log.info("MQTT 已连接并订阅 %d 主题: %s", len(granted), granted)
                await self._stop.wait()
                return
            except Exception as e:
                log.warning("MQTT 连接失败(%s),%ss 后重试", e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
                self._ready.clear()

    def _sub_topics(self):
        t = self.cfg["topics"]
        return [
            (t["ota_check"], 1),
            (t["ota_status"] + "/+", 1),
            (t["audio_check"], 1),
            (t["audio_status"] + "/+", 1),
            (t["vstatus"], 1),
        ]

    async def stop(self):
        self._stop.set()
        conn_task = getattr(self, "_conn_task", None)
        if conn_task:
            conn_task.cancel()
        if self.client:
            await self.client.close()

    def _on_message(self, topic, payload, qos):
        # reader 任务中同步调度;处理器均为同步/快速逻辑
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        log.debug("MQTT <- %s len=%d", topic, len(payload))
        try:
            t = self.cfg["topics"]
            if topic == t["ota_check"]:
                self._handle_ota_check(text)
            elif topic.startswith(t["ota_status"] + "/"):
                self._handle_ota_status(topic.rsplit("/", 1)[-1], text)
            elif topic == t["audio_check"]:
                self._handle_audio_check(text)
            elif topic.startswith(t["audio_status"] + "/"):
                self._handle_audio_status(topic.rsplit("/", 1)[-1], text)
            elif topic == t["vstatus"]:
                self._handle_vstatus(text)
            else:
                log.info("未订阅的 topic: %s", topic)
        except Exception:
            log.exception("处理 %s 失败", topic)

    # ---------------- 业务处理 ----------------

    def _handle_ota_check(self, text):
        req = self._parse(text, "ota_check")
        if req is None:
            return
        device_id = req.get("device_id", "")
        self.hub.touch(device_id, transport="mqtt")
        resp = build_ota_response(req, self.store, self.hub)
        self._publish(f"{self.cfg['topics']['ota_response']}/{device_id}", resp)

    def _handle_audio_check(self, text):
        req = self._parse(text, "audio_check")
        if req is None:
            return
        device_id = req.get("device_id", "")
        self.hub.touch(device_id, transport="mqtt")
        if req.get("type") != "audio_check":
            log.warning("audio_check topic 收到非 audio_check 消息,忽略")
            return
        resp = build_audio_response(req, self.store)
        self._publish(f"{self.cfg['topics']['audio_response']}/{device_id}", resp)

    def _handle_ota_status(self, device_id, text):
        st = self._parse(text, "ota_status")
        if st is None:
            return
        self.hub.record(device_id, "ota_status", st)
        err = st.get("error_code", "NONE")
        if st.get("state") == "failed" and err != "NONE" and err in QUARANTINE_ERROR_CODES:
            artifact = st.get("artifact_id", "")
            if artifact:
                self.store.add_quarantine(artifact, f"{err} @{iso_now()}")

    def _handle_audio_status(self, device_id, text):
        st = self._parse(text, "audio_status")
        if st is None:
            return
        self.hub.record(device_id, "audio_status", st)
        err = int(st.get("error_code", 0) or 0)
        if st.get("state") == "failed" and err != 0:
            self.store.add_quarantine(st.get("audio_id", ""), f"audio_err={err} @{iso_now()}")

    def _handle_vstatus(self, text):
        self.hub.record("board-vstatus", "vstatus", {"text": text.strip()})
        log.info("vstatus: %s", text.strip()[:120])

    # ---------------- 对外能力 ----------------

    def send_vcmd(self, command_text):
        self._publish_raw(self.cfg["topics"]["vcmd"], command_text.encode("utf-8"), qos=1)
        log.info("MQTT -> vcmd: %s", command_text)

    def send_ota_notify(self, device_id, job_id="job-001"):
        self._publish(
            f"{self.cfg['topics']['ota_notify']}/{device_id}",
            {"type": "ota_notify", "job_id": job_id},
        )

    # ---------------- 工具 ----------------

    @staticmethod
    def _parse(text, expected_type):
        try:
            obj = json.loads(text)
        except Exception:
            log.warning("%s: JSON 解析失败", expected_type)
            return None
        return obj

    def _publish_raw(self, topic, payload_b, qos=1, retain=False):
        if self.client is None or not self._ready.is_set():
            log.warning("MQTT 未就绪,丢弃发布: %s", topic)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and self._loop is not None and loop is self._loop:
            # 调用方就在主事件循环线程(如 admin vcmd)
            asyncio.ensure_future(self.client.publish(topic, payload_b, qos=qos))
        elif self._loop is not None and self._loop.is_running():
            # 跨线程发布(如 real_engine 线程):经主 loop 线程安全调度
            asyncio.run_coroutine_threadsafe(
                self.client.publish(topic, payload_b, qos=qos), self._loop)
        else:
            log.warning("无运行中事件循环,发布取消: %s", topic)

    def _publish(self, topic, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._publish_raw(topic, payload, qos=1)
        log.info("MQTT -> %s: %s", topic, payload.decode("utf-8", errors="replace")[:200])
