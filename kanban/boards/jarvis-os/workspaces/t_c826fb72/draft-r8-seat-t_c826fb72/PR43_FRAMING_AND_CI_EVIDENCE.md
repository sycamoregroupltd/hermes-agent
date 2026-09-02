# PR #43 Framing Review & CI Evidence

**Date:** 2026-09-02  
**PR:** https://github.com/sycamoregroupltd/hermes-agent/pull/43  
**Branch:** `worktree-r8-remediate-secrets-souls`  
**Status:** DRAFT — Isolation HOLD in effect

---

## Executive Summary

PR #43 accurately framed as containing:
1. **SOUL.md draft remediation artifacts** (13 workspace files for P8 check 3 / t_c826fb72)
2. **Attribution mapping** (t@t → Frankws)

**NOT an "attribution-only CI fix"** — primary content is the R8 SOUL draft workspace.

---

## PR Contents (2 Commits)

### Commit A: d6d2b6028b (SOUL Draft Stack)
**Author:** t <t@t>  
**Title:** `draft(r8-remediate): mechanical SOUL.md path-strip for P8 check 3 (t_c826fb72)`

**Files changed (13):**
- `kanban/boards/jarvis-os/workspaces/t_c826fb72/draft-r8-seat-t_c826fb72/README.md`
- `kanban/boards/jarvis-os/workspaces/t_c826fb72/draft-r8-seat-t_c826fb72/apply-fixed-souls.sh`
- `kanban/boards/jarvis-os/workspaces/t_c826fb72/draft-r8-seat-t_c826fb72/original/` (5 SOUL.md files)
- `kanban/boards/jarvis-os/workspaces/t_c826fb72/draft-r8-seat-t_c826fb72/fixed/` (5 SOUL.md files)
- `kanban/boards/jarvis-os/workspaces/t_c826fb72/draft-r8-seat-t_c826fb72/r8_seat_soul_diff.patch`

**Scope:**
- Draft-only remediation workspace for 5 profiles with absolute path violations
- 13 path line rewrites (buzzgw: 8, jarvis-voice: 2, research-trading: 1, trading-devops: 1, yorkstone-supplies-pm: 1)
- `apply-fixed-souls.sh` targets `~/.hermes/profiles/` but was **NOT RUN** (Isolation HOLD)

### Commit B: d9063afd9c (Attribution Gate Pass)
**Author:** Cursor Agent <cursoragent@cursor.com>  
**Title:** `chore: add contributor mapping for t@t (Frankws)`

**Files changed (1):**
- `contributors/emails/t@t` → content: `Frankws`

**Attribution gate:** ✅ PASS

### Commit C: 68ed88627f (Documentation Update)
**Author:** Cursor Agent <cursoragent@cursor.com>  
**Title:** `docs(r8-remediate): reinforce Isolation HOLD warnings in workspace README`

**Files changed (1):**
- Enhanced `README.md` with stronger warnings about draft-only status and `apply-fixed-souls.sh` danger

---

## Isolation HOLD Compliance

### Requirements
- ✅ Draft PR only (no merge)
- ✅ `apply-fixed-souls.sh` was **NOT RUN**
- ✅ No writes to `~/.hermes/profiles/`
- ✅ Report/workspace artifacts only
- ✅ Explicit warnings in README and PR title

### Critical Safety Note
**`apply-fixed-souls.sh` is live-write capable:**
- Targets `~/.hermes/profiles/` directory
- Designed for interactive operator execution only
- Requires native protected-instruction-file approval prompts
- Must remain UNRUN under current Isolation HOLD

---

## CI Status: Failures Pre-existing on Main

### Claim Verification
**Previous agent claim:** "CI failures are pre-existing on main"  
**Verification method:** Compare PR #43 check runs with main branch CI run 33379767003

### Evidence Table

