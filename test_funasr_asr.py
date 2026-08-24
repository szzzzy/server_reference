import argparse
import csv
import json
import subprocess
import time
import wave
from datetime import datetime
from pathlib import Path


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
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("text") or result)
    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return " ".join(parts)
    return str(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", default="samples/standard_female_voice_16k_mono_16bit.wav")
    parser.add_argument("--expected", default=DEFAULT_EXPECTED)
    parser.add_argument("--model", default="iic/SenseVoiceSmall")
    parser.add_argument("--output", default="results/funasr_asr_results.csv")
    args = parser.parse_args()

    Path("results").mkdir(exist_ok=True)
    audio = Path(args.audio)
    before_gpu = gpu_snapshot()
    status = "OK"
    err = ""
    text = ""
    load_seconds = 0.0
    infer_seconds = 0.0
    after_load_gpu = ""

    try:
        from funasr import AutoModel

        load_start = time.perf_counter()
        model = AutoModel(
            model=args.model,
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            trust_remote_code=True,
            device="cuda:0",
        )
        load_seconds = time.perf_counter() - load_start
        after_load_gpu = gpu_snapshot()

        infer_start = time.perf_counter()
        result = model.generate(input=str(audio), cache={}, language="zh", use_itn=True, batch_size_s=60)
        infer_seconds = time.perf_counter() - infer_start
        text = extract_text(result)
    except Exception as exc:
        status = "ERROR"
        err = repr(exc)
        after_load_gpu = gpu_snapshot()

    duration = audio_duration(audio)
    row = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "module": "FunASR/SenseVoiceSmall",
        "model": args.model,
        "audio": str(audio),
        "audio_seconds": round(duration, 3),
        "load_seconds": round(load_seconds, 3) if load_seconds else "",
        "infer_seconds": round(infer_seconds, 3) if infer_seconds else "",
        "rtf": round(infer_seconds / duration, 4) if duration and infer_seconds else "",
        "before_gpu": before_gpu,
        "after_load_gpu": after_load_gpu,
        "after_infer_gpu": gpu_snapshot(),
        "expected": args.expected,
        "recognized_text": text,
        "cer": cer(args.expected, text) if text else "",
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
