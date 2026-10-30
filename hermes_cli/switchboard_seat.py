"""Session-bus seat lifecycle for Kanban workers (Switchboard P1).

Frank's standing rule (voice, 2026-10-23): every agent holds a switchboard seat for
exactly as long as it runs — enrol at start, deregister at finish. A Kanban worker is a
fire-and-forget ``hermes chat -q`` subprocess, so the dispatcher has to own that seat for
it: ``_default_spawn`` starts one **supervisor** process per worker, and the supervisor is
what keeps the seat honest.

What the supervisor does (and nothing else):

* ``busctl enrol --listener supervised --doorbell <file>`` — the seat carries a real
  listener (this process), a real doorbell handle (a file it watches) and the worker's
  pid, so ``busctl`` admits it ``active`` instead of ``degraded``.
* heartbeats the seat while the worker pid is alive. ``recipient_is_active`` needs a
  heartbeat inside ``SESSION_BUS_ACTIVE_FRESH_SECONDS`` (1200 s default), so a
  ``HERMES_SWITCHBOARD_HB_SECONDS`` of 120 s is 10x headroom and a long run's seat never
  goes stale.
* drains the seat's unread mail into ``<state>/inbox.ndjson``. It never acks and never
  advances the read marker: an ack means an agent handled the message, and no automated
  process can assert that for the model. Copy-not-consume is what makes ``--listener
  supervised`` true — mid-run injection into the model is deliberately NOT part of P1.
* closes the seat when the worker dies, when the run ends (``task_runs.ended_at``, which
  every reaper path sets), or when another process closed it first (busctl rc 4 =
  heartbeat-after-close, a T10 violation the supervisor must stop, not retry).
* never closes a seat a LATER run of the same card holds: state is per run
  (``<root>/<seat>/run-<id>``) and the seat's ``owner`` file names the run holding it.

Nothing here may ever fail a spawn: every entry point returns a value and swallows
``OSError`` / timeout / malformed output. The seat is observability, not admission
control, and a missing bus must never cost the fleet a worker.

Naming matches the interim ``kanban_bus_bridge.py`` exactly, so the two can never mint two
seats for one run: seat ``hermes-<profile>-<task>``, card ref ``<board>/<task>#<run>``.
Kill switch: ``HERMES_SWITCHBOARD_SEAT=0|off|no|false|disabled`` (checked at spawn).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

#: Env var carrying the per-card seat name into the worker process itself.
SEAT_ENV_VAR = "SWITCHBOARD_SEAT"
#: Test/ops override for the busctl client path (a ``*.py`` file is run with a python).
BUSCTL_ENV_VAR = "HERMES_SWITCHBOARD_BUSCTL"
#: Interpreter used for the supervisor and for a ``*.py`` busctl.
BUS_PYTHON_ENV_VAR = "HERMES_SWITCHBOARD_PYTHON"
#: Spawn-time kill switch; see module docstring.
ENABLE_ENV_VAR = "HERMES_SWITCHBOARD_SEAT"
#: Root for per-seat state dirs (doorbell, inbox spool, stop flag).
STATE_DIR_ENV_VAR = "HERMES_SWITCHBOARD_STATE_DIR"
#: Bus DB path (read-only self-check; the real bus is the default).
BUS_DB_ENV_VAR = "HERMES_SWITCHBOARD_BUS_DB"
HEARTBEAT_ENV_VAR = "HERMES_SWITCHBOARD_HB_SECONDS"
TIMEOUT_ENV_VAR = "HERMES_SWITCHBOARD_TIMEOUT"

DEFAULT_BUSCTL = "/home/frank/.hermes/session-bus/busctl.py"
DEFAULT_BUS_DB = "/home/frank/.hermes/session-bus/bus.db"
#: The listener value that marks a dispatcher-supervised seat (see busctl enrol).
SUPERVISED_LISTENER = "supervised"
DEFAULT_BUS_PYTHON = "/usr/bin/python3"
#: 10x inside the 1200 s freshness window ``recipient_is_active`` enforces.
DEFAULT_HEARTBEAT_SECONDS = 120.0
DEFAULT_CALL_TIMEOUT = 8.0
#: busctl rc for a heartbeat on a closed seat (T10 protocol violation).
RC_HEARTBEAT_AFTER_CLOSE = 4
#: enrol/heartbeat rc when admission rows are missing: recorded, seat stays degraded.
RC_DEGRADED = 3

DISABLED_VALUES = frozenset({"0", "off", "no", "false", "disabled", "none"})
_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_SLEEP_SLICES = 8
DOORBELL_NAME = "doorbell"
_DOORBELL_NAME = DOORBELL_NAME
_STOP_NAME = "stop"
_INBOX_NAME = "inbox.ndjson"
_SUPERVISOR_PID_NAME = "supervisor.pid"
#: Seat-level file naming the run dir that currently holds the seat (see ``owns_seat``).
_OWNER_NAME = "owner"


# ---------------------------------------------------------------------------
# Naming + configuration
# ---------------------------------------------------------------------------

def slug(value, cap: int = 40) -> str:
    """Filesystem/bus-safe token; identical to ``kanban_bus_bridge.name``."""
    return _SLUG_UNSAFE.sub("_", str(value or ""))[:cap]


def seat_name(profile, task_id) -> str:
    """Per-card seat: ``hermes-<profile>-<task>``."""
    return f"hermes-{slug(profile or 'unknown')}-{slug(task_id)}"


def card_ref(board, task_id, run_id=None) -> str:
    """Seat's card binding, ``<board>/<task>#<run>`` (run part only when known)."""
    ref = f"{slug(board or 'default')}/{slug(task_id)}"
    return f"{ref}#{int(run_id)}" if run_id is not None else ref


