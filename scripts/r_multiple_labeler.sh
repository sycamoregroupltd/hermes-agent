#!/bin/bash
# Hermes cron shim for `sycode-r-multiple-labeler` (jarvis / trading-devops).
#
# ANTI-RECURRENCE (t_50ca3389, for t_18fa0a67 -> t_ea15dddf): an earlier version
# did `exec <hardcoded absolute path>` to the producer. On any checkout lacking
# that file the exec returned a SILENT exit 127 (no-agent cron: empty stdout =
# no delivery) and the producer died undetected for ~5 days.
#
# DURABLE FIX (learned from the t_ea15dddf recurrence): the Hermes shim is the
# one layer that is NOT tied to any repo branch, so it MUST be self-sufficient.
# It executes the committed producer directly:
#   1. Resolves the producer relative to the live checkout.
#   2. If the producer is missing on the active branch, SELF-HEALS it from
#      origin/main (which permanently carries
#      server/scripts/r-multiple-labeler-recurring.sh) — with an ALERT so an
#      operator can restore the checkout rather than rely on the crutch.
#   3. exec's the producer. A bare `exec <wrapper>` / `exec <feature-branch-only
#      file>` is NEVER used here, because that reintroduces a branch-dependent
#      hard dependency and the silent-127 recurrence.
#
# Only if the producer is absent from BOTH the live checkout AND origin/main
# does the shim exit 127 *with an ALERT on stderr+stdout* — never silent.
set -u

LIVE_REPO="/home/frank/sycode-trading"
PRODUCER="$LIVE_REPO/server/scripts/r-multiple-labeler-recurring.sh"
ALERT="ALERT r_multiple_labeler.sh"
SELF_HEALED_FROM=""

if [ ! -x "$PRODUCER" ]; then
  # Producer missing on the active checkout: self-heal from origin/main if git
  # is available. The producer lives permanently on origin/main, so this makes
  # a branch that lacks it structurally unable to cause a silent 127.
  if command -v git >/dev/null 2>&1 && [ -d "$LIVE_REPO/.git" ]; then
    if git -C "$LIVE_REPO" cat-file -e "origin/main:server/scripts/r-multiple-labeler-recurring.sh" >/dev/null 2>&1; then
      echo "$ALERT: producer missing at $PRODUCER — self-healing from origin/main (live checkout branch lacks it)" >&2
      if git -C "$LIVE_REPO" show "origin/main:server/scripts/r-multiple-labeler-recurring.sh" >"$PRODUCER" 2>/dev/null \
         && chmod +x "$PRODUCER" 2>/dev/null && [ -x "$PRODUCER" ]; then
        SELF_HEALED_FROM="origin/main"
        echo "$ALERT: producer self-heal OK from origin/main — restore the live checkout to a branch containing the producer to stop this crutch." >&2
      fi
    fi
  fi
fi

if [ -x "$PRODUCER" ]; then
  if [ -n "$SELF_HEALED_FROM" ]; then
    # ALERT on stdout too so the no-agent cron surfaces the self-heal event.
    echo "$ALERT: executing producer after self-heal from $SELF_HEALED_FROM."
  fi
  exec "$PRODUCER"
fi

# Producer truly absent everywhere: fail LOUD, never silent.
echo "$ALERT rc=127: R-multiple-labeler producer missing at $PRODUCER AND not on origin/main (self-heal impossible). The */15 cron cannot run — restore the live checkout." >&2
echo "$ALERT rc=127: producer missing and self-heal failed — see stderr."  # stdout so no-agent cron surfaces it
exit 127
