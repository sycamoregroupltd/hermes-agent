# Kanban provider policy — dispatch hard stop

An operator control that stops the kanban dispatcher from ever creating a
worker process whose effective inference provider is one the operator has
declared off-limits. It is a **hard stop before the spawn**, not a filter
after the fact: a refused card produces no subprocess, no inference
connection, and no tokens.

Off by default. With no policy configured, dispatch behaves exactly as it
did before this feature existed.

## Activation

Two surfaces. Both take provider names, normalized the same way
`--provider` normalizes them (case-insensitive, aliases resolved).

**Config (persistent)** — `config.yaml`:

```yaml
kanban:
  blocked_providers: [nous]        # or: "nous, some-other-provider"
```

**Environment (per-process override)**:

```bash
export HERMES_KANBAN_BLOCKED_PROVIDERS=nous
```

Both accept a list or a comma/whitespace-separated string.

Config is re-read on **every dispatcher tick**, so enabling or lifting the
policy takes effect on the next tick — no gateway restart, no redeploy.
(Same live-read contract as `kanban.auto_decompose`, #49638.)

## Deactivation

| To do this | Do |
| --- | --- |
| Turn the policy off permanently | Set `kanban.blocked_providers: []` (or remove the key) |
| Turn it off for one process | `HERMES_KANBAN_BLOCKED_PROVIDERS= hermes kanban dispatch` |
| Turn it on for one process | `HERMES_KANBAN_BLOCKED_PROVIDERS=nous hermes kanban dispatch` |

## Precedence

1. `HERMES_KANBAN_BLOCKED_PROVIDERS` **when present in the environment** —
   wins outright. Present-but-empty means *policy disabled*; it does not
   fall through to config.
2. `kanban.blocked_providers` from `config.yaml`.
3. Empty — no policy.

A malformed policy value (e.g. a number, or a list of non-strings) is
logged once at WARNING and treated as **no policy**. Turning an operator
typo into a fleet-wide block would be a worse failure than an inert guard;
the warning is how you find out.

## What "effective provider" means

The worker is a `hermes -p <assignee> ... chat -q` subprocess that resolves
its own provider at startup. The gate mirrors that resolution
(`cli.py`, `requested_provider`), in precedence order:

1. `--provider <name>` on argv. The dispatcher emits this **only** when the
   card carries both `model_override` and `provider_override` — a bare
   provider override never reaches argv, so it never counts as one here.
2. The assignee profile's `config.yaml` → `model.provider`.
3. `$HERMES_INFERENCE_PROVIDER`, inherited from the dispatcher.
4. `auto` — resolved inside the child from stored credentials /
   `auth.json` `active_provider`.

So a card that explicitly overrides **away** from a blocked provider
(`model_override` + `provider_override: openai` on a profile whose default
is blocked) dispatches normally: the `--provider openai` on argv replaces
the profile default rather than being weighed against it. A card that
overrides **to** a blocked provider is refused regardless of how benign its
profile looks.

A `provider:model` prefix on a model override (`nous:some-model`) is
treated as an additional candidate, not a replacement. The dispatcher's
`-m` argv does not currently re-split that prefix, so the gate is
deliberately conservative about it.

## Fail-closed rule

If the effective provider cannot be pinned to a concrete allowed name, the
spawn is refused with reason `provider_unresolved`. That covers:

- the assignee profile has no `model.provider` (missing, empty, or the file
  is missing/unparseable), and no `$HERMES_INFERENCE_PROVIDER` is set;
- the resolved provider is literally `auto`.

Both leave the child to pick a provider from stored credentials, which
could be the blocked one. Refusing is the only answer that keeps the
guarantee.

This does **not** globally block unrelated providers on ordinary config
errors: a profile that plainly declares `model.provider: openai` resolves
cleanly and dispatches, whatever else is broken in its config.

## What a refusal does — and does not — do

Refusal happens **before `claim_task`**, so:

- no worker process, no inference connection, no tokens;
- no claim, no run, no `consecutive_failures` increment — a policy denial
  can never trip the spawn-failure circuit breaker or consume a retry;
- the card keeps its status (`ready` stays `ready`, `review` stays
  `review`). Lift the policy and it dispatches on the next tick with no
  operator intervention.

A second, independent check runs inside `_default_spawn` immediately before
`subprocess.Popen`. It raises `ProviderPolicyBlocked`, which the dispatch
loops absorb by releasing the claim without counting a failure. It exists
for the narrow window where the policy changes mid-tick, and for callers
that reach the spawn helper directly.

## Observability

Each refusal is recorded on the card as a `provider_policy_denied` event:

```json
{
  "reason": "provider_blocked",
  "provider": "nous",
  "source": "profile_config",
  "policy": ["nous"],
  "detail": "effective provider 'nous' (from profile_config) is blocked by operator policy"
}
```

`reason` is one of `provider_blocked` or `provider_unresolved`. `source` is
one of `task_override`, `model_override_prefix`, `profile_config`,
`env_inference_provider`, `unresolved`.

The event is written **at most once per unchanged decision**: the
dispatcher re-examines a refused card every tick, and re-appending an
identical event every 60 seconds would bury the card's real history. It is
re-stated after any intervening activity on the card.

Also surfaced as:

- `DispatchResult.skipped_provider_blocked` —
  `(task_id, provider, reason)` triples;
- `hermes kanban dispatch` output and its `--json` form;
- the dashboard `POST /dispatch` response;
- a dispatcher WARNING log line per refusal;
- the gateway's "dispatcher stuck" health warning, which names the policy
  instead of sending you to check PATH and credentials.

## Scope

The gate covers both dispatch lanes — the ready column and the review
column — across every dispatch entry point, because all of them funnel
through `kanban_db.dispatch_once`:

| Entry point | Path |
| --- | --- |
| `hermes kanban dispatch` | `hermes_cli/kanban.py::_cmd_dispatch` |
| `hermes kanban daemon --force` | `kanban_db.run_daemon` |
| Gateway embedded dispatcher | `gateway/kanban_watchers.py` |
| Dashboard "Dispatch" button | `plugins/kanban/dashboard/plugin_api.py::dispatch` |

It does not govern anything other than kanban worker spawn: interactive
sessions, auxiliary model calls, and any other Hermes surface are
unaffected. It stores no credentials and no balance.
