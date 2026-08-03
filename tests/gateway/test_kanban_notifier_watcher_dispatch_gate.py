"""Activation contract for the independent notifier and dispatcher gates.

The notifier key is optional: omitted/null preserves the historical coupling
to dispatch_in_gateway. Explicit booleans cover all four independent states.
The dispatcher gate and singleton-lock boundary must not depend on the new key.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


_UNSET = object()


def _make_runner(with_adapter=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: MagicMock()} if with_adapter else {}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "default"
    runner._profile_adapters = {}
    return runner


def _fake_config(dispatch_in_gateway, notify_in_gateway=_UNSET):
    kanban = {"dispatch_in_gateway": dispatch_in_gateway}
    if notify_in_gateway is not _UNSET:
        kanban["notify_in_gateway"] = notify_in_gateway
    return {"kanban": kanban}


def _notifier_reaches_board_scan(config):
    runner = _make_runner(with_adapter=True)
    scans = []
    sleep_calls = []

    async def fake_sleep(_delay):
        sleep_calls.append(True)
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as kb

    with patch("hermes_cli.config.load_config", return_value=config):
        with patch.object(
            kb,
            "list_boards",
            side_effect=lambda *a, **kw: scans.append(True) or [],
        ):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                with patch("asyncio.to_thread", side_effect=fake_to_thread):
                    asyncio.run(runner._kanban_notifier_watcher())
    return bool(scans)


def _dispatcher_reaches_singleton_lock(config):
    runner = _make_runner()
    with patch("hermes_cli.config.load_config", return_value=config):
        with patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(None, "contended"),
        ) as acquire:
            asyncio.run(runner._kanban_dispatcher_watcher())
    return acquire.called


@pytest.mark.parametrize(
    ("dispatch_enabled", "notify_enabled"),
    [
        (False, False),
        (False, True),
        (True, False),
        (True, True),
    ],
)
def test_dispatch_notifier_truth_table(
    monkeypatch, dispatch_enabled, notify_enabled,
):
    """All four explicit watcher states are independently reachable."""
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    config = _fake_config(dispatch_enabled, notify_enabled)
    assert _notifier_reaches_board_scan(config) is notify_enabled
    assert _dispatcher_reaches_singleton_lock(config) is dispatch_enabled


@pytest.mark.parametrize("dispatch_enabled", [False, True])
@pytest.mark.parametrize("notify_value", [_UNSET, None])
def test_omitted_or_null_notifier_preserves_dispatch_coupling(
    monkeypatch, dispatch_enabled, notify_value,
):
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    config = _fake_config(dispatch_enabled, notify_value)
    assert _notifier_reaches_board_scan(config) is dispatch_enabled


def test_notifier_disabled_returns_before_db_or_sleep():
    """The explicit false gate exits before any polling side effect."""
    runner = _make_runner(with_adapter=True)
    config = _fake_config(True, False)
    with patch("hermes_cli.config.load_config", return_value=config):
        with patch("hermes_cli.kanban_db.connect") as connect:
            with patch("hermes_cli.kanban_db.list_boards") as list_boards:
                with patch("asyncio.sleep") as sleep:
                    asyncio.run(runner._kanban_notifier_watcher())
    connect.assert_not_called()
    list_boards.assert_not_called()
    sleep.assert_not_called()


def test_legacy_dispatch_env_disables_only_when_notifier_omitted(monkeypatch):
    """The old env escape hatch remains part of the compatibility fallback."""
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "false")
    assert _notifier_reaches_board_scan(_fake_config(True)) is False
    assert _notifier_reaches_board_scan(_fake_config(False, True)) is True


@pytest.mark.parametrize("notify_enabled", [False, True])
def test_notify_setting_does_not_change_dispatch_gate(monkeypatch, notify_enabled):
    """Dispatcher activation still depends only on dispatch_in_gateway."""
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    assert _dispatcher_reaches_singleton_lock(
        _fake_config(False, notify_enabled)
    ) is False
    assert _dispatcher_reaches_singleton_lock(
        _fake_config(True, notify_enabled)
    ) is True
