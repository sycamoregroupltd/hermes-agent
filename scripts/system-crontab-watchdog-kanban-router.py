#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""system-crontab-watchdog-kanban-router.py

Companion to system-crontab-watchdog.sh. Invoked by the watchdog ONLY on its
UNHEALTHY branch (missing/unrunnable scripts detected). Fills the structural
gap described in task t_dc22ee7e: alerts reached Frank/Failover but nothing was
actioned because alerts alone don't close the loop. The board must own
remediation.

Pattern: anomaly -> deduped kanban card
  - key    = the watchdog's "targets_<md5>" key derived from the sorted missing
             report (so a stable breakage reuses one card; a NEW breakage re-keys
             and spawns a fresh card immediately).
  - created_by = "system-crontab-watchdog" (mirrors how
             security_audit_route_high_cves.py tags its cards).
  - board  = sycode-trading (the system crontab governs host-level fleet monitors;
             the sycode-trading board is the canonical ops board per the PM route
             in t_dc22ee7e).
  - assignee = devops (control/devops automation per the jarvis PM route comment).

Behavioral contract (mirrors security_audit_route_high_cves.py / anomaly_ledger.py
dedupe discipline — t_0596724e):
  - First UNHEALTHY run for a key -> create ONE card, store its id in the ledger.
  - Subsequent UNHEALTHY runs for the SAME key -> append a fresh comment to the
    existing open card (occurrence counter bump). No second card.
  - When the watchdog reports HEALTHY again for that key -> append a RESOLVED
    comment to the open card, auto-complete it if still in ready/todo/etc (i.e.
    not already picked up by a human), and clear the ledger entry.

The router is invoked with the watchdog's computed missing_report string on stdin
and a canonical set of env overrides for testability (CRONTAB_MON_KANBAN_BOARD,
CRONTAB_MON_KANBAN_ASSIGNEE, CRONTAB_MON_KANBAN_LEDGER).

Exit 0 = routing handled cleanly (card created / commented / resolved / or healthy
with no open card). Exit 2 = an operational error occurred that the scheduler must
alert on the same tick (so a broken router is never silent).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
BOARD = os.environ.get("CRONTAB_MON_KANBAN_BOARD", "sycode-trading")
ASSIGNEE = os.environ.get("CRONTAB_MON_KANBAN_ASSIGNEE", "devops")
CREATED_BY = "system-crontab-watchdog"
LEDGER_PATH = Path(os.environ.get(
    "CRONTAB_MON_KANBAN_LEDGER",
    "/home/frank/.hermes/state/system-crontab-watchdog-kanban-ledger.json",
))
# Statuses where the router is allowed to auto-complete. If a human already
# claimed the card (in_progress) we leave it open and just note the recovery.
OPEN_STATUSES = ("ready", "todo", "running", "blocked", "review")
ACTIVE_STATUSES = ("ready", "todo")  # eligible for auto-complete on resolve

# Mirror the watchdog's key derivation so the router and its alert throttle
# stay in lockstep. The watchdog keys on:  sort | md5sum | first 12 hex chars.
import re as _re

_WATCHDOG_KEY_RE = _re.compile(r"^targets_([0-9a-f]{12})=")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_key(missing_report: str) -> str:
    """Reproduce the watchdog's `targets_<md5>` key from the missing report.

    Mirrors the shell: ``sort`` then ``md5sum`` then first 12 hex chars. We split
    on newlines, sort the lines, and md5 the joined result. We do NOT .strip()
    the whole string first — that lops leading whitespace off the first line only,
    breaking order-invariance (the watchdog's shell ``sort`` treats each line
    atomically including its indentation). Instead we rstrip trailing whitespace
    only and filter blank lines.
    """
    lines = [ln for ln in missing_report.rstrip().splitlines() if ln.strip()]
    sorted_report = "\n".join(sorted(lines))
    h = hashlib.md5(sorted_report.encode("utf-8")).hexdigest()[:12]
    return f"targets_{h}"


