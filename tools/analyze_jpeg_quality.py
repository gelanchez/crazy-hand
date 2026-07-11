#!/usr/bin/env python3
"""
Analyze JPEG quality tradeoff across sessions recorded at different quality settings.

For each session directory, computes average frame size from saved images
and queries InfluxDB for gesture confidence and hand detection rate.

Usage:
    python tools/analyze_jpeg_quality.py \
        --sessions quality10:data/session_q10/images \
                   quality25:data/session_q25/images \
                   quality50:data/session_q50/images \
                   quality75:data/session_q75/images \
        --db-url http://localhost:8181 \
        --db-name crazyflie \
        --output results/jpeg_quality_results.csv

Each session must have been recorded separately after rebuilding and reflashing the AI-Deck
firmware with the desired APP_CFLAGS=-DJPEG_Q_<N> value; quality is compile-time only.
The session label must start with "quality<N>".
"""

import argparse
import os
import statistics
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import requests


FPS = 7.2


def query_influxdb(db_url: str, db_name: str, query: str) -> pd.DataFrame:
    resp = requests.post(
        f"{db_url}/api/v3/query_sql",
        params={"db": db_name},
        json={"q": query},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return pd.DataFrame(data) if data else pd.DataFrame()


def image_stats(images_dir: str) -> dict:
    path = Path(images_dir)
    files = list(path.glob("*.jpg")) + list(path.glob("*.jpeg"))
    if not files:
        return {"n_frames": 0, "mean_size_kb": 0.0, "bandwidth_kbs": 0.0}
    sizes = [f.stat().st_size / 1024 for f in files]
    mean_size = statistics.mean(sizes)
    return {
        "n_frames": len(files),
        "mean_size_kb": round(mean_size, 2),
        "bandwidth_kbs": round(mean_size * FPS, 2),
    }


def perception_stats(db_url: str, db_name: str, start: str, end: str) -> dict:
    query = f"""
        SELECT
            AVG(gesture_confidence) AS mean_confidence,
            COUNT(*) AS total_frames,
            SUM(CASE WHEN hand_detected = true THEN 1 ELSE 0 END) AS detected_frames
        FROM perception
        WHERE time >= '{start}' AND time <= '{end}'
    """
    df = query_influxdb(db_url, db_name, query)
    if df.empty or df["total_frames"].iloc[0] == 0:
        return {"mean_confidence": None, "detection_rate": None}
    total = float(df["total_frames"].iloc[0])
    detected = float(df["detected_frames"].iloc[0])
    return {
        "mean_confidence": round(float(df["mean_confidence"].iloc[0]), 3),
        "detection_rate": round(detected / total, 3) if total > 0 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sessions", nargs="+", required=True,
        metavar="LABEL:PATH",
        help="label:images_dir pairs, e.g. quality50:data/session_q50/images",
    )
    parser.add_argument("--db-url", default="http://localhost:8181")
    parser.add_argument("--db-name", default="crazyflie")
    parser.add_argument(
        "--session-times", nargs="+",
        metavar="LABEL:START:END",
        help="ISO8601 UTC time ranges for InfluxDB queries, e.g. quality50:2026-06-03T10:00:00Z:2026-06-03T10:02:00Z",
        default=[],
    )
    parser.add_argument("--output", default="results/jpeg_quality_results.csv")
    args = parser.parse_args()

    time_map = {}
    for item in args.session_times:
        # Format: LABEL:START:END where START/END are ISO8601 (contain colons).
        # Split only on first ":" to get label, then slice: timestamps are 20 chars each
        # e.g. "quality50:2026-06-03T10:00:00Z:2026-06-03T10:02:00Z"
        label, rest = item.split(":", 1)
        start = rest[:20]   # "YYYY-MM-DDTHH:MM:SSZ"
        end = rest[21:]     # skip the colon separator
        time_map[label] = (start, end)

    rows = []
    for session in args.sessions:
        label, images_dir = session.split(":", 1)
        quality = int(label.replace("quality", ""))

        print(f"\nAnalyzing {label} ({images_dir})...")

        img_stats = image_stats(images_dir)
        print(f"  Frames: {img_stats['n_frames']}, mean size: {img_stats['mean_size_kb']:.1f} KB")

        perc_stats = {"mean_confidence": None, "detection_rate": None}
        if label in time_map:
            start, end = time_map[label]
            perc_stats = perception_stats(args.db_url, args.db_name, start, end)
            print(f"  Confidence: {perc_stats['mean_confidence']}, Detection: {perc_stats['detection_rate']}")
        else:
            print(f"  No session time provided for {label} — skipping InfluxDB query")

        rows.append({
            "quality": quality,
            "label": label,
            **img_stats,
            **perc_stats,
        })

    df = pd.DataFrame(rows).sort_values("quality")
    print("\n\nResults:")
    print(df.to_string(index=False))

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"\nSaved to {args.output}")

    # Plot
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))
    fig.suptitle("JPEG Quality Tradeoff")

    axes[0].plot(df["quality"], df["mean_size_kb"], "o-", color="#2196f3")
    axes[0].set_xlabel("JPEG Quality")
    axes[0].set_ylabel("Mean frame size (KB)")
    axes[0].set_title("Compression")
    axes[0].grid(True, alpha=0.3)

    if df["mean_confidence"].notna().any():
        axes[1].plot(df["quality"], df["mean_confidence"], "o-", color="#4caf50")
        axes[1].set_xlabel("JPEG Quality")
        axes[1].set_ylabel("Mean gesture confidence")
        axes[1].set_ylim(0, 1.05)
        axes[1].set_title("Recognition confidence")
        axes[1].grid(True, alpha=0.3)

    if df["detection_rate"].notna().any():
        axes[2].plot(df["quality"], df["detection_rate"] * 100, "o-", color="#ff9800")
        axes[2].set_xlabel("JPEG Quality")
        axes[2].set_ylabel("Hand detection rate (%)")
        axes[2].set_ylim(0, 105)
        axes[2].set_title("Detection rate")
        axes[2].grid(True, alpha=0.3)

    plt.tight_layout()
    plot_path = str(Path(args.output).parent / "jpeg_quality.png")
    plt.savefig(plot_path, dpi=150)
    print(f"Plot saved: {plot_path}")


if __name__ == "__main__":
    main()
