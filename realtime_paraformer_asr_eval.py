import argparse
import csv
import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from asr_eval_core import audio_duration, cer, extract_text, gpu_snapshot, load_audio_16k_mono, load_paraformer


def query_gpu_used_mb():
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
        return int(str(out).splitlines()[0].strip())
    except Exception:
        return None


class GpuPeakMonitor:
    def __init__(self, interval=0.1):
        self.interval = interval
        self.peak_mb = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            used = query_gpu_used_mb()
            if used is not None:
                if self.peak_mb is None or used > self.peak_mb:
                    self.peak_mb = used
            time.sleep(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join(timeout=2)


def write_row(path, row):
    out = Path(path)
    out.parent.mkdir(exist_ok=True)
    write_header = not out.exists()
    with out.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def load_reference_text(audio_path, explicit_expected, explicit_reference_file):
    if explicit_expected.strip():
        return explicit_expected.strip()
    reference_path = Path(explicit_reference_file) if explicit_reference_file.strip() else Path(audio_path).with_suffix(".txt")
    if reference_path.exists():
        return reference_path.read_text(encoding="utf-8-sig").strip()
    raise FileNotFoundError(
        f"完整标准稿不存在: {reference_path}. "
        "请提供 --expected-file 或在 WAV 同目录放置同名 .txt，禁止无参考稿计算正确率。"
    )


def stream_eval(
    model,
    audio_path,
    expected,
    packet_ms,
    asr_window_ms,
    endpoint_silence_ms,
    realtime_sleep,
):
    speech, _ = load_audio_16k_mono(audio_path)
    sr = 16000
    packet_samples = max(1, int(sr * packet_ms / 1000))
    asr_window_samples = max(1, int(sr * asr_window_ms / 1000))

    # Paraformer streaming uses chunk_size[1] * 960 samples as one recognition stride.
    middle_chunk = max(1, round(asr_window_samples / 960))
    chunk_stride = middle_chunk * 960
    chunk_size = [0, middle_chunk, 5]

    cache = {}
    parts = []
    first_partial_wall = ""
    first_partial_audio_pos = ""
    calls = 0
    processed_until = 0
    start = time.perf_counter()

    for packet_start in range(0, len(speech), packet_samples):
        packet_end = min(packet_start + packet_samples, len(speech))
        if realtime_sleep:
            target_time = packet_end / sr
            now_audio_time = time.perf_counter() - start
            delay = target_time - now_audio_time
            if delay > 0:
                time.sleep(delay)

        while packet_end - processed_until >= chunk_stride:
            chunk = speech[processed_until : processed_until + chunk_stride]
            result = model.generate(
                input=chunk,
                cache=cache,
                is_final=False,
                chunk_size=chunk_size,
                encoder_chunk_look_back=4,
                decoder_chunk_look_back=1,
            )
            calls += 1
            text = extract_text(result)
            if text:
                if first_partial_wall == "":
                    first_partial_wall = round(time.perf_counter() - start, 3)
                    first_partial_audio_pos = round((processed_until + len(chunk)) / sr, 3)
                parts.append(text)
            processed_until += chunk_stride

    if processed_until < len(speech):
        chunk = speech[processed_until:]
        result = model.generate(
            input=chunk,
            cache=cache,
            is_final=True,
            chunk_size=chunk_size,
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        calls += 1
        text = extract_text(result)
        if text:
            if first_partial_wall == "":
                first_partial_wall = round(time.perf_counter() - start, 3)
                first_partial_audio_pos = round(len(speech) / sr, 3)
            parts.append(text)

    elapsed = time.perf_counter() - start
    recognized = "".join(parts)
    duration = len(speech) / sr
    final_after_audio_end = round(max(0.0, elapsed - duration), 3) if realtime_sleep else ""
    return {
        "audio_seconds": round(duration, 3),
        "packet_ms": packet_ms,
        "asr_window_ms": asr_window_ms,
        "endpoint_silence_ms": endpoint_silence_ms,
        "paraformer_chunk_size": str(chunk_size),
        "asr_calls": calls,
        "realtime_sleep": int(realtime_sleep),
        "first_partial_wall_seconds": first_partial_wall,
        "first_partial_audio_pos_seconds": first_partial_audio_pos,
        "final_wall_seconds": round(elapsed, 3),
        "final_after_audio_end_seconds": final_after_audio_end,
        "rtf": round(elapsed / duration, 4) if duration else "",
        "expected": expected,
        "recognized_text": recognized,
        "cer": cer(expected, recognized) if expected else "",
    }


def main():
    parser = argparse.ArgumentParser(description="Replay an audio file as small packets to test realtime-like ASR behavior.")
    parser.add_argument("--audio", default="samples/standard_female_voice_16k_mono_16bit.wav")
    parser.add_argument(
        "--expected",
        default="",
        help="Complete verbatim transcript. Prefer --expected-file for reproducible tests.",
    )
    parser.add_argument(
        "--expected-file",
        default="",
        help="UTF-8 complete verbatim transcript file; defaults to WAV with .txt suffix.",
    )
    parser.add_argument("--model", default="paraformer-zh-streaming")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--packet-ms", type=int, default=20)
    parser.add_argument("--asr-window-ms", type=int, default=600)
    parser.add_argument("--endpoint-silence-ms", type=int, default=700)
    parser.add_argument("--realtime-sleep", action="store_true")
    parser.add_argument("--output", default="results/paraformer_realtime_stream_results.csv")
    args = parser.parse_args()

    audio = Path(args.audio)
    try:
        expected = load_reference_text(audio, args.expected, args.expected_file)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    before_gpu = gpu_snapshot()
    status = "OK"
    error = ""
    after_load_gpu = ""
    load_seconds = ""
    result = {}

    try:
        with GpuPeakMonitor() as monitor:
            model, device, load_elapsed = load_paraformer(args.model, args.device)
            load_seconds = round(load_elapsed, 3)
            after_load_gpu = gpu_snapshot()
            result = stream_eval(
                model=model,
                audio_path=audio,
                expected=expected,
                packet_ms=args.packet_ms,
                asr_window_ms=args.asr_window_ms,
                endpoint_silence_ms=args.endpoint_silence_ms,
                realtime_sleep=args.realtime_sleep,
            )
            peak_gpu_mb = monitor.peak_mb
    except Exception as exc:
        status = "ERROR"
        error = repr(exc)
        device = args.device
        peak_gpu_mb = query_gpu_used_mb()

    row = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "test_type": "realtime_packet_replay_baseline",
        "module": "Paraformer-online/paraformer-zh-streaming",
        "model": args.model,
        "device": device,
        "audio": str(audio),
        "source_audio_seconds": round(audio_duration(audio), 3),
        "load_seconds": load_seconds,
        "before_gpu": before_gpu,
        "after_load_gpu": after_load_gpu,
        "peak_gpu_used_mb_observed": peak_gpu_mb if peak_gpu_mb is not None else "",
        "after_infer_gpu": gpu_snapshot(),
        "status": status,
        "error": error,
    }
    row.update(result)
    write_row(args.output, row)

    print(json.dumps(row, ensure_ascii=False, indent=2))
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
