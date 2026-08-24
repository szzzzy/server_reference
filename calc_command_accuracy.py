import argparse
import csv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Calculate command recognition accuracy.")
    parser.add_argument("--input", required=True, help="CSV with total_trials and correct_trials columns")
    parser.add_argument("--output", default="command_accuracy_result.csv")
    args = parser.parse_args()

    rows = []
    total_trials = 0
    total_correct = 0

    with Path(args.input).open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total = int(float(row.get("total_trials") or 0))
            correct = int(float(row.get("correct_trials") or 0))
            acc = correct / total if total else 0
            row["accuracy"] = round(acc, 4)
            total_trials += total
            total_correct += correct
            rows.append(row)

    if not rows:
        print("No rows found.")
        return

    with Path(args.output).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    overall = total_correct / total_trials if total_trials else 0
    print(f"Saved: {args.output}")
    print(f"Overall accuracy: {overall:.4f}")


if __name__ == "__main__":
    main()
