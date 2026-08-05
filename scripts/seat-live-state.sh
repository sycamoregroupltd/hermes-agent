#!/usr/bin/env bash
# seat-live-state.sh — read-only live-state snapshot for the Hermes trading seat.
#
# CONSUMED BY (named consumers — no black holes):
#   1. SessionStart hook in ~/.claude/settings.json — injects this at every boot.
#   2. On-demand: `bash ~/.hermes/scripts/seat-live-state.sh` — the seat's re-probe command.
#   3. VERIFY: pointers in memory files point here for the fields it covers.
#
# DOCTRINE (read before trusting any line this prints):
#   This snapshot is a FALSIFIABLE PRIOR, not proof. It is a boot-time photograph.
#   A fresh point-of-use probe ALWAYS wins over this snapshot; if they disagree, THIS
#   SCRIPT is the suspect — log the divergence as a snapshot-script bug and fix the probe.
#   Memory holds pointers + reasoning; the LIVE SYSTEM holds the numbers. Never act on a
#   boot fact for a state-changing action (deploy/merge/gate-flip/DML) — re-probe first.
#
# HARD RULES (learned from the adversary red-team, 2026-07-07):
#   - Every probe is timeout-wrapped. The script NEVER hangs and ALWAYS exits 0.
#   - A missing/failed field renders as "PROBE FAILED", NEVER as blank or 0 (a silent
#     blank reads as "fine" and is worse than no snapshot).
#   - The output is SELF-DATED and lists every failed probe in a header.
#   - NO psql (classifier-blocked for the seat). Trading mode via :7777/health; live
#     open-positions is NOT shell-reachable — the block tells the seat to call the MCP.
#   - Read-only: the only write is `git fetch` of remote-tracking refs (safe; never
#     touches working tree or local branches), and it is hard-bounded by `timeout`.
set -uo pipefail

REPO="/home/frank/sycode-trading"
CONTAINER="sycodetrading-server"
BOARD_DB="/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown-time)"
FAILED=""

note_fail() { FAILED="${FAILED:+$FAILED, }$1"; }

# ---- deploy anchor: deployed SHA -------------------------------------------------
DEPLOYED="$(timeout 3 docker inspect "$CONTAINER" \
  --format '{{index .Config.Labels "com.sycodetrading.git.sha"}}' 2>/dev/null)"
[ -n "$DEPLOYED" ] || { DEPLOYED="PROBE FAILED"; note_fail "deployed-sha"; }

# ---- origin/main tip + deploy gap (the merged!=deployed trap) --------------------
FETCH_NOTE="cached"
if timeout 3 git -C "$REPO" fetch -q --depth=50 origin main 2>/dev/null; then
  FETCH_NOTE="fetched"
else
  note_fail "git-fetch(fell back to cached origin/main)"
fi
MAIN="$(timeout 2 git -C "$REPO" rev-parse origin/main 2>/dev/null)"
if [ -z "$MAIN" ]; then MAIN="PROBE FAILED"; note_fail "origin-main"; fi
GAP="unknown"
if [ "$DEPLOYED" != "PROBE FAILED" ] && [ "$MAIN" != "PROBE FAILED" ]; then
  if timeout 2 git -C "$REPO" cat-file -e "$DEPLOYED" 2>/dev/null; then
    GAP="$(timeout 3 git -C "$REPO" rev-list --count "${DEPLOYED}..origin/main" 2>/dev/null)"
    [ -n "$GAP" ] || { GAP="unknown"; note_fail "behind-count"; }
  else
    GAP="unknown(deployed-sha-not-in-local-history:shallow?)"
    note_fail "deploy-gap(sha-missing)"
  fi
fi
DEP_S="${DEPLOYED:0:9}"; MAIN_S="${MAIN:0:9}"
GAP_TAG=""
[ "$GAP" = "0" ] && GAP_TAG="(up-to-date)" || GAP_TAG="(+$GAP merged, UNDEPLOYED)  ⚠ SHIP PENDING"
[ "$GAP" = "unknown" ] && GAP_TAG="(gap unknown — re-verify)"

# ---- shared-checkout HEAD branch (off-main hazard) -------------------------------
HEAD_BR="$(timeout 2 git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)"
[ -n "$HEAD_BR" ] || { HEAD_BR="PROBE FAILED"; note_fail "head-branch"; }
HEAD_TAG=""
if [ "$HEAD_BR" != "main" ] && [ "$HEAD_BR" != "PROBE FAILED" ]; then
  HEAD_TAG="  ⚠ OFF-MAIN (shared/mutable — deploy from origin/main, use a FRESH worktree)"
