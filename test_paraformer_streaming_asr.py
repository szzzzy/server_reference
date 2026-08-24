import argparse
import csv
import json
import subprocess
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf


DEFAULT_EXPECTED = "现在开始进行标准女声麦克风拾音性能测试，本段语音用于模拟正常人声输入。"


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


def normalize_text(text):
    chars = []
    for ch in text:
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


def main():
    parser = argparse.ArgumentParser(description="Test Paraformer streaming ASR chunk by chunk.")
    parser.add_argument("--audio", default="samples/standard_female_voice_16k_mono_16bit.wav")
    parser.add_argument("--expected", default=DEFAULT_EXPECTED)
    parser.add_argument("--model", default="paraformer-zh-streaming")
    parser.add_argument("--output", default="results/paraformer_streaming_asr_results.csv")
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()

    Path("results").mkdir(exist_ok=True)
    audio = Path(args.audio)
    before_gpu = gpu_snapshot()
    status = "OK"
    err = ""
    final_text = ""
    first_partial_seconds = ""
    load_seconds = 0.0
    infer_seconds = 0.0
    after_load_gpu = ""

    try:
        import torch
        from funasr import AutoModel

        if args.device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        else:
            device = args.device

        load_start = time.perf_counter()
        model = AutoModel(model=args.model, device=device, disable_update=True)
        load_seconds = time.perf_counter() - load_start
        after_load_gpu = gpu_snapshot()

        speech, sample_rate = sf.read(str(audio), dtype="float32")
        if speech.ndim > 1:
            speech = np.mean(speech, axis=1)
        if sample_rate != 16000:
            raise RuntimeError(f"Expected 16000 Hz audio, got {sample_rate} Hz")

        chunk_size = [0, 10, 5]
        encoder_chunk_look_back = 4
        decoder_chunk_look_back = 1
        chunk_stride = chunk_size[1] * 960
        cache = {}
        parts = []
        total_chunks = int((len(speech) - 1) / chunk_stride + 1)

        infer_start = time.perf_counter()
        for i in range(total_chunks):
            chunk = speech[i * chunk_stride : (i + 1) * chunk_stride]
            is_final = i == total_chunks - 1
            result = model.generate(
                input=chunk,
                cache=cache,
                is_final=is_final,
                chunk_size=chunk_size,
                encoder_chunk_look_back=encoder_chunk_look_back,
                decoder_chunk_look_back=decoder_chunk_look_back,
            )
            text = extract_text(result)
            if text:
                if first_partial_seconds == "":
                    first_partial_seconds = round(time.perf_counter() - infer_start, 3)
                parts.append(text)
        infer_seconds = time.perf_counter() - infer_start
        final_text = "".join(parts)
    except Exception as exc:
        status = "ERROR"
        err = repr(exc)
        after_load_gpu = gpu_snapshot()

    duration = audio_duration(audio)
    row = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "module": "Paraformer-online/paraformer-zh-streaming",
        "model": args.model,
        "audio": str(audio),
        "audio_seconds": round(duration, 3),
        "load_seconds": round(load_seconds, 3) if load_seconds else "",
        "first_partial_seconds": first_partial_seconds,
        "infer_seconds": round(infer_seconds, 3) if infer_seconds else "",
        "rtf": round(infer_seconds / duration, 4) if duration and infer_seconds else "",
        "before_gpu": before_gpu,
        "after_load_gpu": after_load_gpu,
        "after_infer_gpu": gpu_snapshot(),
        "expected": args.expected,
        "recognized_text": final_text,
        "cer": cer(args.expected, final_text) if final_text else "",
        "status": status,
        "error": err,
    }

    out = Path(args.output)
    write_header = not out.exists()
    with out.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    print(json.dumps(row, ensure_ascii=False, indent=2))
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
