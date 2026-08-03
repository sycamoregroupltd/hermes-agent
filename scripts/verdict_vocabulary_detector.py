#!/usr/bin/env python3
# CANONICAL SOURCE — read-only producer-side DETECTOR for out-of-contract
# REVIEW_VERDICT values. Do not edit profile-local copies.
"""Producer-side DETECTOR for out-of-contract REVIEW_VERDICT values.

Read-only. Does NOT mutate any kanban card. Surfaces malformed
REVIEW_VERDICT comments (e.g. APPROVE_WITH_NOTES, APPROVED_WITH_NOTES) that the
verdict-router (verdict_router.py) fails closed on, which can leave a card
invisible to its consumer (the review "black hole" — see runbook L92-94).

This module is the read-only counterpart to verdict_router.py: it scans the
same BOARD_ALLOWLIST boards and reuses the same open-db-read-only pattern, but
it only REPORTS findings. It never auto-completes, never transitions status,
and never creates a new cron schedule.

Wiring: blocked_task_notifier.py imports detect_malformed_verdicts() and emits
findings through its existing discord #critical-alerts escalation path (no new
schedule).

Hard boundaries (inherited from proposal t_cc6e2ca2 / task t_1d6ed4c0):
- NO loosening of the verdict-router parser/contract.
- NO auto-completion, NO status transition, NO new cron.
- Not A3: no credentials, spend, guardrail weakening, or prod deploy.
- Read-only: opens the kanban DBs read-only.

Detection rules (from task t_1d6ed4c0):
1. Scan task_comments across the BOARD_ALLOWLIST boards.
2. For each comment matching VERDICT_RE, capture the value. Flag it iff the
   uppercase value is NOT in the contract set {APPROVED, APPROVE,
   CHANGES_REQUESTED, REJECT}. REJECT is treated as valid so the detector does
   NOT false-positive on the router's own REJECT handling.
3. Only flag when the card's status is in {blocked, review} — AND extend the
   scope to {running} cards that have ONLY a malformed verdict and no valid
   verdict at all (closes the stuck-running branch).
4. Only flag verdicts whose age > 1h (avoid transient in-flight reviews) and
   where no later valid verdict exists on the same card (idempotency / no
   re-flag).
5. Emit findings via the existing notifier escalation path — never mutate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(os.environ.get("VERDICT_DETECTOR_ROOT", "/home/frank/.hermes"))
BOARDS_DIR = Path(os.environ.get("VERDICT_DETECTOR_BOARDS_DIR", str(ROOT / "kanban" / "boards")))
DEFAULT_DB = Path(os.environ.get("VERDICT_DETECTOR_DEFAULT_DB", str(ROOT / "kanban.db")))
STATE_DIR = Path(os.environ.get("VERDICT_DETECTOR_STATE_DIR", str(ROOT / "cron" / "state")))
DEFAULT_STATE = STATE_DIR / "verdict-vocabulary-detector.state.json"

# Reuse the exact verdict regex from verdict_router.py:40 (kept in lockstep).
VERDICT_RE = re.compile(r"\bREVIEW_VERDICT\s*[:=]\s*([A-Z0-9_]+)", re.I)

# Contract values the router accepts (verdict_router.py:43) PLUS the router's
# own REJECT handling (verdict_router.py:704 — parse_verdict normalizes
# REJECT/REJECTED to "REJECT" and treats it as valid). The detector must NOT
# false-positive on REJECT/APPROVED/APPROVE/CHANGES_REQUESTED.
VALID_VERDICTS = frozenset({"APPROVED", "APPROVE", "CHANGES_REQUESTED", "REJECT"})

# Statuses in scope for flagging.
STATUS_IN_SCOPE = frozenset({"blocked", "review", "running"})

# Minimum age before a malformed verdict is flagged (avoid transient in-flight
# reviews during the 1h router window).
MIN_AGE_SECONDS = 3600

# Escalation tiers (mirrors blocked_task_notifier.ESCALATION_TIERS).
ESCALATION_TIERS = ((0, "NEW"), (24 * 3600, "ESCALATION-24H"), (72 * 3600, "ESCALATION-72H"))


# --- Import the canonical router's helpers when available; otherwise fall back
#     to self-contained copies so this module never hard-fails to import. The
#     router module itself imports second_brain_writer at top level, which may
#     be absent in some environments; we must not let that break the detector.
try:  # pragma: no cover - import path depends on environment
    import verdict_router as vr  # type: ignore

    _affirmative_matches = vr.verdict_declarations  # negation-aware (C4 fix)
    BOARD_ALLOWLIST = vr.BOARD_ALLOWLIST
    EXCLUDED_BOARDS = vr.EXCLUDED_BOARDS
    _HAVE_ROUTER = True
except Exception:  # pragma: no cover
    _affirmative_matches = lambda text: list(VERDICT_RE.finditer(text or ""))
    BOARD_ALLOWLIST = {
        "ai-restaurant",
        "jarvis-os",
        "legacy-yss",
        "quicknote",
        "supero",
        "sycode-ai",
        "sycode-trading",
        "upero",
        "yorkstone",
        "yorkstone-supplies",
    }
    EXCLUDED_BOARDS = {"orchestrator-sync"}
    _HAVE_ROUTER = False


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def open_db_ro(db: Path) -> sqlite3.Connection:
    """Open a board DB read-only (reuses verdict_router.open_db_ro pattern).

    Uses mode=ro (NOT immutable=1): immutable=1 ignores the -wal file and reads a
    potentially STALE snapshot of a live board. mode=ro reads the live WAL
    checkpoint. We do not take a write lock, so a brief reader is safe and the
    router/detector is read-mostly.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _to_seconds(value: int | str | float | None) -> int:
    """Coerce a SQLite created_at value to epoch seconds.

    Accepts int/str epoch seconds or milliseconds; non-numeric/missing => 0.
    """
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    if n > 10_000_000_000:  # plausibly milliseconds
        n = n // 1000
    return n


