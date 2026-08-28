# -*- coding: utf-8 -*-
# ============================================================================
# ASR 模块(Evaluation Core / 评估核心)
# ----------------------------------------------------------------------------
# 职责:FUNASR Paraformer 流式语音识别(ASR)的加载与推理入口。
#   语音识别链路: 一段 VAD 切好的音频(int16 numpy) → 本模块加载的模型 → 中文文本。
#   与 TTS 不同,ASR 直接运行在 GPU 主进程(.venv_5090_llm)内,不单独拆子进程。
# 本文件核心函数:
#   load_paraformer()       — 加载 FunASR AutoModel(本地快照优先,离线可用)
#   recognize_streaming()   — 600ms 块级增量识别(带 cache 状态传递,最后一块 is_final)
#   extract_text() / cer() / normalize_text() — 结果解析与 CER 评估工具
#   load_audio_16k_mono()   — 任意 wav → 16k 单声道 float(供离线评估用)
# 调用方:
#   server/real_engine.py   — 服务器 real 模式(本包完整链路)
#   voice_daemon.py(V5)    — 本地板卡链路(同一文件,共用实现)
# ============================================================================
import subprocess
import time
import wave
import os
from pathlib import Path

import numpy as np
import soundfile as sf


def gpu_snapshot():
    """询问 nvidia-smi 当前 GPU 状态(名称 / 已用 / 总量 / 利用率)。

    返回: 一行 CSV 文本,如 "NVIDIA GeForce RTX 5090, 8192, 32768, 45%"。
    失败(无 nvidia-smi / 无 GPU)时返回 "ERROR: ..." 字符串,绝不抛异常 ——
    调用方(自检、结果记录)只把它当展示信息,失败不影响链路运行。
    """
    try:
        return subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
    except Exception as exc:
        return f"ERROR: {exc}"


def audio_duration(path):
    """读取 wav 时长(秒);失败返回 0.0(不抛异常,用于统计/自检容错)。"""
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate())
    except Exception:
        return 0.0


def load_audio_16k_mono(path):
    """把任意采样率/声道数的 wav 读成 16kHz 单声道 float32(供离线评估 recognize_streaming 用)。

    - soundfile 读取立体声时按均值合成为单声道;
    - 原始已经是 16k 则直接返回;
    - 否则优先用 torchaudio 重采样;torchaudio 不可用时退回线性插值(精度略低但可跑)。
    返回: (speech float32 1-D, 16000)。
    """
    speech, sample_rate = sf.read(str(path), dtype="float32")
    if speech.ndim > 1:
        speech = np.mean(speech, axis=1)
    if sample_rate == 16000:
        return speech, sample_rate

    try:
        import torch
        import torchaudio

        tensor = torch.from_numpy(speech).float().unsqueeze(0)
        resampled = torchaudio.functional.resample(tensor, orig_freq=sample_rate, new_freq=16000)
        return resampled.squeeze(0).cpu().numpy(), 16000
    except Exception:
        # Fallback: simple linear interpolation if torchaudio is unavailable.
        x_old = np.linspace(0.0, 1.0, num=len(speech), endpoint=False)
        new_len = int(round(len(speech) * 16000 / sample_rate))
        x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
        resampled = np.interp(x_new, x_old, speech).astype(np.float32)
        return resampled, 16000


def normalize_text(text):
    """归一化文本用于 CER 比对:只保留中文字符与字母数字,英文字母转小写并去掉标点/空白。

    这样 "把, 吧。" 与 "把吧" 视为等价,避免标点差异污染 CER 指标。
    """
    chars = []
    for ch in str(text):
        if "\u4e00" <= ch <= "\u9fff" or ch.isalnum():
            chars.append(ch.lower())
    return "".join(chars)


def cer(ref, hyp):
    """计算识别错误率 CER(字符错误率,越小越好)。

    实现: 先 normalize_text(去标点/空白/统一大小写),再做标准编辑距离
    (插入/删除/替换各计 1),CER = 编辑距离 / 参考字数。
    返回: 保留 4 位小数的比率;参考文本为空时返回 ""(无意义,不参与比较)。
    注: 普通读音评估门槛常取 CER ≤ 0.35 视为"识别可接受"。
    """
    ref = normalize_text(ref)
    hyp = normalize_text(hyp)
    if not ref:
        return ""
    m, n = len(ref), len(hyp)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    return round(dp[m][n] / m, 4)


