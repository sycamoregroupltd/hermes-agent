#!/usr/bin/env python3
"""SERVICE-GATE escalation watchdog — no_agent cron script.

Scans all kanban boards for tasks blocked >6h with block_kind='needs_input'
or title containing SERVICE-GATE/APPROVAL REQUIRED. For each qualifying task,
it finds-or-creates exactly ONE Frank escalation card on the jarvis-os board:

  - If an OPEN escalation card already exists for the same source task id, the
    watchdog HEARTBEATS it in place (appends a re-fire comment + refreshes
    last_heartbeat_at) instead of creating a new card.
  - If the only card(s) are done/archived but the source's CURRENT block
    episode began BEFORE the newest card was completed, the source never
    actually unblocked — the watchdog REOPENS that card rather than minting a
    duplicate (see the time-aware dedupe note below).
  - Only when no escalation exists, or the source demonstrably re-blocked after
    the newest escalation was resolved, does it create a new card.

Dedup is DATABASE-BACKED against the escalation board itself (the board is the
durable, self-healing source of truth). The previous implementation relied on a
fragile external JSON state file (governor_comment_dedupe.py ->
escalation_dedupe.json) that was being lost between runs, producing triplicate
FRANK ESCALATION cards (one per cron fire per still-blocked source). See
kanban t_71feadc2.

Time-aware dedupe (kanban t_9a621399): that DB-backed lookup replaced the lost
state file but filtered to open cards only (``status NOT IN ('done',
'archived')``), which reintroduced the SAME symptom through a new mechanism —
a PM resolving an escalation made it invisible to dedupe, so the next 30-minute
tick minted a fresh card while the source was still blocked. Census on
2026-08-01 found 8 cards for a single source, 7 done; cards minted == PM
completions + 1. Terminality is now a property of TIME, not status: a
done/archived escalation counts as terminal only when the source's block
episode began strictly AFTER that card's completed_at. Unknown completion
timestamps fail closed (reopen, never mint). MAX_ESCALATION_CARDS_PER_SOURCE is
a hard backstop so no future logic bug can produce another multi-card storm.

Storm guard: never escalate escalation tasks themselves, and keep a small
per-run / per-24h cap so a backlog of non-critical service gates cannot flood
Frank faster than PMs can triage it.

Orphan guard (kanban t_3ab9e690): before creating OR heartbeating any card the
watchdog verifies that (a) the source board's kanban.db exists on disk and
(b) the source task id still resolves in that DB. A reconciliation pass also
sweeps already-open escalation cards whose source board/task has since been
retired and parks them as blocked/transient with ONE explanatory comment,
instead of heartbeating an orphan forever. Mirrors the existing-path filter in
scripts/blocked-state-dispatch-guard.py::_find_boards.

Parked-source exclusion (kanban t_5956838b): a blocked source can be parked in
an explicit non-dispatchable state by its board owner with the canonical
comment marker ``PARKED: awaiting-absent-seat`` (orchestrator disposition,
jarvis seat 2026-08-03). A parked source is waiting on a seat that does not
exist on this host (e.g. Frank's signed macOS Blender on an aarch64 DGX) — no
agent here can ever clear it, so every re-fire is noise with zero new
information. The exclusion is a POSITIVE predicate: only the explicit parked
marker suppresses a source. A genuinely stalled source with a reachable owner
still escalates. The check is fail-safe: an unreadable/missing task_comments
table yields an empty parked set (no suppression), preserving pre-existing
behavior for that board.

Classification-before-budget (kanban t_4e8c2620): the per-candidate loop now
evaluates the block-age threshold and the orphan existence check BEFORE the
escalation rate limiter. Classification is read-only, so this cannot create or
heartbeat a card; the budget still governs writes only. The effect is that
``orphaned candidates skipped`` (and ``under threshold``) are fleet-wide
censuses rather than lower bounds capped by whatever budget happened to remain,
and ``rate-limited`` now means "escalation-eligible but over budget" rather than
"not yet examined". Existence lookups are served from a per-run
SourceExistenceCache (one task-id query per board) so classifying every
candidate does not cost one SQLite connection each.

Silent (empty stdout) when nothing needs escalation — watchdog pattern.

Usage:
    service_gate_escalation_watchdog.py [--dry-run]

``--dry-run`` performs every read and prints the decisions it WOULD take to
stderr without writing to any database.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

KANBAN_DIR = Path(os.path.expanduser("~/.hermes/kanban/boards"))
ESCALATION_BOARD = "jarvis-os"
ESCALATION_ASSIGNEE = "jarvis-os-pm"
BLOCK_HOURS_THRESHOLD = 6

# Terminal statuses for an escalation card: if an escalation was already
# resolved (done/archived) by PM triage and the source re-blocks, a NEW
# escalation is legitimate. "Open" = anything not in this set.
#
# kanban t_9a621399: this constant used to be declared here and then ignored —
# both dedupe call sites inlined the literal ('done','archived') tuple instead.
# It is now the single definition of the policy, expanded into SQL via
# TERMINAL_STATUS_PLACEHOLDERS so there is exactly one place to change it.
TERMINAL_STATUSES = ("done", "archived")
TERMINAL_STATUS_PLACEHOLDERS = ",".join("?" for _ in TERMINAL_STATUSES)

# Hard backstop (kanban t_9a621399): no source task id may ever accumulate more
# than this many escalation cards in total, across ALL statuses. This is not the
# dedupe mechanism — it is the cap that bounds the blast radius if the dedupe
# logic is ever wrong again. The observed defect produced 8 cards for a single
# source; anything above a small handful is a bug, not a workload.
MAX_ESCALATION_CARDS_PER_SOURCE = 3

# --- Rate-limit-aware backoff config (kanban t_e656efdc) -----------------------
# This mechanism is LOCAL-ONLY (creates kanban tasks via local SQLite; it makes
# no provider/API call). The backoff envelope is configured here for
# completeness/consistency with the other 4 critical mechanisms, but NO outbound
# provider call site exists to wrap, so the backoff is intentionally dormant. If
# a future change adds a provider send to this script, use
# rate_limit_backoff.run_subprocess_with_backoff exactly as the
# breaker/notifier/verdict-router now do. No routing/cred/schedule/spend change.
RLB_BASE = float(os.environ.get("RLB_BASE_SECONDS", "2.0"))
RLB_MAX = float(os.environ.get("RLB_MAX_SECONDS", "60.0"))
RLB_ATTEMPTS = int(os.environ.get("RLB_MAX_ATTEMPTS", "3"))
MAX_ESCALATIONS_PER_RUN = 1
MAX_ESCALATIONS_PER_24H = 4

# Title patterns that trigger escalation (case-insensitive substring match)
TITLE_PATTERNS = ["SERVICE-GATE", "APPROVAL REQUIRED"]


# --- Block-cause classifier (kanban t_aec5a53c) -----------------------------
# Ported VERBATIM from the validated design prototype
# (kanban t_aec5a53c / block_cause_classifier.py). The regex precedence was
# tuned over three full-fleet census iterations (v1 regex-only produced a false
# positive on sycode-trading/t_b0d43b01; v2 human-marker-beats-all
# over-suppressed 3 genuine infra blocks). DO NOT re-tune without re-running
# fleet_census.py — see the design note
# Governance/2026-08-02-service-gate-watchdog-block-cause-classifier-design-t_aec5a53c.md.
#
# Contract: classify_block_cause(reason_text, structured_class=None) ->
# (cause, signature) where cause in {"infra","human"}. Fail-safe direction is
# HUMAN: unknown/absent evidence never suppresses a real human gate.
#
# empirical finding (verified 2026-08-02): the block reason for the
# false-positive case is NOT in tasks.last_failure_error (NULL) nor comments;
# it lives in the newest task_events row of kind 'blocked' as JSON
# {"reason": "...", "kind": "needs_input"}. latest_block_reason honours that
# precedence (block-event -> last_failure_error -> comment).

# Error/exception-shaped signatures only. Every alternative must contain an
# explicit failure token (Error/Exception/timeout/unreachable/unavailable/
# 5xx/transport_failed), so a provider NAME alone can never match.
INFRA_SIGNATURES: list[tuple[str, str]] = [
    # Concrete provider exception class names.
    (r"\b(gemini|openai|anthropic|nous|openrouter|xai|mistral)apierror\b",
     "provider-exception-class"),
    (r"\bapi(?:_|\s)?error\b.{0,60}\b(judge|provider|completion gate)\b",
     "api-error-near-judge"),
    (r"\b(judge|goal[-_ ]?judge|completion[-_ ]?judge)\b.{0,80}"
     r"\b(apierror|api error|transport|transport_failed|unreachable|"
     r"unavailable|timed?[-_ ]?out|timeout|5\d\d\b|connection (refused|reset|error))",
     "judge-transport-failure"),
    (r"\bprovider\b.{0,60}\b(unavailable|unreachable|transport_failed|"
     r"rate[-_ ]?limit(ed)?|quota exceeded|5\d\d\b)", "provider-transport-failure"),
    (r"\btransport_failed\b", "transport-failed-token"),
    (r"\b(read|connect|connection)\s?timeout\b", "network-timeout"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE | re.DOTALL), name)
             for p, name in INFRA_SIGNATURES]

# Structured escape hatch: if the board ever grows a block_reason_class column
# (or the block event payload carries one), it wins over regex inference.
STRUCTURED_INFRA_CLASSES = {"infra", "infrastructure", "provider_outage",
                            "judge_transport", "transient_infra"}
STRUCTURED_HUMAN_CLASSES = {"human", "needs_input", "authority", "approval"}

# Human-authority markers. These do NOT beat a hard exception token (see
# HARD_EXCEPTION_SIGNATURES); they only break ties against the softer,
# inference-based infra signatures.
#
# Empirically tuned. A fleet census over all 19 boards (192 candidates) was run
# at each iteration and every classification delta reviewed by hand:
#   * v1 (regex only)         -> 22 INFRA, 1 false positive
#     (sycode-trading/t_b0d43b01, a real "FRANK-APPROVE gate (R3)" whose reason
#     merely NAMES the branch fork/fix/goal-judge-transport-fail-open-audit).
#   * v2 (override beats all) -> fixed that, but over-suppressed 3 GENUINE infra
#     blocks (t_9bbc7d7e, t_d5589a5c, t_0b852226) whose reasons legitimately
#     mention "sign-off" or "awaiting Frank" alongside a real GeminiAPIError.
#   * v3 (this)               -> hard exception token wins; markers only break
#     ties. 0 false positives and 0 over-suppressions across all 192.
# A generic "sign-off" marker was REMOVED in v3: it appears inside genuine
# infra blocks describing what the completed work needs next.
HUMAN_AUTHORITY_MARKERS = [
    (r"\bfrank[-_ ]?approve\b", "frank-approve-gate"),
    (r"\b(needs?|awaiting|pending|requires?)\b.{0,40}\bfrank\b", "awaiting-frank"),
    (r"\bfrank[-_ ]?level\b", "frank-level-gate"),
    (r"\bapproval required\b", "approval-required"),
    (r"\bR3\b.{0,20}\bgate\b|\bgate\b.{0,20}\bR3\b", "r3-gate"),
    (r"\bmust[-_ ]?ask gate\b", "must-ask-gate"),
]
_HUMAN_COMPILED = [(re.compile(p, re.IGNORECASE | re.DOTALL), n)
                   for p, n in HUMAN_AUTHORITY_MARKERS]

# HARD tokens: a literal exception class or transport-failure token. These are
# emitted by machines, never typed by a human describing an authority gate, so
# their presence (after code-ish spans are stripped) is decisive for INFRA.
HARD_EXCEPTION_SIGNATURES = {
    "provider-exception-class", "transport-failed-token", "network-timeout",
}

# Code-ish spans stripped before ANY infra matching, so a branch name, path or
# URL cannot supply a signature. Same census case:
# `fork/fix/goal-judge-transport-fail-open-audit` is a git ref, not an error.
# Backticked spans are deliberately NOT stripped — census case t_d5589a5c
# quotes its real error as `judge error: GeminiAPIError`, which is genuine
# evidence, and stripping backticks over-suppressed it in v2.
_CODEISH = re.compile(
    r"\b\S*/\S*\b"                          # slash-bearing tokens (branch/path/URL)
    r"|\b[\w.-]+\.(py|md|json|ya?ml|sh)\b",  # filenames
    re.IGNORECASE,
)

INFRA = "infra"
HUMAN = "human"


def strip_codeish(text: str) -> str:
    """Blank out git refs, paths, filenames and backticked spans.

    These carry identifier text that looks like an error signature but is not
    evidence of a live fault. Replaced with a space so surrounding words do not
    fuse into a spurious match.
    """
    return _CODEISH.sub(" ", text)


def classify_block_cause(reason_text: str | None,
                         structured_class: str | None = None) -> tuple[str, str]:
    """Return ``(cause, signature)``.

    ``cause`` is ``"infra"`` or ``"human"``. ``signature`` names the matched
    rule (or ``"no-infra-signature"`` / ``"no-block-reason"``), so --dry-run can
    print WHY a candidate was classified the way it was.

    Fail-safe direction: unknown/absent evidence classifies as HUMAN. An
    unclassifiable block keeps today's Frank-level behaviour; only a positive,
    error-shaped match downgrades the route. Suppressing a real human gate is
    the expensive failure, so ambiguity never suppresses.
    """
    if structured_class:
        s = structured_class.strip().lower()
        if s in STRUCTURED_INFRA_CLASSES:
            return INFRA, f"structured:{s}"
        if s in STRUCTURED_HUMAN_CLASSES:
            return HUMAN, f"structured:{s}"
    if not reason_text or not reason_text.strip():
        return HUMAN, "no-block-reason"

    # Strip git refs/paths/filenames first so a branch NAME cannot masquerade
    # as a live transport error.
    scrubbed = strip_codeish(reason_text)
    infra_sig = None
    for rx, name in _COMPILED:
        if rx.search(scrubbed):
            infra_sig = name
            break

    # A hard exception token is machine-emitted and decisive: a real
    # GeminiAPIError is an infra fault even when the same reason also says
    # "awaiting Frank" about what happens next.
    if infra_sig in HARD_EXCEPTION_SIGNATURES:
        return INFRA, infra_sig

    # Softer, inference-based infra signatures yield to an explicit
    # human-authority marker.
    for rx, name in _HUMAN_COMPILED:
        if rx.search(reason_text):
            return HUMAN, f"human-authority:{name}"

    if infra_sig:
        return INFRA, infra_sig
    return HUMAN, "no-infra-signature"


def latest_block_reason(db_path: Path, task_id: str) -> tuple[str | None, str | None, str]:
    """Best available block-reason evidence for ``task_id``.

    Returns ``(reason_text, structured_class, source)`` where ``source`` is one
    of ``block-event`` / ``last_failure_error`` / ``comment`` / ``none``.

    Precedence is evidence-quality ordered, established empirically:
      1. newest ``task_events`` row of kind ``blocked`` -> payload.reason
         (this is where kanban_block() actually writes the reason)
      2. ``tasks.last_failure_error``
      3. newest ``task_comments`` body
    """
    if not db_path.is_file():
        return None, None, "none"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        if row and row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                payload = {}
            reason = payload.get("reason")
            klass = payload.get("block_reason_class") or payload.get("cause_class")
            if reason or klass:
                conn.close()
                return reason, klass, "block-event"
        row = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row and row["last_failure_error"]:
            conn.close()
            return row["last_failure_error"], None, "last_failure_error"
        row = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        conn.close()
        if row and row["body"]:
            return row["body"], None, "comment"
    except sqlite3.Error:
        return None, None, "none"
    return None, None, "none"


# Routing policy -------------------------------------------------------------
# INFRA_WINDOW is deliberately NOT a promote-to-Frank timer. The 2026-08-02 case
# was blocked 37.8h and still auto-recovered on its own, so any wall-clock
# promotion would have paged Frank anyway. INFRA cards surface to the OPERATOR
# lane and only a human retag promotes them.
INFRA_WINDOW_HOURS = 24


def route_for(cause: str, block_hours: float) -> dict:
    """Routing decision for one classified candidate."""
    if cause == INFRA:
        if block_hours < INFRA_WINDOW_HOURS:
            return {"route": "defer", "priority": None,
                    "reason": f"infra-outage block only {block_hours:.1f}h "
                              f"(< {INFRA_WINDOW_HOURS}h infra window) — likely "
                              f"self-recovering, not escalating"}
        return {"route": "operator", "priority": 1,
                "reason": f"infra-outage block {block_hours:.1f}h past the "
                          f"{INFRA_WINDOW_HOURS}h infra window — surfacing to the "
                          f"operator lane, NOT Frank; human retag promotes"}
    return {"route": "frank", "priority": 3,
            "reason": f"human authority gate blocked {block_hours:.1f}h — "
                      f"unchanged Frank-level escalation"}


def now_ts() -> int:
    return int(time.time())


# --- Orphan/existence guards (kanban t_3ab9e690) -------------------------------
# The watchdog used to trust its own escalation card as evidence that a source
# task existed, so a card whose source board or task had been retired kept being
# heartbeated forever (see jarvis-os/t_494a1c32 -> ai-restaurant/t_a85ddbd9).
# Every create/heartbeat now goes through source_exists() first, mirroring the
# existing-path filter in scripts/blocked-state-dispatch-guard.py::_find_boards.

# Source task id is embedded in the escalation title:
#   "FRANK ESCALATION: SERVICE-GATE task <task_id> blocked <n>h"
SOURCE_TASK_RE = re.compile(r"SERVICE-GATE task (t_[0-9a-zA-Z_]+)")
# ...and the source board in the body: "**Source task:** <id> on board `<board>`"
SOURCE_BOARD_RE = re.compile(r"on board\s+`([^`]+)`")


def board_db_path(board: str) -> Path:
    """Resolve a board slug to its kanban.db path (no existence guarantee)."""
    return KANBAN_DIR / board / "kanban.db"


def board_db_exists(board: str) -> bool:
    """Criterion 2a: the source board's kanban.db is present on disk."""
    if not board:
        return False
    return board_db_path(board).is_file()


