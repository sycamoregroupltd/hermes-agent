#!/bin/bash
# Check if relay is alive, restart if not
RELAY_PORT=8644
RELAY_SCRIPT="/home/frank/sycode-trading-production/scripts/discord_kill_switch_relay.py"

if ! ss -tlnp | grep -q ":${RELAY_PORT} "; then
    TOKEN=$(grep DISCORD_BOT_TOKEN /home/frank/.hermes/profiles/jarvis/.env | cut -d'=' -f2)
    if [ -n "$TOKEN" ]; then
        DISCORD_BOT_TOKEN="$TOKEN" nohup python3 "$RELAY_SCRIPT" > /dev/null 2>&1 &
        echo "Relay restarted (PID $!)"
    else
        echo "Cannot restart relay: DISCORD_BOT_TOKEN not found"
    fi
fi
