import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from asr_eval_core import audio_duration, cer, gpu_snapshot, load_paraformer, normalize_text, recognize_streaming


def read_csv(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_rows(path, rows):
    if not rows:
        return
    out = Path(path)
    out.parent.mkdir(exist_ok=True)
    write_header = not out.exists()
    with out.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Batch Paraformer ASR accuracy and command hit-rate test.")
    parser.add_argument("--cases", default="asr_cases.csv")
    parser.add_argument("--commands", default="command_cases.csv")
    parser.add_argument("--model", default="paraformer-zh-streaming")
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()

    before_gpu = gpu_snapshot()
    model, device, load_seconds = load_paraformer(args.model, args.device)
    after_load_gpu = gpu_snapshot()

    rows = []
    for case in read_csv(args.cases):
        audio = Path(case["audio"])
        recognized, first_partial, infer_seconds = recognize_streaming(model, audio)
        duration = audio_duration(audio)
        expected = case.get("expected", "")
        row = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "case_id": case.get("case_id", ""),
            "type": case.get("type", ""),
            "audio": str(audio),
            "audio_seconds": round(duration, 3),
            "load_seconds": round(load_seconds, 3),
            "first_partial_seconds": first_partial,
            "infer_seconds": round(infer_seconds, 3),
            "rtf": round(infer_seconds / duration, 4) if duration else "",
            "expected": expected,
            "recognized_text": recognized,
            "cer": cer(expected, recognized) if expected else "",
            "before_gpu": before_gpu,
            "after_load_gpu": after_load_gpu,
            "after_infer_gpu": gpu_snapshot(),
            "status": "OK",
            "error": "",
        }
        rows.append(row)

    cmd_rows = []
    for case in read_csv(args.commands):
        audio = Path(case["audio"])
        recognized, first_partial, infer_seconds = recognize_streaming(model, audio)
        keyword = case.get("expected_keyword", "")
        hit = normalize_text(keyword) in normalize_text(recognized)
        duration = audio_duration(audio)
        cmd_rows.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "case_id": case.get("case_id", ""),
            "command": case.get("command", ""),
            "audio": str(audio),
            "audio_seconds": round(duration, 3),
            "expected_keyword": keyword,
            "recognized_text": recognized,
            "hit": int(hit),
            "first_partial_seconds": first_partial,
            "infer_seconds": round(infer_seconds, 3),
            "rtf": round(infer_seconds / duration, 4) if duration else "",
            "after_infer_gpu": gpu_snapshot(),
        })

    write_rows("results/paraformer_asr_accuracy_cases.csv", rows)
    write_rows("results/paraformer_command_accuracy_cases.csv", cmd_rows)

    valid_cers = [float(r["cer"]) for r in rows if r["cer"] != ""]
    command_total = len(cmd_rows)
    command_hits = sum(int(r["hit"]) for r in cmd_rows)
    summary = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "model": args.model,
        "device": device,
        "case_count": len(rows),
        "avg_cer": round(sum(valid_cers) / len(valid_cers), 4) if valid_cers else "",
        "command_total": command_total,
        "command_hits": command_hits,
        "command_accuracy": round(command_hits / command_total, 4) if command_total else "",
        "avg_rtf": round(sum(float(r["rtf"]) for r in rows if r["rtf"] != "") / len(rows), 4) if rows else "",
        "after_gpu": gpu_snapshot(),
    }
    write_rows("results/paraformer_asr_summary.csv", [summary])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Saved results/paraformer_asr_accuracy_cases.csv")
    print("Saved results/paraformer_command_accuracy_cases.csv")
    print("Saved results/paraformer_asr_summary.csv")


if __name__ == "__main__":
    main()
