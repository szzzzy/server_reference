# -*- coding: utf-8 -*-
"""对比: 三种提示词 × 三类用户输入 的输出(临时验证脚本)
P0 原版(无情绪) | P1 原版+情绪(用户草稿) | P2 分层版(共情句≤10字/仅明显情绪)
"""
import sys
import time

from transformers import AutoModelForCausalLM, AutoTokenizer

sys.stdout.reconfigure(encoding="utf-8")

P0 = ("你是语音助手。请用中文自然、简练地回答用户问题，回答控制在2到3个连贯短句，"
      "共约30到50个汉字，第一句尽量不超过10个字，直接回答不要铺垫。用户打招呼时，"
      "只回一句简短问候并紧接着询问用户需求，不要只回复\"你好\"。用户语音可能存在"
      "同音字误识，请结合语境理解，不要追问或纠正用词。资料不足时简短说明，不要编造。")

P1 = P0 + ("在进行回复时对用户的情绪进行判断，如果察觉到明显情绪，"
           "开头使用\"听起来你很。。。\"这种句式。")

P2 = ("你是语音助手。请用中文自然、简练地回答用户问题，回答2到3个连贯短句，共约"
      "30到50个汉字，第一句尽量不超过10个字，直接回答不要铺垫。用户打招呼时，只回"
      "一句简短问候并紧接着询问用户需求。用户语音可能有同音字误识，结合语境理解，"
      "不要追问或纠正用词。资料不足时简短说明，不要编造。若用户明显有负面情绪"
      "（如烦、累、担心、失望等），第一句换成简短共情：以\"听起来你很\"开头加一个"
      "二字情绪词（如：听起来你很累），不超过10字，随后照常简短回答；用户没有明显"
      "情绪时禁止共情、直接回答。")

CASES = [
    ("中性提问", "这件展品是哪个朝代的"),
    ("明显情绪", "我找了半天也没找到这个展厅，烦死了"),
    ("疲惫", "逛了半天太累了，走不动了"),
    ("打招呼", "你好"),
]


def main():
    tokenizer = AutoTokenizer.from_pretrained("models/Qwen3-4B-Instruct-2507",
                                              trust_remote_code=True, local_files_only=True)
    llm = AutoModelForCausalLM.from_pretrained(
        "models/Qwen3-4B-Instruct-2507", torch_dtype="auto", device_map="auto",
        trust_remote_code=True, local_files_only=True)

    def ask(system_prompt, user_text):
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False,
                                               add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(llm.device)
        t0 = time.perf_counter()
        out = llm.generate(**inputs, max_new_tokens=80, do_sample=False)
        text = tokenizer.decode(out[0][inputs["input_ids"].shape[1]:],
                                skip_special_tokens=True).strip()
        return text, time.perf_counter() - t0

    for label, sysp in (("P0 原版", P0), ("P1 用户草稿", P1), ("P2 分层版", P2)):
        print(f"\n===== {label} =====", flush=True)
        for tag, user_text in CASES:
            text, dt = ask(sysp, user_text)
            text = text.replace("\n", " ")
            first_sentence = text.split("。")[0]
            cn = len([c for c in text if "\u4e00" <= c <= "\u9fff"])
            print(f"[{tag}] {user_text}", flush=True)
            print(f"  回答: {text}", flush=True)
            print(f"  字数={cn} 首句={len([c for c in first_sentence if chr(19968) <= c <= chr(40959)])}字 "
                  f"共情开头={'听起来你很' in text[:8]} 耗时={dt:.2f}s", flush=True)


if __name__ == "__main__":
    main()
