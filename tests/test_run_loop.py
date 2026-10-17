from datetime import datetime, timedelta

from run_loop import IST, next_aligned_time, plan_window


def at(h, m, s=0):
    return datetime(2026, 10, 18, h, m, s, tzinfo=IST)


def test_next_aligned_time():
    assert next_aligned_time(at(9, 2, 30), 300) == at(9, 5)
    assert next_aligned_time(at(9, 5), 300) == at(9, 10)  # exactly on a boundary -> the next one
    assert next_aligned_time(at(23, 58), 300) == at(0, 0) + timedelta(days=1)


def test_plan_window_hands_off_before_cap():
    first, stop, handoff = plan_window(at(8, 0), "05:30", "19:00", 340, 10)
    assert first == at(8, 0)
    assert stop == at(13, 40)
    assert handoff == at(13, 30)


def test_plan_window_no_handoff_when_cap_reaches_until():
    first, stop, handoff = plan_window(at(14, 0), "05:30", "19:00", 340, 10)
    assert stop == at(19, 0) and handoff is None


def test_plan_window_early_kickoff_waits_for_start():
    first, stop, handoff = plan_window(at(2, 0), "05:30", "19:00", 340, 10)
    assert first == at(5, 30)
    assert stop == at(7, 40)  # the cap counts from the kickoff, including the wait
    assert handoff == at(7, 30)


def test_plan_window_until_is_always_today():
    """A late kickoff after the stop time must not roll over and poll overnight."""
    first, stop, handoff = plan_window(at(19, 11), "05:30", "19:00", 340, 10)
    assert stop == at(19, 0) < at(19, 11)
    assert handoff is None


def test_plan_window_uncapped():
    assert plan_window(at(8, 0), "05:30", "19:00", None, 10) == (at(8, 0), at(19, 0), None)
