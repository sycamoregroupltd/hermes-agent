#!/usr/bin/env bash
# SHIM — t_97133ea8. Hermes cron resolves --script under this profile's scripts/
# directory and rejects symlinks / out-of-dir paths.
# Canonical collector: origin/main PR #1194
#   scripts/monitoring/sycode_deploy_watch_textfile_collector.py
# Executed copy: this profile's scripts/sycode_deploy_watch_textfile_collector.py
# (copied from origin/main PR #1194). Fallback: pristine build-tree copy.
# Consumer: node-exporter textfile
#   /home/frank/sycode-trading/monitoring/node-exporter-textfile/sycode_deploy_watch.prom
# scraped by sycodetrading-node-exporter (user=nobody) into Prometheus rules
# group sycode-deploy-watch then Alertmanager #critical-alerts.
#
# World-readable mode after write is mandatory: Python tempfile default mode
# is owner-only and node-exporter cannot scrape it (this is why the 2026-08-21
# one-shot left a success=1 file that Prometheus never saw).
set -uo pipefail

TEXTFILE_DIR="${TEXTFILE_DIR:-/home/frank/sycode-trading/monitoring/node-exporter-textfile}"
PROM="$TEXTFILE_DIR/sycode_deploy_watch.prom"
HERE="$(cd "$(dirname "$0")" && pwd)"
CANONICAL="${DEPLOY_WATCH_COLLECTOR_PY:-$HERE/sycode_deploy_watch_textfile_collector.py}"
if [ ! -f "$CANONICAL" ]; then
  CANONICAL="/home/frank/.hermes/deploy-state/build-tree/scripts/monitoring/sycode_deploy_watch_textfile_collector.py"
fi

export TEXTFILE_DIR
export DEPLOY_WATCH_STATE_PATH="${DEPLOY_WATCH_STATE_PATH:-/home/frank/.hermes/state/sycode-deploy-watch-state.json}"
export DEPLOY_WATCH_VERSION_URL="${DEPLOY_WATCH_VERSION_URL:-http://127.0.0.1:3001/version}"
export DEPLOY_WATCH_GIT_DIR="${DEPLOY_WATCH_GIT_DIR:-/home/frank/.hermes/deploy-state/build-tree}"
export DEPLOY_WATCH_FETCH="${DEPLOY_WATCH_FETCH:-0}"

mkdir -p "$TEXTFILE_DIR" /home/frank/.hermes/state /home/frank/logs

if [ ! -f "$CANONICAL" ]; then
  echo "sycode deploy-watch collector missing: $CANONICAL" >&2
  exit 1
fi

/usr/bin/flock -n /tmp/sycode-deploy-watch-textfile.lock /usr/bin/python3 "$CANONICAL"
rc=$?
if [ -f "$PROM" ]; then
  # install -m 0644 copies world-readable; python tempfile is owner-only.
  readable="$PROM.readable.$$"
  if /usr/bin/install -m 0644 "$PROM" "$readable"; then
    mv -f "$readable" "$PROM"
  else
    rm -f "$readable"
  fi
fi
exit "$rc"
