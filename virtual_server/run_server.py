# -*- coding: utf-8 -*-
"""虚拟测试服务器主入口:broker + HTTPS 文件 + WSS 语音 + MQTT 控制面,单进程调度。

用法:
  python run_server.py                     # 启动全部服务(默认配置)
  python run_server.py --admin-vcmd "FILE_SEND SD:/test.wav" --admin-vcmd-delay 6
  python run_server.py --no-broker         # 不内置 broker(连外部 broker 时)
"""
import argparse
import asyncio
import json
import logging
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import detect_ip, load_json, new_run_dir, resolve_path, setup_logging
from manifest_store import ManifestStore
from session_hub import SessionHub
from voice_bridge import make_engine

log = logging.getLogger("vs.main")


def build_status_fn(hub, engine):
    def _status():
        out = {
            "hub": hub.snapshot(),
            "engine": engine.snapshot(),
            "recent": hub.recent_list(60),
        }
        try:
            out["manifest"] = json.loads(
                (HERE / "releases/manifest.json").read_text(encoding="utf-8")
            )
        except Exception:
            pass
        return out
    return _status


async def amain(args, cfg, run_dir):
    hub = SessionHub(run_dir)
    engine = make_engine(
        cfg.get("voice", {}).get("mode", "stub"), hub,
        cfg=cfg, run_dir=run_dir, project_root=HERE.parent)
    store_path = resolve_path(HERE, cfg.get("paths", {}).get("manifest", "releases/manifest.json"))
    store = ManifestStore(store_path)
    store.data.setdefault("firmware", {})
    stop_event = asyncio.Event()

    # ---------- 1) 本地 broker ----------
    broker_task = None
    if cfg.get("mqtt", {}).get("enable_local_broker", True) and not args.no_broker:
        from broker import run_broker
        broker_task = asyncio.create_task(run_broker(cfg.get("mqtt", {}), stop_event))

    # ---------- 2) HTTPS 文件服务(线程) ----------
    import ssl as _ssl
    from https_file_server import make_ssl_context, serve_file_server
    fs = cfg.get("file_server", {})
    fs_root = resolve_path(HERE, fs.get("root", "releases/files"))
    fs_ctx = None
    if fs.get("tls", True):
        fs_ctx = make_ssl_context(
            resolve_path(HERE, cfg["paths"]["server_cert"]),
            resolve_path(HERE, cfg["paths"]["server_key"]),
        )
    file_server = serve_file_server(
        fs_root, int(fs.get("port", 8443)), ssl_ctx=fs_ctx,
        host=fs.get("host", "0.0.0.0"), status_fn=build_status_fn(hub, engine),
    )

    # ---------- 3) WSS 语音服务 ----------
    from wss_adapter import WssAdapter
    wss = WssAdapter(cfg, hub, engine, run_dir)
    await wss.start()

    # 引擎下行 sink:从工作线程安全调度到事件循环,广播给在线 WSS 客户端
    if hasattr(engine, "set_sink"):
        loop = asyncio.get_running_loop()

        def _text_sink(t):
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(wss.broadcast_text(t)))

        def _pcm_sink(b):
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(wss.broadcast_pcm(b)))

        engine.set_sink(_text_sink, _pcm_sink)
    if hasattr(engine, "start"):
        engine.start()

    # ---------- 4) MQTT 控制面 ----------
    from mqtt_adapter import MqttAdapter

    async def on_vcmd_cb(text):
        await wss.broadcast_text(text)

    mqtt = MqttAdapter(cfg, hub, store, run_dir)
    mqtt.on_vcmd = on_vcmd_cb
    await mqtt.start()

    # ---------- 5) 管理通道:stdin 控制台 + 一次性 vcmd ----------
    async def console():
        log.info("console 启动(admin_vcmd=%r)", args.admin_vcmd[:40] if args.admin_vcmd else "")
        if args.admin_vcmd:
            try:
                await asyncio.wait_for(mqtt._ready.wait(), timeout=args.admin_vcmd_delay)
            except asyncio.TimeoutError:
                log.warning("MQTT 未就绪,--admin-vcmd 未送达")
                return
            log.info("console: 下发 admin vcmd")
            mqtt.send_vcmd(args.admin_vcmd)
            await wss.broadcast_text(args.admin_vcmd)
        if not args.tty:
            return
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if line:
                mqtt.send_vcmd(line)
                await wss.broadcast_text(line)

    asyncio.create_task(console())

    # ---------- 6) 周期状态 ----------
    async def status_loop():
        while not stop_event.is_set():
            print("\n---- 设备状态 ----\n" + hub.summary_text(), flush=True)
            print("---- 语音引擎 ----")
            print(json.dumps(engine.snapshot(), ensure_ascii=False), flush=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    asyncio.create_task(status_loop())

    log.info("全部服务就绪。Ctrl+C 退出;stdin 输入文本将作为 vcmd 下发。")
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if hasattr(engine, "stop"):
            engine.stop()
        mqtt.stop()
        await wss.stop()
        file_server.shutdown()
        if broker_task:
            broker_task.cancel()
    log.info("服务已全部停止")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--admin-vcmd", default="", help="启动后延迟下发一条 vcmd(如 FILE_SEND SD:/x.wav)")
    ap.add_argument("--admin-vcmd-delay", type=float, default=6.0)
    ap.add_argument("--no-broker", action="store_true")
    ap.add_argument("--tty", action="store_true", help="启用 stdin 控制台(手动下发 vcmd)")
    ap.add_argument("--voice-mode", default="", help="覆盖 voice.mode(stub/real;real 需 .venv_5090_llm)")
    args = ap.parse_args()

    cfg = load_json(args.config)
    if args.voice_mode:
        cfg["voice"]["mode"] = args.voice_mode
    if cfg.get("server", {}).get("addr", "auto") == "auto":
        cfg["server"]["addr"] = detect_ip()
    cfg["paths"] = {k: str(resolve_path(HERE, v)) for k, v in cfg.get("paths", {}).items()}
    run_dir = new_run_dir(HERE / "runs")
    log = setup_logging(run_dir)
    log.info("运行目录: %s", run_dir)
    log.info("服务器地址: %s", cfg["server"]["addr"])
    try:
        asyncio.run(amain(args, cfg, run_dir))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
