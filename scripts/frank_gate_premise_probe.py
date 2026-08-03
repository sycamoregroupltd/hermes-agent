#!/usr/bin/env python3
"""Falsified-frank_gate premise-activity probe (jarvis-os/t_e911f789).

Problem this closes
-------------------
A ``blocked`` card with ``block_kind='frank_gate'`` sits on Frank's decision
surface until a human closes it. When the card's stated ROOT CAUSE is a
*factual claim* ("gateway X is stopped", "card Y is crashed", "probe Z says
BLOCK") and that claim is later contradicted by observed board activity, the
card is dead weight: the decision it asks for is moot. Nothing retires it.

Reference incident: jarvis-os/t_4776f5c9 ("start trading-risk-reviewer gateway
— sole cause of fleet BLOCK", raised 2026-08-02 15:59:58 UTC). The cited-dead
reviewer completed 20+ sycode-trading tasks in the 24h AFTER the raise. The
card was retired by hand (t_6aa6dabc); this probe is the automated path.

Design contract
---------------
* READ-ONLY by default. ``--apply-comments`` (comment only) and
  ``--apply-retire`` (status mutation) each require ``--authorized-by``.
* Premises are extracted from the card's own TITLE + BODY only, never from the
  comment thread — the thread is where the *disproof* lives, and reading it as
  premise would be circular.
* A card is RETIRE-eligible only when it has >=1 extractable falsifiable
  premise AND every extracted premise is contradicted. Cards with no
  falsifiable premise (pure authorization requests: deploy, chmod, credential,
  merge) are HOLD — they are Frank's judgement, not a fact, and can never be
  falsified by activity.
* An authorization request that is INDEPENDENT of the falsified subject also
  forces HOLD. Sentences naming a falsified subject are stripped before the
  authorization scan, because "start the gateway that is down" is moot once
  "the gateway is down" is false.
* Written comments must never contain the reviewer-approval token: core
  ``kanban_db.apply_approvals()`` LIKE-matches that token ANYWHERE in ANY
  comment body and would auto-unblock the card ~100s later. Enforced by
  :func:`assert_no_approval_token`.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BOARDS_DIR = Path("/home/frank/.hermes/kanban/boards")
PROFILES_DIR = Path("/home/frank/.hermes/profiles")
TARGET_BOARDS = ("jarvis-os", "sycode-trading")
AUTHOR = "frank-gate-premise-probe"
MARKER = "frank-gate-premise-probe:v1:t_e911f789"
RETIRED_MARKER = "RETIRED-STALE"
DEFAULT_MIN_COMPLETIONS = 3

TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{6,12}\b")
PROBE_ID_RE = re.compile(r"\b([a-z][a-z0-9-]*-probe(?:-[0-9a-f]{6,12})?)\b", re.I)
SENTENCE_RE = re.compile(r"[^.!?\n]+")

# "the thing is not running" cues
DOWN_CUE_RE = re.compile(
    r"\b(stopped|down|not running|offline|dead|unavailable|inactive|halted|"
    r"never (?:ran|started)|failed to start|no gateway|gateway missing)\b",
    re.I,
)
# "the card is not progressing" cues
STUCK_CUE_RE = re.compile(
    r"\b(crashed|stuck|stalled|limbo|blocked|hung|rotting|not progress\w*|"
    r"no progress|abandoned|orphan\w*)\b",
    re.I,
)
VERDICT_BLOCK_RE = re.compile(r"VERDICT\s*[:=]?\s*BLOCK\b", re.I)
VERDICT_CLEAR_RE = re.compile(r"VERDICT\s*[:=]?\s*(PASS|GREEN|WARN|OK)\b", re.I)

# Clause boundaries. A down-cue in a LATER clause must not attach to a profile
# named in an EARLIER one: "only jarvis, jarvis-voice are running; every
# reviewer gateway is stopped" claims nothing about jarvis being down.
# NOTE: ':' is deliberately NOT a boundary — it would split "VERDICT: BLOCK"
# and silently destroy probe-block extraction.
CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|->|→|\|)\s*")
# How far a down-cue may sit from the profile mention and still be about it.
DOWN_CUE_FORWARD_WINDOW = 40
DOWN_CUE_BACKWARD_WINDOW = 25

# Unconditional authorization requests — Frank's judgement, not a fact.
AUTHORIZATION_RE = re.compile(
    r"\b(requires? (?:explicit )?(?:frank|a3|human|operator)|needs? frank|"
    r"pending frank|await(?:ing)? frank|frank[- /]?a3|frank exact approval|"
    r"explicit authorization|operator apply|guardian[- ]apply|deploy[- ]gated|"
    r"merge (?:pr|the pr)|production deploy|prod deploy|install/repoint|"
    r"repoint|chmod|credential|secret|api key|live[- ]trading|"
    r"ddl|drop table|truncate table|irreversible)\b",
    re.I,
)
# Denial prose ("no credentials, no live trading") must not create a gate.
DENIAL_CUE_RE = re.compile(
    r"\b(no|not|without|do not|don't|never|excluded|exclude[sd]?|safe|"
    r"scope excludes|out of scope|unchanged|read[- ]only|paper[- ]only)\b",
    re.I,
)
# Tokens core apply_approvals() LIKE-matches. Never emit these in a comment.
APPROVAL_TOKEN_RE = re.compile(r"REVIEW_VERDICT\s*[:=]\s*APPROVED", re.I)


class ApprovalTokenLeak(RuntimeError):
    """Raised when generated comment text would trip apply_approvals()."""


def assert_no_approval_token(body: str) -> str:
    """Guard: a probe comment must never carry the auto-unblock token.

    ``kanban_db.apply_approvals()`` LIKE-matches ``REVIEW_VERDICT=APPROVED``
    anywhere in any comment on the card, with no author or block_kind check.
    Emitting it in prose would auto-unblock the very gate we are auditing.
    """
    if APPROVAL_TOKEN_RE.search(body):
        raise ApprovalTokenLeak(
            "generated comment contains the reviewer-approval token; "
            "apply_approvals() would auto-unblock this card"
        )
    return body


# --------------------------------------------------------------------------
# board access (read-only unless explicitly opened writable)
# --------------------------------------------------------------------------


def board_dbs(boards_dir: Path = BOARDS_DIR) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for db in sorted(boards_dir.glob("*/kanban.db")):
        out[db.parent.name] = db
    return out


def connect_ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def connect_rw(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _rows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    last: sqlite3.OperationalError | None = None
    for _ in range(5):
        try:
            return list(con.execute(sql, params).fetchall())
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
            time.sleep(0.4)
    assert last is not None
    raise last


def known_profiles(profiles_dir: Path = PROFILES_DIR) -> set[str]:
    if not profiles_dir.is_dir():
        return set()
    return {p.name for p in profiles_dir.iterdir() if p.is_dir()}


def ts(epoch: int | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def epoch_of(value: Any) -> int:
    """Coerce a DB timestamp to int, tolerating dirty rows.

    Real corruption exists on the live boards: jarvis-os/t_7feb5d03 and
    jarvis-os/t_e84b71fe carry the literal string ``'%s'`` in ``completed_at``
    (an unparameterised INSERT from some writer). A probe that crashes on
    those rows is a probe that never runs, so coerce unparseable values to 0
    (= "no verifiable completion time") instead of raising.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _delta(later: int, earlier: int) -> str:
    secs = max(0, int(later) - int(earlier))
    return f"+{secs // 3600}h{(secs % 3600) // 60:02d}m"


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    board: str
    id: str
    title: str
    body: str
    assignee: str
    status: str
    block_kind: str
    created_at: int

    @property
    def premise_text(self) -> str:
        """Title + body only. The comment thread carries the disproof."""
        return f"{self.title}\n{self.body}"


