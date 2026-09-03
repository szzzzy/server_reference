# -*- coding: utf-8 -*-
"""规则层决策速度微基准(临时脚本,用完删)。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "voice_server_full" / "engine"))
from intent import decide_intent  # noqa: E402

cfg = json.loads((Path(__file__).parent / "voice_server_full" / "server" / "config.json")
                 .read_text(encoding="utf-8"))["voice"]["real"]["intent"]

cases = ["晚安，我要睡了。", "不想聊了", "嗯嗯",
         "走了半天还没找到展厅，累死了", "这件展品是哪个朝代的",
         "再见，回头再聊这个展品", "我有点累了不想说话了", "好的，晚安了"]

for c in cases:
    decide_intent(c, cfg)  # 预热
N = 20000
t0 = time.perf_counter()
for i in range(N):
    decide_intent(cases[i % len(cases)], cfg)
dt = time.perf_counter() - t0
print(f"decide_intent x{N}: 合计 {dt*1000:.1f} ms → {dt/N*1e6:.2f} us/次(含正则+词表)")
