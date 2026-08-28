#!/usr/bin/env python3
"""frank_gate premise-activity probe — RECOMMEND-ONLY.

Task: jarvis-os/t_e911f789 (build on t_27c22567 needs_input SLA auto-route).
Reviewer contract: jarvis-os/t_3e637466 + os-reviewer CHANGES_REQUESTED on t_e911f789.

WHAT THIS DOES
    For every blocked card carrying block_kind='frank_gate', classify the cited
    premise, then look for board activity AFTER the card was raised that
    contradicts it. Emit a RETIRE *recommendation* with the contradicting
    evidence lines, or an explicit DECLINE with the reason it could not be
    fully falsified.

WHAT THIS DELIBERATELY DOES NOT DO  (os-reviewer CHANGES_REQUESTED, 2026-08-03)
    * No status mutation. Ever. There is no auto-retire code path in this file.
      Card mutation stays a separate PM/Frank-lane step gated on an explicit
      per-card os-reviewer sign-off (the pattern actually used for t_4776f5c9
      via t_6aa6dabc + t_3e637466).
    * No writes of any kind: every DB handle is opened sqlite3 mode=ro.
    * A future auto-mutate mode is NOT built here, not even disabled. Building
      it would require its own Frank approval.

WHY A PINNED QUERY (os-reviewer required correction #3)
    Three agents produced three counts (21 / 26 / 27) for "the same metric" on
    t_4776f5c9. None of them was wrong arithmetic; they were three different
    queries. See CANONICAL_ACTIVITY_SQL and the --explain-variance mode.

Usage:
    python3 frank_gate_premise_probe.py                 # dry-run, both boards
    python3 frank_gate_premise_probe.py --canary        # replay t_4776f5c9
    python3 frank_gate_premise_probe.py --explain-variance
    python3 frank_gate_premise_probe.py --regression    # os-reviewer t_c8dfff2e regression suite
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

BOARDS_ROOT = "/home/frank/.hermes/kanban/boards"
DEFAULT_BOARDS = ["jarvis-os", "sycode-trading"]

# Window the premise is tested over: activity in the N seconds AFTER the raise.
ACTIVITY_WINDOW_S = 86_400

# Minimum post-raise completions before an activity claim counts as falsified.
# Deliberately not 1: a single completion can be a straggler flushed by a
# reclaim, not proof the cited service was live.
MIN_ACTIVITY_FOR_FALSIFICATION = 5

TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{6,}\b")

# ---------------------------------------------------------------------------
# THE PINNED CANONICAL EVIDENCE QUERY
# ---------------------------------------------------------------------------
# Exactly one query is allowed to produce the number that appears in a
# recommendation. It is bounded on BOTH sides and the upper bound (as_of) is
# always printed alongside the count, because the raise+24h window can still be
# open when the probe runs — an unbounded-above count is not reproducible.
#
#   raise_epoch  = tasks.created_at of the frank_gate card (its OWN board)
#   as_of_epoch  = min(now, raise_epoch + ACTIVITY_WINDOW_S)   [always printed]
#
# Re-running with the same (subject, raise_epoch, as_of_epoch) triple returns
# the same integer forever. That is the reproducibility bar for retiring a
# Frank gate.
CANONICAL_ACTIVITY_SQL = """
SELECT count(*)
  FROM tasks
 WHERE assignee     = :subject
   AND status       = 'done'
   AND completed_at >= :raise_epoch
   AND completed_at <= :as_of_epoch
"""

CANONICAL_ACTIVITY_ROWS_SQL = """
SELECT id, datetime(completed_at, 'unixepoch')
  FROM tasks
 WHERE assignee     = :subject
   AND status       = 'done'
   AND completed_at >= :raise_epoch
   AND completed_at <= :as_of_epoch
 ORDER BY completed_at DESC
 LIMIT :limit