@dataclass
class Premise:
    kind: str            # profile-down | task-stuck | probe-block
    subject: str
    claim: str
    # verdict is one of: falsified | standing | unverifiable
    #   falsified    — independent post-raise activity contradicts the claim
    #   standing     — evidence exists and is CONSISTENT with the claim still holding
    #   unverifiable — no independent evidence either way (e.g. the only
    #                  corroboration is the card's own thread, or the cited
    #                  subject does not exist on any board)
    verdict: str = "standing"
    evidence: list[str] = field(default_factory=list)

    @property
    def contradicted(self) -> bool:
        return self.verdict == "falsified"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "claim": self.claim,
            "verdict": self.verdict,
            "contradicted": self.contradicted,
            "evidence": list(self.evidence),
        }


def fetch_frank_gate_cards(
    boards: Iterable[str],
    boards_dir: Path = BOARDS_DIR,
    *,
    include_task_ids: Iterable[str] = (),
) -> list[Card]:
    """Blocked frank_gate cards on the named boards.

    ``include_task_ids`` additionally pulls named cards regardless of their
    current status/block_kind — used by ``--canary`` to replay a card that has
    since been retired by hand.
    """
    dbs = board_dbs(boards_dir)
    wanted = set(include_task_ids)
    out: list[Card] = []
    for board in boards:
        db = dbs.get(board)
        if db is None:
            continue
        with connect_ro(db) as con:
            rows = _rows(
                con,
                "SELECT id, title, COALESCE(body,'') body, COALESCE(assignee,'') assignee, "
                "status, COALESCE(block_kind,'') block_kind, COALESCE(created_at,0) created_at "
                "FROM tasks WHERE (status='blocked' AND block_kind='frank_gate') "
                "OR id IN (%s) ORDER BY created_at"
                % (",".join("?" * len(wanted)) or "NULL"),
                tuple(sorted(wanted)),
            )
        for r in rows:
            out.append(
                Card(
                    board=board,
                    id=str(r["id"]),
                    title=str(r["title"] or ""),
                    body=str(r["body"] or ""),
                    assignee=str(r["assignee"] or ""),
                    status=str(r["status"] or ""),
                    block_kind=str(r["block_kind"] or ""),
                    created_at=epoch_of(r["created_at"]),
                )
            )
    return out


