"""
Run collector/collect.py on a loop locally until a stop time - for manually covering a
polling window when the GitHub Actions workflow is paused/disabled (see
.github/workflows/collect.yml, which does the same thing in bash, per-segment, on GH's
own runners). Each cycle runs collect.py as a fresh subprocess (avoids any state/logging
handler leakage between repeated in-process calls), and cycles are aligned to real
clock boundaries (:00, :05, :10, ... for the default 5-min interval) rather than
whenever the script happened to start - matching what a real `*/5 * * * *` cron does.

Usage:
    uv run python run_local_collector.py                     # every 5 min until 19:00 IST
    uv run python run_local_collector.py --until 17:00
    uv run python run_local_collector.py --until 17:00 --interval 300
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")


def parse_until(until_str: str) -> datetime:
    now = datetime.now(IST)
    hour, minute = map(int, until_str.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def next_aligned_time(now: datetime, interval_seconds: int) -> datetime:
    """Next clock boundary that's a multiple of interval_seconds since midnight, e.g.
    for a 300s interval: :00, :05, :10, ... regardless of what time `now` actually is."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    next_slot = (int(elapsed // interval_seconds) + 1) * interval_seconds
    return midnight + timedelta(seconds=next_slot)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run collect.py on a loop until a stop time (IST)")
    parser.add_argument("--until", default="19:00", help="Stop time, IST, HH:MM (default 19:00)")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between polls (default 300)")
    args = parser.parse_args()

    stop_at = parse_until(args.until)
    print(f"Running collector every {args.interval}s (clock-aligned) until "
          f"{stop_at.strftime('%Y-%m-%d %H:%M')} IST", flush=True)

    needs_human = False
    while True:
        target = next_aligned_time(datetime.now(IST), args.interval)
        if target > stop_at:
            break
        sleep_for = (target - datetime.now(IST)).total_seconds()
        if sleep_for > 0:
            time.sleep(sleep_for)

        now_str = datetime.now(IST).strftime("%H:%M:%S")
        result = subprocess.run(["uv", "run", "collector/collect.py"], cwd=PROJECT_ROOT)
        print(f"[{now_str}] collect.py exited {result.returncode}", flush=True)
        if result.returncode >= 2:
            needs_human = True
            print("  -> auth failure or API schema change - check logs/ntfy", flush=True)

    suffix = " (had auth/schema failures during the run - check ntfy/logs)" if needs_human else ""
    print(f"Reached stop time.{suffix}", flush=True)
    return 1 if needs_human else 0


if __name__ == "__main__":
    sys.exit(main())