def task_exists(board: str, task_id: str) -> bool:
    """Criterion 2b: the source task id still resolves in that board's DB."""
    if not task_id or not board_db_exists(board):
        return False
    try:
        conn = sqlite3.connect(f"file:{board_db_path(board)}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ? LIMIT 1", (task_id,)
        ).fetchone()
        conn.close()
        return row is not None
    except sqlite3.Error:
        # An unreadable/corrupt source DB is not proof the task exists, so it is
        # treated as non-existent: fail closed (skip) rather than escalate.
        return False


def board_task_ids(board: str) -> set[str] | None:
    """Return every task id in ``board``'s DB, or None when the DB is absent.

    Single query per board, used to back the per-run existence cache. An
    unreadable/corrupt DB returns an EMPTY set (not None): the board file is
    present but nothing in it resolves, which matches task_exists() failing
    closed for the same case.
    """
    if not board or not board_db_exists(board):
        return None
    try:
        conn = sqlite3.connect(f"file:{board_db_path(board)}?mode=ro", uri=True)
        rows = conn.execute("SELECT id FROM tasks").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


class SourceExistenceCache:
    """Per-run cache of board -> task-id set (kanban t_4e8c2620).

    The scan classifies EVERY blocked candidate with source_exists() before the
    escalation budget is consulted, so with ~189 fleet-wide candidates the old
    open-a-read-only-connection-per-candidate cost would be paid ~189 times.
    This collapses it to one query per board.

    Deliberately instantiated fresh inside main() rather than kept at module
    scope: a run must not see another run's stale view of the boards.
    """

    def __init__(self) -> None:
        self._ids: dict[str, set[str] | None] = {}

    def _ids_for(self, board: str) -> set[str] | None:
        if board not in self._ids:
            self._ids[board] = board_task_ids(board)
        return self._ids[board]

    def source_exists(self, board: str, task_id: str) -> tuple[bool, str]:
        ids = self._ids_for(board)
        if ids is None:
            return False, f"source board '{board}' has no kanban.db on disk"
        if not task_id or task_id not in ids:
            return False, f"source task '{task_id}' not found in board '{board}'"
        return True, "ok"


