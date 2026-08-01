# Fixture — session broker POC

Local, offline fixture for `scripts/session_broker_poc.py`. Nothing here refers
to a live session, board, or card.

## Files

| File | Purpose |
|---|---|
| `session_broker_poc.fixture-inbox.md` | A Session Bus inbox containing five blocks: one non-capsule human message, a valid capsule, a duplicate of it, an unknown-session capsule, and a forbidden-action capsule. |

> **Why these live flat in `scripts/` rather than a `fixtures/` subdirectory:**
> `.gitignore` applies a deliberate default-deny to `/scripts/*` (task
> `t_376ecb33`) and re-includes only flat `*.py`, `*.sh` and `*.md`. Git cannot
> un-ignore a file inside an ignored directory, so a `scripts/fixtures/` subtree
> would be silently untrackable. The flat naming conforms to that security gate
> instead of amending it.

## Why the session id is fake

`claude-poc0001` is not a real seat and is not registered in
`Orchestration/sessions/SESSION-BUS.md`. The broker refuses any capsule whose
`to:` header or `session_id` differs from the single `--managed-session` it was
started with, so pointing this fixture at a real session id would simply produce
`unknown_session` rejections rather than acting on a live seat.

## Capsule schema (v1)

A capsule is an ordinary Session Bus message whose body contains one fenced
`json` block. Blocks without such a payload are ignored, so this format is
backward-compatible with the existing human conversation layer.

```json
{
  "capsule_version": 1,
  "capsule_id": "cap-0001",
  "session_id": "claude-poc0001",
  "action": "resume",
  "provider": "claude-code",
  "task_ref": "t_deadbeef",
  "issued_at": "2026-07-30T12:01:00Z"
}
```

| Field | Rule |
|---|---|
| `capsule_version` | must equal `1` |
| `capsule_id` | non-empty string; the idempotency key |
| `session_id` | must equal the broker's `--managed-session` **and** the block's `to:` |
| `action` | must be in `ACTION_ALLOWLIST` (currently `{"resume"}`) |
| `provider` | must be in `PROVIDER_ALLOWLIST` (`{"claude-code", "grok"}`) |
| `task_ref` | non-empty string |
| `issued_at` | advisory only; not used for control flow in the POC |

## Run it (inert — writes nothing outside `--state-dir`)

```bash
python3 scripts/session_broker_poc.py \
  --managed-session claude-poc0001 \
  --inbox scripts/session_broker_poc.fixture-inbox.md \
  --state-dir /tmp/session-broker-poc
```

Expected: `cap-0001` accepted (one ACK, one DONE, `executed: false`), the
duplicate ignored, `cap-0002` rejected `unknown_session`, `cap-0003` rejected
`forbidden_action`. The first block produces no outcome at all.

Re-running against the same `--state-dir` yields `duplicate` for `cap-0001`,
because the ledger persists.

Without `--emit-to-live-bus` the broker uses the inert recording route and does
not touch Hermes or Obsidian. The state directory is the only thing written.
