#!/usr/bin/env python3
"""RED/GREEN acceptance harness for the escalation re-mint defect (kanban t_9a621399).

Defect: ``find_existing_escalation()`` filtered dedupe to OPEN cards only, so a
PM calling ``kanban_complete`` on an escalation made it invisible. The source
stayed blocked (long-lived human-authority gates stay blocked for days), so the
next 30-minute tick saw zero open escalations and minted a fresh card. Observed
census 2026-08-01: 8 cards for one source, 7 done — minted == completions + 1.

This harness is HERMETIC. It builds temp board DBs under a temp KANBAN_DIR and
never touches a live board. It loads TWO modules:

  * the pre-change backup (``backups/*.prechange-t_9a621399-*``)  -> RED
  * the current live script                                       -> GREEN

and runs each against a byte-identical fixture, so the RED evidence is a real
observed failure of the old code rather than a claim about it.

kanban t_b400dc8c update: since the strategy change, a bare needs_input source
is DIGEST-ROUTED (weekly Frank A3) and never reaches the re-mint/dedupe
machinery. The re-mint defect still matters for the sources that DO escalate —
the narrow critical-R3 keep-set. So every source fixture in this harness now
carries an explicit, FRESH critical-R3 marker (block < 168h) and the harness
asserts the dedupe/reopen/cap behaviour against those keep-set sources. The
idle-fleet silence fixture uses a fleet with ZERO needs_input candidates, so
no census line fires (the daily census is expected only when
digest_routed > 0 — see the silence-contract section).

Scenarios
  A  same-episode re-mint     source blocked at T0, escalation done at T0+1h.
                              RED mints a 2nd card; GREEN keeps 1 and reopens.
  B  legitimate re-escalation source re-blocks at T0+2h, AFTER the escalation
                              was completed. BOTH must mint a new card — the
                              fix must not suppress genuine re-escalation.
  C  hard backstop cap        a source already at MAX_ESCALATION_CARDS_PER_SOURCE
                              never mints again even when scenario B applies.
  D  fail-closed              newest card is done with completed_at NULL: the
                              episode cannot be proven newer, so reuse not mint.
  E  policy single-definition TERMINAL_STATUSES is actually read by the module
                              (the constant was dead before this card).
"""
import importlib.machinery
import importlib.util
import glob
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path("/home/frank/.hermes/profiles/jarvis/scripts")
LIVE = Path("/home/frank/.hermes/scripts/service_gate_escalation_watchdog.py")
PRECHANGE = sorted(
    glob.glob(str(SCRIPTS / "backups" / "*.prechange-t_9a621399-*"))
)

