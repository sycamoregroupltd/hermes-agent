#!/usr/bin/env python3
"""kanban_dedupe_guard.py — Kanban dedupe + Frank-gate dispatch guard.

Born from the 2026-07-05 out-of-band production DDL incident
(fleet vault: Governance/incidents/2026-07-05-out-of-band-production-ddl-incident.md):
sycode-trading-pm cloned gate-blocked t_5c25f222 into t_d0fcaddb on a profile
without the gate, and that profile applied production DDL out-of-band.

Six deterministic rules (no LLM):

  RULE 1 (dupe-of-gate-blocked): an active task (todo/ready/running) whose
  failure signature (referenced t_xxxxxxxx ids, file names, quoted error
  strings) overlaps a CURRENTLY gate-blocked task on the same board is a
  clone. PMs must `hermes kanban link`, not clone. Properly linked tasks
  (parent/child of the blocked task) are exempt.

  RULE 2 (gate-marker dispatch): an active task whose title/body carries
  strong Frank-gate markers (FRANK-GATED, approval-gated, requires Frank
  approval, production DDL, no deploy) assigned to a profile whose SOUL.md
  has NO Frank-escalation language must not run.

  RULE 3 (title-token duplicate window): an active task whose normalized title
  token set is identical to, or Jaccard >= 0.85 with, another non-archived task
  created on the same board in the last 14 days is a HIGH duplicate. Jaccard
  0.50-0.85 is MEDIUM and comment-only. Parent/child links are exempt.

  RULE 4 (stale-reference lane): a blocked RESEARCH-ACTIONABLE / REVIEW lane
  whose referenced source task is already done is a phantom blocker; report it,
  and optionally close it when --resolve-stale-refs is explicitly passed.

  RULE 5 (research-actionable bullet-only card): a newly-created
  RESEARCH-ACTIONABLE card must be a grouped, independent workstream with an
  owner/assignee, acceptance/verification criteria, and a gate/safety marker.
  Single bullet/specification/heading/table/code fragments are blocked at
  kanban_create time; the source task should receive one digest child instead.

  RULE 6 (blocked-reference cooldown, t_69764ac8): an active (todo/ready) task
  whose most recent park was a dependency-wait, or whose title/body/last
  comment carries an explicit RESUME_GATE / "blocked on" / "waiting on"
  reference to another task id, is a phantom-redispatch candidate when (a)
  that reference is NOT a real graph-link parent, (b) the referenced task is
  still open, and (c) the referenced task's content signature is unchanged
  since this rule last acted. recompute_ready promotes todo/blocked tasks
  whose real graph-link parents are done — with zero visibility into prose
  references — so a stale dependency-wait park re-enters the promotion path
  every dispatcher tick and a freshly spawned worker re-derives and
  re-comments the identical already-diagnosed finding. Live-verified
  2026-09-10: sycode-trading/t_6177afc8 (9 claim/spawn/dependency_wait/
  promoted cycles in ~1h) referencing jarvis-os/t_84f1aeda (unchanged).
  Match -> re-park (kind=dependency) with ONE idempotent comment instead of
  a fresh worker dispatch. NEVER suppressed: credential/approval/payment/
  spend/deploy/production markers, a real graph-link parent, a resolved
  (done/archived) reference, or a changed reference signature. A 4h TTL lets
  exactly one fresh dispatch through per episode to refresh the hash.

Enforcement honesty: Hermes has no pre-create/pre-dispatch veto hook
(kanban_task_* hooks are observer-only, fired after commit — verified in
hermes_cli/plugins.py VALID_HOOKS). This script is the detect-and-block
backstop: cron'd no-agent, it BLOCKS todo/ready offenders (reversible via
`hermes kanban unblock`) and ALARMS on running ones. True pre-create
blocking for agent-created tasks is provided separately by the
pre_tool_call hook gate-kanban-dupe-create.sh (matcher: kanban_create),
which calls this script with --hook-check.

Modes:
  (default)            scan boards, enforce (block todo/ready, comment running)
  --dry-run            report only, no board mutations
  --include-archived   include archived/done tasks as offender candidates (testing)
  --assume-blocked ID  treat ID as gate-blocked regardless of status (testing; repeatable)
  --boards a,b|all     boards to scan (default: sycode-trading)
  --hook-check         read a pre_tool_call payload JSON on stdin; print a
                       block reason to stdout if creation should be blocked,
                       print nothing to allow. Always exit 0 (fail-open).

Silent stdout when clean (no-agent cron watchdog contract).
State: ~/.hermes/scripts/state/kanban_dedupe_guard_state.json (no repeat actions).
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))
BOARDS_DIR = HERMES_ROOT / "kanban" / "boards"
PROFILES_DIR = HERMES_ROOT / "profiles"
STATE_PATH = HERMES_ROOT / "scripts" / "state" / "kanban_dedupe_guard_state.json"
DEFAULT_BOARDS = ["sycode-trading"]
ACTIVE_STATUSES = ("todo", "ready", "running")
GUARD_AUTHOR = "kanban-dedupe-guard"
INCIDENT_NOTE = (
    "obsidian-fleet-vault/Governance/incidents/"
    "2026-07-05-out-of-band-production-ddl-incident.md"
)

# --- signature extraction -------------------------------------------------

TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
FILE_RE = re.compile(
    r"[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.(?:tsx?|jsx?|py|sql|sh|md|ya?ml|json)\b"
)
QUOTED_RE = re.compile(r'["`]([^"`\n]{8,120})["`]')

# Gate markers on the BLOCKED side (why a task is gate-blocked). Broad.
GATE_BLOCK_RE = re.compile(
    r"(?i)(frank[- ]gated|approval[- ]gated|needs[- _]approval|"
    r"requires?\s+(frank\s+)?approval|frank\s+approval|await(ing)?\s+frank|"
    r"do\s+not\s+deploy|no\s+deploy|production\s+ddl|human\s+approval|"
    r"needs_input|pending\s+(frank|review|approval))"
)
# Strong gate markers on a CANDIDATE task (RULE 2 trigger). Narrow, explicit.
# Calibration 2026-07-05: "no deploy"/"do not deploy" removed — board history
# shows they appear routinely as legitimate SCOPE constraints inside cards
# ("this task must not deploy"), which is safe on any profile. Only markers
# that mean "this work itself is gated on Frank" remain.
GATE_STRONG_RE = re.compile(
    r"(?i)(FRANK[- ]GATED|approval[- ]gated|requires?\s+frank\s+approval|"
    r"frank\s+approval\s+required|gated\s+on\s+frank|production\s+ddl)"
)
# A profile SOUL that contains this is considered gate-honoring.
SOUL_GATE_RE = re.compile(
    r"(?i)(escalate\s+to\s+frank|frank\s+approval|requires?\s+frank|"
    r"frank[- ]gate|never\s+bypass|guardian\s+gate|must\s+escalate)"
)

COMMON_FILE_NOISE = {"config.yaml", "readme.md", "soul.md", "agents.md", "claude.md"}

TITLE_WINDOW_SECONDS = 14 * 24 * 60 * 60
TITLE_HIGH_THRESHOLD = 0.85
TITLE_MEDIUM_THRESHOLD = 0.50
TITLE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "the", "to", "with", "without",
    "add", "address", "build", "create", "debug", "draft", "enable", "fix",
    "implement", "improve", "investigate", "repair", "review", "route", "run",
    "set", "sync", "triage", "update", "verify", "wire", "work",
    "card", "task", "phase", "p0", "p1", "p2", "p3", "proposal",
}
TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")
NON_ARCHIVED_STATUSES = {"todo", "ready", "running", "blocked", "scheduled", "done"}
REVIEW_TITLE_RE = re.compile(r"(?i)\b(pre[- ]review|review|verify|verification|guardian|risk-review)\b")

# --- RULE 5: research-actionable bullet-only card suppression --------------
RESEARCH_ACTIONABLE_TITLE_RE = re.compile(r"(?i)^RESEARCH-ACTIONABLE\b")
RA_SOURCE_RE = re.compile(r"(?i)\b(?:[a-z0-9_-]+/)?t_[0-9a-f]{8}\b")
OWNER_MARKER_RE = re.compile(r"(?i)\b(owner|assignee|assigned to|profile)\b")
ACCEPTANCE_MARKER_RE = re.compile(
    r"(?i)\b(acceptance\s+(?:test|criteria)|verification\s+criteria|"
    r"required\s+checks?|done\s+when|tests?\s+run|review-required)\b"
)
GATE_MARKER_RE = re.compile(
    r"(?i)\b(gate\s*(?:class)?|A[0-3]\b|safety|review\s+gate|"
    r"Frank\s+gate|no\s+live\s+trading|no\s+credentials?)\b"
)
BULLET_FRAGMENT_RE = re.compile(
    r"(?ix)^(?:\s*(?:[-*+]\s*)?)"
    r"(?:\|.*\||`{1,3}.*|\#{1,6}\s+.*|(?:and|or|but|then|also|vs\.?|where)\b.*|"
    r"\d+[.)]\s*(?:JWT\s+token\s+requirement|Are\s+leak-free|[A-Z][^:]{0,40}$))"
)

# --- RULE 4: stale-reference blocked lanes ---------------------------------
# A RESEARCH-ACTIONABLE / REVIEW child lane whose referenced source task is
# already `done` is a phantom blocker (it exists only to track work that
# finished). Born from jarvis-os PROCESS-FIX t_c60c6a57: t_7cca7076 stayed
# blocked while it referenced t_349cf425 which completed 2026-07-05.
# Match lanes whose title carries the auto-routed routing prefix, or any
# blocked lane that explicitly references a done task id in title/body.
STALE_REF_LANE_RE = re.compile(
    r"(?i)^(?:RESEARCH-ACTIONABLE|RE-?REVIEW|REVIEW)\b"
)
STALE_REF_ID_RE = re.compile(r"(?:[a-z0-9_-]+/)?(t_[0-9a-f]{8})\b")
# For REVIEW lanes, a contrary verdict means genuine work remains.
CONTRARY_VERDICTS = {"CHANGES_REQUESTED", "REJECT", "BLOCK", "REWORK_REQUIRED"}
STALE_REF_VERDICT_RE = re.compile(
    r"REVIEW_VERDICT\s*[:=]\s*([A-Z0-9_]+)", re.IGNORECASE
)

# --- RULE 6: blocked-reference cooldown (t_69764ac8) ------------------------
# recompute_ready promotes todo/blocked tasks whose real graph-link PARENTS
# are all done/archived, unconditionally, every dispatcher tick. A prose
# "RESUME_GATE: waiting on <task>" reference (or a dependency_wait block whose
# reason cites another task) is NEVER a real graph-link parent, so the
# promotion sweep has zero visibility into it: a task parked on a still-open
# reference re-enters the ready pool the very next tick and a freshly spawned
# worker re-derives and re-comments the identical already-diagnosed finding.
# Live-verified 2026-09-10: sycode-trading/t_6177afc8 cycled claim/spawn/
# dependency_wait/promoted 9x in ~1h referencing jarvis-os/t_84f1aeda
# (content unchanged the entire window).
#
# Marker words that introduce an explicit cross-task reference in title/body/
# last comment. Deliberately narrow (mirrors STALE_REF_LANE_RE's precision
# bias) — a broad match would misfire on ordinary prose mentioning a task id.
RULE6_REF_MARKER_RE = re.compile(
    r"(?i)\b(resume_gate|blocked[\s-]+on|waiting[\s-]+on|depends?\s+on)\b"
    r".{0,80}?(t_[0-9a-f]{8})"
)
# Credential/approval/payment/spend/deploy/production carve-out — NEVER
# suppressed regardless of hash/TTL state. Mirrors governor_comment_dedupe.py
# CRITICAL_MARKER_RE (t_3d108e24) so the two dedupe surfaces agree on what
# "critical" means.
RULE6_CRITICAL_MARKER_RE = re.compile(
    r"(?i)\b(credential|credentials|api[- ]?key|secret|password|token|"
    r"approval[- ]?critical|frank[- ]?approval|needs[- ]?approval|"
    r"payment|spend|billing|deploy|production)\b"
)
RULE6_KEY_PREFIX = "rule6-blocked-ref-cooldown:v1:"
RULE6_TTL_HOURS = 4.0
RULE6_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Cross-board task cache: a dependency reference frequently names a task on a
# DIFFERENT board (the t_6177afc8 incident references jarvis-os from
# sycode-trading). task_links (the real graph) is intra-board only per the
# board-isolation design, so a cross-board reference can never be a real
# parent link — resolving it is purely for status/signature lookup.
_BOARD_CACHE: dict[str, dict | None] = {}


def title_role(title: str) -> str:
    return "review" if REVIEW_TITLE_RE.search(title or "") else "work"


def title_high_allowed(title_a: str, title_b: str, toks_a: set[str], toks_b: set[str]) -> bool:
    """False-positive guard for RULE 3 HIGH.

    Short/generic titles and review-vs-implementation pairs are noisy enough to
    remain MEDIUM comment-only even when Jaccard is high. Identical substantive
    work titles (for example repeated PROPOSAL cards) still HIGH-block.
    """
    if len(toks_a | toks_b) < 4:
        return False
    return title_role(title_a) == title_role(title_b)


def title_tokens(title: str) -> set[str]:
    """Normalize a title to high-signal duplicate-detection tokens."""
    tokens: set[str] = set()
    for raw in TITLE_TOKEN_RE.findall((title or "").lower()):
        token = raw.strip("_-")
        if len(token) <= 2 or token in TITLE_STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def research_actionable_block_reason(title: str, body: str, assignee: str) -> str | None:
    """RULE 5: block RESEARCH-ACTIONABLE bullet/spec-line task spam at create time.

    A legitimate research-actionable child is not just a copied bullet. It must
    be an independently runnable digest/workstream with a named owner, an
    acceptance or verification criterion, and a gate/safety boundary. This keeps
    automated decomposers from turning every markdown bullet into a separate
    blocked child card while preserving a path for one grouped implementation
    card per source.
    """
    if not RESEARCH_ACTIONABLE_TITLE_RE.search(title or ""):
        return None

    hay = f"{title}\n{body or ''}"
    has_owner = bool((assignee or "").strip() or OWNER_MARKER_RE.search(hay))
    has_acceptance = bool(ACCEPTANCE_MARKER_RE.search(hay))
    has_gate = bool(GATE_MARKER_RE.search(hay))

    suffix = title.split("—", 1)[-1].split("-", 1)[-1].strip() if title else ""
    looks_fragment = bool(BULLET_FRAGMENT_RE.search(suffix))
    looks_single_source_bullet = bool(RA_SOURCE_RE.search(title or "")) and len((body or "").strip()) < 220

    if has_owner and has_acceptance and has_gate and not looks_fragment:
        return None

    missing = []
    if not has_owner:
        missing.append("owner/assignee")
    if not has_acceptance:
        missing.append("acceptance/verification criteria")
    if not has_gate:
        missing.append("gate/safety marker")
    if looks_fragment or looks_single_source_bullet:
        missing.append("grouped independent-workstream digest")
    return (
        f"BLOCKED by {GUARD_AUTHOR}: RESEARCH-ACTIONABLE child cards must be "
        f"grouped independent workstreams, not one card per bullet/spec line. "
        f"Missing/weak contract: {', '.join(missing)}. Create/comment one digest "
        f"child for the source task with distinct owner, acceptance test, and gate. "
        f"Ref: sycode-trading/t_1243d100"
    )


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def recent_non_archived(task: dict, now: int | None = None) -> bool:
    if task.get("status") not in NON_ARCHIVED_STATUSES:
        return False
    try:
        created_at = int(task.get("created_at") or 0)
    except (TypeError, ValueError):
        return False
    if created_at <= 0:
        return False
    now = int(time.time()) if now is None else now
    return created_at >= now - TITLE_WINDOW_SECONDS


def extract_signature(text: str) -> dict:
    """Failure-signature tokens: task ids, file basenames, quoted error strings."""
    text = text or ""
    task_ids = set(TASK_ID_RE.findall(text))
    files = set()
    for m in FILE_RE.finditer(text):
        base = m.group(0).rsplit("/", 1)[-1].lower()
        if base not in COMMON_FILE_NOISE:
            files.add(base)
    errors = set()
    for m in QUOTED_RE.finditer(text):
        s = " ".join(m.group(1).split()).lower()
        if s.startswith("http"):
            continue
        # Path-like quoted tokens are already captured in `files` — counting
        # them again here would double-count one file as two signature
        # classes (caused a false HIGH on t_a3be3fa4 during calibration).
        if "/" in s and " " not in s:
            continue
        # error-string-ish: has a space or a dot, not a bare word
        if " " in s or "." in s:
            errors.add(s)
    return {"task_ids": task_ids, "files": files, "errors": errors}


def overlap(sig_a: dict, sig_b: dict) -> tuple[int, int, int, list[str]]:
    """Return (shared_task_ids, total_shared, distinct_classes, shared_tokens)."""
    shared_ids = sig_a["task_ids"] & sig_b["task_ids"]
    shared_files = sig_a["files"] & sig_b["files"]
    shared_errors = sig_a["errors"] & sig_b["errors"]
    tokens = sorted(shared_ids) + sorted(shared_files) + sorted(shared_errors)
    classes = sum(1 for s in (shared_ids, shared_files, shared_errors) if s)
    return len(shared_ids), len(tokens), classes, tokens


# --- board access (read-only sqlite) ---------------------------------------

def open_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def load_board(board: str) -> dict | None:
    db_path = BOARDS_DIR / board / "kanban.db"
    if not db_path.is_file():
        return None
    db = open_ro(db_path)
    tasks = {}
    for tid, title, body, assignee, status, created_at, block_kind in db.execute(
        "SELECT id, title, COALESCE(body,''), COALESCE(assignee,''), "
        "status, COALESCE(created_at,0), block_kind FROM tasks"
    ):
        tasks[tid] = {
            "id": tid, "title": title, "body": body,
            "assignee": assignee, "status": status,
            "created_at": created_at, "block_kind": block_kind,
        }
    comments = {}
    for tid, body in db.execute(
        "SELECT task_id, body FROM task_comments ORDER BY created_at"
    ):
        comments.setdefault(tid, []).append(body or "")
    links = set()
    for p, c in db.execute("SELECT parent_id, child_id FROM task_links"):
        links.add((p, c))
    db.close()
    return {"tasks": tasks, "comments": comments, "links": links}


def is_gate_blocked(task: dict, comments: list[str]) -> bool:
    if task["status"] != "blocked":
        return False
    hay = task["title"] + "\n" + task["body"] + "\n" + "\n".join(comments[-6:])
    return bool(GATE_BLOCK_RE.search(hay))


def profile_honors_gate(profile: str) -> bool:
    """Gate-honoring = SOUL.md contains explicit Frank-escalation language.
    Unknown profile or unreadable SOUL => NOT honoring (fail-closed for RULE 2:
    gated work must only go where the gate is written down)."""
    soul = PROFILES_DIR / profile / "SOUL.md"
    try:
        return bool(SOUL_GATE_RE.search(soul.read_text(errors="replace")))
    except OSError:
        return False


# --- actions ---------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"actions": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_PATH)


def run_hermes(args: list[str]) -> bool:
    try:
        r = subprocess.run(
            ["hermes"] + args, capture_output=True, text=True, timeout=60
        )
        return r.returncode == 0
    except Exception:
        return False


def act(board: str, task: dict, rule: str, reason: str,
        state: dict, dry_run: bool, report: list[str]) -> None:
    key = f"{board}:{task['id']}:{rule}"
    if key in state["actions"]:
        return  # already acted this incarnation
    verb = "WOULD-ACT(dry-run)" if dry_run else "ACT"
    # Only 'ready' (and 'running', via the kernel's own path) can actually be
    # blocked: kanban_db.block_task's UPDATE is guarded by
    # `status IN ('running','ready')` and returns False for anything else. The
    # guard used to treat 'todo' as blockable, so every todo target logged
    # "ACT ... -> block" and then failed with "cannot block" — the guard
    # reported success-shaped output while mutating nothing (2026-08-30).
    # todo cards get the alarm-comment instead, which is honest and visible.
    blockable = task["status"] == "ready"
    action = "block" if blockable else "alarm-comment"
    report.append(
        f"{verb} [{rule}] {board}/{task['id']} ({task['status']}, "
        f"assignee={task['assignee'] or '-'}) -> {action}: {reason}"
    )
    if dry_run:
        return
    ok = True
    comment = (
        f"[{GUARD_AUTHOR}] {rule}: {reason} | Policy: link, don't clone "
        f"(hermes kanban link); Frank-gated work only to gate-honoring profiles. "
        f"False positive? `hermes kanban --board {board} unblock {task['id']}`. "
        f"Ref: {INCIDENT_NOTE}"
    )
    ok &= run_hermes(
        ["kanban", "--board", board, "comment", task["id"],
         "--author", GUARD_AUTHOR, comment]
    )
    if blockable:
        # Argument ORDER matters: the reason is a positional with nargs='*', so
        # it MUST come before --kind. With `--kind X "reason"` the top-level
        # hermes parser swallows the reason and errors "unrecognized arguments"
        # — every guard action failed silently this way until 2026-08-30.
        ok &= run_hermes(
            ["kanban", "--board", board, "block", task["id"],
             f"[{GUARD_AUTHOR}] {rule}: {reason}", "--kind", "needs_input"]
        )
    if ok:
        state["actions"][key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(state)
    else:
        report.append(f"ERROR: hermes CLI action failed for {key}")


# --- RULE 4: stale-reference blocked lanes --------------------------------

def latest_stale_ref_verdict(comments: list[str]) -> str | None:
    """Return the latest REVIEW_VERDICT found in a lane's comments, if any."""
    for body in reversed(comments):
        matches = list(STALE_REF_VERDICT_RE.finditer(body or ""))
        if matches:
            return matches[-1].group(1).upper()
    return None


