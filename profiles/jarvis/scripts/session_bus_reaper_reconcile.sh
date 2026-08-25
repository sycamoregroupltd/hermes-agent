#!/usr/bin/env bash
# Session-Bus liveness + reconcile wrapper — cron job 66bb00df0c5d (t_cf5538ac).
# Each daily tick runs the liveness reap FIRST, then the additive --reconcile
# divergence sweep, so stale rows keep being reaped (liveness preserved) and
# pre-existing frontmatter/table divergence cannot re-accumulate.
#
# Why TWO invocations: the reaper's main() is `if --reconcile: return
# reconcile() else: return reap()` — passing --reconcile alone REPLACES the
# liveness reap with the divergence sweep. To honour the t_cf5538ac hard gate
# "Do NOT alter liveness semantics: default invocation still runs the reaper's
# reap path; --reconcile is additive", this wrapper runs BOTH paths in order:
# reap (no flags), then reconcile (--reconcile). Both reuse the same
# .SESSION-BUS.lock, CEO/master-orchestrator exclusion, and per-file backups
# (t_58795f9f / t_9b909712 / t_d4808fd1). Any extra CLI args (e.g. --dry-run,
# --selftest) are forwarded to both paths so verification stays read-only.
#
# CANONICAL source: /home/frank/.hermes/scripts/session_bus_reaper_reconcile.sh
# Profile-local shims (e.g. profiles/jarvis/scripts/) MUST be kept
# byte-identical to this file (CANONICAL-COPY RULE, t_41acb465).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "${HERE}/session_bus_reaper.py" "$@"
python3 "${HERE}/session_bus_reaper.py" --reconcile "$@"