fi
if [ -d "$REPO/.git" ]; then
  [ -d "$REPO/.git/rebase-merge" ] || [ -d "$REPO/.git/rebase-apply" ] && HEAD_TAG="$HEAD_TAG [REBASE IN PROGRESS]"
  [ -f "$REPO/.git/MERGE_HEAD" ] && HEAD_TAG="$HEAD_TAG [MERGE IN PROGRESS]"
fi

# ---- trading MODE gate (paper/live) ----------------------------------------------
HEALTH="$(timeout 2 curl -4 -s http://127.0.0.1:7777/health 2>/dev/null)"
MODE="unknown"
if [ -n "$HEALTH" ]; then
  case "$HEALTH" in
    *paper*|*PAPER*) MODE="paper" ;;
    *live*|*LIVE*)   MODE="LIVE  ⚠⚠ NOT PAPER — canon is paper-only until Frank flips it" ;;
    *)               MODE="up (mode not in /health — call sycode_status)" ;;
  esac
else
  MODE="PROBE FAILED (server /health unreachable)"; note_fail "mode-health"
fi

# ---- provider / auth pulse -------------------------------------------------------
PROV="$(timeout 6 hermes status 2>/dev/null | grep -iE '^\s*(Model|Provider):' \
  | sed 's/^[[:space:]]*//' | tr -s ' ' | paste -sd' | ' - 2>/dev/null)"
[ -n "$PROV" ] || { PROV="PROBE FAILED"; note_fail "provider"; }

# ---- board status counts (sycode-trading lane) -----------------------------------
BOARD="PROBE FAILED"
if [ -f "$BOARD_DB" ]; then
  BOARD="$(timeout 3 sqlite3 "file:${BOARD_DB}?mode=ro" \
    "SELECT COALESCE(SUM(status='running'),0)||' running / '||COALESCE(SUM(status='ready'),0)||' ready / '||COALESCE(SUM(status='blocked'),0)||' blocked / '||COALESCE(SUM(status='todo'),0)||' todo / '||COALESCE(SUM(status='scheduled'),0)||' scheduled' FROM tasks;" 2>/dev/null)"
  [ -n "$BOARD" ] || { BOARD="PROBE FAILED"; note_fail "board-counts"; }
else
  note_fail "board-db-missing"
fi

# ============================================================================
# PR PROBES — raised timeout, retry-on-empty, timed-out distinction
# ============================================================================
# Bug (2026-08-03): timeout 8 was too short under host load; gh routinely exceeded 8s,
# causing false PROBE FAILED that was indistinguishable from real outage/auth-fail.
# Fix: timeout 25s per attempt, one retry after sleep 1, distinguish TIMEOUT vs EMPTY.
GH="--repo sycamoregroupltd/sycode-trading"

gh_pr_probe() {
  # Usage: gh_pr_probe <state> <args...> [--jq '<filter>'] <varname>
  local state="$1"; shift
  local varname="$1"; shift
  # Build remaining args
  set -- "$@"
  local output rc=0

  # Attempt 1
  output=$(timeout 25 gh pr list "$GH" --state "$state" "$@" 2>/dev/null) || rc=$?
  if [ $rc -eq 124 ]; then
    printf -v "$varname" '%s' "PROBE FAILED(TIMEOUT: ${varname} timed out at 25s)"
    return
  fi
  # Attempt 2: retry if empty stdout (transient slowness)
  if [ -z "$output" ]; then
    sleep 1
    output=$(timeout 25 gh pr list "$GH" --state "$state" "$@" 2>/dev/null) || rc=$?
    if [ $rc -eq 124 ]; then
      printf -v "$varname" '%s' "PROBE FAILED(TIMEOUT: ${varname} timed out twice at 25s each)"
      return
    elif [ -z "$output" ]; then
      printf -v "$varname" '%s' "PROBE FAILED(gh returned empty after 2 retries)"
      return
    fi
  fi
  printf -v "$varname" '%s' "$output"
}

# open PR count
gh_pr_probe open '--limit 300' '--json number' --jq 'length' PRS
[ -n "$PRS" ] || { PRS="PROBE FAILED"; note_fail "open-prs"; }

