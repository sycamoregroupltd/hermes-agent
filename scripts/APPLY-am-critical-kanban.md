# Apply plan: critical Alertmanager → Hermes kanban (Isolation-safe)

**Status:** DRAFT only — do NOT live-apply until Frank/Jarvis Review GO.
**Branch:** `feat/t_am-critical-kanban-cards`
**Repo:** `sycamoregroupltd/hermes-agent` (live-lineage scripts tree)
**No hermes update, no money/A3/Deribit, no Telegram receivers, no cron schedule change.**

## Current path (live, confirmed 2026-09-04)

1. Alertmanager receivers → `http://sycode-alertmanager-oob-relay:8655/alertmanager`
2. Container bind-mount writes JSON → `~/.hermes/profiles/jarvis/state/alertmanager-spool/incoming`
3. Hermes cron `sycode-alertmanager-oob-spool-drain` (`abc411626232`, `*/1`, `no_agent`, `deliver=discord:#critical-alerts`)
4. Shim `~/.hermes/profiles/jarvis/scripts/sycode_alertmanager_spool_drain.py` → canonical `~/.hermes/scripts/sycode_alertmanager_spool_drain.py`
5. **Gap:** drain only prints Discord stdout — no kanban create (chat black hole for Frank)

## Proposed change

Extend canonical `scripts/sycode_alertmanager_spool_drain.py`:

- Keep Discord stdout path unchanged (secondary)
- For each alert with `severity=critical` OR `route=critical-alerts`:
  - **firing:** create/dedupe card on board `sycode-trading`, assignee `trading-devops`
  - **resolved:** comment + auto-complete only if card still `todo`/`ready`
- Dedup key: `sycode-am:{alertname}:{fingerprint}` (native AM fingerprint; labels hash fallback)
- Open-card check via sqlite; native `hermes kanban create --idempotency-key`; daily `:reopen:YYYYMMDD` if closed non-archived blocks recreate
- Comment cooldown 1h on repeat firing
- Fail-open: kanban errors never block spool archive / Discord emit
- Flags: `--selftest`, `--dry-run`, `--file <json>` (no consume)
- Kill switch: `ALERT_KANBAN_ENABLED=0` (default=1/ON is intentional for apply-time)

## Dry-run / verify (pre-apply)

```bash
python3 /path/to/new/sycode_alertmanager_spool_drain.py --selftest
SAMPLE=$(python3 -c "import glob; print(sorted(glob.glob(\"/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/archive/*.json\"))[-1])")
python3 /path/to/new/sycode_alertmanager_spool_drain.py --file "$SAMPLE"
```

## Live apply steps (needs explicit GO)

1. Backup live canonical script under `~/.hermes/scripts/`
2. Copy worktree script → `~/.hermes/scripts/sycode_alertmanager_spool_drain.py` and chmod +x
3. No cron edit (job `abc411626232` already runs every minute)
4. Confirm next drain emits `KANBAN: created|deduped...` and board cards appear
5. Rollback: restore backup OR set `ALERT_KANBAN_ENABLED=0`

## Out of scope / still needs GO

- Live copy into `~/.hermes/scripts/` (this PR does not apply)
- Merging this PR
- Changing Alertmanager routes / adding Telegram
- Hermes package update
