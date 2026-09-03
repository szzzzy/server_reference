# -*- coding: utf-8 -*-
"""全量日志统计:每个 run 的断联次数/时长分布/是否伴随 MQTT 重建/服务器最大日志间隔。"""
import datetime
import re
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"
PAT = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})\s+(\S+)\s+(\S+)\s+(.*)$")


def load(p):
    evs = []
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = PAT.match(line)
            if m:
                ts, ms, lvl, comp, rest = m.groups()
                evs.append((datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() + int(ms) / 1000,
                            comp, rest.strip()))
    return evs


rows = []
for d in sorted(RUNS.iterdir()):
    p = d / "virtual_server.log"
    if not p.exists():
        continue
    evs = load(p)
    if not evs:
        continue
    t0 = evs[0][0]
    # 服务器日志最大间隔
    max_gap, gap_at = 0.0, None
    for i in range(1, len(evs)):
        g = evs[i][0] - evs[i - 1][0]
        if g > max_gap:
            max_gap, gap_at = g, (evs[i - 1][0], evs[i][0])
    # 断联统计
    discs = []
    for i, (ts, comp, rest) in enumerate(evs):
        if "WSS 设备异常断开" not in rest:
            continue
        dur = None
        for j in range(i, min(i + 4, len(evs))):
            m = re.search(r"连接时长=([\d.]+)s", evs[j][2])
            if m:
                dur = float(m.group(1))
                break
        # 3s 内是否有 MQTT esp CONNACK(设备重启信号)
        mqtt_reboot = any(
            "CONNACK esp-" in evs[j][2] and 0 <= evs[j][0] - ts <= 3.0
            for j in range(i, min(i + 10, len(evs))))
        discs.append((ts, dur, mqtt_reboot))
    if not discs:
        continue
    durs = [x[1] for x in discs if x[1] is not None]
    reboots = sum(1 for x in discs if x[2])
    rows.append((d.name, len(evs), len(discs), reboots, max_gap,
                 f"{min(durs) if durs else '-'}~{max(durs) if durs else '-'}",
                 discs[0][0] - t0))

print(f"{'run':<18}{'evs':>6}{'disc':>5}{'reboot':>7}{'maxgap_s':>9}{'dur_range_s':>18}{'first_disc_s':>14}")
for r in rows:
    print(f"{r[0]:<18}{r[1]:>6}{r[2]:>5}{r[3]:>7}{r[4]:>9.1f}{r[5]:>18}{r[6]:>14.1f}")
