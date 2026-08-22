#!/usr/bin/env python3
"""Dry-run guard for duplicate governor classification comments.

Read-only: inspects a board's task_comments table and tells governor/recovery
sweeps whether a classification comment should be suppressed because the same
(task_id, classification, owner_packet) was already recorded inside a TTL.

Four coverage classes:
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
  4. governor-refresh comments (added 2026-08-22, t_2044aea3) — keyed on a
     stable identity `governor-refresh:v1:<board>:<task_id>:<owner_slug>:
     <residual_hash>` where residual_hash = sha256(canonicalize(residual))[:12]
     and residual is the DURABLE evidence (e.g. `cron_stale_direct_rows=1`),
     canonicalized lowercase/alnum. Classification is caller-chosen and varies
     every governor cycle for the same long-lived A3/stale-direct blocker, so
     class 1 cannot see those refreshes as duplicates. Class 4 keys on the
     stable residual fingerprint instead: a repeat same-owner refresh whose
     residual has not materially changed is suppressed (cycle-log-only) within
     the TTL; a material residual change -> different hash -> COMMENT; TTL
     aging -> a fresh escalation comment is allowed. The credential/approval-
     critical carve-out for class 4 is evaluated on the explicit
     --critical-escalation flag / the durable residual content, NOT on
     incidental classification tokens (a bare `deploy`/`production` in the
     classification must not force routine A3-deploy-gate refreshes to COMMENT
     forever). Classes 1-3 keep the original carve-out unchanged.
  5. mechanism-liveness comments (added 2026-08-22, t_3d108e24) — keyed on a
     stable identity `mechanism-liveness:v1:<board>:<task_id>:mechanism:<key>:
     owner:<card>` (board + mechanism key + owner card; NOT the caller-chosen
     classification, which the governor varied every cycle and caused the
     2026-08-11 breaker comment churn on t_ba2b1bda). The durable owner packet
     is fingerprinted into matched comments as `owner-packet-hash=<sha256(
     canonicalize(owner_packet))[:12]>`. An unchanged same-owner RED inside
     the TTL is SUPPRESS (governance-log no-op only); after N continuous hours
     (default 4, MECHANISM_LIVENESS_AGING_HOURS) with the same byte-unchanged
     packet the guard returns AGING so the governor emits exactly ONE aging
     escalation to jarvis-os-pm/os-reviewer (embedding the key, the
     mechanism-liveness-aging-escalation marker and the hash); after that it
     is SUPPRESS again until the packet changes (different hash -> COMMENT) or
     the TTL expires. Critical credential/approval escalations are never
     suppressed (the carve-out is evaluated on the durable identity, matching
     classes 1-3).

The CLI stays backward-compatible: the existing --classification / --owner-packet
path is used for class 1. A new --proposal-aging-guard flag selects class 2.
The new deterministic path is selected by --finding-key or --target-comment-id
(plus --action) and takes precedence over the legacy path when present. The
governor-refresh path is selected by --governor-refresh (plus --owner-packet /
--residual, or an explicit --refresh-key) with precedence class-3 > class-4 >
class-2 > class-1.
Credential/approval-critical escalations are NEVER suppressed (existing
carve-out preserved and now enforced in code).
"""
from __future__ import annotations

import argparse
import hashlib
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


# Governor-refresh namespace (class 4, t_2044aea3). Stable identity is the
# DURABLE residual evidence (e.g. `cron_stale_direct_rows=1`) fingerprinted
# into the key — NOT the caller-chosen classification, which the governor
# varies every cycle, and NOT a changing probe filename/timestamp. Shape:
#   governor-refresh:v1:<board>:<task_id>:<owner_slug>:<residual_hash>
# residual_hash = sha256(canonicalize(residual))[:12].
REFRESH_KEY_PREFIX = "governor-refresh:v1:"


