#!/usr/bin/env bash
#
# r8127 watchdog — link/route health probe (Task t_837875d3)
# ----------------------------------------------------------
# Invoked by r8127-watchdog.service (oneshot, every 30s via timer).
#
# The watchdog's job is NOT to recover the link — recovery (ip link
# down/up + driver reload) is delegated to the recovery script
# (t_00aa238c) in r8127-recover.sh. This probe only decides *whether*
# recovery is warranted and, when invoked as a plain health check,
# exits 0 on a healthy link so systemd considers the service happy.
#
# Health == BOTH of:
#   (1) default route present, AND
#   (2) carrier/operstate up on the monitored interface.
# A "healthy" result exits 0. A degraded result (route missing OR
# carrier down) exits non-zero so the caller — typically the
# recovery script or an alerting path — can act. The 30s timer re-arms
# every tick; sustained-down state is tracked by the recovery script
# to avoid flapping on transient blips.
#
# Must run as root (reads /sys + `ip route` requires CAP_NET_ADMIN on
# some hardened setups). Runs as a systemd service, so it is root by
# default; no explicit id check here to keep the unit output clean.
#
set -uo pipefail

IFACE="${WATCHDOG_IFACE:-enP7s7}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/run/r8127-watchdog}"
# When invoked with --recover-on-fail, hand off to the recovery script
# instead of just reporting. Recovery itself is guarded/throttled there.
RECOVER_SCRIPT="${WATCHDOG_RECOVER_SCRIPT:-/usr/local/sbin/r8127-recover.sh}"
DO_RECOVER="${WATCHDOG_RECOVER_ON_FAIL:-0}"

TS="$(date +%s)"
log() { printf '%s r8127-watchdog[%s]: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$IFACE" "$*"; }

carrier_up=0
if [[ -r "/sys/class/net/$IFACE/operstate" ]]; then
    op="$(cat "/sys/class/net/$IFACE/operstate" 2>/dev/null || true)"
    [[ "$op" == "up" ]] && carrier_up=1
fi

default_ok=0
if ip route show default 2>/dev/null | grep -q "dev $IFACE"; then
    default_ok=1
fi

healthy=0
if (( carrier_up == 1 )) && (( default_ok == 1 )); then
    healthy=1
    # Clear any sustained-down tracking so we don't immediately re-trigger.
    rm -f "$STATE_DIR/down_since" 2>/dev/null || true
else
    log "health: carrier_up=$carrier_up default_ok=$default_ok (unhealthy)"
fi

if (( healthy == 0 )); then
    rc=1
    if [[ "${DO_RECOVER:-$DO_RECOVER}" == "1" || "${DO_RECOVER}" == "1" ]] && \
       [[ "${1:-}" == "--recover" ]]; then
        rc=0  # recovery script owns its own exit semantics; do not mask it
        if [[ -x "$RECOVER_SCRIPT" ]]; then
            "$RECOVER_SCRIPT" "$IFACE" || rc=$?
        else
            log "ESCALATION: link unhealthy and recovery script '$RECOVER_SCRIPT' not installed"
            rc=3
        fi
    fi
    exit "$rc"
fi

# Healthy.
log "health: OK (carrier up, default route present via $IFACE)"
exit 0
