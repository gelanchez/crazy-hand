#!/usr/bin/env python3
"""
Post-hoc gesture ground-truth labeling — no live trial timing required.

Capture first: enable process images (Ctrl+P) and save images (Ctrl+S), then freely
perform gestures for a while under one condition (no scripted timing, no per-gesture
Enter). Processed (annotated) frames land in data/processed/<timestamp>.<ext>.

No directory shuffling needed between conditions — capture all conditions into the
same data/processed/ folder in one continuous sitting. Mark each condition's time
window with `record_session.py --label <condition>` (run it once per condition,
back to back, in the same terminal — no need to restart the ground station or move
any files). This tool then reads all of those session files and assigns each sampled
frame to whichever condition's time window contains it (frames outside every window —
e.g. while repositioning the drone or lighting between conditions — are skipped
automatically, not shown to you).

Then run this tool once over the whole capture. It samples a subset of saved frames,
shows each one to you, asks what gesture it actually shows, and looks up what the
system itself detected for that exact frame (nearest InfluxDB perception row by
timestamp — frame filenames are epoch-ms capture timestamps, see wifi_node.py:205 /
logger_node.py). Type 's' to skip a frame that's ambiguous (mid-transition, hand
leaving frame, blur) — skipped frames are not written to the output.

Usage:
    python tools/label_gesture_frames.py \
        --dir data/processed \
        --session-files results/session_good_1.0m.json results/session_good_1.5m.json \
                        results/session_poor_1.0m.json results/session_poor_1.5m.json \
        --output results/labels.csv

Then analyze (a single labels CSV is fine — --labels accepts one or more):
    python tools/analyze_gesture_accuracy.py \
        --labels results/labels.csv \
        --output results/gesture_accuracy_results.csv
"""

import argparse
import bisect
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()  # loads INFLUXDB3_TOKEN from .env, same as client/main.py

GESTURE_CODES = {
    "u": "Thumb_Up",
    "d": "Thumb_Down",
    "v": "Victory",
    "n": "NONE",
}
MATCH_TOLERANCE_MS = 150  # ~1 frame interval at ~8 FPS RAW; see ch5_evaluation.md §5.2.4


