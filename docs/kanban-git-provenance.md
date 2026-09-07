# Git identity and execution-vendor provenance

## Repository identity

Every Hermes-created Git checkout gets a repo-local identity before a commit:

```text
git config user.name <profile-or-seat>
git config user.email <profile-or-seat>@fleet.local
```

Kanban worktree provisioning applies this automatically. Linked worktrees use
Git's `config.worktree` scope so one sibling cannot overwrite another sibling's
identity. A non-Git scratch workspace must apply the same commands after it
creates or clones a repository. The global Git identity is never changed by
Hermes.

The CI `git-author-provenance` check rejects the known leaked global identity
`Claude <claude@anthropic.com>` in pull-request commits. This is a hygiene
tripwire, not a vendor classifier.

## Review contract

Maker and reviewer VENDOR are bound from the execution receipt, never from Git
author or committer fields. Acceptable receipt evidence includes:

- the Hermes `state.db` session's `billing_provider`;
- the provider rollout receipt under `~/.codex/sessions/`;
- an explicit provider `--usage-file` receipt.

If the receipt is absent or contradictory, report vendor as unknown/contested
and route the provenance question for resolution. A repo-local name such as
`codex@fleet.local` or `grok@fleet.local` identifies the checkout owner only;
it does not prove which model/provider executed a command.
