#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Deterministic kanban REVIEW_VERDICT router.

Dry-run by default. In dry-run it only appends shadow decisions to the fleet
Obsidian vault. Mutations require either VERDICT_ROUTER_APPLY=1 or the sentinel
/home/frank/.hermes/cron/state/verdict-router.apply-enabled.

This module also owns the deterministic risk-tier classifier used by review
routing. It consumes explicit changed paths and structured change flags only;
title prose and free-form keywords are deliberately not inputs to that policy.
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

# Kill 6 / W7: stable reason codes for the standalone risk-review boundary.
RISK_REASON_CODES = (
    "money",
    "live_execution",
    "access_material",
    "ddl_or_irreversible_data",
    "measurement_write_path",
    "unknown_input",
)
_RISK_FLAGS = frozenset(RISK_REASON_CODES) | {"paper_only", "refactor", "research", "tests", "docs"}
_RISK_PATH_RULES = (
    ("money", re.compile(r"(?:^|/)(?:billing|payments?|checkout|refund|pricing|spend|money)(?:/|$)|(?:^|/).*?(?:payment|billing|checkout|refund|pricing|spend).*", re.I)),
    ("live_execution", re.compile(r"(?:^|/)(?:orders?|positions?|trade[_-]?intents?|execution|autotrader|live[-_ ]?trading)(?:/|$)|(?:^|/).*?(?:order|position|execution|trade[_-]?intent|live[-_ ]?trading).*", re.I)),
    ("access_material", re.compile(r"(?:^|/)(?:credentials?|secrets?|auth|oauth|tokens?)(?:/|$)|(?:^|/).*?(?:credential|secret|auth|oauth|token|\.env(?:\.|$)).*", re.I)),
    ("ddl_or_irreversible_data", re.compile(r"(?:^|/)(?:supabase/migrations?|drizzle/migrations?|migrations?|schema)(?:/|$)|(?:^|/).*?(?:migration|schema|ddl|backfill|drop|truncate|mass[-_ ]?(?:delete|update)).*", re.I)),
    ("measurement_write_path", re.compile(r"(?:^|/).*?(?:label|outcome|metric|measurement|evaluation|reward|ledger|signal[_-]?journey).*", re.I)),
)
_SAFE_PATH_RE = re.compile(r"^(?:docs?/|research/|tests?/|.*(?:^|/)(?:__tests__|fixtures?)(?:/|$)|.*\.(?:md|mdx|txt|rst|test\.[cm]?[jt]sx?|spec\.[cm]?[jt]sx?))$", re.I)


@dataclass(frozen=True)
class RiskClassification:
    """Fail-closed classification of an explicit change manifest."""

    requires_standalone_risk_review: bool
    matched_reasons: tuple[str, ...]
    fail_closed: bool