def canonicalize(text: str) -> str:
    """Lowercase the text and keep only alphanumerics (durable residual norm).

    This is deliberately coarser than normalize(): the residual is hashed, so
    we drop everything that is not a stable [a-z0-9] token. A changing probe
    filename, timestamp, or punctuation must not perturb the fingerprint.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def owner_slug(owner_packet: str) -> str:
    """Stable slug for an owner_packet (e.g. `jarvis-os/t_62adfa66`)."""
    norm = canonicalize(owner_packet)
    # Keep a readable dash-separated slug; fall back to a hash when empty.
    readable = re.sub(r"[^a-z0-9]+", "-", (owner_packet or "").lower()).strip("-")
    return readable or f"h{hashlib.sha256(norm.encode()).hexdigest()[:8]}"


def build_refresh_key(*, board: str, task_id: str, owner_packet: str, residual: str) -> str:
    residual_hash = hashlib.sha256(canonicalize(residual).encode("utf-8")).hexdigest()[:12]
    return f"{REFRESH_KEY_PREFIX}{board}:{task_id}:{owner_slug(owner_packet)}:{residual_hash}"


# Mechanism-liveness namespace (class 5, t_3d108e24). Stable identity is
# board + mechanism key + owner card — NOT the caller-chosen classification,
# which the governor varied every cycle (mechanism-liveness-breaker-still-red
# -> mechanism-breaker-red-still-current -> unified-health-breaker-red-current
# -> ...) and which made body_matches() return COMMENT every cycle on the
# 2026-08-11 breaker incident (t_ba2b1bda, ~15 near-identical comments over
# ~18h). Shape:
#   mechanism-liveness:v1:<board>:<task_id>:mechanism:<key>:owner:<card>
# The durable owner packet is fingerprinted (owner-packet-hash) so a
# byte-changed packet still routes; the aging marker marks the single aging
# escalation the governor emits to jarvis-os-pm/os-reviewer.
MECHANISM_LIVENESS_KEY_PREFIX = "mechanism-liveness:v1:"
MECHANISM_LIVENESS_AGING_MARKER = "mechanism-liveness-aging-escalation"

# Documented constant (t_3d108e24): N continuous hours RED with an unchanged
# owner packet before exactly ONE aging escalation is due. The class-5 TTL
# defaults to 24h so the first-sighting comment stays inside the scan window
# until the aging window (4h) has elapsed with margin.
MECHANISM_LIVENESS_AGING_HOURS = 4.0
MECHANISM_LIVENESS_TTL_HOURS = 24.0

# Comment-embedded fingerprint marker the governor writes next to the key so
# the next cycle's scan can prove the owner packet is byte-unchanged.
OWNER_PACKET_HASH_RE = re.compile(r"owner-packet-hash=([0-9a-f]{12})")


def owner_packet_hash(owner_packet: str) -> str:
    """Stable 12-hex fingerprint of the durable owner packet (byte-change
    detection). Mirrors the class-4 residual hash: canonicalized alnum text
    so a changing timestamp/punctuation does not perturb the fingerprint."""
    return hashlib.sha256(canonicalize(owner_packet).encode("utf-8")).hexdigest()[:12]


def build_mechanism_liveness_key(*, board: str, task_id: str, mechanism_key: str, owner_card: str) -> str:
    return (
        f"{MECHANISM_LIVENESS_KEY_PREFIX}{board}:{task_id}:"
        f"mechanism:{mechanism_key}:owner:{owner_card}"
    )


def _mechanism_liveness_packet_hash(body: str) -> str | None:
    m = OWNER_PACKET_HASH_RE.search(body or "")
    return m.group(1) if m else None


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


def find_refresh_duplicate(
    *, board: str, task_id: str, refresh_key: str, ttl_seconds: int, now: int
) -> dict[str, Any]:
    """Class 4: suppress a repeat same-owner A3/stale-direct refresh.

    The governor embeds the exact deterministic refresh key (built from board,
    task_id, owner_slug, and a hash of the DURABLE residual — NOT the
    caller-chosen classification) in the refresh comment it writes. If a
    comment on the task already carries that key inside the TTL, the same
    residual is already reported and the card is waiting on the same gate; a
    fresh comment would be spam, so it is suppressed with the cycle-log-only
    reason. Material evidence change => different residual => different hash
    => different key => COMMENT. TTL aging => the last refresh falls out of the
    window => a fresh escalation comment is allowed.
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
        if refresh_key in (row["body"] or ""):
            age_seconds = max(0, now - int(row["created_at"]))
            return {
                "decision": "SUPPRESS",
                "reason": "governor_refresh_unchanged_within_ttl",
                "dedupe_key": {
                    "board": board,
                    "task_id": task_id,
                    "refresh_key": refresh_key,
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
        "reason": "no_matching_refresh_key_within_ttl",
        "dedupe_key": {
            "board": board,
            "task_id": task_id,
            "refresh_key": refresh_key,
            "classification": None,  # caller-chosen label is NOT identity
            "owner_packet": None,
            "ttl_seconds": ttl_seconds,
        },
        "comments_scanned": len(rows),
    }


def find_mechanism_liveness_duplicate(
    *,
    board: str,
    task_id: str,
    mechanism_key: str,
    owner_card: str,
    owner_packet: str,
    ttl_seconds: int,
    aging_seconds: int,
    now: int,
) -> dict[str, Any]:
    """Class 5: suppress repeat same-owner mechanism-liveness RED comments and
    gate the single aging escalation (t_3d108e24).

    The stable identity is the deterministic key (board + mechanism key +
    owner card), NOT the caller-chosen classification. The governor embeds the
    exact key string AND an ``owner-packet-hash=<12-hex>`` fingerprint in every
    mechanism-liveness comment it writes; the next cycle's scan finds it.

    Decisions:
      COMMENT  - first sighting / TTL expired (write ONE "consume this owner
                 packet" comment embedding key + hash), or the owner packet is
                 byte-changed (different hash -> genuinely new evidence that
                 MUST still route; the cooldown applies only while the packet
                 is byte-unchanged).
      SUPPRESS - repeat same-owner-unchanged RED inside the TTL (no-op in the
                 governance log only), or the single aging escalation for this
                 packet was already emitted (do not emit a second one).
      AGING    - the same unchanged packet has been continuously RED for >=
                 aging_seconds and no aging escalation exists yet: the caller
                 emits exactly ONE aging escalation to jarvis-os-pm/os-reviewer
                 embedding the key + mechanism-liveness-aging-escalation marker
                 + hash.
    """
    key = build_mechanism_liveness_key(
        board=board, task_id=task_id, mechanism_key=mechanism_key, owner_card=owner_card
    )
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

    current_hash = owner_packet_hash(owner_packet)
    matches: list[dict[str, Any]] = []
    for row in rows:
        body = row["body"] or ""
        if key not in body:
            continue
        matches.append(
            {
                "id": int(row["id"]),
                "author": row["author"],
                "created_at": int(row["created_at"]),
                "packet_hash": _mechanism_liveness_packet_hash(body),
                "aging_marker": MECHANISM_LIVENESS_AGING_MARKER in body,
            }
        )

    def dedupe_key(extra: dict[str, Any] | None = None) -> dict[str, Any]:
        base = {
            "board": board,
            "task_id": task_id,
            "mechanism_liveness_key": key,
            "mechanism_key": mechanism_key,
            "owner_card": owner_card,
            "owner_packet_hash": current_hash,
            "classification": None,  # caller-chosen label is NOT identity
            "ttl_seconds": ttl_seconds,
            "aging_seconds": aging_seconds,
        }
        if extra:
            base.update(extra)
        return base

    if not matches:
        return {
            "decision": "COMMENT",
            "reason": "mechanism_liveness_new_or_ttl_expired",
            "dedupe_key": dedupe_key(),
            "comments_scanned": len(rows),
        }

    # A byte-changed owner packet MUST still route: the newest comment carries
    # a fingerprint that differs from the current packet's.
    newest = max(matches, key=lambda m: (m["created_at"], m["id"]))
    if newest["packet_hash"] is not None and newest["packet_hash"] != current_hash:
        return {
            "decision": "COMMENT",
            "reason": "mechanism_liveness_owner_packet_changed",
            "dedupe_key": dedupe_key({"matched_comment_packet_hash": newest["packet_hash"]}),
            "matched_comment": {
                "id": newest["id"],
                "author": newest["author"],
                "created_at": newest["created_at"],
                "excerpt": None,
            },
            "comments_scanned": len(rows),
        }

    # Same-packet evidence: comments whose fingerprint matches the current
    # packet (or legacy comments without a fingerprint, treated as same).
    same_packet = [m for m in matches if m["packet_hash"] is None or m["packet_hash"] == current_hash]
    if not same_packet:
        return {
            "decision": "COMMENT",
            "reason": "mechanism_liveness_owner_packet_changed",
            "dedupe_key": dedupe_key(),
            "comments_scanned": len(rows),
        }

    # The single aging escalation was already emitted for this unchanged
    # packet -> no-op in the governance log only, until the packet changes.
    if any(m["aging_marker"] for m in same_packet):
        marked = next(m for m in same_packet if m["aging_marker"])
        return {
            "decision": "SUPPRESS",
            "reason": "mechanism_liveness_aging_escalation_already_emitted",
            "dedupe_key": dedupe_key(),
            "matched_comment": {
                "id": marked["id"],
                "author": marked["author"],
                "created_at": marked["created_at"],
                "age_seconds": max(0, now - marked["created_at"]),
                "excerpt": None,
            },
            "comments_scanned": len(rows),
        }

    # Continuous-RED duration = age of the oldest same-packet comment inside
    # the TTL window (the first-sighting marker).
    oldest = min(same_packet, key=lambda m: (m["created_at"], m["id"]))
    age_seconds = max(0, now - oldest["created_at"])
    if age_seconds >= aging_seconds:
        return {
            "decision": "AGING",
            "reason": "mechanism_liveness_aging_escalation_due",
            "dedupe_key": dedupe_key(),
            "matched_comment": {
                "id": oldest["id"],
                "author": oldest["author"],
                "created_at": oldest["created_at"],
                "age_seconds": age_seconds,
                "excerpt": None,
            },
            "comments_scanned": len(rows),
        }

    return {
        "decision": "SUPPRESS",
        "reason": "mechanism_liveness_unchanged_within_ttl",
        "dedupe_key": dedupe_key(),
        "matched_comment": {
            "id": newest["id"],
            "author": newest["author"],
            "created_at": newest["created_at"],
            "age_seconds": max(0, now - newest["created_at"]),
            "excerpt": None,
        },
        "comments_scanned": len(rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only governor classification comment dedupe guard")
    parser.add_argument("--board", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--classification", default=None)
    parser.add_argument("--owner-packet", default=None)
    parser.add_argument(
        "--ttl-hours",
        type=float,
        default=None,
        help="Dedupe TTL in hours (default 2.0 for classes 1-4; 24.0 for --mechanism-liveness so the first-sighting comment stays inside the window until the aging escalation is due).",
    )
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
    # Class 4 governor-refresh inputs (t_2044aea3)
    parser.add_argument(
        "--governor-refresh",
        action="store_true",
        help="Use the governor-refresh dedupe class (suppress repeat same-owner A3/stale-direct refreshes on a blocked card when the durable residual is unchanged within the TTL).",
    )
    parser.add_argument(
        "--residual",
        default=None,
        help="Durable evidence residual for class 4 (e.g. `cron_stale_direct_rows=1`). Fingerprinted into the refresh key; NOT the classification and NOT a changing probe filename/timestamp. Required with --governor-refresh unless --refresh-key is supplied.",
    )
    parser.add_argument(
        "--refresh-key",
        default=None,
        help="Explicit deterministic governor-refresh key string (e.g. governor-refresh:v1:<board>:<task_id>:<owner_slug>:<residual_hash>). Takes precedence over --residual and the legacy classification path for class 4.",
    )
    # Class 5 mechanism-liveness inputs (t_3d108e24)
    parser.add_argument(
        "--mechanism-liveness",
        action="store_true",
        help="Use the mechanism-liveness dedupe class (suppress repeat same-owner RED comments on a mechanism-liveness owner card while the owner packet is byte-unchanged; return AGING after --aging-hours so the caller emits exactly ONE aging escalation to jarvis-os-pm/os-reviewer).",
    )
    parser.add_argument(
        "--mechanism-key",
        default=None,
        help="Stable mechanism key for class 5 (e.g. `breaker`). Part of the deterministic identity; NOT the caller-chosen classification. Required with --mechanism-liveness unless --ml-key is supplied.",
    )
    parser.add_argument(
        "--owner-card",
        default=None,
        help="Owner task id the mechanism-liveness RED is routed to (e.g. t_ba2b1bda). Part of the deterministic identity. Required with --mechanism-liveness unless --ml-key is supplied.",
    )
    parser.add_argument(
        "--ml-key",
        default=None,
        help="Explicit deterministic mechanism-liveness key string (e.g. mechanism-liveness:v1:<board>:<task_id>:mechanism:<key>:owner:<card>). Takes precedence over --mechanism-key/--owner-card.",
    )
    parser.add_argument(
        "--aging-hours",
        type=float,
        default=None,
        help="Class 5: hours continuously RED with an unchanged owner packet before exactly ONE aging escalation is due (default 4.0, MECHANISM_LIVENESS_AGING_HOURS).",
    )
    args = parser.parse_args(argv)

    # Per-class TTL default: classes 1-4 keep the historical 2.0h; class 5
    # needs the first-sighting comment to remain in the scan window until the
    # aging window has elapsed, so it defaults to 24h (>= aging + margin).
    ttl_hours = args.ttl_hours
    if ttl_hours is None:
        ttl_hours = MECHANISM_LIVENESS_TTL_HOURS if args.mechanism_liveness else 2.0
    ttl_seconds = int(ttl_hours * 3600)
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

    # Class 4 governor-refresh key. Precedence: class-3 > class-4 > class-2 >
    # class-1, so the finding-key path above wins when both are somehow present.
    refresh_key = args.refresh_key
    if args.governor_refresh and refresh_key is None:
        if not args.owner_packet or not args.residual:
            parser.error(
                "--governor-refresh requires --owner-packet and --residual "
                "(or pass --refresh-key with the full key string)"
            )
        refresh_key = build_refresh_key(
            board=args.board,
            task_id=args.task_id,
            owner_packet=args.owner_packet,
            residual=args.residual,
        )

    # Safety carve-out: never suppress credential/approval-critical escalations,
    # even when a deterministic key match exists. This is enforced BEFORE any
    # scan so a critical escalation always returns COMMENT.
    #
    # Class-4 carve-out semantics (design finding, t_330f2377): the existing
    # CRITICAL_MARKER_RE matches bare `deploy`/`production` tokens in the
    # CALLER-CHOSEN classification, which would force routine A3-deploy-gate
    # refreshes to COMMENT forever. For the class-4 path the carve-out is
    # evaluated on the explicit --critical-escalation flag AND the durable
    # residual content only — NOT on incidental classification tokens. The
    # credential/approval-critical suppression for classes 1-3 is unchanged.
    try:
        if args.critical_escalation:
            result = {
                "decision": "COMMENT",
                "reason": "critical_escalation_never_suppressed",
                "dedupe_key": {
                    "board": args.board,
                    "task_id": args.task_id,
                    "classification": args.classification,
                    "owner_packet": args.owner_packet,
                    "finding_key": finding_key,
                    "refresh_key": refresh_key,
                    "ttl_seconds": ttl_seconds,
                },
                "comments_scanned": 0,
            }
        elif args.governor_refresh:
            # Class-4: carve-out is decided on the residual / flag, not the
            # incidental classification text.
            residual_critical = bool(CRITICAL_MARKER_RE.search(args.residual or ""))
            if residual_critical:
                result = {
                    "decision": "COMMENT",
                    "reason": "critical_escalation_never_suppressed",
                    "dedupe_key": {
                        "board": args.board,
                        "task_id": args.task_id,
                        "classification": args.classification,
                        "owner_packet": args.owner_packet,
                        "finding_key": finding_key,
                        "refresh_key": refresh_key,
                        "ttl_seconds": ttl_seconds,
                    },
                    "comments_scanned": 0,
                }
            else:
                result = find_refresh_duplicate(
                    board=args.board,
                    task_id=args.task_id,
                    refresh_key=refresh_key,
                    ttl_seconds=ttl_seconds,
                    now=now,
                )
        elif args.mechanism_liveness:
            # Class 5: stable identity is board + mechanism key + owner card.
            # The critical carve-out is evaluated on the durable identity
            # (mechanism key + owner packet), consistent with classes 1-3: a
            # credential/approval-critical mechanism is never suppressed.
            if not args.ml_key:
                if not args.mechanism_key or not args.owner_card:
                    parser.error(
                        "--mechanism-liveness requires --mechanism-key and --owner-card "
                        "(or pass --ml-key with the full key string)"
                    )
                ml_key = build_mechanism_liveness_key(
                    board=args.board,
                    task_id=args.task_id,
                    mechanism_key=args.mechanism_key,
                    owner_card=args.owner_card,
                )
            else:
                ml_key = args.ml_key
            if not args.owner_packet:
                parser.error("--mechanism-liveness requires --owner-packet (durable RED evidence, fingerprinted for byte-change detection)")
            aging_hours = args.aging_hours if args.aging_hours is not None else MECHANISM_LIVENESS_AGING_HOURS
            aging_seconds = int(aging_hours * 3600)
            if aging_seconds <= 0:
                parser.error("--aging-hours must be positive")
            if is_critical_escalation(
                classification=args.classification or "",
                owner_packet=args.owner_packet or "",
                finding_key=ml_key,
            ):
                result = {
                    "decision": "COMMENT",
                    "reason": "critical_escalation_never_suppressed",
                    "dedupe_key": {
                        "board": args.board,
                        "task_id": args.task_id,
                        "classification": args.classification,
                        "owner_packet": args.owner_packet,
                        "mechanism_liveness_key": ml_key,
                        "aging_seconds": aging_seconds,
                        "ttl_seconds": ttl_seconds,
                    },
                    "comments_scanned": 0,
                }
            else:
                result = find_mechanism_liveness_duplicate(
                    board=args.board,
                    task_id=args.task_id,
                    mechanism_key=args.mechanism_key or "",
                    owner_card=args.owner_card or "",
                    owner_packet=args.owner_packet,
                    ttl_seconds=ttl_seconds,
                    aging_seconds=aging_seconds,
                    now=now,
                )
        elif is_critical_escalation(
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
                    "refresh_key": refresh_key,
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
                parser.error("legacy path requires --classification and --owner-packet (or use --finding-key / --target-comment-id / --proposal-aging-guard / --governor-refresh / --mechanism-liveness)")
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
        if result["decision"] in ("SUPPRESS", "AGING") and result.get("matched_comment"):
            match = result["matched_comment"]
            print(
                f"matched_comment id={match['id']} author={match['author']} "
                f"age_seconds={match.get('age_seconds')} excerpt={match.get('excerpt')!r}"
            )
        print(f"dedupe_key={json.dumps(result['dedupe_key'], sort_keys=True)}")
        print(f"comments_scanned={result['comments_scanned']}")
    return 0 if result["decision"] in {"SUPPRESS", "COMMENT", "AGING"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
