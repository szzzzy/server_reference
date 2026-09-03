# -*- coding: utf-8 -*-
"""对比: 系统提示词加"情绪判断"前后, 文本LLM的TTFT/prompt长度差异(临时验证脚本, 用完删)"""
import threading
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer

OLD = ("你是语音助手。请用中文自然、简练地回答用户问题，回答控制在2到3个连贯短句，"
       "共约30到50个汉字，第一句尽量不超过10个字，直接回答不要铺垫。用户打招呼时，"
       "只回一句简短问候并紧接着询问用户需求，不要只回复\"你好\"。用户语音可能存在"
       "同音字误识，请结合语境理解，不要追问或纠正用词。资料不足时简短说明，不要编造。")
NEW = OLD + ("在进行回复时对用户的情绪进行判断，如果察觉到明显情绪，"
             "开头使用\"听起来你很。。。\"这种句式。")

HISTORY = [
    ("请开始进行语音讲解。", "您好，欢迎使用语音服务。请问您想了解哪件展品？"),
    ("这件展品是什么年代的？", "根据现有资料，这件展品大致制作于明代。"),
    ("请返回主界面。", "已为您返回主界面。"),
    ("介绍一下这个展厅。", "这个展厅主要展示明清瓷器。"),
    ("请拍照识别这个文物。", "我无法拍照识别文物。请告诉我展品标签上的名称。"),
]


def main():
    tokenizer = AutoTokenizer.from_pretrained("models/Qwen3-4B-Instruct-2507",
                                              trust_remote_code=True, local_files_only=True)
    llm = AutoModelForCausalLM.from_pretrained(
        "models/Qwen3-4B-Instruct-2507", torch_dtype="auto", device_map="auto",
        trust_remote_code=True, local_files_only=True)

    def bench(label, system_prompt):
        messages = [{"role": "system", "content": system_prompt}]
        for user, assistant in HISTORY:
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": assistant})
        messages.append({"role": "user", "content": "现在开始进行标准女生麦克风"})
        prompt = tokenizer.apply_chat_template(messages, tokenize=False,
                                               add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
        n_tokens = inputs["input_ids"].shape[1]
        times = []
        for run in range(3):
            streamer = TextIteratorStreamer(tokenizer, skip_prompt=True,
                                            skip_special_tokens=True)
            thread = threading.Thread(target=llm.generate, kwargs=dict(
                **inputs, streamer=streamer, max_new_tokens=60, do_sample=False))
            thread.start()
            t0 = time.perf_counter()
            for _ in streamer:
                times.append(time.perf_counter() - t0)
                break
            thread.join()
        print(f"{label}: prompt_tokens={n_tokens} TTFT={[round(t, 3) for t in times]}  "
              f"min={min(times):.3f}s", flush=True)

    bench("OLD(原提示词)", OLD)
    bench("NEW(加情绪判断)", NEW)


if __name__ == "__main__":
    main()