def classify_risk(changed_paths: object, change_flags: object) -> RiskClassification:
    """Classify a change using paths and structured flags, never title prose.

    Missing, malformed, unknown, or contradictory manifests fail closed. Safe
    paths/flags are intentionally narrow: an unrecognised path is not approval.
    Matching is case-insensitive and path separators are canonicalised so case
    variants and generated-path tricks cannot evade a rule.
    """
    reasons: set[str] = set()
    fail_closed = False
    if not isinstance(changed_paths, (list, tuple)) or not changed_paths:
        return RiskClassification(True, ("unknown_input",), True)
    normalised_paths: list[str] = []
    for raw_path in changed_paths:
        if not isinstance(raw_path, str) or not raw_path.strip():
            fail_closed = True
            continue
        # Validate the RAW path for absolute/traversal forms BEFORE any
        # stripping. Checking only after ".lstrip"-style normalization let an
        # absolute path (leading "/") or a traversal segment ("..") slip
        # through once the leading "./" (or, on a naive strip, "/"/".." too)
        # was removed — a fail-open bypass a reviewer reproduced independently
        # (classify_risk(['../docs/x'], ...), ['/docs/x'], ['..\\docs\\x']).
        stripped = raw_path.strip()
        raw_slash = stripped.replace("\\", "/")
        if raw_slash.startswith("/") or ".." in raw_slash.split("/"):
            reasons.add("unknown_input")
            fail_closed = True
            continue
        # Strip only a leading "./" prefix (not arbitrary dots/slashes)
        path = raw_slash
        if path.startswith("./"):
            path = path[2:]
        path = path.casefold()
        if not path or path.startswith("/") or ".." in path.split("/"):
            reasons.add("unknown_input")
            fail_closed = True
            continue
        normalised_paths.append(path)
        matched = False
        for code, rule in _RISK_PATH_RULES:
            if rule.search(path):
                reasons.add(code)
                matched = True
        if not matched and not _SAFE_PATH_RE.match(path):
            # A structured safe classification (docs/research/tests/refactor)
            # may cover a normal source path; an absent flag may not.
            reasons.add("unknown_input")
            fail_closed = True

    if not isinstance(change_flags, dict):
        return RiskClassification(True, tuple(sorted(reasons | {"unknown_input"})), True)
    unknown_flags = set(change_flags) - _RISK_FLAGS
    if unknown_flags:
        reasons.add("unknown_input")
        fail_closed = True
    # Explicit safe-kind flags are the only permitted classification for normal
    # source paths that are not themselves risk-bearing. They do not waive a
    # matched high-risk path, and malformed/absolute/traversal paths remain bad.
    safe_kind = any(change_flags.get(flag) is True for flag in ("refactor", "research", "tests", "docs"))
    if safe_kind and not unknown_flags and "unknown_input" in reasons and all(
        isinstance(path, str) and not path.strip().startswith(("/", "\\")) and ".." not in path.replace("\\", "/").split("/")
        for path in changed_paths
    ):
        reasons.discard("unknown_input")
        fail_closed = False
    for flag, value in change_flags.items():
        if not isinstance(value, bool):
            reasons.add("unknown_input")
            fail_closed = True
        elif value and flag in RISK_REASON_CODES:
            reasons.add(flag)
    if change_flags.get("paper_only") is True and reasons & (set(RISK_REASON_CODES) - {"unknown_input"}):
        # Paper-only is not a waiver for a risky path/flag; contradictory claims
        # are retained as a fail-closed input rather than silently downgraded.
        reasons.add("unknown_input")
        fail_closed = True
    if change_flags.get("paper_only") is not True and not any(
        change_flags.get(flag) is True for flag in ("money", "live_execution", "access_material", "ddl_or_irreversible_data", "measurement_write_path", "paper_only", "refactor", "research", "tests", "docs")
    ):
        reasons.add("unknown_input")
        fail_closed = True
    return RiskClassification(bool(reasons & set(RISK_REASON_CODES)), tuple(code for code in RISK_REASON_CODES if code in reasons), fail_closed)

# B1/B2 (adversarial write-path review 2026-08-02, task t_65a0c080): a REVIEW_VERDICT
# token may only auto-complete / auto-route when it is *issued* by an authorized
# reviewer, AND that reviewer is not the card's own assignee.
#
# Evidence of the gap:
#   - t_089b30e1: completed off comment 12295 by `trading-strategy-dev` (a worker,
#     not the reviewer) that *quoted* trading-risk-reviewer's REVIEW_VERDICT=APPROVED.
#   - t_6164e58b: completed off comment 14256 by `builder` -- the card's OWN
#     assignee -- asserting REVIEW_VERDICT=APPROVED (self-certification).
# Both are genuine B1/B2 failures and were wrongly closed by apply-mode (2026-07-14).
#
# REVIEWER_ALLOWLIST: terminal-capable review seats that may issue a verdict the
# router acts on. Keep this in lockstep with kanban_dedupe_guard.review_assignee_block_reason.
REVIEWER_ALLOWLIST = frozenset({
    "os-reviewer",
    "trading-risk-reviewer",
    "guardian",
    "platform-reviewer",
    "devops",          # terminal-capable reviewer seat per fleet governance
    "jarvis",
    "elon",
    "elon-governor",
    "jarvis-voice",
})

# A verdict is *attributed* (quoted) rather than *issued* when the same sentence /
# phrase names another author as the verdict source (e.g. "by os-reviewer",
# "trading-risk-reviewer APPROVED", "per reviewer: REVIEW_VERDICT=APPROVED"). An
# attributed verdict is NOT a verdict this comment author issued, so it must not
# auto-route (B1). Fail closed: if attribution cannot be excluded, treat as quoted.
_ATTRIBUTION_RE = re.compile(
    r"\b(by|per|from|according to|quoting|via|as (?:noted|stated|reported) by)\s+"
    r"([A-Za-z0-9_\-]+(?:[ .][A-Za-z0-9_\-]+)*)\b",
    re.I,
)
# Short allowlist of author-like tokens that, when followed by a verdict in the
# SAME clause, indicate the comment author is merely *relaying* another seat's
# verdict. If the named author is not the comment author, the verdict is quoted.
_RELAY_HINT_RE = re.compile(
    r"\b(trading-risk-reviewer|os-reviewer|guardian|platform-reviewer|trading-devops|"
    r"builder|nervous-system-engineer|sycode-trading-pm|jarvis-os-pm|worker|trading-strategy-dev)\b",
    re.I,
)


