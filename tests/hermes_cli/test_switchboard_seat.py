"""Switchboard seat lifecycle for Kanban workers (Switchboard P1, jarvis-os/t_d9902bc9).

Frank's standing rule (2026-10-23): every agent holds a switchboard seat for exactly as
long as it runs — enrol at start, deregister at finish. A Kanban worker cannot do that for
itself (it is a fire-and-forget ``hermes chat -q`` subprocess that may be SIGKILLed), so
the dispatcher owns it: ``_default_spawn`` starts one supervisor per worker, the supervisor
holds the seat (enrol + heartbeat + inbox drain), and ``_end_run`` queues the close that
``write_txn`` drains after COMMIT.

The contracts pinned here:

* spawn names the seat ``hermes-<profile>-<task>``, hands it to the worker via
  ``SWITCHBOARD_SEAT``, and starts the supervisor with ``--listener supervised``, a real
  doorbell file and the worker's pid;
* ``_end_run`` covers every run-ending path (complete, review handoff, archive, reclaim,
  timeout) because they all funnel through it, and the close runs AFTER the commit — the
  fake busctl takes the board's SQLite write lock at close time and must succeed;
* enrolment failure can never block or fail a spawn: no busctl runs on the spawn path at
  all, and every busctl call is bounded and swallowed;
* the supervisor heartbeats while the worker lives, stops beating when the seat was closed
  under it (busctl rc 4, a T10 heartbeat-after-close), and closes on worker death, run end
  or an explicit stop.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import switchboard_seat as seat


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_BUSCTL = '''\
import json, os, sqlite3, sys, time

args = sys.argv[1:]
verb = args[0] if args else "none"
delay = float(os.environ.get("FAKE_BUSCTL_SLEEP", "0") or 0)
if delay:
    time.sleep(delay)
rc = int(os.environ.get("FAKE_BUSCTL_RC_" + verb.upper(), "0") or 0)
entry = {"args": args, "rc": rc}
if verb == "close":
    db = os.environ.get("FAKE_BUSCTL_DB", "")
    entry["write_at_close"] = None
    if db:
        try:
            probe = sqlite3.connect(db, timeout=0.5, isolation_level=None)
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("CREATE TABLE IF NOT EXISTS seat_close_probe (at INTEGER)")
            probe.execute("INSERT INTO seat_close_probe VALUES (1)")
            probe.execute("ROLLBACK")
            probe.close()
            entry["write_at_close"] = True
        except Exception as exc:
            entry["write_at_close"] = False
            entry["write_error"] = "%s: %s" % (type(exc).__name__, exc)
if verb == "scan":
    sys.stdout.write(os.environ.get("FAKE_BUSCTL_SCAN", "[]"))
with open(os.environ["FAKE_BUSCTL_LOG"], "a") as fh:
    fh.write(json.dumps(entry) + "\\n")
sys.exit(rc)
'''


@pytest.fixture(autouse=True)
def _clear_pending_seat_closes():
    """The run-end close queue is module state; never leak it between tests."""
    kb._SEAT_CLOSE_PENDING.clear()
    yield
    kb._SEAT_CLOSE_PENDING.clear()


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated HERMES_HOME with an empty kanban DB and a fake busctl (no real bus)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    fake = tmp_path / "fake_busctl.py"
    fake.write_text(_FAKE_BUSCTL, encoding="utf-8")
    log = tmp_path / "busctl.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_SWITCHBOARD_BUSCTL", str(fake))
    monkeypatch.setenv("HERMES_SWITCHBOARD_PYTHON", sys.executable)
    monkeypatch.setenv("FAKE_BUSCTL_LOG", str(log))
    monkeypatch.setenv("FAKE_BUSCTL_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_SWITCHBOARD_TIMEOUT", "5")
    # The supervisor self-checks its bus row; point it at a path that does not exist so no
    # test reads (or depends on) the live bus.
    monkeypatch.setenv("HERMES_SWITCHBOARD_BUS_DB", str(tmp_path / "no-such-bus.db"))
    return log


def _calls(log: Path) -> list:
    raw = log.read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in raw.splitlines() if line.strip()] if raw else []


def _flag(argv: list, name: str):
    return argv[argv.index(name) + 1] if name in argv else None


def _seat_dir(task_id: str, profile: str = "worker", run_id=None) -> Path:
    """The seat's dir, or (with *run_id*) that run's state dir inside it."""
    session_dir = kb.switchboard_seats_root() / seat.seat_name(profile, task_id)
    return session_dir if run_id is None else session_dir / f"run-{run_id}"


def _claimed_task(title: str = "do the thing", assignee: str = "worker"):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee=assignee)
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
    assert run_id is not None
    return tid, run_id


# ---------------------------------------------------------------------------
# Naming (must match the interim kanban_bus_bridge seat, or one run mints two)
# ---------------------------------------------------------------------------

def test_seat_name_and_card_ref_match_bridge_naming():
    assert seat.seat_name("fleet-engineer", "t_d9902bc9") == "hermes-fleet-engineer-t_d9902bc9"
    assert seat.seat_name("a/b c", "t 1") == "hermes-a_b_c-t_1"
    assert seat.card_ref("jarvis-os", "t_d9902bc9", 131709) == "jarvis-os/t_d9902bc9#131709"
    assert seat.card_ref("jarvis-os", "t_x") == "jarvis-os/t_x"


# ---------------------------------------------------------------------------
# Spawn: env seat, supervisor argv, doorbell
# ---------------------------------------------------------------------------

def test_spawn_sets_seat_env_and_starts_supervisor(board, monkeypatch, tmp_path):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    (tmp_path / ".hermes" / "profiles" / "worker").mkdir(parents=True)
    (tmp_path / ".hermes" / "profiles" / "worker" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8")

    spawned = []

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

    def fake_popen(cmd, *args, **kwargs):
        cmd = list(cmd)
        spawned.append({"cmd": cmd, "env": dict(kwargs.get("env") or {})})
        # The worker is the hermes CLI; the seat supervisor is the keeper script. (A
        # systemd-run scope probe may also come through here — it is not a spawn.)
        return FakeProc(5151 if "keeper" in cmd else 4242)

    monkeypatch.setattr(kbd.subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = kb.Task(
        id="t_seat", title="seat me", body=None, assignee="worker", status="running",
        priority=0, created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=None, claim_lock="lock", claim_expires=None,
        tenant=None, current_run_id=7,
    )

    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 4242                     # the worker is what the caller gets back
    # The worker is ``<python> -m hermes_cli.kanban_worker_client`` (no console script).
    worker = next(c for c in spawned if "hermes_cli.kanban_worker_client" in c["cmd"])
    supervisor = next(c for c in spawned if "keeper" in c["cmd"])

    # The worker carries its own per-card seat name.
    assert worker["env"]["SWITCHBOARD_SEAT"] == "hermes-worker-t_seat"

    # The supervisor is the thing that holds the seat: real listener, real doorbell,
    # the worker's pid, the run's card ref and the board DB it watches for run end.
    assert "keeper" in supervisor["cmd"]
    assert _flag(supervisor["cmd"], "--session") == "hermes-worker-t_seat"
    assert _flag(supervisor["cmd"], "--worker-pid") == "4242"
    assert _flag(supervisor["cmd"], "--run-id") == "7"
    assert _flag(supervisor["cmd"], "--card").endswith("/t_seat#7")
    assert _flag(supervisor["cmd"], "--board-db") == str(kb.kanban_db_path())
    doorbell = Path(_flag(supervisor["cmd"], "--doorbell"))
    assert doorbell.name == seat.DOORBELL_NAME
    assert doorbell.exists()               # the handle is a real file, not a string
    # State is per RUN, so nothing a previous run of this card left behind applies here.
    assert Path(_flag(supervisor["cmd"], "--state-dir")) == _seat_dir("t_seat", run_id=7)
    # No provider secrets ride along into the supervisor.
    assert "OPENAI_API_KEY" not in supervisor["env"]


def test_spawn_never_runs_busctl_itself(board, monkeypatch, tmp_path):
    """The whole bus interaction is the supervisor's: the spawn path cannot block on it."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    (tmp_path / ".hermes" / "profiles" / "worker").mkdir(parents=True)
    (tmp_path / ".hermes" / "profiles" / "worker" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8")

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(kbd.subprocess, "Popen", lambda *a, **k: FakeProc())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = kb.Task(
        id="t_seat", title="seat me", body=None, assignee="worker", status="running",
        priority=0, created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=None, claim_lock="lock", claim_expires=None,
        tenant=None, current_run_id=7,
    )

    assert kbd._default_spawn(task, str(workspace)) == 4242
    assert _calls(board) == []           # the spawn path itself never shells out to busctl


def _spawn_scaffold(monkeypatch, tmp_path):
    """Profile + resolved argv + a fake Popen; returns (task, workspace)."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    (tmp_path / ".hermes" / "profiles" / "worker").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hermes" / "profiles" / "worker" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8")

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(kbd.subprocess, "Popen", lambda *a, **k: FakeProc())
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    task = kb.Task(
        id="t_seat", title="seat me", body=None, assignee="worker", status="running",
        priority=0, created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=None, claim_lock="lock", claim_expires=None,
        tenant=None, current_run_id=7,
    )
    return task, str(workspace)


def test_supervisor_failure_never_fails_the_spawn(board, monkeypatch, tmp_path):
    """A seat is observability, never admission control.

    This is the card's "swallow the failure" contract: the guardian mutant narrows the
    guard so the exception escapes, and this test is what goes red.
    """
    task, workspace = _spawn_scaffold(monkeypatch, tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("supervisor exploded")

    monkeypatch.setattr(seat, "start_supervisor", boom)

    assert kbd._default_spawn(task, workspace) == 4242        # the worker spawned anyway
    assert not _seat_dir(task.id, run_id=7).exists()          # and no half-open seat was left
    assert not (_seat_dir(task.id) / "owner").exists()        # nor a claim on the seat
    assert kb._SEAT_CLOSE_PENDING == {}                       # nothing queued to close


def test_enrolment_failure_is_swallowed_and_bounded(board, monkeypatch, tmp_path):
    """A dead, broken or hanging bus must never raise into the caller."""
    # rc != 0
    monkeypatch.setenv("FAKE_BUSCTL_RC_ENROL", "1")
    assert seat.enrol("hermes-worker-t_a", "b/t_a#1", 1234, "/tmp/doorbell") is False

    # cannot even be executed
    monkeypatch.setenv("HERMES_SWITCHBOARD_BUSCTL", str(tmp_path / "nope.py"))
    assert seat.enrol("hermes-worker-t_a", "b/t_a#1", 1234, "/tmp/doorbell") is False
    # The interpreter runs but cannot open the script (rc 2): still a plain failure value,
    # never an exception, and never mistaken for a success.
    assert seat.heartbeat("hermes-worker-t_a", "b/t_a#1", 1234) != 0
    assert seat.unread_mail("hermes-worker-t_a") == []

    # hangs past the call budget
    monkeypatch.setenv("HERMES_SWITCHBOARD_BUSCTL", str(tmp_path / "fake_busctl.py"))
    monkeypatch.setenv("FAKE_BUSCTL_SLEEP", "30")
    monkeypatch.setenv("HERMES_SWITCHBOARD_TIMEOUT", "0.5")
    import time as _time

    started = _time.monotonic()
    assert seat.enrol("hermes-worker-t_a", "b/t_a#1", 1234, "/tmp/doorbell") is False
    assert _time.monotonic() - started < 10


# ---------------------------------------------------------------------------
# Run end: one close, after the commit, on every path
# ---------------------------------------------------------------------------

def test_run_end_closes_seat_after_commit(board):
    tid, run_id = _claimed_task()
    _seat_dir(tid, run_id=run_id).mkdir(parents=True)   # the dispatcher created it at spawn

    with kbc.connect() as conn:
        # A review handoff reassigns the card BEFORE ending the run; the seat belongs to
        # the profile that RAN it, so the close must still name "worker".
        assert kb.request_review(conn, tid, summary="done", reviewer="reviewer") is True
        assert kb.get_task(conn, tid).assignee == "reviewer"

    calls = [c for c in _calls(board) if c["args"][0] == "close"]
    assert len(calls) == 1
    assert calls[0]["args"] == ["close", "--session", seat.seat_name("worker", tid)]
    # The close did not run under the board's write lock: the fake busctl took
    # BEGIN IMMEDIATE itself and it succeeded.
    assert calls[0]["write_at_close"] is True, calls[0]


def test_completion_closes_seat(board):
    tid, run_id = _claimed_task()
    _seat_dir(tid, run_id=run_id).mkdir(parents=True)
    with kbc.connect() as conn:
        assert kb.complete_task(conn, tid, summary="finished") is True
    sessions = [c["args"][2] for c in _calls(board) if c["args"][0] == "close"]
    assert sessions == [seat.seat_name("worker", tid)]


def test_archive_closes_seat(board):
    tid, run_id = _claimed_task()
    _seat_dir(tid, run_id=run_id).mkdir(parents=True)
    with kbc.connect() as conn:
        assert kb.archive_task(conn, tid) is True
    assert [c["args"][0] for c in _calls(board)] == ["close"]


def test_run_end_without_a_seat_never_touches_the_bus(board):
    """Nothing was enrolled, so nothing is closed: no subprocess, no bus write."""
    tid, _ = _claimed_task()
    with kbc.connect() as conn:
        assert kb.complete_task(conn, tid, summary="finished") is True
    assert _calls(board) == []


def test_reap_of_a_killed_worker_closes_its_seat(board):
    """The terminal-worker reaper kills a worker whose run already ended: close again."""
    row = {"id": 3, "task_id": "t_reaped", "profile": "worker"}   # sqlite3.Row-shaped
    assert kbd._close_seat_for_reaped_run(row) is None
    calls = _calls(board)
    assert [c["args"] for c in calls] == [
        ["close", "--session", "hermes-worker-t_reaped"]
    ]


def test_kill_switch_disables_every_seat_call(board, monkeypatch):
    """``HERMES_SWITCHBOARD_SEAT=off`` is a hard stop: no env, no close, no bus."""
    monkeypatch.setenv("HERMES_SWITCHBOARD_SEAT", "off")
    env: dict = {}
    assert kbd._switchboard_seat_session("worker", SimpleNamespace(id="t_off"), env) is None
    assert "SWITCHBOARD_SEAT" not in env

    tid, run_id = _claimed_task()
    _seat_dir(tid, run_id=run_id).mkdir(parents=True)
    with kbc.connect() as conn:
        assert kb.complete_task(conn, tid, summary="finished") is True
    assert _calls(board) == []


# ---------------------------------------------------------------------------
# Supervisor loop
# ---------------------------------------------------------------------------

def _keeper_state(board, tid="t_keeper", profile="worker", run_id=5) -> Path:
    directory = _seat_dir(tid, profile, run_id=run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _keeper_argv(state_dir: Path, *, session="hermes-worker-t_keeper",
                 card="jarvis-os/t_keeper#5", worker_pid="987654", run_id="5",
                 board_db="") -> list:
    return [
        "--session", session,
        "--card", card,
        "--worker-pid", worker_pid,
        "--run-id", run_id,
        "--board-db", str(board_db),
        "--state-dir", str(state_dir),
        "--doorbell", str(state_dir / seat.DOORBELL_NAME),
        "--interval", "10",
    ]


def _run_keeper(state_dir: Path, **overrides) -> int:
    return seat.keeper_main(_keeper_argv(state_dir, **overrides), slept=lambda _s: None)


def _run_keeper_argv(argv: list) -> int:
    return seat.keeper_main(argv, slept=lambda _s: None)


def test_keeper_enrols_heartbeats_and_closes_on_worker_death(board, monkeypatch):
    state = _keeper_state(board)
    alive = iter([True, True, True, False])
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: next(alive, False))

    assert _run_keeper(state) == 0

    # One tick = enrol, beat, drain-mail; a dead worker ends the loop and closes the seat.
    verbs = [c["args"][0] for c in _calls(board)]
    assert verbs == ["enrol", "heartbeat", "scan",
                     "heartbeat", "scan", "heartbeat", "scan", "close"]
    enrol = _calls(board)[0]["args"]
    assert _flag(enrol, "--listener") == "supervised"
    assert _flag(enrol, "--doorbell") == str(state / seat.DOORBELL_NAME)
    assert _flag(enrol, "--pid") == "987654"
    assert _flag(enrol, "--card") == "jarvis-os/t_keeper#5"
    # Never beat a closed seat: the close is the last thing the supervisor does.
    assert verbs[-1] == "close"


def _fake_bus_with_seat(tmp_path: Path, session: str, *, listener=None, doorbell=None,
                        closed_at=None) -> Path:
    """A minimal bus DB carrying one seat row, for the supervisor's self-check."""
    import sqlite3

    db = tmp_path / "bus.db"
    if db.exists():
        db.unlink()                     # called twice in one test: start clean
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE seat (session TEXT, state TEXT, listener TEXT,"
                " doorbell TEXT, closed_at INTEGER)")
    con.execute("INSERT INTO seat VALUES (?,?,?,?,?)", (session, "degraded", listener, doorbell, closed_at))
    con.commit()
    con.close()
    return db


def test_keeper_re_enrols_when_another_writer_demotes_its_seat(board, monkeypatch, tmp_path):
    """The seat row is shared: busctl downgrades it, so the supervisor must re-enrol.

    The interim ``kanban_bus_bridge`` seats the same kanban runs under the same names with
    no listener/doorbell; a keeper that just kept beating would report a seat that stands
    for nothing.
    """
    state = _keeper_state(board)
    session = "hermes-worker-t_keeper"
    monkeypatch.setenv(
        "HERMES_SWITCHBOARD_BUS_DB",
        str(_fake_bus_with_seat(tmp_path, session)),          # demoted: no listener/doorbell
    )
    alive = iter([True, True, True, False])
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: next(alive, False))

    assert _run_keeper(state) == 0

    verbs = [c["args"][0] for c in _calls(board)]
    assert [v for v in verbs if v != "scan"] == [
        "enrol", "heartbeat", "enrol", "heartbeat", "enrol", "heartbeat", "close",
    ]
    log = (state / "supervisor.log").read_text(encoding="utf-8")
    assert log.count("seat demoted by another writer") == 3