def extract_text(result):
    """从 FunASR model.generate() 的返回值中抽取文本字串。

    FunASR 返回结构通常是 list[dict] → [{"text": "..."}](部分实现直接返回 dict),
    这里兼容三种形态:list[dict] / dict / 可直接 str() 的原始值;取不到时返回空串。
    """
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return str(first.get("text", ""))
    if isinstance(result, dict):
        return str(result.get("text", ""))
    return str(result or "")


def load_paraformer(model_name="paraformer-zh-streaming", device="auto"):
    """加载 FunASR 流式 ASR 模型(ASR 模块的加载入口,进程启动时调用一次,常驻 GPU/内存)。

    参数:
      model_name: 模型名或本地目录。
                   - 在线别名 "paraformer-zh-streaming" 会先找本地 ModelScope 快照
                     (iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online),
                     快照 5 个必需文件齐全才使用,否则回落在线别名(会访问模型中心);
                  - 本地目录(如 models/paraformer-zh-streaming)则直接使用,完全离网。
      device: "auto" = 按 torch.cuda.is_available() 选 cuda:0/cpu;也可显式传 "cuda:0"/"cpu"。
    返回: (model, real_device, load_seconds):
      model        — FunASR AutoModel 实例,前端/模型/推理已装配,后续直接调 model.generate();
      real_device  — 实际加载到的设备字符串;
      load_seconds — 加载耗时(秒,用于启动日志/自检)。
    """
    import torch
    from funasr import AutoModel

    if device == "auto":
        real_device = "cuda:0" if torch.cuda.is_available() else "cpu"
    else:
        real_device = device
    # Prefer the complete local ModelScope snapshot.  Passing the online alias
    # makes FunASR contact the model hub on every process start, even when the
    # weights are already cached, which makes field tests depend on networking.
    resolved_model = model_name
    if model_name == "paraformer-zh-streaming":
        cache_root = Path(os.environ.get("MODELSCOPE_CACHE", Path.home() / ".cache" / "modelscope"))
        local_snapshot = (
            cache_root / "models"
            / "iic--speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
            / "snapshots" / "master"
        )
        required = ("configuration.json", "config.yaml", "model.pt", "tokens.json", "am.mvn")
        if all((local_snapshot / name).is_file() for name in required):
            resolved_model = str(local_snapshot)

    start = time.perf_counter()
    model = AutoModel(model=resolved_model, device=real_device, disable_update=True)
    return model, real_device, time.perf_counter() - start


def recognize_streaming(model, audio_path):
    """对一段已录完的音频做"块级流式识别"(离线评估用;服务器/本地链路实际用的是
    board_serial_asr_test.recognize,与本函数机制一致)。

    流式机制:
      - chunk_size = [0, 10, 5]:FunASR 流式配置 —— 当前块 10 个单位(每单位 60ms=960 样本,
        即 600ms 一块),解码时向右看 5 个单位;chunk_stride = 10*960 = 9600 样本;
      - cache:跨块累积的模型内部状态(encoder/decoder 隐藏状态),相当于模型"记忆",
        这是流式模型与整段识别的本质区别(同音字/多音字因此能在上下文里被校正);
      - is_final:最后一块传 True,通知模型冲刷内部缓冲,输出最后剩余的文本
        (部分字只有 final 后才出现,漏传会丢字);
      - encoder_chunk_look_back=4 / decoder_chunk_look_back=1:解码时回看前面 4/1 块。
    返回: (全文, 首块出字耗时秒, 总耗时秒);首块出字为 "" 表示整段未识别出有效文本。
    """
    speech, sample_rate = load_audio_16k_mono(audio_path)

    chunk_size = [0, 10, 5]
    chunk_stride = chunk_size[1] * 960
    cache = {}
    parts = []
    first_partial = ""
    total_chunks = int((len(speech) - 1) / chunk_stride + 1)
    start = time.perf_counter()
    for i in range(total_chunks):
        chunk = speech[i * chunk_stride : (i + 1) * chunk_stride]
        result = model.generate(
            input=chunk,
            cache=cache,
            is_final=i == total_chunks - 1,
            chunk_size=chunk_size,
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        text = extract_text(result)
        if text:
            if first_partial == "":
                first_partial = round(time.perf_counter() - start, 3)
            parts.append(text)
    elapsed = time.perf_counter() - start
    return "".join(parts), first_partial, elapsed
