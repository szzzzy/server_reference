import csv
import json
from pathlib import Path


def normalize_text(text):
    chars = []
    for ch in str(text):
        if "\u4e00" <= ch <= "\u9fff" or ch.isalnum():
            chars.append(ch.lower())
    return "".join(chars)


def cer(reference, recognized):
    reference = normalize_text(reference)
    recognized = normalize_text(recognized)
    if not reference:
        return None
    previous = list(range(len(recognized) + 1))
    for i, ref_char in enumerate(reference, start=1):
        current = [i]
        for j, hyp_char in enumerate(recognized, start=1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (ref_char != hyp_char),
            ))
        previous = current
    return previous[-1] / len(reference)


def to_float(value):
    try:
        if value == "":
            return None
        return float(value)
    except Exception:
        return None


def read_rows(path):
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_rows(path, rows):
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def load_reference(row):
    audio_path = Path(row.get("audio", ""))
    reference_path = audio_path.with_suffix(".txt")
    if reference_path.exists():
        return reference_path.read_text(encoding="utf-8-sig").strip(), str(reference_path)
    expected = row.get("expected", "").strip()
    return expected, "embedded expected" if expected else ""


def main():
    rows = read_rows("results/paraformer_realtime_stream_results.csv")
    ok_rows = [r for r in rows if r.get("status") == "OK"]
    scored_rows = []
    for row in rows:
        reference, reference_source = load_reference(row)
        scored_cer = cer(reference, row.get("recognized_text", "")) if row.get("status") == "OK" else None
        scored_rows.append({
            **row,
            "reference_source": reference_source,
            "reference_chars": len(normalize_text(reference)),
            "recognized_chars": len(normalize_text(row.get("recognized_text", ""))),
            "scored_cer": round(scored_cer, 4) if scored_cer is not None else "",
            "character_accuracy": round(max(0.0, 1.0 - scored_cer), 4) if scored_cer is not None else "",
        })
    write_rows("results/paraformer_realtime_stream_results_scored.csv", scored_rows)

    summary = []
    grouped = {}
    for row in ok_rows:
        key = (
            row.get("packet_ms", ""),
            row.get("asr_window_ms", ""),
            row.get("endpoint_silence_ms", ""),
        )
        grouped.setdefault(key, []).append(row)

    for (packet_ms, asr_window_ms, endpoint_silence_ms), group in grouped.items():
        cers = []
        for row in group:
            reference, _ = load_reference(row)
            row_cer = cer(reference, row.get("recognized_text", ""))
            if row_cer is not None:
                cers.append(row_cer)
        cers = [v for v in cers if v is not None]
        firsts = [to_float(r.get("first_partial_wall_seconds", "")) for r in group]
        firsts = [v for v in firsts if v is not None]
        finals = [to_float(r.get("final_wall_seconds", "")) for r in group]
        finals = [v for v in finals if v is not None]
        rtfs = [to_float(r.get("rtf", "")) for r in group]
        rtfs = [v for v in rtfs if v is not None]
        peaks = [to_float(r.get("peak_gpu_used_mb_observed", "")) for r in group]
        peaks = [v for v in peaks if v is not None]
        summary.append({
            "packet_ms": packet_ms,
            "asr_window_ms": asr_window_ms,
            "endpoint_silence_ms": endpoint_silence_ms,
            "runs": len(group),
            "avg_cer": round(sum(cers) / len(cers), 4) if cers else "",
            "avg_character_accuracy": round(max(0.0, 1.0 - sum(cers) / len(cers)), 4) if cers else "",
            "avg_first_partial_seconds": round(sum(firsts) / len(firsts), 3) if firsts else "",
            "avg_final_wall_seconds": round(sum(finals) / len(finals), 3) if finals else "",
            "avg_rtf": round(sum(rtfs) / len(rtfs), 4) if rtfs else "",
            "max_peak_gpu_used_mb_observed": int(max(peaks)) if peaks else "",
        })

    summary.sort(key=lambda r: (
        999 if r["avg_cer"] == "" else float(r["avg_cer"]),
        999 if r["avg_first_partial_seconds"] == "" else float(r["avg_first_partial_seconds"]),
    ))
    write_rows("results/paraformer_realtime_stream_summary.csv", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Saved: results/paraformer_realtime_stream_results_scored.csv")
    print("Saved: results/paraformer_realtime_stream_summary.csv")


if __name__ == "__main__":
    main()
