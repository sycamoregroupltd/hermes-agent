# Kanban provider policy — dispatch hard stop

An operator control that stops Kanban from beginning an inference path whose
effective provider is one the operator has declared off-limits. Worker
dispatch is stopped **before process creation**. Triage decomposition and
specification are stopped **before the auxiliary client call** and before any
child fan-out or task promotion. A refusal therefore produces no subprocess,
no auxiliary request, no inference connection, and no tokens.

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

A malformed **declared** policy value (for example a number or a list of
non-strings) fails closed. The same is true if the policy module cannot be
imported, config cannot be parsed, policy evaluation raises, or a non-empty
declaration unexpectedly resolves to an empty set. The event reason is
`policy_evaluation_error`; no worker process or Kanban auxiliary model call is
started until the declaration is repaired or explicitly disabled.

Declaration detection is deliberately independent of the policy module:
`kanban_db` checks the environment override and the raw `config.yaml` key
before importing policy code. This prevents a broken import from silently
turning off an already-declared hard stop. A config file that is unreadable
while the dispatcher cannot prove the policy absent is also refused.

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

For `kanban_decomposer` and `triage_specifier`, the evaluator inspects the
provider-bearing parts of the same routing inputs used by the auxiliary
client:

1. `auxiliary.<task>.provider` when it names a concrete provider;
2. `model.provider` when the auxiliary slot is unset or `auto`;
3. `auxiliary.<task>.fallback_chain`;
4. top-level main fallback providers; and
5. the built-in auto-discovery route, which can select Nous.

An explicit auxiliary provider can fall back to the main model on capacity
errors, so the main provider is included as a possible route even when the
auxiliary slot is concrete. Any blocked or unresolved possible route refuses
the call. A fully concrete external/non-blocked auxiliary route with a
non-blocked main/fallback configuration remains unaffected.

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

Auxiliary refusals happen in
`kanban_decompose.decompose_task` and `kanban_specify.specify_task` before
`agent.auxiliary_client.call_llm` is imported or called. They therefore enter
none of the auxiliary retry, credential-refresh, payment-fallback, or
provider-fallback paths. They create no children and do not promote the triage
task. The task remains in `triage`; no run or failure counter is consumed.

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

`reason` is one of `provider_blocked`, `provider_unresolved`, or
`policy_evaluation_error`. Worker `source` values include `task_override`,
`model_override_prefix`, `profile_config`, `env_inference_provider`, and
`unresolved`. Auxiliary sources identify the task config, main config,
configured fallback, or auto-discovery route. Declaration failures use
`policy_declaration`.

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

The worker gate covers both dispatch lanes — the ready column and the review
column — across every dispatch entry point, because all of them funnel through
`kanban_db.dispatch_once`:

| Entry point | Path |
| --- | --- |
| `hermes kanban dispatch` | `hermes_cli/kanban.py::_cmd_dispatch` |
| `hermes kanban daemon --force` | `kanban_db.run_daemon` |
| Gateway embedded dispatcher | `gateway/kanban_watchers.py` |
| Dashboard "Dispatch" button | `plugins/kanban/dashboard/plugin_api.py::dispatch` |

The auxiliary gates cover the complete Kanban triage surface because every
entry point funnels through one of two functions:

| Entry point | Choke point |
| --- | --- |
| Gateway automatic decomposition tick | `kanban_decompose.decompose_task` |
| `hermes kanban decompose` | `kanban_decompose.decompose_task` |
| Dashboard task "Decompose" action | `kanban_decompose.decompose_task` |
| `hermes kanban specify` | `kanban_specify.specify_task` |
| Dashboard task "Specify" action | `kanban_specify.specify_task` |

The policy does not govern ordinary interactive sessions or unrelated
auxiliary tasks. It stores no credentials and no balance. Enabling the source
change is a separate live-configuration decision; building or testing it does
not modify any running gateway, profile, provider, service, or board policy.