def verdict_is_attributed(text: str, author: str, start: int, end: int) -> bool:
    """True if the verdict token [start:end] is *attributed to another author*
    rather than issued by ``author`` (B1). We look at a bounded window around the
    verdict for relay phrasing that names a different seat.
    """
    window_start = max(0, start - 240)
    window_end = min(len(text), end + 240)
    window = text[window_start:window_end]
    # Direct relay: "by <other>" / "per <other>" where <other> != author.
    for m in _ATTRIBUTION_RE.finditer(window):
        named = m.group(2).strip().lower()
        if named and named != (author or "").lower():
            return True
    # Bare mention of another reviewer seat adjacent to the verdict phrasing in the
    # same window (e.g. "trading-risk-reviewer REVIEW_VERDICT=APPROVED" written by a
    # third party) without the comment author themselves being that seat.
    author_is_reviewer_seat = _RELAY_HINT_RE.match((author or "").lower()) is not None
    if not author_is_reviewer_seat:
        # author is not one of the named reviewer seats; if the window names a
        # reviewer seat that the comment author is NOT, it is an attribution.
        for m in _RELAY_HINT_RE.finditer(window):
            if m.group(0).lower() != (author or "").lower():
                return True
    return False

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
    r"\bno\b|\bnot\b|\bwithout\b|\bdo\s+not\b|\bdon'?t\b|\bnever\b|\bnone\b|\bneither\b|\babsent\b|\bmissing\b|"
    r"\bunissued\b|\bdid\s+not\b|\bdidn'?t\b|\bwasn'?t\b|\bweren'?t\b|\bisn'?t\b|\baren'?t\b|"
    r"\bno[- ]?verdict\b|\bno[- ]?review[- ]?verdict\b|\bvoid\b|\bvoided\b|\black\s+of\b|"
    r"\bdecline[ds]?\b|\bwithdrawn\b|\bnot\s+post\b|\bdo\s+not\s+post\b|\bnever\s+post\b"
)
VERDICT_NEGATION_CUE_RE = re.compile(r"(?:" + _VERDICT_NEGATION_CUES + r")", re.I)
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

# B3 (t_65a0c080): restrict scan scope to real task boards. The previous scan
# enumerated EVERY `*/kanban.db` under BOARDS_DIR, including junk/scratch/backup
# boards (testproj, skilldedupe, supero, .bak_* restore snapshots, ...). Combined
# with the dead review-required gate this put every blocked card on every board in
# router scope. We now scan only the allowlisted task boards plus the canonical
# default DB. Backup (`_bak_*`) and underscore-prefixed dirs are always excluded.
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
    r"no[- ]?op|non[- ]?gated|out[- ]?of[- ]?scope|"
    # B-major (t_65a0c080): 'frank-gated' is itself a denial cue -- it means the
    # action is GATED and requires Frank, i.e. it must NOT auto-complete. The old
    # cue list treated 'frank-gated' as a safe/non-gated phrase, so a comment like
    # 'prod deploy - Frank-gated' bypassed the operator gate. 'frank-gated' /
    # 'frank approval' / 'awaiting frank' are now explicit gate-affirming phrases.
    r"frank[- ]?gated|frank[- ]?approval|awaiting[- ]?frank|needs[- ]?frank|"
    r"operator[- ]?gated|operator[- ]?approval|awaiting[- ]?operator|needs[- ]?operator|"
    r"a3[- ]?gated|a3[- ]?approval"
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


# C2 (t_c3bbc27b): structured block_kind values that already record an
# operator/credential/A3/prod/DB/access wall. A task blocked with one of these
# kinds is, by the system's own recorded state, outside source/docs/spec auto-
# complete scope — so the operator-gate detector gates on it directly without
# any lexical FORBIDDEN_SCOPE_RE match. This is gating on structured fields,
# never on reviewer comment prose.
_OPERATOR_GATE_BLOCK_KINDS = frozenset({"capability"})


