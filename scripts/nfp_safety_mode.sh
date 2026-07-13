#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# ==============================================================================
# nfp_safety_mode.sh — NFP Safety Mode for Trading System
#
# Monitors NFP (Nonfarm Payrolls) release schedule and automatically disables
# trading strategies during the high-volatility window around NFP releases.
#
# NFP schedule: 12:30 UTC on the first Friday of every month.
# Safety window: 6 hours before release (06:30 UTC) through 2 hours after (14:30 UTC).
#
# Usage:
#   ./nfp_safety_mode.sh check   — Detect NFP window, disable strategies if in window
#   ./nfp_safety_mode.sh reset   — Re-enable all strategies that were disabled by NFP mode
#   ./nfp_safety_mode.sh status  — Show current NFP-window status (dry-run, no DB changes)
# ==============================================================================
set -euo pipefail

export PATH="$HOME/.bun/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

REPO=/home/frank/sycode-trading
REPORT_DIR="$REPO/reports/strategy-promotion-funnel"
mkdir -p "$REPORT_DIR"
NFP_LOG="$REPORT_DIR/nfp-events.md"
NFP_STATE="$REPORT_DIR/.nfp_enabled_state.json"

# ------------------------------------------------------------------------------
# NFP Schedule
# ------------------------------------------------------------------------------
NFP_RELEASE_HOUR=12
NFP_RELEASE_MINUTE=30
NFP_SAFETY_WINDOW_BEFORE_HOURS=6
NFP_SAFETY_WINDOW_AFTER_HOURS=2

# ------------------------------------------------------------------------------
# Utility: log to nfp-events.md
# ------------------------------------------------------------------------------
log_event() {
    local level="$1"    # INFO, WARN, ACTION
    local message="$2"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    echo "[${timestamp}] [${level}] ${message}" >> "$NFP_LOG"
    echo "[${timestamp}] [${level}] ${message}"
}

init_log() {
    if [ ! -f "$NFP_LOG" ]; then
        cat > "$NFP_LOG" << 'LOGHEADER'
# NFP Safety Mode Events

| Timestamp | Level | Event |
|-----------|-------|-------|
LOGHEADER
    fi
}

# ------------------------------------------------------------------------------
# Utility: calculate the 1st Friday of a given month
#   get_first_friday YYYY MM
#   returns: YYYY-MM-DD
# ------------------------------------------------------------------------------
get_first_friday() {
    local year="$1"
    local month="$2"
    # Day of week for the 1st of the month: 0=Sun, 1=Mon ... 6=Sat
    local dow
    dow=$(date -d "${year}-${month}-01" '+%w' 2>/dev/null)
    # Calculate days to add to reach Friday (dow=5)
    # If dow <= 5, add (5 - dow) days
    # If dow > 5 (Sat=6), add (5 - dow + 7) = (12 - dow) days
    if [ "$dow" -le 5 ]; then
        echo "${year}-${month}-$(( 1 + 5 - dow ))"
    else
        echo "${year}-${month}-$(( 1 + 12 - dow ))"
    fi
}

# ------------------------------------------------------------------------------
# Utility: get current UTC timestamp components
# ------------------------------------------------------------------------------
get_current_utc() {
    local year month day hour minute dow
    year=$(date -u '+%Y')
    month=$(date -u '+%m')
    day=$(date -u '+%d')
    hour=$(date -u '+%H')
    minute=$(date -u '+%M')
    dow=$(date -u '+%w')  # 0=Sun, 5=Fri, 6=Sat
    echo "$year $month $day $hour $minute $dow"
}

# ------------------------------------------------------------------------------
# Compute NFP release timestamp (UNIX epoch seconds)
# ------------------------------------------------------------------------------
get_nfp_release_epoch() {
    local year="$1"
    local month="$2"
    local nfp_date
    nfp_date=$(get_first_friday "$year" "$month")
    date -d "${nfp_date} ${NFP_RELEASE_HOUR}:${NFP_RELEASE_MINUTE}:00 UTC" '+%s' 2>/dev/null
}