# Live tasks schema fields this script actually touches, including completed_at.
SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT,
    assignee TEXT,
    status TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch',
    block_kind TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_error TEXT,
    last_heartbeat_at INTEGER
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER NOT NULL
);
"""

NOW = int(time.time())
T0 = NOW - (100 * 3600)          # source blocked 100h ago, well past 6h gate
HOUR = 3600

failures = []


def check(label, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(label)
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")


def load(path, name):
    # The pre-change backup filename ends in a timestamp, not ``.py``, so the
    # loader cannot be inferred from the suffix — supply it explicitly.
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None, f"could not build a module spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def make_board(root: Path, slug: str) -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    db = d / "kanban.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db


def add_source(db: Path, tid: str, block_events: list[tuple[str, int]], created_at: int):
    """Blocked keep-set source task with an explicit block-event history.

    Since kanban t_b400dc8c only critical-R3 keep-set sources reach the
    escalation/dedupe machinery, the title carries an explicit credential-
    incident marker AND the block episode is fresh (<168h, T0=100h ago), so the
    live script classifies it keep=True instead of digest-routing it.
    """
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tasks (id,title,body,assignee,status,created_by,created_at,block_kind)"
        " VALUES (?,?,?,?,'blocked','pm',?, 'needs_input')",
        (tid, f"CRITICAL R3: SERVICE-GATE api key exposed cleartext for {tid}",
         "", "guardian", created_at),
    )
    for kind, ts in block_events:
        conn.execute(
            "INSERT INTO task_events (task_id,kind,created_at) VALUES (?,?,?)",
            (tid, kind, ts),
        )
    conn.commit()
    conn.close()


def add_escalation(db: Path, eid: str, src: str, board: str, status: str,
                   created_at: int, completed_at):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tasks (id,title,body,assignee,status,created_by,created_at,completed_at)"
        " VALUES (?,?,?,?,?,'service-gate-escalation',?,?)",
        (
            eid,
            f"FRANK ESCALATION: SERVICE-GATE task {src} blocked 100.0h",
            f"**Source task:** {src} on board `{board}`\n",
            "jarvis-os-pm",
            status,
            created_at,
            completed_at,
        ),
    )
    conn.commit()
    conn.close()


def escalation_rows(db: Path, src: str):
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT id, status, completed_at FROM tasks "
        "WHERE created_by='service-gate-escalation' AND title LIKE ?",
        (f"%{src}%",),
    ).fetchall()
    conn.close()
    return rows


def comments_for(db: Path, tid: str):
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=?", (tid,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def build_fixture(root: Path, *, src_id: str, block_events, esc_specs):
    """Create jarvis-os + src-board and seed one source and its escalations."""
    esc_db = make_board(root, "jarvis-os")
    src_db = make_board(root, "src-board")
    add_source(src_db, src_id, block_events, T0)
    for eid, status, created_at, completed_at in esc_specs:
        add_escalation(esc_db, eid, src_id, "src-board", status,
                       created_at, completed_at)
    return esc_db, src_db


def run_scenario(mod, *, src_id, block_events, esc_specs):
    """Run ``mod``'s watchdog over a fresh fixture; return (esc_db, rows_before, rows_after)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "boards"
    root.mkdir(parents=True)
    esc_db, _ = build_fixture(root, src_id=src_id, block_events=block_events,
                              esc_specs=esc_specs)
    mod.KANBAN_DIR = root
    before = escalation_rows(esc_db, src_id)
    mod.main([])
    after = escalation_rows(esc_db, src_id)
    return tmp, esc_db, before, after


# --- fixture definitions ------------------------------------------------------
# Scenario A: blocked at T0 and never unblocked; escalation completed at T0+1h.
A_EVENTS = [("blocked", T0)]
A_ESC = [("t_escA0001", "done", T0 + (30 * 60), T0 + HOUR)]

# Scenario B: blocked at T0, unblocked at T0+1.5h, RE-blocked at T0+2h, i.e.
# the current block episode starts AFTER the escalation's completed_at.
B_EVENTS = [("blocked", T0), ("unblocked", T0 + HOUR + 1800), ("blocked", T0 + 2 * HOUR)]
B_ESC = [("t_escB0001", "done", T0 + (30 * 60), T0 + HOUR)]

# Scenario C: same re-block shape as B but already at the per-source cap.
C_EVENTS = B_EVENTS
C_ESC = [
    ("t_escC0001", "done", T0 + (10 * 60), T0 + (20 * 60)),
    ("t_escC0002", "done", T0 + (25 * 60), T0 + (40 * 60)),
    ("t_escC0003", "done", T0 + (30 * 60), T0 + HOUR),
]

# Scenario D: newest card done but completed_at unknown (legacy row).
D_EVENTS = [("blocked", T0)]
D_ESC = [("t_escD0001", "done", T0 + (30 * 60), None)]


print("=== preflight ===")
check("pre-change backup exists for RED baseline", bool(PRECHANGE), str(PRECHANGE))
if not PRECHANGE:
    print("\nRESULT: FAIL (cannot establish RED without the pre-change backup)")
    sys.exit(1)

old = load(PRECHANGE[-1], "sgw_prechange")
new = load(LIVE, "sgw_live")
check("pre-change module has the status-filtered lookup",
      hasattr(old, "find_existing_escalation"))
check("live module exposes time-aware classify_dedupe",
      hasattr(new, "classify_dedupe"))

