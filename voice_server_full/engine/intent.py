# -*- coding: utf-8 -*-
"""意图决策层 v1(规则层):ASR 文本 → 意图标签(睡眠/拒绝/不插话/正常问答)。

纯规则实现:无模型依赖、毫秒级、确定性,可单测(tools/servertest_intent.py)。
LLM 语义兜底(Qwen2.5-1.5B)为二期,不在此文件。
(注: 共情功能当前无产品需求,已整体移除;负面情绪归入 question 正常回答。)

判定次序(优先级自上而下,命中即返回):
  sleep > decline > no_reply > question
状态过滤: 仅"唤醒态/持续对话"内调用(待机态由唤醒词状态机处理,不经过本层)。

防误判四原则:
  1) 短语优先于单字(词表为完整短语,不匹配单字);
  2) 强意图(睡眠/拒绝)文本长度门限 gate_chars,超长按 question(复合句不误伤);
  3) 结束类词(再见/拜拜)要求位于文本末尾,避免"再见,那个展品在哪"误判;
  4) 不插话(no_reply)仅对干净短文本(≤no_reply_gate_chars)生效,防"嗯嗯,那二楼有什么"漏答。
引擎侧两级判定(见 real_engine.py): 单次 no_reply 只算"疑似"——按正常对话处理并记录计数,
连续 ≥ no_reply_tolerance_rounds(默认3)次才确认敷衍 → 结束会话(dismiss)。
"""
import re

INTENTS = ("sleep", "decline", "no_reply", "question", "unknown")

DEFAULT_CFG = {
    # 睡眠(短语:文本包含即命中)
    "sleep_phrases": ["我要睡了", "想睡了", "睡觉了", "想睡觉", "困了", "好困", "太困了",
                      "睡吧", "洗洗睡了", "关灯睡了", "先睡了"],
    # 睡眠(独立词:文本整体等于该词才命中,防"晚安的含义是什么"误判)
    "sleep_standalone": ["晚安", "晚安了", "说晚安"],
    # 拒绝(短语:文本包含即命中)—— 含 S5 触发词(不被打扰/想静静类),映射 dismiss
    "decline_phrases": ["不想聊了", "不想说了", "不想说话了", "不想讲话了", "不聊了",
                        "不想再聊了", "就这样吧", "就这样了",
                        "别再问了", "别说了", "不用了", "算了", "挂了吧", "结束对话", "不说了",
                        "别烦我", "想静静", "别打扰我", "让我静静", "我想静静", "我要静静",
                        "别理我", "安静一点", "清静一下"],
    # 拒绝(结束词:文本以该词结尾才命中)
    "decline_suffix": ["再见", "拜拜", "再见了", "拜拜了", "走了"],
    # 不插话(仅对 ≤no_reply_gate_chars 的干净短文本生效)
    "no_reply_words": ["嗯嗯", "嗯", "好", "好的", "行", "哦", "哦哦", "收到", "明白",
                       "知道了", "嗯好", "好呀"],
    "gate_chars": 12,
    "suffix_gate_chars": 12,
    "no_reply_gate_chars": 4,
    "disabled": [],
}

_PUNCT = re.compile(r"[\s，。！？!?、；;,.:：'\"“”‘’()（）\[\]【】~～]+")


def normalize(text):
    """去掉空白与中英文标点(与唤醒词归一逻辑一致)。"""
    return _PUNCT.sub("", str(text or "")).strip()


def decide_intent(text, cfg=None):
    """返回 dict: {intent, matched, text_norm}。

    intent ∈ {sleep, decline, no_reply, empathy, question, unknown}
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    t = normalize(text)
    if not t:
        return {"intent": "unknown", "matched": "", "text_norm": t}
    n = len(t)
    gate = int(cfg.get("gate_chars", 12))
    disabled = set(cfg.get("disabled") or [])

    def _phrase(words):
        for w in words:
            if w and w in t:
                return w
        return None

    def _suffix(words):
        for w in words:
            if w and t.endswith(w):
                return w
        return None

    if "sleep" not in disabled:
        m = _phrase(cfg.get("sleep_phrases", []))
        if m and n <= gate:
            return {"intent": "sleep", "matched": m, "text_norm": t}
        # 独立词: 文本以该词结尾且总长 ≤ 词长+3(允许"好的，晚安了"式短包装;
        # 防"晚安是什么意思"这类含"晚安"的信息问句)
        for w in cfg.get("sleep_standalone", []):
            if w and t.endswith(w) and n <= len(w) + 3:
                return {"intent": "sleep", "matched": w, "text_norm": t}

    if "decline" not in disabled:
        m = _phrase(cfg.get("decline_phrases", []))
        if m and n <= gate:
            return {"intent": "decline", "matched": m, "text_norm": t}
        m = _suffix(cfg.get("decline_suffix", []))
        if m and n <= int(cfg.get("suffix_gate_chars", 12)):
            return {"intent": "decline", "matched": m, "text_norm": t}

    if "no_reply" not in disabled:
        if n <= int(cfg.get("no_reply_gate_chars", 4)):
            m = _phrase(cfg.get("no_reply_words", []))
            if m:
                return {"intent": "no_reply", "matched": m, "text_norm": t}

    return {"intent": "question", "matched": "", "text_norm": t}
