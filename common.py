"""Small helpers shared by the collector, daily alert and dashboard."""

import logging
import logging.handlers
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stdout))


def pct_change(new: float | None, baseline: float | None) -> float | None:
    if new is None or baseline in (None, 0):
        return None
    return (new - baseline) / baseline * 100


def cumulative_kwh(points: list[tuple[datetime, float | None]]) -> list[float]:
    """Trapezoidal-rule integration of chronological (time, watts) points -> running kWh
    total at each point. Missing power (None or NaN) counts as 0 W. The one integration
    used everywhere: the collector's fallback day total (integrate_kwh) and the dashboard's
    cumulative charts (add_cumulative_kwh), so they always agree."""
    totals, total, prev_t, prev_w = [], 0.0, None, None
    for t, w in points:
        w = 0.0 if w is None or (isinstance(w, float) and math.isnan(w)) else w
        if prev_t is not None:
            total += (t - prev_t).total_seconds() / 3600 * (w + prev_w) / 2 / 1000
        totals.append(total)
        prev_t, prev_w = t, w
    return totals