# latest merged PR
gh_pr_probe merged '--limit 1' '--json number,title,mergedAt' --jq '.[0] | "#\(.number) \(.title[:48]) (\(.mergedAt[:10]))"' LATEST_MERGED
[ -n "$LATEST_MERGED" ] || { LATEST_MERGED="PROBE FAILED"; note_fail "latest-merged-pr"; }

# recent open PRs
gh_pr_probe open '--limit 300' '--json number' --jq 'sort_by(.number) | reverse | [limit(6;.[])] | map("#\(.number)") | join(" ")' RECENT_OPEN
[ -n "$RECENT_OPEN" ] || { RECENT_OPEN="PROBE FAILED"; note_fail "recent-open-prs"; }

# ---- North Star active phase (cheap, from curated board) + STATE.md pointer -------
NS="$(grep -P '\tACTIVE\t' /home/frank/.hermes/state/ns-phases.tsv 2>/dev/null | head -1 | awk -F'\t' '{print $1" "$3" → "$4}')"
[ -n "$NS" ] || NS="(phase board unavailable)"

# ---- staleness guard: have PRs merged SINCE the phase board was last curated? -----
NS_STALE=""
FRONTIER_FILE="/home/frank/.hermes/state/ns-phases.frontier"
CURATED_AT_PR="$(cat "$FRONTIER_FILE" 2>/dev/null | grep -oP '^[0-9]+' | head -1)"
LIVE_MAX_PR="$(printf '%s' "$LATEST_MERGED" | grep -oP '#\K[0-9]+' | head -1)"
# Count PRs actually MERGED above the stamp
MERGED_SINCE=""
CURATED_AT_PR="${CURATED_AT_PR:-0}"
gh_pr_probe merged '--limit 100' '--json number' --jq "[.[] | select(.number > ${CURATED_AT_PR})] | length" MERGED_SINCE
if [ -n "$CURATED_AT_PR" ] && [ -n "$MERGED_SINCE" ] && [ "$MERGED_SINCE" -ge 3 ]; then
  NS_STALE="  ⚠ STALE: ${MERGED_SINCE} PRs merged since the phase board was curated (stamp #${CURATED_AT_PR}, latest merged #${LIVE_MAX_PR:-?}) — re-derive ns-phases.tsv + re-stamp ns-phases.frontier BEFORE trusting the phase text"
fi
SMD="/home/frank/obsidian/sycode-trading/STATE.md"
if [ -f "$SMD" ]; then
  AGE=$(( ( $(date +%s) - $(stat -c %Y "$SMD" 2>/dev/null || echo 0) ) / 60 ))
  STATE_PTR="${SMD} (reconciled ${AGE}m ago) — full task↔PR↔deploy↔note lineage; refresh: python3 ~/.hermes/scripts/reconcile-state.py"
else
  STATE_PTR="not yet generated — run: python3 ~/.hermes/scripts/reconcile-state.py"
fi

# ---- emit (compact, one line per field; self-dated; fail-loud header) -------------
FAIL_LINE="none"; [ -n "$FAILED" ] && FAIL_LINE="$FAILED"
cat <<EOF
━━ SEAT LIVE-STATE @ ${NOW} ━━ (falsifiable PRIOR — a fresh probe always wins; if it disagrees, THIS script is the bug)
DEPLOY : deployed=${DEP_S} main=${MAIN_S} ${GAP_TAG}   [origin/main ${FETCH_NOTE}]
HEAD   : ${HEAD_BR}${HEAD_TAG}
MODE   : ${MODE}   →  live open_positions/balance: call mcp__jarvis__sycode_status (shell cannot; gate = open_positions==0)
BOARD  : ${BOARD}   (counts only — real-gate vs crash-casualty is a judgment you still owe)
PROVIDER: ${PROV}   (served model != pin? verify state.db billing_provider — a pin is not proof)
OPEN PRs: ${PRS}   (reviewDecision blank != unreviewed — reviewers post verdicts as comments)
PR FRONTIER: latest merged ${LATEST_MERGED} | recent open ${RECENT_OPEN}   (LIVE from gh — the ground truth; if the NORTH STAR line names lower PRs it is lagging)
NORTH STAR: ${NS}${NS_STALE}
FULL STATE: ${STATE_PTR}
PROBES FAILED: ${FAIL_LINE}
DOCTRINE: memory = provenance, not live state. Verify before you assert or act. Re-probe at point-of-use for any deploy/merge/gate-flip/DML — never act on this boot photo.
EOF
exit 0
