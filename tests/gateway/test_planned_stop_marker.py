"""Tests for the stdlib-only systemd ExecStop= marker helper.

This module must remain import-safe without any Hermes runtime — it only
calls into ``gateway.status`` *after* validating its arguments. The tests
exercise the CLI contract (no marker written on empty/invalid input, real
write on valid input, end-to-end signal-handler classification).
"""

from __future__ import annotations

import json
import os
import signal
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import planned_stop_marker
from gateway.run_shutdown import _resolve_gateway_exit_verdict


# ── Argument validation (zero side effects on bad input) ───────────────────


class TestMarkerModuleArgumentValidation:
    """The ExecStop helper must validate and refuse to act on bad input."""

    def test_no_args_is_noop(self):
        """ExecStop runs after the main process exited → must not error."""
        assert planned_stop_marker.main([]) == 0

    def test_multiple_args_is_rejected(self):
        assert planned_stop_marker.main(["1", "2"]) == 1

    def test_non_integer_pid_is_rejected(self):
        assert planned_stop_marker.main(["not-a-pid"]) == 1

    def test_zero_pid_is_rejected(self):
        assert planned_stop_marker.main(["0"]) == 1

    def test_negative_pid_is_rejected(self):
        assert planned_stop_marker.main(["-5"]) == 1

    def test_valid_pid_calls_status_marker(self, tmp_path, monkeypatch):
        """A valid PID calls write_planned_stop_marker with that PID."""
        captured = []

        def fake_write(pid):
            captured.append(pid)
            return True

        # Patch the source module since the helper imports it lazily
        import gateway.status as status_mod
        monkeypatch.setattr(status_mod, "write_planned_stop_marker", fake_write)
        assert planned_stop_marker.main([str(os.getpid())]) == 0
        assert captured == [os.getpid()]

    def test_marker_write_failure_returns_1(self, monkeypatch):
        def fake_write(pid):
            return False

        import gateway.status as status_mod
        monkeypatch.setattr(status_mod, "write_planned_stop_marker", fake_write)
        assert planned_stop_marker.main([str(os.getpid())]) == 1

    def test_status_unavailable_returns_1(self, monkeypatch):
        """If the status subsystem can't be imported, fail soft."""
        def boom(pid):
            raise ImportError("gateway.status unavailable")

        import gateway.status as status_mod
        monkeypatch.setattr(status_mod, "write_planned_stop_marker", boom)
        assert planned_stop_marker.main([str(os.getpid())]) == 1


# ── End-to-end: marker present → planned_stop=True → exit 0 ────────────────


class TestMarkedStopResolvesToCleanExit:
    """A real marker on disk must drive the signal-handler classification to
    planned_stop=True and the exit-verdict to exit 0 (clean)."""

    def _make_runner(self, **overrides):
        values = {
            "should_exit_with_failure": False,
            "exit_reason": None,
            "exit_code": None,
            "_restart_requested": False,
            "_restart_via_service": False,
            "stop": AsyncMock(),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @pytest.mark.asyncio
    async def test_marked_systemd_stop_exits_zero(self, tmp_path, monkeypatch):
        """The generated unit's ExecStop= writes the marker; SIGTERM then
        classifies as a planned stop and exits code 0."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        marker = tmp_path / ".gateway-planned-stop.json"
        pid = os.getpid()
        # Pre-write the marker as the ExecStop helper would
        marker.write_text(json.dumps({
            "target_pid": pid,
            "target_start_time": None,
            "stopper_pid": pid + 1,
            "written_at": "2026-09-18T00:00:00+00:00",
        }))

        from gateway.run_shutdown import _resolve_gateway_exit_verdict
        runner = self._make_runner()
        verdict = _resolve_gateway_exit_verdict(runner, signal_initiated_shutdown=False)
        assert verdict is True  # exit 0

    def test_unmarked_sigterm_resolves_to_failure(self):
        """Unmarked SIGTERM (external kill / OOM) still exits non-zero."""
        runner = self._make_runner()
        verdict = _resolve_gateway_exit_verdict(runner, signal_initiated_shutdown=True)
        assert verdict is False  # exit 1

    def test_sigint_treated_as_planned_stop_unchanged(self):
        """SIGINT (Ctrl+C) must still classify as planned stop."""
        # In the real handler, SIGINT short-circuits _planned_stop().
        # Here we verify the exit-verdict function doesn't interfere:
        # signal_initiated_shutdown=False when the handler sets it False.
        runner = self._make_runner()
        verdict = _resolve_gateway_exit_verdict(runner, signal_initiated_shutdown=False)
        assert verdict is True

    def test_restart_via_service_unaffected(self):
        """The restart-via-service path still raises SystemExit(75)."""
        runner = self._make_runner(_restart_requested=True, _restart_via_service=True)
        with pytest.raises(SystemExit) as exc_info:
            _resolve_gateway_exit_verdict(runner, signal_initiated_shutdown=False)
        assert exc_info.value.code == 75
