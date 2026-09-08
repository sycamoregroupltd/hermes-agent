# Lease-aware session-bus delivery resolver (`t_81bba503`)

This offline resolver answers one narrow question from an exported snapshot:
which session-bus recipients completed a fresh, process-bound canary exchange?
It reports the `sent`, `received`, and `owned` phases independently and only
resolves a recipient when an exactly correlated ACK arrives within the fresh
request's deadline.

Run the executable fixtures from the repository root:

```bash
python -m scripts.session_bus_delivery tests/fixtures/session_bus_delivery/valid_fresh_process_bound.json
python -m scripts.session_bus_delivery tests/fixtures/session_bus_delivery/stale_recipient.json
python -m scripts.session_bus_delivery tests/fixtures/session_bus_delivery/historical_ack_backlog.json
python -m scripts.session_bus_delivery tests/fixtures/session_bus_delivery/received_not_owned.json
```

Exit status is `0` when at least one recipient resolves, `1` for a valid but
unavailable snapshot, and `2` for malformed or unknown input.

The fixture's process identity is `(pid, started_at)`, matching Hermes's
durable owner-stamp convention. A current lease must name the recipient and
that exact process instance. Recipient/process observations and the canary
request have separate freshness windows. An ACK counts only when its request,
correlated message, route, lease, process, chronology, and deadline all match.
A correlated ACK with a wrong lease or process is reported as `ack_unbound`; a
same-route ACK with wrong request/message correlation is `ack_uncorrelated`.
Only a correctly bound ACK outside the deadline is `ack_deadline_missed`.
Unrelated ACKs remain visible as `ignored_ack_count` but never contribute to
availability.

The command only reads the named JSON file. It does not inspect PIDs, read
Hermes configuration or databases, contact the session bus, start a daemon,
or invoke a gateway/provider. Fixtures must therefore be exported snapshots;
never include credentials or message content.

Rollback is additive: revert the task commit, or remove
`scripts/session_bus_delivery/`, `tests/scripts/test_session_bus_delivery.py`,
and `tests/fixtures/session_bus_delivery/`. No state, config, service, or data
migration needs reversal.
