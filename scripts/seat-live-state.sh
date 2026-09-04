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
#   - Read-only: remote freshness uses a hard-bounded `git ls-remote`; local topology
#     uses existing refs only. Neither probe writes the primary checkout.
set -uo pipefail

REPO="${SYCODE_REPO:-${HOME}/sycode-trading}"
CONTAINER="sycodetrading-server"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
BOARD_DB="${SYCODE_BOARD_DB:-${HERMES_HOME}/kanban/boards/sycode-trading/kanban.db}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RECONCILE_SCRIPT="${HERMES_RECONCILE_SCRIPT:-${SCRIPT_DIR}/reconcile-state.py}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown-time)"
FAILED=""

note_fail() { FAILED="${FAILED:+$FAILED, }$1"; }

# ---- deploy anchor: deployed SHA -------------------------------------------------
DEPLOYED="$(timeout 3 docker inspect "$CONTAINER" \
  --format '{{index .Config.Labels "com.sycodetrading.git.sha"}}' 2>/dev/null)"
[ -n "$DEPLOYED" ] || { DEPLOYED="PROBE FAILED"; note_fail "deployed-sha"; }

# ---- origin/main tip + deploy gap (the merged!=deployed trap) --------------------
# Check remote freshness without updating refs; topology below uses the cached local ref.
FETCH_NOTE="unavailable"
REMOTE_MAIN="$(timeout 3 git -C "$REPO" ls-remote --heads origin refs/heads/main 2>/dev/null \
  | awk 'NR == 1 { print $1 }')"
if [ -n "$REMOTE_MAIN" ]; then
  FETCH_NOTE="remote-checked"
else
  note_fail "git-ls-remote(remote unavailable)"
fi
MAIN="$(timeout 2 git -C "$REPO" rev-parse origin/main 2>/dev/null)"
if [ -z "$MAIN" ]; then MAIN="PROBE FAILED"; note_fail "origin-main"; fi
if [ -n "$REMOTE_MAIN" ] && [ "$MAIN" != "$REMOTE_MAIN" ]; then
  FETCH_NOTE="remote-ahead"
  note_fail "origin-main(local ref differs from remote)"
fi
GAP="unknown"
if [ "$DEPLOYED" != "PROBE FAILED" ] && [ "$MAIN" != "PROBE FAILED" ]; then
  # Guard against shallow-clone ancestry lies: the deployed sha must be a known object.
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
case "$GAP" in
  unknown*) GAP_TAG="(gap unknown — re-verify)" ;;
esac

# ---- shared-checkout HEAD branch (off-main hazard) -------------------------------
HEAD_BR="$(timeout 2 git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)"
[ -n "$HEAD_BR" ] || { HEAD_BR="PROBE FAILED"; note_fail "head-branch"; }
HEAD_TAG=""
if [ "$HEAD_BR" != "main" ] && [ "$HEAD_BR" != "PROBE FAILED" ]; then
  HEAD_TAG="  ⚠ OFF-MAIN (shared/mutable — deploy from origin/main, use a FRESH worktree)"
fi
# Detect mid-operation states so a transient branch isn't misread as a settled one.
if [ -d "$REPO/.git" ]; then
  if [ -d "$REPO/.git/rebase-merge" ] || [ -d "$REPO/.git/rebase-apply" ]; then
    HEAD_TAG="$HEAD_TAG [REBASE IN PROGRESS]"
  fi
  [ -f "$REPO/.git/MERGE_HEAD" ] && HEAD_TAG="$HEAD_TAG [MERGE IN PROGRESS]"
fi

# ---- trading MODE gate (paper/live) — shell-callable, no psql --------------------
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

# ---- open PR count (verdicts live in comments, not reviewDecision) ---------------
PRS="$(timeout 8 gh pr list --repo sycamoregroupltd/sycode-trading --state open \
  --limit 300 --json number --jq 'length' 2>/dev/null)"
[ -n "$PRS" ] || { PRS="PROBE FAILED"; note_fail "open-prs"; }

