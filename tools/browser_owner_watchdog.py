#!/usr/bin/env python3
"""browser_owner_watchdog.py — detached owner-death supervisor for agent-browser.

Problem this fixes (kanban t_8a1037d1): the Hermes browser tool launches a
headless Chromium via the ``agent-browser`` CLI. That CLI double-forks a
detached daemon which spawns Chromium with ``--user-data-dir=/tmp/agent-browser-
chrome-<uuid>``. All of Hermes's own cleanup — atexit handlers, the background
inactivity/orphan-reap thread, ``cleanup_all_browsers`` — runs *inside the
agent process*. If the agent is killed hard (``SIGKILL``, an OS-level crash, a
force-quit), that code never runs, the daemon survives, and the Chromium root
is reparented to pid 1 / systemd --user: an orphan holding swap. t_9b49cd19
built an hourly external reaper to bound the damage; this watchdog closes the
gap at the source so the leak never accumulates in the first place.

Mechanism (mirrors ``tools/mcp_stdio_watchdog.py``): instead of relying on the
agent's own teardown, the browser tool spawns this tiny supervisor as a
detached child *of the agent*. Being a child, it survives the agent's SIGKILL
(it is reparented to init, not killed) and it can detect the owner's death by
watching its own ``getppid()``: the instant the original parent is gone,
``os.getppid()`` no longer equals the recorded original PPID. On owner death
the watchdog reaps every agent-browser daemon whose owning hermes PID is dead
plus its Chromium tree, removes the stale socket dirs and the
``/tmp/agent-browser-chrome-*`` profile dirs, then exits. It never touches a
browser still owned by a live agent (cross-process safe via ``owner_pid``).

Self-termination (so the watchdog cannot itself become a leak):
  1. Owner death  -> reap, then exit.
  2. Absolute lifetime cap (default 24h) -> hard exit regardless.

The watchdog does NOT exit while the owner is alive even when no /tmp socket
dirs remain: it is spawned once per agent process and guards the owner for its
whole lifetime, so every browser session in a long-lived gateway/CLI process is
covered. Exiting on empty dirs would leave later sessions unprotected
(t_8a1037d1 review, round 1).

Stdlib-only, POSIX-only (the spawn site gates on ``os.name == "posix"``), and
fast to start. It does NOT import the heavy ``tools.browser_tool`` module.

Usage (see the spawn site in ``tools/browser_tool.py``)::

    python3 -m tools.browser_owner_watchdog --ppid <original_parent_pid>

Env:
  BROWSER_OWNER_WATCHDOG_POLL_S   poll interval in seconds (default 2)
  BROWSER_OWNER_WATCHDOG_MAX_S    absolute lifetime cap in seconds (default 86400)
  BROWSER_OWNER_WATCHDOG_DRY_RUN  set to 1 to log without killing/removing
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import time
from pathlib import Path

_POLL_S = float(os.environ.get("BROWSER_OWNER_WATCHDOG_POLL_S", "2"))
_MAX_S = float(os.environ.get("BROWSER_OWNER_WATCHDOG_MAX_S", str(24 * 3600)))
_DRY_RUN = os.environ.get("BROWSER_OWNER_WATCHDOG_DRY_RUN") == "1"

UDD_PREFIX = "--user-data-dir=/tmp/agent-browser-chrome-"
TMP_GLOB = "agent-browser-chrome-*"


def _cmdline(pid: int) -> list[str]:
    """Argv tokens. Chromium collapses its argv into ONE space-joined blob, so
    split on whitespace too — matching NUL-separated tokens alone silently
    misses every Chromium process (t_9b49cd19 verified this)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    tokens: list[str] = []
    for chunk in raw.decode("utf-8", "replace").split("\0"):
        tokens.extend(t for t in chunk.split() if t)
    return tokens