"""

# ---------------------------------------------------------------------------
# Premise classification
# ---------------------------------------------------------------------------
# Natural-language extraction is lossy. os-reviewer's point 3 is that a lossy
# parse must never be the thing that clears a Frank gate. So the classifier is
# fail-closed: anything it is not certain about becomes a DECLINE, and the
# authorization class can never be recommended no matter what activity exists.

SERVICE_CLAIM_PATTERNS = [
    re.compile(r"\b(?:start|restart|bring[- ]up)\s+(?P<subject>[a-z0-9][a-z0-9._-]{2,})\s+gateway", re.I),
    re.compile(r"\b(?P<subject>[a-z0-9][a-z0-9._-]{2,})\s+gateway\s+(?:is\s+)?(?:down|dead|stopped|offline)", re.I),
    re.compile(r"\b(?P<subject>[a-z0-9][a-z0-9._-]{2,})\s+(?:is\s+)?(?:stopped|dead|down)\b.*\bgateway", re.I),
]

SOLE_CAUSE_RE = re.compile(r"\b(?:sole|only|single|the)\s+(?:active\s+)?(?:root\s+)?cause\b", re.I)

# An authorization premise is unfalsifiable by observed activity: no amount of
# fleet throughput turns "Frank must approve this chmod" into "already approved".
# Fail-closed: `authoriz\w*` covers noun/verb forms (authorization/authorized/
# authorize) — the old literal `authoriz\b` made the whole branch dead because
# "ation"/"e" continue the word, letting an authorization premise fall through
# to the service-activity class and RECOMMEND-RETIRE (os-reviewer t_c8dfff2e).
# Also cover apostrophe-s ("Frank's approval") and bare "A3 approval"/"A3 sign-off".
AUTHORIZATION_RE = re.compile(
    r"\b(?:await\s+frank"
    r"|frank(?:['’]s)?\s+(?:exact\s+)?(?:approval|authoriz\w*|sign[- ]off|decision)"
    r"|explicit\s+frank"
    r"|frank/a3"
    r"|a3\s+(?:explicit\s+)?(?:authoriz\w*|approval|sign[- ]off)"
    r"|requires?\s+frank)\b",
    re.I,
)

DEPENDENCY_RE = re.compile(
    r"\b(?:depends?\s+on|await(?:s|ing)?|blocked\s+until|triggered\s+only\s+after"
    r"|auto[- ]promotes?\s+once|gated\s+on)\b",
    re.I,
)


@dataclass
class Finding:
    board: str
    task_id: str
    title: str
    raise_epoch: int
    as_of_epoch: int
    verdict: str                       # RECOMMEND-RETIRE | DECLINE
    premise_class: str
    reason: str
    subject: str | None = None
    activity: dict[str, int] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    negative_control: str = "unknown"
    declined_because: str | None = None


def utc(epoch: int | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def board_db(board: str) -> str:
    return f"{BOARDS_ROOT}/{board}/kanban.db"


def ro_connect(board: str) -> sqlite3.Connection:
    """Read-only handle. mode=ro makes a write attempt raise, not silently apply."""
    conn = sqlite3.connect(f"file:{board_db(board)}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only = ON")
    return conn


# ---------------------------------------------------------------------------
# Negative control (os-reviewer required correction #4)
# ---------------------------------------------------------------------------
_PROFILE_STATE_CACHE: dict[str, str] | None = None


def profile_states() -> dict[str, str]:
    """Current runtime gateway state per profile, from `hermes profile list`.

    Every recommendation prints this for the cited subject so a reviewer can
    see when a premise is only PARTLY falsified — e.g. t_4776f5c9, where the
    *sole-cause* claim was false but the *service-stopped* observation was
    (and still is) true.
    """
    global _PROFILE_STATE_CACHE
    if _PROFILE_STATE_CACHE is not None:
        return _PROFILE_STATE_CACHE
    states: dict[str, str] = {}
    exe = shutil.which("hermes")
    if exe:
        try:
            out = subprocess.run(
                [exe, "profile", "list"], capture_output=True, text=True, timeout=90
            ).stdout
            for line in out.splitlines():
                m = re.match(r"\s*[◆*]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s+.*?\b(running|stopped)\b", line)
                if m:
                    states[m.group(1)] = m.group(2)
        except Exception as exc:                                   # pragma: no cover
            states["__error__"] = str(exc)
    else:
        states["__error__"] = "hermes not on PATH"
    _PROFILE_STATE_CACHE = states
    return states


def negative_control_line(subject: str | None) -> str:
    if not subject:
        return "n/a (no service subject parsed)"
    states = profile_states()
    if "__error__" in states and subject not in states:
        return f"UNAVAILABLE (hermes profile list: {states['__error__']})"
    state = states.get(subject)
    if state is None:
        return f"`hermes profile list`: no row for '{subject}' (not a profile, or not registered)"
    return f"`hermes profile list`: {subject} = {state}  [live, {utc(int(time.time()))}]"


# ---------------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------------
def activity_for_subject(
    subject: str, raise_epoch: int, as_of_epoch: int, boards: list[str]
) -> tuple[dict[str, int], list[str]]:
    """Run the PINNED query per board. Returns per-board counts + evidence rows."""
    counts: dict[str, int] = {}
    evidence: list[str] = []
    params = {"subject": subject, "raise_epoch": raise_epoch, "as_of_epoch": as_of_epoch}
    for b in boards:
        try:
            with ro_connect(b) as conn:
                n = conn.execute(CANONICAL_ACTIVITY_SQL, params).fetchone()[0]
                counts[b] = n
                if n:
                    rows = conn.execute(
                        CANONICAL_ACTIVITY_ROWS_SQL, {**params, "limit": 3}
                    ).fetchall()
                    for tid, when in rows:
                        lag_h = 0.0
                        with ro_connect(b) as c2:
                            ca = c2.execute(
                                "SELECT completed_at FROM tasks WHERE id=?", (tid,)
                            ).fetchone()
                        if ca and ca[0]:
                            lag_h = (ca[0] - raise_epoch) / 3600.0
                        evidence.append(
                            f"{b}/{tid} done {when} UTC (+{lag_h:.1f}h after raise) by {subject}"
                        )
        except sqlite3.Error as exc:
            counts[b] = -1
            evidence.append(f"{b}: DB ERROR {exc}")
    return counts, evidence


def cited_task_states(text: str, self_id: str, boards: list[str]) -> list[tuple[str, str, str]]:
    """Resolve every t_xxxx id cited in the premise to (board, id, status)."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for tid in TASK_ID_RE.findall(text or ""):
        if tid == self_id or tid in seen:
            continue
        seen.add(tid)
        for b in boards:
            try:
                with ro_connect(b) as conn:
                    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
                if row:
                    out.append((b, tid, row[0]))
                    break
            except sqlite3.Error:
                continue
    return out