def test_keeper_keeps_its_seat_when_the_row_is_ours(board, monkeypatch, tmp_path):
    """No churn: a seat that still carries our listener + doorbell is not re-enrolled."""
    state = _keeper_state(board)
    session = "hermes-worker-t_keeper"
    doorbell = str(state / seat.DOORBELL_NAME)
    monkeypatch.setenv(
        "HERMES_SWITCHBOARD_BUS_DB",
        str(_fake_bus_with_seat(tmp_path, session, listener="supervised", doorbell=doorbell)),
    )
    alive = iter([True, True, True, False])
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: next(alive, False))

    assert _run_keeper(state) == 0

    verbs = [c["args"][0] for c in _calls(board)]
    assert [v for v in verbs if v != "scan"] == ["enrol", "heartbeat", "heartbeat", "heartbeat", "close"]


def test_seat_demoted_reads_the_row_defensively(board, monkeypatch, tmp_path):
    assert seat.seat_demoted("nobody", "/tmp/x") is False                 # absent row
    monkeypatch.setenv("HERMES_SWITCHBOARD_BUS_DB", str(tmp_path / "missing.db"))
    assert seat.seat_demoted("nobody", "/tmp/x") is False                 # unreadable bus
    db = _fake_bus_with_seat(tmp_path, "s", listener="supervised", doorbell="/tmp/x")
    monkeypatch.setenv("HERMES_SWITCHBOARD_BUS_DB", str(db))
    assert seat.seat_demoted("s", "/tmp/x") is False
    assert seat.seat_demoted("s", "/tmp/other") is True                   # not our doorbell
    monkeypatch.setenv(
        "HERMES_SWITCHBOARD_BUS_DB",
        str(_fake_bus_with_seat(tmp_path, "c", listener="supervised", doorbell="/tmp/x",
                                closed_at=1)),
    )
    assert seat.seat_demoted("c", "/tmp/x") is True                       # closed elsewhere


