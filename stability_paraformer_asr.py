import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from asr_eval_core import audio_duration, gpu_snapshot, load_paraformer, recognize_streaming


def main():
    parser = argparse.ArgumentParser(description="Repeated Paraformer ASR stability test.")
    parser.add_argument("--audio", default="samples/standard_female_voice_16k_mono_16bit.wav")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--model", default="paraformer-zh-streaming")
    args = parser.parse_args()

    Path("results").mkdir(exist_ok=True)
    model, device, load_seconds = load_paraformer(args.model, "auto")
    rows = []
    for i in range(1, args.repeat + 1):
        recognized, first_partial, infer_seconds = recognize_streaming(model, Path(args.audio))
        duration = audio_duration(Path(args.audio))
        rows.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "run": i,
            "model": args.model,
            "device": device,
            "audio": args.audio,
            "audio_seconds": round(duration, 3),
            "load_seconds": round(load_seconds, 3) if i == 1 else 0,
            "first_partial_seconds": first_partial,
            "infer_seconds": round(infer_seconds, 3),
            "rtf": round(infer_seconds / duration, 4) if duration else "",
            "recognized_chars": len(recognized),
            "text_preview": recognized[:120],
            "gpu": gpu_snapshot(),
            "status": "OK",
        })

    out = Path("results/paraformer_stability_results.csv")
    write_header = not out.exists()
    with out.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    summary = {
        "repeat": args.repeat,
        "avg_rtf": round(sum(float(r["rtf"]) for r in rows) / len(rows), 4),
        "avg_first_partial_seconds": round(sum(float(r["first_partial_seconds"]) for r in rows if r["first_partial_seconds"] != "") / len(rows), 4),
        "last_gpu": rows[-1]["gpu"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