# ---------------------------------------------------------------------------
# The decision rule
# ---------------------------------------------------------------------------
def evaluate(board: str, task: sqlite3.Row, boards: list[str]) -> Finding:
    """Fail-closed. DECLINE is the default; RECOMMEND-RETIRE must be earned."""
    tid = str(task["id"])
    title = (task["title"] or "").strip()
    body = task["body"] or ""
    text = f"{title}\n{body}"
    raise_epoch = int(task["created_at"])
    as_of_epoch = min(int(time.time()), raise_epoch + ACTIVITY_WINDOW_S)

    def new_finding(**kw) -> Finding:
        return Finding(
            board=board, task_id=tid, title=title,
            raise_epoch=raise_epoch, as_of_epoch=as_of_epoch, **kw
        )

    # ---- Class A: authorization premise. Never recommendable. --------------
    if AUTHORIZATION_RE.search(text):
        f = new_finding(
            verdict="DECLINE", premise_class="authorization",
            reason="Premise is an authorization requirement, not an activity claim.",
            declined_because=(
                "No volume of observed fleet activity can falsify 'Frank must approve X'. "
                "Activity probing is the wrong instrument; this card is a real Frank gate."
            ),
        )
        # A cited dependency may still have moved — surface it, do NOT act on it.
        deps = cited_task_states(text, tid, boards)
        if deps:
            done = [d for d in deps if d[2] == "done"]
            f.evidence = [f"cited {b}/{t} status={s}" for b, t, s in deps]
            if done:
                f.reason += (
                    f" NOTE: {len(done)}/{len(deps)} cited prerequisite(s) are now done "
                    "— premise is PARTLY falsified, which is exactly why this is not auto-retirable."
                )
        return f

    # ---- Class B: service/gateway activity claim ---------------------------
    subject = None
    for pat in SERVICE_CLAIM_PATTERNS:
        m = pat.search(text)
        if m:
            subject = m.group("subject").lower().strip(".,;:")
            break

    if subject:
        counts, evidence = activity_for_subject(subject, raise_epoch, as_of_epoch, boards)
        total = sum(v for v in counts.values() if v > 0)
        neg = negative_control_line(subject)
        sole = bool(SOLE_CAUSE_RE.search(text))

        f = new_finding(
            premise_class="service_activity", subject=subject,
            activity=counts, evidence=evidence, negative_control=neg,
            verdict="DECLINE", reason="",
        )

        if not sole:
            f.reason = (
                f"Cited service '{subject}' shows {total} post-raise completion(s), "
                "but the premise makes no sole/only-cause claim."
            )
            f.declined_because = (
                "Activity falsifies a SOLE-CAUSE claim, not a service-state observation. "
                f"Negative control still reads: {neg}. Retiring here would discard a "
                "premise that may be substantively true."
            )
            return f

        if total < MIN_ACTIVITY_FOR_FALSIFICATION:
            f.reason = (
                f"Sole-cause claim about '{subject}', but only {total} post-raise "
                f"completion(s) (< MIN_ACTIVITY_FOR_FALSIFICATION={MIN_ACTIVITY_FOR_FALSIFICATION})."
            )
            f.declined_because = "Insufficient contradicting activity to call the premise falsified."
            return f

        f.verdict = "RECOMMEND-RETIRE"
        f.reason = (
            f"Sole-cause claim about '{subject}' is contradicted: {total} task(s) completed "
            f"by '{subject}' in [{utc(raise_epoch)} .. {utc(as_of_epoch)}] "
            f"(per-board {json.dumps(counts)})."
        )
        f.declined_because = None
        return f

    # ---- Class C: dependency premise ---------------------------------------
    if DEPENDENCY_RE.search(text):
        deps = cited_task_states(text, tid, boards)
        done = [d for d in deps if d[2] == "done"]
        f = new_finding(
            verdict="DECLINE", premise_class="dependency",
            reason=(
                f"Dependency premise; {len(done)}/{len(deps)} cited prerequisite(s) done."
                if deps else "Dependency premise with no resolvable cited task ids."
            ),
            evidence=[f"cited {b}/{t} status={s}" for b, t, s in deps],
            declined_because=(
                "Dependency satisfaction is not an activity contradiction. A satisfied "
                "prerequisite means the card may be READY, not that its gate was false — "
                "route to the owning PM, do not retire."
            ),
        )
        return f

    # ---- Class D: unclassified ---------------------------------------------
    return new_finding(
        verdict="DECLINE", premise_class="unclassified",
        reason="No activity-shaped premise could be extracted.",
        declined_because="Fail-closed: an unparsed premise is never auto-recommendable.",
    )


