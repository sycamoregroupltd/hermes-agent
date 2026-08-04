#!/usr/bin/env bash
# pr-review-lane-tick.sh — CONTINUOUS independent-review lane for GITHUB PULL REQUESTS.
#
# WHY (incident 2026-08-04): review-lane-tick.sh reviews grok drafts on kanban CARDS.
# Nothing reviewed PULL REQUESTS. PR review had been outsourced to third-party GitHub
# apps (cursor Bugbot, chatgpt-codex-connector, copilot) — two dead, one non-gating —
# so merge-train.sh's fail-closed verdict gate could never be satisfied and the repo
# reached 140 open PRs with ~62 passing branch protection and ZERO landable.
# This lane is the verdict PRODUCER that gate was always assuming existed.
#
# DESIGN RULES (literal — mirrors review-lane-tick.sh, same failure history):
#  - reviewer != author: the review card goes to a REVIEWER PROFILE, never the PR author.
#  - REVIEW ONLY, stated verbatim: a dispatched reviewer once implemented+pushed+
#    self-approved (PR #399). It must never push, merge, or unblock.
#  - idempotency-keyed on PR NUMBER + HEAD SHA, so a new push voids the old verdict and
#    earns a fresh review — exactly the house rule merge-train.sh enforces.
#  - NO CHECKOUT. The reviewer reads the pinned diff via gh. The repo is a 4.3GB shallow
#    clone where `clone --local` drops the pinned commit, and a stray `git checkout` in a
#    shared tree strands live state. Reading by ref sidesteps both.
#  - this lane does NOT act on verdicts and does NOT merge. Verdict->action stays with the
#    orchestrator; the verdict-router is DO-NOT-ARM until its blockers clear (t_65a0c080).
#  - empty stdout = silent (no-agent watchdog convention).
set -uo pipefail
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

REPO="${PRLANE_REPO:-sycamoregroupltd/sycode-trading}"
BOARD="${PRLANE_BOARD:-sycode-trading}"
REVIEWER="${PRLANE_REVIEWER:-trading-risk-reviewer}"
REQUIRED_CONTEXT="${PRLANE_REQUIRED_CONTEXT:-Lint & Type Check}"
MAX_OPEN_REVIEWS="${PRLANE_MAX_OPEN:-8}"   # do not flood the reviewer profile
BATCH="${PRLANE_BATCH:-4}"                 # review cards created per tick
# Cards created without --priority default to 0 and starve: the sycode-trading ready
# queue routinely holds cards at 40-70, so the first four PR-review cards sat unclaimed
# for 40+ minutes behind unrelated work. 50 clears the bulk tier (1-5) and the 40s while
# staying under the P0/P1 band (60-70), so genuine blockers still outrank review.
PRIORITY="${PRLANE_PRIORITY:-50}"
SCAN_LIMIT="${PRLANE_SCAN_LIMIT:-40}"
STATE=/home/frank/.hermes/state
mkdir -p "$STATE"

exec 9>"$STATE/pr-review-lane.lock"
flock -n 9 || exit 0

[ -d "$HOME/.hermes/profiles/$REVIEWER" ] || { echo "PR REVIEW LANE: reviewer profile $REVIEWER missing" >&2; exit 1; }

# Backpressure: how many review cards are already open for this reviewer?
# NOTE: `--status` accepts ONE value (argparse choices), not a comma list. Summing three
# calls is deliberate — a comma list exits non-zero, and swallowing that error would read
# as "0 open" and disable the cap entirely. Query failure must DISABLE the lane, not
# uncap it: fail-closed, because the failure mode of an uncapped lane is flooding the
# reviewer profile.
# Count only THIS LANE's cards (title prefix), not the reviewer's total queue.
# Counting the reviewer's whole backlog looked correct and was not: trading-risk-reviewer
# already carried 28 cards from other lanes on 2026-08-04, so a total-load cap would have
# held this lane at zero dispatches forever while exiting 0 — a monitor-shaped silence
# indistinguishable from "nothing to do". Throttle on own depth; report saturation aloud.
open_n=0
for st in todo ready running; do
  n=$(hermes kanban --board "$BOARD" list --assignee "$REVIEWER" --status "$st" --json 2>/dev/null \
      | python3 -c 'import sys,json
d=json.load(sys.stdin)
t=d if isinstance(d,list) else d.get("tasks",[])
print(sum(1 for x in t if (x.get("title") or "").startswith("REVIEW PR #")))') || {
        echo "PR REVIEW LANE: backpressure query failed for status=$st — refusing to dispatch uncapped" >&2
        exit 1
      }
  open_n=$((open_n + n))
done
if [ "$open_n" -ge "$MAX_OPEN_REVIEWS" ]; then
  # Saturation is a real state, not a non-event. Say so: a lane that goes quiet for the
  # right reason and a lane that goes quiet because it is broken must not look identical.
  echo "PR REVIEW LANE: saturated — $open_n open PR-review cards (cap $MAX_OPEN_REVIEWS); no new dispatch this tick"
  exit 0
