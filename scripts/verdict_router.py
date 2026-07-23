#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Deterministic kanban REVIEW_VERDICT router.

Dry-run by default. In dry-run it only appends shadow decisions to the fleet
Obsidian vault. Mutations require either VERDICT_ROUTER_APPLY=1 or the sentinel
/home/frank/.hermes/cron/state/verdict-router.apply-enabled.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from second_brain_writer import append_markdown_event

ROOT = Path(os.environ.get("VERDICT_ROUTER_ROOT", "/home/frank/.hermes"))
BOARDS_DIR = Path(os.environ.get("VERDICT_ROUTER_BOARDS_DIR", str(ROOT / "kanban" / "boards")))
DEFAULT_DB = Path(os.environ.get("VERDICT_ROUTER_DEFAULT_DB", str(ROOT / "kanban.db")))
STATE_DIR = Path(os.environ.get("VERDICT_ROUTER_STATE_DIR", str(ROOT / "cron" / "state")))
ENABLE_SENTINEL = STATE_DIR / "verdict-router.apply-enabled"
LOG_DIR = ROOT / "scripts" / "logs"
VAULT_ROOT = Path(os.environ.get("VERDICT_ROUTER_VAULT_ROOT", "/home/frank/obsidian-fleet-vault"))
VAULT_NOTE_DIR = Path(os.environ.get("VERDICT_ROUTER_NOTE_DIR", str(VAULT_ROOT / "Orchestration" / "kanban-verdict-router")))
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
AUTHOR = os.environ.get("VERDICT_ROUTER_AUTHOR", "verdict-router")
LOCK_PATH = STATE_DIR / "verdict-router.lock"
SCRIPT_VERSION = "v1"

VERDICT_RE = re.compile(r"\bREVIEW_VERDICT\s*[:=]\s*([A-Z0-9_]+)", re.I)
TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{8,}\b", re.I)
ROUTER_AUTHORS = {AUTHOR, "cron:deterministic-verdict-router"}
VALID_VERDICTS = {"APPROVE", "APPROVED", "CHANGES_REQUESTED"}

# C4 fix (t_c996e275): negation-aware verdict detection so the router does not
# treat negated / no-verdict prose as an affirmative verdict declaration.
#
# Root cause: VERDICT_RE is a bare lexical matcher. A reviewer comment such as
# "NO REVIEW_VERDICT issued" or "No REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED"
# still contains the substring "REVIEW_VERDICT=APPROVED" and was parsed as an
# APPROVED verdict, emitting a spurious NEEDS-PM marker (incident: sycode-
# trading/t_19901020, comment_id=14406, shadow-log 2026-07-19T20:35:25Z).
#
# Fix: a verdict declaration only counts when its sentence/clause carries no
# negation cue (no / not / do not / never / without / missing / absent / none /
# unissued / "no verdict" ...). A REVIEW_VERDICT token inside a negation scope
# is a denial (or an explicit statement that no verdict was issued) and must not
# route. This mirrors the sentence-scoped negation scope used by the operator-
# gate detector above. Genuine affirmative verdicts ("REVIEW_VERDICT=APPROVED",
# "Target: t_... REVIEW_VERDICT=APPROVED") keep their noun and still route.
_VERDICT_NEGATION_CUES = (
    r"no|not|without|do\s+not|don'?t|never|none|neither|absent|missing|"
    r"unissued|did\s+not|didn'?t|wasn'?t|weren'?t|isn'?t|aren'?t|"
    r"no[- ]?verdict|no[- ]?review[- ]?verdict|void|voided|lack\s+of|"
    r"decline[ds]?|withdrawn|not\s+post|do\s+not\s+post|never\s+post"
)
VERDICT_NEGATION_CUE_RE = re.compile(r"(?:\b" + _VERDICT_NEGATION_CUES + r"\b)", re.I)
_VERDICT_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?\n]")
# Trailing "..." / "/" / "," are verdict-list continuations, not negation scope
# ends, so the sentence boundary for a verdict is the next real sentence break.
_VERDICT_LIST_SEP_RE = re.compile(r"[/,]|\.\.\.|…")


