# -*- coding: utf-8 -*-
"""解析 runs/*/virtual_server.log,对每次 WSS 断联给出上下文(距上一事件/下一事件/前后时长),
用于判断断联是否与服务器阻塞(长时间无事件)或对话活动(TTS/MIC)相关。"""
import re
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"


def parse(path):
    events = []
    pat = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d+)\s+(\S+)\s+(\S+)\s+(.*)$")
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pat.match(line)
            if not m:
                continue
            ts, ms, lvl, comp, rest = m.groups()
            events.append((ts, int(ms), comp, rest.strip()))
    return events


def analyze(path):
    evs = parse(path)
    print("=" * 100)
    print(f"FILE: {path.name}")
    # 服务器侧“无日志间隔”> 3s 的静默窗口(事件循环若被阻塞,这里会出现大间隔)
    print("\n-- 间隔 >3s 的静默窗口(服务器无任何日志) --")
    for i in range(1, len(evs)):
        p, c = evs[i - 1], evs[i]
        dt = (int(c[1]) - int(p[1])) / 1000.0
        if dt > 3.0:
            print(f"  {p[0]} → {c[0]}  (+{dt:.1f}s)  前件: {p[2]} | {p[3][:70]}")

    print("\n-- 每次 WSS 断联上下文 --")
    disc = 0
    for i, (ts, ms, comp, rest) in enumerate(evs):
        if "WSS 设备异常断开" not in rest:
            continue
        disc += 1
        # 距上一事件
        prev = evs[i - 1] if i > 0 else None
        gap = None
        if prev:
            gap = ((ts2sec(ts, ms) - ts2sec(prev[0], prev[1])))
        # 断联后 60s 内事件
        base = ts2sec(ts, ms)
        later = []
        for j in range(i + 1, min(i + 40, len(evs))):
            d = ts2sec(evs[j][0], evs[j][1]) - base
            if d > 60:
                break
            later.append(f"+{d:.1f}s {evs[j][2]}|{evs[j][3][:60]}")
        print(f"\n  #{disc} @ {ts}  距上一事件: {gap if gap is None else round(gap,1)}s")
        print(f"     {rest[:110]}")
        # 前 30s 内最后 5 条 WSS 下行/上行事件
        prior = []
        for k in range(i - 1, max(0, i - 60), -1):
            if ts2sec(evs[k][0], evs[k][1]) < base - 30:
                break
            if evs[k][2].startswith("vs.wss") or evs[k][2] == "vs.real":
                prior.append(f"{-round(base - ts2sec(evs[k][0], evs[k][1]), 1)}s {evs[k][2]}|{evs[k][3][:70]}")
            if len(prior) >= 6:
                break
        for pr in reversed(prior):
            print(f"     {pr}")
        for la in later[:8]:
            print(f"     {la}")


def ts2sec(ts, ms):
    import datetime
    dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    return dt.timestamp() + ms / 1000.0


if __name__ == "__main__":
    names = [
        "20260903_092029", "20260902_180653", "20260902_175845", "20260902_155937",
        "20260902_113031", "20260902_111947", "20260901_174221",
    ] + ([sys.argv[1]] if len(sys.argv) > 1 else [])
    for n in names:
        p = RUNS / n / "virtual_server.log"
        if p.exists():
            analyze(p)
