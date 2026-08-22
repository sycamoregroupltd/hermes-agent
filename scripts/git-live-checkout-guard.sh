#!/usr/bin/env bash
# git-live-checkout-guard.sh — PATH-level `git` wrapper protecting the shared
# live Hermes root (~/.hermes) from worker branch-swaps.
#
# WHY (t_041d138a, P0): ~/.hermes is BOTH a git repo AND the live execution
# directory for every cron job. git 2.43 on this host CANNOT abort a checkout
# (pre-checkout does not fire; post-checkout's exit code is ignored — both
# verified empirically, 2026-08-11). So prevention at the git layer is
# impossible via hooks/aliases (git aliases also CANNOT override builtin
# checkout/switch). The only reliable structural gate is a PATH-level wrapper
# that sits ahead of the real `git` in every worker shell.
#
# BEHAVIOUR: this wrapper REFUSES any HEAD-/working-tree-moving git command
# (checkout, switch, reset, pull, merge, rebase, restore, stash, cherry-pick,
# revert, clean) when the target repo is the live /home/frank/.hermes tree,
# unless HERMES_ALLOW_CHECKOUT=1 is set (the documented escape hatch for the
# seat/operator). Everything else — including `git worktree add` (the sanctioned
# isolated path), fetch, push, add, commit, status, log, branch — passes through
# untouched.
#
# SAFETY: self-contained (no dependency on a working-tree file that a checkout
# could remove). Always ends by exec-ing the real git, so a detection failure
# can never wedge normal git use. Only refuses in the one repo it guards.
#
# INSTALL: copy to a PATH dir ahead of /usr/bin/git (e.g. ~/.local/bin/git).
# Source copy:  <repo>/scripts/git-live-checkout-guard.sh

set -u

LIVE_REPO="${HERMES_LIVE_REPO:-/home/frank/.hermes}"

# Resolve the real git, skipping this wrapper's own directory (avoid recursion).
WRAPPER_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
REAL_GIT=""
for cand in /usr/bin/git /bin/git /usr/local/bin/git "$HOME/.local/bin/git"; do
    cand_dir="$(cd "$(dirname "$cand")" 2>/dev/null && pwd)"
    [ "$cand_dir" = "$WRAPPER_DIR" ] && continue
    if [ -x "$cand" ]; then REAL_GIT="$cand"; break; fi
done
if [ -z "$REAL_GIT" ]; then
    REAL_GIT="$(command -v git 2>/dev/null || true)"
fi
# Absolute, so we never re-enter this wrapper.
case "$REAL_GIT" in
    /*) ;;
    *) REAL_GIT="/usr/bin/git" ;;
esac

# Operator escape hatch.
if [ "${HERMES_ALLOW_CHECKOUT:-0}" = "1" ]; then
    exec "$REAL_GIT" "$@"
fi

# --- Parse target dir + subcommand (index-based) -----------------------------
args=("$@")
n=${#args[@]}
TARGET_DIR=""
SUB=""
i=0
while [ "$i" -lt "$n" ]; do
    arg="${args[$i]}"
    case "$arg" in
        -C)
            # -C takes the NEXT arg as the directory
            j=$((i + 1))
            if [ "$j" -lt "$n" ]; then TARGET_DIR="${args[$j]}"; i=$j; fi
            ;;
        -C*)
            TARGET_DIR="${arg#-C}"
            ;;
        -c|-c*|--git-dir|--work-tree|--git-dir=*|--work-tree=*|--bare|--namespace|--namespace=*|--literal-pathspecs|--no-optional-locks|-p|--paginate|--no-pager)
            # options (value-taking git-dir/work-tree handled by git itself);
            # none of these change which repo HEAD lives in for our purpose
            ;;
        -*)
            # other option; skip
            ;;
        *)
            SUB="$arg"
            break
            ;;
    esac
    i=$((i + 1))
done

DANGEROUS=" checkout switch reset pull merge rebase restore stash cherry-pick revert clean "

if [ -n "$SUB" ] && case "$DANGEROUS" in *" $SUB "*) true;; *) false;; esac; then
    # Determine the repo root git would operate on.
    if [ -n "$TARGET_DIR" ]; then
        ROOT="$("$REAL_GIT" -C "$TARGET_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
    else
        ROOT="$("$REAL_GIT" rev-parse --show-toplevel 2>/dev/null || true)"
    fi
    if [ -n "$ROOT" ]; then
        # Canonicalise both sides (resolve symlinks) so the comparison is robust.
        ROOT_REAL="$(realpath "$ROOT" 2>/dev/null || echo "$ROOT")"
        LIVE_REAL="$(realpath "$LIVE_REPO" 2>/dev/null || echo "$LIVE_REPO")"
        if [ "$ROOT_REAL" = "$LIVE_REAL" ]; then
            echo "REFUSED: 'git $SUB' is not allowed in the shared live tree $LIVE_REPO." >&2
            echo "A branch-swap here silently reverts what every cron job executes (t_041d138a)." >&2
            echo "Work in an isolated worktree instead: git worktree add <scratch> <branch>" >&2
            echo "If you genuinely must move the live tree, set HERMES_ALLOW_CHECKOUT=1 (operator only)." >&2
            exit 1
        fi
    fi
fi

exec "$REAL_GIT" "$@"
