# Rollback notes — apply_approvals anchoring / human-authority fix (t_552cc9e1)

## What changed
`apply_approvals()` in `hermes_cli/kanban_db.py` no longer clears a block on a
bare substring match of `REVIEW_VERDICT=APPROVED`. Four weaknesses are closed:

1. **Anchoring** — marker detection moved out of `SQL LIKE` into a Python regex
   (`_APPROVAL_APPROVED_RE`, `^\s*(?:[-*>]\s*)?REVIEW_VERDICT\s*[:=]\s*APPROVED\b`,
   MULTILINE). A quoted/cited/mid-line occurrence no longer clears a block.
   Markers inside fenced code blocks are stripped (`_strip_fenced_code`) before
   matching, so a verdict *quoted* inside a code span is not a verdict.
2. **Separation of duties** — a comment whose author == the task's assignee does
   NOT clear the block unless the author is on the reviewer-role allowlist
   (`_APPROVAL_REVIEWER_ROLES`). Closes t_cb5a275a / t_552cc9e1.
3. **Human-authority gate** — `needs_input` and `capability` block kinds are
   never auto-cleared by an approval comment (they require a human/operator or
   an explicit approvals-registry grant). Legacy un-typed (`None`) review holds
   stay auto-clearable (the normal `review-required` handoff).
4. **Oldest qualifying verdict wins** — the old `ORDER BY id DESC LIMIT 1` picked
   the newest marker (so an accidental mention could mask a real one). We now
   scan oldest-first and pick the first genuinely-qualifying verdict.

## Tests (must stay green)
- `tests/hermes_cli/test_kanban_apply_approvals_idempotent.py` — rewritten;
  10 cases incl. quoted/cited marker, code-fence marker, unanchored marker,
  self-approval, needs_input, capability, genuine independent-reviewer (no
  regression), idempotence, reopen guard.
- `tests/hermes_cli/test_kanban_blocked_sticky.py` — added
  `test_human_authority_block_resists_approval_auto_clear`.
- Broad kanban suite: `test_kanban_block_kinds.py`, `test_kanban_notify.py`,
  `test_kanban_core_functionality.py` green.

## Known pre-existing baseline failure (NOT caused by this change)
`tests/hermes_cli/test_kanban_db.py::test_dispatch_review_*` (3 tests) fail on
untouched `fork/main` too — they belong to an unmerged review-lane routing PR and
are out of scope here. Reproduced on a separate clean fork/main worktree: same 3
failures, independent of this diff.

## Rollback / safety
- The change is pure tightening: it can only *refuse* a previously-granted
  auto-clear. Nothing that was blocked becomes unblockable — the operator/unblock
  path (`unblock_task`, explicit `kanban_unblock`) is untouched, so any card this
  now correctly leaves blocked can still be cleared by a human.
- **Indexer note:** there is a secondary fleet-local detector at
  `~/.hermes/scripts/kanban-approve-block-lockgate.py` (line 177, AP
  PROVAL_MARKERS substring check) with the SAME substring weakness. It is
  out of scope for this upstream PR and is tracked as a follow-up; it does not
  auto-clear (it only reports), so it is not a clearance vector.

## How to revert
`git revert <merge-commit>` or `git revert <commit>` on the integration branch.
No schema change, no migration, no data backfill needed.
