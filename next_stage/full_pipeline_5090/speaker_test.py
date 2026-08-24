import json
import math
import struct
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports


HERE = Path(__file__).resolve().parent
cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
def detect_board_port():
    ports = list(list_ports.comports())
    for item in ports:
        if item.vid == 0x303A and item.pid == 0x1001:
            return item.device
    for item in ports:
        text = f"{item.description} {item.manufacturer} {item.hwid}".lower()
        if "esp32" in text or "usb jtag" in text or "usb-serial" in text:
            return item.device
    available = ", ".join(item.device for item in ports) or "无"
    raise RuntimeError(f"没有检测到ESP32-S3开发板。当前串口：{available}。请重新插拔USB数据线。")


baud = int(cfg["serial"].get("baud", 921600))
rate = 24000

try:
    port = detect_board_port()
    print(f"已自动识别开发板串口：{port}")
    print(f"正在通过 {port} 测试开发板扬声器……")
    with serial.Serial(port, baud, timeout=2, write_timeout=5) as ser:
        ser.reset_input_buffer()
        volume = max(0, min(100, int(cfg["tts"].get("board_volume_percent", 100))))
        ser.write(b"SPKV" + struct.pack("<I", volume))
        ser.flush()
        time.sleep(0.05)
        ser.write(b"SPKT" + struct.pack("<I", 0))
        ser.flush()
        time.sleep(2.5)
    print(f"板端本地测试完成：音量 {volume}%，应听到三个由低到高的提示音。")
except Exception as exc:
    print(f"扬声器测试失败：{exc}")
    sys.exit(1)
