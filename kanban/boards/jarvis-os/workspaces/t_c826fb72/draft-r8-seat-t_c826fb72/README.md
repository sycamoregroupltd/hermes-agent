# R8 REMEDIATE — draft (t_c826fb72), Claude Sonnet seat, 2026-09-02

Isolation HOLD in effect for this seat: no merge-to-main, no live A3, no hermes
update, no extra gateways, draft PR only, no live profile writes. This
directory is a **draft-only** deliverable; nothing under `/home/frank/.hermes/profiles/`
was touched.

## Scope re-check (live, read-only, 2026-09-02)

**Config secrets (7 profiles named in DONE_WHEN):** already remediated live by
a prior run and confirmed clean by a fresh read-only re-scan today. All
`api_key` fields in buzzgw, elon, fleet-engineer, jarvis-voice,
research-trading, trading-devops, trading-risk-reviewer are `${ENV_VAR}`
references, empty strings, or the local-ollama placeholder — zero
non-placeholder secret values remain. No config.yaml edits were needed or
made by this seat.

**SOUL.md absolute paths:** live re-scan today found 13 `/home/frank/` path
lines across 5 SOULs — same 5 files the board's oracle has been reporting:

| profile | hits |
|---|---|
| buzzgw | 8 |
| jarvis-voice | 2 |
| research-trading | 1 |
| trading-devops | 1 |
| yorkstone-supplies-pm | 1 |

(fleet-engineer is already clean from the prior remediation round.)

## What's in this directory

- `original/*.SOUL.md` — verbatim copies of the live files, captured before
  any edit, for diffing and as the pre-image if this is ever applied.
- `fixed/*.SOUL.md` — the same files with only the flagged path lines
  rewritten to canonical/symbolic wording (e.g. "the fleet vault
  (obsidian-fleet-vault)" instead of the literal `/home/frank/...` path).
  Every other line is byte-identical to `original/`. No guardrail, approval
  boundary, or identity content was weakened or removed — see
  `r8_seat_soul_diff.patch` for the full reviewable diff (13 lines changed
  across 5 files, nothing else).
- `r8_seat_soul_diff.patch` — unified diff, original → fixed, all 5 files.
- `apply-fixed-souls.sh` — NOT run by this seat. For a live, interactive
  operator only (Frank or an authorized seat who can answer the native
  protected-instruction-file approval prompt). It re-diffs live vs. the
  captured `original/` first and skips any profile that has drifted, backs
  up the live file with a timestamp, then copies `fixed/` over it.

## Verification performed by this seat

```
$ grep -c '/home/frank/' fixed/*.SOUL.md
buzzgw.SOUL.md:0
jarvis-voice.SOUL.md:0
research-trading.SOUL.md:0
trading-devops.SOUL.md:0
yorkstone-supplies-pm.SOUL.md:0
```

Zero hits in every fixed copy. Diff scope confirmed minimal (8/2/1/1/1 lines
changed, matching the 13 live hits exactly).

## Why this is draft-only, not a live fix

Prior runs on this card hit two separate walls:
1. A native protected-instruction-file approval gate blocks headless writes
   to SOUL.md and cannot be answered by a kanban worker.
2. This seat's own Frank GO explicitly scopes to "draft PRs only... no live
   apply" for this session (Isolation HOLD).

Both point the same direction: land the fix as a reviewable artifact, apply
it live only under interactive approval. `apply-fixed-souls.sh` is written
for exactly that handoff.

## Skills note

Requested skills `live-tree-mutation-guard`, `skill-hygiene`,
`git-multi-agent-hygiene` are not present under `~/.hermes/skills` at time of
writing — only `security-audit` exists on disk. This seat worked from the
git-hygiene rules already encoded in the SOULs themselves (never
`git checkout`/`reset`/etc. in the live `~/.hermes` tree) plus the
`EnterWorktree` isolation primitive, and did not invent or fabricate the
missing skills.

Seat: Claude Sonnet 5 (jarvis-os board, card t_c826fb72).
Session: https://claude.ai/code/session_01Uwfc7m4yuomrvV9oiqa9pP
