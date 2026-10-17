from datetime import datetime, timedelta

from common import cumulative_kwh, pct_change


def test_pct_change():
    assert pct_change(12, 10) == 20
    assert pct_change(None, 10) is None
    assert pct_change(10, 0) is None
    assert pct_change(10, None) is None


def test_cumulative_kwh_running_total():
    t0 = datetime(2026, 10, 10, 6)
    points = [(t0, 0), (t0 + timedelta(hours=1), 1000), (t0 + timedelta(hours=2), 1000)]
    assert cumulative_kwh(points) == [0.0, 0.5, 1.5]


def test_cumulative_kwh_missing_power_is_zero():
    t0 = datetime(2026, 10, 10, 6)
    points = [(t0, None), (t0 + timedelta(hours=1), float("nan")), (t0 + timedelta(hours=2), 1000)]
    assert cumulative_kwh(points) == [0.0, 0.0, 0.5]


def test_cumulative_kwh_empty():
    assert cumulative_kwh([]) == []
