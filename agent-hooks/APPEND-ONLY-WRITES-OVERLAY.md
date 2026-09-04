# Overlay wiring recipe (t_42f29466)

The hook source lives in hermes-agent `agent-hooks/gate-append-only-writes.py`.
This file is the fleet overlay install recipe. Do **not** commit live
`~/.hermes/config.yaml` (runtime-local; can contain secrets).

## Install (isolated copy, never mutate a dirty shared checkout)

1. Copy `gate-append-only-writes.py`, `gate-append-only-writes.sh`, and
   `gate-append-only-writes.selftest.sh` into `~/.hermes/agent-hooks/`.
2. `chmod +x` the two `.sh` files.
3. Insert this `pre_tool_call` row **before** `gate-second-brain-writes`:

```yaml
  pre_tool_call:
    - command: ~/.hermes/agent-hooks/gate-append-only-writes.sh
      matcher: ^(write_file|patch|terminal)$
      timeout: 15
    - command: ~/.hermes/agent-hooks/gate-second-brain-writes.sh
      matcher: ^(write_file|patch|terminal)$
      timeout: 20
```

4. Register in overlay `run-selftests.sh`:

```bash
run_test "gate-append-only-writes" "bash agent-hooks/gate-append-only-writes.selftest.sh"
```

5. Gateway reload is **orchestrator-gated**. Do not self-restart gateway, MCP,
   sycodetrading-server, or collectors.
6. Operator-gated history recovery bypass: `ALLOW_APPEND_ONLY_REWRITE=1`.

Arena `journal.md` and `trading-arena/IMPROVEMENTS.md` stay patch-append.
Do not use `write_file` full replace on those paths.
