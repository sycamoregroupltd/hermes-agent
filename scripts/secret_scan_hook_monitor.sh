#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies.
# secret_scan_hook_monitor.sh — is the .hermes secret-scan pre-commit gate actually installed,
# actually canonical, and actually able to say no?
#
# WHY (2026-08-05, chip task_5e020ecd): git hooks are not versioned. The 2026-08-05 DSN
# hardening lived only in .git/hooks/pre-commit on this host — one re-clone, one worker
# copying a stale hook, one `git config core.hooksPath`, and the gate is gone with no
# symptom. The failure is silent by construction: a missing pre-commit hook makes every
# commit succeed. The canonical text is now tracked at .githooks/pre-commit; this monitor
# is what notices when the installed copy stops matching it.
#
# CHECKS (any failure => non-zero exit + ALERT line in the log + hermes send):
#   1. canonical .githooks/pre-commit exists            (missing => wrong branch/reverted)
#   2. installed <git-common-dir>/hooks/pre-commit exists and is executable
#   3. sha256(installed) == sha256(canonical)           (drift/tamper/stale copy)
#   4. core.hooksPath is unset                          (set => installed copy is dead code)
#   5. FUNCTIONAL: in a scratch repo, the INSTALLED hook blocks a literal DSN and
#      passes an interpolated one. Hash equality alone would certify a gutted canonical
#      file just as happily — a detector that has not been shown red is not evidence.
#
# Observe-only. Never repairs, never edits the crontab, never touches the live tree.
# Silent when healthy (log line only). NEVER fails open: any internal error is an ALERT.

set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

REPO="${HOOK_MON_REPO:-/home/frank/.hermes}"
LOG_FILE="${LOG_FILE:-/home/frank/.hermes/logs/secret-scan-hook-monitor.log}"
MON_STATE="${MON_STATE:-/home/frank/.hermes/state/secret-scan-hook-monitor-state.txt}"
REALERT_SECS="${REALERT_SECS:-21600}"   # re-alert every 6h while a breach persists
# Same target convention as deploy_liveness_monitor.sh / system-crontab-watchdog.sh.
ALERT_TARGET="${HOOK_MON_ALERT_TARGET:-whatsapp:Frank}"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$MON_STATE")" 2>/dev/null
touch "$MON_STATE" 2>/dev/null
now_iso=$(date -Is); now_epoch=$(date +%s)
log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

FAILURES=()
fail() { FAILURES+=("$1"); log "ALERT: $1"; }

