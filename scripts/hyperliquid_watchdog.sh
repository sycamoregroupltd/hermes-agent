#!/bin/bash
# Hyperliquid poller watchdog — checks sycodetrading-server container health
# and restarts if dead. Quiet when healthy (no output = silent = all good).
# Designed for no_agent cron mode.

CONTAINER="sycodetrading-server"
COMPOSE_DIR="/home/frank/sycode-trading"
HEALTH_URL="http://localhost:3001/health"

# Check if container is running and healthy
STATUS=$(docker inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null)
HEALTH=$(docker inspect "$CONTAINER" --format '{{.State.Health.Status}}' 2>/dev/null)

if [ "$STATUS" != "running" ] || [ "$HEALTH" != "healthy" ]; then
    echo "[$(date -Iseconds)] WATCHDOG: $CONTAINER status=$STATUS health=$HEALTH — restarting"
    cd "$COMPOSE_DIR" && docker compose up -d server 2>&1
    echo "[$(date -Iseconds)] WATCHDOG: Restart issued, waiting 15s for readiness"
    sleep 15
    
    # Verify recovery
    RECOVERED=$(docker inspect "$CONTAINER" --format '{{.State.Status}}/{{.State.Health.Status}}' 2>/dev/null)
    echo "[$(date -Iseconds)] WATCHDOG: Post-restart status=$RECOVERED"
    exit 0
fi

# Also check that the HTTP server responds (double-check beyond Docker health)
HTTP_CHECK=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" 2>/dev/null)
if [ "$HTTP_CHECK" != "200" ]; then
    echo "[$(date -Iseconds)] WATCHDOG: $CONTAINER Docker healthy but HTTP $HTTP_CHECK — restarting"
    cd "$COMPOSE_DIR" && docker compose up -d server 2>&1
    sleep 15
    echo "[$(date -Iseconds)] WATCHDOG: Post-restart — container $(docker inspect "$CONTAINER" --format '{{.State.Status}}/{{.State.Health.Status}}' 2>/dev/null), HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" 2>/dev/null)"
fi

# Silent exit when healthy — watchdog pattern