def operator_gate_terms(
    title: str,
    body: str,
    block_reason: str | None = None,
    block_kind: str | None = None,
) -> "re.Match[str] | None":
    """Operator-gated detection (C2 fix — t_8874b97b / proposal t_9a0af491 / t_c3bbc27b).

    Gate ONLY on the task's OWN scope — title/body, the documented block reason,
    and the structured block_kind — NEVER on reviewer comment prose. The
    proposal's corrected C2 mechanism explicitly excludes reviewer comment prose
    because a verdict that *denies* a gate must not strand an approved card.

    Scoping surfaces, in order:
      1) block_kind == "capability": the system already recorded a hard wall
         (missing credentials, access, or an action no agent can perform), which
         is outside auto-complete scope by definition — gate immediately.
      2) block_reason: the documented block reason text (task_events payload),
         scanned with FORBIDDEN_SCOPE_RE. Genuine gate reasons ("operator
         decision", "credential", "prod") gate; gate-DENIAL narration inside the
         reason is redacted so a "no prod/cred change" reason cannot strand a
         card whose body/title already cleared.
      3) title/body: scanned with FORBIDDEN_SCOPE_RE + gate-DENIAL redaction.

    Within surfaces 2 and 3, gate-DENIAL narration (a forbidden noun in a
    sentence containing a denial cue like "no"/"not"/"do not"/"safe"/"A3-safe"/
    "preserved") is redacted so it cannot trip the detector. A *positive* gate
    assertion ("Run production DB migration and live runtime deploy") still gates.
    This is general across all ~25 terms (not the rejected brittle 3-term patch).
    Over-broad tokens that collide with code identifiers (database/auth/schema)
    are scoped to gate-context phrases.
    """
    # C2 (t_c3bbc27b): structured gate field — block_kind already records the
    # operator/credential/A3/prod/DB wall, so gate without lexical matching.
    if block_kind in _OPERATOR_GATE_BLOCK_KINDS:
        # Return a synthetic match whose group(0) names the structured field so
        # the caller's reason string is descriptive and auditable.
        class _StructMatch:
            @staticmethod
            def group(_: int = 0) -> str:
                return f"block_kind={block_kind}"
        return _StructMatch()  # type: ignore[return-value]
    # C2: reviewer comment prose is excluded from the operator-gate scan. Only
    # the task's own title/body and documented block_reason are scanned.
    surfaces: list[str] = [title or "", body or ""]
    if block_reason:
        surfaces.append(block_reason)
    combined = "\n".join(surfaces)
    return FORBIDDEN_SCOPE_RE.search(redact_gate_denials(combined))

SAFE_DELIVERABLE_RE = re.compile(
    r"\b(source|code|patch|diff|docs?|documentation|spec|test(?:s|ing)?|fixture|lint|typecheck|unit|build)\b",
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
    # C2 (t_c3bbc27b): structured gate signals surfaced from the task's own
    # fields (NOT reviewer comment prose) so the operator-gate detector can gate
    # on the documented block reason and the structured block_kind.
    block_reason: str | None = None
    block_kind: str | None = None


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


def open_db_ro(db: Path) -> sqlite3.Connection:
    """Open a board DB for read-only scanning WITHOUT the immutable=1 flag.

    B-major (t_65a0c080): immutable=1 makes SQLite ignore the -wal file and read a
    potentially STALE snapshot of a live board (the kanban kernel writes via WAL).
    A router scan on an immutable connection could miss the latest comment/event
    and act on stale state. mode=ro reads the live WAL checkpoint instead. We do
    not use read-only WAL locking to avoid blocking the live writer; a brief
    reader is safe and the router is read-mostly.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def boards() -> list[Board]:
    found: list[Board] = []
    if DEFAULT_DB.exists():
        found.append(Board("default", DEFAULT_DB))
    if BOARDS_DIR.exists():
        for db in sorted(BOARDS_DIR.glob("*/kanban.db")):
            slug = db.parent.name
            if slug.startswith("_"):
                continue
            # B3: exclude backup/restore snapshots and anything not in the
            # allowlisted set of real task boards.
            if slug.startswith(".bak") or slug in EXCLUDED_BOARDS:
                continue
            if slug not in BOARD_ALLOWLIST:
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
    con = open_db_ro(board.db)
    try:
        rows = con.execute(
            """
            SELECT id, title, body, assignee
                   , block_kind
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
            # B3 (t_65a0c080): the review-required gate was dead -- the third
            # disjunct `verdict_declarations(body)` is always truthy whenever we
            # reach here, so EVERY blocked card on EVERY board (incl. junk boards)
            # was in scope. The real gate: a card enters the routing pipeline only
            # when it is in review-required scope. "Review-required scope" means
            # the task's own title/body carries the review-required marker (a
            # genuine code-review handoff), NOT merely that some comment contains a
            # verdict token (that is already required above). We also restrict the
            # board list (see BOARD_ALLOWLIST), so junk/coordination boards never
            # reach this point.
            if not (REVIEW_REQUIRED_RE.search(task_text) or REVIEW_REQUIRED_RE.search(body)):
                continue
            # C2 (t_c3bbc27b): surface the task's structured gate signals so the
            # operator-gate detector can gate on them WITHOUT scanning reviewer
            # comment prose. block_kind is a first-class task field; block_reason
            # is the documented reason stored in the latest `blocked` task_event.
            block_reason = latest_block_reason(con, row["id"])
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
                    block_reason=block_reason,
                    block_kind=row["block_kind"],
                )
            )
        return out
    finally:
        con.close()


