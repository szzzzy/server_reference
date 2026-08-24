import csv
from pathlib import Path


def average(rows, field):
    values = [float(row[field]) for row in rows if row.get(field, "") not in ("", None)]
    return round(sum(values) / len(values), 4) if values else ""


def main():
    root = Path("results_5090")
    root.mkdir(exist_ok=True)
    lines = ["RTX 5090 测试汇总", ""]

    llm_path = root / "llm_results.csv"
    if llm_path.exists():
        rows = list(csv.DictReader(llm_path.open(encoding="utf-8-sig")))
        models = sorted({row["model"] for row in rows})
        lines.append("文本大模型：")
        for model in models:
            group = [row for row in rows if row["model"] == model]
            lines.append(
                f"- {model}: {len(group)} 条；平均首 Token {average(group, 'first_token_seconds')} s；"
                f"平均总耗时 {average(group, 'total_seconds')} s；平均速度 {average(group, 'tokens_per_second')} token/s"
            )
        lines.append("")

    tts_path = root / "cosyvoice2_results.csv"
    if tts_path.exists():
        rows = list(csv.DictReader(tts_path.open(encoding="utf-8-sig")))
        clipped = sum(int(row.get("clipped_samples", 0)) for row in rows)
        lines.extend([
            "CosyVoice2 TTS：",
            f"- {len(rows)} 条；平均首段音频 {average(rows, 'first_audio_seconds')} s；平均 RTF {average(rows, 'rtf')}；削波样本 {clipped}",
            "",
        ])

    pipeline_path = root / "funasr_qwen3_pipeline.csv"
    if pipeline_path.exists():
        rows = list(csv.DictReader(pipeline_path.open(encoding="utf-8-sig")))
        lines.extend([
            "FunASR → Qwen3：",
            f"- {len(rows)} 次；平均 ASR {average(rows, 'asr_seconds')} s；平均 LLM {average(rows, 'llm_seconds')} s；平均推理总耗时 {average(rows, 'total_inference_seconds')} s",
            "",
        ])

    if len(lines) == 2:
        lines.append("尚无测试结果，请先从菜单执行模型测试。")
    out = root / "5090_测试汇总.txt"
    out.write_text("\n".join(lines), encoding="utf-8-sig")
    print(out.resolve())
    print("\n".join(lines))


if __name__ == "__main__":
    main()
