# t_08ceae63 smoke-hazard evidence

Date: 2026-09-02
Task: jarvis-os/t_08ceae63

## Root cause

`model_tools._compute_tool_definitions()` appends the `kanban` toolset whenever
`HERMES_KANBAN_TASK` is present and `_is_dispatcher_owned_worker()` is true,
even when the caller explicitly passes an empty `enabled_toolsets` list. A
fresh nested Hermes process has no in-process ContextVar marker, so
`is_dispatcher_owned_worker_context()` returns true while inherited worker
variables remain in its environment. Therefore `--toolsets ""` alone is not a
sufficient safety boundary in a nested process.

Relevant source: `model_tools.py:427-440` and
`agent/delegation_context.py:97-106` (on the base used for this worktree).

## Reproduction and fix evidence

The earlier disposable reproduction was run with fixture-only identity values
and did not open the production board database:

`/tmp/repro_nested_smoke_t_08ceae63.py` -> exit 0.

A direct invocation from this active worker now fails closed before Hermes is
launched:

`./scripts/hermes-safe-skill-smoke.sh gap-plugging` -> exit 78

Observed refusal names the inherited worker variables, including
`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_CLAIM_LOCK`,
`HERMES_KANBAN_DB`, `HERMES_KANBAN_BOARD`, and
`HERMES_KANBAN_WORKSPACE`.

With the complete worker environment removed and the live Hermes executable,
the canonical wrapper returned `HERMES_SAFE_SKILL_SMOKE_PASS`; Hermes reported
`Messages: 2 (1 user, 0 tool calls)` and exit 0. The prompt was fixed and
explicitly says not to use tools.

## Automated verification

- `bash -n scripts/hermes-safe-skill-smoke.sh` -> pass
- `python3 -m pytest scripts/tests/test_hermes_safe_skill_smoke.py -q -o addopts=` -> 3 passed
- `python3 -m pytest tests/cron/test_cron_kanban_env_isolation.py tests/hermes_cli/test_kanban_worker_spawn_toolsets.py -q -o addopts=` -> 21 passed

No gateway restart, deploy, credential, cron, or live-card fixture mutation was
performed. The guidance update remains staged in
`scripts/hermes-safe-skill-smoke-guidance.md` pending `os-reviewer` approval.
