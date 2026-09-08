# Profile-aware provider liveness candidate (`t_6ab45bb1`)

This offline checker classifies provider liveness per explicitly active profile.
It reads one deterministic JSON fixture and emits a deterministic JSON report;
it does not read `HERMES_HOME`, credentials, processes, or the network.

Run the executable fixture evidence from the repository root:

```bash
python -m scripts.provider_liveness tests/fixtures/provider_liveness/primary_success.json
python -m scripts.provider_liveness tests/fixtures/provider_liveness/fallback.json
python -m scripts.provider_liveness tests/fixtures/provider_liveness/unknown_collection.json
python -m scripts.provider_liveness tests/fixtures/provider_liveness/configured_dead.json
python -m scripts.provider_liveness tests/fixtures/provider_liveness/silent_downgrade.json
python -m scripts.provider_liveness tests/fixtures/provider_liveness/no_active_profiles.json
```

Exit status is `0` only for `status: green`, `1` for a valid non-green report,
and `2` for an invalid fixture. `active_profiles` is the activity source of
truth. Each profile's expected primary comes only from its explicit
`config.model.provider`; configured fallback routes come from
`config.fallback_providers`.

An empty `active_profiles` array is valid evidence, but fails closed with a
`no_active_profiles` alarm and exit status `1`.

Only a successful `authenticated_inference` observation proves route
authentication, and success must include `served_provider`. When it matches the
requested `provider`, the configured route is proven. A missing
`served_provider` fails closed as `unknown_collection`; a different value is
`silent_downgrade` and raises `route_served_by_other_provider`. Failure and
unknown results remain valid without `served_provider`. `http_model_listing`
and `credential_presence` observations are reported as non-auth evidence and
cannot make a route green. A failed authenticated inference on any active
configured route alarms; missing or unknown authenticated inference evidence is
`unknown_collection` and is also non-green. Providers belonging only to
inactive profiles, or unconfigured providers observed for an active profile,
are classified `inactive` and do not alarm.

## Source and rollback notes

Fixtures must be exported snapshots: profile activity from the orchestrator or
worker inventory, provider routes from each profile's own config snapshot, and
probe results from an authenticated inference collector. Record provenance
outside secrets; never put tokens or credential values in a fixture. The
checker intentionally performs no collection and makes no claim about a live
installation. The residual D7 limitation is that this repository provides no
fixture producer; provenance and collection remain external responsibilities.

Rollback is additive and local: revert the candidate commit (or remove
`scripts/provider_liveness/`, its test module, and its six fixture files). No
config migration, state cleanup, credential change, or service action is
required.
