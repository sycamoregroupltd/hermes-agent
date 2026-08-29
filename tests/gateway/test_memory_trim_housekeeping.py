"""Memory-trim coverage for the long-lived messaging gateway housekeeper."""

import gateway.run as gateway_run


class _OneTickStopEvent:
    """Run one housekeeping tick without a sleep or background thread."""

    def __init__(self):
        self.waited = False

    def is_set(self):
        return self.waited

    def wait(self, timeout=None):
        self.waited = True
        return True


def test_gateway_housekeeping_calls_periodic_memory_trim(monkeypatch):
    import hermes_cli.mem_trim as mem_trim

    calls = []
    monkeypatch.setattr(
        mem_trim,
        "trim_memory",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)

    assert calls == [{"reason": "messaging gateway housekeeping"}]


class _NTickStopEvent:
    """Run exactly N housekeeping ticks, then signal stop."""

    def __init__(self, n):
        self._remaining = n
        self._waits = 0

    def is_set(self):
        return self._remaining <= 0

    def wait(self, timeout=None):
        self._waits += 1
        self._remaining -= 1
        return self.is_set()


def test_gateway_housekeeping_periodic_orphan_reap_fires_on_interval(monkeypatch):
    """The #60703 orphan desktop-serve reap runs on a bounded tick interval,
    not just at desktop boot — so an orphaned lock-owned backend is reaped
    within a fixed window even on a headless gateway with no desktop boot."""
    import hermes_cli.dashboard_procs as dashboard_procs

    reap_calls = []
    monkeypatch.setattr(
        dashboard_procs,
        "_reap_orphaned_desktop_local_serves",
        lambda **kwargs: reap_calls.append(kwargs) or {"killed": [1234], "matched": [1234], "failed": []},
    )

    # ORPHAN_REAP_EVERY == 5 ticks; run just enough ticks to cross the
    # interval (5 ticks) without a full second sweep (10 ticks).
    gateway_run._start_gateway_housekeeping(_NTickStopEvent(5), interval=0)

    assert len(reap_calls) == 1


def test_gateway_housekeeping_orphan_reap_not_called_before_interval(monkeypatch):
    """Before the interval elapses the sweep is not invoked."""
    import hermes_cli.dashboard_procs as dashboard_procs

    reap_calls = []
    monkeypatch.setattr(
        dashboard_procs,
        "_reap_orphaned_desktop_local_serves",
        lambda **kwargs: reap_calls.append(kwargs) or {"killed": [], "matched": [], "failed": []},
    )

    # 4 ticks < ORPHAN_REAP_EVERY (5) — no reap should fire.
    gateway_run._start_gateway_housekeeping(_NTickStopEvent(4), interval=0)

    assert reap_calls == []