# ------------------------------------------------------------------------------
# Check if current time is within the NFP safety window
# Returns 0 (true) if in window, 1 (false) otherwise
# ------------------------------------------------------------------------------
is_in_nfp_window() {
    read -r year month day hour minute dow <<< "$(get_current_utc)"

    # Determine current NFP month (check this month and next month)
    local nfp_epoch_now nfp_epoch_next now_epoch
    nfp_epoch_now=$(get_nfp_release_epoch "$year" "$((10#$month))" 2>/dev/null)
    
    # If next month (e.g., on the 31st when NFP is next month)
    if [ "$((10#$month))" -eq 12 ]; then
        nfp_epoch_next=$(get_nfp_release_epoch "$((year + 1))" 1 2>/dev/null)
    else
        nfp_epoch_next=$(get_nfp_release_epoch "$year" "$((10#$month + 1))" 2>/dev/null)
    fi
    
    now_epoch=$(date -u '+%s')

    # Pick the nearest NFP release (this month or next)
    local nfp_epoch="$nfp_epoch_now"
    if [ "$now_epoch" -gt "$nfp_epoch_now" ] && [ "$now_epoch" -le "$nfp_epoch_next" ]; then
        nfp_epoch="$nfp_epoch_next"
    fi

    local window_start=$(( nfp_epoch - NFP_SAFETY_WINDOW_BEFORE_HOURS * 3600 ))
    local window_end=$(( nfp_epoch + NFP_SAFETY_WINDOW_AFTER_HOURS * 3600 ))

    if [ "$now_epoch" -ge "$window_start" ] && [ "$now_epoch" -le "$window_end" ]; then
        echo "$nfp_epoch"
        return 0
    fi
    return 1
}

# ------------------------------------------------------------------------------
# Get NFP date info as human-readable string
# ------------------------------------------------------------------------------
get_nfp_info() {
    read -r year month day hour minute dow <<< "$(get_current_utc)"
    local nfp_date
    nfp_date=$(get_first_friday "$year" "$((10#$month))")
    local nfp_epoch
    nfp_epoch=$(get_nfp_release_epoch "$year" "$((10#$month))" 2>/dev/null)
    echo "NFP date: ${nfp_date}, release: ${NFP_RELEASE_HOUR}:${NFP_RELEASE_MINUTE} UTC, window: $(date -u -d "@$(( nfp_epoch - NFP_SAFETY_WINDOW_BEFORE_HOURS * 3600 ))" '+%Y-%m-%dT%H:%MZ') to $(date -u -d "@$(( nfp_epoch + NFP_SAFETY_WINDOW_AFTER_HOURS * 3600 ))" '+%Y-%m-%dT%H:%MZ')"
}

# ------------------------------------------------------------------------------
# DB helpers via docker psql
# ------------------------------------------------------------------------------
db_query() {
    docker exec sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -t -A -F'|' -c "$1" 2>/dev/null
}

db_exec() {
    docker exec sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -c "$1" 2>/dev/null
}

# ------------------------------------------------------------------------------
# Get all currently enabled strategies (id, name)
# ------------------------------------------------------------------------------
get_enabled_strategies() {
    db_query "SELECT id, name FROM strategies WHERE enabled = true ORDER BY name"
}

# ------------------------------------------------------------------------------
# Get count of enabled strategies
# ------------------------------------------------------------------------------
get_enabled_count() {
    db_query "SELECT count(*) FROM strategies WHERE enabled = true"
}

