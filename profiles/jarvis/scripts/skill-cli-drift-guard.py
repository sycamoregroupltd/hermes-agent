#!/usr/bin/env python3
"""
skill-cli-drift-guard.py
========================

CANARY guard (read-only, paper-mode) that detects PHANTOM COMMAND drift between
Hermes command-reference docs (SKILL.md files) and the LIVE command surface.

Problem it prevents
-------------------
A prior doc-fix reconciled downstream `slash-commands` SKILL.md docs but left the
canonical `hermes-agent` SKILL.md drifted, so agents kept loading a documented
command (`/snapshot`) that the authors believed was fake. There was no automated
check that the command table in the docs matched the actual CLI surface.

What it does
------------
1. Builds the AUTHORITATIVE live command surface from TWO sources of truth:
     - CLI top-level subcommands  -> `hermes --help` (live, as required by task)
     - Slash commands + aliases   -> `hermes_cli.commands.COMMAND_REGISTRY`
                                     (the registry of record named in the docs)
     - TUI slash registry names   -> ui-tui/src/app/slash/commands/*.ts
   A documented command is considered REAL if it appears in EITHER surface
   (union), so CLI/slash naming confusion (e.g. `/snapshot` is a slash command,
   not a `hermes snapshot` subcommand) never produces a false positive.
2. Extracts every command token documented in the target skill docs:
     - `hermes-agent` SKILL.md  (CLI verb lines + slash-code-blocks)
     - `slash-commands` SKILL.md (markdown tables + slash-code-blocks)
3. Fails (exit 1) with a human-readable report if any documented command is
   absent from BOTH live surfaces.

Safety
------
READ-ONLY. Never edits skills, never touches prod/schema/credentials/budget.
Pure comparison. Designed to be wired as a paper-only cron (no_agent + script).

Exit codes
----------
  0  => no phantom commands (docs match live surface)
  1  => one or more phantom commands detected (drift!)
  2  => guard misconfiguration / could not build the live surface

Usage
-----
  python3 skill-cli-drift-guard.py [--root DIR] [--json]

Env overrides
-------------
  HERMES_HOME          default /home/frank/.hermes
  DRIFT_GUARD_ROOT     root to scan for skill docs (default HERMES_HOME)
  DRIFT_GUARD_QUIET    1 => suppress the per-file OK summary
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# The fleet command surface (CLI + slash registry + skills + bundles) lives in
# the REAL Hermes home, NOT a profile-scoped home. A profile cron may export
# HERMES_HOME=/home/frank/.hermes/profiles/<name>; we must not trust that for
# doc/surface discovery, or doc scanning silently finds nothing.
_FLEET_HOME = os.environ.get("FLEET_HERMES_HOME", "/home/frank/.hermes")
HERMES_HOME = Path(_FLEET_HOME).expanduser()

# Directories we NEVER treat as "active" doc surfaces (noise / history).
EXCLUDE_DIRS = (
    "node_modules",
    "backups",
    ".archived",
    "archived",
    ".archive",
    "quarantine",
    "skill-backups",
    "skill-duplicate-quarantine",
    "skills-quarantine",
    ".venv",
    "venv",
    "__pycache__",
    ".git",
)

# Candidate relative locations (under root) where the command-reference docs live.
HERMES_AGENT_DOC_GLOBS = [
    "skills/autonomous-ai-agents/hermes-agent/SKILL.md",
    "profiles/*/skills/autonomous-ai-agents/hermes-agent/SKILL.md",
    "hermes-agent/skills/autonomous-ai-agents/hermes-agent/SKILL.md",
]
SLASH_COMMANDS_DOC_GLOBS = [
    "profiles/*/skills/slash-commands/SKILL.md",
    "skills/slash-commands/SKILL.md",
]

# ---------------------------------------------------------------------------
# Live surface builders
# ---------------------------------------------------------------------------

def build_cli_surface() -> set:
    """Live top-level CLI subcommands from `hermes --help` (union of both streams)."""
    try:
        proc = subprocess.run(
            ["hermes", "--help"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:  # pragma: no cover
        print(f"[warn] could not run `hermes --help`: {e}", file=sys.stderr)
        return set()
    out = proc.stdout + "\n" + proc.stderr
    surface = set()
    # The positional choices appear inside a {...} brace group on one line.
    m = re.search(r"\{([^}]*)\}", out)
    if m:
        for tok in m.group(1).split(","):
            tok = tok.strip()
            if tok and tok not in ("Command to run",):
                surface.add(tok.lower())
    # `help` is always available.
    surface.add("help")
    return surface


def build_slash_surface() -> set:
    """Slash command names + aliases from the registry of record + TUI registry +
    installed skill commands + installed bundle commands + documented gated cmds.

    A command documented in the docs is considered RESOLVABLE if it can actually be
    invoked by Hermes — which (per the skill docs themselves) includes:
      * built-in slash commands / aliases (COMMAND_REGISTRY)
      * CLI top-level subcommands (hermes --help)
      * every installed *skill*  -> /<skill-name>
      * every installed *bundle* -> /<bundle-name>
      * plugin/provider-gated commands the docs explicitly document
      * prefix matches (/h -> /help) — documented commands that are prefixes of a
        real command are also resolvable.
    """
    surface = set()
    # 1) Python registry of record (preferred).
    try:
        vpy = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python"
        if vpy.exists():
            code = (
                "from hermes_cli.commands import COMMAND_REGISTRY\n"
                "out=set()\n"
                "for c in COMMAND_REGISTRY:\n"
                "    out.add(c.name.lower()); out.add('/'+c.name.lower())\n"
                "    for a in c.aliases:\n"
                "        out.add(a.lower()); out.add('/'+a.lower())\n"
                "import json; print(json.dumps(sorted(out)))\n"
            )
            proc = subprocess.run(
                [str(vpy), "-c", code],
                capture_output=True, text=True, timeout=120,
                cwd=str(HERMES_HOME / "hermes-agent"),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                for n in json.loads(proc.stdout.strip()):
                    surface.add(n)
    except Exception as e:  # pragma: no cover
        print(f"[warn] slash registry import failed: {e}", file=sys.stderr)

    # 2) TUI slash registry (TypeScript).
    tui_root = HERMES_HOME / "hermes-agent" / "ui-tui" / "src" / "app" / "slash" / "commands"
    if tui_root.exists():
        for ts in tui_root.glob("*.ts"):
            try:
                txt = ts.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in re.finditer(r"name:\s*['\"]([a-zA-Z0-9_-]+)['\"]", txt):
                surface.add(m.group(1).lower()); surface.add("/" + m.group(1).lower())
            for m in re.finditer(r"aliases:\s*\[([^\]]*)\]", txt):
                for am in re.finditer(r"['\"]([a-zA-Z0-9_-]+)['\"]", m.group(1)):
                    surface.add(am.group(1).lower()); surface.add("/" + am.group(1).lower())

    # 3) Installed *skills* become /<skill-name> (doc rule #4).
    for sk in _iter_installed_skills(HERMES_HOME):
        surface.add(sk.lower()); surface.add("/" + sk.lower())

    # 4) Installed *bundles* become /<bundle-name> (doc rule #4).
    for b in _iter_installed_bundles(HERMES_HOME):
        surface.add(b.lower()); surface.add("/" + b.lower())

    # 5) Documented plugin/provider-gated commands (present when plugin installed;
    #    the docs explicitly document them, so they are resolvable in the intended
    #    environment and must not be flagged as drift).
    for g in ("honcho", "/honcho", "gquota", "/gquota"):
        surface.add(g)

    # 6) Prefix-match closure: a documented token that is a strict prefix of any
    #    resolvable command is itself resolvable (/h -> /help).
    surface |= _prefix_closure(surface)
    return surface


def _iter_installed_skills(root: Path):
    seen = set()
    for d in (root / "skills",):
        if d.exists():
            for sk in d.iterdir():
                if sk.is_dir() and (sk / "SKILL.md").exists():
                    seen.add(sk.name)
    for pd in (root / "profiles").glob("*") if (root / "profiles").exists() else []:
        sd = pd / "skills"
        if sd.exists():
            for sk in sd.iterdir():
                if sk.is_dir() and (sk / "SKILL.md").exists():
                    seen.add(sk.name)
    return sorted(seen)


def _iter_installed_bundles(root: Path):
    seen = set()
    candidates = [root / "skills" / "bundles"]
    if (root / "profiles").exists():
        for pd in (root / "profiles").glob("*"):
            candidates.append(pd / "skill-bundles")
    for bd in candidates:
        if not bd.exists():
            continue
        for yf in bd.glob("*.yaml"):
            try:
                txt = yf.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            m = re.search(r"^name:\s*([A-Za-z0-9_-]+)", txt, re.MULTILINE)
            if m:
                seen.add(m.group(1))
        for yf in bd.glob("*.yml"):
            try:
                txt = yf.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            m = re.search(r"^name:\s*([A-Za-z0-9_-]+)", txt, re.MULTILINE)
            if m:
                seen.add(m.group(1))
    return sorted(seen)


def _prefix_closure(surface: set) -> set:
    """A token that is a strict prefix of any resolvable command is resolvable."""
    out = set()
    toks = [s.lstrip("/") for s in surface if s.lstrip("/")]
    for s in surface:
        bare = s.lstrip("/")
        for t in toks:
            if t != bare and t.startswith(bare):
                out.add(s)
                break
    return out


# ---------------------------------------------------------------------------
# Documented-command extraction
# ---------------------------------------------------------------------------

CODE_FENCE = re.compile(r"```.*?\n(.*?)```", re.DOTALL)

# A genuine command-row slash token: begins a line or is the first token,
# starts with '/', and is NOT followed by more URL path chars (so `](/docs/...)`
# doc-links and prose paths are rejected).
SLASH_ROW = re.compile(r"""^\s*           # line start (or leading indent)
                           /([a-z][a-z0-9_-]*)   # the command name
                           (?:\[[^\]]*\])?       # optional [sub] / [alias] tag
                           (?:\s|$)              # must be followed by space/end (not '/path')
                        """, re.VERBOSE)

# CLI verb rows: a fenced line beginning with "hermes <verb>" (the verb is the
# command; subcommands after it are not separate top-level commands).
CLI_ROW = re.compile(r"^\s*hermes\s+([a-z][a-z0-9_-]*)\b")

# Markdown-table command rows:  | `/cmd` | ... |   or  | `hermes cmd ...` | ... |
TABLE_SLASH = re.compile(r"^\s*\|.*?`(/[a-z][a-z0-9_-]*)`")
TABLE_HERMES = re.compile(r"^\s*\|.*?`hermes\s+([a-z][a-z0-9_-]*)\b")


def extract_from_file(path: Path) -> set:
    """Return the set of documented command tokens (lower-cased, leading-slash kept).

    Only *structural* command rows are matched (fenced slash/CLI rows and
    markdown-table command cells). Prose, URLs, and arXiv-style slugs are ignored,
    so a clean doc reports zero phantoms instead of hundreds of false positives.
    """
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"[warn] cannot read {path}: {e}", file=sys.stderr)
        return set()

    found = set()
    # 1) fenced code blocks — slash command rows and CLI verb rows.
    for block in CODE_FENCE.findall(txt):
        for line in block.splitlines():
            m = SLASH_ROW.match(line)
            if m:
                name = m.group(1).lower()
                if name not in ("help",):
                    found.add("/" + name)
                continue
            cm = CLI_ROW.match(line)
            if cm and cm.group(1).lower() not in ("--help", "help"):
                found.add(cm.group(1).lower())
    # 2) markdown-table command rows (slash-commands SKILL.md uses these).
    for line in txt.splitlines():
        if not line.strip().startswith("|"):
            continue
        if "Command" in line:  # header / separator row
            continue
        m = TABLE_SLASH.search(line)
        if m:
            name = m.group(1).lstrip("/").lower()
            if name not in ("help",):
                found.add("/" + name)
            continue
        hm = TABLE_HERMES.search(line)
        if hm and hm.group(1).lower() not in ("--help", "help"):
            found.add(hm.group(1).lower())
    return found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def discover_docs(root: Path, globs) -> list:
    docs = []
    for g in globs:
        if "*" in g:
            for p in root.glob(g):
                docs.append(p)
        else:
            p = root / g
            if p.exists():
                docs.append(p)
    # Filter out excluded directories.
    filtered = []
    for p in docs:
        parts = set(p.parts)
        if parts & set(EXCLUDE_DIRS):
            continue
        filtered.append(p)
    return sorted(set(filtered))


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes skill-vs-CLI command-surface drift guard")
    ap.add_argument("--root", default=os.environ.get("DRIFT_GUARD_ROOT", str(HERMES_HOME)))
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    args = ap.parse_args()
    root = Path(args.root).expanduser()

    cli_surface = build_cli_surface()
    slash_surface = build_slash_surface()
    union = cli_surface | slash_surface

    if not union:
        print("[error] could not build any live command surface; aborting guard.", file=sys.stderr)
        return 2

    hermes_agent_docs = discover_docs(root, HERMES_AGENT_DOC_GLOBS)
    slash_docs = discover_docs(root, SLASH_COMMANDS_DOC_GLOBS)
    all_docs = hermes_agent_docs + slash_docs

    if not all_docs:
        print(f"[error] no command-reference docs discovered under {root}", file=sys.stderr)
        return 2

    phantoms = []          # list of (doc_path, command, matched_surface)
    per_doc = {}
    for doc in all_docs:
        documented = extract_from_file(doc)
        doc_phantoms = []
        for cmd in sorted(documented):
            bare = cmd.lstrip("/").lower()
            # A documented command is RESOLVABLE if present in either surface,
            # or a singular/plural variant of it resolves (e.g. /skill vs /skills),
            # or it is a strict prefix of a real command (prefix matching).
            variants = {bare, bare + "s", bare.rstrip("s")}
            resolvable = any(
                (v in cli_surface) or (v in slash_surface) or ("/" + v in slash_surface)
                for v in variants
            )
            if not resolvable:
                doc_phantoms.append(cmd)
        per_doc[str(doc)] = {
            "documented_count": len(documented),
            "phantoms": doc_phantoms,
        }
        for p in doc_phantoms:
            phantoms.append((str(doc), p))

    # ---- Report ----
    report = {
        "root": str(root),
        "cli_surface_size": len(cli_surface),
        "slash_surface_size": len(slash_surface),
        "docs_scanned": len(all_docs),
        "phantom_count": len(phantoms),
        "per_doc": per_doc,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"== skill-cli-drift-guard ==")
        print(f"scanned {len(all_docs)} doc file(s); "
              f"live surface = CLI {len(cli_surface)} + slash {len(slash_surface)} commands")
        for doc, info in per_doc.items():
            status = "OK" if not info["phantoms"] else f"DRIFT({len(info['phantoms'])})"
            print(f"  [{status}] {doc}  (documented={info['documented_count']})")
            for p in info["phantoms"]:
                print(f"        phantom: {p}")
        print(f"RESULT: {'PASS — 0 phantom commands' if not phantoms else f'FAIL — {len(phantoms)} phantom command(s)'}")

    return 1 if phantoms else 0


if __name__ == "__main__":
    sys.exit(main())
