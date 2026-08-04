#!/usr/bin/env python3
"""Self-test for pr_review_health_monitor.py.

House rule (memory: verifier-blind-spots-false-clean): a detector must be proven
to go RED on known-bad AND CLEAN on known-good, and to FAIL LOUD when its own
probe breaks. A checker that only ever fires is indistinguishable from a checker
that is stuck on.

Run: python3 ~/.hermes/scripts/test_pr_review_health_monitor.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout

spec = importlib.util.spec_from_file_location(
    "prrhm", "/home/frank/.hermes/profiles/jarvis/scripts/pr_review_health_monitor.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FULL_SHA = "a" * 40
OK_CHECK = [{"name": "Lint & Type Check", "conclusion": "SUCCESS"}]
FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(f"{label} {detail}".strip())


def pr(num: int, *, comments=(), reviews=(), checks=OK_CHECK, head=FULL_SHA, age_h=99):
    from datetime import datetime, timedelta, timezone

    created = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat().replace("+00:00", "Z")
    return {
        "number": num, "title": f"pr {num}", "createdAt": created, "updatedAt": created,
        "isDraft": False, "headRefOid": head, "mergeStateStatus": "UNSTABLE",
        "statusCheckRollup": list(checks),
        "comments": [dict(c) for c in comments],
        "reviews": [dict(r) for r in reviews],
    }


def run(prs) -> tuple[str, int]:
    """Run main() against a synthetic PR set; return (stdout, exit_code)."""
    m.gh = lambda *a: json.dumps(prs)  # type: ignore[assignment]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = m.main()
    except m.ProbeError:
        return buf.getvalue(), 1
    return buf.getvalue(), rc


# ---------------------------------------------------------------- known-good
print("known-good (must be SILENT):")

good_comment = {
    "author": {"login": "trading-risk-reviewer"},
    "createdAt": "2026-08-04T12:00:00Z",
    "body": f"VERDICT: APPROVE\nhead {FULL_SHA}\nlgtm",
}
out, rc = run([pr(1, comments=[good_comment])])
check("properly pinned verdict -> no alert", out == "" and rc == 0, repr(out[:200]))

out, rc = run([])
check("zero open PRs -> no alert", out == "" and rc == 0, repr(out[:200]))

out, rc = run([pr(2, comments=[], checks=[{"name": "Lint & Type Check", "conclusion": "FAILURE"}])])
check("PR failing the required gate is not 'starved'", "[A]" not in out, repr(out[:200]))

out, rc = run([pr(3, comments=[], age_h=1)])
check("fresh unreviewed PR under threshold -> no starvation", "[A]" not in out, repr(out[:200]))

draft = pr(4, comments=[]); draft["isDraft"] = True
out, rc = run([draft])
check("draft PR ignored", out == "" and rc == 0, repr(out[:200]))

# ----------------------------------------------------------------- known-bad
print("known-bad (must be RED):")

out, rc = run([pr(5, comments=[])])
check("aged gate-passing PR with no verdict -> [A]", "[A]" in out, repr(out[:200]))

dead = {"author": {"login": "cursor"}, "createdAt": "2026-08-04T12:00:00Z",
        "body": "Bugbot is not enabled for your account, so this pull request was not reviewed."}
out, rc = run([pr(6, comments=[dead])])
check("reviewer app posting a failure notice -> [B]", "[B]" in out, repr(out[:200]))

short = {"author": {"login": "sycamoregroupltd"}, "createdAt": "2026-08-04T12:00:00Z",
         "body": f"## REVIEW_VERDICT: APPROVED\n\nreviewed at commit {FULL_SHA[:10]}"}
out, rc = run([pr(7, comments=[short])])
check("approval citing SHORT sha -> [D] near-miss", "[D]" in out and "SHORT" in out, repr(out[:300]))

stale = {"author": {"login": "sycamoregroupltd"}, "createdAt": "2026-08-04T12:00:00Z",
         "body": f"VERDICT: APPROVE\nhead {'b' * 40}\n"}
out, rc = run([pr(8, comments=[stale])])
check("verdict pinned to a DIFFERENT sha -> starved + [D]", "[A]" in out and "[D]" in out, repr(out[:300]))

out, rc = run([pr(9, comments=[])])
check("no verdict anywhere in window -> [C] producer silent", "[C]" in out, repr(out[:300]))

# ------------------------------------------------------- probe must fail loud
print("probe failure (must NOT report healthy):")


def boom(*a, **k):
    raise m.ProbeError("simulated gh outage")


m.gh = boom  # type: ignore[assignment]
buf = io.StringIO()
try:
    with redirect_stdout(buf):
        m.main()
    check("gh failure surfaces as error, not clean", False, "main() returned normally")
except m.ProbeError:
    check("gh failure raises ProbeError (rc=1, never 'healthy')", buf.getvalue() == "")

print()
if FAILURES:
    print(f"SELFTEST FAILED ({len(FAILURES)}):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("SELFTEST PASSED — detector proven red on bad, clean on good, loud on probe failure.")
