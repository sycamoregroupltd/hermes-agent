# Safe skill-preload smoke guidance (reviewable install packet)

This packet and `install-hermes-safe-skill-guidance.py` are the reviewable
guidance update for the installed `gap-plugging` and
`sector-development-codebase-loop` skills. They are intentionally staged in
the Hermes worktree; do not install them into live profile skills until the
`os-reviewer` approval for `t_08ceae63` is recorded.

## Canonical command

Use the wrapper from a clean caller:

```bash
/home/frank/.hermes/hermes-agent/scripts/hermes-safe-skill-smoke.sh <skill-name>
```

For a profile-specific smoke, set the target profile's `HERMES_HOME` in the
invoking shell (not inside a running worker), for example:

```bash
HERMES_HOME=/home/frank/.hermes/profiles/<profile> \
  /home/frank/.hermes/hermes-agent/scripts/hermes-safe-skill-smoke.sh <skill-name>
```

The wrapper accepts exactly one skill name, validates it, refuses any inherited
`HERMES_KANBAN_*`, `HERMES_SESSION_*`, supervisor markers,
`HERMES_SESSION_SOURCE`, `HERMES_TENANT`, or delegated-child context,
explicitly removes those variables for the child, and invokes Hermes with
`--toolsets ""` plus a no-tools prompt. It must be used instead of any inline
nested Hermes invocation from a Kanban worker.

## Reviewed installation (after approval only)

The installer is the only approved mutation path for these two consumer skills.
It requires exact source anchors, writes each file atomically, makes a sibling
timestamped backup, and refuses partial installation on drift:

```bash
python3 /home/frank/.hermes/hermes-agent/scripts/install-hermes-safe-skill-guidance.py \
  --skills-root /home/frank/.hermes/skills
```

Use `--check` for a read-only post-install assertion. Do not edit either live
`SKILL.md` by hand or paste a nested Hermes command into a review.

## Gap-plugging verification rule

For `cron-job`, `script-patch`, `alert-path`, or `invariant-check` evidence,
run the wrapper only from a non-worker shell. A worker-context attempt must
fail closed with exit 78 before Hermes launches. The positive proof must report
`HERMES_SAFE_SKILL_SMOKE_PASS` and `Messages: ... (1 user, 0 tool calls)` (or an
equivalent machine-readable zero-tool receipt). Never target the current live
card, board DB, or worker workspace as a smoke fixture; if isolation cannot be
proven, report `NOT_PLUGGED` and stop.

## SECTOR loop verification rule

A SECTOR/controller skill-load check is a read-only preload check, not a loop
run. Use the same wrapper with the exact skill name, capture the command output,
and separately verify the controller/ledger through its registered board and
runtime owners. Do not use `-z`, a nested `--profile`, or a manually
constructed dispatcher-shaped command from inside a worker. The wrapper fails
closed with exit 78 if any Kanban, session, delegated-child, or supervisor
context is inherited.

Required evidence packet:

- wrapper path and immutable branch/commit;
- negative exit-78 receipt showing inherited worker variables were rejected;
- positive named-skill preload receipt with zero tool calls;
- controller/ledger read-back from the owning runtime/board (read-only);
- named consumer: `jarvis-os-pm` and the independent reviewer.

The wrapper is a safety boundary, not proof that a skill is installed in every
profile. Run a separate target-profile read-back when that claim is required.