def test_keeper_stops_after_a_t10_heartbeat_after_close(board, monkeypatch):
    """rc 4 means it was closed elsewhere; re-beating would be a protocol violation."""
    state = _keeper_state(board)
    monkeypatch.setenv("FAKE_BUSCTL_RC_HEARTBEAT", "4")
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: True)

    # A keeper that ignores the refusal loops forever; make that a failure, not a hang.
    ticks = []

    def _slept(_seconds):
        ticks.append(1)
        if len(ticks) > 40:
            raise AssertionError("keeper kept looping after its seat was closed")

    assert seat.keeper_main(_keeper_argv(state), slept=_slept) == 0

    verbs = [c["args"][0] for c in _calls(board)]
    assert verbs == ["enrol", "heartbeat"]
    log = (state / "supervisor.log").read_text(encoding="utf-8")
    assert "closed elsewhere" in log


def test_keeper_closes_when_the_run_already_ended(board, monkeypatch):
    """Run-end is durable in ``task_runs.ended_at``; every reaper path sets it."""
    tid, run_id = _claimed_task()
    state = _keeper_state(board, tid=tid, run_id=run_id)
    with kbc.connect() as conn:
        assert kb.complete_task(conn, tid, summary="finished") is True

    board.write_text("", encoding="utf-8")     # that transition's own close already ran
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: True)   # worker still lingering
    argv = _keeper_argv(
        state, session=seat.seat_name("worker", tid),
        card=seat.card_ref("default", tid, run_id), run_id=str(run_id),
        board_db=kb.kanban_db_path(),
    )
    assert seat.keeper_main(argv, slept=lambda _s: None) == 0
    verbs = [c["args"][0] for c in _calls(board)]
    assert verbs == ["close"]                 # run already over: never enrol, just close


