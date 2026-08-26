# -*- coding: utf-8 -*-
"""MQTT 纯栈自测:两个客户端发布/订阅(QoS1 双向)+ 通配符订阅 + clean_session 恢复。

用法(需 broker 在跑):python mqtt_selftest.py [host] [port]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mqtt_pure import MqttPClient


async def main(host, port):
    got = []
    sub = MqttPClient("selftest-sub", host, port, keepalive=30,
                      on_message=lambda t, p, q: got.append(p.decode()))
    pub = MqttPClient("selftest-pub", host, port, keepalive=30)
    await sub.connect()
    await sub.subscribe([("selftest/t/+", 1), ("selftest/wild/#", 1)])
    await pub.connect()

    await pub.publish("selftest/t/x", b"hello-qos1", qos=1)
    await asyncio.sleep(1.0)
    ok1 = got == ["hello-qos1"]

    await pub.publish("selftest/wild/a/b/c", b"wild", qos=0)
    await asyncio.sleep(0.6)
    ok2 = "wild" in got

    # clean_session=False 会话恢复:断开重连后订阅仍在
    await sub.close()
    await asyncio.sleep(0.4)
    await sub.connect()
    await pub.publish("selftest/t/y", b"after-reconnect", qos=1)
    await asyncio.sleep(1.0)
    ok3 = "after-reconnect" in got

    await sub.close()
    await pub.close()
    print(f"QoS1 双向: {'PASS' if ok1 else 'FAIL'}  通配符: {'PASS' if ok2 else 'FAIL'}  "
          f"会话恢复: {'PASS' if ok3 else 'FAIL'}")
    print("全部消息:", got)
    return ok1 and ok2 and ok3


if __name__ == "__main__":
    ok = asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1",
                          int(sys.argv[2]) if len(sys.argv) > 2 else 1883))
    sys.exit(0 if ok else 1)
