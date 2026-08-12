#!/usr/bin/env python3
"""profile_toolset_obligation_audit.py — fleet drift watch for t_f2b75a26.

Detects the silent class of defect where a profile's SOUL.md obliges it to run
commands (verification harnesses, typecheck, tests, git) but its
`platform_toolsets` allowlist omits `terminal`, so the obligation is
unsatisfiable and every exec-dependent dispatch to that profile burns a full
cycle before anyone notices.

`platform_toolsets` is an ALLOWLIST: omitting `terminal` disables exec
regardless of `terminal.backend`. Profiles with NO `platform_toolsets` key
inherit the default toolset and DO get terminal — those are not flagged.

Exit 0 = no gaps. Exit 1 = at least one profile has an exec obligation it
cannot satisfy. Quiet on success so it can run as a no_agent cron watchdog.

Usage:
  profile_toolset_obligation_audit.py            # report gaps only
  profile_toolset_obligation_audit.py --all      # full allowlist table
"""
import glob
import os
import re
import sys

import yaml

PROFILES_DIR = "/home/frank/.hermes/profiles"
# Cron script resolution is per-profile (~/.hermes/profiles/<p>/scripts/), and the
# cron loader rejects symlinks as path traversal, so this file must exist as a real
# copy in both places. Warn loudly if the two diverge rather than letting the
# scheduled copy silently rot behind the canonical one.
CANONICAL_COPY = "/home/frank/.hermes/scripts/profile_toolset_obligation_audit.py"
CRON_COPY = "/home/frank/.hermes/profiles/devops/scripts/profile_toolset_obligation_audit.py"

# Profiles whose exec-obligation gap is a RECORDED INTENTIONAL DECISION, not a defect.
# Suppressed from the non-zero exit; still listed under --all with the rationale.
#
# t_ecf1d553 resolved the PM/writer class three ways:
#   TIER 1 (fixed, removed from this map) — upero-pm, yorkstone-supplies-pm,
#     sycode-trading-pm, sycode-ai-pm, jarvis-os-pm were GRANTED terminal, contained by
#     the gate-pm-landing pre_tool_call hook. delegated-authority.md:6-7 already assigns
#     PMs trunk-landing duty, so this restored a granted authority rather than widening one.
#   TIER 2/3 (below) — these seats keep the git text only because the fleet-wide
#     "Git hygiene (2026-07-13)" block is appended to all 68 SOULs. None has the bespoke
#     "Pushing to our repos" section, none has a plausible git role. The correct fix is a
#     SOUL amendment (routed), not an exec grant.
KNOWN_EXCEPTIONS = {
    "comms-writer": "t_ecf1d553 decision (b): inherited git boilerplate only, no git role — SOUL amendment routed",
    "finance-ops": "t_ecf1d553 decision (b): inherited git boilerplate only, no git role — SOUL amendment routed",
    "jarvis-coordinator": "t_ecf1d553 decision (b): pure router ('do not become an untracked worker') — SOUL amendment routed",
}


# Phrases in a SOUL that only make sense if the profile can execute commands.
EXEC_OBLIGATION = re.compile(
    r"(verify-[a-z0-9-]+\.sh"
    r"|bash\s+/home/"
    r"|bun\s+run\s+"
    r"|bun\s+test\b"
    r"|npm\s+run\s+"
    r"|pytest\b"
    r"|\bpsql\b"
    r"|git\s+(commit|push|fetch|merge-base|worktree|ls-remote)\b"
    r"|run\s+the\s+(harness|verification|tests?|typecheck)"
    r"|re-run\s+the\s+tests?"
    r"|type-check)",
    re.I,
)

# The pre_tool_call hook that contains a reviewer's shell to read-only use.
READONLY_HOOK = "gate-critic-readonly"
# The pre_tool_call hook that contains a landing PM's shell to feature-branch pushes.
LANDING_HOOK = "gate-pm-landing"

# t_ecf1d553 third finding: the same allowlist-omission class applies to `file`.
# A SOUL carrying the mandatory Knowledge Persistence Invariant obliges the profile to
# write notes into a canonical vault; without `file` in the allowlist it has no
# write_file/patch and the obligation is unsatisfiable exactly like the exec case.
VAULT_OBLIGATION = re.compile(
    r"(Knowledge Persistence Invariant|obsidian-fleet-vault|obsidian/sycode-trading)", re.I
)
FILE_EXCEPTIONS = {
    # profile: rationale — set when a seat is deliberately note-free
}


def obligations(soul_path):
    if not os.path.exists(soul_path):
        return []
    hits = []
    for i, line in enumerate(open(soul_path, errors="replace"), 1):
        m = EXEC_OBLIGATION.search(line)
        if m:
            hits.append((i, m.group(0)))
    return hits