# --------------------------------------------------------------------------
# premise extraction (title + body only)
# --------------------------------------------------------------------------


def _down_cue_near(clause: str, prof: str) -> re.Match[str] | None:
    """A down-cue that is plausibly ABOUT this profile, not merely co-located.

    Requires the cue to sit within a short window of the profile mention. Guards
    against the t_4776f5c9 shape: "only jarvis, jarvis-os-pm, jarvis-voice are
    running; every specialist/reviewer gateway is stopped" — 'stopped' is 60+
    chars away and belongs to a different subject entirely.
    """
    for m in re.finditer(rf"(?<![\w-]){re.escape(prof)}(?![\w-])", clause):
        lo = max(0, m.start() - DOWN_CUE_BACKWARD_WINDOW)
        hi = min(len(clause), m.end() + DOWN_CUE_FORWARD_WINDOW)
        cue = DOWN_CUE_RE.search(clause, lo, hi)
        if cue:
            # An explicit running-claim right on the mention wins over a cue.
            if re.search(
                rf"(?<![\w-]){re.escape(prof)}(?![\w-])[^,;]{{0,20}}"
                r"\b(is |= ?)?(running|active|up|alive|live)\b",
                clause[m.start(): hi],
                re.I,
            ):
                continue
            return cue
    return None