def test_keeper_stop_flag_closes_without_beating(board, monkeypatch):
    state = _keeper_state(board)
    (state / "stop").write_text("", encoding="utf-8")
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: True)
    assert _run_keeper(state) == 0
    assert [c["args"][0] for c in _calls(board)] == ["close"]


def test_keeper_spools_mail_without_acking(board, monkeypatch):
    """The listener half: received mail is persisted, never acked, never read-marked."""
    state = _keeper_state(board)
    monkeypatch.setenv(
        "FAKE_BUSCTL_SCAN",
        json.dumps([{"msg_id": "m1", "ts": "2026-10-23T12:00:00Z", "sender": "jarvis",
                     "re": "t_d9902bc9", "ack": "requested", "body": "status?"}]),
    )
    alive = iter([True, False])
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: next(alive, False))

    assert _run_keeper(state) == 0

    spooled = [json.loads(line) for line in
               (state / "inbox.ndjson").read_text(encoding="utf-8").splitlines() if line]
    assert [row["msg_id"] for row in spooled] == ["m1"]
    assert spooled[0]["body"] == "status?"
    verbs = [c["args"][0] for c in _calls(board)]
    assert "ack" not in verbs and "mark-read" not in verbs


def test_keeper_is_quiet_when_the_bus_is_missing(board, monkeypatch, tmp_path):
    """A worker without a bus still gets a supervisor that exits 0."""
    state = _keeper_state(board)
    monkeypatch.setenv("HERMES_SWITCHBOARD_BUSCTL", str(tmp_path / "missing.py"))
    alive = iter([True, False])
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: next(alive, False))
    assert _run_keeper(state) == 0
    assert (state / "supervisor.log").exists()


