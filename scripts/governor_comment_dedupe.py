#!/usr/bin/env python3
"""Dry-run guard for duplicate governor classification comments.

Read-only: inspects a board's task_comments table and tells governor/recovery
sweeps whether a classification comment should be suppressed because the same
(task_id, classification, owner_packet) was already recorded inside a TTL.

Two coverage classes:
  1. provider/auth classification comments (legacy class) — keyed on a
     (classification, owner_packet) pair with alias normalization.
  2. proposal-aging-guard comments (added 2026-07-13, per 03:46Z governor
     self-correction) — keyed on a (proposal_id, aging_guard_marker) pair so a
     governor never re-posts a redundant escalation nudge on a proposal that is
     already correctly escalated / decider-routed.

The CLI stays backward-compatible: the existing --classification / --owner-packet
path is used for class 1. A new --proposal-aging-guard flag selects class 2.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only governor classification comment dedupe guard")
    parser.add_argument("--board", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--classification", required=True)
    parser.add_argument("--owner-packet", required=True)
    parser.add_argument("--ttl-hours", type=float, default=2.0)
    parser.add_argument("--now", type=int, default=None, help="Unix epoch override for deterministic tests")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    # Class 2 selector
    parser.add_argument(
        "--proposal-aging-guard",
        action="store_true",
        help="Use the proposal-aging-guard dedupe class (suppress redundant escalation nudges on proposals already escalated).",
    )
    args = parser.parse_args(argv)

    ttl_seconds = int(args.ttl_hours * 3600)
    if ttl_seconds <= 0:
        parser.error("--ttl-hours must be positive")
    now = int(args.now if args.now is not None else time.time())

    try:
        if args.proposal_aging_guard:
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
