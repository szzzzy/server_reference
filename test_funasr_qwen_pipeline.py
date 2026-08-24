import argparse
import csv
import time
from datetime import datetime
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from asr_eval_core import load_paraformer, recognize_streaming


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--asr-model", required=True)
    parser.add_argument("--llm-model", required=True)
    parser.add_argument("--output", default="results_5090/funasr_qwen3_pipeline.csv")
    args = parser.parse_args()
    for path in (args.audio, args.asr_model, args.llm_model):
        if not Path(path).exists():
            raise FileNotFoundError(f"Required input not found: {path}")

    load_start = time.perf_counter()
    asr, _, _ = load_paraformer(args.asr_model, "auto")
    tokenizer = AutoTokenizer.from_pretrained(args.llm_model, trust_remote_code=True)
    llm = AutoModelForCausalLM.from_pretrained(args.llm_model, torch_dtype="auto", device_map="auto", trust_remote_code=True)
    load_seconds = time.perf_counter() - load_start

    asr_start = time.perf_counter()
    recognized, first_partial_seconds, asr_model_seconds = recognize_streaming(asr, args.audio)
    asr_seconds = time.perf_counter() - asr_start
    messages = [
        {"role": "system", "content": "你是博物馆语音导览助手。回答简洁准确，资料不足时明确说明。"},
        {"role": "user", "content": recognized},
    ]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(text, return_tensors="pt").to(llm.device)
    llm_start = time.perf_counter()
    with torch.inference_mode():
        output = llm.generate(**inputs, max_new_tokens=160, do_sample=False)
    llm_seconds = time.perf_counter() - llm_start
    response = tokenizer.decode(output[0][inputs.input_ids.shape[1] :], skip_special_tokens=True).strip()

    row = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "audio": str(Path(args.audio).resolve()),
        "recognized_text": recognized,
        "llm_response": response,
        "load_seconds": round(load_seconds, 3),
        "asr_seconds": round(asr_seconds, 3),
        "asr_model_seconds": round(asr_model_seconds, 3),
        "asr_first_partial_seconds": first_partial_seconds,
        "llm_seconds": round(llm_seconds, 3),
        "total_inference_seconds": round(asr_seconds + llm_seconds, 3),
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    exists = out.exists()
    with out.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    print(f"ASR: {recognized}")
    print(f"Qwen3: {response}")
    print(f"ASR={asr_seconds:.3f}s, LLM={llm_seconds:.3f}s")
    print(f"Result saved: {out.resolve()}")


if __name__ == "__main__":
    main()
