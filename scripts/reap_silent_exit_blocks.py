#!/usr/bin/env python3
"""Reaper for the silent-exit false-block class (t_74c6693e).

ROOT CAUSE
----------
In detect_crashed_workers() the engine force-trips failure_limit=1 (immediate
auto-block) whenever a worker exits rc=0 without calling kanban_complete /
kanban_block. The catch-silent-exit.sh subagent_stop hook is only a *post-hoc
alert* and cannot block, so the reaper alone decides fate.

Evidence (2026-07-17 probe across 3 boards, 61 affected cards) shows these are
NOT "finished but forgot to complete" — the FAILURE-CLASSIFIER-AUTO comments
carry failure_class=provider_pre_reasoning / provider_error. The worker died at
the PROVIDER stage (pre-reasoning / API auth) BEFORE executing task logic, so
re-dispatching is correct and a hard block is a misclassification. A few cards
were ALSO independently verified complete (os-reviewer APPROVED verdicts,
ELON VERIFIED-COMPLETE) but left stuck in blocked.

This reaper closes BOTH halves and guarantees the acceptance predicate
"zero cards with (last_failure_error = silent-exit string AND status=blocked)"
holds for every card:

  1. done        -> verified-completion evidence present -> mark done.
  2. redrive     -> silent-exit, no real human gate -> re-drive to ready
                    (reset failure counter + clear the mislabel error).
  3. relabel_gate-> silent-exit, but a REAL human gate exists (Frank/A3/
                    os-reviewer sign-off/credentials) -> keep blocked, but
                    rewrite last_failure_error to an honest note so the
                    phantom (error,blocked) pair is gone and the CI gate passes.
  4. skip_other  -> anything not matching the silent-exit error -> untouched.

Idempotent: re-running after a card is already done/ready/relabeled is a no-op.
Safe: never deletes rows; never re-drives a card that carries a real gate.

Usage:
  reap_silent_exit_blocks.py              # dry-run, prints plan, exits 0
  reap_silent_exit_blocks.py --apply      # writes the plan
  reap_silent_exit_blocks.py --apply --board jarvis-os
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time

BOARDS_ROOT = "/home/frank/.hermes/kanban/boards"
# NOTE: sycode-ai was missing here on 2026-08-02 — the reaper silently never
# touched sycode-ai cards, so the provider-API-failure recurrence (t_85378990)
# sat dead in `blocked` forever. upero and sycode-ai are aliases of the same
# board backing; include BOTH so neither name is skipped.
BOARDS = ["jarvis-os", "sycode-trading", "yorkstone-supplies", "upero", "sycode-ai"]

# A healthy non-Nous provider to fail over to when a card's silent-exit block
# is actually a provider/API death. Nous free-tier was the 2026-08-02 outage
# source; openai-codex has a live key and is used by the engine/spawn fallback.
FAILOVER_PROVIDER = os.environ.get("SILENT_EXIT_REAPER_FAILOVER_PROVIDER", "openai-codex")
FAILOVER_HEALTHY_SUBSTR = ("inference-api.nousresearch.com",)

# The exact dispatcher error string (engine: hermes_cli/kanban_db.py:6605)
SILENT_EXIT_ERR = "worker exited cleanly (rc=0) without calling kanban_complete or kanban_block"

# Evidence markers that a blocked card was actually finished (checked FIRST).
# Strict: require INDEPENDENT verification (os-reviewer / governor / external
# review), NOT implementer self-approval. t_59094ca1 proved the danger — its
# comments say the 3 APPROVED markers are implementer SELF-approval and must
# not be treated as done.
DONE_EVIDENCE = re.compile(
    r"(REVIEW_VERDICT\s*=\s*APPROVED|os-reviewer\s+APPROVED|VERIFIED-COMPLETE|"
    r"Independent recovery verification|verification.*now contains the intended|"
    r"Unblock note|completed and verified|verified complete|"
    r"independent (static )?verification|independent (static )?review)",
    re.I,
)
# Marker that the approval is the implementer's own, not independent.
SELF_APPROVAL = re.compile(r"self[- ]approval|implementer.*(approv|self)|SELF-APPROV", re.I)

# Independent approval/review is not terminal completion when a later comment
# says the lane is still waiting on CI, merge/landing, a fresh verdict, or a
# safety/runtime gate. t_b863fa49 (PR #588 open, red CI) proved that broad
# ``independent review`` evidence is unsafe as a standalone done signal.
NOT_DONE_GATE = re.compile(
    r"(CHANGES_REQUESTED|remains blocked|canonical owner remains blocked|"
    r"run_(status|outcome)=blocked|"
    r"awaiting (CI|merge|landing|review|risk|verdict|approval|human|pm)|"
    r"CI[- ]gated|CI .*?(fail|red|blocked|queued|pending|unstable)|"
    r"mergeStateStatus\s*=\s*(BLOCKED|BEHIND|UNSTABLE)|"
    r"PR #?\d+.*?(OPEN|BLOCKED|BEHIND|UNSTABLE|FAIL|red)|"
    r"auto-merge is disabled|not merged|unmerged|open PR|"
    r"STAGED_NOT_INSTALLED|runtime (is )?(still )?(unchanged|staged)|"
    r"no (merge|land|install|runtime|deploy|restart|DB|trade_intents|live action).*authorized|"
    r"fresh (risk|OS|review) verdict|provider-auth wall|"
    r"lifecycle .*?impossible|must re-read)",
    re.I,
)

# A REAL human/A3/dependency/Frank gate must NOT be auto-reaped. These are
# explicit, not incidental: bare "A3" or "gate" alone does NOT count.
REAL_GATE = re.compile(
    r"(needs_input|frank gate|escalate to frank|needs frank|frank must|"
    r"R3 gate|A3 ESCALATION|production deploy|credentials required|live trading|"
    r"awaiting (frank|approval|human|pm|review)|human gate|manual gate|"
    r"awaiting (CI|merge|landing|risk|verdict)|CI[- ]gated|"
    r"mergeStateStatus\s*=\s*(BLOCKED|BEHIND|UNSTABLE)|"
    r"sign-off required|blocked:.*frank|escalation.*frank|"
    r"canonical owner remains blocked|remains blocked|provider-auth wall)",
    re.I,
)

# The auto-classifier comment that betrays a provider-stage death.
PROVIDER_DEATH = re.compile(r"failure_class=(provider\w+|provider_error|protocol_violation)", re.I)

# The new engine event error emitted for a clean rc=0 whose worker log shows a
# provider/API failure (429/404/auth/connection/credit-cap). t_85378990 fix.
PROVIDER_API_FAILURE_ERR = (
    "worker exited cleanly (rc=0) but the MODEL API CALL FAILED "
    "(provider/API failure, NOT a protocol violation)"
)

# Provider/API failure markers in a worker log (reused from the engine regex
# intent). Used to detect the LEGACY silent-exit auto-block class that is
# actually a provider death (before the engine fix shipped to live).
_PROVIDER_API_FAILURE_RE_TAIL = re.compile(
    r"(rate[_\s-]?limit|429|too many requests|"
    r"404|model[^\n]{0,60}not found|requires available credits|"
    r"credit access paused|account balance|"
    r"\b401\b|\b403\b|unauthorized|forbidden|"
    r"api call failed|api error|connection error|connecterror|"
    r"timeout|timed out|exceeded the rate limit|"
    r"inference-api\.nousresearch\.com|provider:\s*nous|"
    r"quota|billing|subscription|"
    r"rate limited after|max retries.*exhausted)",
    re.IGNORECASE,
)


def _worker_log_tail(tid: str, c: sqlite3.Connection, max_bytes: int = 8192) -> str:
    """Best-effort tail of a task's worker log (same layout as the engine)."""
    try:
        rows = c.execute("PRAGMA database_list").fetchall()
        db_file = None
        for r in rows:
            if r[1] == "main":
                db_file = r[2]
                break
        if not db_file or not str(db_file).endswith("kanban.db"):
            return ""
        log_path = os.path.join(
            os.path.dirname(str(db_file)), "logs", f"{tid}.log"
        )
        if not os.path.exists(log_path):
            return ""
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _conn(board: str) -> sqlite3.Connection:
    db = os.path.join(BOARDS_ROOT, board, "kanban.db")
    if not os.path.exists(db):
        raise FileNotFoundError(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _recent_comments(c: sqlite3.Connection, tid: str, n: int = 10) -> list[str]:
    rows = c.execute(
        "SELECT COALESCE(body,'') AS b FROM task_comments "
        "WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
        (tid, n),
    ).fetchall()
    return [r["b"] for r in rows]


def _recent_run_notes(c: sqlite3.Connection, tid: str, n: int = 5) -> list[str]:
    """Return compact recent run evidence for classification.

    A task can carry a stale silent-exit ``last_failure_error`` even after a
    later worker correctly called ``kanban_block``. In that case the durable run
    history, not broad comment text, is the explicit lifecycle signal and must
    keep the card blocked while only the stale failure label is cleared.
    """
    rows = c.execute(
        "SELECT COALESCE(status,'') AS status, COALESCE(outcome,'') AS outcome, "
        "COALESCE(error,'') AS error, COALESCE(summary,'') AS summary "
        "FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?",
        (tid, n),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        out.append(
            "run_status={status} run_outcome={outcome} run_error={error} "
            "run_summary={summary}".format(
                status=r["status"],
                outcome=r["outcome"],
                error=r["error"],
                summary=r["summary"],
            )
        )
    return out


def _classify(c: sqlite3.Connection, tid: str) -> str:
    """Return: done | redrive | relabel_gate | skip_other."""
    row = c.execute(
        "SELECT title, body, status, assignee, consecutive_failures, "
        "COALESCE(last_failure_error,'') AS err FROM tasks WHERE id=?",
        (tid,),
    ).fetchone()
    if row is None:
        return "skip_other"

    err = row["err"] or ""
    # Act on the silent-exit auto-block class OR the new provider-API-failure
    # class (t_85378990). The provider-API-failure class is, by construction,
    # a retryable provider death and must be re-driven (with failover).
    if SILENT_EXIT_ERR not in err and PROVIDER_API_FAILURE_ERR not in err:
        return "skip_other"

    comments = _recent_comments(c, tid)
    run_notes = _recent_run_notes(c, tid)
    blob = "\n".join(comments + run_notes)

    # 1) A later explicit gate wins over broad prior approval language. This
    # prevents open/red PRs or fresh review-waits from being marked done just
    # because an older independent review was APPROVED.
    if NOT_DONE_GATE.search(blob) or NOT_DONE_GATE.search(row["body"] or ""):
        return "relabel_gate"

    # 2) Verified complete wins when no later gate is visible.
    if DONE_EVIDENCE.search(blob) and not SELF_APPROVAL.search(blob):
        return "done"

    # 3) Real human gate -> keep blocked but relabel the mislabel.
    if REAL_GATE.search(blob) or REAL_GATE.search(row["body"] or ""):
        return "relabel_gate"

    # 4) Provider-stage death (new provider-API-failure class, or the legacy
    # silent-exit error whose worker log shows a provider/API failure) -> re-drive.
    worker_log = _worker_log_tail(tid, c)
    if PROVIDER_API_FAILURE_ERR in err or (
        SILENT_EXIT_ERR in err
        and worker_log
        and _PROVIDER_API_FAILURE_RE_TAIL.search(worker_log)
    ):
        return "redrive_provider"
    # 5) Ambiguous silent-exit (no provider evidence) -> re-drive (legacy behavior).
    return "redrive"


def plan(boards: list[str]) -> dict:
    actions: dict[str, list[tuple[str, str, str]]] = {
        "done": [], "redrive": [], "redrive_provider": [],
        "relabel_gate": [], "skip_other": [],
    }
    # A card is in this reaper's scope if its failure error matches the legacy
    # silent-exit string OR the new provider-API-failure string (t_85378990).
    like_patterns = [f"%{SILENT_EXIT_ERR}%", f"%{PROVIDER_API_FAILURE_ERR}%"]
    for b in boards:
        try:
            c = _conn(b)
        except FileNotFoundError:
            continue
        seen: set[str] = set()
        for pat in like_patterns:
            rows = c.execute(
                "SELECT id, title, assignee FROM tasks WHERE status='blocked' "
                "AND last_failure_error LIKE ?",
                (pat,),
            ).fetchall()
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                cls = _classify(c, r["id"])
                actions[cls].append((b, r["id"], (r["title"] or "")[:60]))
        c.close()
    return actions


def _apply_one(db: str, board: str, tid: str, cls: str) -> None:
    c = sqlite3.connect(db)
    try:
        with c:  # implicit write txn
            now = int(time.time())
            if cls == "done":
                c.execute(
                    "UPDATE tasks SET status='done', completed_at=?, "
                    "last_failure_error=NULL, consecutive_failures=0 "
                    "WHERE id=? AND status='blocked'",
                    (now, tid),
                )
                kind, payload = "reaper_auto_done", {
                    "reason": "verified-complete evidence found; silent-exit "
                              "block was a misclassification",
                    "reaper": "t_74c6693e",
                }
                comment = (
                    "DISPOSITION silent-exit-reaper: verified-complete evidence "
                    "found; marking done and clearing stale rc=0-without-lifecycle "
                    "mislabel. reaper=t_74c6693e"
                )
            elif cls == "redrive_provider":
                c.execute(
                    "UPDATE tasks SET status='ready', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL, "
                    "consecutive_failures=0, last_failure_error=NULL "
                    "WHERE id=? AND status='blocked'",
                    (tid,),
                )
                # Fail over to a healthy provider so the retry does NOT land on
                # the same dead Nous free-tier endpoint. We set a task-level
                # provider_override via the hermes CLI (the same surface the
                # worker honors at spawn). Best-effort: a failure here does not
                # block the re-drive — the spawn-time provider fallback chain in
                # the engine still tries the configured fallbacks next attempt.
                failover_msg = ""
                try:
                    import subprocess
                    hermes_bin = os.environ.get("HERMES_BIN", "hermes")
                    res = subprocess.run(
                        [hermes_bin, "kanban", "set-model", "--provider",
                         FAILOVER_PROVIDER, tid],
                        capture_output=True, text=True, timeout=60,
                    )
                    if res.returncode == 0:
                        failover_msg = (
                            f" Set task provider_override -> {FAILOVER_PROVIDER}"
                            f" (healthy provider failover)."
                        )
                    else:
                        failover_msg = (
                            f" (provider_override set failed rc={res.returncode};"
                            f" spawn fallback chain still applies: {res.stderr[:120]})"
                        )
                except Exception as exc:  # never block the re-drive
                    failover_msg = f" (provider_override set error: {exc!r})"
                kind, payload = "reaper_redrive_provider", {
                    "reason": "clean rc=0 but MODEL API CALL FAILED "
                              "(provider/API death, not a protocol violation); "
                              "re-drive on a healthy provider",
                    "reaper": "t_74c6693e",
                    "failover_provider": FAILOVER_PROVIDER,
                }
                comment = (
                    "DISPOSITION silent-exit-reaper: worker exited rc=0 with no "
                    "terminal kanban call, but the worker log shows a provider/API "
                    "failure (429/404/auth/connection/credit-cap) — a RETRYABLE "
                    "provider error, not a protocol violation. Re-driving to ready "
                    f"and clearing the stale mislabel.{failover_msg} reaper=t_74c6693e"
                )
            elif cls == "redrive":
                c.execute(
                    "UPDATE tasks SET status='ready', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL, "
                    "consecutive_failures=0, last_failure_error=NULL "
                    "WHERE id=? AND status='blocked'",
                    (tid,),
                )
                kind, payload = "reaper_redrive", {
                    "reason": "provider-stage silent-exit; re-drive after "
                              "provider recovery instead of hard block",
                    "reaper": "t_74c6693e",
                }
                comment = (
                    "DISPOSITION silent-exit-reaper: provider-stage clean exit "
                    "with no real human gate; re-driving to ready and clearing "
                    "the stale rc=0-without-lifecycle mislabel. reaper=t_74c6693e"
                )
            else:  # relabel_gate
                c.execute(
                    "UPDATE tasks SET last_failure_error=? "
                    "WHERE id=? AND status='blocked'",
                    ("blocked: real human/A3 sign-off gate (prior "
                     "'silent-exit' auto-label cleared by reaper t_74c6693e)", tid),
                )
                kind, payload = "reaper_relabel_gate", {
                    "reason": "kept blocked for genuine gate; removed stale "
                              "silent-exit mislabel so CI gate passes",
                    "reaper": "t_74c6693e",
                }
                comment = (
                    "DISPOSITION silent-exit-reaper: kept blocked because a real "
                    "CI/merge/review/human/A3 gate is visible; clearing only the "
                    "stale rc=0-without-lifecycle last_failure_error so the phantom "
                    "block CI predicate is honest. reaper=t_74c6693e"
                )
            if cls in ("redrive", "redrive_provider"):
                # A blocked→ready re-drive must ALSO emit the
                # block-lifecycle 'unblocked' event. Without it the
                # dispatcher's _has_sticky_block() predicate stays True and
                # the re-driven card is ready-but-unspawnable — the exact
                # stall class fixed at the writer layer by t_20759186 /
                # t_e6bb0f1e. Include the original block reference so
                # auditors can correlate the unblock with the block that
                # opened it.
                last_bl = c.execute(
                    "SELECT id, kind, payload FROM task_events "
                    "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
                    "ORDER BY id DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                if last_bl is not None and last_bl["kind"] == "blocked":
                    unblock_payload = {
                        "reason": "reaper_redrive",
                        "reaper": "t_74c6693e",
                        "block_event_id": last_bl["id"],
                    }
                    try:
                        blk_payload = (
                            json.loads(last_bl["payload"])
                            if last_bl["payload"] else {}
                        )
                    except (json.JSONDecodeError, TypeError):
                        blk_payload = {}
                    for key in ("reason", "kind", "comment_id"):
                        if blk_payload.get(key) is not None:
                            unblock_payload.setdefault(f"block_{key}", blk_payload[key])
                    c.execute(
                        "INSERT INTO task_events (task_id, run_id, kind, payload, "
                        "created_at) VALUES (?, NULL, 'unblocked', ?, ?)",
                        (tid, json.dumps(unblock_payload), now),
                    )
            c.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, "
                "created_at) VALUES (?, NULL, ?, ?, ?)",
                (tid, kind, json.dumps(payload), now),
            )
            c.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (tid, "silent-exit-reaper", comment, now),
            )
            c.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, "
                "created_at) VALUES (?, NULL, ?, ?, ?)",
                (tid, "commented", json.dumps({"author": "silent-exit-reaper", "len": len(comment)}), now),
            )
    finally:
        c.close()


