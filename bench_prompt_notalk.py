# -*- coding: utf-8 -*-
"""验证"三层分支"提示词: 不插话 / 共情 / 正常回答 的冲突消解与输出形态(临时脚本)"""
import sys
import time

from transformers import AutoModelForCausalLM, AutoTokenizer

sys.stdout.reconfigure(encoding="utf-8")

BASE = ("你是语音助手。请用中文自然、简练地回答用户问题，回答控制在2到3个连贯短句，"
        "共约30到50个汉字，第一句尽量不超过10个字，直接回答不要铺垫。用户打招呼时，"
        "只回一句简短问候并紧接着询问用户需求。用户语音可能有同音字误识，结合语境理解，"
        "不要追问或纠正用词。资料不足时简短说明，不要编造。")

# A: 用户草稿风格(平级罗列)
P_OBJ = BASE + ("进行回复时判断用户情绪，若明显负面情绪，开头用\"听起来你很…\"句式。"
                "若用户表示不想继续对话（如：不想聊了/再见/就这样吧），则不插话，"
                "只回一句简短收尾或不回复内容。")

# B: 三层分支(否则链, 优先级明确)
P_LAYER = BASE + ("回答前按优先级判断用户意图：1. 若用户明确表示不想继续对话"
                  "（如\"不想聊了/再见/就这样了/不用了\"），只需回一句不超过10字的收尾"
                  "（如：好的，随时找我），不做任何追问；2. 否则若用户明显有负面情绪"
                  "（如烦、累、担心、失望），第一句以\"听起来你很…\"开头共情、该句不超"
                  "10字，随后照常简短回答；3. 否则正常回答。满足第1项时禁止共情与追问。")

CASES = [
    ("不想聊", "我不想聊了，就这样吧"),
    ("告别", "再见，我先走了"),
    ("敷衍", "嗯嗯，知道了"),
    ("负面情绪", "走了半天还没找到展厅，累死了"),
    ("中性问题", "这件展品是哪个朝代的"),
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
        out = llm.generate(**inputs, max_new_tokens=80, do_sample=False)
        return tokenizer.decode(out[0][inputs["input_ids"].shape[1]:],
                                skip_special_tokens=True).strip().replace("\n", " ")

    for label, sysp in (("A 平级罗列", P_OBJ), ("B 三层分支", P_LAYER)):
        print(f"\n===== {label} =====", flush=True)
        for tag, user_text in CASES:
            text = ask(sysp, user_text)
            first = text.split("。")[0]
            cn = len([c for c in text if "\u4e00" <= c <= "\u9fff"])
            print(f"[{tag}] {user_text}", flush=True)
            print(f"  回答: {text}", flush=True)
            print(f"  字数={cn} 首句={len([c for c in first if chr(19968) <= c <= chr(40959)])}字", flush=True)


if __name__ == "__main__":
    main()
