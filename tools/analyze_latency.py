#!/usr/bin/env python3
"""
Analyze end-to-end pipeline latency from InfluxDB records.

Joins perception (with frame_id) and action tables on nearest timestamp
to compute per-hop latency distribution.

Usage:
    python tools/analyze_latency.py \
        --db-url http://localhost:8181 \
        --db-name crazyflie \
        --session-start "2026-06-03T10:00:00Z" \
        --session-end   "2026-06-03T10:05:00Z" \
        --output results/latency_results.csv
"""

import argparse
import json
import os
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


def fetch_perception(db_url: str, db_name: str, start: str, end: str) -> pd.DataFrame:
    query = f"""
        SELECT time, frame_id, hand_detected, gesture_name
        FROM perception
        WHERE time >= '{start}' AND time <= '{end}'
        ORDER BY time
    """
    df = query_influxdb(db_url, db_name, query)
    if df.empty:
        return df
    df["ts_ms"] = pd.to_datetime(df["time"]).astype("int64") / 1_000_000
    return df


def fetch_action(db_url: str, db_name: str, start: str, end: str) -> pd.DataFrame:
    query = f"""
        SELECT time, state, command, vx, vy
        FROM action
        WHERE time >= '{start}' AND time <= '{end}'
        ORDER BY time
    """
    df = query_influxdb(db_url, db_name, query)
    if df.empty:
        return df
    df["ts_ms"] = pd.to_datetime(df["time"]).astype("int64") / 1_000_000
    return df


def fetch_telemetry(db_url: str, db_name: str, start: str, end: str) -> pd.DataFrame:
    query = f"""
        SELECT time, fps
        FROM telemetry
        WHERE time >= '{start}' AND time <= '{end}'
        ORDER BY time
    """
    df = query_influxdb(db_url, db_name, query)
    if df.empty:
        return df
    df["ts_ms"] = pd.to_datetime(df["time"]).astype("int64") / 1_000_000
    return df


def merge_nearest(left: pd.DataFrame, right: pd.DataFrame,
                  left_key: str, right_key: str,
                  tolerance_ms: float = 200.0) -> pd.DataFrame:
    """Merge two time-indexed dataframes on nearest timestamp within tolerance."""
    # Rename keys before merge to avoid ambiguity when both frames share the same column name.
    left2 = left.sort_values(left_key).rename(columns={left_key: "_ts_left"})
    right2 = right.sort_values(right_key).rename(columns={right_key: "_ts_right"})
    merged = pd.merge_asof(
        left2,
        right2,
        left_on="_ts_left",
        right_on="_ts_right",
        direction="nearest",
        tolerance=tolerance_ms,
    )
    return merged


def compute_latencies(perception: pd.DataFrame, action: pd.DataFrame) -> pd.DataFrame:
    merged = merge_nearest(perception, action, "ts_ms", "ts_ms")
    merged = merged.dropna(subset=["_ts_right"])
    merged["perc_to_action_ms"] = merged["_ts_right"] - merged["_ts_left"]
    return merged


def print_stats(series: pd.Series, label: str) -> None:
    print(f"\n{label}:")
    print(f"  n      = {len(series)}")
    print(f"  mean   = {series.mean():.1f} ms")
    print(f"  median = {series.median():.1f} ms")
    print(f"  p95    = {series.quantile(0.95):.1f} ms")
    print(f"  p99    = {series.quantile(0.99):.1f} ms")
    print(f"  max    = {series.max():.1f} ms")


def plot_latency(merged: pd.DataFrame, output_dir: str) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    col = "perc_to_action_ms"
    data = merged[col].dropna()
    n_sessions = merged["session"].nunique() if "session" in merged.columns else 1
    suffix = f" ({n_sessions} sessions combined, n={len(data)})" if n_sessions > 1 else ""
    fig.suptitle(f"End-to-End Pipeline Latency{suffix}")

    axes[0].hist(data, bins=40, color="#2196f3", edgecolor="white")
    axes[0].set_xlabel("Perception → Action latency (ms)")
    axes[0].set_ylabel("Count")
    axes[0].set_title("Distribution")
    for pct, color in [(0.5, "orange"), (0.95, "red"), (0.99, "darkred")]:
        val = data.quantile(pct)
        axes[0].axvline(val, color=color, linestyle="--",
                        label=f"p{int(pct*100)}={val:.0f}ms")
    axes[0].legend()

    t0 = merged["_ts_left"].min()
    if "session" in merged.columns and merged["session"].nunique() > 1:
        colors = plt.cm.tab10.colors
        for i, session in enumerate(merged["session"].unique()):
            sub = merged[merged["session"] == session]
            axes[1].plot((sub["_ts_left"] - t0) / 1000, sub[col],
                         alpha=0.7, linewidth=0.8, color=colors[i % len(colors)], label=session)
        axes[1].legend(fontsize=8)
        axes[1].set_xlabel("Time since first session start (s)")
        axes[1].set_title("Latency over all sessions")
    else:
        axes[1].plot((merged["_ts_left"] - t0) / 1000, data.values, alpha=0.6, linewidth=0.8)
        axes[1].set_xlabel("Session time (s)")
        axes[1].set_title("Latency over session")
    axes[1].set_ylabel("Latency (ms)")

    plt.tight_layout()
    path = f"{output_dir}/latency.png"
    plt.savefig(path, dpi=150)
    print(f"Plot saved: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default="http://localhost:8181")
    parser.add_argument("--db-name", default="crazyflie")
    parser.add_argument("--session-start",
                        help="ISO8601 UTC start of session, e.g. 2026-06-03T10:00:00Z")
    parser.add_argument("--session-end",
                        help="ISO8601 UTC end of session")
    parser.add_argument("--session-file",
                        help="JSON from record_session.py — overrides --session-start/--session-end if given")
    parser.add_argument("--session-files", nargs="+",
                        help="Multiple JSON files from record_session.py — merges perception/action pairs "
                        "from all of them into one combined analysis (e.g. several short flights)")
    parser.add_argument("--output", default="results/latency_results.csv")
    args = parser.parse_args()

    if args.session_files:
        sessions = [json.loads(Path(p).read_text()) for p in args.session_files]
    elif args.session_file:
        sessions = [json.loads(Path(args.session_file).read_text())]
    elif args.session_start and args.session_end:
        sessions = [{"label": "session", "start": args.session_start, "end": args.session_end}]
    else:
        parser.error("one of --session-files, --session-file, or both --session-start and --session-end, is required")

    output_dir = str(Path(args.output).parent)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    all_merged = []
    for session in sessions:
        label = session.get("label", "session")
        print(f"[{label}] Fetching perception/action records...")
        perception = fetch_perception(args.db_url, args.db_name, session["start"], session["end"])
        action = fetch_action(args.db_url, args.db_name, session["start"], session["end"])
        print(f"  {len(perception)} perception rows, {len(action)} action rows")
        if perception.empty or action.empty:
            print(f"  WARNING: no data for session '{label}' — skipping")
            continue
        merged = compute_latencies(perception, action)
        merged["session"] = label
        all_merged.append(merged)

    if not all_merged:
        print("ERROR: No data found in any session. Check session timestamps and database.")
        return

    merged = pd.concat(all_merged, ignore_index=True)

    print("\nComputing latencies...")
    print_stats(merged["perc_to_action_ms"], "Perception → Action latency")

    merged.to_csv(args.output, index=False)
    print(f"\nResults saved to {args.output}")

    plot_latency(merged, output_dir)


if __name__ == "__main__":
    main()