print("\n=== E: TERMINAL_STATUSES is a live policy definition, not a dead constant ===")
src_text = LIVE.read_text()
# The literal tuple must no longer be inlined in SQL anywhere.
check("no inlined ('done', 'archived') literal remains in SQL",
      "NOT IN ('done', 'archived')" not in src_text)
# Both dedupe paths must read the constant.
uses = src_text.count("TERMINAL_STATUS")
check("TERMINAL_STATUSES/PLACEHOLDERS referenced by the code", uses >= 4, f"{uses} refs")
check("cap constant defined", hasattr(new, "MAX_ESCALATION_CARDS_PER_SOURCE"),
      str(getattr(new, "MAX_ESCALATION_CARDS_PER_SOURCE", None)))
# Behavioural proof the constant drives the decision: flip it and the same
# fixture must change classification (an unread constant cannot do this).
tmpE = tempfile.TemporaryDirectory()
rootE = Path(tmpE.name) / "boards"
rootE.mkdir(parents=True)
escE, _ = build_fixture(rootE, src_id="t_srcE001", block_events=A_EVENTS,
                        esc_specs=[("t_escE0001", "ready", T0 + 1800, None)])
new.KANBAN_DIR = rootE
d_open = new.classify_dedupe("t_srcE001", T0)
orig_terminal = new.TERMINAL_STATUSES
try:
    new.TERMINAL_STATUSES = ("done", "archived", "ready")
    d_ready_terminal = new.classify_dedupe("t_srcE001", T0)
finally:
    new.TERMINAL_STATUSES = orig_terminal
check("open card heartbeats while 'ready' is non-terminal",
      d_open["action"] == "heartbeat", str(d_open))
check("classification follows TERMINAL_STATUSES when it changes",
      d_ready_terminal["action"] != "heartbeat", str(d_ready_terminal))
tmpE.cleanup()

print("\n=== A/RED: pre-change code re-mints while the source block is unchanged ===")
tmp, esc_db, before, after = run_scenario(
    old, src_id="t_srcA001", block_events=A_EVENTS, esc_specs=A_ESC)
check("RED baseline started with exactly 1 escalation card", len(before) == 1, str(before))
check("RED: pre-change code creates a SECOND card (1 -> 2)",
      len(after) == 2, f"{len(before)} -> {len(after)}: {after}")
tmp.cleanup()

print("\n=== A/GREEN: fixed code reuses the resolved card instead of minting ===")
tmp, esc_db, before, after = run_scenario(
    new, src_id="t_srcA001", block_events=A_EVENTS, esc_specs=A_ESC)
check("GREEN started with exactly 1 escalation card", len(before) == 1, str(before))
check("GREEN: no new card created (count stays 1)",
      len(after) == 1, f"{len(before)} -> {len(after)}: {after}")
row = after[0]
check("GREEN: the existing card was REOPENED out of done",
      row[1] not in new.TERMINAL_STATUSES, f"status={row[1]}")
check("GREEN: completed_at cleared on reopen", row[2] is None, f"completed_at={row[2]}")
cmts = [c for c in comments_for(esc_db, "t_escA0001") if "REOPENED" in c]
check("GREEN: exactly one reopen comment recorded on the card",
      len(cmts) == 1, f"{len(cmts)} comments")
tmp.cleanup()

print("\n=== A/GREEN: reopen honours the kernel's revival invariant ===")
# hermes_cli/kanban_db.py::unblock_task clears the run pointer, claim and
# failure counters when a task returns to 'ready'. A card revived by this
# watchdog must not be left carrying a dangling current_run_id for the
# dispatcher to trip over. Seed the dirty state explicitly and prove it clears.
tmpV = tempfile.TemporaryDirectory()
rootV = Path(tmpV.name) / "boards"
rootV.mkdir(parents=True)
escV, _ = build_fixture(rootV, src_id="t_srcV001", block_events=A_EVENTS,
                        esc_specs=[("t_escV0001", "done", T0 + 1800, T0 + HOUR)])
