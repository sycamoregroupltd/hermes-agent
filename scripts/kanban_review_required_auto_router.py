#!/usr/bin/env python3
"""Auto-route reviewer cards for orphaned review-required kanban handoffs.

No-agent cron script for the Jarvis profile. It scans a fixed set of boards for
blocked source tasks whose latest markers (comments OR block-event payloads)
include ``review-required`` and creates exactly one idempotent reviewer card
per review-required handoff round.

Detection priority:
  1. Comments (existing path) — looks for ``review-required``-prefixed handoff
     messages, filtering out known machine-noise authors (failure-classifier-*).
  2. Block events (new fallback path) — scans task_events kind='blocked' where
     payload.reason starts with ``review-required``.  Only fires when no comment
     handoff was found, so existing router-created cards keep stable round
     numbers and are never duplicated.

Safety invariants:
- deterministic SQLite writes only; no credentials, provider routing, or deploys
- no parent links from review card to source task (avoids dispatcher deadlock)
- idempotency key ``review-<source-task-id>-r<N>`` suppresses duplicates for a
  specific source handoff round; later rework handoffs route fresh review cards
- empty stdout when no work is needed so no-agent cron stays quiet
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from verdict_router import RiskClassification, classify_risk

BOARD_REVIEWERS = {
    "jarvis-os": "os-reviewer",
    "sycode-trading": "trading-risk-reviewer",
    "sycode-ai": "platform-reviewer",
    "upero": "guardian",
    "yorkstone-supplies": "yorkstone-supplies-reviewer",
}

# Fixture-card marker: source tasks tagged FIXTURE by soak harnesses or
# re-audit scripts should NEVER spawn reviewer cards — they are synthetic,
# not real handoffs. Accepted forms (t_7493c1e2): title prefix "FIXTURE-" or
# "FIXTURE:" (anchored at start of the title), or a standalone "FIXTURE:"
# body line. STRICT on purpose: real cards whose prose says "fixture" (e.g.
# "fixture-reconcile", "the fixture `x`") must NOT match.
FIXTURE_RE = re.compile(r"\bFIXTURE:", re.I)
FIXTURE_TITLE_RE = re.compile(r"^\s*FIXTURE[-:]", re.I)

# True-owner routing (structural fix, kanban t_06f27b2d).
#
# A review-required handoff card whose *true* blocker is a gate/router defect, a
# maker-still-running dependency, or a Frank A3 hold must be routed to the owner
# that can actually clear it — NOT the board reviewer (os-reviewer on jarvis-os),
# where no reviewer action can resolve it. Default route kind 'reviewer' preserves
# the existing per-board reviewer assignment for genuine review work.
TRUE_OWNER_PROFILES = {
    "reviewer": None,                 # resolved per-board via BOARD_REVIEWERS
    "self_improve": "self-improve-engineer",  # Boris: gate/router defects
    "devops_owner": "devops",         # drive the still-running maker/source task
    "pm_frank": "jarvis-os-pm",       # Frank A3 hold escalation
    "elon": "elon",                   # owner-operator: genuine capability gaps
}

# Signals that a capability/gate block is actually a gate-rule defect (should
# go to self-improve-engineer, not the reviewer). This intentionally requires a
# defect word such as misapplied/false-positive/overbroad near the gate token:
# valid VERIFY_PASS / running-app evidence in a handoff is not itself a defect.
GATE_DEFECT_RE = re.compile(
    r"((verify_pass|completion gate|kernel completion|frontend/web|"
    r"running app|app[-_ ]?verification|review[-_ ]?required gate)"
    r".{0,100}(misappl|false[-_ ]?positive|wrong|incorrect|unnecessary|"
    r"should not|not required|over[-_ ]?broad|defect)|"
    r"(misappl|false[-_ ]?positive|wrong|incorrect|unnecessary|should not|"
    r"not required|over[-_ ]?broad|defect).{0,100}"
    r"(verify_pass|completion gate|kernel completion|frontend/web|running app|"
    r"app[-_ ]?verification|review[-_ ]?required gate))",
    re.I,
)
ROUTER_DEFECT_RE = re.compile(
    # Genuine router/automation DEFECT phrases only. Deliberately NOT the bare
    # substring "verdict-router": that token appears in status/diagnostic comments
    # ("verdict-router left this blocked for manual routing") that merely NAME the
    # verdict-router surfacing a verdict, not a defect requiring the owner-operator.
    # Matching the bare token misrouted genuine gate-false-positive cards (t_177ef664,
    # whose title mentions VERIFY_PASS) to elon instead of self-improve-engineer.
    r"(verdict[-_ ]?router\s*auto[-_ ]?close|auto[-_ ]?close|"
    r"critic read[-_ ]?only gate on kanban_create|"
    r"implementation child creation was blocked|child[-_ ]?creation (was )?blocked|"
    r"router defect|child creation blocked by)",
    re.I,
)
# Signals that the blocker is a Frank A3 / operator / maintainer approval hold.
FRANK_HOLD_RE = re.compile(
    r"(frank approval|frank gate|frank a3|a3 hold|operator approval|"
    r"maintainer approval|needs frank|requires frank|pending frank)",
    re.I,
)
# Signals that the blocker is the maker/source task still running / not delivered.
MAKER_RUNNING_RE = re.compile(
    r"(maker (is |still )?running|source task (is |still )?running|"
    r"blocked on .{0,40}running|maker has not|maker hasn't|"
    r"has not provided|hasn't provided|not provided the required|"
    r"source .{0,30} still (running|blocked)|waiting on (the )?maker)",
    re.I,
)
# A reviewer staged a fix but per the independence contract did NOT apply/run it;
# the remaining action is apply+test by devops, not a reviewer verdict.
STAGED_FIX_RE = re.compile(
    r"(staged .{0,30}fix|ready[- ]?to[- ]?apply|ready to run|did not apply|"
    r"didn't apply|did NOT apply|didn't run|did not run|ready to test|apply .{0,20}fix)",
    re.I,
)

def classify_route_kind(block_kind: str | None, reason: str | None, title_body: str) -> str:
    """Return the true-owner route kind for a review-required handoff.

    Default 'reviewer' preserves the existing per-board reviewer assignment for
    genuine review work. Gate/router defects, maker-still-running dependencies,
    staged-fix-awaiting-apply, and Frank A3 holds are rerouted to the owner that
    can actually clear them (kanban t_06f27b2d).

    Signal precedence (first match wins):
      1. Frank A3 / operator approval hold     -> pm_frank   (jarvis-os-pm to Frank)
      2. Maker / source still running/undelivered -> devops_owner (drive it)
      3. Router/automation defect (verdict-router auto-close, critic read-only
         gate on kanban_create, child-creation blocked) -> elon (owner-operator)
      4. Gate misapplication (VERIFY_PASS, completion gate, frontend-web,
         running-app)                            -> self_improve (Boris, gate-rule fix)
      5. Reviewer staged a fix but did not apply/run -> devops_owner
      6. dependency                             -> devops_owner
      7. anything else (genuine review)         -> reviewer (os-reviewer)

    NOTE: the router/automation-defect branch is checked BEFORE the generic
    gate-misapplication branch on purpose. A block reason may name a specific
    router/automation defect (e.g. "implementation child creation was blocked by
    the reviewer critic read-only gate on kanban_create") while the task TITLE
    also contains a generic gate token such as VERIFY_PASS. Routing that card to
    self-improve-engineer (a gate-RULE fixer) would be the exact misroute this
    task exists to eliminate; the owner-operator (elon) is the correct true owner
    for router/automation defects. See kanban t_be3fc92b / t_06f27b2d.
    """
    bk = (block_kind or "").lower()
    text = " ".join([reason or "", title_body or ""])
    # 1. Frank A3 / operator approval holds -> PM escalation to Frank.
    if bk == "frank_gate" or FRANK_HOLD_RE.search(text):
        return "pm_frank"
    # 2. Maker / source task still running or not delivered -> drive it (devops).
    if MAKER_RUNNING_RE.search(text):
        return "devops_owner"
    # 3. Router/automation defect (verdict-router auto-close, critic read-only
    #    gate on kanban_create, child-creation blocked) -> owner-operator (Elon).
    #    Checked BEFORE gate-misapplication so an explicit router-defect signal in
    #    the block reason wins over a generic gate token that may appear in the title.
    if ROUTER_DEFECT_RE.search(text):
        return "elon"
    # 4. Gate misapplication (VERIFY_PASS / completion gate / frontend-web /
    #    running-app) is a gate-RULE defect -> self-improve (Boris), any block_kind.
    if GATE_DEFECT_RE.search(text):
        return "self_improve"
    # 5. Reviewer staged a fix but did not apply/run it -> devops applies+tests.
    if STAGED_FIX_RE.search(text):
        return "devops_owner"
    # 6. Hard maker dependency.
    if bk == "dependency":
        return "devops_owner"
    # 7. Genuine capability gap with no gate/router signal -> owner-operator.
    if bk == "capability":
        return "elon"
    # 8. Everything else (genuine review) -> board reviewer.
    return "reviewer"

CREATED_BY = "review-required-auto-router"

# Authors whose comments are machine-noise that must not mask handoff markers.
# Filtered out of detection AND excerpt generation so the 5-comment excerpt
# window reliably shows the real handoff.
NOISE_AUTHORS = frozenset({
    "failure-classifier-cron",
    "kanban-failure-classifier-cron",
})

DEFAULT_BOARD_DIR = Path("/home/frank/.hermes/kanban/boards")
DEFAULT_PRIORITY = 80


@dataclass(frozen=True)
class Candidate:
    board: str
    source_id: str
    source_title: str
    source_assignee: str | None
    reviewer: str
    idempotency_key: str
    round_number: int
    detection_source: str  # "comment" or "block_event"
    latest_comment_ids: tuple[int, ...]
    latest_comment_excerpt: str
    block_kind: str | None = None
    block_reason: str | None = None
    route_kind: str = "reviewer"
    risk_classification: RiskClassification | None = None


class RouterError(RuntimeError):
    """Raised for deterministic router failures."""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    required = {
        "tasks": {"id", "title", "body", "assignee", "status", "priority", "created_by", "created_at", "workspace_kind", "idempotency_key"},
        "task_comments": {"id", "task_id", "author", "body", "created_at"},
        "task_events": {"task_id", "kind", "payload", "created_at"},
        "task_links": {"parent_id", "child_id"},
    }
    for table, cols in required.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        have = {row[1] for row in rows}
        missing = cols - have
        if missing:
            raise RouterError(f"{table} missing required columns: {sorted(missing)}")


# ── Comment helpers ──────────────────────────────────────────────────────────

def fetch_all_comments(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, author, body, created_at
        FROM task_comments
        WHERE task_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (task_id,),
    ).fetchall()


def filter_noise(comments: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Remove machine-noise comments from the list."""
    return [c for c in comments if c["author"] not in NOISE_AUTHORS]


def is_review_required_handoff(row: sqlite3.Row) -> bool:
    # Accept the two canonical handoff markers produced by builders/workers:
    #   - review-required: ...
    #   - review-required handoff: ...
    # Do NOT accept broad status prose such as "review-required unresolved: ...";
    # that phrasing is used by reviewer cards to say the SOURCE remains blocked
    # after CHANGES_REQUESTED and must not create nested REVIEW: REVIEW: cards.
    body = (row["body"] or "").casefold().lstrip()
    return body.startswith("review-required:") or body.startswith("review-required handoff:")


def is_review_verdict(row: sqlite3.Row) -> bool:
    return "review_verdict" in (row["body"] or "").casefold()


def comments_contain_review_required(comments: Iterable[sqlite3.Row]) -> bool:
    return any(is_review_required_handoff(row) for row in comments)


def comments_contain_review_verdict(comments: Iterable[sqlite3.Row]) -> bool:
    return any(is_review_verdict(row) for row in comments)


def latest_marker_requires_review(comments: Iterable[sqlite3.Row]) -> bool:
    """Return True when the newest review marker is a review-required handoff.

    A CHANGES_REQUESTED verdict followed by a rework handoff should route a new
    review.  A review verdict after a handoff should suppress routing until the
    implementer posts another handoff.
    """
    for row in comments:
        if is_review_required_handoff(row):
            return True
        if is_review_verdict(row):
            return False
    return False


def review_required_round(comments: Iterable[sqlite3.Row]) -> int:
    return sum(1 for row in comments if is_review_required_handoff(row))


# ── Block-event helpers ──────────────────────────────────────────────────────

def fetch_block_events(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    """Fetch kind='blocked' events, newest first."""
    return conn.execute(
        """
        SELECT id, kind, payload, created_at
        FROM task_events
        WHERE task_id = ? AND kind = 'blocked'
        ORDER BY created_at DESC, id DESC
        """,
        (task_id,),
    ).fetchall()


def block_event_is_review_required(event: sqlite3.Row) -> bool:
    try:
        payload = json.loads(event["payload"])
        reason = (payload.get("reason") or "").casefold().lstrip()
        # Block-event handoffs come from kanban_block(reason="review-required: ...").
        # Reviewer status blockers use "review-required unresolved: ..." and are
        # deliberately excluded so the router does not recursively review its own
        # review cards.
        return reason.startswith("review-required:")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False

def source_block_reason(conn: sqlite3.Connection, source_id: str) -> str:
    """Return the most recent block-event payload.reason for a source task (or '')."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    if not row:
        return ""
    try:
        payload = json.loads(row["payload"])
        return payload.get("reason") or ""
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ""


# ── Idempotency helpers ──────────────────────────────────────────────────────

def review_card_exists(conn: sqlite3.Connection, idempotency_key: str) -> bool:
    """Check whether an idempotency-keyed review card already exists."""
    row = conn.execute(
        """
        SELECT id
        FROM tasks
        WHERE idempotency_key = ?
          AND status != 'archived'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (idempotency_key,),
    ).fetchone()
    return row is not None


def existing_review_for_source(conn: sqlite3.Connection, source_id: str) -> bool:
    """Return True when any non-archived REVIEW card already mentions source_id.

    Catches manually-created review cards whose idempotency keys don't follow
    the auto-router naming convention (e.g. ``review-<source>-r1`` vs a seat's
    ``review-<source>-<custom>``).  Prevents the auto-router from creating a
    duplicate review when the seat already beat it to the punch.
    """
    row = conn.execute(
        """
        SELECT 1 FROM tasks
        WHERE status != 'archived'
          AND created_by = 'review-required-auto-router'
          AND idempotency_key LIKE ?
        UNION
        SELECT 1 FROM tasks
        WHERE status != 'archived'
          AND title LIKE 'REVIEW:%'
          AND title LIKE ?
        LIMIT 1
        """,
        (f"review-{source_id}-r%", f"%{source_id}%"),
    ).fetchone()
    return row is not None


def comment_excerpt(comments: Iterable[sqlite3.Row], max_len: int = 700) -> str:
    parts: list[str] = []
    for row in comments:
        body = (row["body"] or "").strip().replace("\r", "")
        body = " ".join(body.split())
        if body:
            parts.append(f"comment#{row['id']}: {body[:220]}")
    text = " | ".join(parts)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


# ── Risk manifest and discovery ─────────────────────────────────────────────

CHANGE_MANIFEST_RE = re.compile(r"^\s*change_manifest\s*:\s*(\{.*\})\s*$", re.I | re.M)


def risk_classification_from_body(body: str | None) -> RiskClassification:
    """Classify only an explicit JSON manifest in the source task body."""
    match = CHANGE_MANIFEST_RE.search(body or "")
    if not match:
        return classify_risk([], {})
    try:
        manifest = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return classify_risk(None, None)
    if not isinstance(manifest, dict):
        return classify_risk(None, None)
    return classify_risk(manifest.get("changed_paths"), manifest.get("change_flags"))


def risk_reviewer(classification: RiskClassification, board_reviewer: str) -> str:
    """Keep risk-bearing and fail-closed review cards on the risk lane."""
    if classification.requires_standalone_risk_review or classification.fail_closed:
        return "trading-risk-reviewer"
    return board_reviewer


# ── Discovery ────────────────────────────────────────────────────────────────

def discover_candidates(board_dir: Path, boards: Iterable[str] = BOARD_REVIEWERS.keys()) -> list[Candidate]:
    candidates: list[Candidate] = []
    for board in boards:
        if board not in BOARD_REVIEWERS:
            raise RouterError(f"unknown board {board!r}; expected one of {sorted(BOARD_REVIEWERS)}")
        db_path = board_dir / board / "kanban.db"
        if not db_path.is_file():
            continue
        with connect(db_path) as conn:
            ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT id, title, assignee, block_kind, body
                FROM tasks
                WHERE status = 'blocked'
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
            for task in rows:
                source_id = task["id"]

                # Skip fixture-card sources: synthetic audit/re-audit cards are NOT
                # real review-required handoffs — they waste reviewer cycles.
                # Title uses the anchored prefix pattern; body uses the standalone
                # "FIXTURE:" word pattern (see FIXTURE_TITLE_RE / FIXTURE_RE).
                _title = (task["title"] or "").casefold()
                _body  = (task["body"]   or "").casefold()
                if FIXTURE_TITLE_RE.search(_title) or FIXTURE_RE.search(_body):
                    continue

                comments = fetch_all_comments(conn, source_id)
                filtered = filter_noise(comments)
                block_events = fetch_block_events(conn, source_id)

                # ── Detection path 1: comment-based handoff ─────────────────
                # This is the existing path. Noise-filtered comments let the
                # marker survive even when multiple classifier comments are
                # posted after the handoff. Round numbers stay stable so
                # existing router-created cards are never duplicated.
                if comments_contain_review_required(filtered):
                    if not latest_marker_requires_review(filtered):
                        continue
                    round_number = review_required_round(filtered)
                    if round_number < 1:
                        continue
                    key = f"review-{source_id}-r{round_number}"
                    if review_card_exists(conn, key):
                        continue
                    if existing_review_for_source(conn, source_id):
                        continue
                    latest_comments = filtered[:5]
                    _reason = source_block_reason(conn, source_id)
                    _title_body = "\n".join([task["title"] or "", comment_excerpt(latest_comments)])
                    classification = risk_classification_from_body(task["body"])
                    # Known paper-only work stays below the standalone risk lane;
                    # CI-green plus inline review remains the mandatory policy.
                    if not classification.requires_standalone_risk_review and not classification.fail_closed:
                        continue
                    _route_kind = classify_route_kind(task["block_kind"], _reason, _title_body)
                    _reviewer = TRUE_OWNER_PROFILES[_route_kind] if _route_kind != "reviewer" else risk_reviewer(classification, BOARD_REVIEWERS[board])
                    candidates.append(
                        Candidate(
                            board=board,
                            source_id=source_id,
                            source_title=task["title"],
                            source_assignee=task["assignee"],
                            reviewer=_reviewer,
                            idempotency_key=key,
                            round_number=round_number,
                            detection_source="comment",
                            latest_comment_ids=tuple(int(c["id"]) for c in latest_comments),
                            latest_comment_excerpt=comment_excerpt(latest_comments),
                            block_kind=task["block_kind"],
                            block_reason=_reason,
                            route_kind=_route_kind,
                            risk_classification=classification,
                        )
                    )
                    continue

                # ── Detection path 2: block-event handoff (fallback) ────────
                # Some workers call kanban_block() instead of posting a
                # handoff comment.  The review-required marker lives in the
                # block-event's payload.reason. Only fires when no comment
                # handoff was found, so round numbers don't drift for tasks
                # that already have a handoff comment.
                has_event_handoff = any(
                    block_event_is_review_required(e) for e in block_events
                )
                if has_event_handoff:
                    # Verify no review verdict was posted after the block.
                    if comments_contain_review_verdict(filtered):
                        continue
                    round_number = sum(
                        1 for e in block_events if block_event_is_review_required(e)
                    )
                    if round_number < 1:
                        continue
                    key = f"review-{source_id}-r{round_number}"
                    if review_card_exists(conn, key):
                        continue
                    if existing_review_for_source(conn, source_id):
                        continue
                    latest_comments = filtered[:5]
                    _reason = source_block_reason(conn, source_id)
                    _title_body = "\n".join([task["title"] or "", comment_excerpt(latest_comments)])
                    classification = risk_classification_from_body(task["body"])
                    # Known paper-only work stays below the standalone risk lane;
                    # CI-green plus inline review remains the mandatory policy.
                    if not classification.requires_standalone_risk_review and not classification.fail_closed:
                        continue
                    _route_kind = classify_route_kind(task["block_kind"], _reason, _title_body)
                    _reviewer = TRUE_OWNER_PROFILES[_route_kind] if _route_kind != "reviewer" else risk_reviewer(classification, BOARD_REVIEWERS[board])
                    candidates.append(
                        Candidate(
                            board=board,
                            source_id=source_id,
                            source_title=task["title"],
                            source_assignee=task["assignee"],
                            reviewer=_reviewer,
                            idempotency_key=key,
                            round_number=round_number,
                            detection_source="block_event",
                            latest_comment_ids=tuple(int(c["id"]) for c in latest_comments),
                            latest_comment_excerpt=comment_excerpt(latest_comments),
                            block_kind=task["block_kind"],
                            block_reason=_reason,
                            route_kind=_route_kind,
                            risk_classification=classification,
                        )
                    )
    return candidates


# ── Card creation ────────────────────────────────────────────────────────────

def new_task_id(conn: sqlite3.Connection) -> str:
    for _ in range(10):
        task_id = "t_" + secrets.token_hex(4)
        row = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return task_id
    raise RouterError("could not allocate unique task id after 10 attempts")


def review_body(candidate: Candidate) -> str:
    _owner_action = {
        "reviewer": "Reviewer action: inspect the source task comments/diff/tests, leave REVIEW_VERDICT=APPROVED or REVIEW_VERDICT=CHANGES_REQUESTED on the source task, then unblock or keep blocked as appropriate.",
        "self_improve": "Self-improve/Boris action: this block is a GATE/ROUTER DEFECT, not a review item. Inspect the source block reason, fix the gate/router rule, and add a regression test; do not wait for a reviewer verdict.",
        "devops_owner": "DevOps action: this block is a MAKER-STILL-RUNNING dependency. Drive the still-running source/maker task to completion (or a verdict); do not wait for a reviewer verdict.",
        "pm_frank": "PM/Frank action: this block is a Frank A3 / operator approval HOLD. jarvis-os-pm must escalate to Frank for the approval gate; do not wait for a reviewer verdict.",
        "elon": "Owner-operator action: this block is a genuine CAPABILITY GAP. Elon should resolve or delegate the capability; do not wait for a reviewer verdict.",
    }.get(candidate.route_kind, "Action: inspect the source task and resolve the true blocker.")
    return (
        "Auto-routed reviewer card for an orphaned review-required handoff.\n\n"
        f"Source board: `{candidate.board}`\n"
        f"Source task: `{candidate.source_id}`\n"
        f"Source title: {candidate.source_title}\n"
        f"Source assignee: {candidate.source_assignee or '-'}\n"
        f"Review-required round: {candidate.round_number}\n"
        f"Detection source: {candidate.detection_source}\n"
        f"Block kind: {candidate.block_kind or 'unknown'}\n"
        f"True-owner route: {candidate.route_kind} (assigned to `{candidate.reviewer}`)\n"
        f"Risk classification: {candidate.risk_classification}\n"
        f"Block reason (source): {candidate.block_reason or '(none captured)'}\n"
        f"Idempotency key: `{candidate.idempotency_key}`\n\n"
        "Important: this card is intentionally NOT parent-linked to the source task; "
        "kanban parents are hard dependencies and would deadlock review of a blocked source.\n\n"
        f"{_owner_action}\n\n"
        "Latest source comments scanned:\n"
        f"{candidate.latest_comment_excerpt or '(no comment body excerpt available)'}\n"
    )


def insert_review_card(board_dir: Path, candidate: Candidate) -> str:
    db_path = board_dir / candidate.board / "kanban.db"
    now = int(time.time())
    with connect(db_path) as conn:
        ensure_schema(conn)
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
            (candidate.idempotency_key,),
        ).fetchone()
        if existing:
            return str(existing["id"])
        task_id = new_task_id(conn)
        title = f"REVIEW: {candidate.source_title} ({candidate.source_id})"
        body = review_body(candidate)
        with conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, body, assignee, status, priority,
                    created_by, created_at, workspace_kind, idempotency_key
                ) VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, 'scratch', ?)
                """,
                (
                    task_id,
                    title,
                    body,
                    candidate.reviewer,
                    DEFAULT_PRIORITY,
                    CREATED_BY,
                    now,
                    candidate.idempotency_key,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_events (task_id, run_id, kind, payload, created_at)
                VALUES (?, NULL, 'created', ?, ?)
                """,
                (
                    task_id,
                    json.dumps(
                        {
                            "assignee": candidate.reviewer,
                            "status": "ready",
                            "parents": [],
                            "created_by": CREATED_BY,
                            "source_board": candidate.board,
                            "source_task": candidate.source_id,
                            "idempotency_key": candidate.idempotency_key,
                            "route_kind": candidate.route_kind,
                            "risk_classification": candidate.risk_classification.__dict__ if candidate.risk_classification else None,
                            "block_kind": candidate.block_kind,
                            "block_reason": candidate.block_reason,
                        },
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return task_id


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print planned review cards without writing")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output when work is found")
    parser.add_argument("--board-dir", default=str(DEFAULT_BOARD_DIR), help="kanban boards directory")
    parser.add_argument(
        "--boards",
        default=",".join(BOARD_REVIEWERS),
        help="comma-separated board slugs to scan (default: all known)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    board_dir = Path(os.path.expanduser(args.board_dir)).resolve()
    boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    candidates = discover_candidates(board_dir, boards)

    if args.dry_run:
        payload = {
            "dry_run": True,
            "candidates": [candidate.__dict__ for candidate in candidates],
            "count": len(candidates),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not candidates:
        return 0

    created: list[dict[str, str]] = []
    for candidate in candidates:
        review_id = insert_review_card(board_dir, candidate)
        created.append(
            {
                "board": candidate.board,
                "source_task": candidate.source_id,
                "review_task": review_id,
                "reviewer": candidate.reviewer,
                "idempotency_key": candidate.idempotency_key,
            }
        )

    if args.json:
        print(json.dumps({"created": created, "count": len(created)}, indent=2, sort_keys=True))
    else:
        print(
            "review-required-auto-router: "
            + ", ".join(
                f"{item['board']}/{item['source_task']}->{item['review_task']}({item['reviewer']})"
                for item in created
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RouterError as exc:
        print(f"review-required-auto-router error: {exc}", file=sys.stderr)
        raise SystemExit(2)
