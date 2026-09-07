# Git identity and execution-vendor provenance

## Repository identity

Every Hermes-created checkout gets a repo-local identity before a commit:

```text
git config --local user.name <profile-or-seat>
git config --local user.email <profile-or-seat>@fleet.local
```

Kanban worktree provisioning applies this automatically. Linked worktrees enable
Git's `extensions.worktreeConfig` and use the `--worktree` scope so one sibling
cannot overwrite another sibling's identity. Plain repositories use `--local`.
The global Git identity is never changed by Hermes.

A `scratch` workspace is intentionally created as a directory rather than a
repository because many tasks do not use Git. To cover the case where a worker
creates a checkout later, dispatcher provisioning writes
`<workspace>/.hermes-git-bin/git`, an enforced worker-local wrapper, and prepends
that directory to the spawned worker's `PATH`. Successful `git init`, `git
clone`, and `git worktree add` commands through that wrapper configure the
resulting repository immediately. The wrapper is removed with the managed
scratch workspace. This is the explicit scratch-created-clone boundary: only
the dispatcher-spawned worker receives the wrapper; an unrelated process that
clones outside the workspace is out of scope and must configure its own repo.
The fable, codex, and grok seats use the same assignee-derived identity path
(`fable@fleet.local`, `codex@fleet.local`, and `grok@fleet.local`).

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

## Mechanism contract and liveness proof

Store: the source of truth is this repository's versioned
`scripts/ci/check_git_author_provenance.py` and `.github/workflows/ci.yml`.
The dispatcher wrapper is generated from the committed
`_GIT_IDENTITY_WRAPPER` copy in `hermes_cli/kanban_db.py`; it is not a second
editable live copy.

Executed copy and invocation: GitHub Actions checks out the PR head into
`$GITHUB_WORKSPACE`, then the `git-author-provenance` job executes exactly
`python3 scripts/ci/check_git_author_provenance.py`. The job also runs on the
PR event path selected by `detect`, with full history, so the checker can read
`base..head` from `GITHUB_EVENT_PATH`.

Gateway/scheduler/service liveness: GitHub Actions is the service scheduler;
`pull_request` is the admission event; `git-author-provenance` is the named
service job; and `all-checks-pass` consumes its result through `needs`. A PR
cannot pass the required aggregate while this check fails. Dispatcher liveness
is the native kanban dispatch loop: the ready and review paths both call
`_configure_workspace_git_identity` before spawning a worker, and a provisioning
failure is recorded as a spawn failure rather than silently launching an
unconfigured worker.

Deliver target and named consumer: the deliver target is the fork PR checks
surface (`git-author-provenance`), and the named consumer is the existing
`all-checks-pass` aggregate/branch-protection consumer. The worker-local
wrapper delivers the identity invariant to the named worker process through
`PATH` and `HERMES_GIT_IDENTITY_*` environment values; the resulting Git config
is the repository-local consumer used by Git at commit time.

## Verification matrix

| Row | Exact command / evidence | Result |
|---|---|---|
| Dispatcher plain + linked worktree | `python -m pytest -q tests/hermes_cli/test_kanban_git_identity.py` | PASS: local and worktree scopes isolated; global config untouched |
| Scratch clone/init enforcement | `python -m pytest -q tests/hermes_cli/test_kanban_git_identity.py::test_scratch_workspace_provisions_enforced_clone_identity` | PASS: wrapper-created repo has profile-local name/email |
| fable/codex/grok seat coverage | `python -m pytest -q tests/hermes_cli/test_kanban_git_identity.py -k external_seats` | PASS: all three identity seeds are profile-scoped |
| CI checker behavior | `python -m pytest -q tests/scripts/test_git_author_provenance.py` | PASS: Claude identity fails; repo-local identity passes |
| Exact executed CI copy | `python3 scripts/ci/check_git_author_provenance.py --base <base> --head <head>` | PASS/FAIL is emitted from the same committed copy; CI invokes it without alternate wrapper |
| Workflow wiring/liveness | `.github/workflows/ci.yml`: `git-author-provenance` runs `python3 scripts/ci/check_git_author_provenance.py`; `all-checks-pass.needs` includes the job | PASS: static wiring inspected in candidate diff; live PR-run status remains an external GitHub check |

The final row is deliberately not inferred from a local unit test: the
candidate PR's GitHub Actions run is the external liveness evidence, while the
local matrix proves the executed path and its failure semantics.
