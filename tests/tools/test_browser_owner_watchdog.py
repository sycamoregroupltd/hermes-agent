"""Tests for tools/browser_owner_watchdog.py — detached owner-death supervisor.

The acceptance criterion for t_8a1037d1 is: a browser-tool session killed
abruptly (SIGKILL the agent) leaves NO surviving Chromium root. All in-process
cleanup (atexit, background thread) dies with the agent on SIGKILL, so the fix
is a detached watchdog that survives the owner's death and reaps the
agent-browser daemon + Chromium tree plus the stale /tmp profile dirs.

These tests exercise the watchdog for real on POSIX: they spawn actual child
processes (a fake "owner", a fake "agent-browser daemon" which itself spawns a
fake "Chromium" child carrying --user-data-dir=/tmp/agent-browser-chrome-<uuid>)
and SIGKILL the owner, then assert nothing survives.

NOTE: the watchdog globs the REAL /tmp (matching production), so these tests
create uniquely-named agent-browser-* / agent-browser-chrome-* dirs in /tmp and
remove them on teardown. They never touch real browser sessions (unique uuids).
"""

import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

# These tests genuinely deliver real SIGKILLs: the acceptance criterion for
# t_8a1037d1 is that killing the agent (SIGKILL) leaves no surviving Chromium.
# The watchdog under test is a real subprocess that must tree-kill real child
# processes across process groups — this is not something mocks can prove and
# the whole point of the test is to exercise real signal delivery. The
# conftest live-system guard would otherwise block these real os.kill calls.
pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="browser owner watchdog is POSIX-only"),
    pytest.mark.live_system_guard_bypass,
]

WATCHDOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools", "browser_owner_watchdog.py",
)

# A fake "agent-browser daemon": spawns a child that looks like a Chromium root
# (real argv carrying --user-data-dir), then sleeps. Mirrors how the real
# agent-browser daemon owns its Chromium tree. Its own argv includes the
# "agent-browser" token so the watchdog's identity guard accepts it.
_DAEMON_SRC = r"""
import os, subprocess, sys, time
udd = sys.argv[2]
chromium = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)",
     "chromium", "--no-sandbox", udd, "--headless=new"],
    start_new_session=True,
)
print(chromium.pid, flush=True)
time.sleep(600)
"""


def _spawn_sleeper():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class FakeSession:
    """Build a fake /tmp agent-browser socket dir + a daemon that owns a
    Chromium child, exactly as the real browser tool's agent-browser does."""

    def __init__(self, owner_pid):
        self.session_name = f"h_{uuid.uuid4().hex[:10]}"
        self.socket_dir = f"/tmp/agent-browser-{self.session_name}"
        os.makedirs(self.socket_dir, exist_ok=True)
        # Chromium profile dir in the real /tmp (matches production glob).
        self.profile_dir = f"/tmp/agent-browser-chrome-{uuid.uuid4().hex[:12]}"
        os.makedirs(self.profile_dir, exist_ok=True)

        udd = f"--user-data-dir={self.profile_dir}"

        # Fake agent-browser daemon: a real live child that spawns the Chromium.
        # argv[1] = "agent-browser" so the watchdog's identity guard accepts it.
        self.daemon = subprocess.Popen(
            [sys.executable, "-c", _DAEMON_SRC, "agent-browser", udd],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # Read the chromium PID the daemon printed (its child).
        chrom_line = self.daemon.stdout.readline().decode().strip()
        self.chromium_pid = int(chrom_line)

        # Find the chromium process object for poll()/wait().
        self.chromium = self._proc(self.chromium_pid)

        with open(os.path.join(self.socket_dir, f"{self.session_name}.pid"), "w") as f:
            f.write(str(self.daemon.pid))
        with open(os.path.join(self.socket_dir, f"{self.session_name}.owner_pid"), "w") as f:
            f.write(str(owner_pid))

    @staticmethod
    def _proc(pid):
        import psutil
        return psutil.Process(pid)

    def assert_all_alive(self):
        assert self.daemon.poll() is None, "daemon should still be alive"
        assert self.chromium_pid and _pid_alive(self.chromium_pid), \
            "chromium should still be alive"

    def cleanup(self):
        for pid in (self.daemon.pid, self.chromium_pid):
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            self.daemon.wait(timeout=5)
        except Exception:
            pass
        import shutil
        for d in (self.socket_dir, self.profile_dir):
            shutil.rmtree(d, ignore_errors=True)


def _pid_alive(pid):
    """True if PID is live and non-zombie (matches watchdog _alive)."""
    try:
        stat = open(f"/proc/{pid}/stat").read()
        state = stat[stat.rfind(")") + 2:].split()[0]
        return state != "Z"
    except (OSError, IndexError, ValueError):
        return False


@pytest.fixture
def fake_session_factory():
    created = []

    def _make(owner_pid):
        fs = FakeSession(owner_pid)
        created.append(fs)
        return fs

    yield _make
    for fs in created:
        fs.cleanup()


def _watchdog_env():
    env = dict(os.environ)
    env["BROWSER_OWNER_WATCHDOG_POLL_S"] = "0.2"
    env["BROWSER_OWNER_WATCHDOG_GRACE_S"] = "0.5"
    return env


def _run_watchdog(ppid, timeout=30):
    return subprocess.run(
        [sys.executable, WATCHDOG, "--ppid", str(ppid)],
        env=_watchdog_env(), capture_output=True, text=True, timeout=timeout,
    )


def test_reaps_owner_browsers_on_sigkill(fake_session_factory):
    """SIGKILL the owner -> watchdog reaps daemon + Chromium, removes dirs."""
    owner = _spawn_sleeper()
    time.sleep(0.3)
    fs = fake_session_factory(owner.pid)
    fs.assert_all_alive()

    # SIGKILL the owner (hard kill — the acceptance scenario).
    os.kill(owner.pid, signal.SIGKILL)
    owner.wait()

    result = _run_watchdog(owner.pid)
    assert result.returncode == 0

    # Nothing survives: the reaper must have tree-killed daemon + Chromium.
    assert not _pid_alive(fs.daemon.pid), "agent-browser daemon survived owner SIGKILL"
    assert not _pid_alive(fs.chromium_pid), "Chromium root survived owner SIGKILL"
    # Stale dirs removed.
    assert not os.path.exists(fs.socket_dir), "stale socket dir not removed"
    assert not os.path.exists(fs.profile_dir), "stale profile dir not removed"


def test_leaves_owned_browser_alone(fake_session_factory):
    """A browser whose owner is STILL ALIVE must not be reaped."""
    owner = _spawn_sleeper()
    time.sleep(0.3)
    fs = fake_session_factory(owner.pid)
    fs.assert_all_alive()

    # Point the watchdog at an already-dead ppid so it runs its reap pass, but
    # the session's owner is STILL ALIVE -> the reap must no-op.
    dead = _spawn_sleeper()
    os.kill(dead.pid, signal.SIGKILL)
    dead.wait()
    result = _run_watchdog(dead.pid)
    assert result.returncode == 0
    fs.assert_all_alive()
    assert os.path.exists(fs.socket_dir)
    assert os.path.exists(fs.profile_dir)

    owner.kill()
    owner.wait()


def test_noop_when_no_sessions():
    """No browser sessions -> watchdog exits quietly without error."""
    dead = _spawn_sleeper()
    os.kill(dead.pid, signal.SIGKILL)
    dead.wait()
    result = _run_watchdog(dead.pid)
    assert result.returncode == 0
