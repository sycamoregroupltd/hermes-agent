#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""pr_review_health_monitor.py — liveness monitor for the PR *verdict producer*.

WHY (incident 2026-08-04, Frank/fable seat):
  sycode-trading reached 140 open PRs with ~62 passing branch protection and
  ZERO landable. Cause: merge-train.sh is fail-closed on a review verdict
  ("VERDICT: APPROVE" pinned to the head SHA) and NOTHING produced that verdict.
  PR review had been silently outsourced to three third-party GitHub apps:
     - cursor (Bugbot)            -> "Bugbot is not enabled for your account"
     - chatgpt-codex-connector    -> "You have reached your Codex usage limits"  (>= 2026-08-02T11:03Z)
     - copilot-pull-request-reviewer -> posts a descriptive overview, 0 inline
       comments, state COMMENTED, never APPROVE/REQUEST_CHANGES -> gates nothing.
  Every PR carried 2-3 comments, so the pile LOOKED reviewed. It was not.
  Nobody noticed for >2 days because no monitor watched review *output*.

  The existing codex-exhaustion-circuit-breaker does NOT cover this: it watches
  Hermes' own codex credential pool for dispatch, not the GitHub app's
  code-review quota. Different subsystem, so it never fired.

WHAT THIS MEASURES (the predicate the LANDER enforces, not a proxy):
  merge-train.sh admits a PR only when a "VERDICT: APPROVE" line, within 2 lines,
  cites the CURRENT head SHA. This monitor reproduces that exact predicate. If
  it ever diverges from merge-train.sh, THIS SCRIPT IS THE BUG.

ALERTS (any one fires):
  A. STARVATION  — gate-passing PRs with no head-pinned verdict, oldest beyond
                   STARVE_HOURS. The deadlock itself.
  B. DEAD-EXTERNAL — a known reviewer app is posting a failure/upsell notice
                   instead of a review. Catches "looks reviewed, isn't".
  C. NO-OUTPUT   — gate-passing unverdicted PRs exist AND zero verdicts were
                   produced anywhere in the window. The producer is dead, not slow.
  D. NEAR-MISS   — a SUBSTANTIVE review exists but does not satisfy the lander's
                   predicate, so the work is stranded by FORMAT, not by judgement.
                   Found live on 2026-08-04: PR #923 carried a real independent
                   review ("REVIEW_VERDICT: APPROVED", citing short SHA
                   5596aee6f7) which merge-train.sh refuses — it requires the
                   literal token plus the FULL 40-char head SHA within 2 lines.
                   Producer and consumer had drifted apart with nothing watching
                   the seam. This alert is that watcher.

FAIL-CLOSED: any gh/API error exits non-zero with the reason on stderr. A
no-agent cron's ONLY liveness signal is the exit code (stdout is never parsed),
so a partial/failed probe must NOT be allowed to print a clean report.
Healthy = empty stdout, exit 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("PRRHM_REPO", "sycamoregroupltd/sycode-trading")
REQUIRED_CONTEXT = os.environ.get("PRRHM_REQUIRED_CONTEXT", "Lint & Type Check")
VERDICT_TOKEN = os.environ.get("PRRHM_VERDICT_TOKEN", "VERDICT: APPROVE")
SCAN_LIMIT = int(os.environ.get("PRRHM_SCAN_LIMIT", "40"))
STARVE_HOURS = float(os.environ.get("PRRHM_STARVE_HOURS", "12"))
NOOUTPUT_HOURS = float(os.environ.get("PRRHM_NOOUTPUT_HOURS", "24"))
VERDICT_CONTEXT_LINES = 2  # merge-train.sh uses grep -A2

# Substrings that mean "this app did not actually review the PR".
DEAD_EXTERNAL_MARKERS = (
    "is not enabled for your account",
    "was not reviewed",
    "reached your Codex usage limits",
    "usage limits for code reviews",
    "add credits to your account",
)


class ProbeError(RuntimeError):
    """A probe could not be completed — never downgrade to 'healthy'."""


