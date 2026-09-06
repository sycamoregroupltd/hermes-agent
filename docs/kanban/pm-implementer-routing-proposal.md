# PM / implementer routing proposal

Status: source-only proposal for `jarvis-os/t_2a77f79d`; not installed in any live Hermes profile.

This document is the activation and verification contract for the candidate. It is not a claim that the live fleet has been changed or that its current gateway is healthy.

## Problem

A PM profile can own orchestration but may intentionally have only the Kanban/gateway/web coordination surface. Auto-decomposition must not send implementation children to that PM, and the dispatcher must not start a worker that cannot satisfy the child body.

## Proposed config

Add this to the dispatcher-owning profile's `kanban:` section, with the board slug as the key:

```yaml
kanban:
  decompose_default_assignee_by_board:
    jarvis-os: fleet-engineer
    sycode-trading: trading-devops
  decompose_pm_profiles:
    - jarvis-os-pm
    - sycode-trading-pm
```

The source default is an empty mapping/list, preserving existing installations until an operator explicitly configures the board routes. `decompose_default_assignee_by_board` is deliberately separate from `default_assignee`: the latter also routes unassigned ready cards, while this mapping controls only decomposed child fallback. If a configured implementer is absent, routing falls back to the existing profile-resolution behavior rather than creating an unassigned child.

## PM exit contract

A PM that lacks `terminal` and/or `file` must never block its own card with a capability excuse. It must do exactly one of:

1. reassign the card to a capable implementer and request execution;
2. decompose the work into child cards assigned to capable implementers; or
3. keep a genuinely PM-only coordination card and complete the coordination handoff without claiming implementation.

The only explicit exception in decomposition is a root-card body containing the exact line `PM_ONLY: true`. That marker permits PM assignees for coordination work; it does not grant missing terminal/file capability to an implementation request.

## Dispatcher guard

Before claiming a ready card, the dispatcher inspects the body for explicit terminal/file work and resolves the assignee's effective CLI toolsets. If required capability is absent, it does not spawn. It transitions the card through the normal typed Kanban block path with `kind: capability`, records a reroute hint naming the configured board implementer, and exposes the item in `DispatchResult.capability_blocked` / CLI JSON. Resolver failures remain fail-open to avoid turning a temporary profile-introspection problem into a false capability block; normal spawn diagnostics still handle those failures.

## Mechanism contract

### Durable store and authority

The durable candidate store is the fork repository `sycamoregroupltd/hermes-agent`, branch `fleet-engineer/t_2a77f79d`:

`https://github.com/sycamoregroupltd/hermes-agent/tree/fleet-engineer/t_2a77f79d`

The fork branch and its commit history are authoritative for this candidate. The live stores remain authoritative for operation and are intentionally not modified:

- live profile/config store: `/home/frank/.hermes/profiles/` (the proposed `kanban:` mapping would be applied by an approved operator to the dispatcher-owning profile only);
- live board/tracker: `/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db` and the registered `jarvis-os` Kanban adapter;
- live gateway/service: `hermes-gateway-jarvis.service` and its gateway-owned embedded dispatcher ticker.

No live profile, SOUL, config, cron, provider, credential, gateway, board row, or production state is part of this source-only delivery.

### Exact executed copy

The implementation copy independently tested before this contract rework is commit `29e938766fa003fcf10dd4571986ce2ee27285f2`. Its changed paths are:

- `hermes_cli/config_defaults.py`
- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban_db_dispatch.py`
- `hermes_cli/kanban_decompose.py`
- `hermes_cli/kanban_ops.py`
- `docs/kanban/pm-implementer-routing-proposal.md`
- `tests/hermes_cli/test_kanban_capability_guard.py`

The implementation commands and observed results were:

```bash
git diff --check 29e938766fa003fcf10dd4571986ce2ee27285f2^ 29e938766fa003fcf10dd4571986ce2ee27285f2
# expected: exit 0, no output

python -m compileall -q hermes_cli/config_defaults.py hermes_cli/kanban_db.py hermes_cli/kanban_db_dispatch.py hermes_cli/kanban_decompose.py hermes_cli/kanban_ops.py tests/hermes_cli/test_kanban_capability_guard.py
# expected: exit 0, no output

python -m pytest -o addopts= -q tests/hermes_cli/test_kanban_capability_guard.py tests/hermes_cli/test_kanban_decompose.py tests/hermes_cli/test_kanban_default_assignee.py tests/hermes_cli/test_kanban_worker_spawn_toolsets.py tests/hermes_cli/test_kanban_decompose_db.py tests/gateway/test_kanban_auto_decompose_live.py
# observed: 15 passed in 7.64s
```

This rework adds contract documentation only; after commit, the exact documentation rework copy is the tip of this same branch and must be recorded by `git rev-parse HEAD` in the review handoff. No source-only commit is treated as installed.

### Delivery target and named consumers

Delivery is the fork branch above, for review and possible later integration; it is not `origin/main` and is not a live editable-install deployment. The named consumers are:

- `gateway/kanban_watchers.py::GatewayKanbanWatchersMixin._kanban_dispatcher_watcher`, the embedded gateway ticker;
- `gateway/kanban_watchers_dispatcher.py::_KanbanDispatcher.auto_decompose_tick` and `tick_once_for_board`, the per-board ticker work;
- `hermes_cli/kanban_decompose.py`, the CLI and gateway auto-decomposition consumer;
- `hermes_cli/kanban_db_dispatch.py::dispatch_once`, the capability guard and spawn consumer;
- `jarvis-os-pm` and `sycode-trading-pm`, the PM operators whose implementation routing/exit behavior is governed by this proposal;
- `fleet-engineer` and `trading-devops`, the configured implementation fallbacks.

### Gateway, ticker, and service liveness proof

The source-only candidate proves wiring through isolated tests, not through a live restart. These exact commands are the required evidence for the source, gateway/ticker, and service gates:

```bash
# Source and decomposition/guard proof in the candidate checkout
python -m pytest -o addopts= -q \
  tests/hermes_cli/test_kanban_capability_guard.py \
  tests/hermes_cli/test_kanban_decompose.py \
  tests/hermes_cli/test_kanban_default_assignee.py \
  tests/hermes_cli/test_kanban_worker_spawn_toolsets.py \
  tests/hermes_cli/test_kanban_decompose_db.py \
  tests/gateway/test_kanban_auto_decompose_live.py
