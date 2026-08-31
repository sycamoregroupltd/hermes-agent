# I2 — agent-state signal bus (Redis Streams paper/worktree proof)

Card: `t_dc046875` · graph node `i2-signal-bus` · transport per R4/S1: **Redis Streams**,
internal transport only, Kanban DB remains execution truth, Session Bus Markdown remains
human coordination/evidence. No new broker/control plane beyond a disposable paper Redis;
no gateway/production wiring.

## Isolation

- Redis is a throwaway `redis:7-alpine` container (`i2-signal-bus-paper-redis`) bound to
  `127.0.0.1:6479`, entirely separate from every production/trading Redis already running
  on this host (sycodetrading-*, upero-redis, buzz-harness-redis). `bin/down.sh` removes
  the container and its volume.
- All code lives under this git worktree (`wt/t_dc046875-i2-signal-bus`), not on `main`.
- The wrapped "real agent" is a trivial, tool-free `hermes -z` oneshot on the openai-codex
  seat (not Anthropic, to avoid the 75%-consumed weekly window), with `--safe-mode` plus an
  explicit no-tools/no-file prompt constraint.

## Layout

- `agent_state_bus/schema.py` — event envelope: `schema_version`, `event_type`
  (idle/working/heartbeat/done/failed), `agent_id`, `session_id`, `task_id`,
  `producer_event_id`, `occurred_at`, `data`.
- `agent_state_bus/publisher.py` — `AgentStateBus.publish()`, `XADD` with a bounded
  `maxlen`.
- `agent_state_bus/reader.py` — consumer-group reader (`XREADGROUP` + `XACK`), a
  per-agent `AgentView` that overrides a non-terminal status to `STALE(last=...)` once
  no event has been seen for `I2_BUS_STALE_TTL_S` seconds (default 6s). Terminal states
  (`done`/`failed`) are never marked stale.
- `agent_state_bus/bus_debug.py` — **the single debug command**:
  `.venv/bin/python -m agent_state_bus.bus_debug`. Prints stream length, consumer
  group/pending summary, and the current live view with staleness applied.
- `agent_state_bus/run_real_agent.py` — spawns a real `hermes -z` process, publishes
  idle→working→(heartbeats)→done/failed around it. `--simulate-crash-after N` makes the
  wrapper `os.kill(getpid(), 9)` itself N seconds after publishing `working` — a genuine,
  uncontrolled writer death, not a scripted graceful exit.

## Running the live probe

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
bin/demo.sh
```

`bin/demo.sh` is the ORACLE probe: starts an independent reader, runs a real agent to
completion (idle→working→done, observed live), runs a second real agent that is
hard-killed mid-`working` (observed going `STALE` after the TTL, never fabricating a
`done`), proves ack/replay (a non-acking reader's pending entries are redelivered to a
fresh reader under the same consumer name), then dumps the one-shell-command debug view.
Evidence (reader transcript + debug dump) is written to `evidence/`.

## Debug in one command

```bash
.venv/bin/python -m agent_state_bus.bus_debug
```

## Teardown

```bash
bin/down.sh
```
