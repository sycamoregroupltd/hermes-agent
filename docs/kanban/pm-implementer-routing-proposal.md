# PM / implementer routing proposal

Status: source-only proposal for `jarvis-os/t_2a77f79d`; not installed in any live Hermes profile.

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

## Rollback / activation

This branch contains only source and tests. Activation requires a separately reviewed configuration change in the live dispatcher-owning profile, followed by a dry-run and a real next-tick verification. No profile SOUL, live config, gateway, cron, provider, credential, or board state is changed by this proposal.