def derive_key_from_state(state_text: str) -> str | None:
    """Pull the live key straight out of the watchdog's MON_STATE text.

    The state file holds lines like ``targets_646a203dd4b0=1785347920``. We match
    the most recent one so the router keys on exactly what just alerted. Returns
    None when no key line is present (caller falls back to derive_key()).
    """
    for line in reversed(state_text.splitlines()):
        m = _WATCHDOG_KEY_RE.match(line.strip())
        if m:
            return f"targets_{m.group(1)}"
    return None


# ---------------------------------------------------------------------------
# Ledger (atomic save/load, same discipline as anomaly_ledger.py)
# ---------------------------------------------------------------------------
def load_ledger(path: Path | None = None) -> dict:
    """Load the ledger. ``path`` resolves at CALL time (defaults to the module
    global LEDGER_PATH) so tests can repoint it via ``global LEDGER_PATH``."""
    path = Path(path or LEDGER_PATH)
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            data = {"version": 1, "entries": {}}
        data.setdefault("entries", {})
        data.setdefault("version", 1)
        return data
    except Exception:
        try:
            path.rename(path.with_suffix(path.suffix + ".corrupt"))
        except Exception:
            pass
        return {"version": 1, "entries": {}}


def save_ledger(ledger: dict, path: Path | None = None) -> None:
    """Save the ledger. ``path`` resolves at CALL time, same as load_ledger."""
    path = Path(path or LEDGER_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Hermes CLI wrapper (bounded retry, same discipline as security_audit router)
# ---------------------------------------------------------------------------
def _run_hermes(args: list[str], timeout: int = 30, attempts: int = 2,
                base_delay: float = 2.0) -> subprocess.CompletedProcess | None:
    """Run hermes CLI; retry transient failures (timeout/exception). Returns None on give-up."""
    import time
    env = os.environ.copy()
    env["HERMES_HOME"] = "/home/frank/.hermes"
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                [HERMES, *args], capture_output=True, text=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return None
        except Exception:
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return None
    return None


def _extract_task_id(stdout: str) -> str | None:
    try:
        data = json.loads(stdout.strip())
        if isinstance(data, dict):
            return data.get("id") or data.get("task_id")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("id") or data[0].get("task_id")
    except Exception:
        pass
    m = _re.search(r"\b(t_[0-9a-f]{8,})\b", stdout)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Card operations
# ---------------------------------------------------------------------------
CLOSED_STATUSES = ("done", "archived")


def _card_status(task_id: str) -> str | None:
    """Read a task's status directly from the board sqlite DB (no LLM, like anomaly_ledger)."""
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def existing_open_card(key: str) -> str | None:
    """Return the id of an open card for this watchdog key, or None.

    Direct read-only sqlite lookup. NOTE: ``hermes kanban list --json`` does NOT
    include the ``idempotency_key`` column (verified 2026-08-10, t_dc22ee7e), so
    a CLI-list-based scan can never match — the lookup must hit the tasks table.
    """
    db = Path("/home/frank/.hermes/kanban/boards") / BOARD / "kanban.db"
    if not db.exists():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        row = con.execute(
            "SELECT id FROM tasks WHERE idempotency_key=? AND created_by=? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
            (key, CREATED_BY, *OPEN_STATUSES),
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def state_keys(state_text: str) -> list[str]:
    """All distinct ``targets_<md5>`` keys present in the watchdog MON_STATE file.

    The state file is the durable record of every UNHEALTHY key the watchdog has
    throttled. On a healthy tick it tells us which open cards may still be owed a
    RESOLVED comment even when the router's own ledger was lost/cleared.
    """
    keys: list[str] = []
    for line in state_text.splitlines():
        m = _WATCHDOG_KEY_RE.match(line.strip())
        if m:
            full = f"targets_{m.group(1)}"
            if full not in keys:
                keys.append(full)
    return keys


def create_card(key: str, missing_report: str, occurrence: int = 1,
                force_new: bool = False) -> str | None:
    """Create one kanban card for a missing-scripts anomaly. Returns task id or None.

    ``force_new=True`` drops the idempotency key so a recurrence after a
    resolved/closed card opens a genuinely fresh card instead of resurrecting
    the old episode.
    """
    # Parse the missing report into structured lines for the body.
    lines = [ln for ln in missing_report.strip().splitlines() if ln.strip()]
    missing_files = []
    for ln in lines:
        for prefix in ("MISSING:", "NOT-EXECUTABLE:", "NOT-READABLE:"):
            if prefix in ln:
                missing_files.append(ln)
                break
    report_lines = "\n".join(f"- {ln}" for ln in missing_files) if missing_files else missing_report.strip()

    title = f"MONITOR→ACTION: system crontab UNHEALTHY — {len(missing_files)} missing/unrunnable script(s)"
    if occurrence > 1:
        title = f"MONITOR→ACTION: system crontab UNHEALTHY (recurrence #{occurrence}) — {len(missing_files)} missing/unrunnable script(s)"
    body = "\n".join([
        "Auto-routed by `system-crontab-watchdog` from its UNHEALTHY branch "
        "(task t_dc22ee7e). The host crontab invokes scripts that are gone or "
        "unrunnable — those jobs exit 127 every tick and silently produce nothing. "
        "Alerts remain as notification; the board now owns remediation.",
        "",
        f"Dedupe key: `{key}`",
        f"Host: `{os.uname().nodename if hasattr(os, 'uname') else 'dgx'}`",
        f"Detected: {utc_now_iso()}",
        "",
        "Missing / unrunnable script(s):",
        report_lines,
        "",
        "Restore from a `.bak` copy in the same directory if one exists, then confirm "
        "the job's output file starts moving again. Check: `crontab -l`; "
        "`ls -la ~/.hermes/scripts/`.",
        "",
        "Acceptance: restore or remove each missing/unrunnable script entry so this "
        "watchdog returns to HEALTHY and no UNHEALTHY kanban card remains open. "
        "Re-run `system-crontab-watchdog.sh` to verify `[SILENT] system crontab healthy`.",
    ])

    args = [
        "kanban", "--board", BOARD, "create", title,
        "--assignee", ASSIGNEE,
        "--priority", "70",
        "--created-by", CREATED_BY,
        "--body", body,
        "--json",
    ]
    if not force_new:
        args += ["--idempotency-key", key]
    proc = _run_hermes(args)
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"CRONTAB_MON_KANBAN_CREATE_FAIL key={key} err={err[:300]}", file=sys.stderr)
        return None
    tid = _extract_task_id(proc.stdout)
    if not tid:
        print(f"CRONTAB_MON_KANBAN_CREATE_FAIL key={key} no_id_in_output={proc.stdout[:300]}", file=sys.stderr)
        return None
    return tid


def append_comment(task_id: str, key: str, missing_report: str, occurrence: int) -> bool:
    """Append a fresh occurrence comment to an existing open card."""
    lines = [ln for ln in missing_report.strip().splitlines() if ln.strip()]
    report_lines = "\n".join(f"- {ln}" for ln in lines) if lines else missing_report.strip()
    body = "\n".join([
        f"[system-crontab-watchdog occurrence #{occurrence} @ {utc_now_iso()} — still UNHEALTHY]",
        "",
        "Current missing / unrunnable script set (re-alert throttled separately by the watchdog):",
        report_lines,
    ])
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"CRONTAB_MON_KANBAN_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    return True


def resolve_card(task_id: str, key: str) -> bool:
    """Append a RESOLVED comment + auto-complete if the card is still in an active state."""
    ts = utc_now_iso()
    body = f"RESOLVED: system crontab returned HEALTHY as of {ts}. All previously missing/unrunnable script(s) for key `{key}` are now present and runnable."
    proc = _run_hermes(["kanban", "--board", BOARD, "comment", task_id, body])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
        print(f"CRONTAB_MON_KANBAN_RESOLVE_COMMENT_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
        return False
    status = _card_status(task_id)
    if status in ACTIVE_STATUSES:
        proc2 = _run_hermes([
            "kanban", "--board", BOARD, "complete", task_id,
            "--summary", f"system-crontab-watchdog self-heal: key {key} returned HEALTHY @ {ts}.",
        ])
        if proc2 is None or proc2.returncode != 0:
            err = (proc2.stderr or proc2.stdout or "") if proc2 else "timeout"
            print(f"CRONTAB_MON_KANBAN_RESOLVE_COMPLETE_FAIL task={task_id} err={err[:300]}", file=sys.stderr)
            return False
    elif status is not None:
        # A human already picked it up; leave it open, just note the recovery.
        note = f"system-crontab-watchdog: key {key} returned HEALTHY @ {ts}, but task already in '{status}'; left open for owner."
        _run_hermes(["kanban", "--board", BOARD, "comment", task_id, note])
    return True


# ---------------------------------------------------------------------------
# Public: process one watchdog tick's outcome
# ---------------------------------------------------------------------------
def process_tick(*, healthy: bool, missing_report: str, state_text: str) -> dict:
    """Drive one router tick given the watchdog's health outcome.

    ``missing_report``: text block from the watchdog's MISSING/NOT-EXECUTABLE lines
    ``state_text``:    raw contents of the watchdog MON_STATE file (to extract the
                       live throttle key)
    ``healthy``:       True = watchdog exited 0 / printed [SILENT]; False = UNHEALTHY

    Returns a dict: {action, key, task_id?, occurrence?}
    """
    ledger = load_ledger()
    entries = ledger.setdefault("entries", {})

    if not healthy:
        key = derive_key_from_state(state_text) or derive_key(missing_report)
        entry = entries.get(key)
        if entry is not None:
            tid = entry.get("task_id")
            # If the ledger points at a closed/vanished card (recurrence after
            # self-heal, manual completion, archived board row) it is stale —
            # drop it and open a fresh card for THIS episode.
            if _card_status(tid) in CLOSED_STATUSES or _card_status(tid) is None:
                del entries[key]
                save_ledger(ledger)
                entry = None
        if entry is None:
            # Ledger miss: probe the board directly. A prior run may have left
            # an open card that the (now-cleared) ledger forgot, OR the
            # idempotency key may still resolve to a closed card from last time.
            existing = existing_open_card(key)
            if existing:
                # Re-hydrate the ledger so subsequent ticks dedupe again.
                entries[key] = {
                    "task_id": existing,
                    "report_class": "system-crontab-watchdog",
                    "key": key,
                    "assignee": ASSIGNEE,
                    "board": BOARD,
                    "first_seen": utc_now_iso(),
                    "last_seen": utc_now_iso(),
                    "occurrences": 1,
                    "first_fingerprint": _fingerprint(missing_report),
                    "last_fingerprint": _fingerprint(missing_report),
                }
                save_ledger(ledger)
                entry = entries[key]
        if entry is not None:
            entry["occurrences"] = int(entry.get("occurrences", 1)) + 1
            entry["last_seen"] = utc_now_iso()
            entry["last_fingerprint"] = _fingerprint(missing_report)
            save_ledger(ledger)
            tid = entry["task_id"]
            ok = append_comment(tid, key, missing_report, entry["occurrences"])
            return {"action": "deduped", "key": key, "task_id": tid,
                    "occurrences": entry["occurrences"], "commented": ok}
        # First detection (no ledger, no open board card) -> create.
        tid = create_card(key, missing_report, occurrence=1)
        if tid is None:
            return {"action": "create_failed", "key": key}
        if _card_status(tid) in CLOSED_STATUSES:
            # The idempotency key resolved to a previously-closed card of the
            # same report (kanban returns the non-archived task for the key).
            # Open a genuinely fresh card so the board owns THIS episode.
            tid = create_card(key, missing_report, occurrence=1, force_new=True)
            if tid is None:
                return {"action": "create_failed", "key": key}
        entries[key] = {
            "task_id": tid,
            "report_class": "system-crontab-watchdog",
            "key": key,
            "assignee": ASSIGNEE,
            "board": BOARD,
            "first_seen": utc_now_iso(),
            "last_seen": utc_now_iso(),
            "occurrences": 1,
            "first_fingerprint": _fingerprint(missing_report),
            "last_fingerprint": _fingerprint(missing_report),
        }
        save_ledger(ledger)
        return {"action": "created", "key": key, "task_id": tid, "occurrences": 1}

    # Healthy: resolve any lingering open card + ledger entry for every key that
    # is no longer reporting failures.
    #  1) Ledger entries (normal path).
    #  2) Board reconcile: open cards whose idempotency_key appears in the
    #     watchdog state file but that have NO ledger entry (ledger lost/cleared
    #     mid-episode, e.g. manual reset). The state file is the durable record
    #     of the last UNHEALTHY key, so a cleared ledger must not strand a card.
    resolved = []
    resolved_tids = set()
    for key in list(entries.keys()):
        entry = entries[key]
        tid = entry["task_id"]
        ok = resolve_card(tid, key)
        if ok:
            resolved_tids.add(tid)
            resolved.append(tid)
        del entries[key]
    # Board reconcile: open cards whose key appears in the watchdog state file
    # but that have NO ledger entry (ledger lost/cleared mid-episode). The
    # state file is the durable record of the last UNHEALTHY key.
    for key in state_keys(state_text):
        if key in entries:
            continue
        tid = existing_open_card(key)
        if tid and tid not in resolved_tids:
            ok = resolve_card(tid, key)
            if ok:
                resolved_tids.add(tid)
                resolved.append(tid)
    if resolved or entries:
        save_ledger(ledger)
    return {"action": "resolved", "keys": resolved, "task_ids": resolved} if resolved \
        else {"action": "no_entry"}


def _fingerprint(missing_report: str) -> str:
    return hashlib.md5(missing_report.strip().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    selftest = "--selftest" in argv

    if selftest:
        return _selftest()

    # The watchdog pipes the missing_report block on stdin and sets env vars
    # describing its outcome. See system-crontab-watchdog.sh send_alert_branch.
    missing_report = sys.stdin.read() if not sys.stdin.isatty() else ""

    # CRONTAB_MON_HEALTHY=1 means the watchdog found everything OK.
    healthy = os.environ.get("CRONTAB_MON_HEALTHY", "0") == "1"
    # The watchdog writes the live throttle state file path here so we can
    # extract the exact key it just used.
    state_file = os.environ.get("CRONTAB_MON_STATE_FILE", "")
    state_text = ""
    if state_file and os.path.exists(state_file):
        try:
            state_text = Path(state_file).read_text(encoding="utf-8", errors="replace")
        except Exception:
            state_text = ""

    if dry:
        # Dry-run: compute what would happen, print plan, don't mutate the board.
        key = derive_key_from_state(state_text) or (derive_key(missing_report) if missing_report else None)
        if healthy:
            print(f"CRONTAB_MON_DRY_RUN action=resolve key={key or 'all-entries'} healthy=True")
        else:
            if not key:
                print("CRONTAB_MON_DRY_RUN action=create missing_report_empty")
                return 0
            # Check if a card already exists for this key (read-only probe).
            existing = existing_open_card(key) if not dry else None
            action = "append" if existing else "create"
            print(f"CRONTAB_MON_DRY_RUN action={action} key={key} task_id={existing or 'NEW'}")
        return 0

    try:
        result = process_tick(healthy=healthy, missing_report=missing_report, state_text=state_text)
        print(f"CRONTAB_MON_KANBAN_ROUTER {json.dumps(result, sort_keys=True)}")
        return 0
    except Exception as exc:
        print(f"CRONTAB_MON_KANBAN_ROUTER_FAILURE {exc}", file=sys.stderr)
        return 2


def _selftest() -> int:
    """Deterministic test: key derivation + create/dedup/resolve flows with a FakeHarness.

    Runs fully offline (no hermes CLI), mirrors anomaly_ledger_selftest.py shape.
    """
    failures = []

    # 1. Key derivation is deterministic: same input -> same key.
    #    Order-invariance is a property of the watchdog's `sort` step, which
    #    derive_key() already applies before md5, mirroring the shell exactly.
    report = "  MISSING: /home/frank/scripts/foo.sh\n  MISSING: /home/frank/scripts/bar.py"
    k1 = derive_key(report)
    k2 = derive_key(report)
    if k1 != k2:
        failures.append(f"key not deterministic: {k1} != {k2}")
    if not k1.startswith("targets_"):
        failures.append(f"key prefix wrong: {k1}")
    # Verify order-invariance: the watchdog sorts before hashing.
    k3 = derive_key("  MISSING: /home/frank/scripts/bar.py\n  MISSING: /home/frank/scripts/foo.sh")
    if k1 != k3:
        failures.append(f"key not order-invariant: {k1} != {k3}")

    # 2. derive_key_from_state extracts the live key from MON_STATE lines.
    state = "targets_646a203dd4b0=1785347920\nother=1\n"
    sk = derive_key_from_state(state)
    if sk != "targets_646a203dd4b0":
        failures.append(f"derive_key_from_state wrong: {sk}")

    # 2b. state_keys extracts ALL distinct keys (healthy-branch reconcile input).
    all_keys = state_keys("targets_646a203dd4b0=1785347920\ntargets_abc123def456=1786000000\ntargets_646a203dd4b0=1786100000\n")
    if all_keys != ["targets_646a203dd4b0", "targets_abc123def456"]:
        failures.append(f"state_keys wrong: {all_keys}")

    # 3. process_tick: first detection creates; second dedupes; healthy resolves.
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    global LEDGER_PATH
    LEDGER_PATH = tmp / "ledger.json"

    # Override hermes calls by monkeypatching the module-level functions.
    created = []
    commented = []
    completed = []
    statuses = {}

    def fake_run(args, timeout=30, attempts=2, base_delay=2.0):
        if "create" in args:
            tid = f"t_selftest{len(created)+1:04d}"
            created.append((args, tid))
            statuses.setdefault(tid, "ready")  # honor pre-seeded closed status
            out = json.dumps({"id": tid})
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")
        if "comment" in args:
            # args: ["kanban", "--board", BOARD, "comment", task_id, body]
            tid = args[4]
            commented.append((tid, args[-1]))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if "complete" in args:
            # args: ["kanban", "--board", BOARD, "complete", task_id, "--summary", "..."]
            idx = args.index("complete")
            tid = next((a for a in args[idx + 1:] if not a.startswith("--")), None)
            if tid:
                completed.append(tid)
                statuses[tid] = "completed"
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    g = globals()
    real_run_hermes = g.get("_run_hermes")
    real_card_status = g.get("_card_status")
    real_existing = g.get("existing_open_card")
    g["_run_hermes"] = fake_run

    def fake_status(task_id):
        return statuses.get(task_id, "ready")

    g["_card_status"] = fake_status
    # Board scan fake: no open cards by default; tests override as needed.
    g["existing_open_card"] = lambda key: None

    try:
        # First detection.
        res1 = process_tick(healthy=False, missing_report=report, state_text=state)
        if res1["action"] != "created":
            failures.append(f"first tick should create, got {res1}")
        if len(created) != 1:
            failures.append(f"expected 1 create, got {len(created)}")

        # Second detection (same key) -> deduped.
        res2 = process_tick(healthy=False, missing_report=report, state_text=state)
        if res2["action"] != "deduped":
            failures.append(f"second tick should dedupe, got {res2}")
        if len(created) != 1:
            failures.append(f"no second card should be created, got {len(created)}")
        if len(commented) != 1:
            failures.append(f"expected 1 comment on dedupe, got {len(commented)}")

        # Healthy -> resolve + auto-complete (ledger path).
        res3 = process_tick(healthy=True, missing_report="", state_text=STATE)
        if res3["action"] != "resolved":
            failures.append(f"healthy tick should resolve, got {res3}")
        if len(completed) != 1:
            failures.append(f"expected 1 auto-complete, got {len(completed)}")
        # No duplicate resolution when key is in BOTH ledger and state file.
        if len(res3["task_ids"]) != 1:
            failures.append(f"no duplicate resolve expected, got task_ids={res3['task_ids']}")

        # Healthy tick with a state-file key but NO ledger entry -> board
        # reconcile must resolve the open card (ledger-loss path). This is the
        # exact gap seen in production on 2026-08-10: ledger was empty while
        # card t_71861496 stayed open.
        reconcile_tid = "t_reconcile1"
        g["existing_open_card"] = lambda key: reconcile_tid if key == "targets_abc123def456" else None
        statuses[reconcile_tid] = "ready"
        state2 = "targets_abc123def456=1786000000\ntargets_646a203dd4b0=1785347920\n"
        res4 = process_tick(healthy=True, missing_report="", state_text=state2)
        if res4["action"] != "resolved":
            failures.append(f"healthy reconcile should resolve from state file, got {res4}")
        if reconcile_tid not in completed:
            failures.append(f"reconcile card should be auto-completed, completed={completed}")

        # Stale ledger entry pointing at a CLOSED card -> recurrence opens a
        # fresh card instead of commenting on the dead one.
        closed_tid = "t_deadbeef"
        ledger = load_ledger()
        ledger["entries"]["targets_646a203dd4b0"] = {
            "task_id": closed_tid, "key": "targets_646a203dd4b0",
            "assignee": ASSIGNEE, "board": BOARD,
            "first_seen": "2026-08-01T00:00:00Z", "last_seen": "2026-08-01T00:00:00Z",
            "occurrences": 3,
        }
        save_ledger(ledger)
        statuses[closed_tid] = "done"
        before_created = len(created)
        res5 = process_tick(healthy=False, missing_report=report, state_text=state)
        if res5["action"] != "created":
            failures.append(f"stale-ledger tick should create fresh, got {res5}")
        if len(created) != before_created + 1:
            failures.append(f"expected one new card, created={len(created)} before={before_created}")
        if res5["task_id"] == closed_tid:
            failures.append("new card must not reuse the closed card id")
        if any(t == closed_tid for t, _ in commented):
            failures.append(f"closed card must not get comments, commented={commented}")
        if load_ledger()["entries"].get("targets_646a203dd4b0", {}).get("task_id") == closed_tid:
            failures.append("ledger must be re-populated with the new card")
        after_res5 = len(created)

        # Idempotency create returning a CLOSED card -> force_new opens fresh.
        g["existing_open_card"] = lambda key: None  # reset reconcile fake
        state3 = "targets_face12345678=1786100000\n"
        idem_closed_tid = f"t_selftest{len(created)+1:04d}"  # next fake create id
        statuses[idem_closed_tid] = "done"  # idempotent path lands on a closed card
        res6 = process_tick(healthy=False, missing_report=report, state_text=state3)
        if res6["action"] != "created":
            failures.append(f"closed-idem tick should create fresh, got {res6}")
        if res6["task_id"] == idem_closed_tid:
            failures.append(f"force_new must not return the closed card, got {res6['task_id']}")
        if len(created) != after_res5 + 2:
            failures.append(f"expected idempotent + force_new creates, created={len(created)} after_res5={after_res5}")
    finally:
        g["_run_hermes"] = real_run_hermes
        g["_card_status"] = real_card_status
        g["existing_open_card"] = real_existing
        LEDGER_PATH = tmp / "ledger.json"

    if failures:
        print("SELFTEST_FAIL")
        for f in failures:
            print(f" - {f}")
        return 1
    print(f"SELFTEST_PASS key_stable created={len(created)} deduped=1 resolved=2 closed_reuse=2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
