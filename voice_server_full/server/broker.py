# -*- coding: utf-8 -*-
"""本地 MQTT broker(纯标准库实现:1883 明文,可选 8883 TLS)。"""
import asyncio
import logging

log = logging.getLogger("vs.broker")


async def run_broker(cfg, stop_event, on_device_connect=None):
    """cfg: {port:1883, tls_port:8883, enable_tls:false, cert, key}
    on_device_connect: 设备(esp*) MQTT 重连回调(client_id, clean) —— 供上层判"设备重启"。"""
    from mqtt_pure import MqttBroker

    ssl_ctx = None
    if cfg.get("enable_tls"):
        import ssl
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(certfile=cfg["cert"], keyfile=cfg["key"])
    broker = MqttBroker("0.0.0.0", int(cfg.get("port", 1883)), ssl_ctx=ssl_ctx)
    broker.on_device_connect = on_device_connect
    await broker.start()
    log.info("MQTT broker 已启动: 1883(明文)%s",
             " + 8883(TLS)" if cfg.get("enable_tls") else "")
    try:
        await stop_event.wait()
    finally:
        await broker.stop()
        log.info("MQTT broker 已停止")
