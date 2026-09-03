# -*- coding: utf-8 -*-
"""实时监测:每 5s 采样引擎状态 + 每 10s 采样 log 尾部断联,输出汇总。"""
import json
import ssl
import time
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

LOG = r"runs\20260903_142807\virtual_server.log"


def snap():
    try:
        r = urllib.request.urlopen("https://127.0.0.1:8443/__status", context=ctx, timeout=5)
        e = json.loads(r.read().decode("utf-8"))["engine"]
        return (e["frames"], e["seq_gaps"], e["buffered_bytes"], e["online"], e["awake"])
    except Exception as ex:
        return ("ERR", str(ex))


def log_disc():
    """新增断联行(带事件上下文)。"""
    out = []
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            pos = f.tell()
            f.seek(max(0, pos - 40000))
            lines = f.read().splitlines()
    except OSError:
        return out
    for ln in lines:
        if "WSS 设备异常断开" in ln or "WSS 设备重连" in ln or "vstatus" in ln:
            out.append(ln.strip())
    return out


t_end = time.time() + 900  # 15 分钟
prev_frames = prev_gaps = None
seen = set()
t0 = time.time()
while time.time() < t_end:
    f, s, b, on, awake = snap()
    if isinstance(f, int) and prev_frames is not None:
        print(f"t={time.time()-t0:6.1f} frames+{f-prev_frames:5d} gaps+{s-prev_gaps:3d} "
              f"buffered={b:6d} online={on} awake={awake}", flush=True)
    prev_frames, prev_gaps = f, s
    for ln in log_disc():
        if ln not in seen:
            seen.add(ln)
            print("  LOG|", ln, flush=True)
    time.sleep(5)

