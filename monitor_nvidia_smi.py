import argparse
import csv
import subprocess
import time
from datetime import datetime
from pathlib import Path


def query_gpu():
    cmd = [
        "nvidia-smi",
        "--query-gpu=timestamp,name,memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    output = subprocess.check_output(cmd, text=True, encoding="utf-8", errors="replace")
    rows = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            rows.append({
                "host_time": datetime.now().isoformat(timespec="seconds"),
                "nvidia_timestamp": parts[0],
                "gpu_name": parts[1],
                "memory_used_mb": parts[2],
                "memory_total_mb": parts[3],
                "gpu_util_percent": parts[4],
            })
    return rows


def main():
    parser = argparse.ArgumentParser(description="Monitor NVIDIA GPU memory with nvidia-smi.")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--output", default="gpu_memory_log.csv")
    args = parser.parse_args()

    output_path = Path(args.output)
    end_time = time.time() + args.duration
    write_header = not output_path.exists()

    with output_path.open("a", encoding="utf-8-sig", newline="") as f:
        fieldnames = ["host_time", "nvidia_timestamp", "gpu_name", "memory_used_mb", "memory_total_mb", "gpu_util_percent"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        while time.time() < end_time:
            try:
                for row in query_gpu():
                    writer.writerow(row)
                    print(row)
                f.flush()
            except Exception as exc:
                print(f"nvidia-smi failed: {exc}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
