"""Benchmark Qwen3 LLM first-token (TTFT) with a realistic 10-turn prompt.

Usage (Windows):
  .venv_5090_llm\\Scripts\\python.exe bench_llm_ttft.py --model-dir models/Qwen3-4B-Instruct-2507
"""
import argparse
import json
import sys
import time
from pathlib import Path

SYSTEM = ("你是语音助手。请用中文自然、简练地回答用户问题，回答控制在2到3个连贯短句，共约30到50个汉字，"
          "第一句尽量不超过10个字，直接回答不要铺垫。用户打招呼时，只回一句简短问候并紧接着询问用户需求，"
          "不要只回复\"你好\"。用户语音可能存在同音字误识，请结合语境理解，不要追问或纠正用词。"
          "资料不足时简短说明，不要编造。")

# Simulate 10 turns of history (top-level conversation.history_turns=10)
HISTORY = [
    ("请开始进行语音讲解。", "您好，欢迎使用语音服务。请问您想了解哪件展品？"),
    ("这件展品是什么年代的？", "根据现有资料，这件展品大致制作于明代。"),
    ("请返回主界面。", "已为您返回主界面。"),
    ("介绍一下这个展厅。", "这个展厅主要展示明清瓷器。"),
    ("请拍照识别这个文物。", "我无法拍照识别文物。请告诉我展品标签上的名称。"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--user-text", default="现在开始进行标准女生麦克风")
    parser.add_argument("--max-new-tokens", type=int, default=60)
    args = parser.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer

    t0 = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, trust_remote_code=True,
                                              local_files_only=True)
    llm = AutoModelForCausalLM.from_pretrained(
        args.model_dir, torch_dtype="auto", device_map="auto",
        trust_remote_code=True, local_files_only=True)
    load_seconds = time.perf_counter() - t0

    messages = [{"role": "system", "content": SYSTEM}]
    for user, assistant in HISTORY:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": args.user_text})

    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
    prompt_tokens = inputs["input_ids"].shape[1]

    out = {"load_seconds": round(load_seconds, 3), "prompt_tokens": prompt_tokens, "runs": []}
    for run in range(3):
        streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
        generation = dict(**inputs, streamer=streamer, max_new_tokens=args.max_new_tokens,
                          do_sample=False)
        import threading
        t0 = time.perf_counter()
        thread = threading.Thread(target=llm.generate, kwargs=generation)
        thread.start()
        first = None
        tokens = []
        for piece in streamer:
            now = time.perf_counter() - t0
            if first is None:
                first = now
            tokens.append(piece)
        thread.join()
        total = time.perf_counter() - t0
        out["runs"].append({
            "ttft_seconds": round(first, 3),
            "total_seconds": round(total, 3),
            "completion_tokens": len(tokenizer.encode("".join(tokens),
                                                       add_special_tokens=False)),
            "text": "".join(tokens),
        })
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
