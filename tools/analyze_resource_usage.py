#!/usr/bin/env python3
"""
Summarize per-node CPU/memory usage recorded by tools/record_resource_usage.py.

Usage:
    python tools/analyze_resource_usage.py \
        --csv results/resource_usage.csv \
        --output results/resource_usage_results.csv
"""

import argparse
from pathlib import Path

import pandas as pd

# Expected CPU% ranges from ch5_evaluation.md §5.5.3 — flagged, not enforced.
EXPECTED_CPU = {
    "vision_node": (15, 30),
    "wifi_node": (5, 10),
    "gui_node": (5, 15),
    "logger_node": (2, 5),
    "control_node": (0, 1),
}


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    grouped = df.groupby("node").agg(
        n_samples=("cpu_percent", "size"),
        cpu_mean=("cpu_percent", "mean"),
        cpu_max=("cpu_percent", "max"),
        rss_mean_mb=("rss_mb", "mean"),
        rss_max_mb=("rss_mb", "max"),
    ).reset_index()
    return grouped


def print_summary(summary: pd.DataFrame) -> None:
    print(f"\n{'Node':15s} {'Samples':>8s} {'CPU mean':>9s} {'CPU max':>8s} {'RSS mean':>10s} {'RSS max':>10s}  Expected")
    for _, row in summary.iterrows():
        node = row["node"]
        lo, hi = EXPECTED_CPU.get(node, (None, None))
        flag = ""
        if lo is not None and not (lo <= row["cpu_mean"] <= hi):
            flag = f"  ⚠ outside {lo}-{hi}%"
        elif lo is not None:
            flag = f"  ok ({lo}-{hi}%)"
        print(
            f"{node:15s} {row['n_samples']:8d} {row['cpu_mean']:8.1f}% {row['cpu_max']:7.1f}% "
            f"{row['rss_mean_mb']:9.1f}M {row['rss_max_mb']:9.1f}M{flag}"
        )

    missing = set(EXPECTED_CPU) - set(summary["node"])
    if missing:
        print(f"\nNo samples recorded for: {', '.join(sorted(missing))} — process not found while recording?")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="CSV from record_resource_usage.py")
    parser.add_argument("--output", default="results/resource_usage_results.csv")
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    if df.empty:
        print(f"No samples in {args.csv}")
        return

    summary = summarize(df)
    print_summary(summary)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(args.output, index=False)
    print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