def query_influxdb(db_url: str, db_name: str, query: str) -> list[dict]:
    token = os.environ.get("INFLUXDB3_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.post(
        f"{db_url}/api/v3/query_sql",
        json={"db": db_name, "q": query},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json() or []


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_perception_rows(db_url: str, db_name: str, start_ms: int, end_ms: int) -> list[dict]:
    pad_ms = 2000
    query = f"""
        SELECT time, gesture_name, gesture_confidence, hand_detected
        FROM perception
        WHERE time >= '{_iso(start_ms - pad_ms)}' AND time <= '{_iso(end_ms + pad_ms)}'
        ORDER BY time
    """
    rows = query_influxdb(db_url, db_name, query)
    # InfluxDB returns `time` as an ISO string (nanosecond precision truncated to
    # microseconds by fromisoformat) — parse once here so nearest_row's bisect works on ints.
    for r in rows:
        r["time_ns"] = int(datetime.fromisoformat(r["time"]).replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    rows.sort(key=lambda r: r["time_ns"])
    return rows


def nearest_row(rows: list[dict], times_ns: list[int], target_ms: int) -> dict | None:
    if not rows:
        return None
    target_ns = target_ms * 1_000_000
    i = bisect.bisect_left(times_ns, target_ns)
    candidates = [j for j in (i - 1, i) if 0 <= j < len(rows)]
    if not candidates:
        return None
    best = min(candidates, key=lambda j: abs(times_ns[j] - target_ns))
    if abs(times_ns[best] - target_ns) > MATCH_TOLERANCE_MS * 1_000_000:
        return None
    return rows[best]


def list_frames(image_dir: Path) -> list[tuple[int, Path]]:
    frames = []
    for path in image_dir.iterdir():
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        try:
            ts = int(path.stem)
        except ValueError:
            continue
        frames.append((ts, path))
    frames.sort(key=lambda t: t[0])
    return frames


def sample_frames(frames: list[tuple[int, Path]], interval_s: float) -> list[tuple[int, Path]]:
    if not frames:
        return []
    interval_ms = interval_s * 1000
    sampled = [frames[0]]
    for ts, path in frames[1:]:
        if ts - sampled[-1][0] >= interval_ms:
            sampled.append((ts, path))
    return sampled


def load_condition_windows(session_files: list[str]) -> list[tuple[str, int, int]]:
    windows = []
    for path in session_files:
        session = json.loads(Path(path).read_text())
        start_ms = int(datetime.fromisoformat(session["start"]).timestamp() * 1000)
        end_ms = int(datetime.fromisoformat(session["end"]).timestamp() * 1000)
        windows.append((session["label"], start_ms, end_ms))
    return windows


def condition_for(windows: list[tuple[str, int, int]], ts: int) -> str | None:
    for label, start_ms, end_ms in windows:
        if start_ms <= ts <= end_ms:
            return label
    return None


def show_image(path: Path) -> None:
    print(f"  Image: {path}")
    try:
        from PIL import Image
        Image.open(path).show()
    except Exception as e:
        print(f"  (could not auto-open image viewer: {e} — open the path above manually)")


def prompt_label() -> str | None:
    while True:
        raw = input("  Gesture shown? [u=Thumb_Up d=Thumb_Down v=Victory n=None s=skip]: ").strip().lower()
        if raw == "s":
            return None
        if raw in GESTURE_CODES:
            return GESTURE_CODES[raw]
        print("  Unrecognized — type u/d/v/n/s.")


def run(args: argparse.Namespace) -> None:
    image_dir = Path(args.dir)
    frames = list_frames(image_dir)
    if not frames:
        print(f"No timestamp-named image files found in {image_dir}")
        sys.exit(1)

    windows = load_condition_windows(args.session_files)

    sampled = sample_frames(frames, args.sample_interval_s)
    print(f"\n{len(frames)} frames captured, sampling {len(sampled)} at ~{args.sample_interval_s:.0f}s spacing.")
    print(f"{len(windows)} condition window(s) loaded: {', '.join(w[0] for w in windows)}")
    print("Frames outside every window (e.g. repositioning between conditions) are skipped automatically.")
    print("Take your time on each — type 's' to skip anything ambiguous. Ctrl+C to stop early.\n")

    rows = fetch_perception_rows(args.db_url, args.db_name, sampled[0][0], sampled[-1][0])
    times_ns = [r["time_ns"] for r in rows]

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "file", "timestamp_ms", "condition",
        "human_gesture", "system_gesture", "system_confidence", "system_hand_detected",
        "match",
    ]
    n_written = n_match = n_out_of_window = 0
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for idx, (ts, path) in enumerate(sampled, start=1):
            condition = condition_for(windows, ts)
            if condition is None:
                n_out_of_window += 1
                continue

            print(f"[{idx}/{len(sampled)}] condition: {condition}")
            show_image(path)
            human_label = prompt_label()
            if human_label is None:
                print("  (skipped)\n")
                continue

            row = nearest_row(rows, times_ns, ts)
            system_gesture = row["gesture_name"] if row else "NONE"
            system_confidence = row["gesture_confidence"] if row else 0.0
            system_hand_detected = bool(row["hand_detected"]) if row else False
            match = human_label == system_gesture

            writer.writerow({
                "file": str(path),
                "timestamp_ms": ts,
                "condition": condition,
                "human_gesture": human_label,
                "system_gesture": system_gesture,
                "system_confidence": system_confidence,
                "system_hand_detected": system_hand_detected,
                "match": match,
            })
            f.flush()
            n_written += 1
            n_match += int(match)
            print(f"  → system said: {system_gesture} ({'match' if match else 'MISMATCH'})\n")

    print(
        f"Done. {n_written} labeled ({n_match} matched, {n_written - n_match} mismatched), "
        f"{n_out_of_window} sampled frame(s) fell outside every condition window and were skipped. "
        f"Written to {args.output}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-hoc gesture ground-truth labeling")
    parser.add_argument("--dir", required=True, help="Directory of saved processed frames (all conditions combined)")
    parser.add_argument("--session-files", nargs="+", required=True,
                         help="JSON files from 'record_session.py --label <condition>', one per condition")
    parser.add_argument("--db-url", default="http://localhost:8181")
    parser.add_argument("--db-name", default="crazyflie")
    parser.add_argument("--sample-interval-s", type=float, default=2.0,
                         help="Minimum spacing between sampled frames (default: 2.0s)")
    parser.add_argument("--output", required=True, help="Output labels CSV path")
    args = parser.parse_args()

    try:
        run(args)
    except KeyboardInterrupt:
        print("\nStopped early — partial results already written.")


if __name__ == "__main__":
    main()