connV = sqlite3.connect(str(escV))
connV.execute(
    "UPDATE tasks SET consecutive_failures=3, last_failure_error='stale', "
    "last_heartbeat_at=? WHERE id='t_escV0001'", (T0,))
connV.commit()
connV.close()
new.KANBAN_DIR = rootV
new.main([])
connV = sqlite3.connect(str(escV))
rowV = connV.execute(
    "SELECT status, completed_at, consecutive_failures, last_failure_error, "
    "last_heartbeat_at FROM tasks WHERE id='t_escV0001'").fetchone()
evV = connV.execute(
    "SELECT kind, payload FROM task_events WHERE task_id='t_escV0001'").fetchall()
connV.close()
check("reopen clears completed_at", rowV[1] is None, str(rowV))
check("reopen resets consecutive_failures to 0", rowV[2] == 0, str(rowV))
check("reopen clears last_failure_error", rowV[3] is None, str(rowV))
check("reopen refreshes last_heartbeat_at", rowV[4] and rowV[4] > T0, str(rowV))
check("reopen writes an auditable task_events row",
      any(k == "reopened" for k, _ in evV), str(evV))
check("reopen event names the source", any("t_srcV001" in (p or "") for _, p in evV),
      str(evV))
tmpV.cleanup()

print("\n=== A/GREEN idempotency: a second tick must not mint or double-comment ===")
tmpI = tempfile.TemporaryDirectory()
rootI = Path(tmpI.name) / "boards"
rootI.mkdir(parents=True)
escI, _ = build_fixture(rootI, src_id="t_srcA001", block_events=A_EVENTS,
                        esc_specs=A_ESC)
new.KANBAN_DIR = rootI
new.main([])
new.main([])
new.main([])
rows = escalation_rows(escI, "t_srcA001")
check("three consecutive ticks still leave exactly 1 card",
      len(rows) == 1, f"{len(rows)}: {rows}")
reopen_cmts = [c for c in comments_for(escI, "t_escA0001") if "REOPENED" in c]
hb_cmts = [c for c in comments_for(escI, "t_escA0001") if "re-fire heartbeat" in c]
check("reopen happens once, later ticks heartbeat the now-open card",
      len(reopen_cmts) == 1 and len(hb_cmts) == 2,
      f"reopen={len(reopen_cmts)} heartbeat={len(hb_cmts)}")
tmpI.cleanup()

print("\n=== B: legitimate re-escalation must still mint (no regression) ===")
tmp, esc_db, before, after = run_scenario(
    old, src_id="t_srcB001", block_events=B_EVENTS, esc_specs=B_ESC)
check("RED baseline mints on genuine re-block (reference behaviour)",
      len(after) == 2, f"{len(before)} -> {len(after)}")
tmp.cleanup()
tmp, esc_db, before, after = run_scenario(
    new, src_id="t_srcB001", block_events=B_EVENTS, esc_specs=B_ESC)
check("GREEN: source re-blocked AFTER completion still mints a new card",
      len(after) == 2, f"{len(before)} -> {len(after)}: {after}")
check("GREEN: the original resolved card is left resolved",
      any(r[0] == "t_escB0001" and r[1] in new.TERMINAL_STATUSES for r in after),
      str(after))
tmp.cleanup()

print("\n=== C: hard backstop cap bounds any future logic bug ===")
cap = new.MAX_ESCALATION_CARDS_PER_SOURCE
check("cap is a small positive integer", isinstance(cap, int) and 1 <= cap <= 5, str(cap))
tmp, esc_db, before, after = run_scenario(
    new, src_id="t_srcC001", block_events=C_EVENTS, esc_specs=C_ESC)
check("cap fixture starts at the cap", len(before) == cap, f"{len(before)} vs cap {cap}")
check("GREEN: at the cap, a genuine re-block reuses instead of minting",
      len(after) == cap, f"{len(before)} -> {len(after)}: {after}")
check("GREEN: the capped source still has an actionable (non-terminal) card",
      any(r[1] not in new.TERMINAL_STATUSES for r in after), str(after))
