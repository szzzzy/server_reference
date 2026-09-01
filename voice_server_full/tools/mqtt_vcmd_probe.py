# -*- coding: utf-8 -*-
"""MQTT vcmd 探针:订阅 voice/esp32s3/vcmd,打印收到的消息(含 intent_result JSON)。
用法: 服务器运行中,本脚本后台运行,再跑任何语音轮次即可观察。
"""
import argparse
import asyncio
import sys
from pathlib import Path

SRV = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SRV))

from mqtt_pure import MqttPClient  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=600,
                    help="监听秒数(配合语音测试时长使用)")
    args = ap.parse_args()

    def on_message(topic, body, qos):
        print(f"[MQTT] {topic} <- {body.decode('utf-8', 'replace')}", flush=True)

    client = MqttPClient("intent-probe", host="127.0.0.1", port=1883, on_message=on_message)
    await client.connect()
    await client.subscribe([("voice/esp32s3/vcmd", 1)])
    print(f"probe: subscribed voice/esp32s3/vcmd, listening {args.seconds}s...", flush=True)
    for _ in range(args.seconds * 2):
        await asyncio.sleep(0.5)
    print("probe: done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