def apply(actions: dict) -> None:
    for cls in ("done", "redrive", "redrive_provider", "relabel_gate"):
        for b, tid, _ in actions[cls]:
            db = os.path.join(BOARDS_ROOT, b, "kanban.db")
            _apply_one(db, b, tid, cls)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the plan")
    ap.add_argument("--board", help="limit to one board")
    args = ap.parse_args()

    boards = [args.board] if args.board else BOARDS
    actions = plan(boards)

    print(f"[reap_silent_exit_blocks] mode={'APPLY' if args.apply else 'DRY-RUN'}")
    total = sum(len(v) for v in actions.values())
    print(f"  total silent-exit blocked cards: {total}")
    for cls in ("done", "redrive", "relabel_gate", "skip_other"):
        items = actions[cls]
        print(f"  {cls}: {len(items)}")
        for b, tid, t in items:
            print(f"      [{b}] {tid}  {t}")
    print()

    if not args.apply:
        print("  DRY-RUN: no changes written. Re-run with --apply to execute.")
        return 0

    apply(actions)
    print(f"  APPLIED: {len(actions['done'])} done, "
          f"{len(actions['redrive'])} re-driven, "
          f"{len(actions['relabel_gate'])} relabeled (gate preserved).")
    remaining = sum(
        1 for cls in ("skip_other",)
        for _ in actions[cls]
    )
    print(f"  Untouched (not silent-exit class): {remaining}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
