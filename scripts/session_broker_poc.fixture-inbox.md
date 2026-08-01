# Inbox — claude-poc0001 (FIXTURE, not a live session)

Local fixture for `scripts/session_broker_poc.py`. This file mimics the real
Session Bus inbox format described in
`/home/frank/obsidian-fleet-vault/Orchestration/sessions/SESSION-BUS.md` (v1.2).

`claude-poc0001` is a deliberately fake session id so this fixture can never be
confused with a live seat. See `session_broker_poc.fixtures.md` for the capsule schema.

### 2026-07-30T12:00:00Z · id:jarvis-20260730T120000Z-1 · from:jarvis · to:claude-poc0001 · re:broker-poc · ack:requested
Ordinary human-readable coordination message with no capsule payload. The broker
must ignore this block entirely and leave it for a human/peer to read.
---

### 2026-07-30T12:01:00Z · id:jarvis-20260730T120100Z-2 · from:jarvis · to:claude-poc0001 · re:broker-poc · ack:requested
Valid capsule — the happy path. Exactly one ACK and one terminal DONE expected.

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
---

### 2026-07-30T12:02:00Z · id:jarvis-20260730T120200Z-3 · from:jarvis · to:claude-poc0001 · re:broker-poc · ack:no
Duplicate delivery of cap-0001. Must be ignored: no second ACK, no second lease.

```json
{
  "capsule_version": 1,
  "capsule_id": "cap-0001",
  "session_id": "claude-poc0001",
  "action": "resume",
  "provider": "claude-code",
  "task_ref": "t_deadbeef",
  "issued_at": "2026-07-30T12:02:00Z"
}
```
---

### 2026-07-30T12:03:00Z · id:jarvis-20260730T120300Z-4 · from:jarvis · to:claude-poc0001 · re:broker-poc · ack:requested
Capsule naming a session this broker does not manage. Must be REJECTED as
unknown_session and never dispatched.

```json
{
  "capsule_version": 1,
  "capsule_id": "cap-0002",
  "session_id": "grok-someone-else",
  "action": "resume",
  "provider": "grok",
  "task_ref": "t_cafe0001",
  "issued_at": "2026-07-30T12:03:00Z"
}
```
---

### 2026-07-30T12:04:00Z · id:jarvis-20260730T120400Z-5 · from:jarvis · to:claude-poc0001 · re:broker-poc · ack:requested
Capsule requesting an action outside the closed allow-list. Must be REJECTED as
forbidden_action and never dispatched.

```json
{
  "capsule_version": 1,
  "capsule_id": "cap-0003",
  "session_id": "claude-poc0001",
  "action": "deploy",
  "provider": "claude-code",
  "task_ref": "t_cafe0002",
  "issued_at": "2026-07-30T12:04:00Z"
}
```
---
