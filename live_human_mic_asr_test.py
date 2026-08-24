import argparse
import csv
import math
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

from asr_eval_core import cer, extract_text, gpu_snapshot, load_paraformer


SENTENCES = [
    "请开始进行语音讲解。",
    "停止讲解。",
    "请拍照识别这个文物。",
    "请返回主界面。",
    "请介绍这个文物的历史背景。",
    "这个展品是什么年代的？",
    "请告诉我它的主要用途。",
    "这个文物有什么文化价值？",
]


def rms_dbfs(samples):
    if len(samples) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
    if rms <= 1e-12:
        return -120.0
    return 20.0 * math.log10(rms)


def active_voice_dbfs(samples, background_dbfs, sr=16000, frame_ms=20, threshold_above_bg=3.0):
    frame_len = max(1, int(sr * frame_ms / 1000))
    threshold = background_dbfs + threshold_above_bg
    active = []
    frame_count = 0
    active_count = 0
    for start in range(0, len(samples), frame_len):
        frame = samples[start : start + frame_len]
        if len(frame) == 0:
            continue
        frame_count += 1
        if rms_dbfs(frame) > threshold:
            active.append(frame)
            active_count += 1
    if active:
        active_samples = np.concatenate(active)
    else:
        active_samples = np.array([], dtype=np.float32)
    return rms_dbfs(active_samples), (active_count / frame_count if frame_count else 0.0)


def save_wav(path, samples, sr=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(pcm.tobytes())


def record_seconds(seconds, sr=16000):
    audio = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="float32")
    sd.wait()
    return audio.reshape(-1)


def query_gpu_used_mb():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
        return int(out.splitlines()[0].strip())
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
            if used is not None and (self.peak_mb is None or used > self.peak_mb):
                self.peak_mb = used
            time.sleep(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join(timeout=2)


def recognize(model, samples):
    chunk_size = [0, 10, 5]
    chunk_stride = chunk_size[1] * 960
    cache = {}
    parts = []
    first_partial = ""
    start = time.perf_counter()
    total_chunks = int((len(samples) - 1) / chunk_stride + 1)
    for i in range(total_chunks):
        chunk = samples[i * chunk_stride : (i + 1) * chunk_stride]
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


def write_row(path, row):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out.exists()
    with out.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Live human voice microphone ASR test.")
    parser.add_argument("--distance", default="1m")
    parser.add_argument("--angle", default="0")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--record-seconds", type=float, default=5.0)
    parser.add_argument("--background-seconds", type=float, default=10.0)
    parser.add_argument("--model", default="paraformer-zh-streaming")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--output", default="results/live_human_mic_asr_results.csv")
    args = parser.parse_args()

    sr = 16000
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_dir = Path("recordings") / run_id
    print("Loading Paraformer-online...")
    with GpuPeakMonitor() as monitor:
        model, device, load_seconds = load_paraformer(args.model, args.device)
        print(f"Model loaded on {device}, load_seconds={load_seconds:.3f}")
        print(f"GPU after load: {gpu_snapshot()}")

        input("Press Enter, then keep quiet for background noise recording...")
        bg = record_seconds(args.background_seconds, sr)
        background_dbfs = rms_dbfs(bg)
        save_wav(audio_dir / "background.wav", bg, sr)
        print(f"Background dBFS_avg: {background_dbfs:.2f}")

        case_index = 0
        for sentence_id, sentence in enumerate(SENTENCES, start=1):
            for repeat in range(1, args.repeats + 1):
                case_index += 1
                print()
                print(f"[{case_index}] Distance={args.distance}, angle={args.angle}, repeat={repeat}")
                print(f"Read: {sentence}")
                input("Press Enter, read once, then stay quiet until recording ends...")
                samples = record_seconds(args.record_seconds, sr)
                wav_path = audio_dir / f"{args.distance}_{args.angle}_s{sentence_id:02d}_r{repeat}.wav"
                save_wav(wav_path, samples, sr)

                all_dbfs = rms_dbfs(samples)
                voice_dbfs, active_ratio = active_voice_dbfs(samples, background_dbfs, sr)
                snr = voice_dbfs - background_dbfs
                text, first_partial, infer_seconds = recognize(model, samples)
                keyword_ok = "是" if cer(sentence, text) <= 0.35 else "否"
                acceptable = "是" if cer(sentence, text) <= 0.35 else "否"
                row = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "run_id": run_id,
                    "distance": args.distance,
                    "angle": args.angle,
                    "sentence_id": sentence_id,
                    "repeat": repeat,
                    "expected": sentence,
                    "audio_file": str(wav_path),
                    "record_seconds": args.record_seconds,
                    "background_dbfs_avg": round(background_dbfs, 2),
                    "all_segment_dbfs_avg_reference": round(all_dbfs, 2),
                    "active_voice_dbfs_avg": round(voice_dbfs, 2),
                    "snr_db": round(snr, 2),
                    "active_frame_ratio": round(active_ratio, 4),
                    "first_partial_seconds": first_partial,
                    "final_result_seconds": round(infer_seconds, 3),
                    "recognized_text": text,
                    "cer": cer(sentence, text),
                    "keyword_ok": keyword_ok,
                    "acceptable": acceptable,
                    "asr_window_ms": 600,
                    "upload_packet_ms": 20,
                    "vad_endpoint_silence_ms": 700,
                    "gpu_peak_mb_observed": monitor.peak_mb if monitor.peak_mb is not None else "",
                    "gpu_after_case": gpu_snapshot(),
                }
                write_row(args.output, row)
                print(f"Recognized: {text}")
                print(f"SNR={snr:.2f} dB, CER={row['cer']}, final={infer_seconds:.3f}s")
                print(f"Saved row: {args.output}")

    print()
    print("Done.")
    print(f"Recordings saved in: {audio_dir}")
    print(f"Results saved in: {args.output}")


if __name__ == "__main__":
    main()
