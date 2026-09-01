# -*- coding: utf-8 -*-
"""意图决策层(engine/intent.py)规则单元测试 —— 无需服务器,直接运行:
  .venv_5090_llm\\Scripts\\python.exe tools\\servertest_intent.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRV = HERE.parent / "server"
ENG = HERE.parent / "engine"
sys.path.insert(0, str(ENG))

from intent import decide_intent  # noqa: E402  (engine/ 在 sys.path,引擎同款导入方式)

CASES = [
    # (期望意图, 输入文本, 说明)
    ("sleep", "晚安，我要睡了。", "短语+标点 → 睡眠"),
    ("sleep", "我困了", "短语 → 睡眠"),
    ("sleep", "晚安", "独立词 → 睡眠"),
    ("sleep", "好的，晚安了", "时间变量+晚安了 → 睡眠"),
    ("question", "晚安是什么意思", "独立词门限: 含其它内容 → 不判睡眠"),
    ("question", "晚安祝福语怎么说", "同上(反例)"),
    ("decline", "不想聊了", "短语 → 拒绝"),
    ("decline", "就先这样吧，拜拜", "短语且句末结束词 → 拒绝"),
    ("decline", "我先走了，再见", "句末结束词 → 拒绝"),
    ("question", "再见，回头再聊这个展品", "结束词不在句末 → 不判拒绝(反例)"),
    ("decline", "我有点累了，不想说话了", "复合句+短语 → 拒绝(优先级高于共情)"),    ("no_reply", "嗯嗯", "干净短文本 → 不插话"),
    ("no_reply", "知道了", "干净短文本 → 不插话"),
    ("question", "嗯嗯，那二楼有什么", "超门限长文本 → 正常问答(反例)"),
    ("question", "走了半天还没找到展厅，累死了", "负面情绪(共情已移除) → 正常问答"),
    ("question", "听起来你有点失望", "同上"),
    ("question", "我不累", "同上"),
    ("question", "烦死了", "同上"),
    ("question", "这件展品是哪个朝代的", "普通提问 → 正常问答"),
    ("unknown", "", "空文本 → unknown(不入决策链)"),
]


def main():
    passed = failed = 0
    for expected, text, note in CASES:
        got = decide_intent(text)["intent"]
        ok = got == expected
        print(("PASS" if ok else "FAIL") + f"  {text!r} → {got}"
              + (f"(期望 {expected})  {note}" if not ok else f"  {note}"), flush=True)
        passed += ok
        failed += not ok
    print(f"\n意图层单测: passed={passed} failed={failed}", flush=True)
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