def vault_obligation(soul_path):
    """True when the SOUL mandates writing durable notes into a canonical vault."""
    if not os.path.exists(soul_path):
        return False
    return bool(VAULT_OBLIGATION.search(open(soul_path, errors="replace").read()))


def audit():
    gaps, table, file_gaps = [], [], []
    seen_real = set()
    for cfg_path in sorted(glob.glob(f"{PROFILES_DIR}/*/config.yaml")):
        name = cfg_path.split("/")[-2]
        # profiles/<alias> may be a symlink to another profile dir (e.g.
        # sycode-trading -> sycode-trading-pm). Audit the real seat once.
        real = os.path.realpath(os.path.dirname(cfg_path))
        if real in seen_real:
            continue
        seen_real.add(real)
        try:
            cfg = yaml.safe_load(open(cfg_path)) or {}
        except Exception as exc:  # malformed config is itself worth surfacing
            gaps.append((name, "-", f"UNPARSEABLE config.yaml: {exc}", []))
            continue

        pt = cfg.get("platform_toolsets")
        if not pt:
            continue  # inherits default toolset -> has terminal

        hook_txt = yaml.safe_dump(cfg.get("hooks") or {})
        has_hook = READONLY_HOOK in hook_txt or LANDING_HOOK in hook_txt
        soul = f"{PROFILES_DIR}/{name}/SOUL.md"
        hits = obligations(soul)
        needs_vault = vault_obligation(soul)

        for platform, tools in pt.items():
            tools = tools or []
            has_term = "terminal" in tools
            table.append((name, platform, has_term, has_hook, len(hits), tools))
            if not has_term and hits:
                gaps.append((name, platform, "SOUL requires exec but allowlist omits `terminal`", hits))
            if needs_vault and "file" not in tools and name not in FILE_EXCEPTIONS:
                file_gaps.append((name, platform,
                                  "SOUL mandates vault persistence but allowlist omits `file`", []))
    return gaps, table, file_gaps


def copy_drift():
    """Return a warning string if the cron copy has diverged from canonical."""
    try:
        a = open(CANONICAL_COPY, "rb").read()
        b = open(CRON_COPY, "rb").read()
    except OSError as exc:
        return f"WARNING: cannot compare canonical/cron copies of this script: {exc}"
    if a != b:
        return (f"WARNING: {CRON_COPY} has DRIFTED from {CANONICAL_COPY}. "
                "The scheduled detector is running stale logic — re-copy it.")
    return None


def main():
    show_all = "--all" in sys.argv
    gaps, table, file_gaps = audit()
    live = [g for g in gaps if g[0] not in KNOWN_EXCEPTIONS]
    known = [g for g in gaps if g[0] in KNOWN_EXCEPTIONS]
    drift = copy_drift()

    if show_all:
        print(f"{'profile':30s} {'platform':12s} {'term':6s} {'gate':8s} {'exec-refs':9s}")
        for name, platform, term, hook, n, tools in table:
            print(f"{name:30s} {platform:12s} {'yes' if term else 'NO':6s} "
                  f"{'yes' if hook else 'no':8s} {n:<9d} {','.join(tools)}")
        print()
        if known:
            print("Recorded intentional exceptions (suppressed from exit code):")
            for name, platform, _reason, _hits in known:
                print(f"  {name} [{platform}] -> {KNOWN_EXCEPTIONS[name]}")
            print()

    if file_gaps:
        print("VAULT-WRITE OBLIGATION GAP — SOUL mandates persistence but `file` is not allowlisted:")
        for name, platform, reason, _ in file_gaps:
            print(f"  {name} [{platform}]: {reason}")
        print()

    if not live and not file_gaps:
        if drift:
            print(drift)
            return 1
        if show_all:
            print("OK: no unrouted profile has an unsatisfiable exec or vault-write obligation.")
        return 0

    if drift:
        print(drift + "\n")
    if live:
        print("TOOLSET OBLIGATION GAP — profiles that cannot do what their SOUL mandates:")
        for name, platform, reason, hits in live:
            print(f"\n  {name} [{platform}]: {reason}")
            for line_no, frag in hits[:5]:
                print(f"    SOUL.md:{line_no}: {frag.strip()}")
            if len(hits) > 5:
                print(f"    ... +{len(hits) - 5} more exec references")
        print("\nFix: add `terminal` to platform_toolsets.<platform> (reviewers must also carry the")
        print(f"{READONLY_HOOK} hook; landing PMs the {LANDING_HOOK} hook), or amend the SOUL to")
        print("drop the obligation and route exec-dependent work to a terminal-capable profile.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