def scan(boards: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for b in boards:
        try:
            with ro_connect(b) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, title, body, assignee, created_at, status, block_kind "
                    "  FROM tasks WHERE block_kind='frank_gate' AND status='blocked' "
                    " ORDER BY created_at"
                ).fetchall()
        except sqlite3.Error as exc:
            print(f"!! {b}: cannot open board DB: {exc}", file=sys.stderr)
            continue
        for r in rows:
            findings.append(evaluate(b, r, boards))
    return findings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(findings: list[Finding]) -> str:
    L: list[str] = []
    ret = [f for f in findings if f.verdict == "RECOMMEND-RETIRE"]
    dec = [f for f in findings if f.verdict == "DECLINE"]
    L.append("=" * 78)
    L.append("frank_gate PREMISE-ACTIVITY PROBE — DRY RUN (recommend-only, zero writes)")
    L.append(f"run_at         : {utc(int(time.time()))}")
    L.append(f"boards         : {', '.join(DEFAULT_BOARDS)}")
    L.append(f"cards examined : {len(findings)}  (status=blocked AND block_kind='frank_gate')")
    L.append(f"RECOMMEND-RETIRE: {len(ret)}    DECLINE: {len(dec)}")
    L.append("=" * 78)
    for f in findings:
        L.append("")
        L.append(f"[{f.verdict}] {f.board}/{f.task_id}  class={f.premise_class}")
        L.append(f"    title      : {f.title[:100]}")
        L.append(f"    raised     : {utc(f.raise_epoch)}")
        L.append(f"    as_of      : {utc(f.as_of_epoch)}   (pinned upper bound; count is reproducible at this instant)")
        if f.subject:
            L.append(f"    subject    : {f.subject}")
            L.append(f"    activity   : {json.dumps(f.activity)}  (PINNED query)")
        L.append(f"    finding    : {f.reason}")
        for e in f.evidence[:4]:
            L.append(f"      evidence : {e}")
        L.append(f"    NEG-CONTROL: {f.negative_control}")
        if f.declined_because:
            L.append(f"    declined   : {f.declined_because}")
    L.append("")
    L.append("-" * 78)
    L.append("NO CARD WAS MUTATED. This probe has no status-write code path.")
    L.append("Retirement of any RECOMMEND-RETIRE card requires a per-card os-reviewer")
    L.append("sign-off in the PM/Frank lane (pattern: t_6aa6dabc + t_3e637466).")
    L.append("-" * 78)
    return "\n".join(L)


