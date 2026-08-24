import csv
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def run_cmd(cmd):
    try:
        return subprocess.check_output(cmd, text=True, encoding="utf-8", errors="replace").strip()
    except Exception as exc:
        return f"ERROR: {exc}"


def main():
    Path("results").mkdir(exist_ok=True)
    report = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "venv_prefix": sys.prefix,
        "nvidia_smi_found": bool(shutil.which("nvidia-smi")),
        "nvidia_smi": run_cmd([
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]) if shutil.which("nvidia-smi") else "not found",
    }
    for pkg in ["numpy", "soundfile", "librosa", "requests", "jiwer", "pandas"]:
        try:
            mod = __import__(pkg)
            report[f"{pkg}_version"] = getattr(mod, "__version__", "installed")
        except Exception as exc:
            report[f"{pkg}_version"] = f"ERROR: {exc}"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    Path("results/env_check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with Path("results/env_check.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["key", "value"])
        for key, value in report.items():
            writer.writerow([key, value])


if __name__ == "__main__":
    main()