def seat_enabled(env=None) -> bool:
    """False only when the kill switch is set to a disabled word."""
    value = (env or os.environ).get(ENABLE_ENV_VAR, "")
    return str(value).strip().lower() not in DISABLED_VALUES


def busctl_path(env=None) -> str:
    return str((env or os.environ).get(BUSCTL_ENV_VAR) or DEFAULT_BUSCTL)


def python_executable(env=None) -> str:
    """Interpreter for the supervisor and a ``*.py`` busctl client."""
    override = str((env or os.environ).get(BUS_PYTHON_ENV_VAR) or "").strip()
    if override:
        return override
    return sys.executable or DEFAULT_BUS_PYTHON


def call_timeout() -> float:
    try:
        return max(0.5, float(os.environ.get(TIMEOUT_ENV_VAR) or DEFAULT_CALL_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_CALL_TIMEOUT


def heartbeat_seconds() -> float:
    try:
        return max(5.0, float(os.environ.get(HEARTBEAT_ENV_VAR) or DEFAULT_HEARTBEAT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_SECONDS


def busctl_argv(*args, env=None) -> list[str]:
    """Command line for one busctl invocation (a non-``.py`` target is exec'd directly)."""
    target = busctl_path(env)
    if target.endswith(".py"):
        return [python_executable(env), target, *args]
    return [target, *args]


def state_root(env=None) -> str:
    override = str((env or os.environ).get(STATE_DIR_ENV_VAR) or "").strip()
    return override


def seat_state_dir(session: str, *, root=None, env=None) -> str:
    """Per-seat dir: ``<root>/<session>``; holds the ``owner`` file and one dir per run."""
    base = root or state_root(env) or os.path.join(tempfile.gettempdir(), "hermes-switchboard-seats")
    return os.path.join(base, session)


def run_state_dir(session: str, run_id, *, root=None, env=None) -> str:
    """Per-run state dir: ``<root>/<session>/run-<id>`` (doorbell, inbox spool, stop flag).

    The bus seat is named per card (``hermes-<profile>-<task>``), so a retry of the same
    card reuses the seat name. Everything that can stop or close a seat is keyed by RUN
    instead: a stop flag left by run N can never stop run N+1's supervisor, and a close
    for run N is refused once run N+1 owns the seat (``owns_seat``).
    """
    token = str(int(run_id)) if run_id not in (None, "") else "none"
    return os.path.join(seat_state_dir(session, root=root, env=env), f"run-{token}")


# ---------------------------------------------------------------------------
# busctl primitives (never raise, never block a caller)
# ---------------------------------------------------------------------------

def _run(args, *, timeout=None, env=None) -> tuple:
    """-> ``(returncode or None, stdout str)``; None means "could not run at all"."""
    argv = busctl_argv(*args, env=env)
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout or call_timeout(), check=False,
            text=True, errors="replace", stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, ""
    return proc.returncode, proc.stdout or ""


def enrol(session: str, card: str, pid, doorbell: str, *, listener: str = "supervised",
          env=None) -> bool:
    """Best-effort admission. True for rc 0 (active) and rc 3 (recorded degraded)."""
    rc, _ = _run(
        ["enrol", "--session", session, "--card", card, "--pid", str(int(pid)),
         "--listener", listener, "--doorbell", doorbell],
        env=env,
    )
    return rc in (0, RC_DEGRADED)


def heartbeat(session: str, card: str, pid, *, state: str = "active", env=None):
    """-> busctl rc, or None when the bus could not be reached. rc 4 = closed elsewhere."""
    rc, _ = _run(
        ["heartbeat", "--session", session, "--state", state, "--card", card,
         "--pid", str(int(pid))],
        env=env,
    )
    return rc


def close(session: str, *, env=None) -> bool:
    """Best-effort close. Idempotent: busctl's close is a plain UPDATE."""
    rc, _ = _run(["close", "--session", session], env=env)
    return rc == 0


def unread_mail(session: str, *, env=None) -> list:
    """Unread directed mail for the seat; ``[]`` on any failure."""
    rc, out = _run(["scan", "--session", session, "--json"], env=env)
    if rc != 0 or not out.strip():
        return []
    try:
        rows = json.loads(out)
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def bus_db_path(env=None) -> str:
    return str((env or os.environ).get(BUS_DB_ENV_VAR) or DEFAULT_BUS_DB)


def seat_demoted(session: str, doorbell: str, *, env=None) -> bool:
    """True when the bus row no longer carries OUR listener/doorbell, or was closed.

    Read-only and deliberately conservative: an unreadable or absent row returns False, so
    the heartbeat path keeps its normal meaning. The check exists because the seat is a
    shared row: the interim ``kanban_bus_bridge`` seats the same kanban runs under the same
    names (with no listener), and any third party can close a seat. A supervisor that keeps
    beating a row it no longer owns is reporting a seat that stands for nothing — so it
    re-enrols instead. ``busctl`` itself refuses to promote a seat without listener +
    doorbell, which is exactly the state this catches.
    """
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{bus_db_path(env)}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return False
    try:
        row = con.execute(
            "SELECT listener, doorbell, closed_at FROM seat WHERE session = ?", (session,)
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    if row is None:
        return False
    return bool(
        row["closed_at"]
        or row["listener"] != SUPERVISED_LISTENER
        or row["doorbell"] != doorbell
    )


# ---------------------------------------------------------------------------
# Spawn side (called by kanban_db_dispatch._default_spawn)
# ---------------------------------------------------------------------------

def supervisor_argv(*, session: str, card: str, worker_pid, board_db: str, run_id,
                    state_dir: str, doorbell: str, interval: float, env=None) -> list[str]:
    """Pure argv builder for the per-run supervisor (tests pin this, not a running loop)."""
    return [
        python_executable(env),
        os.path.abspath(__file__),
        "keeper",
        "--session", session,
        "--card", card,
        "--worker-pid", str(int(worker_pid)),
        "--run-id", str(int(run_id)) if run_id is not None else "",
        "--board-db", str(board_db or ""),
        "--state-dir", str(state_dir),
        "--doorbell", str(doorbell),
        "--interval", str(float(interval)),
    ]


def keeper_env(base_env=None) -> dict:
    """Minimal environment for the supervisor: no profile secrets, no model routing.

    The worker env is scrubbed of provider keys by the dispatcher; the supervisor does not
    need even that much, so only the handful of variables a python child plus a
    user-systemd scope need are copied (``XDG_RUNTIME_DIR`` / ``DBUS_SESSION_BUS_ADDRESS``
    are what ``systemd-run`` needs to place it outside the gateway cgroup).
    """
    src = base_env if base_env is not None else os.environ
    keys = (
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "USER", "LOGNAME",
        "SHELL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_DATA_DIRS",
        BUSCTL_ENV_VAR, BUS_PYTHON_ENV_VAR, HEARTBEAT_ENV_VAR, TIMEOUT_ENV_VAR,
    )
    env = {key: src[key] for key in keys if src.get(key)}
    if not env.get("PATH"):
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return env


def ensure_state_dir(session: str, *, run_id, root=None, env=None) -> str:
    """Create this run's state dir (0700), materialize its doorbell, and claim the seat.

    A stale stop flag is removed (a reused ``run-none`` dir must not stop the new
    supervisor on its first tick), and the seat's ``owner`` file is pointed at this run
    so any later close issued for an earlier run of the same card is refused.
    """
    directory = run_state_dir(session, run_id, root=root, env=env)
    os.makedirs(os.path.dirname(directory), mode=0o700, exist_ok=True)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    doorbell = os.path.join(directory, _DOORBELL_NAME)
    with open(doorbell, "a", encoding="utf-8"):
        pass
    try:
        os.unlink(stop_flag(directory))
    except FileNotFoundError:
        pass
    owner = _owner_path(directory)
    tmp = f"{owner}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(os.path.basename(directory) + "\n")
    os.replace(tmp, owner)
    return directory


def _owner_path(state_dir: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(state_dir)), _OWNER_NAME)


def owns_seat(state_dir: str) -> bool:
    """True unless the seat's ``owner`` file names a DIFFERENT run than *state_dir*.

    A missing or unreadable owner file means no later run has claimed the seat, so the
    run holding *state_dir* still may close it. A retry of the same card inside the
    terminal-worker reap grace window (or an old supervisor that wakes up late) must not
    close the seat the live run now holds — the bus close is by name, so this is the guard.
    """
    try:
        with open(_owner_path(state_dir), "r", encoding="utf-8-sig") as fh:
            owner = fh.read().strip()
    except OSError:
        return True
    return not owner or owner == os.path.basename(os.path.abspath(state_dir))


def release_seat(state_dir: str) -> None:
    """Drop the seat claim if *state_dir* holds it (a spawn whose supervisor never ran)."""
    if not owns_seat(state_dir):
        return
    try:
        os.unlink(_owner_path(state_dir))
    except OSError:
        pass


def start_supervisor(*, session: str, card: str, worker_pid, board_db: str, run_id,
                     state_dir: str, doorbell: str, interval: float, env=None,
                     wrap=None, popen=None):
    """Launch the supervisor; -> its pid, or None. NEVER raises, never blocks the spawn.

    *wrap* is the optional restart-safe scope wrapper (``callable(argv) -> argv``): a
    supervisor should survive a gateway restart like its worker does, but unlike the
    worker it must never fail the spawn, so the caller passes the degrade-instead-of-raise
    policy.
    """
    if not seat_enabled(env) or not worker_pid:
        return None
    argv = supervisor_argv(
        session=session, card=card, worker_pid=worker_pid, board_db=board_db, run_id=run_id,
        state_dir=state_dir, doorbell=doorbell, interval=interval, env=env,
    )
    if wrap is not None:
        try:
            argv = list(wrap(argv))
        except Exception:
            argv = supervisor_argv(
                session=session, card=card, worker_pid=worker_pid, board_db=board_db,
                run_id=run_id, state_dir=state_dir, doorbell=doorbell, interval=interval,
                env=env,
            )
    launcher = popen or subprocess.Popen
    try:
        proc = launcher(
            argv,
            cwd=state_dir if os.path.isdir(state_dir) else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=keeper_env(env),
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        return None
    pid = getattr(proc, "pid", None)
    try:
        pid_file = os.path.join(state_dir, _SUPERVISOR_PID_NAME)
        with open(pid_file, "w", encoding="utf-8") as fh:
            fh.write(f"{pid}\n")
    except OSError:
        pass
    return pid


def stop_flag(state_dir: str) -> str:
    return os.path.join(state_dir, _STOP_NAME)


def signal_stop(state_dir: str) -> None:
    """Ask a running supervisor to close its seat and exit (best-effort touch)."""
    try:
        flag = stop_flag(state_dir)
        with open(flag, "a", encoding="utf-8"):
            pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Run-end side (called by kanban_db after the run-closing txn commits)
# ---------------------------------------------------------------------------

def close_run_seat(session: str, state_dir: str, *, env=None) -> bool:
    """Stop this run's supervisor and close the seat. Best-effort; safe to call twice.

    *state_dir* is the run's dir (``run_state_dir``). Its stop flag is always set; the bus
    close is skipped when a later run of the same card already owns the seat.
    """
    if not session:
        return False
    if state_dir:
        signal_stop(state_dir)
        if not owns_seat(state_dir):
            return False
    return close(session, env=env)


# ---------------------------------------------------------------------------
# Supervisor loop
# ---------------------------------------------------------------------------

def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    import psutil

    # psutil, not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT, not a probe.
    if not psutil.pid_exists(pid):
        return False
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid:d}/status", "r", encoding="utf-8-sig") as fh:
                for line in fh:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" is dead even though the pid still exists.
                        return "Z" not in line.split(":", 1)[1]
        except OSError:
            return False
    return True


def _run_ended(board_db: str, run_id):
    """True when the run's row says it is over. Read-only; any error means "unknown"."""
    if not board_db or run_id in (None, ""):
        return False
    if not os.path.exists(board_db):
        return False
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{board_db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return False
    try:
        row = con.execute(
            "SELECT ended_at, status FROM task_runs WHERE id = ?", (int(run_id),)
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    if not row:
        return False
    return row[0] is not None or (row[1] and row[1] != "running")


def _spool(session: str, state_dir: str, rows, seen: set) -> int:
    """Append not-yet-spooled mail to ``inbox.ndjson``; -> number appended."""
    fresh = [r for r in rows if isinstance(r, dict) and r.get("msg_id") not in seen]
    if not fresh:
        return 0
    path = os.path.join(state_dir, _INBOX_NAME)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for row in fresh:
                fh.write(json.dumps({
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "seat": session,
                    "msg_id": row.get("msg_id"),
                    "ts": row.get("ts"),
                    "sender": row.get("sender"),
                    "re": row.get("re"),
                    "ack": row.get("ack"),
                    "body": str(row.get("body") or "")[:8000],
                }, ensure_ascii=False) + "\n")
                seen.add(row.get("msg_id"))
    except OSError:
        return 0
    return len(fresh)


def _load_spooled(state_dir: str) -> set:
    seen: set = set()
    try:
        with open(os.path.join(state_dir, _INBOX_NAME), "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(json.loads(line).get("msg_id"))
                except ValueError:
                    continue
    except OSError:
        return seen
    return seen


def _log(state_dir: str, message: str) -> None:
    try:
        log_path = os.path.join(state_dir, "supervisor.log")
        with open(log_path, "a", encoding="utf-8") as fh:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _parse_keeper_argv(argv: list) -> dict:
    opts = {k: "" for k in ("--session", "--card", "--worker-pid", "--run-id", "--board-db",
                            "--state-dir", "--doorbell", "--interval")}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in opts and index + 1 < len(argv):
            opts[token] = argv[index + 1]
            index += 2
            continue
        index += 1
    return opts


def _exit_reason(state_dir: str, worker_pid, board_db: str, run_id):
    """Why the supervisor should stop now, or None to keep holding the seat."""
    if os.path.exists(stop_flag(state_dir)):
        return "stop-flag"
    if not owns_seat(state_dir):
        return "superseded"
    if not _pid_alive(worker_pid):
        return "worker-exit"
    if _run_ended(board_db, run_id):
        return "run-ended"
    return None


def _final_close(session: str, state_dir: str, reason: str, closed_elsewhere: bool) -> None:
    """Close last (heartbeats are already stopped), and only a seat this run still holds."""
    if closed_elsewhere:
        _log(state_dir, f"{reason}: close skipped (seat already closed)")
    elif not owns_seat(state_dir):
        # A later run of this card holds the seat now; closing by name would end ITS seat.
        _log(state_dir, f"{reason}: close skipped (seat held by a later run)")
    else:
        closed = close(session)
        _log(state_dir, f"{reason}: close {'ok' if closed else 'failed'}")


def keeper_main(argv: list, *, slept=None) -> int:
    """Supervise one seat until the work is over. Always exits 0; never raises.

    *slept* is a test hook: ``callable(seconds)`` replaces ``time.sleep``.
    """
    opts = _parse_keeper_argv(list(argv))
    session = opts["--session"]
    if not session:
        return 0
    card = opts["--card"]
    worker_pid = opts["--worker-pid"]
    state_dir = opts["--state-dir"] or seat_state_dir(session)
    doorbell = opts["--doorbell"] or os.path.join(state_dir, _DOORBELL_NAME)
    try:
        interval = max(1.0, float(opts["--interval"] or heartbeat_seconds()))
    except ValueError:
        interval = heartbeat_seconds()
    sleep = slept or time.sleep

    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
    except OSError:
        state_dir = tempfile.gettempdir()
    seen = _load_spooled(state_dir)
    enrolled = False
    closed_elsewhere = False
    reason = "worker-exit"

    try:
        while True:
            stop = _exit_reason(state_dir, worker_pid, opts["--board-db"], opts["--run-id"])
            if stop:
                reason = stop
                break
            if not enrolled:
                enrolled = enrol(session, card, worker_pid, doorbell)
                _log(state_dir, f"enrol {session} -> {'ok' if enrolled else 'failed'} "
                               f"(card={card} pid={worker_pid})")
            if enrolled:
                rc = heartbeat(session, card, worker_pid)
                if rc == RC_HEARTBEAT_AFTER_CLOSE:
                    # Closed by someone else (run-end close, dead-seat sweep): a further
                    # beat is a T10 violation, so stop beating and exit without re-enrol
                    # or a second close.
                    _log(state_dir, "heartbeat refused after close: seat closed elsewhere")
                    closed_elsewhere = True
                    reason = "closed-elsewhere"
                    return 0
                if rc is None:
                    _log(state_dir, "heartbeat unavailable (bus unreachable)")
                elif rc not in (0, RC_DEGRADED):
                    enrolled = False  # admission lost: re-enrol on the next tick
                elif seat_demoted(session, doorbell):
                    # Someone else seated or closed this run (the interim
                    # kanban_bus_bridge, a dead-seat sweep): our listener/doorbell are gone
                    # and busctl has already downgraded the state, so re-enrol rather than
                    # keep beating a row that no longer means what it claims.
                    _log(state_dir, "seat demoted by another writer: re-enrolling")
                    enrolled = False
            try:
                _spool(session, state_dir, unread_mail(session), seen)
            except Exception:
                pass
            for _ in range(_SLEEP_SLICES):
                sleep(interval / _SLEEP_SLICES)
                if os.path.exists(stop_flag(state_dir)):
                    break
    except Exception as exc:  # pragma: no cover - a supervisor must never die loudly
        _log(state_dir, f"supervisor error {type(exc).__name__}: {exc}")
    finally:
        _final_close(session, state_dir, reason, closed_elsewhere)
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "keeper":
        return keeper_main(argv[1:])
    if argv and argv[0] == "close":
        # Operator/repair entry point: close one seat without a supervisor.
        opts = _parse_keeper_argv(argv[1:])
        return 0 if close(opts["--session"]) else 1
    print(__doc__.strip().splitlines()[0])
    print("usage: switchboard_seat.py keeper --session S --card C --worker-pid P ...")
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a process
    sys.exit(main(sys.argv[1:]))