def _ppid_of(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return -1


def _is_systemd_user(pid: int) -> bool:
    argv = _cmdline(pid)
    return bool(argv) and "systemd" in argv[0] and "--user" in argv


def _alive(pid: int) -> bool:
    """True if the PID is a live, non-zombie process.

    We treat zombies (state 'Z') as NOT alive: a zombie holds no memory/swap
    (its resources are already reclaimed) and is only a placeholder awaiting
    reap by its parent — in production that parent is init/systemd after the
    owning agent dies, so it is reaped promptly. Killing a zombie is a no-op;
    the goal is that nothing *consumes resources*, which a zombie does not.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # comm may contain spaces/parens; state is the first field after the
        # final ')' of comm.
        state = stat[stat.rfind(")") + 2:].split()[0]
        return state != "Z"
    except (OSError, IndexError, ValueError):
        # /proc/pid gone (or unreadable) -> not alive.
        return False


def _owner_is_gone(original_ppid: int) -> bool:
    """True once the original parent is no longer our parent (reparented to
    init) or has exited outright."""
    try:
        if os.getppid() != original_ppid:
            return True
    except OSError:
        return True
    return not _alive(original_ppid)


def _tree_kill(pid: int) -> None:
    """SIGTERM then SIGKILL the process and, best-effort, its descendants.

    We cannot use a process group here — the agent-browser daemon detached via
    setsid/double-fork, so it is not in our group. Walk /proc for descendants
    and kill leaf-first so children are signalled even if a parent exits fast.
    """
    try:
        children = sorted(
            (int(e.name) for e in Path("/proc").iterdir() if e.name.isdigit()),
            key=lambda p: -_proc_depth(p),
        )
    except OSError:
        children = []
    # Send SIGTERM to the target and every live descendant.
    targets = [pid] + [c for c in children if _is_descendant(c, pid)]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for t in targets:
            if _alive(t):
                try:
                    os.kill(t, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        time.sleep(0.5)
        if not any(_alive(t) for t in targets):
            break


def _proc_depth(pid: int) -> int:
    depth = 0
    seen = 0
    cur = pid
    while cur > 1 and seen < 64:
        nxt = _ppid_of(cur)
        if nxt == cur or nxt <= 0:
            break
        cur = nxt
        depth += 1
        seen += 1
    return depth


def _is_descendant(pid: int, ancestor: int) -> bool:
    cur = _ppid_of(pid)
    seen = 0
    while cur > 1 and seen < 64:
        if cur == ancestor:
            return True
        nxt = _ppid_of(cur)
        if nxt == cur or nxt <= 0:
            break
        cur = nxt
        seen += 1
    return False


def _socket_dirs() -> list[str]:
    tmpdir = "/tmp"
    out: list[str] = []
    for pattern in ("agent-browser-h_*", "agent-browser-cdp_*",
                    "agent-browser-hermes_*", "agent-browser-rp_*"):
        out.extend(str(p) for p in Path(tmpdir).glob(pattern))
    return sorted(set(out))


def _daemon_of_socket_dir(socket_dir: str) -> int | None:
    """Read the daemon PID from ``<session>.pid`` inside a socket dir."""
    try:
        for entry in Path(socket_dir).iterdir():
            if entry.name.endswith(".pid"):
                return int(entry.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return None


def _owner_of_socket_dir(socket_dir: str) -> int | None:
    """Read the owning hermes PID from ``<session>.owner_pid`` if present."""
    try:
        for entry in Path(socket_dir).iterdir():
            if entry.name.endswith(".owner_pid"):
                return int(entry.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return None


def _reap_owner_browsers() -> None:
    """Reap agent-browser daemons + Chromium whose owning hermes PID is dead,
    and remove stale /tmp socket + profile dirs. Never touches a browser whose
    owner is alive (cross-process safe).

    Two mechanisms, both keyed to owner death (the watchdog only runs this on
    owner death, so no age-based safety margin is needed):
      1. Daemons: read each ``agent-browser-*`` socket dir's ``owner_pid``; if
         that hermes PID is dead (or missing and untrackable), tree-kill the
         daemon (which carries its Chromium children in production) and remove
         the socket dir.
      2. Chromium roots: any Chromium root in /proc carrying
         ``--user-data-dir=/tmp/agent-browser-chrome-*`` that is orphaned
         (PPid 1 / systemd --user — i.e. its launching agent is gone) is
         tree-killed and its profile dir removed. Mirrors t_9b49cd19's proven
         reaper criterion, minus the age gate (we only fire on owner death).
    """
    live_owner_pids: set[int] = set()
    reap_daemons: list[tuple[int, str]] = []  # (daemon_pid, socket_dir)
    stale_dirs: list[str] = []

    for socket_dir in _socket_dirs():
        owner = _owner_of_socket_dir(socket_dir)
        daemon = _daemon_of_socket_dir(socket_dir)
        if owner is not None and _alive(owner):
            live_owner_pids.add(owner)
            continue
        # Owner dead, or missing owner_pid.
        if daemon is not None and _alive(daemon):
            reap_daemons.append((daemon, socket_dir))
        else:
            stale_dirs.append(socket_dir)

    # Reap Chromium roots that are orphaned (PPid 1 / systemd --user) and whose
    # profile dir is not held by a live owner. We compute the set of profile
    # dirs still referenced by any live process before killing anything.
    live_profile_dirs: set[str] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        for arg in _cmdline(int(entry.name)):
            if arg.startswith(UDD_PREFIX):
                live_profile_dirs.add(arg.split("=", 1)[1])

    orphan_chromium: list[tuple[int, str]] = []  # (pid, profile_dir)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        argv = _cmdline(pid)
        udd = next((a for a in argv if a.startswith(UDD_PREFIX)), None)
        if not udd:
            continue
        if any(a.startswith("--type=") for a in argv):
            continue  # child process; dies with its root
        parent = _ppid_of(pid)
        if parent != 1 and not _is_systemd_user(parent):
            continue  # still owned by a live daemon/agent
        profile_dir = udd.split("=", 1)[1]
        if profile_dir in live_profile_dirs:
            continue  # some live process still references it — leave alone
        orphan_chromium.append((pid, profile_dir))

    reaped = 0
    for daemon_pid, socket_dir in reap_daemons:
        # Identity guard: never tree-kill an arbitrary PID from a world-writable
        # temp dir; confirm it is actually bound to this agent-browser session.
        argv = " ".join(_cmdline(daemon_pid)).lower()
        if "agent-browser" not in argv and "agent-browser" not in (
                os.path.basename(socket_dir) or ""):
            stale_dirs.append(socket_dir)
            continue
        if not _DRY_RUN:
            _tree_kill(daemon_pid)
        reaped += 1
        if not _DRY_RUN:
            shutil.rmtree(socket_dir, ignore_errors=True)

    for chrom_pid, profile_dir in orphan_chromium:
        if not _DRY_RUN:
            _tree_kill(chrom_pid)
            shutil.rmtree(profile_dir, ignore_errors=True)
        reaped += 1

    for socket_dir in stale_dirs:
        if not _DRY_RUN:
            shutil.rmtree(socket_dir, ignore_errors=True)

    # Final stale-profile-dir pass: after the daemon/chromium kills, recompute
    # the live set from *non-zombie* processes and remove any /tmp profile dir
    # no longer referenced by a live process. Mirrors t_9b49cd19's keep-set.
    _cleanup_orphan_profile_dirs()

    if reaped or stale_dirs:
        print(
            f"browser_owner_watchdog: owner gone -> reaped {reaped} agent-browser "
            f"daemon/chromium process(es), removed {len(stale_dirs)} stale socket dir(s)",
            flush=True,
        )


def _cleanup_orphan_profile_dirs() -> None:
    """Remove /tmp/agent-browser-chrome-* dirs not referenced by any live
    (non-zombie) process. The keep-set is every profile dir still named in a
    live process cmdline; anything else is stale and safe to remove."""
    live_dirs: set[str] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if not _alive(pid):
            continue
        for arg in _cmdline(pid):
            if arg.startswith(UDD_PREFIX):
                live_dirs.add(arg.split("=", 1)[1])
    for d in Path("/tmp").glob(TMP_GLOB):
        if str(d) in live_dirs:
            continue
        if not _DRY_RUN:
            shutil.rmtree(d, ignore_errors=True)


def _run(original_ppid: int) -> int:
    """Watch the owner for its WHOLE lifetime.

    We deliberately do NOT self-terminate when the /tmp socket dirs are empty
    while the owner is still alive. The browser tool spawns (and keeps) one
    watchdog per live agent process, so if it exited on empty dirs it would
    leave later sessions in a long-lived gateway/CLI process with NO watchdog —
    and a SIGKILL during that session would leak orphan Chromium exactly as
    before (t_8a1037d1 review, round 1). The watchdog is stdlib-only and sleeps
    2s per poll, so a single instance guarding the owner for its whole life is
    trivially cheap. Exit only on owner death (after reaping) or the absolute
    ``_MAX_S`` lifetime cap (a safety net so a wedged watchdog can never
    accumulate).
    """
    start = time.time()
    while True:
        if _owner_is_gone(original_ppid):
            _reap_owner_browsers()
            return 0

        if time.time() - start >= _MAX_S:
            return 0

        time.sleep(_POLL_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detached owner-death watchdog for agent-browser sessions.",
    )
    parser.add_argument("--ppid", type=int, required=True)
    args = parser.parse_args(argv)
    return _run(args.ppid)


if __name__ == "__main__":
    sys.exit(main())