# ---------------------------------------------------------------------------
# A retry of the same card reuses the seat NAME, never the previous run's state
# ---------------------------------------------------------------------------

def _spawn_db_task(monkeypatch, tmp_path, tid: str, *, worker_pid: int) -> list:
    """Spawn the claimed DB task *tid* for real; -> the supervisor argv it started.

    Popen is faked only for the spawn itself: the keeper run afterwards needs the real one
    for its (fake) busctl calls.
    """
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    (tmp_path / ".hermes" / "profiles" / "worker").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hermes" / "profiles" / "worker" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8")
    (tmp_path / "ws").mkdir(exist_ok=True)
    started = []

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

    def fake_popen(cmd, *args, **kwargs):
        cmd = list(cmd)
        if "keeper" in cmd:
            started.append(cmd)
            return FakeProc(worker_pid + 1)
        return FakeProc(worker_pid)

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(kbd.subprocess, "Popen", fake_popen)
        assert kbd._default_spawn(task, str(tmp_path / "ws")) == worker_pid
    assert len(started) == 1
    return started[0][started[0].index("keeper") + 1:]


def _end_and_reclaim(tid: str) -> int:
    """End *tid*'s current run (a crash/reclaim) and claim it again; -> the new run id."""
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            kb._end_run(conn, tid, outcome="crashed", status="crashed")
            conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
                         (tid,))
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
    return run_id


