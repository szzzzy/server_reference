# -*- coding: utf-8 -*-
"""前置语音检测器(噪声"不进入轮次"): fsmn-vad 段级"此处有无语音"打分。

定位: 在(能量)VAD 判"疑似语音段"之后、进 ASR/意图/问答之前调用一次;
噪声段被直接跳过(不 MIC_STOP / 不空播报 / 不刷新交互),从源头不进轮。
与"识别后判决"的 noise_gate 互补: 本模块拦在入口, noise_gate 兜底在出口
(如 220Hz 纯音这类 fsmn-vad 误判为语音、但 ASR 恒空的段)。

判定规则(长短段区分):
  - 段长 ≥ ratio_sec(2s):  语音占比 ratio ≥ min_speech_ratio(0.2) → 语音;
  - 段长 < ratio_sec(2s):  语音绝对时长 ≥ min_speech_ms(300ms) → 语音。
经验值: 8~10s 白噪声 ratio≈0.06~0.07(判非语音);真语音段 ratio≈0.5~0.9。
模型缺失/判段异常一律"按语音放行"(宁可多轮,不可漏话 —— 能量 VAD 仍是主判据)。

配置: config.json → voice.real.vad.speech_detector
  {enabled, provider:"fsmn_vad", model_dir, device:"cpu", min_speech_ratio, min_speech_ms}
"""
import logging
import time
from pathlib import Path

log = logging.getLogger("vs.vad")


class FsmnVadDetector:
    """fsmn-vad(与 ASR 同栈 FunASR)封装: 输入 int16 样本 → (是否语音, 占比, 语音毫秒)。"""

    provider = "fsmn_vad"

    def __init__(self, model_dir, device="cpu", min_ratio=0.2, min_ms=300.0, ratio_sec=2.0):
        from funasr import AutoModel

        self._model = AutoModel(model=str(model_dir), disable_update=True, device=device)
        self._min_ratio = float(min_ratio)
        self._min_ms = float(min_ms)
        self._ratio_sec = float(ratio_sec)
        log.info("前置语音检测: fsmn-vad 就绪(model=%s, 短段≥%.0fms / 长段占比≥%.2f)",
                 model_dir, min_ms, min_ratio)

    def is_speech(self, samples):
        """返回 (is_speech, speech_ratio, speech_ms);异常 → (True, -1, -1) 放行。"""
        try:
            t0 = time.perf_counter()
            res = self._model.generate(input=samples, cache={})
            dt = (time.perf_counter() - t0) * 1000
            segs = res[0].get("value", []) if res else []
            speech_ms = float(sum(int(b) - int(a) for a, b in segs))
            dur_ms = len(samples) / 16.0                      # 16kHz 样本 → 毫秒
            ratio = speech_ms / dur_ms if dur_ms > 0 else 0.0
            if dur_ms >= self._ratio_sec * 1000:
                ok = ratio >= self._min_ratio
            else:
                ok = speech_ms >= self._min_ms
            return bool(ok), round(ratio, 3), round(speech_ms, 0)
        except Exception as exc:
            log.warning("fsmn-vad 判段失败(%s) → 按语音放行", exc)
            return True, -1.0, -1.0


def load_speech_detector(cfg, deps_root):
    """按配置加载检测器(引擎启动时调用一次);不可用 → None(纯能量 VAD 行为不变)。

    cfg: voice.real.vad.speech_detector(可缺省/disabled/缺失模型 → None,并日志告警)。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not cfg.get("enabled", False):
        return None
    provider = str(cfg.get("provider", "fsmn_vad"))
    if provider != "fsmn_vad":
        log.warning("前置语音检测: 未知 provider=%r,回退纯能量 VAD", provider)
        return None
    model_dir = str(cfg.get("model_dir", "") or "models/fsmn-vad/snapshots/master")
    p = Path(model_dir) if Path(model_dir).is_absolute() else Path(deps_root) / model_dir
    if not (p / "config.yaml").exists():
        log.warning("前置语音检测: 模型缺失(%s) → 回退纯能量 VAD(请下载 fsmn-vad 到该路径)", p)
        return None
    try:
        return FsmnVadDetector(
            p,
            device=str(cfg.get("device", "cpu")),
            min_ratio=float(cfg.get("min_speech_ratio", 0.2)),
            min_ms=float(cfg.get("min_speech_ms", 300.0)),
        )
    except Exception as exc:
        log.warning("前置语音检测: 加载失败(%s) → 回退纯能量 VAD", exc)
        return None