def _verdict_sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Return the sentence/clause span containing the verdict token
    [start:end]. Cuts at '.', '!', '?' or newline, but not at a bare '/' or ','
    (which join a list like "APPROVED/CHANGES_REQUESTED" or "APPROVED, CHANGES").
    """
    sent_start = 0
    for m in _VERDICT_SENTENCE_BOUNDARY_RE.finditer(text, 0, start):
        sent_start = m.end()
    sent_end = len(text)
    for m in _VERDICT_SENTENCE_BOUNDARY_RE.finditer(text, end):
        # Skip list separators so "No REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED"
        # stays inside one negation scope.
        sep = m.group(0)
        if _VERDICT_LIST_SEP_RE.fullmatch(sep):
            continue
        sent_end = m.start()
        break
    return sent_start, sent_end


def _is_negated_verdict(text: str, start: int, end: int) -> bool:
    """True if the verdict token [start:end] sits inside a negation scope — a
    negation cue appears before it in the same sentence (e.g. "No
    REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED") OR the verdict token itself is
    the denial subject ("NO REVIEW_VERDICT issued" — cue 'NO' precedes 'REVIEW_VERDICT').
    """
    sent_start, sent_end = _verdict_sentence_span(text, start, end)
    sentence = text[sent_start:sent_end]
    return VERDICT_NEGATION_CUE_RE.search(sentence) is not None


def verdict_declarations(text: str) -> "list[re.Match[str]]":
    """Return only *affirmative* REVIEW_VERDICT matches — i.e. matches whose
    sentence/clause carries no negation cue. Negated / no-verdict prose is
    excluded (fail closed).
    """
    return [m for m in VERDICT_RE.finditer(text) if not _is_negated_verdict(text, m.start(), m.end())]

EXCLUDED_BOARDS = {"orchestrator-sync"}  # coordination-only boards, not task boards

FORBIDDEN_SCOPE_INNER = (
    r"deploy(?:ment)?|prod(?:uction)?|runtime|go[- ]?live|live[-_ ]?(?:mode|trading|capped)|"
    r"gateway\s+restart|service\s+restart|cron\s+activation|apply\s+sentinel|"
    r"database\s+migration|database\s+write|database\s+schema|db\s+migration|db\s+write|schema\s+(?:migration|change|update|write|definition)|seed|delete|drop\s+table|truncate|mass\s+delete|irreversible|"
    r"credential|secret|token|api[-_ ]?key|auth\s+(?:provider|rotation|token|change|fix|update|config|service|service\s+change|path)|payment|pricing|checkout|refund|money|spend|billing|"
    r"a3|operator\s+approval|maintainer\s+approval|frank\s+approval|push/merge-to-trunk|workforce-scaler"
)

FORBIDDEN_SCOPE_RE = re.compile(r"\b(" + FORBIDDEN_SCOPE_INNER + r")\b", re.I)

# C2 fix (t_8874b97b / proposal t_9a0af491): negation-aware operator-gate detection.
#
# Root cause: the operator-gate detector did pure lexical substring matching of
# ~25 forbidden nouns over title+body+reviewer-comment. A card whose OWN text
# *denies* a gate ("no prod, no creds", "do not deploy", "A3-safe", "no
# production deploy, credentials") still matched the noun and got stranded with a
# false NEEDS-OPERATOR. This is the false-positive class the proposal fixes.
#
# Fix: before scanning for forbidden nouns, redact gate-DENIAL narration — a
# forbidden noun asserted inside a clause that *denies* a gate (contains a
# denial cue like "no"/"not"/"do not"/"safe"/"A3-safe"/"preserved") cannot trip
# the detector. A *positive* gate assertion ("Run production DB migration and
# live runtime deploy") lives in a clause with no denial cue, so it still gates.
# This is general across all 25 terms (not the rejected brittle 3-term patch).
_GATE_DENIAL_CUES = (
    r"no|not|without|do\s+not|don'?t|free\s+of|den(?:y|ied|ial)|avoid\w*|never|"
    r"unnecessary|exclud\w*|waiv\w*|safe|intact|preserv\w*|unchanged|"
    r"no[- ]?op|non[- ]?gated|frank[- ]?gated|out[- ]?of[- ]?scope"
)
GATE_DENIAL_CUE_RE = re.compile(r"\b(?:" + _GATE_DENIAL_CUES + r")\b", re.I)
# A forbidden noun is a gate-DENIAL (safe to ignore) when a denial cue precedes
# it within the same sentence/clause — i.e. the noun sits inside the cue's
# negation scope ("no credential, prod, or DB change"; "do not deploy"). A noun
# with NO preceding cue in its sentence is a positive gate assertion and still
# gates. Sentence boundary = . ! ? or newline.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?\n]")


def _is_denied_noun(text: str, noun_start: int, noun_end: int) -> bool:
    """True if the forbidden noun [noun_start:noun_end] is a gate-DENIAL: a denial
    cue appears in the same sentence, either preceding it or following it
    (e.g. "A3 gates intact" — cue 'intact' follows; "no prod change" — cue 'no'
    precedes). A noun with no cue in its sentence is a positive gate assertion.
    Sentence boundary = . ! ? or newline.
    """
    # Sentence span containing the noun.
    sent_start = 0
    for m in _SENTENCE_BOUNDARY_RE.finditer(text, 0, noun_start):
        sent_start = m.end()
    sent_end = len(text)
    for m in _SENTENCE_BOUNDARY_RE.finditer(text, noun_end):
        sent_end = m.start()
        break
    sentence = text[sent_start:sent_end]
    return GATE_DENIAL_CUE_RE.search(sentence) is not None


def redact_gate_denials(text: str) -> str:
    """Return ``text`` with gate-DENIAL *nouns* blanked so they cannot trip the
    operator-gate detector. A forbidden noun is treated as a denial only when a
    denial cue (no/not/do not/safe/A3-safe/preserved/...) precedes it within the
    same sentence. Genuine *positive* gate assertions (no preceding cue) keep
    their noun and still gate.
    """
    if not text:
        return ""
    out = []
    last = 0
    for m in FORBIDDEN_SCOPE_RE.finditer(text):
        out.append(text[last:m.start()])
        if _is_denied_noun(text, m.start(), m.end()):
            out.append(" ")
        else:
            out.append(m.group(0))  # keep positive gate assertion
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def operator_gate_terms(title: str, body: str, comment: str | None = None) -> "re.Match[str] | None":
    """Operator-gated detection (C2 fix — t_8874b97b / proposal t_9a0af491).

    Gate ONLY on task title/body scope (and, per the safety contract, block
    reason / structured gate metadata) — NEVER on reviewer comment prose. The
    proposal's corrected C2 mechanism explicitly excludes reviewer comment prose
    because a verdict that *denies* a gate must not strand an approved card.

    Within the title/body, gate-DENIAL narration (a forbidden noun in a sentence
    containing a denial cue like "no"/"not"/"do not"/"safe"/"A3-safe"/"preserved")
    is redacted so it cannot trip the detector. A *positive* gate assertion
    ("Run production DB migration and live runtime deploy") still gates. This is
    general across all ~25 terms (not the rejected brittle 3-term patch). Over-
    broad tokens that collide with code identifiers (database/auth/schema) are
    scoped to gate-context phrases.
    """
    # C2: reviewer comment prose is excluded from the operator-gate scan.
    combined = "\n".join([title or "", body or ""])
    return FORBIDDEN_SCOPE_RE.search(redact_gate_denials(combined))

SAFE_DELIVERABLE_RE = re.compile(
    r"\b(source|code|patch|diff|docs?|documentation|spec|test(?:s|ing)?|fixture|lint|typecheck|unit|build)\b",
    re.I,
)
READ_ONLY_ASSERTION_RE = re.compile(
    r"\b(read[-_ ]?(?:only|only)|paper[-_ ]?only|no[-_ ]?(?:db\s+write|engine\s+mutation|production\s+deploy|cron\s+change|credential\s+(?:rotat|change|creat)|insert|update|delete|alter|drop(?:\s+table)?))\b",
    re.I,
)
REVIEW_REQUIRED_RE = re.compile(r"\breview[-_ ]required\b", re.I)
FRONTEND_APP_RE = re.compile(r"\b(apps/web/|frontend|page\.tsx|layout\.tsx|component|middleware|trpc)\b", re.I)
VERIFY_PASS_RE = re.compile(r"\bVERIFY_PASS\b")
NEGATED_VERIFY_PASS_RE = re.compile(r"\b(no|missing|without|absent)\b.*\bVERIFY_PASS\b|\bVERIFY_PASS\b.*\b(missing|absent|not\s+present)\b", re.I)


@dataclass(frozen=True)
class Board:
    slug: str
    db: Path


@dataclass(frozen=True)
class Candidate:
    board: Board
    task_id: str
    title: str
    body: str
    assignee: str | None
    latest_comment_id: int
    latest_comment_author: str
    latest_comment_body: str
    latest_comment_created_at: int


@dataclass(frozen=True)
class Decision:
    board: str
    task_id: str
    comment_id: int
    verdict: str | None
    action: str
    reason: str
    dry_run: bool
    source_author: str
    target_validation: str
    scope_class: str
    result: str
    idempotency_action: str | None = None

    @property
    def idempotency_key(self) -> str:
        action = self.idempotency_action or self.action
        return f"verdict-router:v1:{self.board}:{self.task_id}:comment:{self.comment_id}:action:{action}"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_note() -> Path:
    return VAULT_NOTE_DIR / f"{dt.datetime.now(dt.timezone.utc).date().isoformat()}-verdict-router-shadow-log.md"


def append_note(entry: dict) -> None:
    note = today_note()
    event = (
        f"### {entry['timestamp']} — {entry['board']}/{entry['task_id']}\n"
        f"- script_version: `{SCRIPT_VERSION}`\n"
        f"- mode: `{entry['mode']}`\n"
        f"- action: `{entry['action']}`\n"
        f"- verdict_value: `{entry.get('verdict_value', entry.get('verdict'))}`\n"
        f"- source_comment_id: `{entry.get('source_comment_id', entry.get('comment_id'))}`\n"
        f"- source_author: `{entry.get('source_author', '')}`\n"
        f"- target_validation: `{entry.get('target_validation', 'not-applicable')}`\n"
        f"- scope_class: `{entry.get('scope_class', 'unknown')}`\n"
        f"- result: `{entry.get('result', 'skipped')}`\n"
        f"- idempotency_key: `{entry['idempotency_key']}`\n"
        f"- reason: {entry['reason']}\n"
    )
    if entry.get("command"):
        event += f"- command: `{entry['command']}`\n"
    if entry.get("stdout"):
        event += f"- stdout: `{entry['stdout'][:500]}`\n"
    if entry.get("stderr"):
        event += f"- stderr: `{entry['stderr'][:500]}`\n"
    report_date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    append_markdown_event(
        note,
        event,
        initial_body=(
            "# Kanban Verdict Router Shadow Log\n\n"
            "Logs deterministic decisions by `verdict_router.py` for REVIEW_VERDICT comments. "
            "Dry-run mode is the default until os-reviewer approves mutation enablement.\n\n"
            "Related: [[Learnings/2026-07-03-verdict-routing-gap|verdict-routing gap]]; "
            "kanban task `t_2afc2c67`.\n\n## Entries"
        ),
        title=f"Kanban Verdict Router Shadow Log — {report_date}",
        type="task-evidence",
        status="active",
        created=report_date,
        updated=report_date,
        confidence="high",
        tags=["hermes", "kanban", "verdict-router", "shadow-log"],
        sources=["/home/frank/.hermes/kanban/boards"],
        project="control-plane",
        owners=["jarvis"],
        knowledge_tier="evidence",
        generated=True,
        generator="verdict_router.py",
    )


def append_run_log(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / f"verdict-router_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')}.log").open("a", encoding="utf-8") as f:
        f.write(f"[{utc_now()}] {line}\n")


def boards() -> list[Board]:
    found: list[Board] = []
    if DEFAULT_DB.exists():
        found.append(Board("default", DEFAULT_DB))
    if BOARDS_DIR.exists():
        for db in sorted(BOARDS_DIR.glob("*/kanban.db")):
            slug = db.parent.name
            if slug.startswith("_"):
                continue
            if slug in EXCLUDED_BOARDS:
                continue
            found.append(Board(slug, db))
    return found


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def safe_int(value: Any, *, context: str, default: int | None = None) -> int | None:
    """Parse integer-ish SQLite values without letting corrupt rows abort a board scan."""
    try:
        if value is None:
            raise TypeError("None is not an integer")
        return int(value)
    except (TypeError, ValueError):
        append_run_log(f"skip-nonnumeric-int context={context} value={value!r}")
        return default


def latest_comment(con: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    if not table_exists(con, "task_comments"):
        return None
    placeholders = ",".join("?" for _ in ROUTER_AUTHORS)
    rows = con.execute(
        f"""
        SELECT id, author, body, created_at
          FROM task_comments
         WHERE task_id=?
           AND COALESCE(author, '') NOT IN ({placeholders})
         ORDER BY id DESC
        """,
        (task_id, *sorted(ROUTER_AUTHORS)),
    ).fetchall()
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            safe_int(row["created_at"], context=f"{task_id}:task_comments.created_at:{row['id']}", default=0) or 0,
            safe_int(row["id"], context=f"{task_id}:task_comments.id", default=0) or 0,
        ),
    )


def prior_router_marker(con: sqlite3.Connection, task_id: str, marker: str) -> bool:
    if not table_exists(con, "task_comments"):
        return False
    row = con.execute(
        "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
        (task_id, f"%{marker}%"),
    ).fetchone()
    if row is not None:
        return True
    legacy_marker = marker.replace("verdict-router:v1:", "verdict-router:", 1)
    if legacy_marker != marker:
        row = con.execute(
            "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
            (task_id, f"%{legacy_marker}%"),
        ).fetchone()
        if row is not None:
            return True
    return False


def router_processed_comment(con: sqlite3.Connection, task_id: str, comment_id: int) -> bool:
    """Check if the verdict-router already left a marker for this task+comment combo."""
    if not table_exists(con, "task_comments"):
        return False
    placeholders = ",".join("?" for _ in ROUTER_AUTHORS)
    return con.execute(
        f"SELECT 1 FROM task_comments WHERE task_id=? AND author IN ({placeholders}) AND body LIKE ? LIMIT 1",
        (task_id, *sorted(ROUTER_AUTHORS), f"%verdict-router marker=%comment:{comment_id}:action:%"),
    ).fetchone() is not None


def candidates_for_board(board: Board) -> list[Candidate]:
    con = sqlite3.connect(str(board.db))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, title, body, assignee
              FROM tasks
             WHERE status = 'blocked'
             ORDER BY priority DESC, created_at ASC
            """
        ).fetchall()
        out: list[Candidate] = []
        for row in rows:
            comment = latest_comment(con, row["id"])
            if comment is None:
                continue
            comment_id = safe_int(comment["id"], context=f"{board.slug}/{row['id']}:task_comments.id")
            if comment_id is None:
                continue
            comment_created_at = safe_int(
                comment["created_at"],
                context=f"{board.slug}/{row['id']}:task_comments.created_at:{comment_id}",
                default=0,
            )
            body = comment["body"] or ""
            # C4 fix (t_c996e275): a candidate must contain an *affirmative*
            # verdict declaration. Negated / no-verdict prose ("No
            # REVIEW_VERDICT=APPROVED", "NO REVIEW_VERDICT issued") must not
            # enter the routing pipeline at all — it is a denial, not a verdict.
            if not verdict_declarations(body):
                continue
            if router_processed_comment(con, row["id"], comment_id):
                continue
            task_text = "\n".join([row["title"] or "", row["body"] or ""])
            if not (REVIEW_REQUIRED_RE.search(task_text) or REVIEW_REQUIRED_RE.search(body) or verdict_declarations(body)):
                continue
            out.append(
                Candidate(
                    board=board,
                    task_id=row["id"],
                    title=row["title"] or "",
                    body=row["body"] or "",
                    assignee=row["assignee"],
                    latest_comment_id=comment_id,
                    latest_comment_author=comment["author"] or "",
                    latest_comment_body=body,
                    latest_comment_created_at=comment_created_at or 0,
                )
            )
        return out
    finally:
        con.close()


