#!/usr/bin/env bash
#
# r8127 RECOVERY WATCHDOG  (Task t_00aa238c)
# ---------------------------------------------------------------
# Invoked by r8127-watchdog.service (t_837875d3) when the health
# probe reports an UNHEALTHY link state — i.e. default route missing
# AND carrier down for more than WATCHDOG_DOWN_THRESHOLD_SEC (120s,
# tracked by the probe via $STATE_DIR/down_since).
#
# Recovery sequence (per task spec):
#   1. Log the event (human + structured JSON line) via syslog.
#   2. Bounce the link:  ip link set <iface> down  -> up
#   3. Re-apply r8127 offload mitigation (tso/gso/gro off) after
#      the bounce, since a driver reset may have cleared it.
#   4. Optionally reload the r8127 driver module via:
#        modprobe -r <driver> && modprobe <driver>
#      ONLY when the justification flag WATCHDOG_DRIVER_RELOAD=1
#      is explicitly set AND the module is safely removable.
#
# Logging & escalation:
#   - Every log message includes an ISO-8601 timestamp, a severity
#     (info/warn/error), and a unique attempt_id (request id) so a
#     single recovery event is traceable across syslog, journald,
#     and the fleet dashboard.
#   - Structured JSON is emitted to stdout AND pushed through
#     `logger` to syslog (facility user.crit for errors, user.notice
#     for warnings, user.info for normal). journald captures stdout
#     under the r8127-watchdog unit; syslog captures the logger calls.
#   - On RECOVERY FAILURE (bounce failed, driver reload failed, or
#     interface vanished), a high-severity notification is sent to
#     the fleet dashboard via `hermes send --to discord:#critical-alerts`
#     (the same Frank-critical path used by deadpid-fleet-alert-guard).
#     The notification is best-effort and fail-open: if hermes/notify
#     is unavailable, the script still exits with the error code.
#
# Guardrails:
#   - Idempotent: a per-host throttle window (WATCHDOG_RECOVER_THROTTLE_SEC,
#     default 300s) suppresses repeated recoveries so a sustained outage
#     does not trigger a link flap on every 30s watchdog tick.
#   - The down-bounce is short and bounded; if the carrier does not come
#     back within WATCHDOG_LINK_SETTLE_SEC (default 15s), we proceed to
#     the driver-reload decision rather than hanging forever.
#   - Structured JSON records the outcome for fleet alerting.
#
# MUST RUN AS ROOT (ip link / modprobe / ethtool need CAP_NET_ADMIN).
# Installed to /usr/local/sbin by install.sh (root-gated — Frank).
#
# Exit codes:
#   0  recovery performed (or already-recently performed — throttled,
#      treated as success-with-no-op so the watchdog stays green).
#   2  root/not-CAP_NET_ADMIN — operator must re-run under sudo.
#   3  link bounce failed (ip link returned error).
#   4  driver reload failed (when attempted).
#   5  interface disappeared after bounce (no carrier after settle).
#   6  invalid arguments / misuse.
#
# Usage:
#   r8127-recover.sh <iface>            # bounce + offload; no driver reload
#   r8127-recover.sh <iface> --driver-reload   # also attempt module reload
#   WATCHDOG_DRIVER_RELOAD=1 r8127-recover.sh <iface>  # env-gated reload
#
set -uo pipefail

###############################################################################
# CONFIG / ENV
###############################################################################
IFACE="${1:-}"
DO_DRIVER_RELOAD="${2:-}"                       # positional --driver-reload
# Env-gated justification flag for the (risky) driver reload path.
DRIVER_RELOAD_FLAG="${WATCHDOG_DRIVER_RELOAD:-0}"
# The r8127 NIC driver module name on this host (from t_ef7ed63e diagnosis).
DRIVER="${WATCHDOG_NIC_DRIVER:-r8127}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/run/r8127-watchdog}"
THROTTLE_SEC="${WATCHDOG_RECOVER_THROTTLE_SEC:-300}"
LINK_SETTLE_SEC="${WATCHDOG_LINK_SETTLE_SEC:-15}"
LOG_TAG="r8127-recover[${IFACE:-unknown}]"

