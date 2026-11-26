from datetime import datetime, timedelta, timezone

from app.services.execution_loop import _remaining_timeout


def test_deadline_accepts_naive_sqlite_utc_datetime():
    now = datetime(2026, 11, 26, 8, 0, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 11, 26, 8, 2, 0)
    assert _remaining_timeout(deadline, 120, now=now) == 120


def test_deadline_uses_remaining_time_when_shorter_than_model_timeout():
    now = datetime(2026, 11, 26, 8, 0, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(seconds=30)
    assert _remaining_timeout(deadline, 120, now=now) == 30


def test_deadline_returns_non_positive_for_expired_deadline():
    now = datetime(2026, 11, 26, 8, 0, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 11, 26, 7, 59, 59)
    assert _remaining_timeout(deadline, 120, now=now) == -1
