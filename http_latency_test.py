import argparse
import csv
import json
import time
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser(description="Test HTTP model API latency, TTFT-like latency, and RTF.")
    parser.add_argument("--url", required=True, help="HTTP API URL")
    parser.add_argument("--audio", help="Audio file path. Optional.")
    parser.add_argument("--text", default="请介绍这个文物的年代用途和历史背景。", help="Text prompt")
    parser.add_argument("--audio-seconds", type=float, default=0.0, help="Audio duration in seconds for RTF calculation")
    parser.add_argument("--model-name", default="unknown_model")
    parser.add_argument("--output", default="performance_result.csv")
    parser.add_argument("--field", default="text", help="Text field name for JSON request")
    args = parser.parse_args()

    files = None
    data = None
    json_body = None

    if args.audio:
        audio_path = Path(args.audio)
        files = {"audio": (audio_path.name, audio_path.open("rb"), "application/octet-stream")}
        data = {args.field: args.text}
    else:
        json_body = {args.field: args.text}

    start = time.perf_counter()
    response = requests.post(args.url, files=files, data=data, json=json_body, timeout=300)
    first_response = time.perf_counter()
    response.raise_for_status()
    end = time.perf_counter()

    total_latency_ms = (end - start) * 1000
    ttft_ms = (first_response - start) * 1000
    rtf = (end - start) / args.audio_seconds if args.audio_seconds > 0 else ""

    result = {
        "model_name": args.model_name,
        "api_type": "HTTP",
        "test_audio_sec": args.audio_seconds,
        "ttft_ms": round(ttft_ms, 2),
        "total_latency_ms": round(total_latency_ms, 2),
        "rtf": round(rtf, 4) if rtf != "" else "",
        "status_code": response.status_code,
        "response_preview": response.text[:200].replace("\n", " "),
    }

    output_path = Path(args.output)
    write_header = not output_path.exists()
    with output_path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(result.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(result)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
