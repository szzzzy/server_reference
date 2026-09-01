"""Benchmark CosyVoice2 first-chunk latency: native torch vs JIT (load_jit).

Usage (Windows):
  .venv_5090_tts\\Scripts\\python.exe bench_tts_first_chunk.py --jit 0
  .venv_5090_tts\\Scripts\\python.exe bench_tts_first_chunk.py --jit 1
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio


def load_audio_compat(path, *args, **kwargs):
    samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(samples.T.copy()), sample_rate


torchaudio.load = load_audio_compat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--cosy-root", required=True)
    parser.add_argument("--prompt-wav", required=True)
    parser.add_argument("--prompt-text", required=True)
    parser.add_argument("--jit", type=int, default=0)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cache-spk", type=str, default="",
                        help="非空时: 启动即 add_zero_shot_spk 并带 zero_shot_spk_id 推断(前端特征缓存)")
    args = parser.parse_args()

    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    sys.path.insert(0, args.cosy_root)
    sys.path.insert(0, str(Path(args.cosy_root) / "third_party" / "Matcha-TTS"))
    from cosyvoice.cli.cosyvoice import CosyVoice2

    t0 = time.perf_counter()
    model = CosyVoice2(args.model_dir, load_jit=bool(args.jit), load_trt=False,
                       load_vllm=False, fp16=True)
    load_seconds = time.perf_counter() - t0
    spk_id = args.cache_spk
    if spk_id:
        t_spk = time.perf_counter()
        model.add_zero_shot_spk(args.prompt_text, args.prompt_wav, spk_id)
        print("[spk cache] add_zero_shot_spk cost %.3fs" % (time.perf_counter() - t_spk),
              flush=True)

    text = "你好，需要什么帮助？"
    results = {"jit": bool(args.jit), "load_seconds": round(load_seconds, 3),
               "runs": [], "cache_spk": spk_id or None}
    for i in range(args.runs):
        t0 = time.perf_counter()
        first = None
        chunks = 0
        audio_seconds = 0.0
        for res in model.inference_zero_shot(text, args.prompt_text, args.prompt_wav,
                                             stream=True, zero_shot_spk_id=spk_id):
            now = time.perf_counter() - t0
            if first is None:
                first = now
            chunks += 1
            audio_seconds += res["tts_speech"].shape[-1] / model.sample_rate
        results["runs"].append({
            "first_chunk_seconds": round(first, 3) if first is not None else None,
            "total_seconds": round(time.perf_counter() - t0, 3),
            "chunks": chunks,
            "audio_seconds": round(audio_seconds, 3),
        })
    print(json.dumps(results, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