def extract_premises(card: Card, profiles: set[str]) -> list[Premise]:
    seen: set[tuple[str, str]] = set()
    found: list[Premise] = []

    def add(kind: str, subject: str, sentence: str) -> None:
        key = (kind, subject.lower())
        if key in seen:
            return
        seen.add(key)
        found.append(Premise(kind=kind, subject=subject, claim=_norm(sentence)))

    for match in SENTENCE_RE.finditer(card.premise_text):
        sent = match.group(0)
        if not sent.strip():
            continue

        # probe-block is scoped to the SENTENCE: the probe id and its verdict
        # are routinely separated by an arrow ("probe-x @time -> VERDICT: BLOCK"),
        # so clause scope would drop the pair.
        if VERDICT_BLOCK_RE.search(sent):
            for pid in PROBE_ID_RE.findall(sent):
                add("probe-block", pid, sent)

        for clause in CLAUSE_SPLIT_RE.split(sent):
            if not clause.strip():
                continue

            if DOWN_CUE_RE.search(clause):
                # Longest names first so 'jarvis-os-pm' is not shadowed by 'jarvis'.
                for prof in sorted(profiles, key=len, reverse=True):
                    if _down_cue_near(clause, prof):
                        add("profile-down", prof, clause)

            if STUCK_CUE_RE.search(clause):
                for tid in TASK_ID_RE.findall(clause):
                    if tid == card.id:
                        continue
                    add("task-stuck", tid, clause)

    return found


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())[:240]


# --------------------------------------------------------------------------
# live evidence (scanned across every board — activity is cross-board)
# --------------------------------------------------------------------------


def profile_activity_after(
    profile: str, after_epoch: int, boards_dir: Path = BOARDS_DIR
) -> tuple[int, list[tuple[str, str, int]]]:
    """(count, [(board, task_id, completed_at)]) of tasks that profile finished.

    ``typeof(completed_at)='integer'`` is not cosmetic: SQLite sorts TEXT above
    every INTEGER, so a corrupt text timestamp would satisfy ``> after_epoch``
    unconditionally and manufacture fake contradicting evidence.
    """
    hits: list[tuple[str, str, int]] = []
    for board, db in board_dbs(boards_dir).items():
        with connect_ro(db) as con:
            for r in _rows(
                con,
                "SELECT id, completed_at c FROM tasks "
                "WHERE assignee=? AND status='done' "
                "AND typeof(completed_at)='integer' AND completed_at > ? "
                "ORDER BY c DESC",
                (profile, after_epoch),
            ):
                hits.append((board, str(r["id"]), epoch_of(r["c"])))
    hits.sort(key=lambda h: h[2], reverse=True)
    return len(hits), hits


def task_state(task_id: str, boards_dir: Path = BOARDS_DIR) -> tuple[str, str, int] | None:
    """(board, status, completed_at) for a task id, searched across boards."""
    for board, db in board_dbs(boards_dir).items():
        with connect_ro(db) as con:
            row = _rows(
                con,
                "SELECT status, completed_at c FROM tasks WHERE id=?",
                (task_id,),
            )
        if row:
            return board, str(row[0]["status"]), epoch_of(row[0]["c"])
    return None


def probe_cleared_after(
    probe_id: str,
    after_epoch: int,
    boards_dir: Path = BOARDS_DIR,
    *,
    exclude_task_id: str = "",
) -> list[tuple[str, str, int, str]]:
    """Comments posted after ``after_epoch`` naming ``probe_id`` with a clear verdict.

    ``exclude_task_id`` drops the gate card's OWN thread. A card's own comment
    is not independent corroboration of (or against) its own premise — the
    t_4776f5c9 post-mortem called this out explicitly.
    """
    out: list[tuple[str, str, int, str]] = []
    like = f"%{probe_id}%"
    for board, db in board_dbs(boards_dir).items():
        with connect_ro(db) as con:
            for r in _rows(
                con,
                "SELECT task_id, COALESCE(body,'') body, created_at c "
                "FROM task_comments WHERE typeof(created_at)='integer' "
                "AND created_at > ? AND body LIKE ? AND task_id <> ? "
                "ORDER BY created_at DESC LIMIT 40",
                (after_epoch, like, exclude_task_id),
            ):
                body = str(r["body"])
                for sent in SENTENCE_RE.finditer(body):
                    s = sent.group(0)
                    if probe_id.lower() in s.lower() and VERDICT_CLEAR_RE.search(s):
                        out.append((board, str(r["task_id"]), epoch_of(r["c"]), _norm(s)))
                        break
    out.sort(key=lambda h: h[2], reverse=True)
    return out