def gh(*args: str) -> str:
    cp = subprocess.run(
        ("gh",) + args,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if cp.returncode != 0:
        raise ProbeError(f"gh {' '.join(args)} -> rc={cp.returncode}: {cp.stderr.strip()[:300]}")
    return cp.stdout


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def has_pinned_verdict(bodies: list[str], head: str) -> bool:
    """Reproduce merge-train.sh: `grep VERDICT_TOKEN -A2 | grep -q $HEAD`."""
    if not head:
        return False
    for body in bodies:
        lines = (body or "").splitlines()
        for i, line in enumerate(lines):
            if VERDICT_TOKEN in line:
                window = lines[i : i + 1 + VERDICT_CONTEXT_LINES]
                if any(head in w for w in window):
                    return True
    return False


# Every approval spelling seen in the wild. Counted across 14 days of task_comments
# on 2026-08-04: REVIEW_VERDICT= 1365, VERDICT: APPROVE 160, REVIEW_VERDICT: 112.
# The '=' form is the MOST COMMON and matches the lander's grep NOT AT ALL -- and it
# was missing from this list, so the near-miss detector was blind to the single
# biggest source of stranded reviews. A detector that only recognises the format it
# expects will always report the fleet as healthier than it is.
APPROVAL_TOKENS = (
    "VERDICT: APPROVE",
    "VERDICT: APPROVED",
    "REVIEW_VERDICT: APPROV",
    "REVIEW_VERDICT=APPROV",
    "REVIEW_VERDICT = APPROV",
)


def near_miss_reason(bodies: list[str], head: str) -> str:
    """A substantive approval exists but the lander's predicate fails. Why?

    Only called when has_pinned_verdict() is already False, so any approval
    token found here is by definition stranded. Distinguishing WHICH half of
    the contract broke is what makes the alert actionable.
    """
    short = head[:10] if head else ""
    for body in bodies:
        text = body or ""
        if not any(tok in text for tok in APPROVAL_TOKENS):
            continue
        # ORDER MATTERS. Test the token first: the short SHA is a PREFIX of the full
        # SHA, so "short in text" is trivially true whenever the full SHA is present.
        # Checking it first mislabelled every full-SHA verdict as "cites SHORT sha"
        # -- e.g. PR #866, which carries exact_head=<full 40 chars> and whose real
        # defect is the token spelling. Detection was right, the fix advice was wrong,
        # which is worse than silence because it sends the reader at the wrong repair.
        if "VERDICT: APPROVE" not in text:
            return "approval uses a non-canonical token; lander greps the literal 'VERDICT: APPROVE'"
        if head and head in text:
            return "approval carries the full sha but not within 2 lines of the token; move them onto one line"
        if short and short in text:
            return f"approval cites SHORT sha {short}; lander needs the FULL 40-char sha within 2 lines"
        return "approval present but head sha absent within 2 lines (stale verdict, or push voided it)"
    return ""


def required_check_passed(pr: dict) -> bool:
    for c in pr.get("statusCheckRollup") or []:
        if c.get("name") == REQUIRED_CONTEXT:
            return c.get("conclusion") == "SUCCESS"
    return False


def main() -> int:
    fields = "number,title,createdAt,updatedAt,isDraft,headRefOid,mergeStateStatus,statusCheckRollup,comments,reviews"
    raw = gh(
        "pr", "list", "--repo", REPO, "--state", "open",
        "--limit", str(SCAN_LIMIT), "--json", fields,
    )
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProbeError(f"unparseable gh pr list output: {e}")

    if not prs:
        return 0  # no open PRs: nothing to gate, genuinely healthy

    starved: list[tuple[int, float, str]] = []
    near_miss: list[tuple[int, str]] = []
    dead_external: dict[str, int] = {}
    newest_verdict_age: float | None = None
    gate_passing = 0
    t = now()

    for pr in prs:
        if pr.get("isDraft"):
            continue
        head = pr.get("headRefOid") or ""
        bodies = [c.get("body", "") for c in (pr.get("comments") or [])]
        bodies += [r.get("body", "") for r in (pr.get("reviews") or [])]

        # (B) external reviewer posting a failure notice instead of a review
        for c in pr.get("comments") or []:
            body = c.get("body") or ""
            if any(m in body for m in DEAD_EXTERNAL_MARKERS):
                who = ((c.get("author") or {}).get("login")) or "unknown"
                dead_external[who] = dead_external.get(who, 0) + 1

        # (C) is the producer emitting anything at all, anywhere?
        for c in (pr.get("comments") or []) + (pr.get("reviews") or []):
            if VERDICT_TOKEN in (c.get("body") or ""):
                ts = c.get("createdAt") or c.get("submittedAt")
                if ts:
                    age = (t - parse_ts(ts)).total_seconds() / 3600.0
                    if newest_verdict_age is None or age < newest_verdict_age:
                        newest_verdict_age = age

        # (A) starvation: passes the gate, but the lander would refuse it
        if required_check_passed(pr):
            gate_passing += 1
            if not has_pinned_verdict(bodies, head):
                age_h = (t - parse_ts(pr["createdAt"])).total_seconds() / 3600.0
                if age_h >= STARVE_HOURS:
                    starved.append((pr["number"], age_h, (pr.get("title") or "")[:60]))

                # (D) near-miss: real approval present, wrong shape for the lander
                why = near_miss_reason(bodies, head)
                if why:
                    near_miss.append((pr["number"], why))

    alerts: list[str] = []

    if starved:
        starved.sort(key=lambda x: -x[1])
        oldest = starved[0]
        alerts.append(
            f"[A] VERDICT STARVATION: {len(starved)}/{gate_passing} gate-passing PRs have no "
            f"'{VERDICT_TOKEN}' pinned to head. merge-train.sh will refuse ALL of them. "
            f"Oldest #{oldest[0]} at {oldest[1]:.0f}h — {oldest[2]}"
        )

    if dead_external:
        who = ", ".join(f"{k} x{v}" for k, v in sorted(dead_external.items()))
        alerts.append(
            f"[B] EXTERNAL REVIEWER DEAD: apps posting failure/upsell notices instead of "
            f"reviews ({who}). These PRs LOOK reviewed and are not."
        )

    if near_miss:
        detail = "; ".join(f"#{n} ({w})" for n, w in near_miss[:4])
        alerts.append(
            f"[D] VERDICT FORMAT NEAR-MISS: {len(near_miss)} PR(s) carry a real approval the "
            f"lander refuses on FORMAT, not judgement — {detail}. Reviewer output and "
            f"merge-train.sh's predicate have drifted apart."
        )

    if starved and (newest_verdict_age is None or newest_verdict_age > NOOUTPUT_HOURS):
        seen = "never in scan window" if newest_verdict_age is None else f"{newest_verdict_age:.0f}h ago"
        alerts.append(
            f"[C] VERDICT PRODUCER SILENT: last verdict {seen} (threshold {NOOUTPUT_HOURS:.0f}h) "
            f"while {len(starved)} PRs wait. The review lane is dead, not slow."
        )

    if not alerts:
        return 0  # healthy -> silent (no-agent cron convention)

    print(f"PR REVIEW HEALTH — {REPO} — {t.strftime('%Y-%m-%dT%H:%MZ')}")
    print(f"scanned {len(prs)} open PRs (limit {SCAN_LIMIT}); {gate_passing} pass '{REQUIRED_CONTEXT}'")
    for a in alerts:
        print(f"  {a}")
    print("  FIX: pr-review-lane dispatches reviewer profiles to produce head-pinned verdicts.")
    # NOT `hermes cron runs` — that records durable attempts for AGENT-mode jobs only and
    # prints "No cron execution attempts recorded" for a no-agent script even when it is
    # running fine. The last-run line in `cron list` is the real signal.
    print("       Check it ran:  hermes cron list | grep -A7 'Name:      pr-review-lane$'")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as e:
        # Let the failure reach the exit code: a no-agent cron reports ONLY rc.
        print(f"pr_review_health_monitor: PROBE FAILED (not healthy): {e}", file=sys.stderr)
        sys.exit(1)
