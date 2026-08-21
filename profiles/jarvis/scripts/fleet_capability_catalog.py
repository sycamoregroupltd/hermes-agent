#!/usr/bin/env python3
"""Generate the Fleet Capability Catalog into the fleet vault.

Enumerates every agent-usable capability across both hosts so any provider
(Claude Code, Codex, Hermes profiles, Grok/Gemini adapters) can find and use them:

  Mac  (via `ssh mac`) : Claude Code plugins, MCP servers, local skills
  DGX  (local)         : Hermes skills, MCP servers, profiles, cron jobs

Runs as a hermes no-agent cron job. The page it writes is AUTO-GENERATED and
carries a do-not-edit banner; durable usage guidance lives in hand-written
capability runbooks, which this page indexes rather than duplicates.

Exit codes: 0 ok, 1 write failure. Collector failures degrade to a noted
section rather than aborting the page — a partial catalog beats none.
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

VAULT = "/home/frank/obsidian-fleet-vault"
OUT = os.path.join(VAULT, "System", "Fleet-Capability-Catalog.md")
RUNBOOK_DIRS = [
    ("fleet vault", os.path.join(VAULT, "System", "capability-runbooks")),
]
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "mac"]
errors = []


def run(cmd, timeout=180, stdin=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, input=stdin)
        return r.stdout
    except Exception as exc:                                    # noqa: BLE001
        errors.append(f"{' '.join(cmd[:3])}…: {exc}")
        return ""


def mac_json(snippet):
    """Run a python snippet on the Mac and parse its JSON stdout."""
    out = run(SSH + ["python3", "-"], timeout=120, stdin=snippet)
    try:
        return json.loads(out)
    except Exception as exc:                                    # noqa: BLE001
        errors.append(f"mac collector: {exc}")
        return None


# --------------------------------------------------------------------------
# Mac: Claude Code plugins (installed + enabled state + marketplace blurb)
# --------------------------------------------------------------------------
MAC_PLUGINS = r'''
import json, os
H = os.path.expanduser("~")
inst = json.load(open(H + "/.claude/plugins/installed_plugins.json"))["plugins"]
enabled = json.load(open(H + "/.claude/settings.json")).get("enabledPlugins", {})
try:
    mk = {p["name"]: p for p in json.load(open(
        H + "/.claude/plugins/marketplaces/claude-plugins-official/.claude-plugin/marketplace.json"
    ))["plugins"]}
except Exception:
    mk = {}
rows = []
for pid, entries in inst.items():
    name = pid.split("@")[0]
    for e in entries:
        rows.append({
            "name": name,
            "id": pid,
            "version": e.get("version", "?"),
            "scope": e.get("scope", "?"),
            "enabled": bool(enabled.get(pid, False)),
            "desc": (mk.get(name, {}).get("description") or "")[:120],
        })
print(json.dumps(rows))
'''

MAC_SKILLS = r'''
import json, os
d = os.path.expanduser("~/.claude/skills")
out = []
for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
    p = os.path.join(d, n, "SKILL.md")
    desc = ""
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="replace").read().split("\n")[:20]:
            if line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip('"')[:150]
                break
    out.append({"name": n, "desc": desc})
print(json.dumps(out))
'''


def mac_mcp():
    """`claude mcp list` is the only source that sees claude.ai connectors too."""
    out = run(SSH + ["bash -lc 'claude mcp list 2>/dev/null'"], timeout=240)
    rows = []
    for line in out.split("\n"):
        m = re.match(r"^(.+?):\s+(.*?)\s+-\s+([✔!✘⊘])\s*(.*)$", line.strip())
        if not m:
            continue
        name, transport, glyph, note = m.groups()
        status = {"✔": "connected", "!": "needs auth", "✘": "failed", "⊘": "disabled"}.get(glyph, glyph)
        rows.append({
            "name": name.strip(),
            "transport": transport.strip()[:80],
            "status": status if not note else f"{status}",
            "cloud": name.startswith("claude.ai "),
        })
    if not rows:
        errors.append("mac `claude mcp list` returned no parseable rows")
    return rows


# --------------------------------------------------------------------------
# DGX: Hermes skills / MCPs / profiles / crons
# --------------------------------------------------------------------------
def dgx_skills():
    """Filesystem is authoritative — `hermes skills list` truncates long names."""
    home = os.path.expanduser("~")
    where = defaultdict(set)
    roots = [(os.path.join(home, ".hermes", "skills"), "global")]
    pdir = os.path.join(home, ".hermes", "profiles")
    if os.path.isdir(pdir):
        for prof in os.listdir(pdir):
            roots.append((os.path.join(pdir, prof, "skills"), prof))
    for path, label in roots:
        if not os.path.isdir(path):
            continue
        try:
            for skill in os.listdir(path):
                if not skill.startswith("."):
                    where[skill].add(label)
        except OSError:
            continue
    return where


def dgx_mcp():
    out = run(["hermes", "mcp", "list"], timeout=90)
    rows = []
    for line in out.split("\n"):
        # rich table rows:  name  transport  tools  status
        m = re.match(r"^\s{2}([a-z0-9][\w.-]*)\s{2,}(\S.*?)\s{2,}(\S+)\s{2,}(.+?)\s*$", line)
        if m and m.group(1) not in ("Name",):
            rows.append({
                "name": m.group(1),
                "transport": m.group(2).strip()[:60],
                "status": m.group(4).strip().replace("✓", "").strip(),
            })
    return rows


def dgx_crons():
    out = run(["hermes", "cron", "list"], timeout=120)
    names = re.findall(r"^\s*Name:\s+(\S+)", out, re.M)
    errs = len(re.findall(r"error:", out))
    return names, errs


def find_runbooks():
    found = []
    for label, d in RUNBOOK_DIRS:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".md"):
                    found.append((label, f[:-3]))
    return found


# --------------------------------------------------------------------------
def table(headers, rows):
    if not rows:
        return "_none found_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        cells = [str(c).replace("|", "\\|") for c in r]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main():
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    today = now.strftime("%Y-%m-%d")

    plugins = mac_json(MAC_PLUGINS) or []
    mskills = mac_json(MAC_SKILLS) or []
    mmcp = mac_mcp()
    dskills = dgx_skills()
    dmcp = dgx_mcp()
    profiles = sorted(os.listdir(os.path.expanduser("~/.hermes/profiles"))) \
        if os.path.isdir(os.path.expanduser("~/.hermes/profiles")) else []
    cron_names, cron_errs = dgx_crons()
    runbooks = find_runbooks()

    # de-dup plugins by name, preferring user scope
    seen, prows = {}, []
    for p in sorted(plugins, key=lambda x: (x["name"], x["scope"] != "user")):
        if p["name"] in seen:
            continue
        seen[p["name"]] = 1
        prows.append([p["name"], p["version"], p["scope"],
                      "yes" if p["enabled"] else "**no**", p["desc"]])

    local_mcp = [m for m in mmcp if not m["cloud"]]
    cloud_mcp = [m for m in mmcp if m["cloud"]]
    n_dskills = len(dskills)
    n_profiles = len(profiles)
    connector_note = ("" if cloud_mcp else
        "**None listed here does NOT mean none exist** — claude.ai connectors are "
        "interactively authenticated, so a headless cron run cannot enumerate them. "
        "Run `claude mcp list` in an interactive session on the Mac to see the full set "
        "(~33 at last manual check).")

    doc = f"""---
