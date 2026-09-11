#!/usr/bin/env python3
"""
Marks a session's start/end by keypress instead of hand-typing ISO8601 timestamps
and pasting them into --session-start/--session-end for analyze_latency.py or
analyze_gesture_timing.py.

Usage:
    python tools/record_session.py --label latency
    # Enter #1: right before takeoff (Exp 5.2/5.5) or right before the first gesture (Exp 5.4)
    # ... do the flight / gesture trials ...
    # Enter #2: right after landing / right after the last trial

Writes results/session_<label>.json — pass it to the analyze script instead of
typing timestamps by hand:

    python tools/analyze_latency.py --session-file results/session_latency.json ...
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Keypress-based session start/end marker")
    parser.add_argument("--label", required=True, help="Session label, e.g. latency, gesture_timing, resource")
    parser.add_argument("--output-dir", default="results")
    args = parser.parse_args()

    input("Press Enter to mark SESSION START (e.g. right before takeoff)... ")
    start = iso_now()
    print(f"  START: {start}")

    input("Session running. Press Enter to mark SESSION END (e.g. right after landing)... ")
    end = iso_now()
    print(f"  END:   {end}")

    out_path = Path(args.output_dir) / f"session_{args.label}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"label": args.label, "start": start, "end": end}, indent=2))

    print(f"\nSaved {out_path}")
    print(f"Use: --session-file {out_path}")


if __name__ == "__main__":
    main()