# ---- live PR ground-truth (so a stale curated line can NEVER hide a cluster) ------
# The North Star line below is HAND-CURATED and can lag reality. These two probes are
# LIVE from gh — they are the falsification test for the curated line. If the curated
# NS text references a PR far below LATEST_MERGED, the seat is booting on stale progress.
LATEST_MERGED="$(timeout 8 gh pr list --repo sycamoregroupltd/sycode-trading --state merged \
  --limit 1 --json number,title,mergedAt \
  --jq '.[0] | "#\(.number) \(.title[:48]) (\(.mergedAt[:10]))"' 2>/dev/null)"
[ -n "$LATEST_MERGED" ] || { LATEST_MERGED="PROBE FAILED"; note_fail "latest-merged-pr"; }
RECENT_OPEN="$(timeout 8 gh pr list --repo sycamoregroupltd/sycode-trading --state open \
  --limit 300 --json number --jq 'sort_by(.number) | reverse | [limit(6;.[])] | map("#\(.number)") | join(" ")' 2>/dev/null)"
[ -n "$RECENT_OPEN" ] || { RECENT_OPEN="PROBE FAILED"; note_fail "recent-open-prs"; }

# ---- North Star active phase (cheap, from curated board) + STATE.md pointer -------
NS="$(awk -F'\t' '$2 == "ACTIVE" {print $1" "$3" → "$4; exit}' "${HERMES_HOME}/state/ns-phases.tsv" 2>/dev/null)"
[ -n "$NS" ] || NS="(phase board unavailable)"

# ---- staleness guard: have PRs merged SINCE the phase board was last curated? -----
# ns-phases.frontier records the latest-merged PR# at the moment the TSV was curated.
# If the live frontier has advanced past it, new progress exists that the curated NS
# text predates → boot is at risk of reciting stale progress. This is the true test
# (NOT "highest PR the line names" — an honest line may reference an open PR below the
# merged frontier). Re-stamp the frontier whenever you re-curate ns-phases.tsv.
NS_STALE=""
FRONTIER_FILE="${HERMES_HOME}/state/ns-phases.frontier"
CURATED_AT_PR="$(grep -oE '^[0-9]+' "$FRONTIER_FILE" 2>/dev/null | head -1)"
LIVE_MAX_PR="$(printf '%s' "$LATEST_MERGED" | grep -oE '#[0-9]+' | head -1 | tr -d '#')"
# Count PRs actually MERGED above the stamp — a PR-number delta lies when a burst of
# PRs is opened-but-unmerged (proven 2026-07-12: stamp 434, latest merged 460, "26 stale"
# — but exactly ONE PR had merged in between).
MERGED_SINCE="$(timeout 8 gh pr list --repo sycamoregroupltd/sycode-trading --state merged \
  --limit 100 --json number --jq "[.[] | select(.number > ${CURATED_AT_PR:-0})] | length" 2>/dev/null)"
if [ -n "$CURATED_AT_PR" ] && [ -n "$MERGED_SINCE" ] && [ "$MERGED_SINCE" -ge 3 ]; then
  NS_STALE="  ⚠ STALE: ${MERGED_SINCE} PRs merged since the phase board was curated (stamp #${CURATED_AT_PR}, latest merged #${LIVE_MAX_PR:-?}) — re-derive ns-phases.tsv + re-stamp ns-phases.frontier BEFORE trusting the phase text"
fi
SMD="${SYCODE_VAULT:-${HOME}/obsidian/sycode-trading}/STATE.md"
if [ -f "$SMD" ]; then
  AGE=$(( ( $(date +%s) - $(stat -c %Y "$SMD" 2>/dev/null || echo 0) ) / 60 ))
  STATE_PTR="${SMD} (reconciled ${AGE}m ago) — full task↔PR↔deploy↔note lineage; refresh: python3 ${RECONCILE_SCRIPT}"
else
  STATE_PTR="not yet generated — run: python3 ${RECONCILE_SCRIPT}"
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
