import argparse
import csv
import time
from datetime import datetime
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from asr_eval_core import load_paraformer, recognize_streaming, cer


EXPECTED = {
    1: "请开始进行语音讲解。",
    2: "停止讲解。",
    3: "请拍照识别这个文物。",
    4: "请返回主界面。",
    5: "请介绍这个文物的历史背景。",
    6: "这个展品是什么年代的？",
    7: "请告诉我它的主要用途。",
    8: "这个文物有什么文化价值？",
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audio-root", default="recordings")
    p.add_argument("--asr-model", default="models/paraformer-zh-streaming")
    p.add_argument("--llm-model", default="models/Qwen3-4B-Instruct-2507")
    p.add_argument("--output", default="results_5090/funasr_qwen3_vad_tuned_batch.csv")
    args = p.parse_args()

    root = Path(args.audio_root)
    files = sorted(root.glob("board_20260820_105022/*_s??_r*.wav")) + sorted(root.glob("board_20260820_105556/*_s??_r*.wav"))
    if not files:
        raise FileNotFoundError("未找到今天调整后录音：recordings\\board_20260820_105022 或 board_20260820_105556")

    load_start = time.perf_counter()
    asr, _, _ = load_paraformer(args.asr_model, "auto")
    tokenizer = AutoTokenizer.from_pretrained(args.llm_model, trust_remote_code=True)
    llm = AutoModelForCausalLM.from_pretrained(args.llm_model, torch_dtype="auto", device_map="auto", trust_remote_code=True)
    llm.eval()
    load_seconds = time.perf_counter() - load_start
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["time", "audio", "distance", "sentence_id", "expected", "recognized_text", "asr_cer", "llm_response", "asr_seconds", "asr_first_partial_seconds", "llm_seconds", "total_inference_seconds", "model_load_seconds"]
    rows = []
    for audio in files:
        name = audio.stem
        sid = int(name.split("_s")[1].split("_")[0])
        distance = "1m" if "board_20260820_105022" in str(audio) else "2m"
        expected = EXPECTED[sid]
        t0 = time.perf_counter()
        recognized, first_partial, asr_seconds = recognize_streaming(asr, audio)
        messages = [
            {"role": "system", "content": "你是博物馆语音导览助手。回答简洁准确，资料不足时明确说明。"},
            {"role": "user", "content": recognized},
        ]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
        llm_start = time.perf_counter()
        with torch.inference_mode():
            output = llm.generate(**inputs, max_new_tokens=160, do_sample=False)
        llm_seconds = time.perf_counter() - llm_start
        response = tokenizer.decode(output[0][inputs.input_ids.shape[1]:], skip_special_tokens=True).strip()
        rows.append({
            "time": datetime.now().isoformat(timespec="seconds"), "audio": str(audio), "distance": distance,
            "sentence_id": sid, "expected": expected, "recognized_text": recognized, "asr_cer": cer(expected, recognized),
            "llm_response": response, "asr_seconds": round(asr_seconds, 3), "asr_first_partial_seconds": first_partial,
            "llm_seconds": round(llm_seconds, 3), "total_inference_seconds": round(asr_seconds + llm_seconds, 3),
            "model_load_seconds": round(load_seconds, 3),
        })
        print(f"[{distance} s{sid:02d}] ASR={recognized} | CER={rows[-1]['asr_cer']} | LLM={response}")
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    print(f"Saved: {out.resolve()}")


if __name__ == "__main__":
    main()
