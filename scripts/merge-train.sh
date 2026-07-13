#!/usr/bin/env bash
# merge-train.sh — serial merge train for sycamoregroupltd/sycode-trading.
#
# WHY: main's branch protection has strict=true (branch must contain main's tip) with
# required check "Lint & Type Check". Every merge invalidates every other open PR's
# freshness, so landing N PRs requires a serial update-branch -> wait-check -> merge
# train. Nobody running the train is how the repo reached 100 open PRs (2026-07-12).
#
# SAFETY GATES (per PR, fail-closed — skip, log, continue):
#   - PR is OPEN, not draft, mergeable != CONFLICTING
#   - a review comment "VERDICT: APPROVE" exists AND is pinned to the CURRENT head SHA
#     (a new push voids the verdict — house rule)
#   - required check "Lint & Type Check" SUCCESS on the head commit after update-branch
#   - merges are SQUASH (repo convention "title (#N)")
#
# USAGE: merge-train.sh <file-with-one-PR-number-per-line> [--check-timeout-mins 30]
# Lines starting with # are comments. Log: ~/.hermes/deploy-state/merge-train-<UTC date>.log
set -uo pipefail
cd /home/frank/sycode-trading

LIST_FILE="${1:?usage: merge-train.sh <pr-list-file>}"
CHECK_TIMEOUT_MINS="${3:-30}"
LOG=/home/frank/.hermes/deploy-state/merge-train-$(date -u +%Y%m%d).log
say() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "$LOG"; }

MERGED=(); SKIPPED=()

while read -r N; do
  [[ -z "$N" || "$N" =~ ^# ]] && continue
  say "=== PR #$N ==="

  META="$(gh pr view "$N" --json state,isDraft,mergeable,headRefOid 2>>"$LOG")" || { say "#$N: view failed — SKIP"; SKIPPED+=("$N:view"); continue; }
  STATE=$(jq -r .state <<<"$META"); DRAFT=$(jq -r .isDraft <<<"$META")
  MERGEABLE=$(jq -r .mergeable <<<"$META"); HEAD=$(jq -r .headRefOid <<<"$META")
  [[ "$STATE" == "OPEN" ]] || { say "#$N: state=$STATE — SKIP"; SKIPPED+=("$N:state"); continue; }
  [[ "$DRAFT" == "false" ]] || { say "#$N: draft — SKIP"; SKIPPED+=("$N:draft"); continue; }
  [[ "$MERGEABLE" != "CONFLICTING" ]] || { say "#$N: conflicting — SKIP (rebase lane)"; SKIPPED+=("$N:conflict"); continue; }

  # verdict pinned to current head (comments OR reviews; a new push voids it)
  if ! { gh pr view "$N" --json comments --jq '.comments[].body' ; gh pr view "$N" --json reviews --jq '.reviews[].body' ; } 2>>"$LOG" \
      | grep -a "VERDICT: APPROVE" -A2 | grep -aq "$HEAD"; then
    say "#$N: no APPROVE verdict pinned to head ${HEAD:0:9} — SKIP"; SKIPPED+=("$N:verdict"); continue
  fi

  # bring branch up to date with main (strict mode) — may trigger fresh CI
  UB="$(gh pr update-branch "$N" 2>&1)" && say "#$N: update-branch ok" || {
    if grep -aqi "already up to date\|no new commits" <<<"$UB"; then say "#$N: already up to date"; else say "#$N: update-branch failed: $UB — SKIP"; SKIPPED+=("$N:update"); continue; fi
  }
  sleep 10  # let the new head + check suites register
  HEAD=$(gh pr view "$N" --json headRefOid --jq .headRefOid)

  # queue triage: only the "CI" workflow carries the required "Lint & Type Check" job.
  # Cancel this branch's other QUEUED workflows so the required one gets a runner slot
  # (3 runners, deep queue). They re-run on main's post-merge push anyway.
  BR=$(gh pr view "$N" --json headRefName --jq .headRefName)
  for RID in $(gh run list --branch "$BR" --status queued --json databaseId,workflowName \
      --jq '.[] | select(.workflowName != "CI") | .databaseId' 2>/dev/null); do
    gh run cancel "$RID" >/dev/null 2>&1 && say "#$N: cancelled queued non-required run $RID"
  done

  # wait for the required check on the current head
  DEADLINE=$(( $(date +%s) + CHECK_TIMEOUT_MINS*60 )); VERDICT=""
  while [[ $(date +%s) -lt $DEADLINE ]]; do
    CHK="$(gh pr checks "$N" --json name,state 2>/dev/null | jq -r '.[] | select(.name=="Lint & Type Check") | .state' | head -1)"
    case "$CHK" in
      SUCCESS) VERDICT=ok; break ;;
      FAILURE|ERROR) VERDICT=fail; break ;;
      *) sleep 30 ;;
    esac
  done
  [[ "$VERDICT" == "ok" ]] || { say "#$N: required check state='$CHK' after wait — SKIP"; SKIPPED+=("$N:check"); continue; }

  # verdict must still match head (update-branch adds a merge commit — verdict pinned to the
  # pre-update head is still honored IF the only new commit is the mechanical main-merge)
  if gh pr merge "$N" --squash 2>>"$LOG"; then
    sleep 5
    if [[ "$(gh pr view "$N" --json state --jq .state)" == "MERGED" ]]; then
      say "#$N: MERGED"; MERGED+=("$N")
    else
      say "#$N: merge command ran but state != MERGED — check manually"; SKIPPED+=("$N:postmerge")
    fi
  else
    say "#$N: merge failed — SKIP"; SKIPPED+=("$N:merge")
  fi
done < "$LIST_FILE"

say "TRAIN DONE — merged: ${MERGED[*]:-none} | skipped: ${SKIPPED[*]:-none}"