def source_exists(
    board: str, task_id: str, cache: "SourceExistenceCache | None" = None
) -> tuple[bool, str]:
    """Return (ok, reason). ``ok`` False means the candidate is orphaned.

    ``cache`` is an optional per-run SourceExistenceCache; when supplied the
    answer is served from the board's cached task-id set instead of opening a
    fresh read-only connection. Semantics are identical either way.
    """
    if cache is not None:
        return cache.source_exists(board, task_id)
    if not board_db_exists(board):
        return False, f"source board '{board}' has no kanban.db on disk"
    if not task_exists(board, task_id):
        return False, f"source task '{task_id}' not found in board '{board}'"
    return True, "ok"


def parse_escalation_source(title: str, body: str) -> tuple[str | None, str | None]:
    """Recover (source_board, source_task_id) from an escalation card."""
    task_match = SOURCE_TASK_RE.search(title or "")
    board_match = SOURCE_BOARD_RE.search(body or "")
    return (
        board_match.group(1) if board_match else None,
        task_match.group(1) if task_match else None,
    )


def check_configured_boards() -> list[str]:
    """Criterion 4: startup self-check over the board root.

    Returns the list of live board slugs (dir + readable kanban.db) and warns on
    stderr for every board directory that is registered on disk but is missing
    its kanban.db, so a retired board is visible rather than silently scanned.
    """
    live: list[str] = []
    if not KANBAN_DIR.is_dir():
        print(
            f"WARN: kanban board root {KANBAN_DIR} does not exist — nothing to scan",
            file=sys.stderr,
        )
        return live
    for board_path in sorted(KANBAN_DIR.iterdir()):
        if not board_path.is_dir():
            continue
        board = board_path.name
        if board.startswith(("_", ".")) or board == "attachments":
            continue
        if not (board_path / "kanban.db").is_file():
            print(
                f"WARN: board '{board}' has no kanban.db — skipping (retired/stale)",
                file=sys.stderr,
            )
            continue
        live.append(board)
    return live


