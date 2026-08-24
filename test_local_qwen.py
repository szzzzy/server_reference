import argparse
import csv
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer


def gpu_used_mb():
    if not torch.cuda.is_available():
        return 0
    return round(torch.cuda.memory_allocated() / 1024**2)


def append_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--cases", default="llm_test_cases.json")
    parser.add_argument("--output", default="results_5090/llm_results.csv")
    parser.add_argument("--max-new-tokens", type=int, default=192)
    args = parser.parse_args()

    model_path = Path(args.model_dir)
    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}. Download it from the menu first.")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8-sig"))

    before_mb = gpu_used_mb()
    load_start = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype="auto",
        device_map="auto",
        trust_remote_code=True,
    )
    model.eval()
    load_seconds = time.perf_counter() - load_start
    loaded_mb = gpu_used_mb()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    rows = []

    for case in cases:
        messages = [
            {"role": "system", "content": "你是博物馆语音导览助手。回答准确、简洁，不编造资料。"},
            {"role": "user", "content": case["prompt"]},
        ]
        rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(rendered, return_tensors="pt").to(model.device)
        streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
        generation = dict(
            **inputs,
            streamer=streamer,
            max_new_tokens=args.max_new_tokens,
            do_sample=False,
        )
        torch.cuda.reset_peak_memory_stats() if torch.cuda.is_available() else None
        start = time.perf_counter()
        thread = threading.Thread(target=model.generate, kwargs=generation)
        thread.start()
        pieces = []
        first_token_seconds = None
        for piece in streamer:
            if first_token_seconds is None and piece:
                first_token_seconds = time.perf_counter() - start
            pieces.append(piece)
        thread.join()
        total_seconds = time.perf_counter() - start
        response = "".join(pieces).strip()
        output_tokens = len(tokenizer.encode(response, add_special_tokens=False))
        peak_mb = round(torch.cuda.max_memory_allocated() / 1024**2) if torch.cuda.is_available() else 0
        row = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "model": args.model_name,
            "case_id": case["id"],
            "category": case["category"],
            "prompt": case["prompt"],
            "response": response,
            "load_seconds": round(load_seconds, 3),
            "gpu_before_load_mb": before_mb,
            "gpu_loaded_mb": loaded_mb,
            "gpu_peak_case_mb": peak_mb,
            "first_token_seconds": round(first_token_seconds, 3) if first_token_seconds is not None else "",
            "total_seconds": round(total_seconds, 3),
            "output_tokens": output_tokens,
            "tokens_per_second": round(output_tokens / total_seconds, 3) if total_seconds else "",
        }
        rows.append(row)
        print(f"[{case['id']}] first={row['first_token_seconds']}s total={row['total_seconds']}s")
        print(response)

    append_rows(Path(args.output), rows)
    print(f"Results saved: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