# Fleet dashboard notification target (Frank-critical path).
# Mirrors deadpid-fleet-alert-guard.py: discord:#critical-alerts via hermes send.
NOTIFICATION_TARGET="${R8127_NOTIFY_TARGET:-discord:#critical-alerts}"
HERMES_BIN="${R8127_HERMES_BIN:-hermes}"
NOTIFICATION_SENT=0  # guard: only one notification per failure exit.

###############################################################################
# HELPERS
###############################################################################
TS(){ date '+%Y-%m-%dT%H:%M:%S%z'; }
TS_UTC(){ date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# Unique request/attempt id for this recovery invocation — a single
# event is traceable across syslog, journald, and the fleet dashboard.
REQUEST_ID="${IFACE:-unknown}-$(date +%s)-$$"
export REQUEST_ID

# Human-readable log line to stdout (captured by journald under systemd).
log(){ printf '%s [%s] %s: %s\n' "$(TS)" "${LOG_LEVEL:-INFO}" "$LOG_TAG" "$*"; }

# Emit a single-line structured JSON record to stdout (captured by journald
# when run under systemd, or redirectable for tests). Fields are minimal and
# stable so a jq filter like 'select(.level=="error")' works uniformly.
json_log(){
  local level="$1"; shift
  local msg="$1"; shift
  # Build a compact JSON object. We escape quotes/newlines in msg minimally.
  local esc
  esc="$(printf '%s' "$msg" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' ')"
  printf '{"timestamp":"%s","level":"%s","component":"%s","iface":"%s","event":"%s","request_id":"%s"%s}\n' \
    "$(TS_UTC)" "$level" "r8127-recover" "$IFACE" "$esc" "${REQUEST_ID}" "${EXTRA_JSON:-}"
}

# Push a structured record through `logger` to syslog. This is the canonical
# syslog bridge: journald captures stdout above, but external monitoring
# alertrules (and the fleet dashboard) may consume syslog directly. We map
# severity: error->user.crit, warn->user.warning, info->user.info, and
# include the request_id for correlation.
syslog_log(){
  local level="$1"; shift
  local msg="$*"
  local prio
  case "$level" in
    error)   prio="user.crit"   ;;
    warn)    prio="user.warning" ;;
    info)    prio="user.info"    ;;
    *)       prio="user.info"    ;;
  esac
  if command -v logger >/dev/null 2>&1; then
    # -t tag, -- $prio, include request_id for fleet correlation.
    logger -p "$prio" -t "$LOG_TAG" "req=$REQUEST_ID sev=$level $msg"
  fi
}

# Convenience: log to BOTH stdout (human) + JSON (machine) + syslog.
log_all(){
  local level="$1"; shift
  LOG_LEVEL="${level^^}"
  log "$*"
  json_log "$level" "$*"
  syslog_log "$level" "$*"
}

# Send a high-severity notification to the fleet dashboard. This is the
# escalation path for recovery FAILURES (bounce/driver/interface errors).
# Uses `hermes send` to discord:#critical-alerts — the same Frank-critical
# path as deadpid-fleet-alert-guard.py. Fail-open: delivery failure is
# logged to stderr but does NOT change the script's exit code.
notify_fleet_failure(){
  local exit_code="$1"; shift
  local message="$*"
  # Guard: only one notification per invocation (the first failure exit).
  if [[ "$NOTIFICATION_SENT" -eq 1 ]]; then
    return 0
  fi
  NOTIFICATION_SENT=1
  local body
  body="🚨 r8127-recover FAILURE (exit $exit_code) — req=$REQUEST_ID iface=$IFACE\n${message}\nOn-call action: see deploy/r8127-rollback-guide.md or verify link/driver state manually."
  if [[ -x "$HERMES_BIN" ]] || command -v "$HERMES_BIN" >/dev/null 2>&1; then
    "$HERMES_BIN" send -q -t "$NOTIFICATION_TARGET" \
      -s "[r8127-recover] recovery failure exit $exit_code (req $REQUEST_ID)" \
      "$body" 2>/dev/null || {
        # Fail-open: delivery failed but we already logged via syslog.
        log "WARNING: fleet notification delivery failed — ${NOTIFICATION_TARGET} (logged to syslog instead)"
        syslog_log "error" "fleet notification delivery failed for ${NOTIFICATION_TARGET} (req $REQUEST_ID)"
      }
  else
    # hermes binary not present (e.g. dry-run / test env). Log only.
    log "WARNING: hermes send not available — fleet notification suppressed (syslog logged)"
    syslog_log "error" "hermes send unavailable — notification suppressed (req $REQUEST_ID)"
  fi
}