def get_parked_source_ids(db_path: Path) -> set[str]:
    """Return ids of tasks carrying the canonical parked marker.

    A source parked with ``PARKED: awaiting-absent-seat`` in a comment is
    waiting on a seat that does not exist on this host (orchestrator
    disposition, jarvis seat 2026-08-03; kanban t_5956838b). No agent can
    ever clear it, so re-escalating is noise. This is a POSITIVE predicate:
    only the explicit marker suppresses a source — never a blanket age cap.

    Fail-safe: an unreadable or schema-less DB (no task_comments table)
    yields an EMPTY set, i.e. no suppression, preserving pre-existing
    behavior for that board. An unreadable comments table must not silently
    silence genuine escalations.
    """
    if not db_path.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT DISTINCT task_id FROM task_comments "
            "WHERE body LIKE ?",
            ("%PARKED: awaiting-absent-seat%",),
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def get_blocked_tasks(db_path: Path) -> list[dict]:
    """Return blocked tasks matching escalation criteria.

    This is the RAW candidate query. Parked-source exclusion (kanban
    t_5956838b) is applied in main()'s per-candidate loop, where the
    ``skipped_parked`` counter lives, so a dry-run proves the exclusion in
    both directions: parked sources are skipped and counted, genuinely
    stalled sources still escalate.
    """
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, title, assignee, status, block_kind, created_by, created_at,
                   consecutive_failures, last_failure_error
            FROM tasks
            WHERE status = 'blocked'
              AND (block_kind = 'needs_input'
                   OR title LIKE '%SERVICE-GATE%'
                   OR title LIKE '%APPROVAL REQUIRED%')
            ORDER BY id
            """
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def is_escalation_task(board: str, task: dict) -> bool:
    """Return True for watchdog-created escalation cards.

    These cards are triage wrappers around source tasks. Re-escalating them
    creates self-referential storms like "FRANK ESCALATION ... escalation".
    """
    title = task.get("title") or ""
    return (
        board == ESCALATION_BOARD
        and (
            task.get("created_by") == "service-gate-escalation"
            or title.startswith("FRANK ESCALATION: SERVICE-GATE")
        )
    )


def recent_escalations_count(hours: int = 24) -> int:
    """Count watchdog-created escalation cards in the recent window."""
    escalation_db = KANBAN_DIR / ESCALATION_BOARD / "kanban.db"
    if not escalation_db.is_file():
        return 0
    cutoff = now_ts() - (hours * 3600)
    try:
        conn = sqlite3.connect(str(escalation_db))
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE created_by = 'service-gate-escalation'
              AND created_at > ?
            """,
            (cutoff,),
        ).fetchone()
        conn.close()
        return int(row[0] if row else 0)
    except sqlite3.OperationalError:
        return 0