def boards() -> list[tuple[str, Path]]:
    """Enumerate in-scope boards. Mirrors verdict_router.boards()."""
    found: list[tuple[str, Path]] = []
    if DEFAULT_DB.exists():
        found.append(("default", DEFAULT_DB))
    if BOARDS_DIR.exists():
        for db in sorted(BOARDS_DIR.glob("*/kanban.db")):
            slug = db.parent.name
            if slug.startswith("_"):
                continue
            if slug.startswith(".bak") or slug in EXCLUDED_BOARDS:
                continue
            if slug not in BOARD_ALLOWLIST:
                continue
            found.append((slug, db))
    return found


@dataclass(frozen=True)
class Finding:
    board: str
    task_id: str
    comment_id: int
    comment_author: str
    verdict_value: str
    comment_created_at: int  # epoch seconds
    age_seconds: int
    task_status: str
    task_title: str

    @property
    def idempotency_key(self) -> str:
        return (
            f"verdict-vocabulary-detector:v1:{self.board}:{self.task_id}"
            f":comment:{self.comment_id}:value:{self.verdict_value}"
        )


def normalize_verdict(raw: str) -> str:
    """Normalize a captured verdict token to its contract form.

    Mirrors verdict_router.parse_verdict (lines 702-705): APPROVE -> APPROVED,
    REJECT/REJECTED -> REJECT. Used so the detector does not false-positive on
    the router's own accepted spellings. Anything else is returned unchanged
    (and is therefore treated as out-of-contract if not in VALID_VERDICTS).
    """
    r = raw.strip().upper()
    if r == "APPROVE":
        return "APPROVED"
    if r in {"REJECT", "REJECTED"}:
        return "REJECT"
    return r