def test_retry_of_a_card_gets_a_supervisor_the_old_stop_flag_cannot_stop(
        board, monkeypatch, tmp_path):
    """Finding (a1): run 1's close leaves a stop flag; run 2's supervisor must still enrol."""
    tid, first = _claimed_task()
    _spawn_db_task(monkeypatch, tmp_path, tid, worker_pid=4242)
    second = _end_and_reclaim(tid)                   # run 1's close set ITS stop flag
    assert second != first
    argv = _spawn_db_task(monkeypatch, tmp_path, tid, worker_pid=5252)
    board.write_text("", encoding="utf-8")

    alive = iter([True, False])
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: next(alive, False))
    assert seat.keeper_main(argv, slept=lambda _s: None) == 0

    verbs = [c["args"][0] for c in _calls(board)]
    assert verbs[0] == "enrol", verbs                # not an instant stop-flag close
    assert _flag(_calls(board)[0]["args"], "--card").endswith(f"/{tid}#{second}")


def test_reaper_close_of_an_old_run_spares_the_retry_holding_the_seat(
        board, monkeypatch, tmp_path):
    """Finding (a2): the reaper acts on a run that ended a grace period ago; by then the
    card may be running again under the same seat name, and that seat must stay open."""
    tid, first = _claimed_task()
    _spawn_db_task(monkeypatch, tmp_path, tid, worker_pid=4242)
    second = _end_and_reclaim(tid)
    _spawn_db_task(monkeypatch, tmp_path, tid, worker_pid=5252)
    board.write_text("", encoding="utf-8")

    kbd._close_seat_for_reaped_run({"id": first, "task_id": tid, "profile": "worker"})

    assert [c for c in _calls(board) if c["args"][0] == "close"] == []
    assert not (_seat_dir(tid, run_id=second) / "stop").exists()   # live supervisor untouched
    assert (_seat_dir(tid, run_id=first) / "stop").exists()        # the reaped run's is set


def test_old_supervisor_waking_late_never_closes_the_retry_seat(board, monkeypatch, tmp_path):
    """The old run's supervisor notices its end on a later tick; it must not close by name."""
    tid, _ = _claimed_task()
    old_argv = _spawn_db_task(monkeypatch, tmp_path, tid, worker_pid=4242)
    _end_and_reclaim(tid)
    _spawn_db_task(monkeypatch, tmp_path, tid, worker_pid=5252)
    board.write_text("", encoding="utf-8")

    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: False)    # old worker is gone
    assert seat.keeper_main(old_argv, slept=lambda _s: None) == 0
    assert [c["args"][0] for c in _calls(board)] == []