def escalation_cards_for_source(task_id: str) -> list[dict]:
    """Return EVERY watchdog escalation card for ``task_id``, newest first.

    Unlike the old open-only lookup this deliberately includes done/archived
    cards: the dedupe decision (see classify_dedupe) is time-aware, so a
    resolved card is still evidence about this block episode until the source
    demonstrably re-blocked after that card was completed.

    Each dict has id, status, created_at, completed_at. ``completed_at`` is
    None when the column is absent (legacy board schema) or unset.
    """
    escalation_db = KANBAN_DIR / ESCALATION_BOARD / "kanban.db"
    if not escalation_db.is_file():
        return []
    base = """
        SELECT id, status, created_at, {completed}
        FROM tasks
        WHERE created_by = 'service-gate-escalation'
          AND title LIKE ?
        ORDER BY created_at DESC
    """
    try:
        conn = sqlite3.connect(str(escalation_db))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                base.format(completed="completed_at"), (f"%{task_id}%",)
            ).fetchall()
        except sqlite3.OperationalError:
            # Legacy/partial schema with no completed_at column. Fail closed:
            # completed_at unknown means we cannot prove the block episode is
            # newer than the resolution, so classify_dedupe will reopen rather
            # than mint (see the NULL-completed_at branch there).
            rows = conn.execute(
                base.format(completed="NULL AS completed_at"), (f"%{task_id}%",)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def classify_dedupe(task_id: str, block_start_ts: int) -> dict:
    """Time-aware find-or-create decision for one source task (kanban t_9a621399).

    Returns ``{"action", "escalation_id", "reason", "cap_enforced"}`` where
    action is one of ``create`` / ``heartbeat`` / ``reopen``. ``cap_enforced``
    is True ONLY when MAX_ESCALATION_CARDS_PER_SOURCE actually changed the
    outcome — i.e. a mint was suppressed. It is deliberately not "count >= cap":
    a source that merely has a lot of historical cards but is being heartbeated
    anyway was not saved by the cap, and reporting it as capped would overstate
    what the backstop did.

    The previous rule was status-aware: any done/archived escalation was
    invisible, so a PM completing a card guaranteed a fresh card on the next
    30-minute tick for as long as the source stayed blocked. Observed effect:
    8 cards for a single source, 7 of them done — cards minted == PM
    completions + 1. (This is the second time this symptom class appeared; the
    first fix, kanban t_71feadc2, replaced a lost JSON state file with a DB
    lookup and reintroduced the same symptom through that lookup's status
    filter. The fix here removes the status filter from the *visibility*
    question entirely and makes terminality a property of TIME.)

    The rule now is: a done/archived escalation is terminal for this source
    ONLY IF the source's current block episode began strictly after that
    escalation was completed. That is a genuine re-block and deserves a new
    card. Anything else — including the common case of a long-lived human
    authority gate that never unblocked — reuses the existing card.

    Fail-closed everywhere: when the completion timestamp is unknown we reopen
    rather than mint, because a reopened card is still fully visible to Frank
    whereas a duplicate card is unrecoverable noise.
    """
    cards = escalation_cards_for_source(task_id)
    if not cards:
        return {
            "action": "create",
            "escalation_id": None,
            "reason": "no escalation card exists for this source",
            "cap_enforced": False,
        }

    at_cap = len(cards) >= MAX_ESCALATION_CARDS_PER_SOURCE

    open_cards = [c for c in cards if c["status"] not in TERMINAL_STATUSES]
    if open_cards:
        return {
            "action": "heartbeat",
            "escalation_id": open_cards[0]["id"],
            "reason": f"open escalation {open_cards[0]['id']} already tracks this source",
            "cap_enforced": False,
        }

    newest = cards[0]
    completed_at = newest["completed_at"]

    if completed_at is None:
        return {
            "action": "reopen",
            "escalation_id": newest["id"],
            "reason": (
                f"newest escalation {newest['id']} is {newest['status']} but has no "
                f"completed_at, so the block episode cannot be proven newer than the "
                f"resolution — failing closed and reusing the card"
            ),
            "cap_enforced": False,
        }

    if block_start_ts > completed_at:
        if at_cap:
            return {
                "action": "reopen",
                "escalation_id": newest["id"],
                "reason": (
                    f"source re-blocked after escalation {newest['id']} was resolved, but "
                    f"{len(cards)} escalation cards already exist for this source "
                    f"(cap {MAX_ESCALATION_CARDS_PER_SOURCE}) — reusing instead of minting"
                ),
                "cap_enforced": True,
            }
        return {
            "action": "create",
            "escalation_id": None,
            "reason": (
                f"source block episode began at {block_start_ts} which is after the "
                f"newest escalation {newest['id']} completed at {completed_at} — "
                f"legitimate re-escalation"
            ),
            "cap_enforced": False,
        }

    return {
        "action": "reopen",
        "escalation_id": newest["id"],
        "reason": (
            f"source block episode began at {block_start_ts}, before escalation "
            f"{newest['id']} was completed at {completed_at} — the source never "
            f"unblocked, so this is the same episode, not a new one"
        ),
        "cap_enforced": False,
    }


def get_last_comments(db_path: Path, task_id: str, limit: int = 3) -> list[dict]:
    """Return the last N comments for a task."""
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT author, body, created_at
            FROM task_comments
            WHERE task_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def get_block_events(db_path: Path, task_id: str) -> list[dict]:
    """Return block-related events for a task to find when it was blocked."""
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT kind, payload, created_at
            FROM task_events
            WHERE task_id = ?
              AND kind IN ('blocked', 'created', 'unblocked')
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (task_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def estimate_block_start(
    db_path: Path, task_id: str, task_created_at: int
) -> int:
    """Estimate when the task entered blocked state.

    Looks for the most recent 'blocked' event; falls back to task created_at.
    """
    events = get_block_events(db_path, task_id)
    # Walk events from newest to oldest; find the most recent 'blocked'
    # that hasn't been followed by an 'unblocked'
    blocked_at = None
    for ev in events:
        if ev["kind"] == "unblocked":
            # If there's an unblocked after the blocked, the current block
            # started after that unblocked — but we can't easily determine
            # that from events alone. Use the most recent blocked event
            # that is NOT followed by an unblocked.
            break
        if ev["kind"] == "blocked" and blocked_at is None:
            blocked_at = ev["created_at"]
    if blocked_at is not None:
        return blocked_at
    # Fallback: use task created_at (task was created as blocked)
    return task_created_at


def retire_orphaned_escalation(
    escalation_id: str,
    board: str | None,
    task_id: str | None,
    reason: str,
    now: int,
    dry_run: bool = False,
) -> bool:
    """Criterion 3: park an escalation whose source no longer exists.

    Transitions the card to blocked/transient and posts exactly ONE explanatory
    comment. Idempotent: if a retirement comment is already present the card is
    left alone, so the watchdog stops heartbeating an orphan forever instead of
    re-commenting every 30 minutes.
    """
    marker = "ORPHANED SOURCE"
    escalation_db = KANBAN_DIR / ESCALATION_BOARD / "kanban.db"
    if not escalation_db.is_file():
        return False
    body = (
        f"{marker}: watchdog existence check failed — {reason}. "
        f"This escalation references `{board}/{task_id}`, which cannot be "
        f"verified against a live board DB, so there is nothing for Frank to "
        f"unblock or approve. Parking as blocked/transient and ceasing "
        f"heartbeats (kanban t_3ab9e690)."
    )
    try:
        conn = sqlite3.connect(str(escalation_db))
        already = conn.execute(
            """
            SELECT 1 FROM task_comments
            WHERE task_id = ?
              AND author = 'service-gate-escalation'
              AND body LIKE ?
            LIMIT 1
            """,
            (escalation_id, f"%{marker}%"),
        ).fetchone()
        if already is not None:
            conn.close()
            return False
        if dry_run:
            conn.close()
            print(
                f"DRY-RUN would RETIRE orphan escalation {escalation_id} "
                f"({board}/{task_id}): {reason}",
                file=sys.stderr,
            )
            return True
        conn.execute(
            """
            INSERT INTO task_comments (task_id, author, body, created_at)
            VALUES (?, 'service-gate-escalation', ?, ?)
            """,
            (escalation_id, body, now),
        )
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'transient' "
            "WHERE id = ?",
            (escalation_id,),
        )
        conn.commit()
        conn.close()
        print(
            f"RETIRED orphan escalation {escalation_id} ({board}/{task_id}): {reason}",
            file=sys.stderr,
        )
        return True
    except sqlite3.Error as e:
        print(f"FAILED retiring orphan {escalation_id}: {e}", file=sys.stderr)
        return False


def reconcile_open_escalations(now: int, dry_run: bool = False) -> int:
    """Sweep already-open escalation cards whose source has been retired.

    Runs before the scan so an orphan created by an older build of this script
    (e.g. jarvis-os/t_494a1c32) is parked rather than heartbeated. Returns the
    number of cards retired this run.
    """
    escalation_db = KANBAN_DIR / ESCALATION_BOARD / "kanban.db"
    if not escalation_db.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{escalation_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, title, body
            FROM tasks
            WHERE created_by = 'service-gate-escalation'
              AND status NOT IN ({TERMINAL_STATUS_PLACEHOLDERS})
            """,
            TERMINAL_STATUSES,
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return 0

    retired = 0
    for row in rows:
        board, task_id = parse_escalation_source(row["title"], row["body"])
        if not task_id:
            # Unparseable card — leave it for a human rather than guessing.
            continue
        ok, reason = source_exists(board or "", task_id)
        if ok:
            continue
        if retire_orphaned_escalation(
            row["id"], board, task_id, reason, now, dry_run=dry_run
        ):
            retired += 1
    return retired


def create_escalation_task(
    board: str,
    task_id: str,
    title: str,
    block_duration_hours: float,
    comments: list[dict],
    block_start_ts: int,
    dry_run: bool = False,
    priority: int = 3,
    infra_tag: bool = False,
) -> bool:
    """Create a Frank escalation kanban task on jarvis-os board via SQLite.

    Called ONLY when no open escalation already exists for this source task
    (see classify_dedupe) AND the source board+task were verified to
    exist on disk (see source_exists). Returns True on success.

    ``priority`` / ``infra_tag`` come from the block-cause router
    (kanban t_aec5a53c): a human authority gate routes to Frank at the
    traditional priority 3, while an INFRA outage that has aged past the
    24h window surfaces to the operator lane at priority 1 with an ``[INFRA]``
    title tag so it is visibly NOT a Frank-approval request.
    """
    import uuid

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    block_start = datetime.fromtimestamp(block_start_ts, tz=timezone.utc)

    # Build evidence summary
    comment_lines = []
    for c in comments:
        ts = datetime.fromtimestamp(c["created_at"], tz=timezone.utc)
        author = c.get("author", "unknown")
        snippet = (c.get("body") or "")[:120].replace("\n", " ")
        comment_lines.append(
            f"  - {ts.strftime('%H:%M %d %b')} [{author}]: {snippet}"
        )

    comment_section = "\n".join(comment_lines) if comment_lines else "  (no comments)"

    escalation_title = (
        f"FRANK ESCALATION: SERVICE-GATE task {task_id} blocked {block_duration_hours:.1f}h"
    )
    if infra_tag:
        escalation_title = (
            f"[INFRA] ESCALATION: SERVICE-GATE task {task_id} blocked {block_duration_hours:.1f}h"
        )
    escalation_body = (
        f"Auto-escalated by service-gate-escalation watchdog.\n\n"
        f"**Source task:** {task_id} on board `{board}`\n"
        f"**Title:** {title}\n"
        f"**Blocked since:** {block_start.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"**Block duration:** {block_duration_hours:.1f} hours\n"
        f"**Threshold:** {BLOCK_HOURS_THRESHOLD}h\n\n"
        f"**Last {len(comments)} comments:**\n"
        f"{comment_section}\n\n"
        f"**Action required:** Review and unblock or approve the source task."
    )

    new_id = f"t_{uuid.uuid4().hex[:8]}"
    escalation_db = KANBAN_DIR / ESCALATION_BOARD / "kanban.db"

    if dry_run:
        print(
            f"DRY-RUN would ESCALATE: {board}/{task_id} -> (new card) "
            f"{escalation_title}",
            file=sys.stderr,
        )
        return True

    try:
        conn = sqlite3.connect(str(escalation_db))
        conn.execute(
            """
            INSERT INTO tasks (id, title, body, assignee, status, priority,
                               created_by, created_at, workspace_kind)
            VALUES (?, ?, ?, ?, 'ready', ?, 'service-gate-escalation', ?, 'scratch')
            """,
            (new_id, escalation_title, escalation_body, ESCALATION_ASSIGNEE,
             priority, now_ts),
        )
        conn.commit()
        conn.close()
        print(
            f"ESCALATED: {board}/{task_id} -> {new_id} ({escalation_title})",
            file=sys.stderr,
        )
        return True
    except sqlite3.OperationalError as e:
        print(
            f"FAILED to create escalation for {board}/{task_id}: {e}",
            file=sys.stderr,
        )
        return False


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names present in ``table``, or an empty set if it is unreadable."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _record_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: dict, now: int
) -> None:
    """Best-effort audit event on the escalation board.

    A reopen is a status transition the dashboard and any downstream auditor
    should be able to see without diffing comment text, so it gets a real
    task_events row. Failure to write the audit trail must never abort the
    reopen itself (the card state is the operative artifact), so this swallows
    schema/insert errors — a board whose task_events shape differs still gets
    the correct card state and the explanatory comment.
    """
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, kind, json.dumps(payload), now),
        )
    except sqlite3.Error as e:
        print(
            f"WARN: could not record '{kind}' event for {task_id}: {e}",
            file=sys.stderr,
        )


def heartbeat_existing_escalation(
    existing_id: str,
    board: str,
    task_id: str,
    block_duration_hours: float,
    now_ts: int,
    dry_run: bool = False,
    reopen: bool = False,
    reason: str = "",
    infra_tag: bool = False,
    priority: int = 3,
) -> None:
    """Update an existing escalation card IN PLACE — do NOT create a new row.

    Appends a re-fire heartbeat comment and refreshes last_heartbeat_at. This
    is the core of the find-or-create fix: repeated watchdog fires against the
    same still-blocked source update the single existing card instead of
    spawning duplicates (kanban t_71feadc2).

    ``reopen`` (kanban t_9a621399): when the only card for this source is
    done/archived but the source's block episode predates that completion, the
    card is REOPENED (status -> 'ready', completed_at cleared) rather than
    leaving a resolved card as the sole record of a still-live gate. Without
    this the time-aware dedupe would suppress the duplicate but leave nothing
    actionable on the board.

    ``infra_tag`` / ``priority`` (kanban t_aec5a53c): when the block-cause
    router classifies the source as an INFRA outage aged past the 24h window,
    the card is routed to the operator lane. When heartbeating an existing card
    on that route we retag its title with an ``[INFRA]`` prefix and drop
    priority to 1 so it reads as operator-visible, not a Frank-approval request.
    """
    escalation_db = KANBAN_DIR / ESCALATION_BOARD / "kanban.db"
    verb = "REOPEN" if reopen else "HEARTBEAT"
    if dry_run:
        print(
            f"DRY-RUN would {verb}: {board}/{task_id} -> existing {existing_id}"
            + (f" ({reason})" if reason else ""),
            file=sys.stderr,
        )
        return
    body = (
        f"Watchdog re-fire heartbeat: source `{board}/{task_id}` still "
        f"blocked {block_duration_hours:.1f}h. No new escalation card "
        f"created (find-or-create, kanban t_71feadc2)."
    )
    if reopen:
        body = (
            f"Watchdog REOPENED this escalation: source `{board}/{task_id}` is "
            f"still blocked {block_duration_hours:.1f}h and its block episode "
            f"predates this card's resolution, so completing the card did not "
            f"resolve the gate. Time-aware dedupe (kanban t_9a621399) reuses "
            f"this card instead of minting a duplicate. Reason: {reason}"
        )
    try:
        conn = sqlite3.connect(str(escalation_db))
        conn.execute(
            """
            INSERT INTO task_comments (task_id, author, body, created_at)
            VALUES (?, 'service-gate-escalation', ?, ?)
            """,
            (existing_id, body, now_ts),
        )
        if reopen:
            cols = _table_columns(conn, "tasks")
            sets = ["status = 'ready'", "last_heartbeat_at = ?"]
            params: list = [now_ts]
            for col in (
                "completed_at",
                "current_run_id",
                "claim_lock",
                "claim_expires",
                "worker_pid",
                "last_failure_error",
            ):
                if col in cols:
                    sets.append(f"{col} = NULL")
            if "consecutive_failures" in cols:
                sets.append("consecutive_failures = 0")
            params.append(existing_id)
            conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params
            )
            _record_event(
                conn,
                existing_id,
                "reopened",
                {
                    "actor": "service-gate-escalation",
                    "source_board": board,
                    "source_task": task_id,
                    "reason": reason,
                },
                now_ts,
            )
        else:
            conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
                (now_ts, existing_id),
            )
        # kanban t_aec5a53c: when the block-cause router sends this card to the
        # operator lane (INFRA outage aged past the 24h window), make that
        # routing visible on the card itself — retag the title with an [INFRA]
        # prefix and lower priority to 1. Idempotent: an already-[INFRA]-tagged
        # card is left alone, and a non-infra heartbeat (infra_tag False) never
        # touches title or priority.
        if infra_tag:
            cols = _table_columns(conn, "tasks")
            cur = conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (existing_id,)
            ).fetchone()
            if cur and not (cur[0] or "").startswith("[INFRA]"):
                new_title = cur[0].replace(
                    "FRANK ESCALATION: SERVICE-GATE",
                    "[INFRA] ESCALATION: SERVICE-GATE",
                    1,
                )
                if "priority" in cols:
                    conn.execute(
                        "UPDATE tasks SET title = ?, priority = ? WHERE id = ?",
                        (new_title, priority, existing_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET title = ? WHERE id = ?",
                        (new_title, existing_id),
                    )
        conn.commit()
        conn.close()
        print(
            f"{verb}: {board}/{task_id} -> existing {existing_id}",
            file=sys.stderr,
        )
    except sqlite3.OperationalError as e:
        print(
            f"FAILED {verb.lower()} for {existing_id}: {e}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv

    now = now_ts()
    threshold_ts = now - (BLOCK_HOURS_THRESHOLD * 3600)
    recent_escalations = recent_escalations_count(24)
    escalated = 0
    skipped_duplicate = 0
    skipped_recent = 0
    skipped_escalation_tasks = 0
    skipped_rate_limited = 0
    skipped_orphaned = 0
    skipped_parked = 0
    # kanban t_9a621399: reopened = resolved cards revived because the source's
    # block episode predates their completion (the re-mint defect's blast site).
    # capped_sources = sources already at/over MAX_ESCALATION_CARDS_PER_SOURCE,
    # i.e. the hard backstop refused to mint even though other logic allowed it.
    reopened = 0
    capped_sources = 0
    # kanban t_aec5a53c: block-cause routing counters. `skipped_infra_deferred`
    # counts INFRA-outage candidates that are inside the 24h self-recovery
    # window (no card minted, no escalation to Frank). `infra_operator_routed`
    # counts INFRA candidates aged past the window that surface to the operator
    # lane (priority 1, [INFRA] title tag) instead of Frank. Both are additive
    # and never suppress a human gate.
    skipped_infra_deferred = 0
    infra_operator_routed = 0

    # Criterion 4: startup self-check. Warns for every board dir missing its
    # kanban.db and returns only the live boards, mirroring _find_boards() in
    # scripts/blocked-state-dispatch-guard.py.
    live_boards = check_configured_boards()

    # Criterion 3: park any already-open escalation whose source board/task has
    # since been retired, BEFORE the scan can heartbeat it again.
    retired_orphans = reconcile_open_escalations(now, dry_run=dry_run)

    boards_scanned = 0
    # Per-run existence cache (kanban t_4e8c2620): one task-id query per board
    # instead of one read-only connection per candidate, now that EVERY
    # candidate is classified rather than only the <=1 that fits the budget.
    # Scoped to this run only; reconcile_open_escalations() above deliberately
    # keeps its own uncached path so its behaviour is bit-for-bit unchanged.
    existence_cache = SourceExistenceCache()

    for board in live_boards:
        db_path = board_db_path(board)
        boards_scanned += 1

        tasks = get_blocked_tasks(db_path)
        # Parked-source exclusion (kanban t_5956838b): identify sources parked
        # as awaiting-absent-seat so they are skipped with a counter, not
        # escalated. The raw query returns everything; exclusion + counting
        # lives here so --dry-run proves it both ways.
        parked_ids = set()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM task_comments "
                "WHERE body LIKE ?",
                ("%PARKED: awaiting-absent-seat%",),
            ).fetchall()
            conn.close()
            parked_ids = {r[0] for r in rows}
        except sqlite3.Error:
            pass  # unreadable/missing comments table -> empty set, no suppression
        for task in tasks:
            if is_escalation_task(board, task):
                skipped_escalation_tasks += 1
                continue

            task_id = task["id"]
            if task_id in parked_ids:
                skipped_parked += 1
                print(
                    f"SKIP parked source {board}/{task_id}: "
                    f"awaiting-absent-seat (parked, non-dispatchable)",
                    file=sys.stderr,
                )
                continue

            task_created = task["created_at"]
            block_start = estimate_block_start(db_path, task_id, task_created)

            if block_start > threshold_ts:
                skipped_recent += 1
                continue

            block_hours = (now - block_start) / 3600

            # Criterion 2: verify the source board DB and source task both
            # resolve before ANY create or heartbeat. A candidate read from a
            # board that has since been retired mid-run is dropped here rather
            # than turned into an escalation Frank cannot action.
            #
            # kanban t_4e8c2620: this classification runs BEFORE the escalation
            # rate limiter, so `orphaned candidates skipped` is a fleet-wide
            # census rather than a lower bound capped by remaining budget. The
            # budget still governs escalation CREATION only (checked below) —
            # classifying a candidate never creates or heartbeats a card.
            ok, reason = source_exists(board, task_id, cache=existence_cache)
            if not ok:
                skipped_orphaned += 1
                print(
                    f"SKIP orphaned candidate {board}/{task_id}: {reason}",
                    file=sys.stderr,
                )
                continue

            # kanban t_aec5a53c: classify the block CAUSE (read-only) BEFORE the
            # rate limiter and before any write. The evidence source is the
            # newest 'blocked' task_events payload; last_failure_error and
            # comments are fallbacks. INFRA outages (e.g. goal-judge
            # GeminiAPIError) must NOT page Frank — they route to the operator
            # lane or are deferred; HUMAN authority gates keep today's path.
            reason_text, structured_class, evidence_src = latest_block_reason(
                db_path, task_id
            )
            cause, signature = classify_block_cause(reason_text, structured_class)
            route = route_for(cause, block_hours)
            is_infra_route = (cause == INFRA and route["route"] == "operator")
            if dry_run:
                print(
                    f"CLASSIFY {board}/{task_id} cause={cause} signature={signature} "
                    f"evidence-source={evidence_src} route={route['route']}"
                    + (f" priority={route['priority']}" if route["priority"] else "")
                    + f" — {route['reason']}",
                    file=sys.stderr,
                )
            if cause == INFRA and route["route"] == "defer":
                # Inside the infra self-recovery window: no card, no Frank page.
                # Still classified for census/observability; never suppresses a
                # human gate because cause HUMAN never defers.
                skipped_infra_deferred += 1
                continue

            # Rate limit: governs escalation CREATION and heartbeats, i.e. every
            # write path below. Deliberately evaluated after classification.
            if escalated >= MAX_ESCALATIONS_PER_RUN:
                skipped_rate_limited += 1
                continue

            if recent_escalations + escalated >= MAX_ESCALATIONS_PER_24H:
                skipped_rate_limited += 1
                continue

            # Time-aware find-or-create (kanban t_9a621399). A done/archived
            # escalation is terminal ONLY when the source's current block
            # episode started after that card was completed; otherwise the
            # source never unblocked and we reuse the card (heartbeat if open,
            # reopen if resolved) instead of minting a duplicate. A hard cap on
            # total cards per source backstops any future logic bug.
            decision = classify_dedupe(task_id, block_start)
            if decision["action"] in ("heartbeat", "reopen"):
                is_reopen = decision["action"] == "reopen"
                heartbeat_existing_escalation(
                    decision["escalation_id"],
                    board,
                    task_id,
                    block_hours,
                    now,
                    dry_run=dry_run,
                    reopen=is_reopen,
                    reason=decision["reason"],
                    infra_tag=is_infra_route,
                    priority=route["priority"] if is_infra_route else 3,
                )
                skipped_duplicate += 1
                if is_reopen:
                    reopened += 1
                if decision["cap_enforced"]:
                    capped_sources += 1
                    print(
                        f"CAP: {board}/{task_id} — {decision['reason']}",
                        file=sys.stderr,
                    )
                continue

            comments = get_last_comments(db_path, task_id, limit=3)
            if create_escalation_task(
                board,
                task_id,
                task["title"],
                block_hours,
                comments,
                block_start,
                dry_run=dry_run,
                priority=route["priority"] if is_infra_route else 3,
                infra_tag=is_infra_route,
            ):
                escalated += 1
                if is_infra_route:
                    infra_operator_routed += 1

    summary = (
        f"service-gate-escalation: {escalated} escalated, "
        f"{skipped_duplicate} duplicate (heartbeated in place), "
        f"{skipped_recent} under threshold, "
        f"{skipped_escalation_tasks} escalation tasks skipped, "
        f"{skipped_orphaned} orphaned candidates skipped, "
        f"{skipped_parked} parked sources skipped, "
        f"{retired_orphans} orphan escalations retired, "
        f"{skipped_rate_limited} rate-limited, "
        f"{boards_scanned} boards scanned, "
        f"{reopened} reopened, "
        f"{capped_sources} capped, "
        f"{skipped_infra_deferred} infra deferred, "
        f"{infra_operator_routed} infra operator-routed"
    )

    if dry_run:
        # A dry run is operator-invoked, so always report — the watchdog's
        # silent-unless-actionable contract only applies to real cron fires.
        print(f"DRY-RUN {summary}")
        return

    # Only print to stdout if the run actually changed something (watchdog
    # pattern). Retiring an orphan is a real state change and worth reporting,
    # and so is reopening a resolved escalation (kanban t_9a621399) — a card
    # coming back from done is exactly what a PM needs to see.
    if escalated > 0 or retired_orphans > 0 or reopened > 0:
        print(summary)
    # else: silent — nothing to escalate


if __name__ == "__main__":
    main()