def verdict_instances(con: sqlite3.Connection, task_id: str) -> list[tuple[int, str, int, str]]:
    """Return all affirmative REVIEW_VERDICT declarations on a card.

    Each entry is (created_at_seconds, value_upper, comment_id, author). Ordered
    by created_at then comment_id. Uses the router's negation-aware affirmative
    matcher when available (so "No REVIEW_VERDICT=FOO" prose is excluded).
    """
    if not table_exists(con, "task_comments"):
        return []
    rows = con.execute(
        "SELECT id, author, body, created_at FROM task_comments WHERE task_id=? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    out: list[tuple[int, str, int, str]] = []
    for r in rows:
        body = r["body"] or ""
        created = _to_seconds(r["created_at"])
        cid = int(r["id"]) if r["id"] is not None else 0
        author = r["author"] or ""
        for m in _affirmative_matches(body):
            value = normalize_verdict(m.group(1))
            if not value:
                continue
            out.append((created, value, cid, author))
    return out


def detect_malformed_verdicts(
    now: int | None = None,
    boards_override: Iterable[tuple[str, Path]] | None = None,
) -> list[Finding]:
    """Scan in-scope boards and return findings for out-of-contract verdicts.

    Pure read-only. Never mutates a card. Mirrors the detection rules in
    task t_1d6ed4c0 (see module docstring).
    """
    now = now if now is not None else int(time.time())
    findings: list[Finding] = []
    for slug, db in (boards_override if boards_override is not None else boards()):
        con = open_db_ro(db)
        try:
            if not table_exists(con, "tasks") or not table_exists(con, "task_comments"):
                continue
            rows = con.execute(
                "SELECT id, title, status FROM tasks WHERE status IN ('blocked','review','running')"
            ).fetchall()
            for row in rows:
                task_id = row["id"]
                status = row["status"]
                instances = verdict_instances(con, task_id)
                if not instances:
                    continue

                valid_ts = [ts for ts, val, _, _ in instances if val in VALID_VERDICTS]
                has_valid = bool(valid_ts)
                latest_valid_ts = max(valid_ts) if valid_ts else None
                malformed = [
                    (ts, val, cid, author)
                    for ts, val, cid, author in instances
                    if val not in VALID_VERDICTS
                ]
                if not malformed:
                    continue

                # Status gate.
                if status in ("blocked", "review"):
                    in_scope = True
                elif status == "running":
                    # Running cards only when they have ONLY a malformed verdict
                    # and no valid verdict at all (no later valid verdict).
                    in_scope = not has_valid
                else:
                    in_scope = False
                if not in_scope:
                    continue

                for ts, val, cid, author in malformed:
                    age = now - ts
                    if age <= MIN_AGE_SECONDS:
                        continue  # transient in-flight review
                    # No later valid verdict resolves it (idempotency / no re-flag).
                    if latest_valid_ts is not None and ts < latest_valid_ts:
                        continue
                    findings.append(
                        Finding(
                            board=slug,
                            task_id=task_id,
                            comment_id=cid,
                            comment_author=author,
                            verdict_value=val,
                            comment_created_at=ts,
                            age_seconds=age,
                            task_status=status,
                            task_title=(row["title"] or "")[:120],
                        )
                    )
        finally:
            con.close()
    return findings


def format_message(findings: list[Finding], *, now: int | None = None, header: str | None = None) -> str:
    now = now if now is not None else int(time.time())
    prefix = header or "🚨 Out-of-contract REVIEW_VERDICT detected (review black-hole risk):"
    lines = [prefix]
    for f in findings:
        age_h = f.age_seconds / 3600
        lines.append(f"  • [{f.board}] {f.task_id} — {f.task_title}")
        lines.append(
            f"    verdict={f.verdict_value} comment_id={f.comment_id} "
            f"author={f.comment_author} status={f.task_status} age={age_h:.1f}h"
        )
    lines.append(
        "These verdicts are not in the router contract {APPROVED,APPROVE,CHANGES_REQUESTED,REJECT} "
        "and will fail closed to needs_pm — the card is invisible to its consumer."
    )
    lines.append("Action: re-issue the review with a contract verdict (or fix the producer). Read-only detector: no card was mutated.")
    lines.append("Governance: proposal t_cc6e2ca2 / task t_1d6ed4c0.")
    return "\n".join(lines)


def _alert_tier(age_seconds: float, delivered_tiers: list[str]) -> str | None:
    due = None
    for threshold, name in ESCALATION_TIERS:
        if age_seconds >= threshold:
            due = name
    if due and due not in delivered_tiers:
        return due
    return None


def _load_state(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def run_with_alerting(
    send: Callable[[str], tuple[bool, str]],
    *,
    state_path: str | None = None,
    now: int | None = None,
) -> tuple[int, list[str]]:
    """Detect malformed verdicts and emit via ``send(message) -> (ok, detail)``.

    Dedupes across runs using a JSON state file. Returns (findings_count,
    failures). Does NOT mutate any card. ``send`` is the notifier's send_alert.
    """
    now = now if now is not None else int(time.time())
    resolved_state = Path(state_path or os.environ.get("VERDICT_DETECTOR_STATE", str(DEFAULT_STATE)))
    findings = detect_malformed_verdicts(now=now)
    state = _load_state(resolved_state)
    current = {f.idempotency_key: f for f in findings}

    # Seed first_seen for new keys; refresh volatile fields for continuing ones.
    for key, f in current.items():
        if key not in state:
            state[key] = {
                "first_seen_epoch": now,
                "delivered_tiers": [],
                "board": f.board,
                "task_id": f.task_id,
                "verdict_value": f.verdict_value,
                "comment_id": f.comment_id,
            }
        else:
            state[key].update(
                {
                    "board": f.board,
                    "task_id": f.task_id,
                    "verdict_value": f.verdict_value,
                    "comment_id": f.comment_id,
                }
            )
    # Prune keys no longer present (card fixed / verdict replaced).
    for key in list(state.keys()):
        if key not in current:
            del state[key]

    due: dict[str, tuple[Finding, str]] = {}
    for key, f in current.items():
        entry = state[key]
        tiers = entry.setdefault("delivered_tiers", [])
        tier = _alert_tier(now - int(entry.get("first_seen_epoch", now)), tiers)
        if tier:
            due[key] = (f, tier)

    failures: list[str] = []
    for key, (f, tier) in due.items():
        header = (
            "🚨 Out-of-contract REVIEW_VERDICT detected (review black-hole risk):"
            if tier == "NEW"
            else f"🚨 {tier}: Out-of-contract REVIEW_VERDICT still unacked (review black-hole risk):"
        )
        msg = format_message([f], now=now, header=header)
        ok, detail = send(msg)
        if ok:
            state[key]["delivered_tiers"].append(tier)
            state[key]["last_delivery_success_epoch"] = now
        else:
            failures.append(f"{key}:{tier}:{detail}")
    _write_state(resolved_state, state)
    return len(findings), failures


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only detector for out-of-contract REVIEW_VERDICT values.")
    ap.add_argument("--state", default=None, help="State file for send dedup (default cron/state path).")
    ap.add_argument("--alert", action="store_true", help="Emit via blocked_task_notifier.send_alert when findings exist.")
    ap.add_argument("--now", type=int, default=None, help="Override 'now' epoch (for tests/replays).")
    args = ap.parse_args()

    findings = detect_malformed_verdicts(now=args.now)
    if not findings:
        print("verdict-vocabulary-detector: 0 findings")
        return 0
    print(format_message(findings, now=args.now))
    if args.alert:
        try:
            from blocked_task_notifier import send_alert  # type: ignore
        except Exception as exc:  # pragma: no cover
            print(f"verdict-vocabulary-detector: alert skipped (send_alert unavailable: {exc!r})")
            return len(findings)
        count, failures = run_with_alerting(send_alert, state_path=args.state, now=args.now)
        print(f"verdict-vocabulary-detector: alerted {count} finding(s); failures={failures}")
    return len(findings)


if __name__ == "__main__":
    raise SystemExit(main())
