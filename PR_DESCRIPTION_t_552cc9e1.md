# fix(kanban): anchor REVIEW_VERDICT approval + gate human-authority holds (t_552cc9e1)

## Problem

`apply_approvals()` selected the auto-clear trigger with a raw SQL substring
match:

```sql
SELECT id, body FROM task_comments WHERE task_id = ? AND (
  body LIKE '%REVIEW_VERDICT=APPROVED%' OR body LIKE '%REVIEW_VERDICT: APPROVED%')
ORDER BY id DESC LIMIT 1
```

Four compounding weaknesses let a block be cleared WITHOUT a real approval:

1. **No anchoring** — a quoted/cited/mid-line occurrence anywhere in a comment
   body cleared the block. Live incident: jarvis-os/t_15b7ebc4, a Frank-gated
   `needs_input` card, auto-unblocked and dispatched a worker 101s after a PM
   comment merely *quoted* the token while narrating a different card.
2. **No separation of duties** — the comment author was never compared to the
   task assignee; a worker could approve its own block.
3. **No block_kind check** — `needs_input` / `capability` human-decision holds
   cleared via the same path as an ordinary review hold.
4. **Newest marker wins** — `ORDER BY id DESC LIMIT 1` let an accidental mention
   mask/override a real one.

Measured blast radius across all boards: 232 auto-clears, 56 on `needs_input`,
108 (47%) where comment author == task assignee.

## Fix

- Marker detection moved into the Python regex layer beside
  `_APPROVAL_NEGATED_RE` / `_APPROVAL_REOPEN_RE`, anchored at line start and
  ignoring matches inside fenced code blocks (`_APPROVAL_APPROVED_RE`,
  `_strip_fenced_code`).
- Separation of duties: refuse the clear when `comment.author == task.assignee`
  unless the author is on the reviewer-role allowlist (`_is_approval_reviewer`).
- Human-authority gate: `needs_input` / `capability` blocks are never
  auto-cleared (require a human/operator or an explicit approvals-registry grant).
  Legacy un-typed review holds stay auto-clearable (the normal `review-required`
  handoff).
- Oldest qualifying verdict wins (replaces `ORDER BY id DESC LIMIT 1`).

## Tests

- `tests/hermes_cli/test_kanban_apply_approvals_idempotent.py` — rewritten to the
  corrected contract; proves quoted/cited marker, code-fence marker, unanchored
  marker, self-approval, `needs_input`, and `capability` do NOT clear, while a
  genuine independent-reviewer approval on a review-class block STILL clears
  (no regression). 10 cases pass.
- `tests/hermes_cli/test_kanban_blocked_sticky.py` — added
  `test_human_authority_block_resists_approval_auto_clear`.
- `test_kanban_block_kinds.py` / `test_kanban_notify.py` / `test_kanban_core_functionality.py` green.

## Notes

- Pure tightening: only refuses previously-granted auto-clears; the operator
  `unblock_task` path is untouched, so any card left blocked can still be cleared
  by a human. No schema change. Rollback: `git revert`.
- Pre-existing baseline failures `test_kanban_db.py::test_dispatch_review_*` (3)
  fail identically on untouched `fork/main` (unmerged review-lane routing PR) and
  are unrelated to this diff.

Fixes the root cause class behind t_552cc9e1, t_15b7ebc4, t_cb5a275a.
