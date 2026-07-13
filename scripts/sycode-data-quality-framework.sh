#!/usr/bin/env bash
# Continuous Data Quality Monitoring & Self-Healing Watchdog
# Scheduled via Hermes Cron
#
# Explicit --board pin (systemic fix t_4f419b25): the diagnostic-card generator
# creates cards on a board slug; its dedup lookups now decode the board from the
# idempotency key, but pinning the board here too guarantees creation + dedup
# can never diverge if the on-disk `kanban/current` pin changes. The card's
# idempotency key embeds the board slug so all follow-up ops resolve correctly.
export HERMES_KANBAN_BOARD=jarvis-os
exec /home/frank/.hermes/scripts/sycode_data_quality_framework.py --heal