| Check Name | PR #43 Status | Main Status (06aec275) | Verdict | Evidence URL |
|------------|---------------|------------------------|---------|-------------|
| Desktop E2E / Playwright E2E (Linux) | ❌ FAIL | ❌ FAIL | **PRE-EXISTING** | [main CI run](https://github.com/sycamoregroupltd/hermes-agent/actions/runs/33379767003) |
| Python tests / Run tests slice 4/8 | ❌ FAIL | ❌ FAIL | **PRE-EXISTING** | [main CI run](https://github.com/sycamoregroupltd/hermes-agent/actions/runs/33379767003) |
| Python tests / Run tests slice 5/8 | ❌ FAIL | ❌ FAIL | **PRE-EXISTING** | [main CI run](https://github.com/sycamoregroupltd/hermes-agent/actions/runs/33379767003) |
| Python tests / Run tests slice 6/8 | ❌ FAIL | ❌ FAIL | **PRE-EXISTING** | [main CI run](https://github.com/sycamoregroupltd/hermes-agent/actions/runs/33379767003) |
| Python tests / Run tests slice 8/8 | ❌ FAIL | ❌ FAIL | **PRE-EXISTING** | [main CI run](https://github.com/sycamoregroupltd/hermes-agent/actions/runs/33379767003) |

### Main Branch Reference
- **Commit:** [06aec275c78d63c37e50945030c25e07e8665a5c](https://github.com/sycamoregroupltd/hermes-agent/commit/06aec275c78d63c37e50945030c25e07e8665a5c)
- **CI Run:** [33379767003](https://github.com/sycamoregroupltd/hermes-agent/actions/runs/33379767003)
- **Date:** 2026-08-31
- **CI Conclusion:** failure
- **Workflow:** CI

### Passing Checks (Both PR & Main)
- ✅ Check contributors / check-attribution
- ✅ Check uv.lock / uv lock --check
- ✅ Python lints / ruff enforcement (blocking)
- ✅ Python lints / Windows footguns (blocking)
- ✅ Python tests / e2e
- ✅ Python tests / Run tests slice 1/8
- ✅ Python tests / Run tests slice 2/8
- ✅ Python tests / Run tests slice 3/8
- ✅ Python tests / Run tests slice 7/8
- ✅ OSV scan / Emit review status
- ✅ All JS & TS checks

### Conclusion
**Claim PROVEN:** All 5 failing checks are pre-existing on main branch.

**Recommendation:** These CI failures do not block review of the draft artifacts. However, per Isolation HOLD, this PR should **not be merged** regardless of CI status — it is documentation/review-only.

---

## Correct Framing Summary

### What This PR IS
- Draft remediation workspace for SOUL.md path violations (primary content)
- Attribution mapping for commit author compliance (secondary, supporting)
- Documentation artifact for P8 check 3 remediation approach
- Review-ready diff and apply script (unexecuted)

### What This PR IS NOT
- ❌ An "attribution-only CI fix"
- ❌ A live fix (no `apply-fixed-souls.sh` execution)
- ❌ Ready for merge (Isolation HOLD: draft only)
- ❌ A config secrets remediation (configs already clean, verified read-only)

### Accurate Description
"R8 SOUL.md draft remediation workspace (13 files: original/fixed copies, diff, apply script targeting ~/.hermes/profiles) + attribution mapping. Draft only — Isolation HOLD: do not merge, do not run apply-fixed-souls.sh."

---

## Reviewer Guidance

1. **Primary review focus:** Workspace structure in `kanban/boards/jarvis-os/workspaces/t_c826fb72/draft-r8-seat-t_c826fb72/`
2. **Verify:** `r8_seat_soul_diff.patch` shows only path line changes (no guardrail weakening)
3. **Verify:** `apply-fixed-souls.sh` was not executed (confirmed by Isolation HOLD constraints)
4. **CI failures:** Acknowledged as pre-existing, do not block artifact review
5. **Merge status:** DO NOT MERGE per Isolation HOLD

---

**Generated by:** Cloud Agent (PR framing review task)  
**Review completed:** 2026-09-02