def test_reaper_close_honours_the_kill_switch(board, monkeypatch):
    """Mutant G7: the reaper's close must be as dead as every other seat call when off."""
    row = {"id": 3, "task_id": "t_reaped", "profile": "worker"}
    run_dir = _seat_dir("t_reaped", run_id=3)
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_SWITCHBOARD_SEAT", "off")

    kbd._close_seat_for_reaped_run(row)

    assert _calls(board) == []
    assert not (run_dir / "stop").exists()


# ---------------------------------------------------------------------------
# A rolled-back run-end never closes the seat (finding b)
# ---------------------------------------------------------------------------

def test_rolled_back_run_end_leaves_the_seat_open(board):
    tid, run_id = _claimed_task()
    _seat_dir(tid, run_id=run_id).mkdir(parents=True)
    with kbc.connect() as conn:
        with pytest.raises(RuntimeError):
            with kb.write_txn(conn):
                kb._end_run(conn, tid, outcome="completed")
                raise RuntimeError("transition failed after the run-end")
        assert kb.get_task(conn, tid).current_run_id == run_id        # the run is still live
        with kb.write_txn(conn):                                        # any later txn
            conn.execute("UPDATE tasks SET priority = 1 WHERE id = ?", (tid,))
    assert _calls(board) == []
    assert kb._SEAT_CLOSE_PENDING == {}


def test_failed_commit_of_a_run_end_leaves_the_seat_open(board, monkeypatch):
    tid, run_id = _claimed_task()
    _seat_dir(tid, run_id=run_id).mkdir(parents=True)
    real = kbc._execute_boundary_with_retry
    fail = {"commit": True}

    def flaky(conn, sql):
        if sql == "COMMIT" and fail.pop("commit", False):
            raise sqlite3.OperationalError("database is locked")
        return real(conn, sql)

    monkeypatch.setattr(kbc, "_execute_boundary_with_retry", flaky)
    with kbc.connect() as conn:
        with pytest.raises(sqlite3.OperationalError):
            with kb.write_txn(conn):
                kb._end_run(conn, tid, outcome="completed")
        assert kb.get_task(conn, tid).current_run_id == run_id
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 1 WHERE id = ?", (tid,))
    assert _calls(board) == []
    assert kb._SEAT_CLOSE_PENDING == {}


def test_rolled_back_savepoint_drops_only_its_own_seat_close(board):
    """A nested run-end that rolls back is forgotten; the outer txn's own close still runs."""
    kept, kept_run = _claimed_task(title="kept")
    undone, undone_run = _claimed_task(title="undone")
    _seat_dir(kept, run_id=kept_run).mkdir(parents=True)
    _seat_dir(undone, run_id=undone_run).mkdir(parents=True)
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            kb._end_run(conn, kept, outcome="completed")
            with pytest.raises(RuntimeError):
                with kb.write_txn(conn, allow_nested=True):
                    kb._end_run(conn, undone, outcome="completed")
                    raise RuntimeError("nested step failed")
    sessions = [c["args"][2] for c in _calls(board) if c["args"][0] == "close"]
    assert sessions == [seat.seat_name("worker", kept)]


def test_superseded_keeper_stops_beating_without_a_stop_flag(board, monkeypatch):
    """A keeper whose seat a later run claimed exits quietly even if its stop flag was lost."""
    state = _keeper_state(board, run_id=5)
    (state.parent / "owner").write_text("run-6\n", encoding="utf-8")
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: True)       # old worker lingering
    ticks = []

    def _slept(_seconds):
        ticks.append(1)
        if len(ticks) > 40:
            raise AssertionError("superseded keeper kept holding the seat")

    assert seat.keeper_main(_keeper_argv(state), slept=_slept) == 0
    assert ticks == []                               # it never even reached a sleep
    assert _calls(board) == []                       # no enrol, no beat, no close by name


def test_reused_run_dir_starts_without_the_previous_stop_flag(board):
    """A spawn with no run id reuses ``run-none``; the last run's stop must not carry over."""
    root = str(kb.switchboard_seats_root())
    first = seat.ensure_state_dir("hermes-worker-t_norun", run_id=None, root=root)
    seat.signal_stop(first)
    again = seat.ensure_state_dir("hermes-worker-t_norun", run_id=None, root=root)
    assert again == first
    assert not Path(seat.stop_flag(again)).exists()
    assert seat.owns_seat(again)


# ---------------------------------------------------------------------------
# R2: a seat failure is board-visible (task event on the run + warning), not debug-only
# ---------------------------------------------------------------------------

