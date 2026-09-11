#!/usr/bin/env python3
"""
Analyze JPEG quality tradeoff across sessions recorded at different quality settings.

For each session directory, computes average frame size and bandwidth from saved
images. This is the metric ch4's JPEG subsection actually needs validated (default
quality, compression ratio, bandwidth, FPS drop) — no hand/gesture needs to be in
frame and no InfluxDB data is required for this.

Usage:
    python tools/analyze_jpeg_quality.py \
        --sessions quality10:data/session_q10/images \
                   quality20:data/session_q20/images \
                   quality50:data/session_q50/images \
                   quality90:data/session_q90/images \
        --output results/jpeg_quality_results.csv

Each session must have been recorded separately after rebuilding and reflashing the AI-Deck
firmware with the desired APP_CFLAGS=-DJPEG_Q_<N> value (valid: 0/10/20/50/90/95/100 only);
quality is compile-time only. The session label must start with "quality<N>".

Optional: --session-times or --session-files (JSON from 'record_session.py --label
quality<N>') additionally query InfluxDB for mean gesture confidence and hand detection
rate per quality level, if you want that extra (not required) comparison — that requires
process images on and a hand actually in frame during capture.
"""

import argparse
import json
import os
import statistics
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import requests
from dotenv import load_dotenv

load_dotenv()  # loads INFLUXDB3_TOKEN from .env, same as client/main.py


def query_influxdb(db_url: str, db_name: str, query: str) -> pd.DataFrame:
    token = os.environ.get("INFLUXDB3_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.post(
        f"{db_url}/api/v3/query_sql",
        json={"db": db_name, "q": query},
        headers=headers,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return pd.DataFrame(data) if data else pd.DataFrame()


CORRUPT_SIZE_RATIO = 0.5  # frame flagged as corrupt if smaller than this fraction of the session median


def image_stats(images_dir: str) -> dict:
    path = Path(images_dir)
    files = list(path.glob("*.jpg")) + list(path.glob("*.jpeg"))
    if not files:
        return {"n_frames": 0, "n_corrupt": 0, "corrupt_rate": 0.0, "mean_size_kb": 0.0,
                 "measured_fps": 0.0, "bandwidth_kbs": 0.0}
    sizes = sorted(f.stat().st_size / 1024 for f in files)
    median_size = sizes[len(sizes) // 2]
    # A capture-timing failure (encoding overrun, VSYNC tear) truncates the frame partway
    # through, producing a JPEG far smaller than a normal frame of the same quality/content —
    # not a low-detail scene, a torn one. Flag and exclude these from the size/bandwidth mean,
    # which would otherwise be silently deflated by corrupted frames; report the rate separately
    # since a high corrupt rate is itself the more important finding for that quality level.
    clean = [s for s in sizes if s >= median_size * CORRUPT_SIZE_RATIO]
    n_corrupt = len(sizes) - len(clean)
    mean_size = statistics.mean(clean) if clean else 0.0
    # Filenames are the hardware capture timestamp in ms (see logger_node image archiving);
    # measured FPS = (n-1) frame intervals spanning (last-first) timestamp, not the fixed
    # FPS constant, which was only ever a pre-measurement estimate and varies by quality level.
    timestamps = sorted(int(f.stem) for f in files)
    span_s = (timestamps[-1] - timestamps[0]) / 1000.0
    measured_fps = (len(timestamps) - 1) / span_s if span_s > 0 else 0.0
    return {
        "n_frames": len(files),
        "n_corrupt": n_corrupt,
        "corrupt_rate": round(n_corrupt / len(files), 3),
        "mean_size_kb": round(mean_size, 2),
        "measured_fps": round(measured_fps, 2),
        "bandwidth_kbs": round(mean_size * measured_fps, 2),
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
        "--session-times", nargs="+", default=[],
        metavar="LABEL:START:END",
        help="ISO8601 UTC time ranges for InfluxDB queries, e.g. quality50:2026-06-03T10:00:00Z:2026-06-03T10:02:00Z",
    )
    parser.add_argument(
        "--session-files", nargs="+", default=[],
        metavar="PATH",
        help="JSON files from 'record_session.py --label quality<N>' (one per quality level) — "
        "alternative to --session-times, no hand-typed timestamps",
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
    for path in args.session_files:
        session = json.loads(Path(path).read_text())
        time_map[session["label"]] = (session["start"], session["end"])

    rows = []
    for session in args.sessions:
        label, images_dir = session.split(":", 1)
        quality = int(label.replace("quality", ""))

        print(f"\nAnalyzing {label} ({images_dir})...")

        img_stats = image_stats(images_dir)
        corrupt_note = f", {img_stats['n_corrupt']} corrupt ({img_stats['corrupt_rate']:.0%})" if img_stats['n_corrupt'] else ""
        print(f"  Frames: {img_stats['n_frames']}{corrupt_note}, mean size (clean frames): {img_stats['mean_size_kb']:.1f} KB")

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

    # Plot: frame size, bandwidth, and corrupt rate — the three metrics this reduced-scope
    # test actually measures (gesture confidence/detection dropped, see tool docstring).
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))
    fig.suptitle("JPEG Quality Tradeoff")

    axes[0].plot(df["quality"], df["mean_size_kb"], "o-", color="#2196f3")
    axes[0].set_xlabel("JPEG Quality")
    axes[0].set_ylabel("Mean frame size, clean frames (KB)")
    axes[0].set_title("Compression")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(df["quality"], df["bandwidth_kbs"], "o-", color="#4caf50")
    axes[1].set_xlabel("JPEG Quality")
    axes[1].set_ylabel("Bandwidth (KB/s)")
    axes[1].set_title("Bandwidth")
    axes[1].grid(True, alpha=0.3)

    # Numeric x-axis with matching width, not categorical — keeps quality spacing
    # consistent with the other two panels (e.g. 50->90 visibly farther apart than 10->20).
    bar_width = (df["quality"].max() - df["quality"].min()) * 0.06
    axes[2].bar(df["quality"], df["corrupt_rate"] * 100, width=bar_width,
                color=["#f44336" if r > 0 else "#4caf50" for r in df["corrupt_rate"]])
    axes[2].set_xlabel("JPEG Quality")
    axes[2].set_ylabel("Corrupt frame rate (%)")
    axes[2].set_ylim(0, 105)
    axes[2].set_xlim(axes[0].get_xlim())
    axes[2].set_title("Capture reliability")
    axes[2].grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plot_path = str(Path(args.output).parent / "jpeg_quality.png")
    plt.savefig(plot_path, dpi=150)
    print(f"Plot saved: {plot_path}")


if __name__ == "__main__":
    main()