# ------------------------------------------------------------------------------
# Disable all enabled strategies (return their IDs for state storage)
# ------------------------------------------------------------------------------
disable_all_strategies() {
    log_event "ACTION" "Disabling ALL enabled strategies for NFP safety"
    
    # Get & store enabled strategies before disabling
    local enabled_list
    enabled_list=$(get_enabled_strategies)
    local count
    count=$(echo "$enabled_list" | grep -c '|' 2>/dev/null || echo "0")
    
    if [ "$count" -eq 0 ] || [ "$count" = "0" ]; then
        log_event "INFO" "No enabled strategies to disable"
        return 0
    fi
    
    # Store state as JSON
    echo '{"timestamp":"'"$(date -u '+%Y-%m-%dT%H:%M:%SZ')"'","nfp_date":"'"$(get_first_friday "$(date -u '+%Y')" "$(date -u '+%m')")"'"' > "$NFP_STATE"
    echo ',"strategies":[' >> "$NFP_STATE"
    
    local first=true
    while IFS='|' read -r sid sname; do
        [ -z "$sid" ] && continue
        if [ "$first" = true ]; then
            first=false
        else
            echo "," >> "$NFP_STATE"
        fi
        echo '{"id":"'"$sid"'","name":"'"$sname"'"}' >> "$NFP_STATE"
    done <<< "$enabled_list"
    
    echo ']}' >> "$NFP_STATE"
    
    # Disable each strategy
    while IFS='|' read -r sid sname; do
        [ -z "$sid" ] && continue
        log_event "ACTION" "Disabling strategy: $sname ($sid)"
        db_exec "UPDATE strategies SET enabled = false, updated_at = NOW(), version = version + 1 WHERE id = '$sid' AND enabled = true" > /dev/null 2>&1 || true
    done <<< "$enabled_list"
    
    # Refresh cache
    refresh_strategy_cache
    
    local after_count
    after_count=$(get_enabled_count)
    log_event "INFO" "NFP safety disable complete. Previously enabled: $count, Now enabled: $after_count"
}

# ------------------------------------------------------------------------------
# Re-enable strategies that were disabled by NFP mode
# ------------------------------------------------------------------------------
reenable_strategies() {
    if [ ! -f "$NFP_STATE" ]; then
        log_event "WARN" "No NFP state file found — nothing to re-enable"
        return 0
    fi
    
    log_event "ACTION" "Re-enabling strategies after NFP window"
    
    local count
    count=$(grep -c '"id"' "$NFP_STATE" 2>/dev/null || echo "0")
    
    if [ "$count" -eq 0 ]; then
        log_event "INFO" "No strategies to re-enable"
        rm -f "$NFP_STATE"
        return 0
    fi
    
    # Extract IDs and re-enable
    local ids
    ids=$(grep '"id"' "$NFP_STATE" | sed 's/.*"id":"\([^"]*\)".*/\1/')
    
    local reenabled=0
    while IFS= read -r sid; do
        [ -z "$sid" ] && continue
        # Get the name from state
        local sname
        sname=$(grep "$sid" "$NFP_STATE" | sed 's/.*"name":"\([^"]*\)".*/\1/')
        log_event "ACTION" "Re-enabling strategy: $sname ($sid)"
        db_exec "UPDATE strategies SET enabled = true, updated_at = NOW(), version = version + 1 WHERE id = '$sid' AND enabled = false" > /dev/null 2>&1 || true
        reenabled=$((reenabled + 1))
    done <<< "$ids"
    
    # Refresh cache
    refresh_strategy_cache
    
    local after_count
    after_count=$(get_enabled_count)
    log_event "INFO" "NFP re-enable complete. Re-enabled: $reenabled, Now enabled: $after_count"
    
    # Archive the state file
    mv "$NFP_STATE" "${NFP_STATE}.$(date -u '+%Y%m%d_%H%M%S').bak" 2>/dev/null || true
}

# ------------------------------------------------------------------------------
# Refresh strategy cache via admin JWT
# ------------------------------------------------------------------------------
refresh_strategy_cache() {
    log_event "INFO" "Refreshing strategy cache"
    local admin_token
    admin_token=$(docker exec sycodetrading-server /usr/local/bin/bun -e '
        import {SignJWT} from "jose";
        const s = new TextEncoder().encode(process.env.JWT_SECRET);
        const t = await new SignJWT({
            userId: "jarvis-admin",
            email: "jarvis-admin@sycode.local",
            role: "admin"
        })
        .setProtectedHeader({ alg: "HS256" })
        .setIssuedAt()
        .setExpirationTime("1h")
        .sign(s);
        console.log(t);
    ' 2>/dev/null) || {
        log_event "WARN" "Failed to generate admin JWT for cache refresh"
        return 1
    }
    
    local http_code
    http_code=$(curl -s -o /dev/null -w '%{http_code}' \
        -X POST http://localhost:3001/api/strategies/cache/refresh \
        -H "Authorization: Bearer ${admin_token}" \
        -H "Content-Type: application/json" 2>/dev/null) || http_code="000"
    
    if [ "$http_code" = "200" ]; then
        log_event "INFO" "Strategy cache refreshed successfully (HTTP $http_code)"
    else
        log_event "WARN" "Strategy cache refresh returned HTTP $http_code"
    fi
}

