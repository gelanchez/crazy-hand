#!/usr/bin/env python3
"""
Records per-node CPU/memory usage for Exp 5.5, replacing the ad-hoc psutil
one-liner with structured CSV output (no manual parsing of printed dicts).

Usage (start BEFORE takeoff, in a separate terminal, run through the flight,
Ctrl+C right after landing):

    python tools/record_resource_usage.py --output results/resource_usage.csv

Also snapshots /dev/shm and InfluxDB data-dir sizes at start and Ctrl+C, so the
size-growth checks from Exp 5.5's protocol don't need separate manual commands.
"""

import argparse
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

NODES = ["wifi_node", "vision_node", "control_node", "gui_node", "logger_node"]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def sample_nodes() -> list[dict]:
    rows = []
    for p in psutil.process_iter(["pid", "cmdline", "cpu_percent", "memory_info"]):
        cmd = " ".join(p.info["cmdline"] or [])
        for node in NODES:
            if node in cmd:
                rows.append({
                    "ts": iso_now(),
                    "node": node,
                    "pid": p.info["pid"],
                    "cpu_percent": p.info["cpu_percent"],
                    "rss_mb": p.info["memory_info"].rss / (1024 * 1024),
                })
                break
    return rows


def snapshot_sizes(influxdb_dir: Path) -> dict:
    return {
        "shm_bytes": dir_size_bytes(Path("/dev/shm")),
        "influxdb_bytes": dir_size_bytes(influxdb_dir),
    }


def run(args: argparse.Namespace) -> None:
    influxdb_dir = Path(args.influxdb_dir).expanduser()

    print(f"Sampling {NODES} every {args.interval_s:.0f}s. Ctrl+C to stop (do it right after landing).\n")
    before = snapshot_sizes(influxdb_dir)
    print(f"  /dev/shm at start:   {before['shm_bytes'] / 1024:.1f} KB")
    print(f"  InfluxDB dir at start: {before['influxdb_bytes'] / (1024*1024):.1f} MB ({influxdb_dir})\n")

    # First cpu_percent() call per process always returns 0.0 — warm it up.
    sample_nodes()
    time.sleep(0.1)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    n_samples = 0
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ts", "node", "pid", "cpu_percent", "rss_mb"])
        writer.writeheader()
        try:
            while True:
                rows = sample_nodes()
                for row in rows:
                    writer.writerow(row)
                f.flush()
                n_samples += len(rows)
                found = {r["node"] for r in rows}
                missing = set(NODES) - found
                status = f"  {len(rows)} processes" + (f" (missing: {', '.join(sorted(missing))})" if missing else "")
                print(status, flush=True)
                time.sleep(args.interval_s)
        except KeyboardInterrupt:
            pass

    after = snapshot_sizes(influxdb_dir)
    print(f"\nStopped. {n_samples} samples written to {args.output}")
    print(f"  /dev/shm:      {before['shm_bytes']/1024:.1f} KB → {after['shm_bytes']/1024:.1f} KB "
          f"(Δ {(after['shm_bytes']-before['shm_bytes'])/1024:+.1f} KB)")
    print(f"  InfluxDB dir:  {before['influxdb_bytes']/(1024*1024):.1f} MB → {after['influxdb_bytes']/(1024*1024):.1f} MB "
          f"(Δ {(after['influxdb_bytes']-before['influxdb_bytes'])/(1024*1024):+.2f} MB)")
    print("\nNext step: python tools/analyze_resource_usage.py --csv " + args.output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-node CPU/memory usage recorder")
    parser.add_argument("--output", default="results/resource_usage.csv")
    parser.add_argument("--interval-s", type=float, default=2.0)
    parser.add_argument("--influxdb-dir", default="~/.influxdb/data",
                         help="InfluxDB 3 data directory to track growth of (default: ~/.influxdb/data)")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