def recommendation_comment(f: Finding) -> str:
    """The exact comment format the probe would post. Reviewer signs off on THIS."""
    return "\n".join([
        f"FRANK_GATE PREMISE PROBE — {f.verdict} (recommend-only; no status change made)",
        "",
        f"Card       : {f.board}/{f.task_id}",
        f"Raised     : {utc(f.raise_epoch)}",
        f"As-of      : {utc(f.as_of_epoch)}   <- pinned upper bound; re-running with this",
        "             as_of reproduces the count below exactly, forever.",
        f"Premise    : {f.premise_class}" + (f" (subject='{f.subject}')" if f.subject else ""),
        "",
        "CONTRADICTING EVIDENCE (pinned canonical query):",
        f"  {f.reason}",
        *[f"  - {e}" for e in f.evidence[:5]],
        "",
        "NEGATIVE CONTROL (current runtime state of the cited service):",
        f"  {f.negative_control}",
        "  ^ If this still shows the cited service down/stopped, the premise is only",
        "    PARTLY falsified. A falsified sole-cause claim is not a falsified",
        "    service-state observation.",
        "",
        "ACTION: none taken. This is a recommendation to the PM/Frank lane only.",
        "Retirement requires a per-card os-reviewer sign-off (t_6aa6dabc + t_3e637466).",
        "Probe: jarvis-os/t_e911f789 (read-only; sqlite mode=ro; no mutation path exists).",
    ])


# ---------------------------------------------------------------------------
# Canary: would this have caught t_4776f5c9?
# ---------------------------------------------------------------------------
CANARY_ID = "t_4776f5c9"
CANARY_BOARD = "jarvis-os"
# The instant the PM posted the retirement comment on t_4776f5c9 (comment
# created_at). Pinning as_of here is what makes "21" reproducible.
CANARY_AS_OF = 1785716533          # 2026-08-03 00:22:13 UTC
CANARY_EXPECTED = 21


