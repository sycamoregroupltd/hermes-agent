"""Tests for the browser owner-watchdog spawn wiring in tools/browser_tool.py.

The acceptance criterion for t_8a1037d1 is that a SIGKILL'd agent leaves no
surviving Chromium root. The mechanism is a detached owner-death watchdog
spawned from the browser tool (see tools/browser_owner_watchdog.py). These
tests verify the *wiring*: that the browser tool spawns the watchdog once, with
the right args, on the first session, and that it is idempotent per-live-
watchdog — if the watchdog dies for any reason (24h cap, crash), a later
session respawns a fresh one so a long-lived gateway/CLI process never runs a
browser session unprotected. (The watchdog's actual reap-on-SIGKILL behaviour
is covered end-to-end in test_browser_owner_watchdog.py.)
"""

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="browser owner watchdog is POSIX-only"
)


def _fresh_browser_tool():
    """Import browser_tool with a clean module-level watchdog state so each test
    observes a fresh spawn decision."""
    import importlib
    import tools.browser_tool as bt
    importlib.reload(bt)
    bt._owner_watchdog_proc = None
    return bt


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


class TestOwnerWatchdogSpawn:
    def test_spawns_watchdog_once_while_alive(self, monkeypatch):
        bt = _fresh_browser_tool()

        spawned = []

        def fake_popen(cmd, **kwargs):
            spawned.append(cmd)
            # A live watchdog, so poll() returns None on every call.
            return _FakeProc(alive=True)

        monkeypatch.setattr(bt.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(os, "name", "posix")

        # Simulate three session creations. The first spawns; the next two are
        # no-ops while the watchdog stays alive.
        bt._start_browser_cleanup_thread()
        bt._start_browser_cleanup_thread()
        bt._start_browser_cleanup_thread()

        assert len(spawned) == 1, "watchdog must spawn once while it stays alive"
        cmd = spawned[0]
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("browser_owner_watchdog.py")
        assert "--ppid" in cmd
        assert cmd[cmd.index("--ppid") + 1] == str(os.getpid())
        # The live watchdog proc is retained so poll() can observe it later.
        assert bt._owner_watchdog_proc is not None

    def test_respawns_after_watchdog_dies(self, monkeypatch):
        """A later session must respawn the watchdog once the previous one dies.

        This is the multi-session guard: in a long-lived gateway/CLI process the
        watchdog could exit via its 24h hard cap (or a crash) while the agent
        stays alive. If the spawn were one-shot-per-process-lifetime, a session
        opened after that would have NO watchdog and a SIGKILL would leak
        Chromium exactly as before. Idempotency must be per-live-watchdog.
        """
        bt = _fresh_browser_tool()

        spawned = []

        def fake_popen(cmd, **kwargs):
            spawned.append(cmd)
            # Every spawned watchdog starts alive.
            return _FakeProc(alive=True)

        monkeypatch.setattr(bt.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(os, "name", "posix")

        bt._start_browser_cleanup_thread()  # session 1: spawns watchdog #1
        bt._start_browser_cleanup_thread()  # watchdog #1 alive -> no-op
        bt._owner_watchdog_proc = _FakeProc(alive=False)  # watchdog #1 dies
        bt._start_browser_cleanup_thread()  # session 2: respawns watchdog #2

        assert len(spawned) == 2, "a dead watchdog must be respawned"
        assert bt._owner_watchdog_proc.poll() is None, \
            "new watchdog must be alive (retained for poll())"

    def test_does_not_spawn_on_windows(self, monkeypatch):
        bt = _fresh_browser_tool()

        spawned = []

        def fake_popen(cmd, **kwargs):
            spawned.append(cmd)
            return _FakeProc(alive=True)

        monkeypatch.setattr(bt.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(os, "name", "nt")

        bt._start_browser_cleanup_thread()
        assert len(spawned) == 0, "watchdog must not spawn on non-POSIX"
