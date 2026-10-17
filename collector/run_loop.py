"""
Runs collector/collect.py every 5 minutes (clock-aligned) within today's IST collection
window. Used both by .github/workflows/collect.yml and locally, e.g. to cover a window by
hand while the workflow is paused/disabled.

Each cycle runs collect.py as a fresh subprocess (avoids any state/logging handler leakage
between repeated in-process calls), and cycles are aligned to real clock boundaries
(:00, :05, :10, ... for the default 5-min interval) rather than whenever the script
happened to start - matching what a real `*/5 * * * *` cron does.

--max-minutes caps this run's own length (a GitHub-hosted job dies at 6h); if the cap
falls before --until, --handoff-cmd is run --handoff-before minutes ahead of the cap to
start a successor (collect.yml passes a `gh workflow run collect.yml ...` command), so the
two overlap briefly instead of leaving a gap. See collect.yml for why it chains this way.

Usage:
    uv run collector/run_loop.py                     # every 5 min, 05:30-19:00 IST
    uv run collector/run_loop.py --until 17:00
    uv run collector/run_loop.py --max-minutes 340 --handoff-cmd "gh workflow run collect.yml"
"""

import argparse
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")


def today_at(now: datetime, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_aligned_time(now: datetime, interval_seconds: int) -> datetime:
    """Next clock boundary that's a multiple of interval_seconds since midnight, e.g.
    for a 300s interval: :00, :05, :10, ... regardless of what time `now` actually is."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    next_slot = (int(elapsed // interval_seconds) + 1) * interval_seconds
    return midnight + timedelta(seconds=next_slot)


def plan_window(now: datetime, start: str, until: str, max_minutes: float | None,
                handoff_before_minutes: float) -> tuple[datetime, datetime, datetime | None]:
    """(first_poll_at, stop_at, handoff_at) for a run starting at `now`. `until` is
    always TODAY's time - a run that starts after it does nothing (never rolls over to
    tomorrow, which would have a late kickoff poll all night). handoff_at is None when
    this run's own cap already reaches `until`, i.e. no successor is needed."""
    first_poll_at = max(now, today_at(now, start))
    stop_at = today_at(now, until)
    if max_minutes is None:
        return first_poll_at, stop_at, None
    cap = now + timedelta(minutes=max_minutes)
    if cap >= stop_at:
        return first_poll_at, stop_at, None
    return first_poll_at, cap, cap - timedelta(minutes=handoff_before_minutes)


def sleep_until(t: datetime) -> None:
    remaining = (t - datetime.now(IST)).total_seconds()
    if remaining > 0:
        time.sleep(remaining)


def hand_off(cmd: str) -> bool:
    print(f"handing off to a successor: {cmd}", flush=True)
    result = subprocess.run(shlex.split(cmd), cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print(f"::warning::handoff command exited {result.returncode} - the next scheduled "
              f"kickoff will have to restart collection", flush=True)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run collect.py on a loop within today's IST window")
    parser.add_argument("--start", default="05:30", help="First poll no earlier than this, IST HH:MM (default 05:30)")
    parser.add_argument("--until", default="19:00", help="Stop time today, IST HH:MM (default 19:00)")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between polls (default 300)")
    parser.add_argument("--max-minutes", type=float, default=None, help="Cap on this run's own length")
    parser.add_argument("--handoff-before", type=float, default=10,
                        help="Minutes before --max-minutes to run --handoff-cmd (default 10)")
    parser.add_argument("--handoff-cmd", default=None, help="Command that starts a successor run")
    args = parser.parse_args()

    now = datetime.now(IST)
    first_poll_at, stop_at, handoff_at = plan_window(now, args.start, args.until, args.max_minutes,
                                                     args.handoff_before)
    if not args.handoff_cmd:
        handoff_at = None
    if now >= stop_at:
        print(f"Already past today's stop time ({args.until} IST) - nothing to do.", flush=True)
        return 0
    print(f"Polling every {args.interval}s (clock-aligned) from {first_poll_at:%H:%M} until "
          f"{stop_at:%Y-%m-%d %H:%M} IST" + (f", handing off at {handoff_at:%H:%M}" if handoff_at else ""),
          flush=True)

    needs_human = handoff_failed = False
    while True:
        now = datetime.now(IST)
        if handoff_at and now >= handoff_at:
            handoff_failed = not hand_off(args.handoff_cmd)
            handoff_at = None
        target = first_poll_at if now < first_poll_at else next_aligned_time(now, args.interval)
        if target > stop_at:
            break
        if handoff_at and handoff_at < target:
            sleep_until(handoff_at)
            continue
        sleep_until(target)

        result = subprocess.run(["uv", "run", "collector/collect.py"], cwd=PROJECT_ROOT)
        print(f"[{datetime.now(IST):%H:%M:%S}] collect.py exited {result.returncode}", flush=True)
        # 1 = transient network failure (self-heals via collect.py's backoff); 2 = auth
        # failure, 3 = API schema change - those need a human, and collect.py has already
        # sent an ntfy alert, but keep polling in case it's a one-off blip.
        if result.returncode >= 2:
            needs_human = True
            print("::warning::auth failure or API schema change - check logs/ntfy", flush=True)

    if handoff_at:  # cap reached without the in-loop handoff ever firing
        handoff_failed = not hand_off(args.handoff_cmd)

    print("Reached stop time." + (" (had auth/schema failures - check ntfy/logs)" if needs_human else ""),
          flush=True)
    return 1 if needs_human or handoff_failed else 0


if __name__ == "__main__":
    sys.exit(main())
