#!/usr/bin/env python3
"""
Analyze gesture recognition accuracy from a recorded session.

Usage:
    python tools/analyze_gesture_accuracy.py \
        --ground-truth results/gesture_accuracy.csv \
        --db-url http://localhost:8181 \
        --db-name crazyflie \
        --output results/gesture_accuracy_results.csv

Requires: pandas, matplotlib, influxdb_client (or requests for raw HTTP).
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import requests


GESTURES = ["Thumb_Up", "Thumb_Down", "Victory"]
WINDOW_MS = 300  # match window around each trial


def query_influxdb(db_url: str, db_name: str, query: str) -> pd.DataFrame:
    resp = requests.post(
        f"{db_url}/api/v3/query_sql",
        params={"db": db_name},
        json={"q": query},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


def load_ground_truth(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["start_timestamp_ms"] = pd.to_numeric(df["start_timestamp_ms"])
    df["end_timestamp_ms"] = pd.to_numeric(df["end_timestamp_ms"])
    return df


def fetch_perception(db_url: str, db_name: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    start_ns = start_ms * 1_000_000
    end_ns = end_ms * 1_000_000
    query = f"""
        SELECT time, gesture_name, gesture_confidence, hand_detected
        FROM perception
        WHERE time >= {start_ns} AND time <= {end_ns}
        ORDER BY time
    """
    return query_influxdb(db_url, db_name, query)


def most_common_gesture(df: pd.DataFrame) -> str:
    if df.empty or not df["hand_detected"].any():
        return "NONE"
    detected = df[df["hand_detected"] == True]
    if detected.empty:
        return "NONE"
    return detected["gesture_name"].mode().iloc[0]


def analyze(ground_truth: pd.DataFrame, db_url: str, db_name: str) -> pd.DataFrame:
    rows = []
    total = len(ground_truth)
    for i, trial in ground_truth.iterrows():
        if (i + 1) % 20 == 0:
            print(f"  Processing trial {i+1}/{total}...", flush=True)
        perception = fetch_perception(
            db_url, db_name,
            int(trial["start_timestamp_ms"]),
            int(trial["end_timestamp_ms"]),
        )
        detected = most_common_gesture(perception)
        rows.append({
            "lighting": trial["lighting"],
            "background": trial["background"],
            "distance": trial["distance"],
            "expected": trial["gesture"],
            "detected": detected,
            "correct": detected == trial["gesture"],
            "hand_detected_any": not perception.empty and perception["hand_detected"].any(),
            "mean_confidence": (
                perception[perception["hand_detected"] == True]["gesture_confidence"].mean()
                if not perception.empty else 0.0
            ),
        })
    return pd.DataFrame(rows)


def print_confusion_matrix(results: pd.DataFrame) -> None:
    classes = GESTURES + ["NONE"]
    print("\nConfusion matrix (rows=expected, cols=detected):")
    header = f"{'':15s}" + "".join(f"{c:15s}" for c in classes)
    print(header)
    for exp in classes:
        row_df = results[results["expected"] == exp]
        row = f"{exp:15s}"
        for det in classes:
            count = len(row_df[row_df["detected"] == det])
            row += f"{count:15d}"
        print(row)


def print_precision_recall(results: pd.DataFrame) -> None:
    print("\nPrecision / Recall per gesture:")
    print(f"{'Gesture':15s} {'Precision':>10s} {'Recall':>10s} {'F1':>10s} {'Support':>10s}")
    for gesture in GESTURES:
        tp = len(results[(results["expected"] == gesture) & (results["detected"] == gesture)])
        fp = len(results[(results["expected"] != gesture) & (results["detected"] == gesture)])
        fn = len(results[(results["expected"] == gesture) & (results["detected"] != gesture)])
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        support = len(results[results["expected"] == gesture])
        print(f"{gesture:15s} {precision:10.3f} {recall:10.3f} {f1:10.3f} {support:10d}")


def print_condition_breakdown(results: pd.DataFrame) -> None:
    print("\nAccuracy by condition:")
    for lighting in results["lighting"].unique():
        for background in results["background"].unique():
            for distance in results["distance"].unique():
                subset = results[
                    (results["lighting"] == lighting) &
                    (results["background"] == background) &
                    (results["distance"] == distance)
                ]
                acc = subset["correct"].mean()
                det = subset["hand_detected_any"].mean()
                print(
                    f"  {lighting:5s} / {background:9s} / {distance:5s}: "
                    f"accuracy={acc:.2%}  detection={det:.2%}  n={len(subset)}"
                )


def plot_results(results: pd.DataFrame, output_dir: str) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle("Gesture Recognition Accuracy")

    for ax, gesture in zip(axes, GESTURES):
        subset = results[results["expected"] == gesture]
        conditions = subset.groupby(["lighting", "background"])["correct"].mean().reset_index()
        labels = [f"{r.lighting}/{r.background}" for _, r in conditions.iterrows()]
        values = conditions["correct"].values
        ax.bar(labels, values, color=["#4caf50" if v >= 0.8 else "#f44336" for v in values])
        ax.set_title(gesture)
        ax.set_ylabel("Accuracy")
        ax.set_ylim(0, 1.05)
        ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1))
        ax.tick_params(axis="x", rotation=30)

    plt.tight_layout()
    path = os.path.join(output_dir, "gesture_accuracy.png")
    plt.savefig(path, dpi=150)
    print(f"\nPlot saved: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ground-truth", default="results/gesture_accuracy.csv")
    parser.add_argument("--db-url", default="http://localhost:8181")
    parser.add_argument("--db-name", default="crazyflie")
    parser.add_argument("--output", default="results/gesture_accuracy_results.csv")
    args = parser.parse_args()

    print(f"Loading ground truth from {args.ground_truth}...")
    gt = load_ground_truth(args.ground_truth)
    print(f"  {len(gt)} trials loaded.")

    print("Querying InfluxDB for perception data...")
    results = analyze(gt, args.db_url, args.db_name)

    print_confusion_matrix(results)
    print_precision_recall(results)
    print_condition_breakdown(results)

    output_dir = str(Path(args.output).parent)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    results.to_csv(args.output, index=False)
    print(f"\nResults saved to {args.output}")

    plot_results(results, output_dir)


if __name__ == "__main__":
    main()
