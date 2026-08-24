import argparse
import csv
import math
import struct
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import serial
import serial.tools.list_ports

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
    x = samples.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(np.square(x))))
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
    active_samples = np.concatenate(active) if active else np.array([], dtype=np.int16)
    return rms_dbfs(active_samples), (active_count / frame_count if frame_count else 0.0)


def checksum8(data):
    return sum(data) & 0xFF


def list_ports():
    return [p.device for p in serial.tools.list_ports.comports()]


def open_serial(port, baud):
    return serial.Serial(port=port, baudrate=baud, timeout=2)


def discard_buffered_audio(ser, settle_seconds=0.15):
    ser.reset_input_buffer()
    time.sleep(settle_seconds)
    ser.reset_input_buffer()


def read_exact(ser, n):
    data = bytearray()
    while len(data) < n:
        chunk = ser.read(n - len(data))
        if not chunk:
            raise TimeoutError("serial read timeout")
        data.extend(chunk)
    return bytes(data)


def read_frame(ser):
    magic = b"PCM1"
    window = bytearray()
    while True:
        b = ser.read(1)
        if not b:
            raise TimeoutError("waiting for PCM1 frame")
        window.extend(b)
        if len(window) > 4:
            del window[0]
        if bytes(window) == magic:
            rest = read_exact(ser, 12)
            header = magic + rest
            seq = struct.unpack_from("<I", header, 4)[0]
            payload_len = struct.unpack_from("<H", header, 8)[0]
            dbfs_x100 = struct.unpack_from("<h", header, 10)[0]
            expected_sum = header[15]
            payload = read_exact(ser, payload_len)
            if checksum8(payload) != expected_sum:
                continue
            samples = np.frombuffer(payload, dtype="<i2").copy()
            return seq, dbfs_x100 / 100.0, samples


def capture_seconds(ser, seconds, sr=16000):
    samples = []
    dbfs_frames = []
    target = int(seconds * sr)
    start = time.perf_counter()
    while sum(len(x) for x in samples) < target:
        _, dbfs, frame = read_frame(ser)
        samples.append(frame)
        dbfs_frames.append(dbfs)
        if time.perf_counter() - start > seconds + 10:
            break
    if not samples:
        return np.array([], dtype=np.int16), []
    return np.concatenate(samples)[:target], dbfs_frames


def capture_until_endpoint(
    ser,
    max_seconds,
    background_dbfs,
    endpoint_silence_ms,
    threshold_above_bg=6.0,
    endpoint_threshold_above_bg=None,
    endpoint_active_penalty=4.0,
    voice_start_ms=100,
    voice_start_window_ms=None,
    sr=16000,
):
    samples = []
    dbfs_frames = []
    max_samples = int(max_seconds * sr)
    endpoint_samples = int(endpoint_silence_ms * sr / 1000)
    voice_start_samples = int(voice_start_ms * sr / 1000)
    if voice_start_window_ms is None:
        voice_start_window_ms = voice_start_ms
    voice_start_window_samples = int(voice_start_window_ms * sr / 1000)
    start_threshold_dbfs = background_dbfs + threshold_above_bg
    if endpoint_threshold_above_bg is None:
        endpoint_threshold_above_bg = threshold_above_bg
    endpoint_threshold_dbfs = background_dbfs + endpoint_threshold_above_bg
    total_samples = 0
    active_samples = 0
    start_activity_window = []
    start_activity_window_samples = 0
    trailing_silence_samples = 0
    speech_started = False
    endpoint_triggered = False
    speech_start_sample = None
    last_active_sample = None
    start = time.perf_counter()

    while total_samples < max_samples:
        _, board_dbfs, frame = read_frame(ser)
        remaining = max_samples - total_samples
        frame = frame[:remaining]
        if len(frame) == 0:
            break
        samples.append(frame)
        frame_dbfs = rms_dbfs(frame)
        dbfs_frames.append(board_dbfs)
        total_samples += len(frame)

        if not speech_started:
            frame_active_samples = len(frame) if frame_dbfs > start_threshold_dbfs else 0
            start_activity_window.append((len(frame), frame_active_samples))
            start_activity_window_samples += len(frame)
            active_samples += frame_active_samples
            while start_activity_window and start_activity_window_samples > voice_start_window_samples:
                old_total, old_active = start_activity_window.pop(0)
                start_activity_window_samples -= old_total
                active_samples -= old_active
            if active_samples >= voice_start_samples:
                speech_started = True
                speech_start_sample = max(0, total_samples - start_activity_window_samples)
                last_active_sample = total_samples
                trailing_silence_samples = 0
        else:
            if frame_dbfs > endpoint_threshold_dbfs:
                last_active_sample = total_samples
                # Do not reset the complete endpoint timer for one short click,
                # keyboard tap or level spike.  Active audio consumes the
                # accumulated silence faster than quiet audio adds it, so
                # continuous speech still keeps the counter near zero while
                # isolated 20 ms spikes no longer postpone the endpoint by a
                # full endpoint_silence_ms period.
                penalty = int(len(frame) * endpoint_active_penalty)
                trailing_silence_samples = max(0, trailing_silence_samples - penalty)
            else:
                trailing_silence_samples += len(frame)
                if trailing_silence_samples >= endpoint_samples:
                    endpoint_triggered = True
                    break

        if time.perf_counter() - start > max_seconds + 10:
            break

    captured = np.concatenate(samples) if samples else np.array([], dtype=np.int16)
    return captured, dbfs_frames, {
        "endpoint_triggered": endpoint_triggered,
        "speech_started": speech_started,
        "vad_threshold_dbfs": start_threshold_dbfs,
        "vad_start_threshold_dbfs": start_threshold_dbfs,
        "vad_endpoint_threshold_dbfs": endpoint_threshold_dbfs,
        "vad_endpoint_threshold_above_bg_db": endpoint_threshold_above_bg,
        "vad_endpoint_active_penalty": endpoint_active_penalty,
        "vad_voice_start_window_ms": voice_start_window_ms,
        "trailing_silence_ms": round(trailing_silence_samples * 1000 / sr),
        "speech_start_seconds": round(speech_start_sample / sr, 3) if speech_start_sample is not None else "",
        "last_active_seconds": round(last_active_sample / sr, 3) if last_active_sample is not None else "",
        "post_speech_wait_seconds": (
            round((total_samples - last_active_sample) / sr, 3)
            if last_active_sample is not None else ""
        ),
    }


