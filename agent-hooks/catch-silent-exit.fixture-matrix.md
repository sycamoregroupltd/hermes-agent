# catch-silent-exit no-signal regression fixture matrix (v2 — production-faithful wire shape)

Task: t_47396a5a

Purpose: deterministic RED coverage for the protocol-violation zombie class where a completion-gate/review-router-generated worker exits `rc=0` without calling `kanban_complete` or `kanban_block`.

Primary fixtures: `agent-hooks/catch-silent-exit.fixtures.json`
Reference harness/self-test: `agent-hooks/catch-silent-exit-harness.py` and `agent-hooks/catch-silent-exit.selftest.sh`

Payload wire shape: follows `agent/shell_hooks.py._serialize_payload()` exactly:
- Top-level: `hook_event_name`, `tool_name`, `tool_input`, `session_id`, `cwd`
- Child-specific fields under `extra`: `extra.child_status`, `extra.child_role`, `extra.child_summary`, `extra.parent_session_id`, `extra.parent_turn_id`, `extra.child_session_id`, `extra.duration_ms`

The harness feeds JSON payloads directly to `catch-silent-exit.sh` as a `subagent_stop` hook payload. It does not read or mutate live kanban boards, provider config, credentials, or provider routing.

| Fixture | Input payload | Expected terminal signal | Current behavior before fix | Intended failure reason |
|---|---|---|---|---|
| `rc0-completed-without-kanban-terminal-signal-fails-closed` | `extra.child_status=completed`, `extra.child_summary` says `worker exited cleanly (rc=0) without calling kanban_complete or kanban_block` | deterministic useful terminal signal: block/alert reason containing `missing terminal kanban signal` | `catch-silent-exit.sh` treats `completed` as clean and emits `{}` | The worker can become a protocol-violation zombie because the post-stop hook cannot distinguish clean completion from rc=0/no-kanban-signal completion. The hook reads the real payload path (`extra.child_status`) but still treats `completed` as benign. |

Runnable command (expected to fail before the fix):

```bash
/home/frank/.hermes/agent-hooks/catch-silent-exit.selftest.sh
```

Expected RED output shape before the fix:

```text
catch-silent-exit regression harness FAIL
FAIL rc0-completed-without-kanban-terminal-signal-fails-closed: decision: expected 'block', got 'allow'; stdout='{}'; extra.child_status='completed'; ... no useful terminal signal ...
```

v2 changes (addressing os-reviewer CHANGES_REQUESTED):
- Fixture payload changed from top-level `child_status`, `child_role` to `hook_event_name` + `extra.*` shape matching `agent/shell_hooks.py._serialize_payload()`
- `catch-silent-exit.sh` reads `extra.child_status` instead of top-level `child_status`, matching the real runtime wire path
- Harness error messages now report `extra.child_status` instead of `payload_child_status`

Acceptance boundary:
- This task intentionally adds the failing regression harness only.
- No network/provider calls are required.
- No live kanban board or credential/provider state is mutated by the harness.
- A later implementation task should make the same command pass by emitting a deterministic useful terminal signal for rc=0 exits without `kanban_complete`/`kanban_block` evidence.
