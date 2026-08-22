"""Tests for the optional ``schedule.offset_minutes`` jitter/phase field in
``compute_next_run``.

Implements DECIDER-approved proposal t_0256865b: a deterministic phase offset
that spreads jobs that would otherwise fire on the same tick (reducing the
TERMINAL_CWD readers-writer lock cascade, incident t_b79554a8 / #79768).

The field is strictly additive and sanitized: absent, negative, or non-numeric
values fall back to today's behavior (no offset) and never raise.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("croniter")

from cron.jobs import compute_next_run


def _shift_aware(dt: datetime, minutes: int) -> datetime:
    """Add ``minutes`` to an aware datetime, preserving its tzinfo."""
    return dt + timedelta(minutes=minutes)


class TestOffsetInterval:
    def test_offset_applied_to_first_interval_run(self, monkeypatch):
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=ZoneInfo("UTC"))
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "interval", "minutes": 60, "offset_minutes": 5}
        result = compute_next_run(schedule)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        # now + 60 (interval) + 5 (offset) = now + 65
        assert next_dt == _shift_aware(now, 65)

    def test_offset_applied_to_interval_with_last_run(self, monkeypatch):
        # STABILITY regression (t_aafa78ce): feeding the FIRST compute_next_run
        # output back as last_run_at must preserve period = minutes with a
        # constant offset phase — NOT single-anchored drift (period = minutes +
        # offset). A 60m/offset5 job must fire 01:05, 02:05, 03:05, ...
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=ZoneInfo("UTC"))
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "interval", "minutes": 60, "offset_minutes": 5}

        # First run anchors the phase: now + 60 + 5 = now + 65 = 23:05.
        first = compute_next_run(schedule)
        assert first is not None
        first_dt = datetime.fromisoformat(first)
        assert first_dt == _shift_aware(now, 65)
        assert first_dt.minute == 5

        # Second run: anchor (already offset) + minutes only -> 00:05 next day.
        second = compute_next_run(schedule, last_run_at=first)
        assert second is not None
        second_dt = datetime.fromisoformat(second)
        # Period is exactly `minutes` (60), not minutes + offset (65).
        assert second_dt - first_dt == timedelta(minutes=60)
        # Phase stays constant at :05.
        assert second_dt.minute == 5

        # Third run: still period 60, phase constant :05 -> 01:05.
        third = compute_next_run(schedule, last_run_at=second)
        assert third is not None
        third_dt = datetime.fromisoformat(third)
        assert third_dt - second_dt == timedelta(minutes=60)
        assert third_dt.minute == 5


class TestOffsetCron:
    def test_offset_applied_to_cron_expr(self, monkeypatch):
        morocco = ZoneInfo("Africa/Casablanca")
        last = datetime(2026, 4, 6, 14, 10, 0, tzinfo=morocco)
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=morocco)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        # every 6h on the hour; base Apr 6 14:10 -> next grid point 18:00.
        schedule = {"kind": "cron", "expr": "0 */6 * * *", "offset_minutes": 5}
        result = compute_next_run(schedule, last_run_at=last.isoformat())
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt.date().isoformat() == "2026-04-06"
        assert next_dt.hour == 18
        assert next_dt.minute == 5

    def test_offset_zero_is_a_noop(self, monkeypatch):
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=ZoneInfo("UTC"))
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "interval", "minutes": 60, "offset_minutes": 0}
        result = compute_next_run(schedule)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt == _shift_aware(now, 60)


class TestOffsetAbsent:
    def test_absent_offset_interval_unchanged(self, monkeypatch):
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=ZoneInfo("UTC"))
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "interval", "minutes": 60}
        result = compute_next_run(schedule)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt == _shift_aware(now, 60)

    def test_absent_offset_cron_unchanged(self, monkeypatch):
        morocco = ZoneInfo("Africa/Casablanca")
        last = datetime(2026, 4, 6, 14, 10, 0, tzinfo=morocco)
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=morocco)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "cron", "expr": "0 */6 * * *"}
        result = compute_next_run(schedule, last_run_at=last.isoformat())
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt.date().isoformat() == "2026-04-06"
        assert next_dt.hour == 18
        assert next_dt.minute == 0


class TestOffsetSanitized:
    def test_negative_offset_ignored(self, monkeypatch):
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=ZoneInfo("UTC"))
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "interval", "minutes": 60, "offset_minutes": -15}
        result = compute_next_run(schedule)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt == _shift_aware(now, 60)

    @pytest.mark.parametrize("bogus", ["5", "five", True, False, [5], {"m": 5}])
    def test_bogus_offset_ignored(self, monkeypatch, bogus):
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=ZoneInfo("UTC"))
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "interval", "minutes": 60, "offset_minutes": bogus}
        result = compute_next_run(schedule)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt == _shift_aware(now, 60), f"bogus={bogus!r} must be ignored"

    def test_float_offset_is_numeric_and_accepted(self, monkeypatch):
        # A float is numeric (not "non-numeric"); it is truncated to whole
        # minutes and applied.
        now = datetime(2026, 4, 10, 22, 0, 0, tzinfo=ZoneInfo("UTC"))
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        schedule = {"kind": "interval", "minutes": 60, "offset_minutes": 5.9}
        result = compute_next_run(schedule)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        assert next_dt == _shift_aware(now, 65)
