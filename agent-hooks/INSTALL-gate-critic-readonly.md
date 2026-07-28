# Install implications & shell-hook approval fingerprint drift — `gate-critic-readonly`

Branch: `fix/t_2f52534d-critic-kanban-create`
Commit: `66558ccbafd58f75b7a423a3442e2569e4a3f3e5`
Source repo: `sycamoregroupltd/hermes-dgx-fleet-automation` (DGX fleet automation tree)
Companion task chain: `t_2f52534d` (carve-out maker) → `t_98513b5c` (independent verification) → `t_4accf7d1` (this landing/doc card)

> Maker card scope: this document is the deliverable of the **maker** lane. It DOES NOT
> perform any of the following — those are out of scope for this card and belong to a
> separate, gated rollout by the `os-reviewer` lane and the orchestrator:
> - live install / copy of the hook into `/home/frank/.hermes/agent-hooks/`
> - profile-config change, allowlist change, or any `config.yaml` edit
> - deploy, service restart, or gateway restart
> - credential change, provider routing change, or any production mutation
> - trading mutation, DB/queue mutation, or runtime mutation
> - self-review (review is the independent `os-reviewer` lane's job — see end of doc)

---

## 1. What this commit changes

Commit `66558cc` adds a narrow, fail-closed carve-out to the critic read-only gate so a
reviewer profile can still create/route kanban work (`kanban_create`) **without** mutating
the artifact it judges. It introduces:

- `agent-hooks/gate-critic-readonly.py` — adds `CONTROL_PLANE_ROUTE_TOOLS = {"kanban_create"}`
  and an early `allow()` when `tool == "kanban_create"`. All other write-shaped calls and
  shell VCS mutations remain blocked per the existing rules (the separate
  `gate-kanban-dupe-create.sh` still hard-gates `kanban_create` payloads).
- `agent-hooks/gate-critic-readonly.sh` — unchanged logic (delegates to the .py).
- `agent-hooks/gate-critic-readonly.selftest.sh` — 13 deterministic cases (allow
  `kanban_comment`/`kanban_block`/`kanban_complete`/exact `kanban_create`; block
  `create_file`/`write_file`/`patch`/namespaced write/terminal git/content-less write/
  deceptive `kanban_create_file` names).
- `agent-hooks/gate-kanban-dupe-create.sh` — the separate Frank-duplicate/guard gate hooked
  on `kanban_create`.
- `agent-hooks/run-selftests.sh` — umbrella runner wiring `gate-critic-readonly`.

Self-test result on this checkout: `PASS=13 FAIL=0` (exit 0).

---

## 2. Profiles that reference `gate-critic-readonly` (executed-copy / install targets)

The hook is wired in `pre_tool_call` (event, **no matcher**, so it sees every tool call) in
each of these 9 reviewer/guardian profiles, paired with `gate-kanban-dupe-create.sh`
(matcher `kanban_create`):

| Profile | `gate-critic-readonly.sh` | `gate-kanban-dupe-create.sh` (matcher `kanban_create`) |
|---|---|---|
| `guardian` | config.yaml:342 | config.yaml:344 |
| `os-reviewer` | config.yaml:335 | config.yaml:337 |
| `platform-reviewer` | config.yaml:335 | config.yaml:337 |
| `upero-design-reviewer` | config.yaml:335 | config.yaml:337 |
| `eval-runner` | config.yaml:342 | config.yaml:344 |
| `tenant-guardian` | config.yaml:342 | config.yaml:344 |
| `paper-risk` | config.yaml:342 | config.yaml:344 |
| `trading-risk-reviewer` | config.yaml:372 | config.yaml:374 |
| `yorkstone-supplies-reviewer` | config.yaml:320 | config.yaml:322 |

**Install consequence:** copying the new `gate-critic-readonly.py` from this branch to
`/home/frank/.hermes/agent-hooks/` immediately widens every one of the 9 profiles above —
they will start ALLOWING exact `kanban_create` routing (reviewers can spawn child builder
tasks) while all artifact mutation stays blocked. No profile `config.yaml` change is needed
because the wiring (`pre_tool_call` → `gate-critic-readonly.sh`) already exists; only the
hook *script body* changes behaviour.

---

## 3. Shell-hook approval fingerprint drift (the part that needs a human gate)

### 3a. Live hook differs from committed hook (SHA drift)

| File | Live SHA256 (`/home/frank/.hermes/agent-hooks/`) | Committed SHA256 (this branch @ `66558cc`) |
|---|---|---|
| `gate-critic-readonly.py` | `ded9873539d29a7b85844797f0d0b1f0f1aedcfc665fdaa8faa684680642d213` | `abb136c6805d955e90aee54aab02703811fa8b1fe5832b6f169d1124a5d144ed` |
| `gate-critic-readonly.sh` | `79e8964e22587aab9f83e683dfdceef8fab47c471a478ed33e31818dbaf30df9` | `79e8964e22587aab9f83e683dfdceef8fab47c471a478ed33e31818dbaf30df9` |

The `.sh` wrapper is byte-identical. The `.py` differs: the live copy lacks the
`CONTROL_PLANE_ROUTE_TOOLS` carve-out. Installing this branch moves the live `.py` SHA to
`abb136c6…`.

### 3b. Allowlist fingerprint record — MISSING for `gate-critic-readonly.sh`

The committed `shell-hooks-allowlist.json` in this repo contains a fingerprint entry for
`gate-kanban-dupe-create.sh` (approved `2026-07-17T07:33:57Z`, `script_mtime_at_approval`
`2026-07-05T20:19:14Z`) but contains **no entry for `gate-critic-readonly.sh`**.

Why it still works today: every referencing profile sets `hooks_auto_accept: true`, so a
`pre_tool_call` hook runs without an explicit allowlist fingerprint record. The allowlist
file is the *optional, stricter* approval regime (used by `gate-provider-governance`,
`gate-second-brain-writes`, `gate-config-writes`, `gate-kanban-complete`, `gate-kanban-dupe-create`,
etc.).

**Drift to resolve at rollout** (by the `os-reviewer`/orchestrator lane, NOT this card):
- If the fleet ever enforces the allowlist as a hard gate for `pre_tool_call` hooks, a new
  fingerprint entry for `gate-critic-readonly.sh` MUST be added (covering both the new
  `abb136c6…` `.py` and the unchanged `79e8964e…` `.sh`) or the hook will fail to load under
  that regime.
- Documented here so the reviewer does not silently assume the critic hook is already
  approval-recorded: it is not. The `gate-kanban-dupe-create.sh` entry already exists and
  needs no change for this carve-out.
- The `"approved_at"` / `"script_mtime_at_approval"` values for any new critic-hook entry
  must reflect the real install time, not a retroactive date — otherwise the adoption
  auditor's fingerprint proof becomes inconsistent with `profile_script_drift_watch.py`.

### 3c. Adoption-auditor expectation

Per the second-brain contract, every Hermes root and **every named profile** must carry the
exact write-gate + first-turn-injector entries, and per-profile consent fingerprints must
match the installed script modification fingerprints. The 9 profiles above already pin
`gate-critic-readonly.sh` in `pre_tool_call`; their installed-script fingerprint will change
once the new `.py` lands. The reviewer lane should re-run `profile_script_drift_watch.py`
after install to confirm the 9 profiles' recorded fingerprints update to `abb136c6…`.

---

## 4. Rollback plan (for the rollout lane, not executed here)

- Revert the installed `gate-critic-readonly.py` to the prior `ded98735…` (pre-carve-out)
  by restoring the live copy from the previous commit / known-good backup, OR `git checkout
  <parent-of-66558cc> -- agent-hooks/gate-critic-readonly.py` in the fleet-automation repo
  and re-deploy.
- `gate-critic-readonly.sh` and all profile `config.yaml` wiring are unchanged, so no profile
  edit is required for rollback.
- The separate `gate-kanban-dupe-create.sh` is independent and unaffected by rollback of the
  carve-out.
- No migration, DB/queue, credential, provider, or trading state is touched by this change.

---

## 5. Verification already performed (evidence)

- Focused selftest `agent-hooks/gate-critic-readonly.selftest.sh`: `PASS=13 FAIL=0` (exit 0).
- Umbrella `agent-hooks/run-selftests.sh`: exit 0.
- `python3 -m py_compile agent-hooks/gate-critic-readonly.py`: OK.
- All 4 changed `.sh` files pass `bash -n`.
- `git status` on this branch is clean apart from the untracked `agent-hooks/__pycache__/`
  (build artifact, removed before landing).
- Independent verification lane `t_98513b5c` confirmed the same suites pass on `66558cc`
  from a throwaway worktree and that no drift/other tracked files changed.

---

## 6. Handoff statements (required by card acceptance)

- **No live install performed.** The hook was NOT copied to `/home/frank/.hermes/agent-hooks/`.
- **No profile-config change, allowlist change, deploy, restart, credential change,
  provider routing change, trading mutation, DB/queue mutation, or runtime mutation performed.**
- **No self-review performed.** This maker card only documents and lands code on the feature
  branch. The independent **`os-reviewer`** lane must review and approve before any rollout
  to live profiles.
