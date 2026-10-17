from datetime import date, datetime

from daily_alert import IST, build_message, pct_change, report_date


def test_report_date_late_run_reports_previous_day(monkeypatch):
    monkeypatch.delenv("REPORT_DATE", raising=False)
    assert report_date(datetime(2026, 10, 18, 1, 2, tzinfo=IST)) == date(2026, 10, 17)
    assert report_date(datetime(2026, 10, 18, 6, 0, tzinfo=IST)) == date(2026, 10, 18)
    assert report_date(datetime(2026, 10, 18, 21, 0, tzinfo=IST)) == date(2026, 10, 18)


def test_report_date_override(monkeypatch):
    monkeypatch.setenv("REPORT_DATE", "2026-09-01")
    assert report_date(datetime(2026, 10, 18, 21, 0, tzinfo=IST)) == date(2026, 9, 1)


def test_pct_change():
    assert pct_change(12, 10) == 20
    assert pct_change(None, 10) is None
    assert pct_change(10, 0) is None
    assert pct_change(10, None) is None


def test_build_message_includes_ranks_gaps_and_comparisons():
    msg = build_message(date(2026, 10, 17), 12.0, 10.0, 11.0, 2.4, "12:05",
                        [("09:05", "10:11")], None, yearly_rank=2, alltime_rank=3)
    assert "#2 this year and #3 all-time" in msg
    assert "Total production: 12.00 kWh" in msg
    assert "vs yesterday (10.00 kWh): +20%" in msg
    assert "09:05-10:11" in msg
    assert "Weather" not in msg


def test_build_message_handles_missing_data():
    msg = build_message(date(2026, 10, 17), None, None, None, None, None, [], None, None, None)
    assert "Total production: unavailable" in msg
    assert "vs yesterday" not in msg and "Top production day" not in msg
