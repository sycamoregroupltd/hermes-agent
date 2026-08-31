#!/usr/bin/env bash
# Start the ephemeral paper-broker Redis for the I2 signal-bus proof.
# Isolation: dedicated container/volume/port, never a shared/production Redis.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose -f docker-compose.yml up -d
for i in $(seq 1 20); do
  if docker exec i2-signal-bus-paper-redis redis-cli ping >/dev/null 2>&1; then
    echo "redis ready on 127.0.0.1:6479"
    exit 0
  fi
  sleep 0.5
done
echo "redis did not become ready" >&2
exit 1
