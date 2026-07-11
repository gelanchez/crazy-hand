#!/usr/bin/env python3
"""
Gesture accuracy test — prompts operator to show gestures and logs ground truth.

Usage:
    python tools/gesture_accuracy_test.py [--output results/gesture_accuracy.csv]

The system must be running normally (all nodes active, camera streaming).
After the session, join the output CSV with the InfluxDB `perception` table
using BETWEEN start_timestamp_ms AND end_timestamp_ms to compute precision/recall per gesture.
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

GESTURES = ["Thumb_Up", "Thumb_Down", "Victory"]
LIGHTINGS = ["good", "poor"]
BACKGROUNDS = ["plain", "cluttered"]
DISTANCES = ["1.0m", "1.5m"]
TRIALS_PER_CELL = 10
HOLD_SECONDS = 2.0
REST_SECONDS = 1.5


def build_trial_list() -> list[dict]:
    trials = []
    for lighting, background, distance, gesture in product(
        LIGHTINGS, BACKGROUNDS, DISTANCES, GESTURES
    ):
        for trial in range(1, TRIALS_PER_CELL + 1):
            trials.append(
                {
                    "lighting": lighting,
                    "background": background,
                    "distance": distance,
                    "gesture": gesture,
                    "trial": trial,
                }
            )
    return trials


def run(output_path: str) -> None:
    trials = build_trial_list()
    total = len(trials)

    print(f"\nGesture accuracy test — {total} trials")
    print(f"Output: {output_path}")
    print("\nPress Enter to start each trial. Ctrl+C to abort.\n")

    fieldnames = [
        "trial_index",
        "lighting",
        "background",
        "distance",
        "gesture",
        "trial",
        "start_timestamp_ms",
        "end_timestamp_ms",
    ]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        current_condition = {}

        for idx, trial in enumerate(trials, start=1):
            condition = (trial["lighting"], trial["background"], trial["distance"])
            if condition != current_condition:
                current_condition = condition
                print(
                    f"\n{'='*60}\n"
                    f"  Lighting:   {trial['lighting']}\n"
                    f"  Background: {trial['background']}\n"
                    f"  Distance:   {trial['distance']}\n"
                    f"{'='*60}\n"
                    f"  Set up the conditions above, then press Enter.\n"
                )
                input()

            print(
                f"[{idx:3d}/{total}] Show gesture: {trial['gesture']:12s} "
                f"(trial {trial['trial']}/{TRIALS_PER_CELL}) — press Enter when ready"
            )
            input()

            print(f"  → SHOW {trial['gesture']} NOW", flush=True)
            start_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            time.sleep(HOLD_SECONDS)
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            print(f"  → REST ({REST_SECONDS:.0f}s)", flush=True)
            time.sleep(REST_SECONDS)

            writer.writerow(
                {
                    "trial_index": idx,
                    "lighting": trial["lighting"],
                    "background": trial["background"],
                    "distance": trial["distance"],
                    "gesture": trial["gesture"],
                    "trial": trial["trial"],
                    "start_timestamp_ms": start_ms,
                    "end_timestamp_ms": end_ms,
                }
            )
            f.flush()

    print(f"\nDone. Results written to {output_path}")
    print(
        "\nNext step: join this CSV with InfluxDB `perception` table on timestamp\n"
        "  WHERE perception.ts BETWEEN start_timestamp_ms AND end_timestamp_ms\n"
        "  Compare perception.gesture_name vs expected gesture → precision/recall."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Gesture accuracy ground-truth logger")
    parser.add_argument(
        "--output",
        default="results/gesture_accuracy.csv",
        help="Output CSV path (default: results/gesture_accuracy.csv)",
    )
    args = parser.parse_args()

    try:
        run(args.output)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
