import argparse
import csv
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio


def load_audio_compat(path, *args, **kwargs):
    samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(samples.T.copy()), sample_rate


# New torchaudio releases require TorchCodec for load(); CosyVoice only needs WAV input here.
torchaudio.load = load_audio_compat


def wav_stats(path):
    with wave.open(str(path), "rb") as handle:
        sr = handle.getframerate()
        frames = handle.getnframes()
        samples = np.frombuffer(handle.readframes(frames), dtype="<i2")
    peak = int(np.max(np.abs(samples.astype(np.int32)))) if len(samples) else 0
    clipped = int(np.sum(np.abs(samples.astype(np.int32)) >= 32767))
    return frames / sr if sr else 0.0, peak, clipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--prompt-wav", required=True)
    parser.add_argument("--prompt-text", default="请介绍这个文物的历史背景。")
    parser.add_argument("--cases", default="tts_test_cases.txt")
    parser.add_argument("--output-dir", default="outputs_5090/cosyvoice2")
    parser.add_argument("--result", default="results_5090/cosyvoice2_results.csv")
    args = parser.parse_args()

    cosy_root = Path("third_party/CosyVoice").resolve()
    sys.path.insert(0, str(cosy_root))
    sys.path.insert(0, str(cosy_root / "third_party/Matcha-TTS"))
    from cosyvoice.cli.cosyvoice import CosyVoice2

    if not Path(args.model_dir).exists():
        raise FileNotFoundError("CosyVoice2 model not found. Run menu option 9 first.")
    if not Path(args.prompt_wav).exists():
        raise FileNotFoundError(f"Prompt WAV not found: {args.prompt_wav}")

    load_start = time.perf_counter()
    model = CosyVoice2(args.model_dir, load_jit=False, load_trt=False, load_vllm=False, fp16=True)
    load_seconds = time.perf_counter() - load_start
    texts = [line.strip() for line in Path(args.cases).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    for index, text in enumerate(texts, start=1):
        start = time.perf_counter()
        first_chunk = None
        chunks = []
        for result in model.inference_zero_shot(text, args.prompt_text, args.prompt_wav, stream=True):
            if first_chunk is None:
                first_chunk = time.perf_counter() - start
            chunks.append(result["tts_speech"].cpu())
        elapsed = time.perf_counter() - start
        audio = torch.cat(chunks, dim=1) if chunks else torch.zeros((1, 0))
        wav_path = output_dir / f"tts_{run_id}_{index:02d}.wav"
        samples = audio.squeeze(0).detach().cpu().float().numpy()
        sf.write(str(wav_path), samples, model.sample_rate, subtype="PCM_16")
        duration, peak, clipped = wav_stats(wav_path)
        rows.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "case_id": index,
            "text": text,
            "audio_file": str(wav_path),
            "load_seconds": round(load_seconds, 3),
            "first_audio_seconds": round(first_chunk, 3) if first_chunk is not None else "",
            "total_seconds": round(elapsed, 3),
            "audio_seconds": round(duration, 3),
            "rtf": round(elapsed / duration, 4) if duration else "",
            "peak_abs": peak,
            "clipped_samples": clipped,
        })
        print(f"[{index}] first={rows[-1]['first_audio_seconds']}s rtf={rows[-1]['rtf']} saved={wav_path}")

    out = Path(args.result)
    out.parent.mkdir(parents=True, exist_ok=True)
    exists = out.exists()
    with out.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
    print(f"Results saved: {out.resolve()}")


if __name__ == "__main__":
    main()