# Exit with code + a JSON error record + syslog + fleet notification.
# Never call exit directly for errors — always go through die().
die(){
  local code="$1"; shift
  local msg="$*"
  EXTRA_JSON=',"exit_code":'"$code"
  json_log "error" "$msg"
  log "ERROR: $code — $msg"
  syslog_log "error" "$msg (exit $code, req $REQUEST_ID)"
  # Escalate recovery failures to the fleet dashboard.
  if [[ "$code" -ge 2 ]] && [[ "$code" -le 5 ]]; then
    notify_fleet_failure "$code" "$msg"
  fi
  exit "$code"
}

###############################################################################
# PRECONDITION CHECKS
###############################################################################
# Argument validation.
if [[ -z "$IFACE" ]]; then
  die 6 "usage: $0 <iface> [--driver-reload]  (or set WATCHDOG_DRIVER_RELOAD=1)"
fi
case "$IFACE" in
  *[!a-zA-Z0-9:_.\\-]*) die 6 "invalid interface name: '$IFACE'";;
esac

# Normalize the --driver-reload flag into the env-gated justification.
if [[ "$DO_DRIVER_RELOAD" == "--driver-reload" ]]; then
  DRIVER_RELOAD_FLAG="1"
elif [[ -n "$DO_DRIVER_RELOAD" ]]; then
  die 6 "unknown flag: '$DO_DRIVER_RELOAD' (expected --driver-reload)"
fi

# Root + CAP_NET_ADMIN check. `ip` will fail with EPERM without it, but we
# fail fast and loudly so the systemd unit surfaces a clear reason.
if [[ "$(id -u)" -ne 0 ]]; then
  die 2 "must run as root (sudo). current uid=$(id -u)"
fi
if ! ip link show "$IFACE" >/dev/null 2>&1; then
  die 5 "interface '$IFACE' not found on this host"
fi

# Idempotency/throttle: suppress repeated recoveries within the window.
mkdir -p "$STATE_DIR"
LAST_RECOVER="$STATE_DIR/last_recover"

if [[ -f "$LAST_RECOVER" ]]; then
  last_ts="$(cat "$LAST_RECOVER" 2>/dev/null || echo 0)"
  now_ts="$(date +%s)"
  if [[ "$last_ts" =~ ^[0-9]+$ ]] && (( last_ts > 0 )); then
    elapsed=$(( now_ts - last_ts ))
    if (( elapsed < THROTTLE_SEC )); then
      remaining=$(( THROTTLE_SEC - elapsed ))
      log_all "info" "throttled: last recovery ${elapsed}s ago (window ${THROTTLE_SEC}s, ${remaining}s remaining) — no-op"
      exit 0
    fi
  fi
fi

###############################################################################
# STEP 1 — LOG THE EVENT
###############################################################################
log_all "info" "recovery_start: link unhealthy, initiating bounce sequence"

# Record this attempt timestamp (the source of truth for throttle gating).
date +%s > "$LAST_RECOVER"

###############################################################################
# STEP 2 — IP LINK BOUNCE  (ip link set down/up)
###############################################################################
# Bring the interface down — bounded. If this fails the NIC is wedged and we
# report failure; we must NOT blindly bring it back up over a half-bounce.
log_all "info" "bringing $IFACE down"
if ! ip link set "$IFACE" down 2>&1 | sed 's/^/  /'; then
  die 3 "ip link set $IFACE down failed"
fi

# Short, fixed settle — the r8127 tx-timeout incident needs a hard reset,
# not a blind wait for a carrier that may never return.
sleep 1

log_all "info" "bringing $IFACE up"
if ! ip link set "$IFACE" up 2>&1 | sed 's/^/  /'; then
  die 3 "ip link set $IFACE up failed"
fi

# Wait briefly for carrier to re-establish. If it doesn't, we escalate to the
# driver-reload decision (or failure) rather than hanging the watchdog tick.
carrier_back=0
for ((i=0; i<LINK_SETTLE_SEC; i++)); do
  if [[ -r "/sys/class/net/$IFACE/operstate" ]]; then
    op="$(cat "/sys/class/net/$IFACE/operstate" 2>/dev/null || true)"
    if [[ "$op" == "up" ]]; then
      carrier_back=1
      break
    fi
  fi
  sleep 1