# expected: exit 0; all tests pass; no worker is spawned for a missing capability

# Ticker hook proof (run in the candidate checkout)
python -m pytest -o addopts= -q tests/hermes_cli/test_kanban_dispatch_tick_hook.py
# expected: exit 0; embedded dispatch tick remains callable and isolated per board

# If the file-level run exhibits the known order-dependent fixture issue, run
# each hook invariant independently:
python -m pytest -o addopts= -q tests/hermes_cli/test_kanban_dispatch_tick_hook.py::test_active_tick_fires_hook_with_outcome_ok
python -m pytest -o addopts= -q tests/hermes_cli/test_kanban_dispatch_tick_hook.py::test_tick_hook_fires_after_dispatch_lock_released
# expected for each isolated command: 1 passed

The observed file-level run in this checkout is 2 failed, 17 passed: both
failures are the two hook tests above. Each isolated command passes (1 passed
in 1.87s and 1 passed in 1.65s). The same order-dependent failure was reported
by the independent reviewer against an `origin/main` archive, so this is a
known test-harness residual, not evidence that the candidate ticker wiring is
healthy in a live service. It remains a caveat for review and is not hidden.

# Read-only service/runtime preflight, after a separately approved activation
env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_BOARD \
  -u HERMES_KANBAN_WORKSPACE -u HERMES_KANBAN_DB -u HERMES_TENANT \
  hermes --profile jarvis gateway status --deep --full
# expected: service active/running, no new startup/dispatcher exception, and the
# output identifies the expected gateway service and current runtime state

# Read-only dispatcher dry run, after activation and before any real spawn
env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_BOARD \
  -u HERMES_KANBAN_WORKSPACE -u HERMES_KANBAN_DB -u HERMES_TENANT \
  hermes --profile jarvis kanban --board jarvis-os dispatch --dry-run --json
# expected: JSON includes the candidate card's capability-block/reroute result,
# or a normal spawn decision for capable assignments; no process is spawned

# One real next-tick proof, only after os-reviewer approval and operator activation
systemctl --user status hermes-gateway-jarvis.service --no-pager -l
journalctl --user -u hermes-gateway-jarvis.service --since "-2 min" --no-pager -l
# expected: Active: active (running), stable MainPID, and no dispatcher error
# in the recent journal window; the operator must also verify the board
# event/worker record for the exercised card before declaring liveness
```

The current read-only preflight was run during this task. It reported the systemd service as active/running, but also reported an outdated installed service definition and recent Slack `Session is closed` errors. Therefore live gateway/service liveness is not claimed by this proposal; those pre-existing runtime findings remain an explicit activation prerequisite rather than being hidden as a passing result. No restart or other live mutation was performed.

If activation is rejected or the candidate is withdrawn, source rollback is `git revert --no-edit <approved-candidate-commit>` on the fork branch. Any live config rollback must restore the operator's reviewed pre-activation snapshot at `/home/frank/.hermes/profiles/jarvis/config.yaml` and then repeat the same read-only status and next-tick proof; that action is operator-gated and was not executed here.

## Verification matrix

| Acceptance criterion | Evidence | Result |
|---|---|---|
| Decomposed children default to the board implementer (`fleet-engineer` on `jarvis-os`, `trading-devops` on `sycode-trading`) unless explicitly PM-only. | `hermes_cli/kanban_decompose.py`; `test_decompose_routes_children_to_board_implementer_and_reserves_pm_only`; commit `29e938766fa003fcf10dd4571986ce2ee27285f2`. | PASS in isolated candidate tests; live config activation not performed. |
| A PM without terminal/file reassigns or decomposes and never blocks its own implementation card as a capability excuse. | PM exit contract above; PM exclusion and exact `PM_ONLY: true` behavior in `kanban_decompose.py` and the same regression test. | PARTIAL: routing guard is tested and the contract is documented; updating live PM SOUL/exit instructions is deliberately outside this source-only delivery. |
| Dispatcher refuses terminal/file-demanding work on a profile without those toolsets, emits a typed capability block, and provides a reroute hint. | `hermes_cli/kanban_db_dispatch.py`; `test_dispatcher_blocks_missing_terminal_file_capability`; `test_dispatcher_capability_guard_is_dry_run_only`; observed focused suite result `15 passed`. | PASS in isolated candidate tests; live dispatch dry-run/next-tick proof is pending reviewed activation. |
| Regression test covers the fixed behavior. | `tests/hermes_cli/test_kanban_capability_guard.py`; exact pytest command above; observed `15 passed in 7.64s`. | PASS. |

## Activation boundary and rollback

This branch contains source and tests plus this activation contract. Activation requires a separately reviewed configuration change in the live dispatcher-owning profile, followed by the read-only dry run and a real next-tick verification described above. No profile SOUL, live config, gateway, cron, provider, credential, or board state is changed by this proposal. The candidate must round-trip through `os-reviewer` before any landing or live install.
