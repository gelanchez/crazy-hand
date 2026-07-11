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
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import requests


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
    df["ts_ms"] = pd.to_numeric(df["time"]) / 1_000_000
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
    df["ts_ms"] = pd.to_numeric(df["time"]) / 1_000_000
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
    df["ts_ms"] = pd.to_numeric(df["time"]) / 1_000_000
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
    fig.suptitle("End-to-End Pipeline Latency")

    col = "perc_to_action_ms"
    data = merged[col].dropna()

    axes[0].hist(data, bins=40, color="#2196f3", edgecolor="white")
    axes[0].set_xlabel("Perception → Action latency (ms)")
    axes[0].set_ylabel("Count")
    axes[0].set_title("Distribution")
    for pct, color in [(0.5, "orange"), (0.95, "red"), (0.99, "darkred")]:
        val = data.quantile(pct)
        axes[0].axvline(val, color=color, linestyle="--",
                        label=f"p{int(pct*100)}={val:.0f}ms")
    axes[0].legend()

    axes[1].plot(merged["_ts_left"] - merged["_ts_left"].iloc[0], data.values,
                 alpha=0.6, linewidth=0.8)
    axes[1].set_xlabel("Session time (ms)")
    axes[1].set_ylabel("Latency (ms)")
    axes[1].set_title("Latency over session")

    plt.tight_layout()
    path = f"{output_dir}/latency.png"
    plt.savefig(path, dpi=150)
    print(f"Plot saved: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default="http://localhost:8181")
    parser.add_argument("--db-name", default="crazyflie")
    parser.add_argument("--session-start", required=True,
                        help="ISO8601 UTC start of session, e.g. 2026-06-03T10:00:00Z")
    parser.add_argument("--session-end", required=True,
                        help="ISO8601 UTC end of session")
    parser.add_argument("--output", default="results/latency_results.csv")
    args = parser.parse_args()

    output_dir = str(Path(args.output).parent)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print("Fetching perception records...")
    perception = fetch_perception(args.db_url, args.db_name, args.session_start, args.session_end)
    print(f"  {len(perception)} rows")

    print("Fetching action records...")
    action = fetch_action(args.db_url, args.db_name, args.session_start, args.session_end)
    print(f"  {len(action)} rows")

    if perception.empty or action.empty:
        print("ERROR: No data found. Check session timestamps and database.")
        return

    print("Computing latencies...")
    merged = compute_latencies(perception, action)

    print_stats(merged["perc_to_action_ms"], "Perception → Action latency")

    merged.to_csv(args.output, index=False)
    print(f"\nResults saved to {args.output}")

    plot_latency(merged, output_dir)


if __name__ == "__main__":
    main()