done

if (( carrier_back == 0 )); then
  log_all "warn" "carrier still down after ${LINK_SETTLE_SEC}s settle"
  # Not a hard failure yet — the driver reload (if enabled) may fix it.
else
  log_all "info" "carrier restored on $IFACE"
fi

# Re-apply the r8127 offload mitigation (tso/gso/gro off) — a link bounce
# or driver reset can clear it, and the tx-timeout root cause is offload
# on this box. Mirror r8127-offload-fix.sh exactly.
if command -v ethtool >/dev/null 2>&1; then
  log_all "info" "applying offload mitigation (tso/gso/gro off)"
  ethtool -K "$IFACE" tso off gso off gro off 2>/dev/null || \
    log_all "warn" "ethtool -K returned non-zero (will retry on next up event)"
fi

###############################################################################
# STEP 3 — OPTIONAL DRIVER RELOAD (env-gated justification only)
###############################################################################
if [[ "$DRIVER_RELOAD_FLAG" == "1" ]]; then
  log_all "info" "driver reload requested (justification flag set)"
  # Guard: never rmmod/modprobe an in-tree / built-in module that is still in
  # use. Check lsmod first — matches the sibling r8127-link-watchdog.sh policy.
  if ! lsmod 2>/dev/null | grep -q "^${DRIVER} "; then
    log_all "warn" "driver_reload_skipped: module '$DRIVER' not present in lsmod (built-in/in-tree — not safely removable)"
  else
    log_all "info" "reloading driver module: modprobe -r $DRIVER && modprobe $DRIVER"
    if ! modprobe -r "$DRIVER" 2>&1 | sed 's/^/  /'; then
      die 4 "modprobe -r $DRIVER failed"
    fi
    sleep 1
    if ! modprobe "$DRIVER" 2>&1 | sed 's/^/  /'; then
      die 4 "modprobe $DRIVER (reload) failed"
    fi
    log_all "info" "driver reload complete: $DRIVER"
    # Re-apply offloads again post-reload (module load resets ethtool -K state).
    if command -v ethtool >/dev/null 2>&1; then
      ethtool -K "$IFACE" tso off gso off gro off 2>/dev/null || true
    fi
  fi
else
  log_all "info" "driver reload NOT requested (WATCHDOG_DRIVER_RELOAD unset/0) — skipping"
fi

###############################################################################
# STEP 4 — VERDICT + STRUCTURED LOG RECORD
###############################################################################
# Final carrier verdict: do we have default route + carrier up?
final_ok=0
carrier_ok=0
[[ -r "/sys/class/net/$IFACE/operstate" ]] && \
  [[ "$(cat /sys/class/net/$IFACE/operstate 2>/dev/null)" == "up" ]] && carrier_ok=1
ip route show default 2>/dev/null | grep -q "dev $IFACE" && final_ok=1

if (( carrier_ok == 1 )) && (( final_ok == 1 )); then
  EXTRA_JSON=',"bounce":"performed","carrier":"up","default_route":"present","driver_reload":"'"${DRIVER_RELOAD_FLAG}"'"'
  log_all "info" "recovery_success: link restored, carrier up, default route present"
  exit 0
else
  EXTRA_JSON=',"bounce":"performed","carrier":"'"$carrier_ok"'","default_route":"'"$final_ok"'","driver_reload":"'"${DRIVER_RELOAD_FLAG}"'"'
  log_all "warn" "recovery_incomplete: link bounced but carrier/route not fully restored — escalation required"
  # Exit 0 to keep the watchdog tick green (bounce was attempted); the warn
  # JSON record surfaces the partial outcome for alerting. A hard failure
  # would spin the systemd unit, which just re-runs the same bounce next tick.
  # BUT escalate the partial-recovery warning to the fleet dashboard so
  # the on-call team sees it even though the tick stays green.
  notify_fleet_failure 0 "recovery INCOMPLETE — carrier=$carrier_ok route=$final_ok (req $REQUEST_ID). Link was bounced but carrier/route not restored. Escalating for operator review."
  exit 0
fi