# ------------------------------------------------------------------------------
# Status mode — dry-run: show NFP info without modifying DB
# ------------------------------------------------------------------------------
status_mode() {
    echo "======================================"
    echo "  NFP Safety Mode — Status"
    echo "======================================"
    echo ""
    
    local nfp_info
    nfp_info=$(get_nfp_info)
    echo "Schedule: $nfp_info"
    echo ""
    
    local enabled_list
    enabled_list=$(get_enabled_strategies)
    local count
    count=$(echo "$enabled_list" | grep -c '|' 2>/dev/null || echo "0")
    echo "Currently enabled strategies: $count"
    if [ "$count" -gt 0 ]; then
        echo "$enabled_list" | while IFS='|' read -r sid sname; do
            [ -z "$sid" ] && continue
            echo "  - $sname ($sid)"
        done
    fi
    echo ""
    
    # Check NFP window
    local nfp_epoch
    if nfp_epoch=$(is_in_nfp_window); then
        local now_epoch
        now_epoch=$(date -u '+%s')
        local window_start=$(( nfp_epoch - NFP_SAFETY_WINDOW_BEFORE_HOURS * 3600 ))
        local window_end=$(( nfp_epoch + NFP_SAFETY_WINDOW_AFTER_HOURS * 3600 ))
        
        echo "⚠️  INSIDE NFP SAFETY WINDOW ⚠️"
        echo "Window: $(date -u -d "@$window_start" '+%Y-%m-%dT%H:%MZ') → $(date -u -d "@$window_end" '+%Y-%m-%dT%H:%MZ')"
        echo "NFP release: $(date -u -d "@$nfp_epoch" '+%Y-%m-%dT%H:%MZ')"
        echo ""
        echo "Action required: DISABLE strategies"
    else
        echo "✅ Outside NFP safety window — no action needed"
    fi
    
    echo ""
    echo "State file: ${NFP_STATE}"
    [ -f "$NFP_STATE" ] && echo "  EXISTS (strategies stored for re-enable)" || echo "  (not set)"
    echo "Log file: ${NFP_LOG}"
}

# ------------------------------------------------------------------------------
# Check mode — detect NFP window and disable if needed
# ------------------------------------------------------------------------------
check_mode() {
    init_log
    log_event "INFO" "NFP safety check running"
    
    local nfp_info
    nfp_info=$(get_nfp_info)
    log_event "INFO" "NFP schedule: $nfp_info"
    
    local enabled_count
    enabled_count=$(get_enabled_count)
    log_event "INFO" "Currently enabled strategies: $enabled_count"
    
    local nfp_epoch
    if nfp_epoch=$(is_in_nfp_window); then
        log_event "WARN" "INSIDE NFP SAFETY WINDOW — disabling strategies"
        disable_all_strategies
    else
        log_event "INFO" "Outside NFP window — no action needed"
    fi
}

# ------------------------------------------------------------------------------
# Reset mode — re-enable strategies after NFP passes
# ------------------------------------------------------------------------------
reset_mode() {
    init_log
    log_event "INFO" "NFP reset running — re-enabling strategies"
    
    if [ -f "$NFP_STATE" ]; then
        reenable_strategies
    else
        log_event "INFO" "No NFP state file found; checking if any strategies are still disabled"
        # If none disabled, nothing to do
        enabled_count=$(get_enabled_count)
        log_event "INFO" "Currently enabled strategies: $enabled_count"
    fi
}

# ==============================================================================
# Main
# ==============================================================================
case "${1:-check}" in
    check)
        check_mode
        ;;
    reset)
        reset_mode
        ;;
    status)
        status_mode
        ;;
    *)
        echo "Usage: $0 {check|reset|status}"
        echo ""
        echo "  check   — Detect NFP window and disable strategies if needed"
        echo "  reset   — Re-enable strategies disabled by NFP safety mode"
        echo "  status  — Show NFP status without making changes"
        exit 1
        ;;
esac
