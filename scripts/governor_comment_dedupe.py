#!/usr/bin/env python3
"""Dry-run guard for duplicate governor classification comments.

Read-only: inspects a board's task_comments table and tells governor/recovery
sweeps whether a classification comment should be suppressed because the same
(task_id, classification, owner_packet) was already recorded inside a TTL.

Three coverage classes:
  1. provider/auth classification comments (legacy class) — keyed on a
     (classification, owner_packet) pair with alias normalization.
  2. proposal-aging-guard comments (added 2026-07-13, per 03:46Z governor
     self-correction) — keyed on a (proposal_id, aging_guard_marker) pair so a
     governor never re-posts a redundant escalation nudge on a proposal that is
     already correctly escalated / decider-routed.
  3. deterministic finding-key comments (added 2026-08-03, t_5d0658a7) — keyed
     on a stable identity string `verdict-sweep:v1:<board>:<task_id>:comment:
     <target_comment_id>:action:<action>` (modelled on the verdict router's
     idempotency key). Classification is caller-chosen and is NOT a stable
     identity for a finding, so a repeat route of the SAME unactioned verdict
     must be suppressed even when the caller supplies a new --classification
     string each cycle. The caller embeds the exact key string in the comment
     it writes; the next cycle's scan finds it and suppresses.

The CLI stays backward-compatible: the existing --classification / --owner-packet
path is used for class 1. A new --proposal-aging-guard flag selects class 2.
The new deterministic path is selected by --finding-key or --target-comment-id
(plus --action) and takes precedence over the legacy path when present.
Credential/approval-critical escalations are NEVER suppressed (existing
carve-out preserved and now enforced in code).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

BOARD_ROOT = Path("/home/frank/.hermes/kanban/boards")

# Marker tokens that identify an EXISTING governor aging-guard / escalation nudge
# comment on a PROPOSAL card. These are deliberately narrow: they must match the
# governor's own ELON-AGING-GUARD signature, not generic words like "escalated"
# or "decision requested" that appear in unrelated Frank-gated cards. A broad
# match would cause false SUPPRESS and let a genuinely-new nudge be skipped.
AGING_GUARD_MARKERS = [
    "aging-guard",                       # governor ELON-AGING-GUARD signature
    "proposal past decision deadline",   # past-deadline nudge text
    "decision deadline reached",         # deadline-reached nudge text
    "aging-guard nudge",                 # explicit nudge label
]

# Deterministic finding-key namespace (class 3, t_5d0658a7). Modelled on the
# verdict router's idempotency key shape `verdict-router:v1:<board>:<task_id>:
# comment:<comment_id>:action:<action>`. The governor sweep embeds the exact
# key string in the comment it writes; the next cycle's scan finds it.
FINDING_KEY_PREFIX = "verdict-sweep:v1:"


def build_finding_key(*, board: str, task_id: str, target_comment_id: int, action: str) -> str:
    return f"{FINDING_KEY_PREFIX}{board}:{task_id}:comment:{target_comment_id}:action:{action}"


# Credential/approval-critical carve-out (never suppress). Matches the
# caller-supplied classification / owner_packet / finding-key text. These
# escalations are safety-critical: a duplicate-write guard must NEVER silence
# them, even when a deterministic key match exists. Deliberately narrow and
# explicit (mirrors the cron prompt's "Never suppress credential/approval-
# critical escalations" rule); a broad match would let a genuinely-new
# critical escalation be skipped.
CRITICAL_MARKER_RE = re.compile(
    r"(?i)\b(credential|credentials|api[- ]?key|secret|password|token|"
    r"approval[- ]?critical|frank[- ]?approval|needs[- ]?approval|"
    r"payment|spend|billing|deploy|production)\b"
)


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def tokens(text: str) -> list[str]:
    return [tok for tok in normalize(text).split() if tok]


def classification_aliases(classification: str) -> list[str]:
    raw = classification.strip()
    aliases = [raw]
    norm = normalize(raw)
    if norm == "provider auth pre reasoning":
        aliases.extend(
            [
                "provider_auth_pre_reasoning",
                "provider/auth/fallback-quota",
                "provider_auth/rate-limit",
                "provider/auth pre-reasoning",
                "interrupted by provider/auth/fallback-quota",
                "provider/auth fallback failure before useful reasoning",
            ]
        )
    return aliases


def body_matches(body: str, classification: str, owner_packet: str) -> bool:
    body_norm = normalize(body)
    owner_norm = normalize(owner_packet)
    if owner_norm not in body_norm:
        return False

    for alias in classification_aliases(classification):
        alias_norm = normalize(alias)
        if alias_norm and alias_norm in body_norm:
            return True

    # Fallback for natural-language labels: require all non-trivial words.
    key_tokens = [tok for tok in tokens(classification) if len(tok) > 2]
    return bool(key_tokens) and all(tok in body_norm for tok in key_tokens)


def board_db(board: str) -> Path:
    if "/" in board or board in {"", ".", ".."}:
        raise ValueError(f"invalid board slug: {board!r}")
    return BOARD_ROOT / board / "kanban.db"


def find_duplicate(
    *, board: str, task_id: str, classification: str, owner_packet: str, ttl_seconds: int, now: int
) -> dict[str, Any]:
    db = board_db(board)
    if not db.exists():
        raise FileNotFoundError(str(db))

    since = now - ttl_seconds
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, author, body, created_at
            FROM task_comments
            WHERE task_id=? AND created_at>=?
            ORDER BY created_at DESC, id DESC
            """,
            (task_id, since),
        ).fetchall()
    finally:
        con.close()

    for row in rows:
        if body_matches(row["body"] or "", classification, owner_packet):
            age_seconds = max(0, now - int(row["created_at"]))
            return {
                "decision": "SUPPRESS",
                "reason": "duplicate_classification_within_ttl",
                "dedupe_key": {
                    "board": board,
                    "task_id": task_id,
                    "classification": classification,
                    "owner_packet": owner_packet,
                    "ttl_seconds": ttl_seconds,
                },
                "matched_comment": {
                    "id": int(row["id"]),
                    "author": row["author"],
                    "created_at": int(row["created_at"]),
                    "age_seconds": age_seconds,
                    "excerpt": (row["body"] or "")[:300],
                },
                "comments_scanned": len(rows),
            }

    return {
        "decision": "COMMENT",
        "reason": "no_matching_classification_owner_packet_within_ttl",
        "dedupe_key": {
            "board": board,
            "task_id": task_id,
            "classification": classification,
            "owner_packet": owner_packet,
            "ttl_seconds": ttl_seconds,
        },
        "comments_scanned": len(rows),
    }