def save_wav(path, samples, sr=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(samples.astype("<i2").tobytes())


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
    audio = samples.astype(np.float32) / 32768.0
    chunk_size = [0, 10, 5]
    chunk_stride = chunk_size[1] * 960
    cache = {}
    parts = []
    first_partial = ""
    start = time.perf_counter()
    total_chunks = int((len(audio) - 1) / chunk_stride + 1)
    for i in range(total_chunks):
        chunk = audio[i * chunk_stride : (i + 1) * chunk_stride]
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
    return "".join(parts), first_partial, time.perf_counter() - start


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
    parser = argparse.ArgumentParser(description="ESP32-S3 board microphone serial PCM to Paraformer ASR test.")
    parser.add_argument("--port", default="")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--distance", default="1m")
    parser.add_argument("--angle", default="0")
    parser.add_argument("--environment", default="未记录")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--record-seconds", type=float, default=5.0)
    parser.add_argument("--background-seconds", type=float, default=10.0)
    parser.add_argument("--endpoint-silence-ms", type=int, default=700)
    parser.add_argument("--vad-threshold-above-bg", type=float, default=6.0)
    parser.add_argument("--vad-end-threshold-above-bg", type=float, default=None)
    parser.add_argument("--vad-end-active-penalty", type=float, default=4.0)
    parser.add_argument("--voice-start-ms", type=int, default=100)
    parser.add_argument("--voice-start-window-ms", type=int, default=None)
    parser.add_argument("--pre-capture-settle-ms", type=int, default=150)
    parser.add_argument("--output", default="results/board_mic_asr_results.csv")
    parser.add_argument("--model", default="paraformer-zh-streaming")
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()

    if not args.port:
        ports = list_ports()
        print("Available ports:", ", ".join(ports) if ports else "none")
        args.port = input("Input ESP32 COM port, for example COM5: ").strip()
    if not args.port:
        raise RuntimeError("COM port is required.")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_dir = Path("recordings") / f"board_{run_id}"
    print(f"Opening board serial {args.port} @ {args.baud}...")
    ser = open_serial(args.port, args.baud)
    time.sleep(1)
    ser.reset_input_buffer()

    print("Loading Paraformer-online...")
    with GpuPeakMonitor() as monitor:
        model, device, load_seconds = load_paraformer(args.model, args.device)
        print(f"Model loaded on {device}, load_seconds={load_seconds:.3f}")
        print(f"GPU after load: {gpu_snapshot()}")

        input("Press Enter, then keep quiet near the board for background recording...")
        discard_buffered_audio(ser)
        bg, _ = capture_seconds(ser, args.background_seconds)
        background_dbfs = rms_dbfs(bg)
        save_wav(audio_dir / "background.wav", bg)
        print(f"Background dBFS_avg: {background_dbfs:.2f}")

        case_index = 0
        for sentence_id, sentence in enumerate(SENTENCES, start=1):
            for repeat in range(1, args.repeats + 1):
                case_index += 1
                print()
                print(f"[{case_index}] Distance={args.distance}, angle={args.angle}, repeat={repeat}")
                print(f"Read to the board: {sentence}")
                input("Press Enter, then speak when 'NOW SPEAK' appears...")
                discard_buffered_audio(ser, settle_seconds=max(0, args.pre_capture_settle_ms) / 1000.0)
                print("NOW SPEAK / 现在请讲话")
                samples, frame_dbfs, endpoint = capture_until_endpoint(
                    ser,
                    max_seconds=args.record_seconds,
                    background_dbfs=background_dbfs,
                    endpoint_silence_ms=args.endpoint_silence_ms,
                    threshold_above_bg=args.vad_threshold_above_bg,
                    endpoint_threshold_above_bg=args.vad_end_threshold_above_bg,
                    endpoint_active_penalty=args.vad_end_active_penalty,
                    voice_start_ms=args.voice_start_ms,
                    voice_start_window_ms=args.voice_start_window_ms,
                )
                wav_path = audio_dir / f"{args.distance}_{args.angle}_s{sentence_id:02d}_r{repeat}.wav"
                save_wav(wav_path, samples)
                all_dbfs = rms_dbfs(samples)
                voice_dbfs, active_ratio = active_voice_dbfs(samples, background_dbfs)
                snr = voice_dbfs - background_dbfs
                text, first_partial, infer_seconds = recognize(model, samples)
                row_cer = cer(sentence, text)
                row = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "run_id": run_id,
                    "source": "ESP32-S3 board microphone serial PCM",
                    "port": args.port,
                    "baud": args.baud,
                    "distance": args.distance,
                    "angle": args.angle,
                    "environment": args.environment,
                    "sentence_id": sentence_id,
                    "repeat": repeat,
                    "expected": sentence,
                    "audio_file": str(wav_path),
                    "record_seconds": round(len(samples) / 16000, 3),
                    "max_record_seconds": args.record_seconds,
                    "background_dbfs_avg": round(background_dbfs, 2),
                    "all_segment_dbfs_avg_reference": round(all_dbfs, 2),
                    "active_voice_dbfs_avg": round(voice_dbfs, 2),
                    "snr_db": round(snr, 2),
                    "active_frame_ratio": round(active_ratio, 4),
                    "first_partial_seconds": first_partial,
                    "final_result_seconds": round(infer_seconds, 3),
                    "recognized_text": text,
                    "cer": row_cer,
                    "keyword_ok": "是" if row_cer != "" and row_cer <= 0.35 else "否",
                    "acceptable": "是" if row_cer != "" and row_cer <= 0.35 else "否",
                    "asr_window_ms": 600,
                    "upload_packet_ms": 20,
                    "vad_endpoint_silence_ms": args.endpoint_silence_ms,
                    "vad_threshold_above_bg_db": args.vad_threshold_above_bg,
                    "vad_threshold_dbfs": round(endpoint["vad_threshold_dbfs"], 2),
                    "vad_start_threshold_above_bg_db": args.vad_threshold_above_bg,
                    "vad_start_threshold_dbfs": round(endpoint["vad_start_threshold_dbfs"], 2),
                    "vad_endpoint_threshold_above_bg_db": endpoint["vad_endpoint_threshold_above_bg_db"],
                    "vad_endpoint_threshold_dbfs": round(endpoint["vad_endpoint_threshold_dbfs"], 2),
                    "vad_endpoint_active_penalty": endpoint["vad_endpoint_active_penalty"],
                    "vad_voice_start_ms": args.voice_start_ms,
                    "vad_voice_start_window_ms": endpoint["vad_voice_start_window_ms"],
                    "pre_capture_settle_ms": args.pre_capture_settle_ms,
                    "vad_speech_started": "是" if endpoint["speech_started"] else "否",
                    "vad_endpoint_triggered": "是" if endpoint["endpoint_triggered"] else "否",
                    "vad_trailing_silence_ms": endpoint["trailing_silence_ms"],
                    "vad_speech_start_seconds": endpoint["speech_start_seconds"],
                    "vad_last_active_seconds": endpoint["last_active_seconds"],
                    "vad_post_speech_wait_seconds": endpoint["post_speech_wait_seconds"],
                    "gpu_peak_mb_observed": monitor.peak_mb if monitor.peak_mb is not None else "",
                    "gpu_after_case": gpu_snapshot(),
                }
                write_row(args.output, row)
                print(f"Recognized: {text}")
                print(f"SNR={snr:.2f} dB, CER={row_cer}, final={infer_seconds:.3f}s")
                print(
                    f"VAD endpoint={'triggered' if endpoint['endpoint_triggered'] else 'not triggered'}, "
                    f"audio={len(samples) / 16000:.3f}s, silence={endpoint['trailing_silence_ms']}ms"
                )
                print(f"Saved row: {args.output}")

    ser.close()
    print()
    print("Done.")
    print(f"Board mic recordings saved in: {audio_dir}")
    print(f"Results saved in: {args.output}")


if __name__ == "__main__":
    main()