title: "Fleet Capability Catalog — Claude Code plugins, MCP servers, and capability index"
type: reference
status: active
created: 2026-08-20
updated: {today}
confidence: high
tags:
  - fleet
  - catalog
  - capabilities
  - skills
  - mcp
  - plugins
  - agents
  - auto-generated
sources:
  - "file:/home/frank/.hermes/scripts/fleet_capability_catalog.py"
  - "runtime:ssh mac claude mcp list"
  - "runtime:hermes mcp list, hermes cron list"
  - "file:/home/frank/.hermes/profiles/*/skills"
---
# Fleet Capability Catalog

> **AUTO-GENERATED — do not edit by hand.** Regenerated by
> `~/.hermes/scripts/fleet_capability_catalog.py` (hermes cron `fleet-capability-catalog`).
> Hand edits are overwritten on the next run. Durable *usage* guidance belongs in a
> capability runbook (see §6), which this page indexes.

Generated **{stamp}**.

Every agent-usable capability across the fleet, so any provider — Claude Code, Codex,
Hermes profiles, Grok/Gemini adapters — can find what exists and where it runs.
This page answers *what exists*; the runbooks answer *how to use it*.

| Surface | Count |
|---|---|
| Mac — Claude Code plugins | {len(prows)} |
| Mac — MCP servers (local) | {len(local_mcp)} |
| Mac — MCP servers (claude.ai connectors) | {len(cloud_mcp)} |
| Mac — local skills | {len(mskills)} |
| DGX — Hermes skills (distinct) | {len(dskills)} |
| DGX — Hermes MCP servers | {len(dmcp)} |
| DGX — Hermes profiles | {len(profiles)} |
| DGX — cron jobs | {len(cron_names)} |

