import argparse
import csv
import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
MODEL_DIR = PROJECT_ROOT / "models" / "CosyVoice2-0.5B"
COSY_ROOT = PROJECT_ROOT / "third_party" / "CosyVoice"
CONFIG_PATH = HERE / "config" / "tts_roles.json"
OUTPUT_ROOT = HERE / "outputs"
RESULT_ROOT = HERE / "results"


def load_audio_compat(path, *args, **kwargs):
    samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(samples.T.copy()), sample_rate


torchaudio.load = load_audio_compat


def load_model():
    if not MODEL_DIR.exists():
        raise FileNotFoundError(f"找不到模型：{MODEL_DIR}")
    if not COSY_ROOT.exists():
        raise FileNotFoundError(f"找不到CosyVoice源码：{COSY_ROOT}")
    sys.path.insert(0, str(COSY_ROOT))
    sys.path.insert(0, str(COSY_ROOT / "third_party" / "Matcha-TTS"))
    from cosyvoice.cli.cosyvoice import CosyVoice2

    started = time.perf_counter()
    model = CosyVoice2(str(MODEL_DIR), load_jit=False, load_trt=False,
                       load_vllm=False, fp16=True)
    return model, time.perf_counter() - started


def available_speakers(model):
    for name in ("list_available_spks", "list_avaliable_spks"):
        method = getattr(model, name, None)
        if callable(method):
            return list(method())
    return []


def speaker_for_gender(speakers, gender):
    for speaker in speakers:
        normalized = str(speaker).lower()
        if gender == "female" and any(marker in normalized for marker in ("女", "female", "woman")):
            return speaker
        if gender == "male" and "female" not in normalized and any(
            marker in normalized for marker in ("男", "male", "man")
        ):
            return speaker
    return None


def collect_audio(generator):
    chunks = []
    first_seconds = None
    started = time.perf_counter()
    for result in generator:
        if first_seconds is None:
            first_seconds = time.perf_counter() - started
        chunks.append(result["tts_speech"].cpu())
    elapsed = time.perf_counter() - started
    audio = torch.cat(chunks, dim=1) if chunks else torch.zeros((1, 0))
    return audio, first_seconds, elapsed


def save_audio(path, audio, sample_rate):
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = audio.squeeze(0).detach().cpu().float().numpy()
    sf.write(str(path), samples, sample_rate, subtype="PCM_16")
    with wave.open(str(path), "rb") as handle:
        pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
        duration = handle.getnframes() / handle.getframerate()
    peak = int(np.max(np.abs(pcm.astype(np.int32)))) if len(pcm) else 0
    clipped = int(np.sum(np.abs(pcm.astype(np.int32)) >= 32767))
    rms = float(np.sqrt(np.mean((pcm.astype(np.float64) / 32768.0) ** 2))) if len(pcm) else 0
    rms_dbfs = 20 * np.log10(max(rms, 1e-12))
    return duration, peak, clipped, rms_dbfs


