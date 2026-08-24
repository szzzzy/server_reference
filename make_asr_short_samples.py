import csv
import math
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np
import soundfile as sf


SAMPLES = [
    ("cmd_001", "command", "开始介绍", "开始介绍"),
    ("cmd_002", "command", "停止讲解", "停止讲解"),
    ("cmd_003", "command", "拍照识别", "拍照识别"),
    ("cmd_004", "command", "返回首页", "返回首页"),
    ("cmd_005", "command", "音量调大", "音量调大"),
    ("cmd_006", "command", "音量调小", "音量调小"),
    ("qa_001", "dictation", "请介绍这个文物", "请介绍这个文物"),
    ("qa_002", "dictation", "这个文物是什么年代", "这个文物是什么年代"),
    ("qa_003", "dictation", "它有什么历史价值", "它有什么历史价值"),
    ("qa_004", "dictation", "请用简单的话讲解", "请用简单的话讲解"),
]


def write_beep_voice_like(path, text, sample_rate=16000):
    # Fallback audio is not real speech; it only prevents missing files if Windows TTS fails.
    duration = max(1.0, len(text) * 0.18)
    total = int(duration * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for i in range(total):
            amp = int(9000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            frames.extend(struct.pack("<h", amp))
        wav.writeframes(frames)


def make_tts(path, text):
    ps = f"""
Add-Type -AssemblyName System.Speech
$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speak.Rate = 0
$speak.Volume = 100
$speak.SetOutputToWaveFile('{path}')
$speak.Speak('{text}')
$speak.Dispose()
"""
    try:
        subprocess.check_call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
        return True
    except Exception:
        write_beep_voice_like(Path(path), text)
        return False


def ensure_16k_mono(path):
    wav_path = Path(path)
    try:
        audio, sr = sf.read(str(wav_path), dtype="float32")
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)
        if sr != 16000:
            x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
            new_len = int(round(len(audio) * 16000 / sr))
            x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
            audio = np.interp(x_new, x_old, audio).astype(np.float32)
        sf.write(str(wav_path), audio, 16000, subtype="PCM_16")
    except Exception:
        pass


def main():
    out_dir = Path("samples/asr_short")
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = []
    commands = []
    tts_ok_count = 0
    for case_id, kind, text, expected in SAMPLES:
        wav_path = out_dir / f"{case_id}.wav"
        ok = make_tts(str(wav_path.resolve()), text)
        ensure_16k_mono(wav_path)
        tts_ok_count += int(ok)
        cases.append({
            "case_id": case_id,
            "type": kind,
            "audio": str(wav_path).replace("\\", "/"),
            "expected": expected,
        })
        if kind == "command":
            commands.append({
                "case_id": case_id,
                "command": expected,
                "audio": str(wav_path).replace("\\", "/"),
                "expected_keyword": expected,
            })

    with Path("asr_cases.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "type", "audio", "expected"])
        writer.writeheader()
        writer.writerows(cases)

    with Path("command_cases.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "command", "audio", "expected_keyword"])
        writer.writeheader()
        writer.writerows(commands)

    print(f"Generated {len(cases)} short ASR samples in {out_dir}")
    print(f"Windows TTS success count: {tts_ok_count}/{len(cases)}")
    print("Updated asr_cases.csv and command_cases.csv")


if __name__ == "__main__":
    main()
