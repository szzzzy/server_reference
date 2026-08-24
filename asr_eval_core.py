import subprocess
import time
import wave
import os
from pathlib import Path

import numpy as np
import soundfile as sf


def gpu_snapshot():
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
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate())
    except Exception:
        return 0.0


def load_audio_16k_mono(path):
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
    chars = []
    for ch in str(text):
        if "\u4e00" <= ch <= "\u9fff" or ch.isalnum():
            chars.append(ch.lower())
    return "".join(chars)


def cer(ref, hyp):
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
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return str(first.get("text", ""))
    if isinstance(result, dict):
        return str(result.get("text", ""))
    return str(result or "")


def load_paraformer(model_name="paraformer-zh-streaming", device="auto"):
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
