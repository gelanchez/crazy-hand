#!/usr/bin/env python3
"""
Analyze gesture confirmation timing — time from first gesture detection to command issuance.

Queries InfluxDB perception + action tables, finds each state-change event,
locates the earliest preceding gesture detection in the debounce window,
and computes the confirmation delta.

Usage:
    python tools/analyze_gesture_timing.py \
        --db-url http://localhost:8181 \
        --db-name crazyflie \
        --session-start "2026-06-03T10:00:00Z" \
        --session-end   "2026-06-03T10:05:00Z" \
        --output results/gesture_timing_results.csv

Protocol: fly in AIRBORNE/TRACKING, show each gesture (any order) and hold until command
fires, repeat as many times per gesture as decided. This script processes any session
that contains state changes, regardless of trial count or order.
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


THEORETICAL_WORST_MS = 435  # 300ms debounce + 120ms inter-frame (8.3 FPS RAW) + ~15ms inference
DEBOUNCE_MS = 300
LOOKBACK_MS = 1000  # how far back to search for first gesture detection

GESTURE_TO_COMMAND = {
    "Thumb_Up": "TAKEOFF",
    "Thumb_Down": "LAND",
    "Victory": "TOGGLE_TRACKING",
}


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
        SELECT time, gesture_name, gesture_confidence, hand_detected
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
        SELECT time, state, command, source
        FROM action
        WHERE time >= '{start}' AND time <= '{end}'
          AND source = 'GESTURE'
        ORDER BY time
    """
    df = query_influxdb(db_url, db_name, query)
    if df.empty:
        return df
    df["ts_ms"] = pd.to_datetime(df["time"]).astype("int64") / 1_000_000
    return df


def find_state_changes(action: pd.DataFrame) -> pd.DataFrame:
    """Identify rows where state or command changed."""
    changes = action[action["state"] != action["state"].shift()]
    return changes.copy()


def find_first_gesture_before(perception: pd.DataFrame,
                               event_ms: float,
                               target_gesture: str,
                               lookback_ms: float = LOOKBACK_MS) -> float | None:
    """Find the earliest ms where target_gesture was detected before event_ms."""
    window = perception[
        (perception["ts_ms"] >= event_ms - lookback_ms) &
        (perception["ts_ms"] <= event_ms) &
        (perception["gesture_name"] == target_gesture) &
        (perception["hand_detected"] == True)
    ]
    if window.empty:
        return None
    return window["ts_ms"].min()


def analyze(perception: pd.DataFrame, action: pd.DataFrame) -> pd.DataFrame:
    state_changes = find_state_changes(action)
    rows = []

    for _, event in state_changes.iterrows():
        new_state = event["state"]
        command = event["command"]
        event_ms = event["ts_ms"]

        # Map state transition to expected gesture. Note: LANDING is a multi-row process —
        # control_node republishes state=LANDING/command=NONE on every frame during the
        # physical descent, then finally state=IDLE/command=LAND once grounded (~1.4-1.6s
        # later). We want the trigger moment (entry into LANDING), not that later completion
        # row, so match on the state alone rather than requiring command == "LAND".
        gesture = None
        if new_state in ("AIRBORNE", "TRACKING") and command == "TAKEOFF":
            gesture = "Thumb_Up"
        elif new_state == "LANDING":
            gesture = "Thumb_Down"
        elif command == "TOGGLE_TRACKING":
            gesture = "Victory"

        if gesture is None:
            continue

        first_detection_ms = find_first_gesture_before(perception, event_ms, gesture)
        if first_detection_ms is None:
            continue

        delta_ms = event_ms - first_detection_ms
        rows.append({
            "gesture": gesture,
            "command": command,
            "new_state": new_state,
            "event_ms": event_ms,
            "first_detection_ms": first_detection_ms,
            "confirmation_ms": round(delta_ms, 1),
            "within_theoretical": delta_ms <= THEORETICAL_WORST_MS * 1.5,
        })

    return pd.DataFrame(rows)


def print_stats(results: pd.DataFrame) -> None:
    print(f"\nTotal events analyzed: {len(results)}")
    print(f"Theoretical worst-case: {THEORETICAL_WORST_MS} ms\n")

    for gesture in results["gesture"].unique():
        subset = results[results["gesture"] == gesture]["confirmation_ms"]
        print(f"{gesture}:")
        print(f"  n      = {len(subset)}")
        print(f"  mean   = {subset.mean():.1f} ms")
        print(f"  median = {subset.median():.1f} ms")
        print(f"  p95    = {subset.quantile(0.95):.1f} ms")
        print(f"  max    = {subset.max():.1f} ms")
        outliers = (subset > THEORETICAL_WORST_MS * 1.5).sum()
        print(f"  outliers (>{THEORETICAL_WORST_MS * 1.5:.0f} ms) = {outliers}")


def plot_timing(results: pd.DataFrame, output_dir: str) -> None:
    gestures = results["gesture"].unique()
    fig, axes = plt.subplots(1, len(gestures), figsize=(5 * len(gestures), 5))
    if len(gestures) == 1:
        axes = [axes]
    fig.suptitle("Gesture Confirmation Timing")

    for ax, gesture in zip(axes, gestures):
        data = results[results["gesture"] == gesture]["confirmation_ms"]
        ax.hist(data, bins=15, color="#9c27b0", edgecolor="white")
        ax.axvline(THEORETICAL_WORST_MS, color="red", linestyle="--",
                   label=f"Theoretical worst ({THEORETICAL_WORST_MS}ms)")
        ax.axvline(data.mean(), color="orange", linestyle="-",
                   label=f"Mean ({data.mean():.0f}ms)")
        ax.set_xlabel("Confirmation time (ms)")
        ax.set_ylabel("Count")
        ax.set_title(gesture)
        ax.legend(fontsize=8)

    plt.tight_layout()
    path = f"{output_dir}/gesture_timing.png"
    plt.savefig(path, dpi=150)
    print(f"\nPlot saved: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default="http://localhost:8181")
    parser.add_argument("--db-name", default="crazyflie")
    parser.add_argument("--session-start")
    parser.add_argument("--session-end")
    parser.add_argument("--session-file",
                        help="JSON from record_session.py — overrides --session-start/--session-end if given")
    parser.add_argument("--output", default="results/gesture_timing_results.csv")
    args = parser.parse_args()

    if args.session_file:
        session = json.loads(Path(args.session_file).read_text())
        args.session_start, args.session_end = session["start"], session["end"]
    elif not (args.session_start and args.session_end):
        parser.error("either --session-file, or both --session-start and --session-end, are required")

    output_dir = str(Path(args.output).parent)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print("Fetching perception records...")
    perception = fetch_perception(args.db_url, args.db_name, args.session_start, args.session_end)
    print(f"  {len(perception)} rows")

    print("Fetching action records...")
    action = fetch_action(args.db_url, args.db_name, args.session_start, args.session_end)
    print(f"  {len(action)} rows")

    if perception.empty or action.empty:
        print("ERROR: No data found.")
        return

    print("Analyzing gesture confirmation timing...")
    results = analyze(perception, action)

    if results.empty:
        print("No gesture-triggered state changes found in the session.")
        return

    print_stats(results)

    results.to_csv(args.output, index=False)
    print(f"\nResults saved to {args.output}")

    plot_timing(results, output_dir)


if __name__ == "__main__":
    main()