def evaluate_premises(
    card: Card,
    premises: list[Premise],
    *,
    boards_dir: Path = BOARDS_DIR,
    min_completions: int = DEFAULT_MIN_COMPLETIONS,
) -> None:
    for p in premises:
        if p.kind == "profile-down":
            count, hits = profile_activity_after(p.subject, card.created_at, boards_dir)
            if count >= min_completions:
                p.verdict = "falsified"
                board, tid, when = hits[0]
                p.evidence.append(
                    f"{p.subject} completed {count} task(s) AFTER the gate was raised "
                    f"({ts(card.created_at)}); most recent {board}/{tid} done {ts(when)} "
                    f"({_delta(when, card.created_at)} after raise)"
                )
                for board, tid, when in hits[1:4]:
                    p.evidence.append(f"  also {board}/{tid} done {ts(when)}")
            else:
                p.verdict = "standing"
                p.evidence.append(
                    f"{p.subject}: only {count} completion(s) after raise "
                    f"(threshold {min_completions}) — premise NOT contradicted"
                )
        elif p.kind == "task-stuck":
            state = task_state(p.subject, boards_dir)
            if state is None:
                p.verdict = "unverifiable"
                p.evidence.append(
                    f"{p.subject}: not found on any board — cannot verify either way"
                )
                continue
            board, status, completed = state
            if status == "done" and completed > card.created_at:
                p.verdict = "falsified"
                p.evidence.append(
                    f"cited-stuck {board}/{p.subject} is status=done, completed {ts(completed)} "
                    f"({_delta(completed, card.created_at)} after raise)"
                )
            else:
                p.verdict = "standing"
                p.evidence.append(
                    f"cited-stuck {board}/{p.subject} still status={status} "
                    f"(completed_at={ts(completed)}) — premise NOT contradicted"
                )
        elif p.kind == "probe-block":
            clears = probe_cleared_after(
                p.subject, card.created_at, boards_dir, exclude_task_id=card.id
            )
            if clears:
                p.verdict = "falsified"
                board, tid, when, line = clears[0]
                p.evidence.append(
                    f"probe {p.subject} reported a clear verdict after the raise: "
                    f"{board}/{tid} @ {ts(when)} ({_delta(when, card.created_at)}): {line}"
                )
            else:
                # A re-run that never happened is not the same as a re-run that
                # confirmed BLOCK. Absence of an independent verdict is silence,
                # not corroboration — mark it unverifiable, not standing.
                p.verdict = "unverifiable"
                p.evidence.append(
                    f"probe {p.subject}: no INDEPENDENT clear verdict observed after raise "
                    f"(the card's own thread {card.id} is excluded — a card cannot "
                    "corroborate itself); premise UNVERIFIABLE, not confirmed"
                )


# --------------------------------------------------------------------------
# disposition
# --------------------------------------------------------------------------


def independent_authorization_hit(card: Card, premises: list[Premise]) -> str | None:
    """Authorization request that does NOT depend on a falsified subject.

    Sentences naming a falsified subject are stripped first: "start the gateway
    that is down" is moot once "the gateway is down" is proven false, so it must
    not keep the card alive. Anything left that still asks for Frank/A3
    authority is a real, unfalsifiable gate.
    """
    subjects = [p.subject.lower() for p in premises if p.contradicted]
    for match in SENTENCE_RE.finditer(card.premise_text):
        sent = match.group(0)
        low = sent.lower()
        if any(s in low for s in subjects):
            continue
        hit = AUTHORIZATION_RE.search(sent)
        if not hit:
            continue
        if DENIAL_CUE_RE.search(sent):
            continue
        return _norm(sent)
    return None


