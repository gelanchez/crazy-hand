#!/usr/bin/env python3
"""
Analyze gesture recognition accuracy from labeled frames produced by
tools/label_gesture_frames.py (one CSV per condition, or a single merged CSV).

Usage:
    python tools/analyze_gesture_accuracy.py \
        --labels results/labels_good_1.0m.csv results/labels_good_1.5m.csv \
                 results/labels_poor_1.0m.csv results/labels_poor_1.5m.csv \
        --output results/gesture_accuracy_results.csv

Requires: pandas, matplotlib.
"""

import argparse
import os
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

GESTURES = ["Thumb_Up", "Thumb_Down", "Victory"]


def load_labels(paths: list[str]) -> pd.DataFrame:
    # keep_default_na=False: MediaPipe emits a real gesture class literally named "None"
    # (distinct from our own "NONE" fallback for no-hand-detected) — pandas' default NA
    # coercion would otherwise silently turn that string into a missing value.
    frames = [pd.read_csv(p, keep_default_na=False) for p in paths]
    df = pd.concat(frames, ignore_index=True)
    df["match"] = df["match"].astype(bool)
    df["system_hand_detected"] = df["system_hand_detected"].astype(bool)
    return df


def print_confusion_matrix(df: pd.DataFrame) -> None:
    classes = GESTURES + ["NONE"]
    columns = classes + ["OTHER"]  # anything the system emitted outside our 3 gestures/NONE
    print("\nConfusion matrix (rows=human label, cols=system detected; OTHER = e.g. Open_Palm, MediaPipe's own 'None' class):")
    header = f"{'':15s}" + "".join(f"{c:15s}" for c in columns)
    print(header)
    for human in classes:
        row_df = df[df["human_gesture"] == human]
        row = f"{human:15s}"
        for system in classes:
            count = len(row_df[row_df["system_gesture"] == system])
            row += f"{count:15d}"
        other = len(row_df[~row_df["system_gesture"].isin(classes)])
        row += f"{other:15d}"
        print(row)


def print_precision_recall(df: pd.DataFrame) -> None:
    print("\nPrecision / Recall per gesture:")
    print(f"{'Gesture':15s} {'Precision':>10s} {'Recall':>10s} {'F1':>10s} {'Support':>10s}")
    for gesture in GESTURES:
        tp = len(df[(df["human_gesture"] == gesture) & (df["system_gesture"] == gesture)])
        fp = len(df[(df["human_gesture"] != gesture) & (df["system_gesture"] == gesture)])
        fn = len(df[(df["human_gesture"] == gesture) & (df["system_gesture"] != gesture)])
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        support = len(df[df["human_gesture"] == gesture])
        print(f"{gesture:15s} {precision:10.3f} {recall:10.3f} {f1:10.3f} {support:10d}")


def print_condition_breakdown(df: pd.DataFrame) -> None:
    print("\nAccuracy by condition:")
    for condition in sorted(df["condition"].unique()):
        subset = df[df["condition"] == condition]
        acc = subset["match"].mean()
        det = subset["system_hand_detected"].mean()
        conf = subset[subset["system_hand_detected"]]["system_confidence"].mean()
        print(
            f"  {condition:15s}: accuracy={acc:.2%}  detection={det:.2%}  "
            f"mean_confidence={conf:.2f}  n={len(subset)}"
        )


def plot_results(df: pd.DataFrame, output_dir: str) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle("Gesture Recognition Accuracy")

    for ax, gesture in zip(axes, GESTURES):
        subset = df[df["human_gesture"] == gesture]
        by_condition = subset.groupby("condition")["match"].agg(["mean", "count"]).reset_index()
        labels = by_condition["condition"].tolist()
        values = by_condition["mean"].values
        counts = by_condition["count"].values
        ax.bar(labels, values, color=["#4caf50" if v >= 0.8 else "#f44336" for v in values])
        # Label sample size on every bar so a 0% (or 100%) bar can't be misread as "no data"
        # — it's a real accuracy over that many labeled trials, however few.
        for i, (v, n) in enumerate(zip(values, counts)):
            ax.annotate(f"n={n}", (i, v + 0.03), ha="center", fontsize=9)
        ax.set_title(gesture)
        ax.set_ylabel("Accuracy")
        ax.set_ylim(0, 1.15)
        ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1))
        ax.tick_params(axis="x", rotation=30)

    plt.tight_layout()
    path = os.path.join(output_dir, "gesture_accuracy.png")
    plt.savefig(path, dpi=150)
    print(f"\nPlot saved: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", nargs="+", required=True,
                         help="One or more labeled-frame CSVs from label_gesture_frames.py")
    parser.add_argument("--output", default="results/gesture_accuracy_results.csv")
    args = parser.parse_args()

    print(f"Loading {len(args.labels)} labeled-frame file(s)...")
    df = load_labels(args.labels)
    print(f"  {len(df)} labeled frames loaded across {df['condition'].nunique()} condition(s).")

    print_confusion_matrix(df)
    print_precision_recall(df)
    print_condition_breakdown(df)

    output_dir = str(Path(args.output).parent)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"\nResults saved to {args.output}")

    plot_results(df, output_dir)


if __name__ == "__main__":
    main()
