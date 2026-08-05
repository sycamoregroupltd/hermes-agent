#!/usr/bin/env bash
# post-checkout — live Hermes root self-healing guard.
#
# ~/.hermes is BOTH a git repo and the live execution directory for every cron
# job. Any worker running `git checkout <branch>` silently redefines what the
# whole fleet executes (2026-08-03 incident: a swap reverted 28 files that cron
# stores reference; see obsidian-fleet-vault/Operations/2026-08-03-shared-checkout-reverts-live-crons.md).
#
# pre-checkout does NOT fire on this git build (2.43, verified empirically), and
# post-checkout's exit code CANNOT abort a checkout (verified empirically), so
# this guard SELF-HEALS: after ANY successful branch/commit checkout it restores
# every file referenced by an enabled cron job to its content from the PREVIOUS
# HEAD — the code the fleet was actually running. The swap therefore cannot
# change what cron executes.
#
# After restoration the working tree differs from the new HEAD, so git's own
# "Your local changes ... would be overwritten by checkout" protection then
# blocks any further checkout that would touch live cron scripts — converting
# the silent failure into a loud, native refusal.
#
# ORDERING ROBUSTNESS: on this host git sometimes runs post-checkout BEFORE the
# working-tree update has finished (observed on 2026-08-03: refs all still match
# prev at hook time, file deletion lands after the hook), and sometimes after.
# The foreground loop handles the "after" ordering; a deferred background pass
# waits (up to 10s, polling disk-vs-new-tree) for git to finish applying the new
# tree, then re-instates prev content for any referenced file that changed.
#
# SELF-CONTAINED: the referenced-path computation is embedded here so the hook
# never depends on a working-tree file whose content varies by branch. It tries
# scripts/cron_untracked_script_guard.py --referenced-paths first (fast path)
# and falls back to the embedded logic when that mode is unavailable.
#
# POSIX-shell compatible on purpose: git may execute hooks under /bin/sh when
# the shebang is missing or unusable, and dash does not support bash heredocs
# (<<<). No bashisms. Must always exit 0 — a self-heal hook must never wedge a
# checkout (a hook that fails to parse DOES abort the checkout, so keep it valid
# POSIX).
#
# Installed at: <repo>/.git/hooks/post-checkout   (outside the working tree, so
#               checkouts cannot remove it)
# Source copy:  <repo>/scripts/git-live-cron-postcheckout.sh
# The hourly cron_untracked_script_guard.py verifies the installed hook exists,
# is executable, and matches this source (CONTROL finding otherwise).

set -u

REPO="${HERMES_LIVE_REPO:-/home/frank/.hermes}"
GUARD="$REPO/scripts/cron_untracked_script_guard.py"
LOGFILE="${HERMES_LIVE_POSTCHECKOUT_LOG:-$REPO/logs/live-cron-postcheckout.log}"

NEW_SHA="${1:-}"
PREV_SHA="${2:-}"
FLAG="${3:-}"
GIT_PID="$PPID"

# File checkouts (prev == new) restore from the index; out of scope. Only
# HEAD-changing branch/commit checkouts can swap what cron runs.
if [ -z "$PREV_SHA" ] || [ -z "$NEW_SHA" ] || [ "$PREV_SHA" = "$NEW_SHA" ]; then
    exit 0
fi