def parse_verdict(comment_body: str) -> str | None:
    # C4 fix (t_c996e275): only *affirmative* verdict declarations count. A
    # negated / no-verdict token (e.g. "No REVIEW_VERDICT=APPROVED") is a denial
    # and must not route as a verdict. Fail closed: 0 or >1 affirmative tokens
    # => None (ambiguous / no verdict).
    matches = verdict_declarations(comment_body)
    if len(matches) != 1:
        return None
    raw = matches[0].group(1).strip().upper()
    if raw == "APPROVE":
        return "APPROVED"
    if raw in {"REJECT", "REJECTED"}:
        return "REJECT"
    if raw in VALID_VERDICTS:
        return raw
    return raw[:80] or "AMBIGUOUS"


def task_ids_in_comment(candidate: Candidate) -> list[str]:
    return [m.group(0) for m in TASK_ID_RE.finditer(candidate.latest_comment_body)]


def target_validation(candidate: Candidate, verdict: str | None) -> str:
    if verdict is None:
        return "not-applicable"
    ids = task_ids_in_comment(candidate)
    unique = set(ids)
    if verdict == "CHANGES_REQUESTED" and not unique:
        return "same-card"
    if not unique:
        return "missing-target"
    if len(unique) > 1:
        return "multi-target"
    return "same-card" if candidate.task_id in unique else "cross-target"