def canary() -> int:
    print("=" * 78)
    print("CANARY — replay t_4776f5c9 (the gate that had no retirement path)")
    print("=" * 78)
    with ro_connect(CANARY_BOARD) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id,title,body,assignee,created_at,status,block_kind FROM tasks WHERE id=?",
            (CANARY_ID,),
        ).fetchone()
    if row is None:
        print(f"FAIL: {CANARY_ID} not found on {CANARY_BOARD}")
        return 1

    raise_epoch = int(row["created_at"])
    print(f"card       : {CANARY_BOARD}/{CANARY_ID}  (now status={row['status']}, block_kind={row['block_kind']})")
    print(f"title      : {row['title']}")
    print(f"raised     : {utc(raise_epoch)}  (epoch {raise_epoch})")
    print(f"as_of      : {utc(CANARY_AS_OF)}  (epoch {CANARY_AS_OF}) <- retirement instant, pinned")
    print()

    # Re-run the classifier exactly as the live scan would, but with the
    # historical as_of so the number is the one the decision was made on.
    subject = None
    text = f"{row['title']}\n{row['body'] or ''}"
    for pat in SERVICE_CLAIM_PATTERNS:
        m = pat.search(text)
        if m:
            subject = m.group("subject").lower().strip(".,;:")
            break
    if not subject:
        print("FAIL: classifier did not extract a service subject — canary would MISS.")
        return 1
    sole = bool(SOLE_CAUSE_RE.search(text))
    print(f"classifier : premise_class=service_activity subject='{subject}' sole_cause={sole}")

    counts, evidence = activity_for_subject(subject, raise_epoch, CANARY_AS_OF, DEFAULT_BOARDS)
    sycode = counts.get("sycode-trading", 0)
    print(f"PINNED query counts: {json.dumps(counts)}")
    for e in evidence[:5]:
        print(f"  evidence : {e}")
    print(f"NEG-CONTROL: {negative_control_line(subject)}")
    print()

    ok_count = sycode == CANARY_EXPECTED
    would_recommend = bool(subject) and sole and sum(v for v in counts.values() if v > 0) >= MIN_ACTIVITY_FOR_FALSIFICATION
    print(f"count reproduction : sycode-trading={sycode} expected={CANARY_EXPECTED} -> {'PASS' if ok_count else 'FAIL'}")
    print(f"verdict            : {'RECOMMEND-RETIRE' if would_recommend else 'DECLINE'} -> "
          f"{'PASS (would have caught it)' if would_recommend else 'FAIL (would have missed it)'}")

    # t_0e693411 is the specific row the retirement cited.
    with ro_connect("sycode-trading") as conn:
        r = conn.execute(
            "SELECT id,status,assignee,completed_at FROM tasks WHERE id='t_0e693411'"
        ).fetchone()
    if r:
        lag = (r[3] - raise_epoch) / 3600.0
        cited_ok = r[1] == "done" and r[2] == subject
        print(f"cited row t_0e693411: status={r[1]} assignee={r[2]} completed={utc(r[3])} "
              f"(+{lag:.2f}h after raise) -> {'PASS' if cited_ok else 'FAIL'}")
    else:
        cited_ok = False
        print("cited row t_0e693411: NOT FOUND -> FAIL")

    print()
    print("--- recommendation comment the probe would have posted ---")
    f = Finding(
        board=CANARY_BOARD, task_id=CANARY_ID, title=row["title"],
        raise_epoch=raise_epoch, as_of_epoch=CANARY_AS_OF,
        verdict="RECOMMEND-RETIRE", premise_class="service_activity",
        subject=subject, activity=counts, evidence=evidence,
        negative_control=negative_control_line(subject),
        reason=(f"Sole-cause claim about '{subject}' is contradicted: "
                f"{sum(v for v in counts.values() if v > 0)} task(s) completed by '{subject}' in "
                f"[{utc(raise_epoch)} .. {utc(CANARY_AS_OF)}] (per-board {json.dumps(counts)})."),
    )
    print(recommendation_comment(f))
    return 0 if (ok_count and would_recommend and cited_ok) else 1


