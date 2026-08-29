"""Tests for the browser owner-watchdog spawn wiring in tools/browser_tool.py.

The acceptance criterion for t_8a1037d1 is that a SIGKILL'd agent leaves no
surviving Chromium root. The mechanism is a detached owner-death watchdog
spawned from the browser tool (see tools/browser_owner_watchdog.py). These
tests verify the *wiring*: that the browser tool spawns the watchdog exactly
once, with the right args, on the first session. (The watchdog's actual
reap-on-SIGKILL behaviour is covered end-to-end in
test_browser_owner_watchdog.py.)
"""

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="browser owner watchdog is POSIX-only"
)


def _fresh_browser_tool():
    """Import browser_tool with a clean module-level watchdog flag so each test
    observes a fresh spawn decision."""
    import importlib
    import tools.browser_tool as bt
    importlib.reload(bt)
    bt._owner_watchdog_spawned = False
    return bt


class TestOwnerWatchdogSpawn:
    def test_spawns_watchdog_once_on_first_session(self, monkeypatch):
        bt = _fresh_browser_tool()

        spawned = []

        def fake_popen(cmd, **kwargs):
            spawned.append(cmd)
            # Pretend success; we don't need a real child here.
            import types
            p = types.SimpleNamespace(poll=lambda: None)
            return p

        monkeypatch.setattr(bt.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(os, "name", "posix")

        # Simulate two session creations (first spawns, second is a no-op).
        bt._start_browser_cleanup_thread()
        bt._start_browser_cleanup_thread()

        assert len(spawned) == 1, "watchdog must spawn exactly once per process"
        cmd = spawned[0]
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("browser_owner_watchdog.py")
        assert "--ppid" in cmd
        assert cmd[cmd.index("--ppid") + 1] == str(os.getpid())

    def test_does_not_spawn_on_windows(self, monkeypatch):
        bt = _fresh_browser_tool()

        spawned = []

        def fake_popen(cmd, **kwargs):
            spawned.append(cmd)
            import types
            return types.SimpleNamespace(poll=lambda: None)

        monkeypatch.setattr(bt.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(os, "name", "nt")

        bt._start_browser_cleanup_thread()
        assert len(spawned) == 0, "watchdog must not spawn on non-POSIX"