def _seat_events(run_id: int, kind: str) -> list:
    """Task events of *kind* recorded under one run (the board is the source of truth)."""
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE run_id = ? AND kind = ? ORDER BY id",
            (run_id, kind),
        ).fetchall()
    return [json.loads(r[0]) if r[0] else {} for r in rows]


def test_keeper_enrol_failure_is_recorded_once_per_run(board, monkeypatch):
    """A run whose enrol keeps failing leaves ONE board event, not one per retry tick."""
    tid, run_id = _claimed_task()
    state = _seat_dir(tid, "worker", run_id=run_id)
    state.mkdir(parents=True, exist_ok=True)
    attempts: list = []

    def enrol_fails(*args, **_kwargs):
        attempts.append(args)
        return False

    monkeypatch.setattr(seat, "enrol", enrol_fails)
    alive = iter([True, True, True, False])
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: next(alive, False))

    argv = _keeper_argv(state, session=seat.seat_name("worker", tid),
                        card=f"jarvis-os/{tid}#{run_id}", run_id=str(run_id),
                        board_db=kb.kanban_db_path())
    assert _run_keeper_argv(argv) == 0

    assert len(attempts) >= 2                                   # it really did retry
    events = _seat_events(run_id, "switchboard_seat_enrol_failed")
    assert len(events) == 1
    assert events[0]["session"] == seat.seat_name("worker", tid)


def test_keeper_supervisor_crash_is_recorded_on_the_run(board, monkeypatch):
    tid, run_id = _claimed_task()
    state = _seat_dir(tid, "worker", run_id=run_id)
    state.mkdir(parents=True, exist_ok=True)

    def enrol_raises(*_args, **_kwargs):
        raise RuntimeError("bus exploded")

    monkeypatch.setattr(seat, "enrol", enrol_raises)
    alive = iter([True, True, False])          # worker alive long enough to reach enrol
    monkeypatch.setattr(seat, "_pid_alive", lambda _pid: next(alive, False))
    argv = _keeper_argv(state, session=seat.seat_name("worker", tid),
                        card=f"jarvis-os/{tid}#{run_id}", run_id=str(run_id),
                        board_db=kb.kanban_db_path())
    assert _run_keeper_argv(argv) == 0                          # a supervisor never dies loudly

    events = _seat_events(run_id, "switchboard_seat_supervisor_failed")
    assert len(events) == 1
    assert "RuntimeError" in events[0]["error"] and "bus exploded" in events[0]["error"]


def test_record_seat_event_never_raises_without_a_board(board):
    """No board or run means nothing to attach to: report False, never raise."""
    assert seat.record_seat_event(None, "t_x#1", 1, "switchboard_seat_enrol_failed", {}) is False
    assert seat.record_seat_event(kb.kanban_db_path(), "t_x#1", None, "switchboard_seat_enrol_failed", {}) is False
    assert seat.record_seat_event("/nonexistent/dir/kanban.db", "t_x#1", 1, "k", {}) is False


def test_spawn_supervisor_failure_is_recorded_on_the_run(board, monkeypatch, tmp_path):
    """The spawn path's silent drop is now a board event on the run that has no seat."""
    task, workspace = _spawn_scaffold(monkeypatch, tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("supervisor exploded")

    monkeypatch.setattr(seat, "start_supervisor", boom)
    assert kbd._default_spawn(task, workspace) == 4242          # the spawn still succeeds

    events = _seat_events(task.current_run_id, "switchboard_seat_supervisor_failed")
    assert len(events) == 1
    assert "supervisor exploded" in events[0]["error"]


def test_standalone_keeper_file_records_the_seat_failure_on_the_board(board, monkeypatch):
    """The keeper runs as ``python <switchboard_seat.py> keeper`` (not an import).

    Regression for the executed copy: the file must put the checkout root on sys.path
    itself, or ``from hermes_cli import kanban_db`` fails and the board event is lost.
    """
    import subprocess
    import sys as _sys

    tid, run_id = _claimed_task()
    state = _seat_dir(tid, "worker", run_id=run_id)
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAKE_BUSCTL_RC_ENROL", "1")          # every enrol fails
    worker = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(3)"])
    try:
        argv = _keeper_argv(state, session=seat.seat_name("worker", tid),
                            card=f"upero/{tid}#{run_id}", run_id=str(run_id),
                            worker_pid=str(worker.pid), board_db=kb.kanban_db_path())
        child_env = {k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run([_sys.executable, seat.__file__, "keeper", *argv],
                              capture_output=True, timeout=60, env=child_env, cwd=str(state))
    finally:
        worker.kill()
        worker.wait()

    assert proc.returncode == 0, proc.stderr.decode()[-400:]
    events = _seat_events(run_id, "switchboard_seat_enrol_failed")
    assert len(events) == 1