def comment_mentions_other_card(candidate: Candidate) -> bool:
    ids = set(task_ids_in_comment(candidate))
    return bool(ids and (ids - {candidate.task_id}))


def first_finding_excerpt(comment_body: str) -> str:
    for line in comment_body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("REVIEW_VERDICT") or stripped.lower().startswith("target"):
            continue
        if re.search(r"finding|block|fail", stripped, re.I):
            return stripped[:240]
    for line in comment_body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.upper().startswith("REVIEW_VERDICT"):
            return stripped[:240]
    return "reviewer requested changes"


def classify_scope(candidate: Candidate, verdict: str | None) -> tuple[str, str]:
    # C2 fix: operator-gate detection scans title/body only (reviewer comment
    # prose excluded) and uses negation-aware redaction so gate-DENIAL narration
    # ("no prod, no creds", "A3-safe", "schema.prisma" file path) cannot strand
    # a card — see operator_gate_terms.
    forbidden = operator_gate_terms(candidate.title, candidate.body)
    if forbidden:
        task_text = "\n".join([candidate.title or "", candidate.body or ""])
        if READ_ONLY_ASSERTION_RE.search(task_text):
            return (
                "source_docs_spec_test_only",
                f"read-only/paper-only/no-write assertion overrides bare lexical gate term "
                f"{forbidden.group(0)!r} on title/body",
            )
        return "operator_gated", f"operator-gated term: {forbidden.group(0)!r}"
    if verdict == "REJECT":
        return "standard", "standard scope (no operator-gated keywords)"
    if verdict not in {"APPROVED", "CHANGES_REQUESTED"}:
        return "ambiguous", "unrecognized or ambiguous REVIEW_VERDICT value"
    # SAFE_DELIVERABLE_RE still requires a source/docs/spec/test deliverable
    # anchor before a same-card APPROVED card may auto-complete (fail-closed).
    combined = "\n".join([candidate.title, candidate.body, candidate.latest_comment_body])
    if not SAFE_DELIVERABLE_RE.search(combined):
        return "ambiguous", "no source/docs/spec/test deliverable marker found"
    return "source_docs_spec_test_only", "source/docs/spec/test scope markers and no operator-gated keywords"