# ---------------------------------------------------------------------------
# Variance explainer — why 21 vs 26 vs 27
# ---------------------------------------------------------------------------
def explain_variance() -> int:
    db = "sycode-trading"
    raise_epoch = 1785686398          # t_4776f5c9 created_at, jarvis-os board
    wrong_epoch = 1785671998          # epoch quoted in the os-reviewer comment
    subject = "trading-risk-reviewer"

    print("=" * 78)
    print("WHY THREE AGENTS GOT THREE COUNTS (21 / 26 / 27) FOR 'THE SAME' METRIC")
    print("=" * 78)
    print("None of them mis-counted. They ran three different queries.\n")

    def q(lo: int, hi: int) -> int:
        with ro_connect(db) as c:
            return c.execute(
                CANONICAL_ACTIVITY_SQL,
                {"subject": subject, "raise_epoch": lo, "as_of_epoch": hi},
            ).fetchone()[0]

    print(f"t_4776f5c9.created_at = {raise_epoch} = {utc(raise_epoch)}   <- ground truth")
    print(f"elon comment (task_comments id=18298, t_4776f5c9) = 1785712223 = {utc(1785712223)}   <- real '26' observation instant\n")

    print("CAUSE 1 — the [raise, raise+24h] window was still OPEN, so an")
    print("          unbounded-above count grows every time you run it:")
    for label, asof in [
        ("elon    2026-08-02 23:10 comment (id=18298)", 1785712223),
        ("PM      2026-08-03 00:22 retire ", 1785716533),
        ("os-rev  2026-08-03 00:28 verify ", 1785716929),
        ("now                             ", int(time.time())),
    ]:
        capped = min(asof, raise_epoch + ACTIVITY_WINDOW_S)
        print(f"    as_of={label} -> count={q(raise_epoch, capped):>3}")
    print("    => 17 (elon's real comment) / 21 (PM) / 22 (os-reviewer minutes later)")
    print("       are the SAME query, minutes apart. Not a discrepancy — an unpinned")
    print("       upper bound.\n")

    print("CAUSE 2 — os-reviewer's comment cites window [1785671998, +86400].")
    print(f"          1785671998 = {utc(wrong_epoch)}")
    print(f"          created_at = {utc(raise_epoch)}")
    print(f"          delta      = {(raise_epoch - wrong_epoch) // 3600}h (local-vs-UTC offset applied to the raise time)")
    print(f"    count[wrong_epoch .. +24h] as_of 00:28:49 = {q(wrong_epoch, 1785716929):>3}  <- reproduces the '27'")
    print(f"    count[created_at .. +24h]  as_of 00:28:49 = {q(raise_epoch, 1785716929):>3}\n")

    print("CAUSE 3 — elon's '26 in the last 24h' is a TRAILING-24h window from his")
    print("          own observation time, not a post-raise window:")
    # e = elon's real comment created_at (jarvis-os task_comments id=18298 on
    # t_4776f5c9). Previously hardcoded 1785716166 (00:16:06 UTC) — the wrong
    # instant, which printed 30 instead of the real 26 at 1785712223.
    e = 1785712223
    with ro_connect(db) as c:
        trail = c.execute(
            "SELECT count(*) FROM tasks WHERE assignee=? AND status='done' "
            "AND completed_at BETWEEN ? AND ?", (subject, e - 86400, e)
        ).fetchone()[0]
    print(f"    trailing-24h from {utc(e)} = {trail}")
    print(f"    post-raise window, same instant = {q(raise_epoch, e)}\n")

    print("-" * 78)
    print("FIX ADOPTED: CANONICAL_ACTIVITY_SQL is bounded on both sides, as_of is")
    print("always printed with the count, and as_of = min(now, raise+24h). Any")
    print("reviewer re-running the triple (subject, raise_epoch, as_of) gets the")
    print("identical integer. That is the reproducibility bar for retiring a gate.")
    print("-" * 78)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--canary", action="store_true", help="replay t_4776f5c9")
    ap.add_argument("--explain-variance", action="store_true", help="decompose the 21/26/27 counts")
    ap.add_argument("--regression", action="store_true", help="run all regression tests from os-reviewer t_c8dfff2e")
    ap.add_argument("--json", action="store_true", help="machine-readable findings")
    ap.add_argument("--boards", nargs="*", default=DEFAULT_BOARDS)
    ap.add_argument("--show-comment", metavar="TASK_ID", help="print the comment the probe would post")
    args = ap.parse_args()

    if args.explain_variance:
        return explain_variance()
    if args.canary:
        return canary()
    if args.regression:
        return regression_test()

    findings = scan(args.boards)
    if args.json:
        print(json.dumps([{
            "board": f.board, "task_id": f.task_id, "verdict": f.verdict,
            "premise_class": f.premise_class, "subject": f.subject,
            "raise_epoch": f.raise_epoch, "as_of_epoch": f.as_of_epoch,
            "activity": f.activity, "reason": f.reason,
            "negative_control": f.negative_control,
            "declined_because": f.declined_because, "evidence": f.evidence,
        } for f in findings], indent=2))
        return 0

    print(render(findings))
    if args.show_comment:
        match = [f for f in findings if f.task_id == args.show_comment]
        if match:
            print("\n--- recommendation comment for", args.show_comment, "---")
            print(recommendation_comment(match[0]))
    return 0