# --- referenced-path computation (self-contained) ---------------------------
# Prefer the guard's mode when available; fall back to embedded logic.
REFS="$(python3 "$GUARD" --referenced-paths 2>/dev/null)"
case "$REFS" in
    /*)
        ;;
    *)
        REFS="$(python3 - "$REPO" <<'PY'
import json, re, sys
from pathlib import Path
REPO = Path(sys.argv[1])
COMMAND_FIELDS = ("command", "prompt")
TOKEN_RE = re.compile(r"(?:/|~/)[\w./~+-]+\.(?:py|sh|bash|pl|rb|js|ts)\b")

def job_enabled(j):
    if j.get("enabled") is False: return False
    if j.get("state") == "paused": return False
    if j.get("disabled") is True: return False
    return True

def resolve_like_scheduler(profile_home, script):
    scripts_dir = profile_home / "scripts"
    sd = scripts_dir.resolve()
    raw = Path(script).expanduser()
    path = raw.resolve() if raw.is_absolute() else (scripts_dir / raw).resolve()
    path.relative_to(sd)
    return path

def scan(profile_home, data, paths):
    for job in (data.get("jobs") or []):
        if not job_enabled(job): continue
        script = job.get("script")
        if script:
            try:
                resolved = resolve_like_scheduler(profile_home, script)
                resolved.relative_to(REPO)
                paths.append(str(resolved))
            except ValueError:
                pass
        for field in COMMAND_FIELDS:
            value = job.get(field)
            if not isinstance(value, str): continue
            for token in dict.fromkeys(TOKEN_RE.findall(value)):
                resolved = Path(token).expanduser().resolve()
                try:
                    resolved.relative_to(REPO)
                    paths.append(str(resolved))
                except ValueError:
                    pass

paths = []
for store_path in sorted((REPO / "profiles").glob("*/cron/jobs.json")):
    label = store_path.parts[-3]
    try:
        data = json.loads(store_path.read_text())
    except Exception:
        continue
    scan(REPO / "profiles" / label, data, paths)
root = REPO / "cron" / "jobs.json"
if root.exists():
    try:
        data = json.loads(root.read_text())
    except Exception:
        data = None
    if data is not None:
        scan(REPO, data, paths)
for p in sorted(set(paths)):
    print(p)
PY
)"
        ;;
esac
[ -n "$REFS" ] || exit 0

mkdir -p "$(dirname "$LOGFILE")"
TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-postcheckout.XXXXXX")"
trap 'rm -rf "$TMPDIR"' EXIT

# POSIX-safe: write refs to a file and redirect the read loop from it.
printf '%s\n' "$REFS" > "$TMPDIR/refs"

RESTORED=0
N_SHOW_FAIL=0
N_CP_FAIL=0
N_MATCH=0
while IFS= read -r abs; do
    [ -z "$abs" ] && continue
    # Only protect files inside the repo working tree.
    rel="${abs#"$REPO"/}"
    [ "$rel" = "$abs" ] && continue

    # Content from the PREVIOUS HEAD — what the fleet was running before the swap.
    if ! git -C "$REPO" show "$PREV_SHA:$rel" > "$TMPDIR/prev" 2>/dev/null; then
        N_SHOW_FAIL=$((N_SHOW_FAIL + 1))
        continue  # not present in prev (added by this checkout): leave it
    fi
    # Restore only if the checkout actually changed it on disk.
    if cmp -s "$TMPDIR/prev" "$abs" 2>/dev/null; then
        N_MATCH=$((N_MATCH + 1))
        continue
    fi

    # Preserve the executable bit from git's tree mode (100755 -> 755).
    mode="$(git -C "$REPO" ls-tree "$PREV_SHA" -- "$rel" | awk '{print $1}')"
    if [ -n "$mode" ]; then
        chmod "${mode#100}" "$TMPDIR/prev" 2>/dev/null || true
    fi
    if cp "$TMPDIR/prev" "$abs" 2>/dev/null; then
        RESTORED=$((RESTORED + 1))
        echo "  $rel" >> "$LOGFILE"
    else
        N_CP_FAIL=$((N_CP_FAIL + 1))
    fi
done < "$TMPDIR/refs"

# --- deferred pass (update-after-hook ordering) ------------------------------
# Files that this checkout changes AND that cron references. The changed list
# must live OUTSIDE $TMPDIR: the foreground trap removes $TMPDIR as soon as the
# hook exits, before the background subshell can read it.
CHANGEDFILE="$REPO/logs/live-cron-postcheckout.changed.$$"
: > "$CHANGEDFILE"
git -C "$REPO" diff --name-only "$PREV_SHA" "$NEW_SHA" 2>/dev/null | while IFS= read -r rel; do
    [ -z "$rel" ] && continue
    if grep -Fxq "$REPO/$rel" "$TMPDIR/refs"; then
        echo "$rel" >> "$CHANGEDFILE"
    fi