def frontend_app_without_verify_pass(candidate: Candidate) -> bool:
    combined = "\n".join([candidate.title, candidate.body, candidate.latest_comment_body])
    if not FRONTEND_APP_RE.search(combined):
        return False
    for line in combined.splitlines():
        if VERIFY_PASS_RE.search(line) and not NEGATED_VERIFY_PASS_RE.search(line):
            return False
    return True


def make_decision(candidate: Candidate, verdict: str | None, action: str, reason: str, dry_run: bool, validation: str, scope: str, result: str) -> Decision:
    return Decision(
        candidate.board.slug,
        candidate.task_id,
        candidate.latest_comment_id,
        verdict,
        action,
        reason,
        dry_run,
        candidate.latest_comment_author,
        validation,
        scope,
        result,
    )


def mark_idempotent(decision: Decision) -> Decision:
    return Decision(
        decision.board,
        decision.task_id,
        decision.comment_id,
        decision.verdict,
        "skip",
        "idempotency key already present",
        decision.dry_run,
        decision.source_author,
        decision.target_validation,
        decision.scope_class,
        "skipped_idempotent",
        decision.action,
    )


def decide(candidate: Candidate, dry_run: bool) -> Decision:
    verdict = parse_verdict(candidate.latest_comment_body)
    validation = target_validation(candidate, verdict)
    scope, scope_reason = classify_scope(candidate, verdict)

    if verdict == "APPROVED":
        if scope == "operator_gated":
            return make_decision(candidate, verdict, "needs_operator", scope_reason, dry_run, validation, scope, "would_comment")
        if validation != "same-card":
            return make_decision(candidate, verdict, "needs_pm", f"target validation {validation}", dry_run, validation, "ambiguous", "would_comment")
        if frontend_app_without_verify_pass(candidate):
            return make_decision(candidate, verdict, "needs_pm", "frontend/app work without VERIFY_PASS", dry_run, validation, "ambiguous", "would_comment")
        if scope != "source_docs_spec_test_only":
            return make_decision(candidate, verdict, "needs_pm", scope_reason, dry_run, validation, scope, "would_comment")
        return make_decision(candidate, verdict, "complete", "same-card approval for source/docs/spec/test-only scope", dry_run, validation, scope, "would_complete")

    if verdict == "CHANGES_REQUESTED":
        if scope == "operator_gated":
            return make_decision(candidate, verdict, "needs_operator", scope_reason, dry_run, validation, scope, "would_comment")
        if validation not in {"same-card"} or comment_mentions_other_card(candidate):
            return make_decision(candidate, verdict, "needs_pm", f"target validation {validation}", dry_run, validation, "ambiguous", "would_comment")
        finding = first_finding_excerpt(candidate.latest_comment_body)
        return make_decision(candidate, verdict, "unblock_rework", f"same-card changes requested verdict; blocking finding: {finding}", dry_run, validation, scope, "would_unblock")

    if verdict == "REJECT":
        if scope == "operator_gated":
            return make_decision(candidate, verdict, "rejected", "REJECTED by reviewer with A3/operator-gated scope; blocked for Frank/PM triage", dry_run, validation, scope, "would_comment")
        return make_decision(candidate, verdict, "rejected", "REJECTED by reviewer on standard scope; blocked for PM triage", dry_run, validation, scope, "would_comment")

    return make_decision(candidate, verdict, "needs_pm", "ambiguous or malformed verdict", dry_run, validation, "ambiguous", "would_comment")