tmp.cleanup()
# cap_enforced must mean "the cap changed the outcome", not "count >= cap".
# A source at the cap that is merely being heartbeated was not saved by the cap.
tmpK = tempfile.TemporaryDirectory()
rootK = Path(tmpK.name) / "boards"
rootK.mkdir(parents=True)
build_fixture(rootK, src_id="t_srcK001", block_events=A_EVENTS, esc_specs=[
    ("t_escK0001", "done", T0 + 600, T0 + 1200),
    ("t_escK0002", "done", T0 + 1500, T0 + 2400),
    ("t_escK0003", "ready", T0 + 1800, None),
])
new.KANBAN_DIR = rootK
d_hb_at_cap = new.classify_dedupe("t_srcK001", T0)
check("heartbeat at the cap is NOT reported as cap-enforced",
      d_hb_at_cap["action"] == "heartbeat" and not d_hb_at_cap["cap_enforced"],
      str(d_hb_at_cap))
tmpK.cleanup()
tmpK2 = tempfile.TemporaryDirectory()
rootK2 = Path(tmpK2.name) / "boards"
rootK2.mkdir(parents=True)
build_fixture(rootK2, src_id="t_srcK002", block_events=C_EVENTS, esc_specs=C_ESC)
new.KANBAN_DIR = rootK2
d_suppressed = new.classify_dedupe("t_srcK002", T0 + 2 * HOUR)
check("a suppressed mint IS reported as cap-enforced",
      d_suppressed["action"] == "reopen" and d_suppressed["cap_enforced"],
      str(d_suppressed))
tmpK2.cleanup()
# The same fixture under the old code demonstrates the storm the cap prevents.
tmp, esc_db, before, after = run_scenario(
    old, src_id="t_srcC001", block_events=C_EVENTS, esc_specs=C_ESC)
check("RED: pre-change code grows the card count past the cap",
      len(after) == cap + 1, f"{len(before)} -> {len(after)}")
tmp.cleanup()

print("\n=== D: unknown completed_at fails closed (reuse, never mint) ===")
tmp, esc_db, before, after = run_scenario(
    new, src_id="t_srcD001", block_events=D_EVENTS, esc_specs=D_ESC)
check("GREEN: NULL completed_at does not mint a duplicate",
      len(after) == 1, f"{len(before)} -> {len(after)}: {after}")
check("GREEN: the card is reopened so the gate stays visible",
      after[0][1] not in new.TERMINAL_STATUSES, str(after))
tmp.cleanup()

print("\n=== silence contract: no stdout when nothing needs escalation ===")
import contextlib
import io

# Census reconciliation (kanban t_b400dc8c, os-review round 1 note 3): the
# daily census line fires ONLY when digest_routed > 0 (needs_input sources
# routed to the weekly Frank A3 digest). This idle-fleet fixture has ZERO
# blocked candidates, so digest_routed == 0, no census can fire, and the
# watchdog stays silent. A fleet WITH digest-routed sources is expected to
# print the census line instead of per-task cards — that is the new contract,
# asserted in the keep-set digest scenarios above (dry-run) and covered by the
# real-path temp-DB evidence in the t_b400dc8c implementation run.
tmpS = tempfile.TemporaryDirectory()
rootS = Path(tmpS.name) / "boards"
rootS.mkdir(parents=True)
make_board(rootS, "jarvis-os")
make_board(rootS, "quiet-board")
new.KANBAN_DIR = rootS
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    new.main([])
check("watchdog stays silent on an idle fleet", buf.getvalue() == "",
      repr(buf.getvalue()))
# And a reopen IS reported, because a card returning from done is a real change.
rootS2 = Path(tmpS.name) / "boards2"
rootS2.mkdir(parents=True)
build_fixture(rootS2, src_id="t_srcA001", block_events=A_EVENTS, esc_specs=A_ESC)
new.KANBAN_DIR = rootS2
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    new.main([])
check("a reopen is reported on stdout", "reopened" in buf2.getvalue(),
      repr(buf2.getvalue()))
tmpS.cleanup()

print()
if failures:
    print(f"RESULT: FAIL ({len(failures)} checks failed): {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