send_alert() {
    local key="$1" subject="$2" body="$3" last fb
    last=$(/usr/bin/grep -a "^${key}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt "$REALERT_SECS" ]; then
        log "SUPPRESSED key=$key (re-alert window)"
        return 0
    fi
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body" 2>/dev/null; then
        log "ALERT-SENT target=$ALERT_TARGET key=$key"
    else
        log "ALERT-FAILED target=$ALERT_TARGET key=$key"
        # `-` not `:-` on purpose: an explicitly EMPTY HOOK_MON_FALLBACKS disables
        # failover, so a red-test drill (HOOK_MON_ALERT_TARGET=drill:none
        # HOOK_MON_FALLBACKS='' MON_STATE=/tmp/...) can prove detection without
        # paging Frank. Unset still means the real failover chain.
        for fb in ${HOOK_MON_FALLBACKS-discord:#critical-alerts telegram:506972405}; do
            if hermes send -q -t "$fb" -s "FAILOVER: $subject" "$body" 2>/dev/null; then
                log "ALERT-SENT-FALLBACK target=$fb key=$key"
                break
            fi
        done
    fi
    echo "${key}=${now_epoch}" >> "$MON_STATE"
}

CANONICAL="$REPO/.githooks/pre-commit"
common_dir=$(cd "$REPO" 2>/dev/null && cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd)
if [ -z "${common_dir:-}" ]; then
    fail "cannot resolve git common dir for $REPO (repo missing or not a git checkout)"
    INSTALLED=""
else
    INSTALLED="$common_dir/hooks/pre-commit"
fi

# 1 + 2 + 3: existence and content identity.
can_sha=""; ins_sha=""
if [ ! -f "$CANONICAL" ]; then
    fail "canonical hook MISSING: $CANONICAL (checked-out branch has no tracked gate)"
else
    can_sha=$(sha256sum "$CANONICAL" 2>/dev/null | cut -d' ' -f1)
fi
if [ -n "$INSTALLED" ]; then
    if [ ! -f "$INSTALLED" ]; then
        fail "installed hook MISSING: $INSTALLED — every commit in this repo is UNGATED (run: bash $REPO/.githooks/install.sh)"
    else
        [ -x "$INSTALLED" ] || fail "installed hook NOT EXECUTABLE: $INSTALLED — git will skip it"
        ins_sha=$(sha256sum "$INSTALLED" 2>/dev/null | cut -d' ' -f1)
    fi
fi
if [ -n "$can_sha" ] && [ -n "$ins_sha" ] && [ "$can_sha" != "$ins_sha" ]; then
    fail "installed hook DIFFERS from canonical (installed=${ins_sha:0:12} canonical=${can_sha:0:12}) — re-run $REPO/.githooks/install.sh after reviewing the diff"
fi

# 4: core.hooksPath would redirect git away from the copy we just verified.
hp=$(cd "$REPO" 2>/dev/null && git config --get core.hooksPath 2>/dev/null)
[ -n "${hp:-}" ] && fail "core.hooksPath is set to '$hp' — the verified hook at $INSTALLED is dead code"

# 5: functional red/green on the INSTALLED hook (what git would really run).
if [ -n "$INSTALLED" ] && [ -f "$INSTALLED" ]; then
    tmp=$(mktemp -d 2>/dev/null)
    if [ -z "${tmp:-}" ]; then
        fail "self-test could not create a temp dir — gate behaviour UNVERIFIED"
    else
        # Fake, non-secret credential. Assembled via a variable so this file's own
        # bytes never contain a literal DSN (the gate scans this script too).
        FAKEPW='F4keN0tReal'
        git init -q "$tmp" 2>/dev/null
        install -m 0700 "$INSTALLED" "$tmp/.git/hooks/pre-commit" 2>/dev/null
        printf 'DB="postgresql://postgres:%s@127.0.0.1:5432/postgres"\n' "$FAKEPW" > "$tmp/bad.sh"
        printf 'DB="postgresql://postgres:${DB_PASS}@127.0.0.1:5432/postgres"\n' > "$tmp/good.sh"
        git -C "$tmp" add bad.sh 2>/dev/null
        if git -C "$tmp" -c user.name=t -c user.email=t@t -c commit.gpgsign=false \
               commit -qm red >/dev/null 2>&1; then
            fail "self-test RED FAILED: the installed hook did NOT block a literal DSN"
        fi
        git -C "$tmp" rm -q --cached bad.sh >/dev/null 2>&1; rm -f "$tmp/bad.sh"
        git -C "$tmp" add good.sh 2>/dev/null
        if ! git -C "$tmp" -c user.name=t -c user.email=t@t -c commit.gpgsign=false \
               commit -qm green >/dev/null 2>&1; then
            fail "self-test GREEN FAILED: the installed hook blocked an interpolated (safe) DSN — it will wedge automation_vc_keeper"
        fi
        rm -rf "$tmp"
    fi
fi

if [ "${#FAILURES[@]}" -eq 0 ]; then
    log "OK hook=${ins_sha:0:12} canonical=${can_sha:0:12} self-test=red+green"
    exit 0
fi

body="secret-scan pre-commit gate BREACH on $(hostname) at $now_iso
repo:      $REPO
canonical: $CANONICAL
installed: ${INSTALLED:-<unresolved>}
$(printf '  - %s\n' "${FAILURES[@]}")
Fix: bash $REPO/.githooks/install.sh   (see $REPO/.githooks/README.md)
Log: $LOG_FILE"
send_alert "secret_scan_hook" "SECRET-SCAN HOOK BREACH (${#FAILURES[@]})" "$body"
printf 'SECRET-SCAN HOOK BREACH: %s\n' "${FAILURES[@]}" >&2
exit 1