def shell_cmd(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("HERMES_PROFILE", AUTHOR)
    env.setdefault("HERMES_PROFILE_NAME", AUTHOR)
    return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)


def board_cli_prefix(board: str) -> list[str]:
    return [HERMES_BIN, "kanban", "--board", board]


def add_comment(board: str, task_id: str, body: str) -> subprocess.CompletedProcess[str]:
    return run_cli(board_cli_prefix(board) + ["comment", task_id, body, "--author", AUTHOR])


def perform(decision: Decision, candidate: Candidate) -> tuple[str | None, subprocess.CompletedProcess[str] | None]:
    if decision.action == "skip":
        return "idempotent-skip", None
    marker = decision.idempotency_key
    con = sqlite3.connect(str(candidate.board.db))
    con.row_factory = sqlite3.Row
    try:
        if prior_router_marker(con, candidate.task_id, marker):
            return "already-marked", None
    finally:
        con.close()

    evidence = (
        f"verdict-router marker={marker}\n"
        f"verdict_value={decision.verdict}; latest_comment_id={decision.comment_id}; "
        f"review_comment_author={candidate.latest_comment_author}; reason={decision.reason}\n"
    )
    if decision.action == "complete":
        summary = (
            f"ACCEPTED by deterministic verdict-router: REVIEW_VERDICT={decision.verdict} "
            f"on comment {decision.comment_id}; {decision.reason}."
        )
        metadata = json.dumps(
            {
                "accepted_by": AUTHOR,
                "review_comment_id": decision.comment_id,
                "review_comment_author": candidate.latest_comment_author,
                "verdict": decision.verdict,
                "idempotency_key": marker,
                "safety_scope": decision.reason,
            },
            sort_keys=True,
        )
        args = board_cli_prefix(decision.board) + ["complete", decision.task_id, "--summary", summary, "--metadata", metadata]
        return shell_cmd(args), run_cli(args)
    if decision.action == "unblock_rework":
        finding = first_finding_excerpt(candidate.latest_comment_body)
        comment = (
            "verdict-router: REWORK_REQUIRED\n"
            f"source_comment_id={decision.comment_id} source_author={candidate.latest_comment_author} verdict_value=CHANGES_REQUESTED\n"
            f"blocking_finding={finding}\n"
            "Address the finding, then block again as review-required when ready.\n"
            f"idempotency_key={marker}"
        )
        args = board_cli_prefix(decision.board) + ["unblock", decision.task_id, "--reason", comment]
        return shell_cmd(args), run_cli(args)
    if decision.action == "needs_operator":
        comment = (
            "NEEDS-OPERATOR: verdict-router refused to auto-complete this approved card.\n"
            f"{evidence}"
            "Reason: scope appears deploy/runtime/DB/live/A3/operator-gated or otherwise outside source/docs/spec auto-complete."
        )
        args = board_cli_prefix(decision.board) + ["comment", decision.task_id, comment, "--author", AUTHOR]
        return shell_cmd(args), run_cli(args)
    if decision.action == "rejected":
        comment = (
            "REJECTED: verdict-router processed REVIEW_VERDICT=REJECT.\n"
            f"{evidence}"
        )
        if decision.scope_class == "operator_gated":
            comment += "\nA3-REJECT: operator-gated scope — needs Frank/PM triage."
        args = board_cli_prefix(decision.board) + ["comment", decision.task_id, comment, "--author", AUTHOR]
        return shell_cmd(args), run_cli(args)
    comment = (
        "NEEDS-PM: verdict-router left this review verdict blocked for manual routing.\n"
        f"{evidence}"
        "Reason: ambiguous verdict or target/scope failed closed."
    )
    args = board_cli_prefix(decision.board) + ["comment", decision.task_id, comment, "--author", AUTHOR]
    return shell_cmd(args), run_cli(args)


