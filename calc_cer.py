import argparse
import csv
from pathlib import Path


def edit_distance(a, b):
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
    return dp[-1][-1]


def normalize_text(s):
    drops = set(" ，。！？、,.!?;；:：\t\r\n\"'“”‘’")
    return "".join(ch for ch in s.strip() if ch not in drops)


def main():
    parser = argparse.ArgumentParser(description="Calculate Chinese CER from CSV.")
    parser.add_argument("--input", required=True, help="CSV with reference_text and recognized_text columns")
    parser.add_argument("--output", default="asr_cer_result.csv")
    args = parser.parse_args()

    rows = []
    with Path(args.input).open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ref = normalize_text(row.get("reference_text", ""))
            hyp = normalize_text(row.get("recognized_text", ""))
            errors = edit_distance(ref, hyp)
            total = len(ref)
            cer = errors / total if total else 0
            row["total_chars"] = total
            row["error_chars"] = errors
            row["cer"] = round(cer, 4)
            rows.append(row)

    if not rows:
        print("No rows found.")
        return

    with Path(args.output).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    avg_cer = sum(float(r["cer"]) for r in rows) / len(rows)
    print(f"Saved: {args.output}")
    print(f"Average CER: {avg_cer:.4f}")


if __name__ == "__main__":
    main()