def find_proposal_aging_guard_duplicate(
    *, board: str, task_id: str, ttl_seconds: int, now: int
) -> dict[str, Any]:
    """Class 2: suppress a redundant proposal-aging-guard / escalation nudge.

    If a recent comment on the same proposal task already carries an
    aging-guard / escalation marker, a new nudge is redundant churn and must be
    suppressed. This closes the 03:46Z governor self-correction loop: the
    governor must no-op when the proposal is already correctly escalated and
    decider-routed.
    """
    db = board_db(board)
    if not db.exists():
        raise FileNotFoundError(str(db))

    since = now - ttl_seconds
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, author, body, created_at
            FROM task_comments
            WHERE task_id=? AND created_at>=?
            ORDER BY created_at DESC, id DESC
            """,
            (task_id, since),
        ).fetchall()
    finally:
        con.close()

    for row in rows:
        body_norm = normalize(row["body"] or "")
        for marker in AGING_GUARD_MARKERS:
            if normalize(marker) in body_norm:
                age_seconds = max(0, now - int(row["created_at"]))
                return {
                    "decision": "SUPPRESS",
                    "reason": "proposal_aging_guard_already_present_within_ttl",
                    "dedupe_key": {
                        "board": board,
                        "task_id": task_id,
                        "classification": "proposal-aging-guard",
                        "owner_packet": task_id,
                        "ttl_seconds": ttl_seconds,
                    },
                    "matched_comment": {
                        "id": int(row["id"]),
                        "author": row["author"],
                        "created_at": int(row["created_at"]),
                        "age_seconds": age_seconds,
                        "excerpt": (row["body"] or "")[:300],
                    },
                    "comments_scanned": len(rows),
                }

    return {
        "decision": "COMMENT",
        "reason": "no_proposal_aging_guard_marker_within_ttl",
        "dedupe_key": {
            "board": board,
            "task_id": task_id,
            "classification": "proposal-aging-guard",
            "owner_packet": task_id,
            "ttl_seconds": ttl_seconds,
        },
        "comments_scanned": len(rows),
    }


def is_critical_escalation(*, classification: str, owner_packet: str, finding_key: str) -> bool:
    """True when the caller-supplied identity text names a credential/approval-
    critical escalation. Those are NEVER suppressed (safety carve-out)."""
    haystack = " ".join([classification or "", owner_packet or "", finding_key or ""])
    return bool(CRITICAL_MARKER_RE.search(haystack))


def find_finding_key_duplicate(
    *, board: str, task_id: str, finding_key: str, ttl_seconds: int, now: int
) -> dict[str, Any]:
    """Class 3: suppress a repeat governor route of the SAME finding.

    The caller embeds the exact deterministic key string (built from board,
    task_id, target comment id, action — NOT from the caller-chosen
    classification) in the comment it writes. If a comment on the task already
    carries that key inside the TTL, the finding is already routed and waiting
    on a seat; a new evidence comment would be manufactured activity, so it is
    suppressed with a distinct seat-wait / aging reason. The caller emits an
    aging escalation instead of a repeat comment.
    """
    db = board_db(board)
    if not db.exists():
        raise FileNotFoundError(str(db))

    since = now - ttl_seconds
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, author, body, created_at
            FROM task_comments
            WHERE task_id=? AND created_at>=?
            ORDER BY created_at DESC, id DESC
            """,
            (task_id, since),
        ).fetchall()
    finally:
        con.close()

    for row in rows:
        if finding_key in (row["body"] or ""):
            age_seconds = max(0, now - int(row["created_at"]))
            return {
                "decision": "SUPPRESS",
                "reason": "seat_wait_finding_already_routed_within_ttl",
                "dedupe_key": {
                    "board": board,
                    "task_id": task_id,
                    "finding_key": finding_key,
                    "classification": None,  # caller-chosen label is NOT identity
                    "owner_packet": None,
                    "ttl_seconds": ttl_seconds,
                },
                "matched_comment": {
                    "id": int(row["id"]),
                    "author": row["author"],
                    "created_at": int(row["created_at"]),
                    "age_seconds": age_seconds,
                    "excerpt": (row["body"] or "")[:300],
                },
                "comments_scanned": len(rows),
            }

    return {
        "decision": "COMMENT",
        "reason": "no_matching_finding_key_within_ttl",
        "dedupe_key": {
            "board": board,
            "task_id": task_id,
            "finding_key": finding_key,
            "classification": None,
            "owner_packet": None,
            "ttl_seconds": ttl_seconds,
        },
        "comments_scanned": len(rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only governor classification comment dedupe guard")
    parser.add_argument("--board", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--classification", default=None)
    parser.add_argument("--owner-packet", default=None)
    parser.add_argument("--ttl-hours", type=float, default=2.0)
    parser.add_argument("--now", type=int, default=None, help="Unix epoch override for deterministic tests")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    # Class 2 selector
    parser.add_argument(
        "--proposal-aging-guard",
        action="store_true",
        help="Use the proposal-aging-guard dedupe class (suppress redundant escalation nudges on proposals already escalated).",
    )
    # Class 3 deterministic finding-key inputs (t_5d0658a7)
    parser.add_argument(
        "--target-comment-id",
        type=int,
        default=None,
        help="Stable identity: the source verdict comment id this finding is routed from. With --action, builds the deterministic finding key; suppression is then decided on (board, task_id, target_comment_id, action) regardless of --classification text.",
    )
    parser.add_argument(
        "--action",
        default=None,
        help="Action label for the deterministic finding key (e.g. needs-pm). Required with --target-comment-id unless --finding-key is supplied.",
    )
    parser.add_argument(
        "--finding-key",
        default=None,
        help="Explicit deterministic finding key string (e.g. verdict-sweep:v1:<board>:<task_id>:comment:<comment_id>:action:<action>). Takes precedence over --target-comment-id/--action and over the legacy classification path.",
    )
    parser.add_argument(
        "--critical-escalation",
        action="store_true",
        help="Mark this escalation credential/approval-critical. It is NEVER suppressed (safety carve-out); always returns COMMENT.",
    )
    args = parser.parse_args(argv)

    ttl_seconds = int(args.ttl_hours * 3600)
    if ttl_seconds <= 0:
        parser.error("--ttl-hours must be positive")
    now = int(args.now if args.now is not None else time.time())

    # Deterministic finding-key path (class 3) takes precedence when present.
    finding_key = args.finding_key
    if finding_key is None and args.target_comment_id is not None:
        if not args.action:
            parser.error("--target-comment-id requires --action (or pass --finding-key)")
        finding_key = build_finding_key(
            board=args.board,
            task_id=args.task_id,
            target_comment_id=args.target_comment_id,
            action=args.action,
        )

    # Safety carve-out: never suppress credential/approval-critical escalations,
    # even when a deterministic key match exists. This is enforced BEFORE any
    # scan so a critical escalation always returns COMMENT.
    try:
        if args.critical_escalation or is_critical_escalation(
            classification=args.classification or "",
            owner_packet=args.owner_packet or "",
            finding_key=finding_key or "",
        ):
            result = {
                "decision": "COMMENT",
                "reason": "critical_escalation_never_suppressed",
                "dedupe_key": {
                    "board": args.board,
                    "task_id": args.task_id,
                    "classification": args.classification,
                    "owner_packet": args.owner_packet,
                    "finding_key": finding_key,
                    "ttl_seconds": ttl_seconds,
                },
                "comments_scanned": 0,
            }
        elif finding_key is not None:
            result = find_finding_key_duplicate(
                board=args.board,
                task_id=args.task_id,
                finding_key=finding_key,
                ttl_seconds=ttl_seconds,
                now=now,
            )
        elif args.proposal_aging_guard:
            # For class 2, --classification/--owner-packet are ignored; the
            # proposal id is the task id. Keep --classification optional-friendly
            # by allowing a placeholder, but it is not used for matching.
            result = find_proposal_aging_guard_duplicate(
                board=args.board,
                task_id=args.task_id,
                ttl_seconds=ttl_seconds,
                now=now,
            )
        else:
            if not args.classification or not args.owner_packet:
                parser.error("legacy path requires --classification and --owner-packet (or use --finding-key / --target-comment-id / --proposal-aging-guard)")
            result = find_duplicate(
                board=args.board,
                task_id=args.task_id,
                classification=args.classification,
                owner_packet=args.owner_packet,
                ttl_seconds=ttl_seconds,
                now=now,
            )
    except Exception as exc:
        print(json.dumps({"decision": "ERROR", "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2

    if args.format == "json":
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"{result['decision']}: {result['reason']}")
        if result["decision"] == "SUPPRESS":
            match = result["matched_comment"]
            print(
                f"matched_comment id={match['id']} author={match['author']} "
                f"age_seconds={match['age_seconds']} excerpt={match['excerpt']!r}"
            )
        print(f"dedupe_key={json.dumps(result['dedupe_key'], sort_keys=True)}")
        print(f"comments_scanned={result['comments_scanned']}")
    return 0 if result["decision"] in {"SUPPRESS", "COMMENT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