# ---------------------------------------------------------------------------
# Regression tests — reproduce & verify all os-reviewer corrections
# ---------------------------------------------------------------------------
def _regression_auth_phrases() -> list[str]:
    """Verify every auth phrase identified by os-reviewer now matches AUTHORIZATION_RE."""
    failed: list[str] = []
    for phrase in [
        "A3 explicit authorization needed",
        "A3 authorization required",
        "Frank authorization needed",
        "A3 approval needed",
        "Frank's approval",
        "Frank\u2019s approval",   # typographic (curly) apostrophe variant
        "A3 sign-off required",
        "await frank deploy",
        "explicit frank approval",
        "requires frank clearance",
        "frank/a3 needed",
    ]:
        if not AUTHORIZATION_RE.search(phrase):
            failed.append(f"NO MATCH: '{phrase}'")
    return failed


def _regression_dangerous_case() -> tuple[str, str]:
    """Assert the exact reproducer from os-reviewer t_c8dfff2e: auth+service+sole→DECLINE(authorization).

    Returns (verdict, premise_class).
    """

    class Row:
        def __init__(self, d: dict): self._d = d
        def __getitem__(self, k: str): return self._d[k]

    text = ("start trading-risk-reviewer gateway \u2014 sole cause of fleet BLOCK. "
            "A3 explicit authorization needed before deploy.")
    f = evaluate(
        "jarvis-os",
        Row({
            "id": "t_regression_dangerous",
            "title": "start trading-risk-reviewer gateway",
            "body": text,
            "created_at": 1785712223,
            "status": "blocked",
            "block_kind": "frank_gate",
        }),
        ["jarvis-os", "sycode-trading"],
    )
    msg = ""
    if f.verdict != "DECLINE" or f.premise_class != "authorization":
        msg = (f"FAIL: verdict={f.verdict} class={f.premise_class} "
               f"activity={f.activity}")
    return (msg, f"{f.verdict}:{f.premise_class}")


def _regression_explain_variance_epochs() -> list[str]:
    """Verify explain-variance hard-coded epochs are real DB values."""
    conn = ro_connect("jarvis-os")
    row = conn.execute(
        "SELECT id, created_at FROM task_comments WHERE id=18298",
    ).fetchone()
    conn.close()
    if row and row[1] == 1785712223:
        return []  # elon epoch verified against DB comment 18298
    return [f"Epoch mismatch: got {row[1]} expected 1785712223"]


def regression_test() -> int:
    """Run all regression checks; exit 0 only if every one passes."""
    print("=" * 78)
    print("REGRESSION TESTS — pre-fix defect reproduction verification")
    print("=" * 78)

    n_passed = 0
    n_total = 0

    # --- Defect 1a: auth phrases match ---
    n_total += 1
    fails = _regression_auth_phrases()
    if fails:
        print(f"[FAIL] Auth phrases ({len(fails)} missed):")
        for ff in fails:
            print(f"       {ff}")
    else:
        print("[PASS] All auth phrases match AUTHORIZATION_RE")
        n_passed += 1

    # --- Defect 1b: dangerous case -> DECLINE(authorization) ---
    n_total += 1
    msg, result = _regression_dangerous_case()
    if msg:
        print(f"[FAIL] Dangerous case: {result} — {msg}")
    else:
        print(f"[PASS] Dangerous case -> {result} (must be DECLINE:authorization)")
        n_passed += 1

    # --- Defect 2: hardcoded epochs anchored to real DB ---
    n_total += 1
    ep_fails = _regression_explain_variance_epochs()
    if ep_fails:
        print(f"[FAIL] Epoch anchor: {ep_fails}")
    else:
        print("[PASS] Explain-variance epoch anchor = real comment 18298 (1785712223)")
        n_passed += 1

    # --- Structural: auth still first in evaluate() ---
    n_total += 1
    src = open(__file__).read()
    classify_pos = src.index("# ---- Class A: authorization")
    svc_pos = src.index("# ---- Class B: service/gateway activity")
    if classify_pos < svc_pos:
        print("[PASS] Authorization check comes BEFORE service-activity (short-circuit intact)")
        n_passed += 1
    else:
        print("[FAIL] Authorization check is AFTER service-activity — order swapped!")

    print(f"\nResults: {n_passed}/{n_total} regression checks passed.")
    return 0 if n_passed == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
