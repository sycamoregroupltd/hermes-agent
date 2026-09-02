# Cloud overflow planner (prepare-only)

Status: **READY, not activated**. This document covers `hermes_cli/cloud_overflow.py`
and the `hermes kanban cloud-overflow` CLI. It plans — it never spawns, launches,
or spends. Activation (a real launcher that calls provider CLIs) is a separate,
future, Frank-approved change; nothing in this seam does it today.

## What it does

When a jarvis-os board is saturated at `max_spawn` (live running-task count >=
the configured cap — currently 3), the planner looks for exactly one eligible
`READY` card and produces a structured plan: which provider it would use, why
the card is eligible, and what gate blocks the next step. It writes a lease
row to an isolated SQLite state store so a repeated tick is idempotent. It
never touches the live kanban board, never shells out to a provider CLI, and
never mutates a task's assignee/status.

## Isolation HOLD carried forward

Per `~/.hermes/deploy-state/ops-notes/2026-09-02-cloud-overflow-scale-beyond-dgx.md`
and jarvis-os card `t_a1ded1b2`:

- No merge-to-main, no Deribit/live trading, no live A3 work, no `hermes update`,
  no new specialist gateways, no secret dumps.
- Provider order is fixed: Cursor Cloud → Claude Cloud → Codex Cloud
  (`PROVIDER_ORDER` in `cloud_overflow.py`).
- Codex Cloud is fail-closed: it requires BOTH a shape-validated `ENV_ID`
  (`^[A-Za-z0-9_-]{3,128}$`, rejects whitespace/shell metacharacters) AND an
  exact-match Frank approval record (`approved_by == "frank"`, an
  `approved_at` timestamp, and the SAME `task_id` and `env_id` as this launch
  — a record for a different task or env never satisfies this one). Missing
  or malformed either yields `CodexCloudAdapter.refusal_reason()` — a typed
  string like `codex_missing_env_id` / `codex_malformed_env_id` /
  `codex_missing_exact_approval` — and zero API/pay-per-token call.

## Eligibility (fail-closed)

A `READY` card is only a candidate when ALL of the following hold — the
absence of any signal is a refusal, never an implicit pass:

1. `status == "ready"`, no active `claim_lock`, all parents satisfied.
2. An explicit work class of `docs`/`documentation`/`research` via skill,
   label, or `metadata.work_class`/`metadata.type` — title prose is never
   classified.
3. An explicit **isolation-safe marker**: `"isolation-safe"` present in the
   card's skills/labels/`metadata.labels`/`metadata.classes`, OR in
   `metadata.acceptance_contract`. A docs/research card with no isolation-safe
   marker still refuses (`missing_isolation_safe_acceptance_contract`) — this
   is the acceptance-contract requirement, independent of work class.
4. No excluded class present (`EXCLUDED_CLASSES`): money/payments/
   live-trading/trading, credentials/secrets, production-deploy/deploy,
   irreversible-data, auth, tenant-isolation, provider-routing,
   guardrail-mutation, shared-writable-directory, or `a3` (A3-gated work is
   Frank-only per the fleet kernel and can never be an overflow candidate).

## Structured output contract

Every `run_tick()` call returns a `TickResult` with:

- `status` / `action` / `reason` — coarse machine-readable outcome.
- `board`, `task_id`, `provider`, `idempotency_key` — the selected candidate
  (or `None` when nothing was eligible).
- `trigger_evidence` — the board saturation snapshot (`running`/`max_spawn`/
  `saturated` per board) plus the candidate id and its eligibility reason.
- `isolation_verdict` — `eligible_isolation_safe`, `eligible` (mid-pipeline),
  or `not_evaluated` (nothing reached eligibility, e.g. paused/no saturation).
- `approval_verdict` — one entry per configured provider in `PROVIDER_ORDER`,
  always present even when a provider was never configured
  (`not_configured`), so an operator sees the whole queue state in one call.
- `next_action` — a human-readable statement of what happens next (always
  `HUMAN_GATE — Frank approval required...` on a successful plan; `none —
  <reason>` otherwise). There is no path where the planner claims it will act
  autonomously.

## Running it (dry-run only)

```bash
hermes kanban cloud-overflow \
  --fixture tests/fixtures/cloud_overflow.json \
  --state /tmp/cloud-overflow-state.sqlite3 \
  --dry-run --json
```

- `--fixture` is REQUIRED. There is no live-board mode; `cloud_overflow_command`
  only ever reads a JSON fixture (`load_fixture`), never `kanban_db.py` calls
  targeting a real board's `HERMES_KANBAN_DB`.
- `--state` is an isolated SQLite path (WAL, single table) tracking leases —
  never the kanban DB.
- Running the exact same command twice returns `reason: duplicate_lease` on
  the second call (idempotent by `(board, task_id, source_revision, provider)`).
- `--pause` / `--kill-switch` short-circuit before any board read and return
  `status: blocked`.

`snapshot_registered_boards()` exists as the (currently unused by the CLI)
seam that reads real boards via `kanban_db.list_boards`/`list_tasks`/
`count_running_tasks` for a future scheduled tick — it is read-only (no writes
to the kanban DB) but is not wired into any cron or dispatcher path yet.

## Extending toward activation (future, gated)

`record_launch()` is the only function that can call a provider's `.launch()`
(via `ProviderAdapter.launch`). It:

- Requires an explicit `approved=True` argument — the prepare CLI never
  passes this.
- Writes a sanitized receipt (`sanitize_receipt`) with an allowlisted field
  set — no prompt text, no secrets — as a kanban comment via
  `kanban_comment_writer` (or an injected comment writer).
- Marks the lease `unresolved` (not `launched`) if the receipt comment write
  fails, so a broken audit trail is never silently treated as done.

Turning this into an active loop requires, at minimum:

1. A real launcher CLI/cron entry point that calls `record_launch` with
   `approved=True` only after loading a verified Frank approval record.
2. Flipping `config/cloud-overflow-loop-registry.yaml`'s `status` from
   `paused` to `active` and installing the cron/kanban trigger it describes,
   per `fleet-loop-registry`.
3. A fresh `os-reviewer` review of the launcher path specifically (this
   prepare-only seam's review does not cover it).
4. No dynamic minting of kanban cards — `run_tick` never calls `kanban_create`
   or any create path, and that constraint must survive the future launcher.

## Rollback

This is a new module (`hermes_cli/cloud_overflow.py`), a new CLI subcommand
(`hermes kanban cloud-overflow`), and a new test/fixture pair. To roll back:

```bash
git revert <this-PR's-merge-commit>
```

There is no schema migration, no cron installed, and no config flag flipped
live — `config/cloud-overflow-loop-registry.yaml` ships with `status: paused`
and no `job_id`, so reverting the commit is a complete, self-contained
rollback with no follow-up cleanup step.

## Observability

- `pytest tests/hermes_cli/test_cloud_overflow.py -q` is the loop's `oracle`
  per the registry row — run it before trusting any behavior change.
- The state store (`OverflowState`) is inspectable directly:
  `sqlite3 <state-path> 'SELECT * FROM overflow_state'`.
- No logs beyond the CLI's own stdout/stderr; nothing here writes to
  `~/.hermes` logs, cron logs, or the kanban event stream.
