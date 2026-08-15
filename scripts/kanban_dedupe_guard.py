#!/usr/bin/env python3
"""kanban_dedupe_guard.py — Kanban dedupe + Frank-gate dispatch guard.

Born from the 2026-07-05 out-of-band production DDL incident
(fleet vault: Governance/incidents/2026-07-05-out-of-band-production-ddl-incident.md):
sycode-trading-pm cloned gate-blocked t_5c25f222 into t_d0fcaddb on a profile
without the gate, and that profile applied production DDL out-of-band.

Five deterministic rules (no LLM):

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
    for tid, title, body, assignee, status, created_at in db.execute(
        "SELECT id, title, COALESCE(body,''), COALESCE(assignee,''), "
        "status, COALESCE(created_at,0) FROM tasks"
    ):
        tasks[tid] = {
            "id": tid, "title": title, "body": body,
            "assignee": assignee, "status": status,
            "created_at": created_at,
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
    blockable = task["status"] in ("todo", "ready")
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
        ok &= run_hermes(
            ["kanban", "--board", board, "block", task["id"],
             "--kind", "needs_input", f"[{GUARD_AUTHOR}] {rule}: {reason}"]
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


# --- core scan ---------------------------------------------------------------

def scan_board(board: str, *, include_archived: bool, assume_blocked: set[str],
               state: dict, dry_run: bool, report: list[str],
               resolve_stale_refs: bool = False) -> None:
    data = load_board(board)
    if data is None:
        report.append(f"ERROR: board '{board}' has no kanban.db")
        return
    tasks, comments, links = data["tasks"], data["comments"], data["links"]

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
                       resolve_stale_refs=args.resolve_stale_refs)
        except Exception as e:  # never wedge the cron
            report.append(f"ERROR scanning {board}: {e!r}")

    if report:
        print(f"kanban-dedupe-guard findings ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}):")
        for line in report:
            print(" - " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