done

if [ -s "$CHANGEDFILE" ]; then
    (
        # NOTE: base dir MUST be /tmp, not $TMPDIR — the foreground trap removes
        # $TMPDIR when the hook exits, which would orphan this subshell's files.
        TMP2="$(mktemp -d /tmp/hermes-postcheckout-bg.XXXXXX)"
        cp "$CHANGEDFILE" "$TMP2/changed"
        rm -f "$CHANGEDFILE"

        # Wait until the PARENT git process exits — that is the reliable signal
        # that the worktree update has fully landed (on this host the update can
        # finish AFTER post-checkout returns). Cap at ~15s; if the wait times out
        # we still restore prev content, which is the safe default.
        i=0
        while [ "$i" -lt 60 ] && kill -0 "$GIT_PID" 2>/dev/null; do
            sleep 0.25
            i=$((i + 1))
        done

        # Re-instate PREV content for every changed ref (idempotent: skips if
        # the foreground pass already restored it).
        RESTORED2=0
        while IFS= read -r rel; do
            [ -z "$rel" ] && continue
            abs="$REPO/$rel"
            if ! git -C "$REPO" show "$PREV_SHA:$rel" > "$TMP2/prev" 2>/dev/null; then
                continue
            fi
            if cmp -s "$TMP2/prev" "$abs" 2>/dev/null; then
                continue
            fi
            mode="$(git -C "$REPO" ls-tree "$PREV_SHA" -- "$rel" | awk '{print $1}')"
            if [ -n "$mode" ]; then
                chmod "${mode#100}" "$TMP2/prev" 2>/dev/null || true
            fi
            if cp "$TMP2/prev" "$abs" 2>/dev/null; then
                RESTORED2=$((RESTORED2 + 1))
                echo "  $rel" >> "$LOGFILE"
            fi
        done < "$TMP2/changed"

        {
            echo "post-checkout-bg $(date -Is) changed=$(wc -l < "$TMP2/changed") restored=$RESTORED2 prev=$PREV_SHA new=$NEW_SHA flag=$FLAG"
        } >> "$LOGFILE"
        rm -rf "$TMP2"
    ) &
else
    rm -f "$CHANGEDFILE"
fi

{
    echo "post-checkout $(date -Is) refs=$(wc -l < "$TMPDIR/refs") restored=$RESTORED match=$N_MATCH showfail=$N_SHOW_FAIL cpfail=$N_CP_FAIL prev=$PREV_SHA new=$NEW_SHA flag=$FLAG"
} >> "$LOGFILE"

# --- live cron-store git-tracking guard (t_6c32b13c completion) --------------
# NOTE: this hook's restore loop above covers only SCRIPTS referenced by cron
# jobs — the jobs.json stores themselves are untracked runtime state with no
# prev-HEAD blob to restore from (that is why historical log lines show
# restored=0 for store clobbers; it was never in scope here).
# Live stores must NEVER be tracked. A checkout to a pre-untracking commit
# re-tracks them — and because they are gitignored, git treats them as
# expendable and silently OVERWRITES the live files with sanitized copies.
# Repair the git state immediately (untrack index + commit the untracking on
# HEAD) so the NEXT git operation cannot clobber again; the 2m
# fleet_cron_store_clobber_canary is the scheduled backstop for reset --hard,
# which fires no hook. Background + always-exit-0: must never wedge a checkout.
(
    STORE_OUT="$(python3 "$REPO/scripts/cron_store_git_clobber_guard.py" --apply --json --quiet 2>/dev/null)"
    if [ -n "$STORE_OUT" ]; then
        {
            echo "post-checkout store-guard $(date -Is) prev=$PREV_SHA new=$NEW_SHA:"
            printf '%s\n' "$STORE_OUT" | sed 's/^/  /'
        } >> "$LOGFILE"
    fi
) &

exit 0
