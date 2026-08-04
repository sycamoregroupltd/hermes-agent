#!/usr/bin/env bash
# review-lane-tick.sh — CONTINUOUS independent-review lane (no-agent cron).
# Pairs with grok-lane-tick.sh: grok DRAFTS, this lane gets those drafts
# INDEPENDENTLY REVIEWED by existing hermes reviewer profiles, which the
# dispatcher spawns on a different provider/model than grok.
# Created 2026-08-02 (Frank: "add some reviewer seats" — grok at 10 seats made
# review the binding constraint; hand-running Claude reviews was a seat dependency).
#
# DESIGN RULES (literal):
#  - reviewer != implementer: the review card goes to a REVIEWER PROFILE,
#    never to grok and never to the card's own assignee.
#  - review cards are created UNLINKED (fleet rule: parent-linking a review
#    under the card it reviews deadlocks both — impls hand off by BLOCKING and
#    parents only release on DONE).
#  - idempotency-keyed per target card, so re-ticks never duplicate.
#  - the reviewer is told READ-ONLY, verbatim, because a dispatched reviewer
#    once implemented+pushed+self-approved (PR #399).
#  - this lane does NOT act on verdicts. Verdict->action stays with the
#    orchestrator/PM: the verdict-router is DO-NOT-ARM until its 4 blockers
#    are fixed (t_65a0c080).
#  - empty stdout = silent (no-agent watchdog pattern).
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HK="${HK:-120}"   # bound every hermes call: unbounded calls starved the dispatcher (08-02)
STATE=/home/frank/dgx-fable-orchestrator/state
LOG=$STATE/review-lane.log
CELLS=$STATE/grok-cells
MAX_OPEN_REVIEWS=12   # do not flood the reviewer profiles
BATCH=6               # review cards created per tick

mkdir -p "$STATE"
exec 9>"$STATE/review-lane.lock"
flock -n 9 || exit 0

reviewer_for() {   # board -> reviewer profile (all verified present under ~/.hermes/profiles/)
  case "$1" in
    sycode-trading)     echo trading-risk-reviewer ;;
    jarvis-os)          echo os-reviewer ;;
    upero)              echo upero-design-reviewer ;;
    yorkstone-supplies) echo yorkstone-supplies-reviewer ;;
    *)                  echo platform-reviewer ;;
  esac
}

created=0
for b in sycode-trading jarvis-os upero ai-restaurant; do
  db="$HOME/.hermes/kanban/boards/$b/kanban.db"
  [ -f "$db" ] || continue
  rp=$(reviewer_for "$b")
  [ -d "$HOME/.hermes/profiles/$rp" ] || { echo "REVIEW LANE: reviewer profile $rp missing for $b"; continue; }

  # backpressure: how many review cards for this reviewer are already open?
  open=$(sqlite3 "file:${db}?mode=ro" "SELECT COUNT(*) FROM tasks
     WHERE assignee='$rp' AND status IN ('todo','ready','running');" 2>/dev/null || echo 0)
  [ "$open" -ge "$MAX_OPEN_REVIEWS" ] && continue

  # targets: cards carrying a grok cell output, still blocked, with no review card yet
  targets=$(sqlite3 "file:${db}?mode=ro" "
    SELECT DISTINCT c.task_id FROM task_comments c JOIN tasks t ON t.id=c.task_id
    WHERE c.author='fable-grok' AND t.status='blocked'
      AND NOT EXISTS (SELECT 1 FROM tasks r WHERE r.title LIKE 'REVIEW grok draft: '||c.task_id||'%')
    LIMIT $BATCH;" 2>/dev/null)

  for tid in $targets; do
    [ "$created" -ge "$BATCH" ] && break
    out="$CELLS/$tid.md"
    [ -f "$out" ] || continue
    body="INDEPENDENT REVIEW of a grok work-cell draft. You are NOT the author.

REVIEW ONLY. Do not edit, commit, push, deploy, or approve/unblock anything you judged. Do not implement the work. If you believe code must change, say so in your verdict — do not write it.

TARGET CARD: $b / $tid
GROK DRAFT: $out (read it in full)

Do this:
1. Read the target card (title, body, and its comments) and identify (a) its acceptance criteria and (b) the reviewer delta or blocker that put it in this state.
2. Read the grok draft.
3. Judge whether the draft actually satisfies the acceptance criteria and addresses the delta.
4. SPOT-CHECK the draft's load-bearing factual claims yourself: do the cited file paths exist, do line numbers match, are cited PRs/SHAs real, do commands/SQL make sense? Grok has hallucinated a module before and has twice been fooled by shallow-clone artifacts, so verify rather than trust. Note whether its [UNVERIFIED] markers are honest or evasive.
5. Flag anything requiring A3 (money, live trading, credentials, irreversible data ops, new spend, provider routing) — never endorse those.

Post your verdict as a comment on $tid in this exact form, then complete THIS review card:
REVIEW_VERDICT: APPROVE | CHANGES_REQUESTED | REJECT
plus: what you verified (with evidence), what is wrong, and the single next action.

An honest 'the draft restates the problem without solving it' is a valuable verdict. Do not rubber-stamp."
    if timeout "${HK:-120}" hermes kanban --board "$b" create "REVIEW grok draft: $tid" \
        --body "$body" --assignee "$rp" \
        --idempotency-key "grok-review-$tid" --created-by fable --priority 1 >/dev/null 2>&1; then
      created=$((created+1))
      echo "$(date -u +%FT%TZ) review card created for $b/$tid -> $rp" >> "$LOG"
    fi
  done
done

if [ "$created" -gt 0 ]; then
  echo "$(date -u +%FT%TZ) tick created $created review cards" >> "$LOG"
fi
exit 0   # silent: nothing to review is a healthy state, not a fault
