"""Fleet-dispatch starvation alert delivery (t_2a652a17).

Jarvis acceptance (2026-08-03 SEAT DECISION): the alert fires on a synthetic
spawned=0 / ready>0 condition AND is DELIVERED (not merely logged — an
undelivered alert is the failure mode this card exists to close).

These tests drive the REAL gateway embedded dispatcher loop
(``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher``) with a synthetic
board whose ``dispatch_once`` returns zero spawns while the ready probe
reports work available, then assert the #critical-alerts delivery helper was
called — not just that a warning was logged.

Three scenarios:
1. Block-gate starvation (blocked_claim_attempts populated) → delivered alert
   names the exact card ids + the ``hermes kanban unblock`` fix.
2. Plain stuck (no block gate) → delivered profile-health alert.
3. Healthy tick (something spawned) → NO alert.
Plus a unit test that the helper forwards to the established
``cron.scheduler._alert_critical_alerts`` channel and a fail-safe test that a
broken delivery path degrades to ``logger.critical`` instead of raising.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _deliver_fleet_dispatch_alert,
)
from gateway.run import GatewayRunner
from hermes_cli.kanban_db import DispatchResult


def _make_runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_dispatcher_lock_handle = None
    return runner


def _fake_config(dispatch_in_gateway: bool = True) -> dict:
    return {
        "kanban": {
            "dispatch_in_gateway": dispatch_in_gateway,
            "auto_decompose": False,  # keep the test hermetic
            "dispatch_interval_seconds": 1,
        }
    }


def _run_dispatcher(
    runner: GatewayRunner,
    dispatch_result: DispatchResult,
    ready: bool = True,
    ticks_before_stop: int = 9,
) -> None:
    """Run the embedded dispatcher loop against a synthetic board.

    Patches the kanban_db seams so no real board DB is touched: boards are a
    single fake slug, dispatch_once returns the injected result, the ready
    probe returns ``ready``, the singleton lock is "unavailable" (config-only
    control — never contends with a live gateway), and asyncio.sleep stops
    the loop after ``ticks_before_stop`` calls (boot sleep + per-tick sleeps).
    """

    async def _fake_sleep(delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= ticks_before_stop:
            runner._running = False

    async def _fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    sleep_calls = 0

    fake_conn = MagicMock()

    with patch("hermes_cli.config.load_config", return_value=_fake_config()):
        with patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(None, "unavailable"),
        ):
            with patch.object(_kb, "list_boards", return_value=[{"slug": "test"}]):
                with patch.object(_kb, "connect", return_value=fake_conn):
                    with patch.object(_kb, "dispatch_once", return_value=dispatch_result):
                        with patch.object(_kb, "has_spawnable_ready", return_value=ready):
                            with patch.object(_kb, "has_spawnable_review", return_value=False):
                                with patch.object(_kb, "reap_worker_zombies", return_value=[]):
                                    with patch("asyncio.sleep", side_effect=_fake_sleep):
                                        with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                                            asyncio.run(runner._kanban_dispatcher_watcher())


def test_block_gate_starvation_delivers_alert_naming_cards() -> None:
    """HEALTH_WINDOW consecutive spawned=0 ticks with blocked_claim_attempts
    must DELIVER a #critical-alerts ping naming the refused cards."""
    runner = _make_runner()
    delivered: list[str] = []

    result = DispatchResult(
        spawned=[],
        blocked_claim_attempts=["t_forced1"],
        skipped_block_gate=["t_forced1"],
    )

    with patch(
        "gateway.kanban_watchers._deliver_fleet_dispatch_alert",
        side_effect=delivered.append,
    ):
        _run_dispatcher(runner, result)

    assert delivered, "expected a DELIVERED fleet-dispatch alert"
    msg = delivered[0]
    assert "kanban dispatcher stuck" in msg
    assert "t_forced1" in msg, f"alert must name the starved card: {msg}"
    assert "hermes kanban unblock" in msg, f"alert must carry the unblock fix: {msg}"


def test_plain_stuck_delivers_profile_health_alert() -> None:
    """spawned=0 while ready>0 WITHOUT block-gate involvement must still
    DELIVER (profile-health diagnostic), not just log."""
    runner = _make_runner()
    delivered: list[str] = []

    result = DispatchResult(spawned=[], blocked_claim_attempts=[])

    with patch(
        "gateway.kanban_watchers._deliver_fleet_dispatch_alert",
        side_effect=delivered.append,
    ):
        _run_dispatcher(runner, result)

    assert delivered, "expected a DELIVERED fleet-dispatch alert"
    msg = delivered[0]
    assert "kanban dispatcher stuck" in msg
    assert "profile health" in msg, f"expected profile-health diagnostic: {msg}"


def test_healthy_tick_does_not_alert() -> None:
    """When the dispatcher spawns a worker, no starvation alert may fire."""
    runner = _make_runner()
    delivered: list[str] = []

    result = DispatchResult(spawned=[("t1", "profile-a", "/tmp/w")])

    with patch(
        "gateway.kanban_watchers._deliver_fleet_dispatch_alert",
        side_effect=delivered.append,
    ):
        _run_dispatcher(runner, result)

    assert delivered == [], "a healthy tick must not deliver a starvation alert"


def test_ready_false_no_alert() -> None:
    """A correctly idle board (no spawnable ready work) must not alert."""
    runner = _make_runner()
    delivered: list[str] = []

    result = DispatchResult(spawned=[], blocked_claim_attempts=[])

    with patch(
        "gateway.kanban_watchers._deliver_fleet_dispatch_alert",
        side_effect=delivered.append,
    ):
        _run_dispatcher(runner, result, ready=False)

    assert delivered == [], "correctly-idle board must not deliver an alert"


def test_deliver_helper_forwards_to_critical_alerts_channel(monkeypatch) -> None:
    """The delivery helper must forward to the established
    cron.scheduler._alert_critical_alerts (Discord #critical-alerts) — the
    same loud channel the cron dead-pin guard uses."""
    import cron.scheduler as sched_mod

    forwarded: list[str] = []
    monkeypatch.setattr(sched_mod, "_alert_critical_alerts", forwarded.append)

    _deliver_fleet_dispatch_alert("kanban dispatcher stuck: test message")

    assert forwarded, "helper must forward to cron.scheduler._alert_critical_alerts"
    assert forwarded[0] == "kanban dispatcher stuck: test message"


def test_deliver_helper_fails_safe_to_logger_critical(monkeypatch, caplog) -> None:
    """A broken delivery path must degrade to logger.critical, never raise —
    a failed alert must not crash the dispatcher tick."""

    def _boom(message: str) -> None:
        raise RuntimeError("delivery exploded")

    import cron.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_alert_critical_alerts", _boom)

    with caplog.at_level(logging.CRITICAL, logger="gateway.run"):
        _deliver_fleet_dispatch_alert("kanban dispatcher stuck: boom")

    assert any(
        "fleet-dispatch alert delivery failed" in rec.getMessage()
        for rec in caplog.records
    ), "broken delivery must degrade to logger.critical"
