"""Isolate the real per-token cost of the CosyVoice2 speech LLM (Qwen2Encoder loop).

Steps the model actually does per token (llm.py L538-549):
  forward_one_step(inputs_embeds, masks, cache) -> y_pred, cache
  llm_decoder(y_pred[:, -1]).log_softmax(-1); sampling_ids(); lm_input = next token emb

We time the SAME loop shape with a synthetic single-token input for N steps,
measuring ms/step and effective tokens/sec.
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
    parser.add_argument("--steps", type=int, default=50)
    parser.add_argument("--blanken", default="",
                        help="若指定 CosyVoice-BlankEN 目录,直接用 transformers 加载,"
                             "并可强制 attention 实现(--attn)")
    parser.add_argument("--attn", default="", choices=["", "sdpa", "eager", "flash_attention_2"])
    args = parser.parse_args()

    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    if args.blanken:
        from transformers import AutoModelForCausalLM
        kwargs = {"torch_dtype": torch.bfloat16}
        if args.attn:
            kwargs["attn_implementation"] = args.attn
        qwen = AutoModelForCausalLM.from_pretrained(args.blanken, **kwargs).to("cuda")
        # 从 llm.pt 拿 decoder/speech_embedding,模拟真实生成循环
        sd = torch.load(str(Path(args.model_dir) / "llm.pt"), map_location="cpu",
                        weights_only=True)
        decoder = torch.nn.Linear(896, sd["llm_decoder.weight"].shape[0]).to("cuda")
        decoder.weight.data = sd["llm_decoder.weight"].float().to("cuda")
        decoder.bias.data = sd["llm_decoder.bias"].float().to("cuda")
        speech_emb = torch.nn.Embedding(sd["speech_embedding.weight"].shape[0], 896).to("cuda")
        speech_emb.weight.data = sd["speech_embedding.weight"].float().to("cuda")
        llm_dim = 896
        print(f"[blanken 直载] attn={args.attn or '默认'} impl={qwen.config._attn_implementation}"
              f" attn_class={type(qwen.model.layers[0].self_attn).__name__}", flush=True)
    else:
        sys.path.insert(0, args.cosy_root)
        sys.path.insert(0, str(Path(args.cosy_root) / "third_party" / "Matcha-TTS"))
        from cosyvoice.cli.cosyvoice import CosyVoice2
        model = CosyVoice2(args.model_dir, load_jit=False, load_trt=False,
                           load_vllm=False, fp16=True)
        qwen = model.model.llm.llm.model
        llm_dim = model.model.llm.llm_input_size
        decoder = model.model.llm.llm_decoder
        speech_emb = model.model.llm.speech_embedding
        print(f"[cosyvoice 路径] attn_class={type(qwen.model.layers[0].self_attn).__name__}",
              flush=True)

    device = next(qwen.parameters()).device
    assert str(device).startswith("cuda"), f"模型不在GPU上: {device}"
    print(f"qwen device: {device}, dim={llm_dim}", flush=True)

    emb = torch.randn(1, 1, llm_dim, device=device, dtype=torch.float32)
    cache = None
    times = []
    context = torch.cuda.stream(torch.cuda.Stream(device=device))
    start = time.perf_counter()
    for i in range(args.steps):
        with context, torch.cuda.amp.autocast(dtype=torch.float16):
            t0 = time.perf_counter()
            masks = torch.tril(torch.ones((1, emb.shape[1], emb.shape[1]),
                                          device=device)).to(torch.bool)
            y_pred_outs = qwen(inputs_embeds=emb, attention_mask=masks,
                               output_hidden_states=True, return_dict=True,
                               use_cache=True, past_key_values=cache)
            y_pred = y_pred_outs.hidden_states[-1]
            cache = y_pred_outs.past_key_values
            logp = decoder(y_pred[:, -1]).log_softmax(dim=-1)
            top_id = int(logp.argmax(dim=-1)[0].item())   # 纯贪心,近似 sampling 开销
            emb = speech_emb.weight[top_id].reshape(1, 1, -1)
        torch.cuda.synchronize()
        times.append(time.perf_counter() - t0)
    total = time.perf_counter() - start
    times = times[5:]  # 丢弃前5步的预热/首次 cache 分配
    avg_ms = sum(times) / len(times) * 1000
    print(json.dumps({
        "steps": args.steps,
        "avg_ms_per_step": round(avg_ms, 2),
        "tok_per_sec": round(1000 / avg_ms, 1),
        "min_ms": round(min(times) * 1000, 2),
        "max_ms": round(max(times) * 1000, 2),
        "total_seconds": round(total, 3),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