def acquire_lock() -> int | None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, f"{os.getpid()} {utc_now()}\n".encode("utf-8"))
        return fd
    except FileExistsError:
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
        except OSError:
            age = 0
        if age > 900:
            LOCK_PATH.unlink(missing_ok=True)
            return acquire_lock()
        return None


def release_lock(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)
        LOCK_PATH.unlink(missing_ok=True)


def apply_enabled(cli_apply: bool, cli_dry_run: bool) -> bool:
    if cli_dry_run:
        return False
    if cli_apply:
        return True
    return os.environ.get("VERDICT_ROUTER_APPLY") == "1" or ENABLE_SENTINEL.exists()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route latest kanban REVIEW_VERDICT comments deterministically")
    parser.add_argument("--apply", action="store_true", help="Mutate board state; otherwise dry-run unless sentinel/env enables apply")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run even if sentinel/env enables apply")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(list(argv) if argv is not None else None)

    fd = acquire_lock()
    if fd is None:
        append_run_log("lock-held; skipping")
        return 0
    try:
        dry_run = not apply_enabled(args.apply, args.dry_run)
        mode = "dry-run" if dry_run else "apply"
        decisions: list[Decision] = []
        failures: list[dict] = []
        for board in boards():
            try:
                cands = candidates_for_board(board)
            except Exception as exc:
                failures.append({"board": board.slug, "error": str(exc)})
                append_note(
                    {
                        "timestamp": utc_now(),
                        "mode": mode,
                        "board": board.slug,
                        "task_id": "__board_scan_error__",
                        "action": "scan_error",
                        "verdict": "ERROR",
                        "comment_id": 0,
                        "idempotency_key": f"verdict-router:{board.slug}:scan-error:{int(time.time())}",
                        "reason": str(exc),
                    }
                )
                continue
            for cand in cands:
                decision = decide(cand, dry_run)
                con = sqlite3.connect(str(cand.board.db))
                try:
                    if prior_router_marker(con, cand.task_id, decision.idempotency_key):
                        decision = mark_idempotent(decision)
                finally:
                    con.close()
                decisions.append(decision)
                entry = {
                    "timestamp": utc_now(),
                    "mode": mode,
                    "board": decision.board,
                    "task_id": decision.task_id,
                    "action": decision.action,
                    "verdict": decision.verdict,
                    "verdict_value": decision.verdict,
                    "comment_id": decision.comment_id,
                    "source_comment_id": decision.comment_id,
                    "source_author": decision.source_author,
                    "target_validation": decision.target_validation,
                    "scope_class": decision.scope_class,
                    "result": decision.result,
                    "idempotency_key": decision.idempotency_key,
                    "reason": decision.reason,
                }
                if dry_run:
                    append_note(entry)
                    continue
                command, proc = perform(decision, cand)
                entry["command"] = command or "idempotent-skip"
                if proc is not None:
                    entry["stdout"] = (proc.stdout or "").strip()
                    entry["stderr"] = (proc.stderr or "").strip()
                    if proc.returncode != 0:
                        entry["action"] = f"{decision.action}_failed"
                        failures.append({"board": decision.board, "task_id": decision.task_id, "rc": proc.returncode, "stderr": proc.stderr})
                append_note(entry)

        summary = {
            "mode": mode,
            "boards_scanned": len(boards()),
            "decisions": [d.__dict__ | {"idempotency_key": d.idempotency_key} for d in decisions],
            "failures": failures,
            "note": str(today_note()),
        }
        append_run_log(json.dumps({"mode": mode, "decisions": len(decisions), "failures": len(failures), "note": str(today_note())}, sort_keys=True))
        if args.json:
            print(json.dumps(summary, sort_keys=True))
        elif failures:
            print(f"verdict-router: {len(failures)} failure(s); note={today_note()}")
        elif decisions:
            print(f"verdict-router: {len(decisions)} {mode} decision(s); note={today_note()}")
        # Empty stdout means silent no-op for no-agent cron.
        return 1 if failures else 0
    finally:
        release_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