def classify(
    card: Card,
    *,
    profiles: set[str],
    boards_dir: Path = BOARDS_DIR,
    min_completions: int = DEFAULT_MIN_COMPLETIONS,
) -> dict[str, Any]:
    premises = extract_premises(card, profiles)
    evaluate_premises(card, premises, boards_dir=boards_dir, min_completions=min_completions)

    falsified = [p for p in premises if p.verdict == "falsified"]
    standing = [p for p in premises if p.verdict == "standing"]
    unverifiable = [p for p in premises if p.verdict == "unverifiable"]

    if not premises:
        disposition, reason = "HOLD-NO-PREMISE", (
            "no falsifiable factual premise in title/body — this gate is a judgement "
            "call, not a claim activity can disprove"
        )
    elif standing:
        disposition, reason = "PARTIAL-RESCOPE", (
            f"{len(falsified)}/{len(premises)} premise(s) falsified; "
            f"{len(standing)} still standing — re-scope, do not retire"
        )
    elif not falsified:
        # every premise is unverifiable: no independent evidence either way.
        disposition, reason = "PARTIAL-RESCOPE", (
            f"all {len(unverifiable)} premise(s) unverifiable — no independent "
            "post-raise evidence for or against; needs a human, not a retirement"
        )
    else:
        auth = independent_authorization_hit(card, premises)
        if auth:
            disposition, reason = "HOLD-AUTHORIZATION", (
                f"every premise falsified, but an independent authorization request "
                f"remains: {auth}"
            )
        else:
            unverif_note = (
                f" ({len(unverifiable)} unverifiable premise(s) carried, none standing)"
                if unverifiable else ""
            )
            disposition, reason = "RETIRE-ELIGIBLE", (
                f"{len(falsified)}/{len(premises)} stated premise(s) falsified by "
                f"independent post-raise activity{unverif_note}; the decision this "
                "gate asks for is moot"
            )

    return {
        "board": card.board,
        "task_id": card.id,
        "title": card.title[:160],
        "assignee": card.assignee,
        "status": card.status,
        "block_kind": card.block_kind,
        "raised_at": ts(card.created_at),
        "raised_epoch": card.created_at,
        "disposition": disposition,
        "reason": reason,
        "premises": [p.as_dict() for p in premises],
    }


# --------------------------------------------------------------------------
# emitted text
# --------------------------------------------------------------------------


def retirement_comment(plan: dict[str, Any], *, authorized_by: str) -> str:
    lines = [
        f"{RETIRED_MARKER} RECOMMENDATION ({MARKER})",
        "",
        f"PREMISE(S) CLAIMED by this card at raise ({plan['raised_at']}):",
    ]
    for p in plan["premises"]:
        lines.append(f"  - [{p['kind']}] {p['subject']}: \"{p['claim']}\"")
    lines.append("")
    lines.append("CONTRADICTING ACTIVITY OBSERVED AFTER RAISE:")
    for p in plan["premises"]:
        for ev in p["evidence"]:
            lines.append(f"  - {ev}")
    lines.append("")
    lines.append(f"DISPOSITION: {plan['disposition']} — {plan['reason']}")
    lines.append(
        "GATE: probe is read-only by default; status mutation is PM/Frank lane "
        f"(authorized-by={authorized_by}). No credential, deploy, provider-routing, "
        "or trading action is taken by this probe."
    )
    return assert_no_approval_token("\n".join(lines))