## 1. Mac — Claude Code plugins

Installed at user scope unless noted. `enabled=no` means installed but switched off in
`~/.claude/settings.json`, so its skills/agents/MCPs are **not** available to a session.

{table(["plugin", "version", "scope", "enabled", "what it gives you"], prows)}
## 2. Mac — MCP servers

Local servers (stdio/HTTP configured on the machine):

{table(["server", "transport", "status"], [[m["name"], m["transport"], m["status"]] for m in local_mcp])}
claude.ai account connectors (available to any Claude session on this account; `needs auth`
means a one-time OAuth in the client). {connector_note}

{table(["connector", "endpoint", "status"], [[m["name"].replace("claude.ai ", ""), m["transport"], m["status"]] for m in cloud_mcp])}
## 3. Mac — local skills

`~/.claude/skills/` — hand-authored, always available to Claude Code on this Mac.

{table(["skill", "description"], [[s["name"], s["desc"]] for s in mskills])}
## 4. DGX — Hermes skills and profiles → see the existing catalogs

**Not duplicated here.** Hermes skills and profiles already have a canonical generated
catalog, produced by `control-spine/scripts/generate_knowledge_catalogs.py`:

- [[Skills/Skills-Home]] — Hermes skill catalog, plus a page per skill in `Skills/Catalog/`
- [[Agents/Agents-Home]] — Hermes profiles / agents
- [[Skills/Inactive-Skills]] — retired and quarantined skills
- `System/Catalogs/catalog-manifest.yaml` — machine-readable counts and routing

This page deliberately covers only what that generator does **not** see: the Mac
Claude Code surface and MCP servers on both hosts. Current DGX totals for orientation:
{n_dskills} distinct skills installed across {n_profiles} profiles.

## 5. DGX — Hermes MCP servers, profiles, cron

{table(["server", "transport", "status"], [[m["name"], m["transport"], m["status"]] for m in dmcp])}
**Profiles ({len(profiles)}):** each has its own skills, cron, and provider config —
`HERMES_PROFILE=<name> hermes …` targets one.

**Cron jobs:** {len(cron_names)} registered{f", {cron_errs} reporting an error on last run" if cron_errs else ""}.
Inspect with `hermes cron list`. Cron is where most fleet automation actually lives, so a
capability that looks unused may simply be driven by a job rather than a person.

## 6. Capability runbooks (hand-written — how to actually use things)

This catalog is an inventory, not instructions. Where a tool needs judgment, real access
paths, or worked examples, it gets a runbook:

{table(["runbook", "vault"], [[f"[[{n}]]", lbl] for lbl, n in runbooks]) if runbooks
 else "_No capability runbooks yet. Create them under `System/capability-runbooks/` and they appear here automatically._" + chr(10)}
Project-scoped runbooks live in their owning project vault, not here — e.g. the
SycodeTrading monitoring stack is documented in that project's vault under
`operations/runbooks/monitoring-stack-runbook.md`, linked from its MOC.

## 7. How to add a capability so others can find it

1. Install it (`claude plugin install …`, `hermes skills install …`, or an MCP entry).
2. Re-run this catalog — or wait for the nightly cron — so it appears above.
3. If using it needs more than its name, write a runbook in `System/capability-runbooks/`
   and it is indexed in §6 automatically.
4. If it is project-specific, document it in that project's vault and link it from that
   project's MOC instead.
"""
    if errors:
        doc += "\n## Collector warnings\n\n" + "\n".join(f"- {e}" for e in errors) + "\n"

    try:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write(doc)
    except OSError as exc:
        print(f"FAILED to write {OUT}: {exc}", file=sys.stderr)
        return 1

    print(f"Fleet Capability Catalog written: {OUT}")
    print(f"  mac: {len(prows)} plugins, {len(local_mcp)} local MCP, {len(cloud_mcp)} connectors, {len(mskills)} skills")
    print(f"  dgx: {len(dskills)} skills, {len(dmcp)} MCP, {len(profiles)} profiles, {len(cron_names)} crons")
    if errors:
        print(f"  warnings: {len(errors)} — see page footer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