def resolve_stale_ref_lane(board: str, task: dict, ref_id: str,
                           state: dict, dry_run: bool,
                           report: list[str]) -> None:
    """Close a phantom blocker: comment + kanban complete, idempotent."""
    key = f"{board}:{task['id']}:RULE4-stale-ref"
    if key in state["actions"]:
        return
    verb = "WOULD-RESOLVE(dry-run)" if dry_run else "RESOLVE"
    report.append(
        f"{verb} [RULE4-stale-ref] {board}/{task['id']} "
        f"(blocked, assignee={task['assignee'] or '-'}) -> complete: "
        f"references done task {ref_id}"
    )
    if dry_run:
        return
    comment = (
        f"[{GUARD_AUTHOR}] RULE4-stale-ref: this lane references {ref_id} "
        f"which is already done, so it is a phantom blocker. Auto-closing "
        f"with evidence comment. If genuine work remains, reopen a concrete "
        f"child task. Ref: jarvis-os/t_c60c6a57."
    )
    ok = True
    ok &= run_hermes(
        ["kanban", "--board", board, "comment", task["id"],
         "--author", GUARD_AUTHOR, comment]
    )
    ok &= run_hermes(
        ["kanban", "--board", board, "complete", task["id"],
         "--summary",
         f"Auto-resolved stale-reference blocker: source {ref_id} is done."]
    )
    if ok:
        state["actions"][key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(state)
    else:
        report.append(f"ERROR: resolve failed for {key}")


def scan_board_stale_refs(board: str, *, state: dict, dry_run: bool,
                          resolve: bool, report: list[str],
                          tasks: dict, comments: dict) -> None:
    """RULE 4: find blocked auto-routed lanes referencing a done task id.

    Scoped to RESEARCH-ACTIONABLE / RE-REVIEW / REVIEW child lanes (the
    auto-generated lanes from research_review_extractor.py / completion-gate
    router whose sole purpose is to track the referenced source task). An
    arbitrary `blocked` card that merely *mentions* a done task (e.g. a build
    lane spawned from a done proposal, or a VERIFY lane against a done
    escalation) is NOT a phantom blocker and is left alone — it has genuine
    remaining work. Conservative: `blocked` lanes only, referenced source
    `done` only, REVIEW lanes with a contrary verdict skipped. Reports always;
    closes only when `resolve=True`."""
    for t in tasks.values():
        if t["status"] != "blocked":
            continue
        title = t["title"] or ""
        # Scope: only the auto-routed lane prefixes, per t_c60c6a57.
        if not STALE_REF_LANE_RE.match(title):
            continue
        body = t.get("body") or ""
        # Capture the bare task id even when a board prefix is present
        # (e.g. "jarvis-os/t_349cf425"); look up by the bare id.
        refs = [m.group(1) for m in STALE_REF_ID_RE.finditer(title + "\n" + body)]
        if not refs:
            continue
        done_refs = []
        for rid in set(refs):
            row = tasks.get(rid)
            if row and row["status"] == "done":
                done_refs.append(rid)
        if not done_refs:
            continue
        # For REVIEW lanes, a contrary verdict means genuine work remains.
        if title.lower().startswith("review"):
            verdict = latest_stale_ref_verdict(comments.get(t["id"], []))
            if verdict in CONTRARY_VERDICTS:
                report.append(
                    f"SKIP [RULE4-stale-ref] {board}/{t['id']}: contrary "
                    f"verdict '{verdict}' — genuine work remains"
                )
                continue
        ref_id = done_refs[0]
        if resolve:
            resolve_stale_ref_lane(board, t, ref_id, state, dry_run, report)
        else:
            verb = "WOULD-RESOLVE(dry-run)" if dry_run else "REPORT"
            report.append(
                f"{verb} [RULE4-stale-ref] {board}/{t['id']} "
                f"(blocked, assignee={t['assignee'] or '-'}) -> stale: "
                f"references done task {ref_id}"
            )


# --- RULE 6: blocked-reference cooldown (t_69764ac8) ------------------------

def canonicalize(text: str) -> str:
    """Lowercase and keep only alphanumerics (durable residual norm). Mirrors
    governor_comment_dedupe.py's canonicalize() so the two dedupe surfaces
    fingerprint content identically — a changing timestamp/punctuation in a
    status comment must not perturb the fingerprint."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def ref_signature_hash(ref_task: dict, ref_comments: list[str], *, n_comments: int = 5) -> str:
    """sha256[:12] of canonicalize(title+body+last N comments) for the
    REFERENCED task — the content fingerprint that must stay unchanged for
    RULE 6 to suppress. Mirrors owner_packet_hash() in
    governor_comment_dedupe.py (t_3d108e24)."""
    text = (ref_task.get("title") or "") + (ref_task.get("body") or "")
    text += "".join(ref_comments[-n_comments:]) if n_comments else ""
    return hashlib.sha256(canonicalize(text).encode("utf-8")).hexdigest()[:12]


def _all_board_names() -> list[str]:
    try:
        return sorted(p.name for p in BOARDS_DIR.iterdir() if (p / "kanban.db").is_file())
    except OSError:
        return []


def _cached_board(board: str) -> dict | None:
    """Per-process cache: a scan visits many boards; avoid reloading one
    board's sqlite file repeatedly when several tasks reference it."""
    if board not in _BOARD_CACHE:
        try:
            _BOARD_CACHE[board] = load_board(board)
        except Exception:
            _BOARD_CACHE[board] = None
    return _BOARD_CACHE[board]


def resolve_ref_anywhere(ref_id: str, *, home_board: str) -> tuple[str, dict, list[str]] | None:
    """Find ref_id's task row + comments on any board (home board first).

    A blocked-reference cooldown candidate frequently cites a task on a
    DIFFERENT board (t_6177afc8 on sycode-trading references t_84f1aeda on
    jarvis-os) since task_links — the real graph — is intra-board only, so a
    cross-board mention can never be a real parent link in the first place.
    Returns None when the id cannot be found on any board (nothing to
    compare against, so the caller must treat it as a non-match)."""
    order = [home_board] + [b for b in _all_board_names() if b != home_board]
    for board in order:
        data = _cached_board(board)
        if not data:
            continue
        row = data["tasks"].get(ref_id)
        if row:
            return board, row, data["comments"].get(ref_id, [])
    return None


def last_dependency_wait_reason(board: str, task_id: str) -> str | None:
    """Most recent dependency_wait event's reason for task_id, or None.

    Covers a kind=dependency park whose reason text cites the referenced
    task without matching an explicit RULE6_REF_MARKER_RE marker word (the
    marker regex is deliberately narrow; the event payload is the ground
    truth for a real dependency-kind block)."""
    db_path = BOARDS_DIR / board / "kanban.db"
    if not db_path.is_file():
        return None
    db = open_ro(db_path)
    try:
        row = db.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'dependency_wait' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        db.close()
    if not row or not row[0]:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    reason = payload.get("reason")
    return reason if isinstance(reason, str) else None


def find_rule6_reference(board: str, task: dict, comments: list[str]) -> str | None:
    """Return the referenced task id for a RULE 6 candidate, or None.

    Preference order: explicit marker in title/body, then in the last
    comment (cheapest, most precise), then — only for an actual
    kind=dependency park — the last dependency_wait event's reason (covers
    parks whose reason text cites the ref without an explicit marker word)."""
    haystacks = [(task.get("title") or "") + "\n" + (task.get("body") or "")]
    if comments:
        haystacks.append(comments[-1])
    for hay in haystacks:
        m = RULE6_REF_MARKER_RE.search(hay)
        if m:
            return m.group(2)
    if task.get("block_kind") == "dependency":
        reason = last_dependency_wait_reason(board, task["id"])
        if reason:
            ids = STALE_REF_ID_RE.findall(reason)
            if ids:
                return ids[0]
    return None


# Statuses in which a referenced task still counts as "open" for RULE 6 —
# anything NOT in this set (done/archived, or an id that doesn't resolve at
# all) means the reference has resolved and the candidate must dispatch
# normally, never be suppressed.
RULE6_REF_OPEN_STATUSES = {"todo", "ready", "running", "blocked", "scheduled", "triage"}


def rule6_critical(task: dict, ref_task: dict, ref_comments: list[str]) -> bool:
    """Credential/approval/payment/spend/deploy/production carve-out — NEVER
    suppressed regardless of hash/TTL state, checked against both sides of
    the reference."""
    hay = " ".join([
        task.get("title") or "", task.get("body") or "",
        ref_task.get("title") or "", ref_task.get("body") or "",
        " ".join(ref_comments[-3:]),
    ])
    return bool(RULE6_CRITICAL_MARKER_RE.search(hay))


def _rule6_parse_ts(value: str) -> float:
    return calendar.timegm(time.strptime(value, RULE6_ISO_FMT))


def scan_board_blocked_ref_cooldown(board: str, *, state: dict, dry_run: bool,
                                    enforce: bool, report: list[str],
                                    tasks: dict, comments: dict) -> None:
    """RULE 6: blocked-reference cooldown (t_69764ac8).

    See module docstring RULE 6 for the full incident writeup. Conservative
    by construction: a candidate is skipped entirely (dispatch normally, no
    state write beyond the baseline) whenever the reference resolves
    (done/archived/unknown), its content signature just changed, a
    credential/approval-critical marker is present anywhere in either task,
    or this is the very first sighting of this exact (task, ref, signature)
    tuple — there is nothing yet to suppress a duplicate of. Suppression
    only fires for a SECOND-OR-LATER sighting of the identical tuple inside
    the TTL window, and even then only silences the duplicate COMMENT: the
    task is pushed back to 'todo' via a plain kind=dependency reblock (when
    it was 'ready') so a claim/spawn race loses more often — this cron is a
    best-effort throttle on cadence, not a kernel-level fix (out of
    footprint per the accepted design). `enforce=False` (the safe default)
    still writes state/report so a >=24h dry-run evidence report can be
    captured, but never calls the mutating `hermes kanban block`; flipping
    enforcement requires the same Frank sign-off as this script's existing
    rules (t_d787b0f8 / t_71d3e221)."""
    now = time.time()
    for t in tasks.values():
        if t["status"] not in ("todo", "ready"):
            continue
        cs = comments.get(t["id"], [])
        ref_id = find_rule6_reference(board, t, cs)
        if not ref_id or ref_id == t["id"]:
            continue
        resolved = resolve_ref_anywhere(ref_id, home_board=board)
        if resolved is None:
            continue  # unresolvable id — nothing to compare against
        ref_board, ref_task, ref_comments = resolved
        if ref_task["status"] not in RULE6_REF_OPEN_STATUSES:
            continue  # ref resolved -> dispatch normally, never suppress
        if rule6_critical(t, ref_task, ref_comments):
            continue  # credential/approval-critical -> never suppressed
        sig_hash = ref_signature_hash(ref_task, ref_comments)
        key = f"{RULE6_KEY_PREFIX}{board}:{t['id']}:{ref_id}:{sig_hash}"
        prior_ts = state["actions"].get(key)
        if prior_ts is None:
            # First sighting of this exact signature: record the baseline
            # only, take no other action. A genuinely new match must never
            # be silently suppressed sight-unseen.
            report.append(
                f"BASELINE [RULE6-blocked-ref-cooldown] {board}/{t['id']} "
                f"-> {ref_board}/{ref_id} sig={sig_hash}: first sighting of "
                f"this signature, dispatching normally"
            )
            if not dry_run:
                state["actions"][key] = time.strftime(RULE6_ISO_FMT, time.gmtime(now))
                save_state(state)
            continue
        try:
            age_hours = (now - _rule6_parse_ts(prior_ts)) / 3600.0
        except ValueError:
            age_hours = RULE6_TTL_HOURS  # unparsable timestamp -> fail open, treat as expired
        if age_hours >= RULE6_TTL_HOURS:
            # TTL elapsed: exactly one fresh pass-through to refresh the
            # baseline, then resume suppressing on the next sighting.
            report.append(
                f"TTL-REFRESH [RULE6-blocked-ref-cooldown] {board}/{t['id']} "
                f"-> {ref_board}/{ref_id} sig={sig_hash}: {age_hours:.1f}h >= "
                f"{RULE6_TTL_HOURS}h TTL, one fresh dispatch allowed through"
            )
            if not dry_run:
                state["actions"][key] = time.strftime(RULE6_ISO_FMT, time.gmtime(now))
                save_state(state)
            continue
        verb = "WOULD-SUPPRESS(dry-run)" if dry_run else "SUPPRESS"
        report.append(
            f"{verb} [RULE6-blocked-ref-cooldown] {board}/{t['id']} "
            f"({t['status']}) -> {ref_board}/{ref_id} unchanged sig={sig_hash} "
            f"({age_hours:.1f}h < {RULE6_TTL_HOURS}h TTL): suppressing duplicate "
            f"re-diagnosis comment"
            + (", reblocking kind=dependency" if t["status"] == "ready" else "")
        )
        if dry_run:
            continue
        if not enforce:
            report.append(
                f"SKIPPED (enforcement not enabled; pass "
                f"--enforce-blocked-ref-cooldown) [RULE6-blocked-ref-cooldown] "
                f"{board}/{t['id']}"
            )
            continue
        if t["status"] == "ready":
            # Argument ORDER matters here too (see act()): the reason is a
            # positional with nargs='*' and MUST come before --kind.
            ok = run_hermes(
                ["kanban", "--board", board, "block", t["id"],
                 f"[{GUARD_AUTHOR}] RULE6-blocked-ref-cooldown: still waiting "
                 f"on {ref_board}/{ref_id} (unchanged evidence, key={key})",
                 "--kind", "dependency"]
            )
            if not ok:
                report.append(
                    f"ERROR: RULE6 reblock failed for {board}/{t['id']}"
                )


# --- core scan ---------------------------------------------------------------

def scan_board(board: str, *, include_archived: bool, assume_blocked: set[str],
               state: dict, dry_run: bool, report: list[str],
               resolve_stale_refs: bool = False,
               enforce_blocked_ref_cooldown: bool = False) -> None:
    data = load_board(board)
    if data is None:
        report.append(f"ERROR: board '{board}' has no kanban.db")
        return
    tasks, comments, links = data["tasks"], data["comments"], data["links"]
    _BOARD_CACHE[board] = data  # seed RULE 6's cross-board cache; avoid a reload

    gate_blocked = {
        t["id"]: t for t in tasks.values()
        if is_gate_blocked(t, comments.get(t["id"], [])) or t["id"] in assume_blocked
    }
    sigs = {
        tid: extract_signature(t["title"] + "\n" + t["body"])
        for tid, t in tasks.items()
    }
    title_sigs = {
        tid: title_tokens(t["title"])
        for tid, t in tasks.items()
        if recent_non_archived(t)
    }

    candidate_statuses = ACTIVE_STATUSES + (("done", "archived", "blocked")
                                            if include_archived else ())
    for t in tasks.values():
        if t["status"] not in candidate_statuses:
            continue

        # RULE 1: dupe of a currently gate-blocked task
        for bid, blocked in gate_blocked.items():
            if t["id"] == bid:
                continue
            if (bid, t["id"]) in links or (t["id"], bid) in links:
                continue  # properly linked — exempt
            # explicit mention of the blocked id counts as a shared token
            mention = 1 if bid in (t["title"] + t["body"]) else 0
            n_ids, n_total, n_classes, tokens = overlap(sigs[t["id"]], sigs[bid])
            n_ids += mention
            n_total += mention
            if mention:
                tokens = [bid] + tokens
                n_classes += 1
            # HIGH: shared explicit task id + anything else; or 2+ markers of
            # distinct classes (e.g. same file AND same error string — the
            # t_5c25f222/t_d0fcaddb incident shape); or 3+ markers overall.
            high = ((n_ids >= 1 and n_total >= 2) or
                    (n_total >= 2 and n_classes >= 2) or n_total >= 3)
            medium = n_total == 2 and not high
            if not (high or medium):
                continue
            conf = "HIGH" if high else "MEDIUM"
            reason = (
                f"shares failure signature with gate-blocked {bid} "
                f"('{blocked['title'][:70]}') — {n_total} shared markers "
                f"[{conf}]: {', '.join(tokens[:5])}"
            )
            if high:
                act(board, t, "RULE1-dupe-of-gate-blocked", reason,
                    state, dry_run, report)
            else:
                # medium: comment/alarm only, never block
                t_ro = dict(t, status="running")  # force comment-path
                act(board, t_ro, "RULE1-dupe-suspect", reason,
                    state, dry_run, report)
            break  # one finding per task is enough

        # RULE 2: Frank-gated work on a non-gate-honoring profile.
        # Active tasks only — this is a pre-dispatch guard, not a historian.
        if t["status"] in ACTIVE_STATUSES:
            m = GATE_STRONG_RE.search(t["title"] + "\n" + t["body"])
            if m and t["assignee"] and not profile_honors_gate(t["assignee"]):
                reason = (
                    f"body carries Frank-gate marker '{m.group(0)}' but assignee "
                    f"'{t['assignee']}' has no Frank-escalation language in its "
                    f"SOUL.md — gated work must go to a gate-honoring profile"
                )
                act(board, t, "RULE2-gated-to-gateless-profile", reason,
                    state, dry_run, report)

        # RULE 3: title-token duplicate window (same board, recent, non-archived).
        # Active candidates only. Linked parent/child pairs are legitimate
        # decomposition and are exempt. MEDIUM is comment-only; HIGH blocks
        # todo/ready and comments running.
        if t["status"] in ACTIVE_STATUSES:
            toks = title_sigs.get(t["id"], set())
            if len(toks) >= 2:
                for other_id, other_toks in title_sigs.items():
                    if other_id == t["id"]:
                        continue
                    if (other_id, t["id"]) in links or (t["id"], other_id) in links:
                        continue
                    if len(other_toks) < 2:
                        continue
                    score = jaccard(toks, other_toks)
                    if score < TITLE_MEDIUM_THRESHOLD:
                        continue
                    other = tasks[other_id]
                    shared = sorted(toks & other_toks)
                    high = (
                        (toks == other_toks or score >= TITLE_HIGH_THRESHOLD)
                        and title_high_allowed(t["title"], other["title"], toks, other_toks)
                    )
                    conf = "HIGH" if high else "MEDIUM"
                    reason = (
                        f"title-token duplicate window matched {other_id} "
                        f"('{other['title'][:70]}') at J={score:.2f} [{conf}]; "
                        f"shared tokens: {', '.join(shared[:8])}"
                    )
                    if high:
                        act(board, t, "RULE3-title-token-duplicate", reason,
                            state, dry_run, report)
                    else:
                        t_ro = dict(t, status="running")  # force comment-path
                        act(board, t_ro, "RULE3-title-token-suspect", reason,
                            state, dry_run, report)
                    break

    # RULE 4: stale-reference blocked lanes (phantom blockers that
    # reference an already-done source task). Conservative: report-only
    # unless resolve=True. Reuses the shared state/comment/complete path.
    scan_board_stale_refs(
        board, state=state, dry_run=dry_run, resolve=resolve_stale_refs,
        report=report, tasks=tasks, comments=comments,
    )

    # RULE 6: blocked-reference cooldown (an active task parked on a still-
    # open, content-unchanged prose reference is a phantom-redispatch
    # candidate). Report/baseline-only unless --enforce-blocked-ref-cooldown
    # is explicitly passed (Frank-approval-gated per the accepted design,
    # same precedent as RULE 4's --resolve-stale-refs).
    scan_board_blocked_ref_cooldown(
        board, state=state, dry_run=dry_run,
        enforce=enforce_blocked_ref_cooldown,
        report=report, tasks=tasks, comments=comments,
    )


def hook_check() -> None:
    """Read pre_tool_call payload on stdin. Print block reason to stdout to
    block; print nothing to allow. Always exit 0 (fail-open)."""
    try:
        payload = json.load(sys.stdin)
        ti = payload.get("tool_input") or {}
        title = str(ti.get("title") or "")
        body = str(ti.get("body") or "")
        assignee = str(ti.get("assignee") or "")
        board_hint = str(ti.get("board") or "")
    except Exception:
        return  # allow
    if not (title or body):
        return

    ra_reason = research_actionable_block_reason(title, body, assignee)
    if ra_reason:
        print(ra_reason)
        return

    new_sig = extract_signature(title + "\n" + body)
    boards = [board_hint] if board_hint else [
        p.name for p in BOARDS_DIR.iterdir()
        if (p / "kanban.db").is_file()
    ]
    for board in boards:
        try:
            data = load_board(board)
        except Exception:
            continue
        if not data:
            continue
        for t in data["tasks"].values():
            if not is_gate_blocked(t, data["comments"].get(t["id"], [])):
                continue
            mention = 1 if t["id"] in (title + body) else 0
            n_ids, n_total, n_classes, tokens = overlap(
                new_sig, extract_signature(t["title"] + "\n" + t["body"]))
            n_ids += mention
            n_total += mention
            if mention:
                n_classes += 1
            if ((n_ids >= 1 and n_total >= 2) or
                    (n_total >= 2 and n_classes >= 2) or n_total >= 3):
                print(
                    f"BLOCKED by {GUARD_AUTHOR}: this new task duplicates "
                    f"gate-blocked {t['id']} on board '{board}' "
                    f"(shared markers: {', '.join(([t['id']] if mention else []) + tokens[:4])}). "
                    f"Cloning a gate-blocked task onto another profile is how the "
                    f"2026-07-05 production-DDL bypass happened. Instead: comment on "
                    f"{t['id']}, or create a child and `hermes kanban link` it, or "
                    f"escalate to Frank to unblock. Ref: {INCIDENT_NOTE}"
                )
                return
        new_title_tokens = title_tokens(title)
        if len(new_title_tokens) >= 2:
            for t in data["tasks"].values():
                if not recent_non_archived(t):
                    continue
                old_tokens = title_tokens(t["title"])
                if len(old_tokens) < 2:
                    continue
                score = jaccard(new_title_tokens, old_tokens)
                if (
                    (new_title_tokens == old_tokens or score >= TITLE_HIGH_THRESHOLD)
                    and title_high_allowed(title, t["title"], new_title_tokens, old_tokens)
                ):
                    shared = ", ".join(sorted(new_title_tokens & old_tokens)[:8])
                    print(
                        f"BLOCKED by {GUARD_AUTHOR}: this new task is a HIGH "
                        f"title-token duplicate of {t['id']} on board '{board}' "
                        f"(J={score:.2f}; shared tokens: {shared}). Link/comment "
                        f"the existing card instead of cloning. False positive? "
                        f"Archive the original before refiling, or create a linked child. "
                        f"Ref: {INCIDENT_NOTE}"
                    )
                    return
    # RULE 2 at create time: gated body -> gateless assignee
    m = GATE_STRONG_RE.search(title + "\n" + body)
    if m and assignee and not profile_honors_gate(assignee):
        print(
            f"BLOCKED by {GUARD_AUTHOR}: task body carries Frank-gate marker "
            f"'{m.group(0)}' but assignee '{assignee}' has no Frank-escalation "
            f"language in its SOUL.md. Assign gated work to a gate-honoring "
            f"profile (e.g. trading-devops) or escalate to Frank. Ref: {INCIDENT_NOTE}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--boards", default=",".join(DEFAULT_BOARDS))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--assume-blocked", action="append", default=[])
    ap.add_argument("--hook-check", action="store_true")
    ap.add_argument("--resolve-stale-refs", action="store_true",
                    help="RULE 4: auto-close blocked lanes referencing a done "
                         "task (default: report-only, no board mutation).")
    ap.add_argument("--enforce-blocked-ref-cooldown", action="store_true",
                    help="RULE 6: reblock (kind=dependency) an active task "
                         "whose reference is unchanged within the TTL "
                         "(default: report/baseline-only, no board mutation; "
                         "Frank-approval-gated per accepted design t_69764ac8).")
    args = ap.parse_args()

    if args.hook_check:
        hook_check()
        return 0

    if args.boards == "all":
        boards = sorted(p.name for p in BOARDS_DIR.iterdir()
                        if (p / "kanban.db").is_file())
    else:
        boards = [b.strip() for b in args.boards.split(",") if b.strip()]

    state = load_state()
    report: list[str] = []
    for board in boards:
        try:
            scan_board(board, include_archived=args.include_archived,
                       assume_blocked=set(args.assume_blocked),
                       state=state, dry_run=args.dry_run, report=report,
                       resolve_stale_refs=args.resolve_stale_refs,
                       enforce_blocked_ref_cooldown=args.enforce_blocked_ref_cooldown)
        except Exception as e:  # never wedge the cron
            report.append(f"ERROR scanning {board}: {e!r}")

    if report:
        print(f"kanban-dedupe-guard findings ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}):")
        for line in report:
            print(" - " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