def consolidated_note(plans: list[dict[str, Any]], *, generated_at: str) -> str:
    retire = [p for p in plans if p["disposition"] == "RETIRE-ELIGIBLE"]
    partial = [p for p in plans if p["disposition"] == "PARTIAL-RESCOPE"]
    hold = [p for p in plans if p["disposition"].startswith("HOLD")]
    lines = [
        f"FALSIFIED-FRANK_GATE RETIREMENT SWEEP ({MARKER})",
        f"generated_at: {generated_at}",
        "",
        f"scanned={len(plans)} retire_eligible={len(retire)} "
        f"partial_rescope={len(partial)} hold={len(hold)}",
        "",
    ]
    if retire:
        lines.append("RETIRE-ELIGIBLE (stated root cause falsified by observed activity):")
        for p in retire:
            lines.append(f"- {p['board']}/{p['task_id']} — {p['title']}")
            for pr in p["premises"]:
                for ev in pr["evidence"]:
                    lines.append(f"    {ev}")
    else:
        lines.append("RETIRE-ELIGIBLE: none this sweep.")
    if partial:
        lines.append("")
        lines.append("PARTIAL-RESCOPE (some premises falsified, some standing):")
        for p in partial:
            lines.append(f"- {p['board']}/{p['task_id']} — {p['reason']}")
    if hold:
        lines.append("")
        lines.append("HOLD (no falsifiable premise, or an independent authorization gate):")
        for p in hold:
            lines.append(f"- {p['board']}/{p['task_id']} [{p['disposition']}] — {p['reason']}")
    lines.append("")
    lines.append(
        "One consolidated note per sweep by design: N individual card mutations "
        "without a trace is exactly the failure this replaces."
    )
    return assert_no_approval_token("\n".join(lines))


# --------------------------------------------------------------------------
# writes (gated)
# --------------------------------------------------------------------------


def already_marked(con: sqlite3.Connection, task_id: str) -> bool:
    return bool(
        _rows(
            con,
            "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
            (task_id, f"%{MARKER}%"),
        )
    )


