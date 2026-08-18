#!/usr/bin/env python3
"""needs_input SLA probe: auto-route stale needs_input blocks to the Frank A3 gate.

Builds on t_f3a11584 (needs-input-decision-stub). Where that stub validates the
*content* of a needs_input blocker, this probe enforces the *age* SLA: any card
in status='blocked' with block_kind='needs_input' whose
COALESCE(started_at, created_at) is older than the SLA (default 24h) is past
SLA and must not rot silently.

Behaviour:
  DRY-RUN (default): opens every board DB read-only (mode=ro), lists the exact
  breached-card set per board, and prints the consolidated 'needs_input SLA
  breach' card payload it WOULD emit. Zero writes. This is the mode the
  os-reviewer signs off on (acceptance criterion 3 of t_27c22567).

  --emit: creates ONE consolidated card per run (not one per breached card) on
  the configured governance board, assignee=elon, with block_kind='needs_input',
  carrying the full breached-card manifest so it lands in the Frank A3 decision
  batch like t_d3ed7765. Emission is idempotent per day via a kanban_create
  idempotency key (needs-input-sla-breach-<date>).

  Emission guard (hardened t_3824e584): before minting, --emit suppresses
  creation while ANY non-archived 'SLA OWNER:' decomposition card across the
  scanned boards is ready/running/todo (a per-board owner is actively
  decomposing the backlog), OR while a non-closed 'NEEDS_INPUT SLA BREACH' card
  still exists on the target board (reuse it instead of minting a daily
  duplicate). This breaks the 2026-08-04->2026-08-12 duplicate-report loop
  (t_cec31e25/t_44a5f775). Dry-run still prints the full would-emit payload for
  review, plus a guard-verdict note.

  --heartbeat: writes a liveness heartbeat file so that a separate monitor can
  detect probe absence. The probe MUST run with --heartbeat or the cron wrapper
  must touch the heartbeat file after a successful run.

Deployment target (post-review): a profile cron job (e.g. os-configurator or
dedicated watcher) running:
  needs-input-sla-probe.py --emit --heartbeat
every 6h.

GATES: read-only probe (dry-run zero writes; --emit only writes via hermes
kanban CLI, no direct DB writes); no credentials/secrets; no live trading;
no prod deploy; no schema writes. Verified external to this script.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field

# ─── Default board registry ────────────────────────────────────────────

DEFAULT_BOARDS: list[tuple[str, str]] = [
    ("/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db", "jarvis-os"),
    ("/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db", "sycode-trading"),
    ("/home/frank/.hermes/kanban/boards/upero/kanban.db", "upero"),
]

DEFAULT_SLA_HOURS = 24
# Consolidated card lands on the jarvis-os governance board, assigned to
# elon (the Frank A3 decision batch owner, matching reference Frank-batch
# cards t_d3ed7765 / t_47e7d975 / t_84545f19) so it enters the admin routing
# pipeline.
DEFAULT_TARGET_BOARD = "jarvis-os"
DEFAULT_TARGET_ASSIGNEE = "elon"

# The exact SLA predicate, shared by the sqlite query and the docs. Kept as a
# single source so the reviewer's manual dry-run matches the probe byte-for-byte.
SLA_WHERE = (
    "status = 'blocked'\n"
    "  AND block_kind = 'needs_input'\n"
    "  AND COALESCE(started_at, created_at) < strftime('%s','now') - :sla_seconds"
)


@dataclass
class BreachedCard:
    board: str
    task_id: str
    title: str
    assignee: str | None
    age_hours: int
    block_kind: str

    def to_dict(self) -> dict:
        return {
            "board": self.board,
            "task_id": self.task_id,
            "title": self.title,
            "assignee": self.assignee,
            "age_hours": self.age_hours,
            "block_kind": self.block_kind,
        }


@dataclass
class BoardScan:
    board: str
    db_path: str
    breached: list[BreachedCard] = field(default_factory=list)
    error: str | None = None


# ─── Probe core ────────────────────────────────────────────────────────

def scan_board(db_path: str, slug: str, sla_hours: int) -> BoardScan:
    """Read-only scan of one board for needs_input cards past the SLA.

    Opens the DB with mode-ro and a busy_timeout; never writes.
    """
    scan = BoardScan(board=slug, db_path=db_path)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=5000")
    except Exception as e:
        scan.error = f"cannot open db read-only: {e}"
        return scan

    try:
        cursor = con.execute(
            f"""
            SELECT id, title, assignee,
                   CAST((strftime('%s','now') - COALESCE(started_at, created_at)) / 3600 AS INT) AS age_hours,
                   block_kind
            FROM tasks
            WHERE {SLA_WHERE}
            ORDER BY age_hours DESC
            """,
            {"sla_seconds": sla_hours * 3600},
        )
        for tid, title, assignee, age_hours, block_kind in cursor.fetchall():
            scan.breached.append(
                BreachedCard(
                    board=slug,
                    task_id=tid,
                    title=title,
                    assignee=assignee,
                    age_hours=age_hours,
                    block_kind=block_kind,
                )
            )
    except Exception as e:
        scan.error = f"query failed: {e}"
    finally:
        con.close()
    return scan


def scan_all(boards: list[tuple[str, str]], sla_hours: int) -> list[BoardScan]:
    return [scan_board(path, slug, sla_hours) for path, slug in boards]


# ─── Emission guard (t_3824e584) ───────────────────────────────────────

# Owner/decomposition-card marker convention: any non-archived card whose title
# starts with this prefix is a per-board SLA decomposition owner that is already
# working the backlog (e.g. sycode-trading/t_41efc723, sycode-ai/t_a6ed5a5e,
# upero/t_759633ef). While one is active, minting a fresh consolidated report is
# redundant — the duplicate loop must not recreate cards before owners finish.
SLA_OWNER_TITLE_PREFIX = "SLA OWNER:"
# Existing consolidated breach-card marker on the target board. While a
# non-closed one exists, reuse it instead of minting a per-day duplicate.
BREACH_TITLE_PREFIX = "NEEDS_INPUT SLA BREACH"
# Statuses that mean an owner/decomposition card is actively being worked and
# should suppress a fresh mint.
OWNER_ACTIVE_STATUSES = ("ready", "running", "todo")
# Statuses that count as "closed" for a prior breach card (safe to reuse/skip).
BREACH_CLOSED_STATUSES = ("archived", "done", "cancelled")


def scan_emit_suppressors(
    boards: list[tuple[str, str]], target_board: str, target_prefix: str | None = None,
) -> list[str]:
    """Read-only scan for cards that should suppress --emit card creation.

    Returns a list of human-readable suppression reasons; an empty list means
    emission may proceed. Three independent conditions:

    1. ANY non-archived 'SLA OWNER:' decomposition card across the scanned
       boards is ready/running/todo — an owner is already decomposing the
       backlog, so a new consolidated report is redundant. Applies to both the
       daily breach and the weekly digest.
    2. A non-closed card matching ``target_prefix`` (default
       'NEEDS_INPUT SLA BREACH') already exists on the target board — reuse it
       instead of minting a duplicate. For the weekly digest the prefix is
       'FRANK A3 WEEKLY DIGEST', so an open digest for the week is reused.

    Read-only: opens every board DB mode=ro; never writes.
    """
    prefix = target_prefix or BREACH_TITLE_PREFIX
    suppressors: list[str] = []
    for path, slug in boards:
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.execute("PRAGMA busy_timeout=5000")
            try:
                # 1. active SLA-owner decomposition cards on this board
                owner_rows = con.execute(
                    "SELECT id, title, status FROM tasks "
                    f"WHERE title LIKE :prefix "
                    f"AND status IN ({','.join('?' * len(OWNER_ACTIVE_STATUSES))}) "
                    "ORDER BY created_at ASC",
                    (SLA_OWNER_TITLE_PREFIX + "%", *OWNER_ACTIVE_STATUSES),
                ).fetchall()
                for tid, title, status in owner_rows:
                    suppressors.append(
                        f"open SLA-owner card {slug}/{tid} ({status}): {title[:60]}"
                    )
                # 2. existing open target card on the target board
                if slug == target_board:
                    existing = con.execute(
                        "SELECT id, title, status FROM tasks "
                        "WHERE title LIKE :prefix "
                        f"AND status NOT IN ({','.join('?' * len(BREACH_CLOSED_STATUSES))}) "
                        "ORDER BY created_at DESC LIMIT 1",
                        (prefix + "%", *BREACH_CLOSED_STATUSES),
                    ).fetchone()
                    if existing:
                        suppressors.append(
                            f"existing open target card {existing[0]} ({existing[2]}): "
                            f"{existing[1][:60]} — reuse instead of minting a new one"
                        )
            finally:
                con.close()
        except Exception as e:
            suppressors.append(f"suppressor scan failed on {slug}: {e}")
    return suppressors


def find_prior_week_count(target_board: str, week_label: str) -> int | None:
    """Best-effort read of the prior ISO week's digest breached_count.

    Returns None when no prior digest card is found or the board cannot be read
    (the weekly card then simply omits the delta line). Matches the previous
    ISO week by title prefix 'FRANK A3 WEEKLY DIGEST (<prev-iso>)'. Read-only.
    """
    # Derive the previous ISO week label from the current one.
    try:
        parts = week_label.split("-W")
        year = int(parts[0])
        week = int(parts[1])
    except (ValueError, IndexError):
        return None
    import datetime as _dt
    d = _dt.date.fromisocalendar(year, week, 1) - _dt.timedelta(days=7)
    prev_label = iso_week_label(d)
    db_path = None
    for path, slug in DEFAULT_BOARDS:
        if slug == target_board:
            db_path = path
            break
    if not db_path:
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            row = con.execute(
                "SELECT metadata FROM tasks "
                "WHERE title LIKE :prefix ORDER BY created_at DESC LIMIT 1",
                (f"{DIGEST_TITLE_PREFIX} ({prev_label})%",),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None
        meta = json.loads(row[0])
        return meta.get("breached_count")
    except Exception:
        return None



# ─── Weekly Frank A3 digest (t_7c598bd9) ───────────────────────────────

# Weekly digest card marker on the target board. While a non-closed one exists
# for the current ISO week, reuse it instead of minting a duplicate (the weekly
# analog of BREACH_TITLE_PREFIX).
DIGEST_TITLE_PREFIX = "FRANK A3 WEEKLY DIGEST"
# Closed statuses for a prior weekly digest card (safe to reuse/skip).
DIGEST_CLOSED_STATUSES = BREACH_CLOSED_STATUSES

# Decision-type taxonomy (verified against the live backlog; strategy note
# 2026-08-18-frank-a3-weekly-digest-strategy-t_b50df0ef). Each section maps to
# keyword/regex hints matched against the card title (case-insensitive). The
# first section whose any-hint matches wins; unmatched cards fall to
# OTHER / OWNER-REQUIRED.
DIGEST_SECTIONS: list[tuple[str, list[str]]] = [
    ("INFRASTRUCTURE", [
        r"pgbouncer", r"\bpostgres\b", r"\bdns\b", r"\bvault\b", r"network",
        r"deploy", r"mlflow", r"backup", r"proxy", r"gateway", r"\bdocker\b",
        r"\bserver\b", r"restart", r"firewall", r"\btls\b", r"\bcert\b",
        r"\bcron\b", r"jobs\.json", r"branch[- ]protection", r"ci gate",
    ]),
    ("AI/NVIDIA", [
        r"nvidia", r"\beula\b", r"isaac", r"blender", r"\bdmg\b", r"\bgpu\b",
        r"\bcuda\b", r"driver", r"model pin", r"provider pin", r"image patch",
        r"mlflow image", r"inference", r"local-inference",
    ]),
    ("DB TERMINATION & DATA OPS", [
        r"termination", r"lock tangle", r"\bvacuum\b", r"retention",
        r"backfill", r"\bfk\b", r"append[- ]only", r"pg_hba", r"\bdb\b",
        r"\bsql\b", r"migration", r"data[- ]repair", r"data[- ]repair",
        r"\btable\b", r"column", r"\bindex\b", r"strategy_id", r"signal_journey",
        r"managed_positions", r"circuitbreaker", r"dailyPnL", r"ml training",
    ]),
    ("RESEARCH GATES", [
        r"calibration", r"conviction", r"edge registry", r"promotion",
        r"lineage", r"tournament", r"\brisk\b", r"research", r"backtest",
        r"\bsignal\b", r"classifier", r"\beval\b", r"\bedge\b", r"referee",
        r"verdict", r"confidence", r"self[- ]tuner",
    ]),
    ("CREDENTIALS / SECURITY", [
        r"key rotation", r"secret[- ]store", r"webhook provisioning",
        r"api key", r"secret", r"credential", r"\btoken\b", r"password",
        r"binance", r"groq", r"\bauth\b", r"\bssh\b", r"cleartext",
        r"whatsapp", r"bridge",
    ]),
    ("SPEND", [
        r"recurring cost", r"paid api", r"subscription", r"billing",
        r"\bcost\b", r"\bspend\b", r"payment", r"\bcredit\b",
    ]),
]
# Fallback section for items with no clean decision type.
OTHER_SECTION = "OTHER / OWNER-REQUIRED"

# Classification priority: sections are matched in this order so that a card
# mentioning a strong security/credential token wins over a generic
# infra keyword (e.g. "rotate Groq key ... 219 backups" must land in
# CREDENTIALS, not INFRASTRUCTURE via "backup"). Display order stays
# DIGEST_SECTIONS; this list only controls which section wins on a tie.
CLASSIFY_PRIORITY = [
    "CREDENTIALS / SECURITY",
    "SPEND",
    "AI/NVIDIA",
    "DB TERMINATION & DATA OPS",
    "RESEARCH GATES",
    "INFRASTRUCTURE",
]


def classify_item(title: str) -> str:
    """Map a card title to one decision-type digest section (case-insensitive).

    Matches the title against each section's hints in CLASSIFY_PRIORITY order;
    the first section whose any-hint matches wins. Unmatched cards fall to
    OTHER / OWNER-REQUIRED.
    """
    import re
    hints_by_section = dict(DIGEST_SECTIONS)
    low = title.lower()
    for section in CLASSIFY_PRIORITY:
        for hint in hints_by_section[section]:
            if re.search(hint, low):
                return section
    return OTHER_SECTION


def build_weekly_digest_card(
    scans: list[BoardScan],
    sla_hours: int,
    iso_week_label: str,   # e.g. "2026-W33"
    prior_delta: int | None = None,
    board: str = DEFAULT_TARGET_BOARD,
) -> dict | None:
    """Build the single weekly Frank A3 digest card payload.

    Returns None when there is nothing to report (no breaches, no errors),
    the probe's quiet-green state.

    Idempotency key is ``frank-a3-weekly-digest-<ISO-week>``: ONE card per ISO
    week, reused across runs within the week — never per day. Title carries the
    ISO week, the item count, and explicit APPROVE/DEFER/CLOSE triage verbs.
    Items are grouped by decision-type section (INFRASTRUCTURE, AI/NVIDIA,
    DB TERMINATION & DATA OPS, RESEARCH GATES, CREDENTIALS / SECURITY, SPEND,
    OTHER / OWNER-REQUIRED) with a per-item ``[board/task_id] age | assignee |
    title`` line plus a decision stub, and a machine-scannable triage legend +
    guardrails footer (unchanged: routing artifact only, no auto-apply, every
    card keeps its own A3 gating, Frank veto).
    """
    all_breached = [c for s in scans for c in s.breached]
    errors = [(s.board, s.error) for s in scans if s.error]
    if not all_breached and not errors:
        return None

    per_board_counts = {s.board: len(s.breached) for s in scans}
    oldest_h = max((c.age_hours for c in all_breached), default=0)

    # Group by decision-type section, preserving age-desc order within each.
    grouped: dict[str, list[BreachedCard]] = {s: [] for s, _ in DIGEST_SECTIONS}
    grouped.setdefault(OTHER_SECTION, [])
    for c in sorted(all_breached, key=lambda c: -c.age_hours):
        grouped[classify_item(c.title)].append(c)

    lines = [
        "FRANK A3 WEEKLY DIGEST — grouped triage batch for the Frank A3 queue.",
        "",
        f"ISO week: {iso_week_label}",
        f"Total needs_input items past SLA (>={sla_hours}h): {len(all_breached)} "
        f"(across {sum(1 for n in per_board_counts.values() if n)} boards).",
    ]
    if prior_delta is not None:
        delta = len(all_breached) - prior_delta
        sign = "+" if delta >= 0 else ""
        lines.append(f"Delta vs prior week: {sign}{delta}")
    lines.append(f"Oldest item: {oldest_h}h")
    lines.append(f"Per-board: " + ", ".join(f"{b}={n}" for b, n in per_board_counts.items()))
    lines.append("")
    lines.append(
        "DECIDE per item below. Triage verbs (machine-scannable): "
        "APPROVE = unblock and execute per the item's own packet/guardrails; "
        "DEFER = set a new explicit SLA/review date (stays blocked, no "
        "re-escalation); CLOSE = supersede/archive with cited successor or reason."
    )
    lines.append("")

    for section, _ in DIGEST_SECTIONS + [(OTHER_SECTION, [])]:
        items = grouped.get(section, [])
        lines.append(f"## {section} ({len(items)})")
        if not items:
            lines.append("  (none)")
            continue
        for c in items:
            lines.append(
                f"- [{c.board}/{c.task_id}] {c.age_hours}h | {c.assignee or 'unassigned'} "
                f"| {c.title[:100]}  ->  DECIDE: APPROVE / DEFER / CLOSE"
            )
        lines.append("")

    if errors:
        lines.append("Board scan errors (investigate probe, not the cards):")
        for board, err in errors:
            lines.append(f"- {board}: {err}")
        lines.append("")

    lines.append(
        "Guardrails: this digest is a routing artifact only. It grants no "
        "auto-apply authority; every listed card keeps its own A3 gating. "
        "Frank retains veto on every item. No auto-apply."
    )

    return {
        "title": (
            f"{DIGEST_TITLE_PREFIX} ({iso_week_label}): "
            f"{len(all_breached)} items — approve/defer/close"
        ),
        "assignee": DEFAULT_TARGET_ASSIGNEE,
        "board": board,
        "priority": 90,
        "idempotency_key": f"frank-a3-weekly-digest-{iso_week_label}",
        "body": "\n".join(lines),
        "metadata": {
            "probe": "needs-input-sla-probe",
            "mode": "weekly-digest",
            "source_task": "t_7c598bd9",
            "strategy": "t_b50df0ef",
            "iso_week": iso_week_label,
            "sla_hours": sla_hours,
            "breached_count": len(all_breached),
            "per_board": per_board_counts,
            "sections": {s: len(grouped.get(s, [])) for s, _ in DIGEST_SECTIONS + [(OTHER_SECTION, [])]},
            "breached": [c.to_dict() for c in all_breached],
            "scan_errors": [{"board": b, "error": e} for b, e in errors],
        },
    }


def iso_week_label(d: dt.date) -> str:
    """Return the ISO week label 'YYYY-WW' for a date (e.g. 2026-W33)."""
    iso = d.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


# ─── Consolidated card payload ─────────────────────────────────────────

def build_consolidated_card(
    scans: list[BoardScan],
    sla_hours: int,
    run_date: str,
) -> dict | None:
    """Build the single consolidated 'needs_input SLA breach' card payload.

    Returns None when there is nothing to report (no breaches, no errors),
    which is the probe's quiet-green state.
    """
    all_breached = [c for s in scans for c in s.breached]
    errors = [(s.board, s.error) for s in scans if s.error]
    if not all_breached and not errors:
        return None

    per_board_counts = {s.board: len(s.breached) for s in scans}
    lines = [
        "CONSOLIDATED NEEDS_INPUT SLA BREACH — Frank A3 decision batch.",
        "",
        f"SLA: needs_input cards must receive a decision within {sla_hours}h of "
        "COALESCE(started_at, created_at). The cards below are past SLA and have "
        "no single owner; they are rotting. Decide per card: assign an owner, "
        "answer the embedded decision stub, or explicitly defer with a new SLA.",
        "",
        f"Breached counts: "
        + ", ".join(f"{b}={n}" for b, n in per_board_counts.items()),
        "",
        "Breached cards (board/task_id, age, assignee, title):",
    ]
    for c in sorted(all_breached, key=lambda c: -c.age_hours):
        lines.append(
            f"- {c.board}/{c.task_id} | {c.age_hours}h | "
            f"{c.assignee or 'unassigned'} | {c.title[:90]}"
        )
    if errors:
        lines.append("")
        lines.append("Board scan errors (investigate probe, not the cards):")
        for board, err in errors:
            lines.append(f"- {board}: {err}")
    lines.append("")
    lines.append(
        "Guardrails: this card is a routing artifact only. It grants no "
        "auto-apply authority; every listed card keeps its own A3 gating. "
        "Frank retains veto on every item. No auto-apply."
    )

    return {
        "title": (
            f"NEEDS_INPUT SLA BREACH ({run_date}): "
            f"{len(all_breached)} cards past {sla_hours}h across "
            f"{sum(1 for n in per_board_counts.values() if n)} boards"
        ),
        "assignee": DEFAULT_TARGET_ASSIGNEE,
        "board": DEFAULT_TARGET_BOARD,
        "priority": 90,
        "idempotency_key": f"needs-input-sla-breach-{run_date}",
        "body": "\n".join(lines),
        "metadata": {
            "probe": "needs-input-sla-probe",
            "source_task": "t_27c22567",
            "sla_hours": sla_hours,
            "breached_count": len(all_breached),
            "per_board": per_board_counts,
            "breached": [c.to_dict() for c in all_breached],
            "scan_errors": [{"board": b, "error": e} for b, e in errors],
        },
    }


def format_dry_run(
    scans: list[BoardScan], card: dict | None, sla_hours: int,
    suppressors: list[str] | None = None,
) -> str:
    out: list[str] = []
    out.append(f"=== needs_input SLA probe DRY-RUN (SLA={sla_hours}h) ===")
    total = 0
    for s in scans:
        if s.error:
            out.append(f"[ERR] {s.board}: {s.error}")
            continue
        out.append(f"[{s.board}] {len(s.breached)} breached:")
        for c in s.breached:
            out.append(
                f"  {c.task_id} | {c.age_hours:>4}h | {c.assignee or '-':<20} | {c.title[:70]}"
            )
        total += len(s.breached)
    out.append(f"=== total breached: {total} ===")
    out.append("")
    if card is None:
        out.append("Quiet-green: no breaches, no card would be emitted.")
    else:
        out.append("Consolidated card that --emit WOULD create (dry-run, no writes):")
        out.append(json.dumps({k: v for k, v in card.items() if k != "metadata"}, indent=2))
        out.append("metadata.breached_count: " + str(card["metadata"]["breached_count"]))
    # Emission-guard verdict (t_3824e584): the full payload is still printed
    # above so the reviewer can see exactly what --emit would create, but the
    # guard note tells whether --emit would actually be suppressed.
    out.append("")
    if suppressors:
        out.append("Emission guard: --emit WOULD BE SUPPRESSED (existing owner/breach card):")
        for line in suppressors:
            out.append(f"  - {line}")
    else:
        out.append("Emission guard: no open SLA-owner/breach card — --emit would proceed.")
    return "\n".join(out)


# ─── Emission (gated, call ``--emit`` only) ────────────────────────────

def emit_card(card: dict) -> int:
    """Create the consolidated card with block_kind='needs_input' via hermes CLI.

    Two-step: `hermes kanban create --board <board> --initial-status running
    --assignee <a> --priority 90 --idempotency-key <k> --body <b> <title>`
    (so card exists), then `hermes kanban block --board <board> --kind
    needs_input <task-id> <reason>` (stamps block_kind and blocker stats).

    Idempotency key prevents the same calendar-day sweep from creating a
    duplicate: a re-run within a UTC day returns the existing card.

    The subprocess env is sanitized of HERMES_KANBAN_DB / HERMES_KANBAN_BOARD
    so the CLI's `--board <card board>` is authoritative even when the probe is
    invoked from a kanban-worker session whose env pins another board (the
    worker env otherwise overrides --board and the card lands on the wrong
    board — hit during t_7c598bd9 test emits).
    """
    # Clean env for the CLI subprocess: remove any board-pinning vars so
    # `--board` (from card["board"]) resolves the target board, never the
    # caller's pinned HERMES_KANBAN_DB/BOARD.
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")}
    # Step 1: create the card (runs briefly, then blocked)
    create_cmd = [
        "hermes", "kanban", "--board", card["board"], "create",
        "--assignee", card["assignee"],
        "--priority", str(card["priority"]),
        "--idempotency-key", card["idempotency_key"],
        "--initial-status", "running",
        "--json",
        "--body", card["body"],
        card["title"],
    ]
    proc = subprocess.run(create_cmd, capture_output=True, text=True, env=clean_env)
    if proc.returncode != 0:
        sys.stderr.write(f"create failed (rc={proc.returncode}):\n{proc.stderr}\n{proc.stdout}\n")
        return proc.returncode

    task_id = None
    already_blocked = False
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
            task_id = payload.get("id")  # --json returns the task object
            # Idempotent re-create (same daily idempotency key) returns the
            # EXISTING card, already status=blocked from the first sweep of the
            # day. Re-running `block` on it fails with "cannot block <id>", which
            # would record a spurious failed cron run every 6h. Detect it here
            # and skip the block step.
            already_blocked = payload.get("status") == "blocked"
        except json.JSONDecodeError:
            # fallback: parse plain-text "Created <task_id> (..."
            tail = proc.stdout.split()[-1] if proc.stdout else ""
            if tail.startswith("t_"):
                task_id = tail

    if not task_id:
        sys.stderr.write("could not parse task-id from create output\n")
        return 1

    if already_blocked:
        sys.stdout.write(
            f"idempotent re-run: card {task_id} already blocked "
            "(created earlier today under the same idempotency key); "
            "skipping block step\n"
        )
        return 0

    # Step 2: block it with block_kind='needs_input' — two-step because
    # ``kanban create`` has no --block-kind flag and block_task() requires a
    # running/ready card.
    block_reason = (
        f"{card['metadata'].get('mode', 'Consolidated SLA breach')}: "
        f"{card['metadata']['breached_count']} stale needs_input cards across "
        f"{len(card['metadata']['per_board'])} boards"
    )
    block_cmd = [
        "hermes", "kanban", "--board", card["board"], "block",
        "--kind", "needs_input",
        task_id,
        block_reason,
    ]
    proc = subprocess.run(block_cmd, capture_output=True, text=True, env=clean_env)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(f"block failed (rc={proc.returncode}):\n{proc.stderr}\n")

    return proc.returncode


# ─── Liveness monitor ──────────────────────────────────────────────────

def write_heartbeat(hb_path: str) -> None:
    """Write a liveness heartbeat.

    Format: <epoch_seconds>\n so a monitor can compare mtime/age or parse the
    integer.  An absent or too-old file triggers the liveness alarm.
    """
    try:
        with open(hb_path, "w") as f:
            f.write(f"{int(dt.datetime.now(dt.timezone.utc).timestamp())}\n")
    except OSError as e:
        sys.stderr.write(f"heartbeat write failed: {e}\n")


# ─── CLI ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="needs_input SLA probe (dry-run by default; --emit creates the card)",
    )
    parser.add_argument(
        "--board", "-b", action="append",
        help="Board as /path/to/kanban.db:slug. Repeatable. Defaults to jarvis-os, sycode-trading, upero.",
    )
    parser.add_argument("--sla-hours", type=int, default=DEFAULT_SLA_HOURS)
    parser.add_argument(
        "--weekly", action="store_true",
        help="Emit the WEEKLY Frank A3 digest (one card per ISO week, grouped "
             "triage) instead of the daily consolidated breach card. Combined "
             "with --emit it creates the weekly digest; alone it dry-runs it. "
             "Idempotency key frank-a3-weekly-digest-<ISO-week>.",
    )
    parser.add_argument(
        "--target-board", default=DEFAULT_TARGET_BOARD,
        help=f"Board slug the consolidated/digest card lands on. Default: {DEFAULT_TARGET_BOARD}. "
             "Use for a safe test-board emit.",
    )
    parser.add_argument(
        "--emit", action="store_true",
        help="Create the card (post-review only).",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable dry-run output.")
    parser.add_argument(
        "--heartbeat", metavar="PATH", default=None,
        help="Write a liveness heartbeat file at the given absolute path. "
             "Essential for the correctness-monitor (SEAT DECISION binding req #3).",
    )
    parser.add_argument(
        "--heartbeat-path", metavar="PATH", default="/tmp/needs-input-sla-probe.heartbeat",
        help="Alternative absolute path for the liveness file. "
             "(Default: /tmp/needs-input-sla-probe.heartbeat)",
    )
    args = parser.parse_args()

    boards = DEFAULT_BOARDS
    if args.board:
        boards = []
        for entry in args.board:
            path, slug = entry.rsplit(":", 1)
            boards.append((path, slug))

    hb_path = args.heartbeat or args.heartbeat_path
    target_board = args.target_board

    run_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    scans = scan_all(boards, args.sla_hours)

    if args.weekly:
        week_label = iso_week_label(dt.date.today())
        prior = find_prior_week_count(target_board, week_label)
        card = build_weekly_digest_card(scans, args.sla_hours, week_label,
                                        prior_delta=prior, board=target_board)
        target_prefix = DIGEST_TITLE_PREFIX
        mode = "weekly-digest"
    else:
        week_label = ""  # unused in daily mode; keeps the dry-run line simple
        card = build_consolidated_card(scans, args.sla_hours, run_date)
        target_prefix = BREACH_TITLE_PREFIX
        mode = "daily-breach"

    suppressors = scan_emit_suppressors(boards, target_board, target_prefix=target_prefix)

    if not args.emit:
        if args.json:
            print(json.dumps({
                "mode": mode,
                "sla_hours": args.sla_hours,
                "scans": [
                    {
                        "board": s.board,
                        "error": s.error,
                        "breached": [c.to_dict() for c in s.breached],
                    }
                    for s in scans
                ],
                "would_emit": card is not None and not suppressors,
                "card_preview": card,
                "emit_suppressors": suppressors,
            }, indent=2))
        else:
            print(format_dry_run(scans, card, args.sla_hours, suppressors))
            if card is not None:
                print(f"\n[mode: {mode} | ISO week: {week_label if args.weekly else run_date} | "
                      f"target board: {target_board}]")

        # Exit 1 when a card would be emitted; 0 when quiet. If emission would be
        # suppressed by an open owner/target card, that is a healthy quiet state
        # (the loop is intentionally not re-minting), so exit 0.
        # Write heartbeat AFTER determining the exit code — the sweep completed
        # regardless of breach; a missing file is the liveness alarm.
        if hb_path:
            write_heartbeat(hb_path)
        sys.exit(1 if card and not suppressors else 0)

    # --emit mode
    if card is None:
        print("quiet-green: nothing to emit")
        if hb_path:
            write_heartbeat(hb_path)
        sys.exit(0)

    # Emission guard (t_3824e584 / t_7c598bd9): skip creation while any open
    # SLA-owner/decomposition card is active on a scanned board, or while a
    # non-closed target card (daily breach OR weekly digest) already exists on
    # the target board. Reuse the existing card rather than minting a duplicate.
    if suppressors:
        sys.stdout.write(
            "emit suppressed by emission guard — reusing existing owner/target "
            "card instead of minting a duplicate:\n"
        )
        for line in suppressors:
            sys.stdout.write(f"  - {line}\n")
        if hb_path:
            write_heartbeat(hb_path)
        sys.exit(0)

    rc = emit_card(card)

    if hb_path:
        write_heartbeat(hb_path)

    sys.exit(rc)


if __name__ == "__main__":
    main()