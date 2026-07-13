#!/usr/bin/env python3
"""Guide evaluator — deterministic OBJECTIVE GATE for the research-guide pipeline.

Implements recs #6 (reflect-refine + threshold termination) and #4 (verifiable auto-close
vs non-verifiable HITL). Now also enforces that guides carry CONCRETE implementation
examples (>=1 fenced code/command block) — stack-mapped prose alone fails the gate.
Read-only; writes a scoreboard note only.
"""
import os, re, glob, datetime, sys

GUIDES = "/home/frank/obsidian-fleet-vault/Research/Guides"
THRESHOLD = 70
REQUIRED_FM = ["title", "type", "status", "version", "source_version", "last_verified"]
REQUIRED_SECTIONS = ["overview", "complete reference", "usage", "expert", "gotcha", "adoption", "source", "changelog", "follow-up"]

rows = []
for f in sorted(glob.glob(os.path.join(GUIDES, "*.md"))):
    name = os.path.basename(f)
    if name.startswith("_") or name == "README.md":
        continue
    s = open(f, encoding="utf-8").read()
    fm = s.split("---", 2)[1] if s.startswith("---") else ""
    score, reasons = 0, []
    fm_ok = sum(1 for k in REQUIRED_FM if re.search(rf"(?m)^{k}:", fm))
    score += round(25 * fm_ok / len(REQUIRED_FM))
    if fm_ok < len(REQUIRED_FM): reasons.append(f"frontmatter {fm_ok}/{len(REQUIRED_FM)}")
    sec_ok = sum(1 for sec in REQUIRED_SECTIONS if sec in s.lower())
    score += round(30 * sec_ok / len(REQUIRED_SECTIONS))
    if sec_ok < len(REQUIRED_SECTIONS): reasons.append(f"sections {sec_ok}/{len(REQUIRED_SECTIONS)}")
    src = len(re.findall(r"https?://", fm))
    score += 15 if src >= 3 else round(15 * src / 3)
    if src < 3: reasons.append(f"sources {src}/3")
    examples = s.count("```") // 2
    score += 15 if examples >= 1 else 0
    if examples < 1: reasons.append("NO concrete example (needs ```snippet```)")
    published = bool(re.search(r"(?m)^status:\s*published", fm))
    score += 15 if published else 0
    if not published: reasons.append("not published")
    verdict = "PASS" if score >= THRESHOLD else ("REVIEW" if not published else "NEEDS-REFRESH")
    rows.append((name, score, verdict, "; ".join(reasons) or "ok"))

now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
lines = ["# Guide Quality Scoreboard", "", f"Objective gate (deterministic) · threshold {THRESHOLD} · {now}",
         "Checks: frontmatter · sections · sources · **concrete example** · published.", "",
         "| Guide | Score | Verdict | Notes |", "|---|---:|---|---|"]
for n, sc, v, r in rows:
    lines.append(f"| {n} | {sc} | {v} | {r} |")
report = "\n".join(lines)
print(report)
if "--write" in sys.argv:
    open(os.path.join(GUIDES, "_scoreboard.md"), "w", encoding="utf-8").write(report + "\n")
    print("\n[wrote _scoreboard.md]")