def latest_block_reason(con: sqlite3.Connection, task_id: str) -> str | None:
    """Return the documented block reason for ``task_id`` (C2, t_c3bbc27b).

    Block reasons are stored as JSON in ``task_events.payload.reason`` for rows
    with ``kind='blocked'`` — the same field the ``kanban_block`` tool writes.
    We return the most recent blocked-event reason so the operator-gate detector
    can gate on it (gating on the system's own recorded scope, never on a
    reviewer's prose). Returns ``None`` when no blocked event exists, so callers
    fail closed (no reason => no lexical match from this surface). Resilient to
    malformed payload JSON and absent ``task_events`` table.
    """
    if not table_exists(con, "task_events"):
        return None
    try:
        row = con.execute(
            """
            SELECT payload
              FROM task_events
             WHERE task_id=?
               AND kind='blocked'
             ORDER BY id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    payload = row["payload"]
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) else None


def parse_verdict(comment_body: str, *, author: str | None = None, assignee: str | None = None) -> str | None:
    # C4 fix (t_c996e275): only *affirmative* verdict declarations count. A
    # negated / no-verdict token (e.g. "No REVIEW_VERDICT=APPROVED") is a denial
    # and must not route as a verdict. Fail closed: 0 or >1 affirmative tokens
    # => None (ambiguous / no verdict).
    #
    # B1/B2 (t_65a0c080): the verdict must be *issued* by an authorized reviewer
    # who is not the card's own assignee. When ``author``/``assignee`` are given:
    #   - the comment author must be in REVIEWER_ALLOWLIST (authorized reviewer),
    #   - the author must not equal the card assignee (no self-certification),
    #   - the verdict token must not be *attributed* to a different seat (quoted).
    # If any of these fail, return None (the router must not act on it). Fail
    # closed: missing author/assignee => None (cannot establish attribution).
    matches = verdict_declarations(comment_body)
    if len(matches) != 1:
        return None
    m = matches[0]
    raw = m.group(1).strip().upper()
    verdict: str | None
    if raw == "APPROVE":
        verdict = "APPROVED"
    elif raw in {"REJECT", "REJECTED"}:
        verdict = "REJECT"
    elif raw in VALID_VERDICTS:
        verdict = raw
    else:
        verdict = raw[:80] or "AMBIGUOUS"

    if author is not None or assignee is not None:
        if not _verdict_issuable_by(comment_body, m.start(), m.end(), author, assignee):
            return None
    return verdict


def _verdict_issuable_by(text: str, start: int, end: int, author: str | None, assignee: str | None) -> bool:
    """B1/B2 gate: is this verdict token a verdict *issued* (not quoted) by an
    authorized reviewer who is not the card's assignee? Fail closed => False.
    """
    if author is None:
        return False
    if author.lower() not in REVIEWER_ALLOWLIST:
        return False
    if assignee is not None and author.lower() == assignee.lower():
        # self-certification: a worker approving its own card.
        return False
    if verdict_is_attributed(text, author, start, end):
        # quoted verdict from another seat; this comment did not issue it.
        return False
    return True


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
    # C2 fix (t_8874b97b / t_c3bbc27b): operator-gate detection scans the
    # task's OWN scope only — title/body, the documented block reason, and the
    # structured block_kind — NEVER reviewer comment prose. Within each surface
    # it uses negation-aware redaction so gate-DENIAL narration ("no prod, no
    # creds", "A3-safe", "schema.prisma" file path) cannot strand a card. The
    # block reason / block_kind are first-class task fields (set by
    # kanban_block / the dispatcher), so gating on them is gating on the
    # operator/credential/A3/prod/DB scope the system already recorded — not on
    # a reviewer's prose. See operator_gate_terms.
    block_reason = candidate.block_reason
    block_kind = candidate.block_kind
    forbidden = operator_gate_terms(
        candidate.title, candidate.body, block_reason=block_reason, block_kind=block_kind
    )
    if forbidden:
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
    verdict = parse_verdict(
        candidate.latest_comment_body,
        author=candidate.latest_comment_author,
        assignee=candidate.assignee,
    )
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
    return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, env=env)


def board_cli_prefix(board: str) -> list[str]:
    return [HERMES_BIN, "kanban", "--board", board]


def add_comment(board: str, task_id: str, body: str) -> subprocess.CompletedProcess[str]:
    return run_cli(board_cli_prefix(board) + ["comment", task_id, body, "--author", AUTHOR])


def perform(decision: Decision, candidate: Candidate) -> tuple[str | None, subprocess.CompletedProcess[str] | None]:
    if decision.action == "skip":
        return "idempotent-skip", None
    marker = decision.idempotency_key
    con = open_db_ro(candidate.board.db)
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
        # B4 (t_65a0c080): also persist the completion marker as a task_comments
        # row. The idempotency check (prior_router_marker) already scans comments
        # for this marker, but apply-mode historically wrote it ONLY to
        # --summary/--metadata (verified: 0 marker rows vs ~15 apply-era
        # completes), so a reopened+re-blocked card re-completed off a stale
        # APPROVE. Writing the comment closes that gap: re-runs see the marker and
        # skip (see mark_idempotent / prior_router_marker).
        marker_comment = (
            f"verdict-router COMPLETE marker={marker}\n"
            f"verdict_value={decision.verdict}; completed_comment_id={decision.comment_id}; "
            f"review_comment_author={candidate.latest_comment_author}; reason={decision.reason}\n"
            "This row is the durable router-completion signal; do not remove."
        )
        args = board_cli_prefix(decision.board) + [
            "complete", decision.task_id,
            "--summary", summary,
            "--metadata", metadata,
            "--comment", marker_comment,
            "--author", AUTHOR,
        ]
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


def _pid_alive(pid: int) -> bool:
    """Best-effort check that a PID is still running (cross-platform)."""
    if pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(int(pid)))
    except Exception:
        pass
    # Last-resort fallback if gateway.status is unavailable: psutil directly.
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        return False


def acquire_lock() -> int | None:
    """Acquire a single-instance lock.

    B-major (t_65a0c080): the prior stale-lock break was a TOCTOU -- two cron
    instances could both observe an aged-out lock, both call unlink, both
    re-create, and then BOTH run concurrently (double-apply). We now:
      1) re-create atomically with O_EXCL after the unlink (so only one wins),
      2) only break a stale lock if its recorded PID is NOT still alive,
      3) bound the retry loop so a live competing holder wins cleanly.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()} {utc_now()}\n".encode("utf-8"))
            return fd
        except FileExistsError:
            # Read the recorded owner PID (if any) before deciding to break.
            try:
                with open(str(LOCK_PATH), "r", encoding="utf-8") as f:
                    owner_line = f.readline().split()[0]
                owner_pid = int(owner_line)
            except (OSError, ValueError, IndexError):
                owner_pid = -1
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except OSError:
                age = 0
            # Only break when the lock is old AND the recorded owner is no longer
            # running. A live owner (even an aged lock) means another instance is
            # still working -- yield to it, do not double-apply.
            if age > 900 and not _pid_alive(owner_pid):
                try:
                    LOCK_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
                continue  # re-loop: the next O_EXCL attempt is atomic
            return None
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
                con = open_db_ro(cand.board.db)
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