def apply_plan(
    plan: dict[str, Any],
    *,
    boards_dir: Path = BOARDS_DIR,
    authorized_by: str,
    retire: bool,
    now_epoch: int | None = None,
) -> str:
    """Comment (+ optionally retire) one RETIRE-ELIGIBLE card. Idempotent."""
    if plan["disposition"] != "RETIRE-ELIGIBLE":
        return "skipped-not-eligible"
    now = int(time.time()) if now_epoch is None else now_epoch
    db = board_dbs(boards_dir)[plan["board"]]
    body = retirement_comment(plan, authorized_by=authorized_by)
    with connect_rw(db) as con:
        try:
            if already_marked(con, plan["task_id"]):
                return "already-present"
            con.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?,?,?,?)",
                (plan["task_id"], AUTHOR, body, now),
            )
            con.execute(
                "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
                "VALUES (?,NULL,?,?,?)",
                (
                    plan["task_id"],
                    "commented",
                    json.dumps({"author": AUTHOR, "marker": MARKER,
                                "disposition": plan["disposition"]}),
                    now,
                ),
            )
            if retire:
                con.execute(
                    "UPDATE tasks SET status='done', completed_at=?, "
                    "result=? WHERE id=?",
                    (
                        now,
                        f"{RETIRED_MARKER}: frank_gate premise falsified by post-raise "
                        f"activity ({MARKER}, authorized-by={authorized_by})",
                        plan["task_id"],
                    ),
                )
                con.execute(
                    "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
                    "VALUES (?,NULL,?,?,?)",
                    (
                        plan["task_id"],
                        "completed",
                        json.dumps({"by": AUTHOR, "marker": MARKER,
                                    "reason": RETIRED_MARKER,
                                    "authorized_by": authorized_by}),
                        now,
                    ),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
    return "retired" if retire else "comment-added"


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def build_plans(
    *,
    boards: Iterable[str] = TARGET_BOARDS,
    boards_dir: Path = BOARDS_DIR,
    profiles_dir: Path = PROFILES_DIR,
    min_completions: int = DEFAULT_MIN_COMPLETIONS,
    canary_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    profiles = known_profiles(profiles_dir)
    cards = fetch_frank_gate_cards(boards, boards_dir, include_task_ids=canary_ids)
    return [
        classify(c, profiles=profiles, boards_dir=boards_dir, min_completions=min_completions)
        for c in cards
    ]


def render_text(plans: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for p in plans:
        out.append(
            f"{p['disposition']:<20} {p['board']}/{p['task_id']} raised={p['raised_at']} "
            f"assignee={p['assignee']} status={p['status']}/{p['block_kind'] or '(empty)'}"
        )
        out.append(f"  title: {p['title']}")
        out.append(f"  reason: {p['reason']}")
        for pr in p["premises"]:
            flag = "FALSIFIED" if pr["contradicted"] else "STANDING "
            out.append(f"  [{flag}] {pr['kind']}:{pr['subject']} :: {pr['claim']}")
            for ev in pr["evidence"]:
                out.append(f"      evidence: {ev}")
        if not p["premises"]:
            out.append("  (no falsifiable premise extracted)")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boards", nargs="*", default=list(TARGET_BOARDS))
    ap.add_argument("--boards-dir", type=Path, default=BOARDS_DIR)
    ap.add_argument("--profiles-dir", type=Path, default=PROFILES_DIR)
    ap.add_argument("--min-completions", type=int, default=DEFAULT_MIN_COMPLETIONS,
                    help="post-raise completions that falsify a 'profile is down' claim")
    ap.add_argument("--canary", nargs="*", default=[], metavar="TASK_ID",
                    help="also replay these task ids regardless of current status "
                         "(e.g. t_4776f5c9)")
    ap.add_argument("--apply-comments", action="store_true",
                    help="post the RETIRE recommendation comment (no status change)")
    ap.add_argument("--apply-retire", action="store_true",
                    help="ALSO set status=done with a RETIRED-STALE marker")
    ap.add_argument("--authorized-by", default="",
                    help="required for any write: PM/Frank authorization reference")
    ap.add_argument("--emit-consolidated", type=Path, default=None,
                    help="write the consolidated Frank/Elon batch note to this path")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    writing = args.apply_comments or args.apply_retire
    if writing and not args.authorized_by.strip():
        print("ERROR: --apply-comments/--apply-retire require --authorized-by "
              "(PM/Frank lane authorization reference)", file=sys.stderr)
        return 2

    plans = build_plans(
        boards=args.boards,
        boards_dir=args.boards_dir,
        profiles_dir=args.profiles_dir,
        min_completions=args.min_completions,
        canary_ids=args.canary,
    )

    applied: dict[str, int] = {}
    if writing:
        for plan in plans:
            res = apply_plan(
                plan,
                boards_dir=args.boards_dir,
                authorized_by=args.authorized_by.strip(),
                retire=args.apply_retire,
            )
            applied[res] = applied.get(res, 0) + 1

    generated_at = datetime.now(timezone.utc).isoformat()
    note = consolidated_note(plans, generated_at=generated_at)
    if args.emit_consolidated:
        args.emit_consolidated.parent.mkdir(parents=True, exist_ok=True)
        args.emit_consolidated.write_text(note + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for p in plans:
        counts[p["disposition"]] = counts.get(p["disposition"], 0) + 1

    if args.json:
        print(json.dumps({
            "generated_at": generated_at,
            "mode": ("apply-retire" if args.apply_retire
                     else "apply-comments" if args.apply_comments else "dry-run"),
            "boards": args.boards,
            "counts": counts,
            "applied": applied or {"dry_run": 1},
            "plans": plans,
            "consolidated_note": note,
        }, indent=2, sort_keys=True))
    else:
        print(f"FRANK_GATE_PREMISE_PROBE mode="
              f"{'apply-retire' if args.apply_retire else 'apply-comments' if args.apply_comments else 'dry-run'} "
              f"boards={','.join(args.boards)} scanned={len(plans)} "
              f"counts={json.dumps(counts, sort_keys=True)} "
              f"applied={json.dumps(applied, sort_keys=True) if applied else '{}'}")
        print()
        print(render_text(plans))
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