def command_probe(model):
    speakers = available_speakers(model)
    official_assets = sorted(str(path) for path in (COSY_ROOT / "asset").glob("*.wav"))
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    out = RESULT_ROOT / "available_speakers.json"
    out.write_text(json.dumps({"speakers": speakers, "official_reference_assets": official_assets},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("模型内置音色：", speakers if speakers else "未提供可枚举的SFT音色")
    print("官方参考音频：", official_assets if official_assets else "未找到asset目录中的WAV")
    print("检测结果：", out)


def find_official_seed():
    asset_dir = COSY_ROOT / "asset"
    preferred = (
        "zero_shot_prompt.wav",
        "instruct_prompt.wav",
        "cross_lingual_prompt.wav",
    )
    for name in preferred:
        path = asset_dir / name
        if path.exists():
            return path
    candidates = sorted(asset_dir.glob("*.wav"))
    return candidates[0] if candidates else None


def build_references(model, config):
    speakers = available_speakers(model)
    reference_text = config["reference_text"]
    ref_dir = OUTPUT_ROOT / "references"
    manifest = {}
    seed = find_official_seed()
    if not speakers and seed is None:
        raise FileNotFoundError(f"模型没有SFT音色，且未找到官方参考音频：{COSY_ROOT / 'asset'}")

    for role in config["roles"]:
        speaker = speaker_for_gender(speakers, role["gender"])
        if speaker is not None:
            generator = model.inference_sft(reference_text, speaker, stream=False)
            source_mode = "model_sft_speaker"
            source = str(speaker)
        elif seed is not None and hasattr(model, "inference_instruct2"):
            generator = model.inference_instruct2(
                reference_text, role["instruction"], str(seed), stream=False
            )
            source_mode = "official_asset_instruct2"
            source = str(seed)
        else:
            print(f"[跳过] {role['name']}：既没有匹配SFT音色，也不能使用instruct2。")
            continue
        audio, first, elapsed = collect_audio(generator)
        path = ref_dir / f"{role['id']}_reference.wav"
        duration, peak, clipped, rms_dbfs = save_audio(path, audio, model.sample_rate)
        duration_ok = 3.0 <= duration <= 10.0
        manifest[role["id"]] = {
            "role_name": role["name"], "gender": role["gender"],
            "source_mode": source_mode, "source": source,
            "audio": str(path), "text": reference_text,
            "duration_seconds": round(duration, 3), "duration_standard_ok": duration_ok,
            "rms_dbfs": round(rms_dbfs, 2),
            "peak_abs": peak, "clipped_samples": clipped,
            "first_audio_seconds": round(first, 3) if first is not None else None,
            "total_seconds": round(elapsed, 3),
        }
        print(f"[{role['name']}] {source_mode} -> {path}")
        print(f"  时长={duration:.2f}秒，3-10秒标准={'通过' if duration_ok else '不通过'}")
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    out = RESULT_ROOT / "reference_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("参考音频清单：", out)
    return manifest


def command_test(model, load_seconds, config):
    manifest_path = RESULT_ROOT / "reference_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else build_references(model, config)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    rows = []
    for role in config["roles"]:
        ref = manifest.get(role["id"])
        if ref is None:
            print(f"[跳过] {role['name']}：缺少该角色参考音频。")
            continue
        prompt_wav = ref["audio"]
        for case_id, text in enumerate(config["test_texts"], start=1):
            generator = model.inference_zero_shot(text, ref["text"], prompt_wav, stream=True)
            mode = "zero_shot_role_reference"
            audio, first, elapsed = collect_audio(generator)
            wav_path = OUTPUT_ROOT / run_id / role["id"] / f"case_{case_id:02d}.wav"
            duration, peak, clipped, rms_dbfs = save_audio(wav_path, audio, model.sample_rate)
            rows.append({
                "time": datetime.now().isoformat(timespec="seconds"), "run_id": run_id,
                "role_id": role["id"], "role_name": role["name"], "gender": role["gender"],
                "age": role["age"], "intensity": role["intensity"], "style": role["style"],
                "instruction": role["instruction"], "mode": mode, "reference_audio": prompt_wav,
                "reference_text": ref["text"], "case_id": case_id, "text": text,
                "audio_file": str(wav_path), "model_load_seconds": round(load_seconds, 3),
                "first_audio_seconds": round(first, 3) if first is not None else "",
                "total_seconds": round(elapsed, 3), "audio_seconds": round(duration, 3),
                "rtf": round(elapsed / duration, 4) if duration else "",
                "rms_dbfs": round(rms_dbfs, 2), "peak_abs": peak, "clipped_samples": clipped,
            })
            print(f"[{role['name']} / {case_id}] first={rows[-1]['first_audio_seconds']}s rtf={rows[-1]['rtf']} -> {wav_path}")
    if not rows:
        raise RuntimeError("没有生成角色音频。请查看available_speakers.json和reference_manifest.json。")
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    result_path = RESULT_ROOT / f"tts_role_results_{run_id}.csv"
    with result_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("结果：", result_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("probe", "references", "test"))
    args = parser.parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    model, load_seconds = load_model()
    if args.command == "probe":
        command_probe(model)
    elif args.command == "references":
        build_references(model, config)
    else:
        command_test(model, load_seconds, config)


if __name__ == "__main__":
    main()