fi

# Candidates: open, non-draft, passing the ONLY required context, lacking a head-pinned
# verdict. Same predicate merge-train.sh enforces — if these disagree, this lane is the bug.
candidates=$(gh pr list --repo "$REPO" --state open --limit "$SCAN_LIMIT" \
    --json number,isDraft,headRefOid,mergeStateStatus,statusCheckRollup,comments,reviews,title 2>/dev/null \
  | REQ="$REQUIRED_CONTEXT" python3 -c '
import json, os, sys
req = os.environ["REQ"]
try:
    prs = json.load(sys.stdin)
except Exception:
    sys.exit(3)   # unparseable -> fail loud, do not silently review nothing
out = []
for pr in prs:
    if pr.get("isDraft") or pr.get("mergeStateStatus") == "DIRTY":
        continue          # conflicted PRs belong to the rebase lane, not review
    head = pr.get("headRefOid") or ""
    if not any(c.get("name") == req and c.get("conclusion") == "SUCCESS"
               for c in pr.get("statusCheckRollup") or []):
        continue
    bodies = [c.get("body") or "" for c in (pr.get("comments") or [])]
    bodies += [r.get("body") or "" for r in (pr.get("reviews") or [])]
    pinned = False
    for b in bodies:
        lines = b.splitlines()
        for i, l in enumerate(lines):
            if "VERDICT: APPROVE" in l and any(head in w for w in lines[i:i+3]):
                pinned = True
                break
        if pinned:
            break
    if not pinned:
        out.append(f'"'"'{pr["number"]}\t{head}\t{(pr.get("title") or "")[:70]}'"'"')
print("\n".join(out))
') || { echo "PR REVIEW LANE: candidate query failed" >&2; exit 1; }

created=0
while IFS=$'\t' read -r num head title; do
  [ -z "${num:-}" ] && continue
  [ "$created" -ge "$BATCH" ] && break

  body="INDEPENDENT REVIEW of GitHub pull request #${num} in ${REPO}. You are NOT the author.

REVIEW ONLY. Do not edit, commit, push, merge, deploy, rebase, or unblock anything. Do not implement the work. If you believe code must change, say so in your verdict — do not write it. You are not permitted to approve your own work; if you find you authored this PR, stop and say so.

TARGET: PR #${num} — ${title}
HEAD SHA: ${head}

Read the PR WITHOUT checking out the repo (it is a 4.3GB shallow clone; a local clone drops the pinned commit and a stray checkout strands live state):
  gh pr view ${num} --repo ${REPO} --json title,body,files,additions,deletions
  gh pr diff ${num} --repo ${REPO}
Review the diff AT ${head}. If the head SHA has moved since this card was created, STOP and say so — your verdict would be void.

Do this:
1. Identify what the PR claims to do, and what it actually does. Judge the delta.
2. SPOT-CHECK the load-bearing claims yourself: do cited file paths exist in the diff, do the tests actually exercise the changed path, are cited PRs/SHAs/task ids real? Do not trust the PR description — descriptions have been wrong before.
3. Look specifically for: silent-failure paths (an error that cannot reach an exit code or an alert), config/flag changes whose delivery is unverified, write-path changes, and anything that widens what runs in production.
4. Flag anything requiring A3 (money, live trading, credentials, irreversible data ops, new spend, provider routing) — never endorse those; route them to Frank.

END YOUR REVIEW WITH EXACTLY ONE OF THESE TWO LINES, verbatim, as the LAST line:
  VERDICT: APPROVE ${head}
  VERDICT: REJECT ${head}
The full 40-character SHA on the same line is MANDATORY — merge-train.sh greps for that literal token and that exact SHA, and a verdict without it is silently discarded (this stranded PR #923 on 2026-08-04). Do not abbreviate the SHA. Do not reword the token. If you are unsure, choose REJECT and explain.

Then post the review to the PR:
  gh pr comment ${num} --repo ${REPO} --body-file <your-review-file>
Posting the comment is the deliverable. A review that stays in your workspace has not been delivered."

  if hermes kanban --board "$BOARD" create \
        "REVIEW PR #${num} @ ${head:0:9}" \
        --body "$body" \
        --assignee "$REVIEWER" \
        --skill github-code-review \
        --priority "$PRIORITY" \
        --idempotency-key "pr-review-${REPO}-${num}-${head}" \
        --workspace scratch \
        --max-runtime 45m \
        --created-by pr-review-lane >/dev/null 2>&1; then
    created=$((created + 1))
    echo "PR REVIEW LANE: queued review of #${num} @ ${head:0:9} -> ${REVIEWER}"
  else
    echo "PR REVIEW LANE: FAILED to queue review of #${num}" >&2
  fi
done <<< "$candidates"

exit 0
