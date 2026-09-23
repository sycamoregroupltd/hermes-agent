#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Standing no-black-holes detector (t_635a3c9b).

Weekly no-agent cron audit for report-like producers that write to local/origin
or file sinks without a documented consumer. Empty stdout means clean/silent.
When findings exist, emits a capped Discord-ready report by default and creates
one idempotency-keyed triage card on the jarvis-os board. Reviewers can request
full evidence explicitly via --json or --max-findings without changing default
Discord noise.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

HOME = Path("/home/frank")
HERMES_HOME = Path(os.environ.get("HERMES_REAL_HOME") or os.environ.get("HERMES_HOME") or (HOME / ".hermes")).expanduser()
if not (HERMES_HOME / "profiles").exists() and (HERMES_HOME / ".hermes" / "profiles").exists():
    HERMES_HOME = HERMES_HOME / ".hermes"
PROFILES_DIR = HERMES_HOME / "profiles"
STATE_DIR = HERMES_HOME / "state"
ALLOWLIST_PATH = STATE_DIR / "no_black_holes_allowlist.json"
STATE_PATH = STATE_DIR / "no_black_holes_state.json"
BOARD = os.environ.get("NO_BLACK_HOLES_BOARD", "jarvis-os")
IDEMPOTENCY_KEY = "no-black-holes-weekly-findings"
DEFAULT_MAX_FINDINGS_PER_SECTION = int(os.environ.get("NO_BLACK_HOLES_MAX_FINDINGS", "30"))
REPORT_RE = re.compile(r"report|summary|findings|alert", re.I)

KNOWN_OUTPUT_DIRS = [
    HOME / "sycode-trading" / "reports",
    HOME / "obsidian" / "quant-team" / "analytics",
    HOME / "obsidian" / "quant-team" / "reports",
    HOME / "obsidian" / "quant-team" / "strategy-performance",
    HOME / "obsidian" / "sycode-trading" / "analytics",
    HOME / "obsidian" / "sycode-trading" / "reports",
    HOME / "obsidian" / "sycode-trading" / "strategy-performance",
    HOME / "obsidian-fleet-vault" / "Research" / "Reviews",
    HERMES_HOME / "var",
]
REFERENCE_ROOTS = [
    HERMES_HOME / "scripts",
    PROFILES_DIR,
    HOME / "obsidian-fleet-vault",
    HOME / "obsidian" / "quant-team",
    HOME / "obsidian" / "sycode-trading",
]
VAULTS = [HOME / "obsidian-fleet-vault", HOME / "obsidian" / "quant-team", HOME / "obsidian" / "sycode-trading"]
SKIP_DIRS = {".git", ".obsidian", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", "venv", ".venv"}
TEXT_EXTS = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".toml", ".csv", ".log"}

DEFAULT_ALLOWLIST: dict[str, Any] = {
    "version": 1,
    "notes": "Entries are explicit documented consumers/legitimate local sinks for t_635a3c9b no-black-holes detector.",
    "cron_jobs": {
        # Known local mutator/maintenance jobs: they do not produce human report artifacts.
        "jarvis:dgx-skill-curator-loop": "local maintenance mutator; no report-like human output expected",
        "jarvis:dgx-self-improvement-loop": "self-improvement collector consumed by governance loop/state, not a report channel",
        "jarvis:native-curator-backup": "backup/curator maintenance, local artifact by design",
        "jarvis:native-hermes-backup": "backup maintenance, local artifact by design",
        "jarvis:paper-trader-agent": "paper-trader internal loop; local agent state, not report sink",
    },
    "file_paths": {},
    "vault_notes": {},
}

@dataclass
class Finding:
    section: str
    key: str
    severity: str
    summary: str
    detail: str


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except Exception as exc:
        return {"_error": f"failed to parse {path}: {exc}", **(default if isinstance(default, dict) else {})}


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp_name, path)


def ensure_allowlist(*, create: bool = True) -> dict[str, Any]:
    if create and not ALLOWLIST_PATH.exists():
        atomic_write_json(ALLOWLIST_PATH, DEFAULT_ALLOWLIST)
    allow = read_json(ALLOWLIST_PATH, DEFAULT_ALLOWLIST)
    for k, v in DEFAULT_ALLOWLIST.items():
        allow.setdefault(k, v if not isinstance(v, dict) else {})
    return allow


def walk_files(root: Path, *, exts: set[str] | None = None, max_files: int | None = None) -> Iterable[Path]:
    if not root.exists():
        return
    count = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            if cur.is_dir():
                if cur.name in SKIP_DIRS:
                    continue
                for child in cur.iterdir():
                    stack.append(child)
            elif cur.is_file():
                if exts is None or cur.suffix.lower() in exts:
                    yield cur
                    count += 1
                    if max_files and count >= max_files:
                        return
        except (OSError, PermissionError):
            continue


def text_contains_reference(path: Path, needles: list[str]) -> bool:
    try:
        txt = path.read_text(errors="ignore")
    except Exception:
        return False
    return any(n and n in txt for n in needles)


def build_reference_blob(max_bytes: int = 60_000_000) -> str:
    """Build one bounded corpus for cheap inbound-reference checks."""
    chunks: list[str] = []
    used = 0
    for root in REFERENCE_ROOTS:
        if not root.exists():
            continue
        for p in walk_files(root, exts=TEXT_EXTS, max_files=18000) or []:
            try:
                txt = p.read_text(errors="ignore")
            except Exception:
                continue
            chunks.append(txt)
            used += len(txt)
            if used >= max_bytes:
                return "\n".join(chunks)
    return "\n".join(chunks)


def has_inbound_reference(target: Path, reference_blob: str) -> bool:
    needles = [str(target), target.name, target.stem]
    return any(n and n in reference_blob for n in needles)


def iter_cron_stores() -> Iterable[tuple[str, Path, dict[str, Any]]]:
    paths = []
    if (HERMES_HOME / "cron" / "jobs.json").exists():
        paths.append(("global", HERMES_HOME / "cron" / "jobs.json"))
    seen = set()
    if PROFILES_DIR.exists():
        for p in sorted(PROFILES_DIR.glob("*/cron/jobs.json")):
            rp = os.path.realpath(p)
            if rp in seen:
                continue  # symlink alias — dedupe (sycode-trading -> sycode-trading-pm)
            seen.add(rp)
            rpath = Path(rp)
            # Key by the RESOLVED (real) profile dir name so an alias shares one profile identity.
            paths.append((rpath.parents[1].name, rpath))
    for profile, path in paths:
        data = read_json(path, {"jobs": []})
        for job in data.get("jobs", []) if isinstance(data, dict) else []:
            if isinstance(job, dict):
                yield profile, path, job


def job_key(profile: str, job: dict[str, Any]) -> str:
    return f"{profile}:{job.get('name') or job.get('id') or '<unnamed>'}"


def job_inventory_key(profile: str, job: dict[str, Any]) -> str:
    material = "|".join(str(job.get(k) or "") for k in ["id", "name", "script", "deliver"])
    return f"cron:{profile}:{hashlib.sha1(material.encode()).hexdigest()[:12]}"


def is_report_like(job: dict[str, Any]) -> bool:
    text = "\n".join(str(job.get(k) or "") for k in ["name", "prompt", "script", "last_error"])
    if REPORT_RE.search(text):
        return True
    # Heuristic for script producers even when prompt is empty.
    script = str(job.get("script") or "")
    if script:
        for base in [HERMES_HOME / "scripts", HERMES_HOME / "profiles" / "jarvis" / "scripts"]:
            sp = base / script
            if sp.exists():
                try:
                    if REPORT_RE.search(sp.read_text(errors="ignore")[:20000]):
                        return True
                except Exception:
                    pass
    return False


def audit_cron_outputs(allow: dict[str, Any]) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    inventory = {}
    allowed = allow.get("cron_jobs", {})
    for profile, path, job in iter_cron_stores():
        if not job.get("enabled", True):
            continue
        deliver = str(job.get("deliver") or "local")
        key = job_key(profile, job)
        inv_key = job_inventory_key(profile, job)
        inventory[inv_key] = {"profile": profile, "name": job.get("name"), "id": job.get("id"), "deliver": deliver, "script": job.get("script")}
        if deliver not in {"local", "origin", ""}:
            continue
        if key in allowed or str(job.get("id") or "") in allowed:
            continue
        if is_report_like(job):
            findings.append(Finding(
                "cron-output", key, "high",
                f"report-like enabled cron job uses deliver={deliver}",
                f"store={path}; id={job.get('id')}; script={job.get('script')}; add documented consumer to {ALLOWLIST_PATH} or retarget delivery",
            ))
    return findings, inventory


def audit_file_sinks(allow: dict[str, Any], reference_blob: str) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    inventory = {}
    cutoff = now_utc() - timedelta(days=7)
    allowed = allow.get("file_paths", {})
    for root in KNOWN_OUTPUT_DIRS:
        if not root.exists():
            continue
        for p in walk_files(root, exts=None, max_files=8000) or []:
            try:
                if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) < cutoff:
                    continue
            except Exception:
                continue
            # Skip files inside vault directories — they're governed by vault_orphan rules
            if any(str(p).startswith(str(v)) for v in VAULTS):
                continue
            key = str(p)
            inventory[f"file:{hashlib.sha1(key.encode()).hexdigest()[:12]}"] = {"path": key, "mtime": p.stat().st_mtime}
            # Check exact match first, then fnmatch against glob patterns
            if key in allowed or p.name in allowed:
                continue
            if any(fnmatch.fnmatch(key, pattern) for pattern in allowed):
                continue
            if not has_inbound_reference(p, reference_blob):
                findings.append(Finding(
                    "file-sink", key, "medium",
                    "recent output file has zero inbound references",
                    f"path={p}; modified_last_7d; add a consumer/index link or allowlist if intentionally local",
                ))
    return findings, inventory


def is_agent_authored_note(path: Path, txt: str) -> bool:
    head = txt[:1200].lower()
    markers = ["agent", "hermes", "kanban", "created_by:", "author: jarvis", "task:", "t_"]
    return any(m in head for m in markers)


def has_consumer_frontmatter(txt: str) -> bool:
    """Check YAML frontmatter for consumer-related keys that document intentional retention.

    Recognized keys (case-insensitive):
    - ``consumers:`` — list or string of named consumers.
    - ``consumer:`` — singular string.
    - ``report_consumer:`` — explicit report consumer declaration.
    - ``producer_consumer:`` — explicit producer-to-consumer mapping.
    - ``consumer_index:`` — path to a consumer index that documents this note's recipients.

    Also recognizes the registry path ``Operations/CONSUMER-REGISTRY`` in the notes
    ``sources:`` list as evidence that a note is intentionally tracked in the consumer registry.
    """
    if not txt or not txt.startswith("---"):
        return False
    # Extract YAML frontmatter (between first two --- markers)
    end_idx = txt.find("---", 3)
    if end_idx == -1:
        return False
    fm = txt[3:end_idx].strip().lower()
    # Check for consumer-related keys
    consumer_keys = ["consumers:", "consumer:", "report_consumer:", "producer_consumer:", "consumer_index:"]
    for key in consumer_keys:
        if key in fm:
            return True
    # Check sources for CONSUMER-REGISTRY reference
    if "consumer-registry" in fm or "CONSUMER-REGISTRY" in fm:
        return True
    return False


def audit_vault_orphans(allow: dict[str, Any]) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    inventory = {}
    cutoff = now_utc() - timedelta(hours=48)
    all_md: list[Path] = []
    note_texts: dict[Path, str] = {}
    inbound_counts: dict[str, int] = {}
    link_re = re.compile(r"\[\[([^\]#|]+)")
    for vault in VAULTS:
        if vault.exists():
            all_md.extend(list(walk_files(vault, exts={".md"}, max_files=20000) or []))
    for p in all_md:
        try:
            txt = p.read_text(errors="ignore")
        except Exception:
            continue
        note_texts[p] = txt
        for target in link_re.findall(txt):
            target = target.strip()
            if target:
                inbound_counts[target] = inbound_counts.get(target, 0) + 1
    allowed = allow.get("vault_notes", {})
    for p in all_md:
        try:
            st = p.stat()
            if datetime.fromtimestamp(st.st_mtime, tz=timezone.utc) > cutoff:
                continue
            txt = note_texts.get(p, "")
        except Exception:
            continue
        if not is_agent_authored_note(p, txt):
            continue
        key = str(p)
        inventory[f"vault:{hashlib.sha1(key.encode()).hexdigest()[:12]}"] = {"path": key}
        if key in allowed or p.stem in allowed:
            continue
        if any(fnmatch.fnmatch(key, pattern) for pattern in allowed):
            continue
        if inbound_counts.get(p.stem, 0) <= 0 and not has_consumer_frontmatter(txt):
            findings.append(Finding(
                "vault-orphan", key, "low",
                "agent-authored vault note older than 48h has zero inbound wikilinks and no consumer frontmatter",
                f"path={p}; link it from a MOC/index/log, add YAML `consumers:` frontmatter, or allowlist if intentionally standalone",
            ))
    return findings, inventory


def audit_new_producers(current_inventory: dict[str, Any], old_state: dict[str, Any], allow: dict[str, Any]) -> list[Finding]:
    old = old_state.get("producer_inventory", {}) if isinstance(old_state, dict) else {}
    if not old:
        return []
    findings = []
    allowed_keys = set(allow.get("producer_keys", []))
    cron_allowed = allow.get("cron_jobs", {})
    file_allowed = allow.get("file_paths", {})
    vault_allowed = allow.get("vault_notes", {})
    for key, meta in current_inventory.items():
        if key in old or key in allowed_keys:
            continue
        # Check if this producer is already covered by another allowlist section
        if key.startswith("cron:"):
            # Directly delivered cron producers already have an explicit consumer channel;
            # no-black-holes is about local/origin/scratch outputs without named consumers.
            # Ticking/liveness of those jobs is covered by mechanism-liveness/cron-health.
            deliver = str(meta.get("deliver") or "local")
            if deliver not in {"local", "origin", ""}:
                continue
            # Check if this profile/job name is documented in the cron_jobs allowlist.
            name = meta.get("name", "")
            profile = meta.get("profile", "")
            if f"{profile}:{name}" in cron_allowed:
                continue
        elif key.startswith("file:"):
            path = meta.get("path", "")
            if any(fnmatch.fnmatch(path, pattern) for pattern in file_allowed):
                continue
        elif key.startswith("vault:"):
            path = meta.get("path", "")
            if any(fnmatch.fnmatch(path, pattern) for pattern in vault_allowed):
                continue
        findings.append(Finding(
            "new-producer", key, "medium",
            "new producer appeared since prior scan and needs consumer check",
            json.dumps(meta, sort_keys=True)[:500],
        ))
    return findings


def create_triage_card(findings: list[Finding], dry_run: bool) -> str | None:
    if dry_run or not findings:
        return None
    body_lines = [
        "Standing no-black-holes detector found output producers/sinks without documented consumers.",
        f"Source task: t_635a3c9b. Findings: {len(findings)}. Allowlist: {ALLOWLIST_PATH}. State: {STATE_PATH}.",
        "Acceptance rule: add a consumer/index, retarget delivery, or justify in allowlist; keep detector quiet-when-clean.",
        "",
    ]
    for f in findings[:40]:
        body_lines.append(f"- [{f.section}/{f.severity}] {f.key}: {f.summary} — {f.detail[:500]}")
    cmd = [
        "hermes", "kanban", "--board", BOARD, "create",
        "TRIAGE: no-black-holes detector findings",
        "--assignee", "jarvis-os-pm",
        "--triage",
        "--priority", "70",
        "--idempotency-key", IDEMPOTENCY_KEY,
        "--created-by", "no-black-holes-detector",
        "--body", "\n".join(body_lines),
        "--json",
    ]
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=45, check=False, env={**os.environ, "HERMES_HOME": str(HERMES_HOME), "HERMES_PROFILE": "jarvis"})
        if cp.returncode == 0:
            try:
                return json.loads(cp.stdout).get("id") or json.loads(cp.stdout).get("task_id") or cp.stdout.strip()
            except Exception:
                return cp.stdout.strip()[:200]
        return f"ERROR creating triage card rc={cp.returncode}: {cp.stderr.strip()[:300] or cp.stdout.strip()[:300]}"
    except Exception as exc:
        return f"ERROR creating triage card: {exc}"


def findings_by_section(findings: list[Finding]) -> dict[str, list[Finding]]:
    by_section: dict[str, list[Finding]] = {}
    for f in findings:
        by_section.setdefault(f.section, []).append(f)
    return by_section


def format_report(findings: list[Finding], triage: str | None, mode: str, *, max_findings: int | None) -> str:
    if not findings:
        return ""
    by_section = findings_by_section(findings)
    lines = [
        f"🔴 NO-BLACK-HOLES DETECTOR: {len(findings)} finding(s) ({mode})",
        f"allowlist={ALLOWLIST_PATH}",
        f"state={STATE_PATH}",
    ]
    if triage:
        lines.append(f"triage_card={triage}")
    for section, items in sorted(by_section.items()):
        lines.append(f"\n## {section} ({len(items)})")
        visible_items = items if max_findings is None else items[:max_findings]
        for f in visible_items:
            lines.append(f"- {f.severity.upper()} {f.key}: {f.summary}; {f.detail}")
        if max_findings is not None and len(items) > max_findings:
            lines.append(f"- … {len(items) - max_findings} more")
    return "\n".join(lines)


def format_json(findings: list[Finding], triage: str | None, mode: str, *, dry_run: bool, state_written: bool) -> str:
    by_section = findings_by_section(findings)
    payload = {
        "mode": mode,
        "dry_run": dry_run,
        "state_written": state_written,
        "triage_card": triage,
        "allowlist": str(ALLOWLIST_PATH),
        "state": str(STATE_PATH),
        "counts": {
            "total": len(findings),
            "by_section": {section: len(items) for section, items in sorted(by_section.items())},
        },
        "findings": [asdict(f) for f in findings],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def fixture_run() -> tuple[list[Finding], dict[str, Any]]:
    findings = [
        *[
            Finding("cron-output", f"fixture:local-weekly-summary-{i:02d}", "high", "report-like enabled cron job uses deliver=local", "pre-fix fixture reproduces local summary black hole")
            for i in range(35)
        ],
        Finding("file-sink", "/fixture/reports/unlinked-review.md", "medium", "recent output file has zero inbound references", "pre-fix fixture reproduces orphan report file"),
        Finding("vault-orphan", "/fixture/vault/Agent Finding.md", "low", "agent-authored vault note older than 48h has zero inbound wikilinks", "pre-fix fixture reproduces orphan agent note"),
        Finding("new-producer", "fixture:new-producer", "medium", "new producer appeared since prior scan and needs consumer check", "pre-fix fixture reproduces new producer guard"),
    ]
    return findings, {"fixture": True, "producer_inventory": {"fixture:new-producer": {"consumer": None}}}


def run(args: argparse.Namespace) -> int:
    max_findings = None if args.max_findings < 0 else args.max_findings
    allow = ensure_allowlist(create=not args.dry_run and not args.fixture)
    old_state = read_json(STATE_PATH, {})
    if args.fixture:
        findings, new_state = fixture_run()
        triage = create_triage_card(findings, dry_run=True)
        if args.json:
            print(format_json(findings, triage, "fixture-dry-run", dry_run=True, state_written=False))
        else:
            print(format_report(findings, triage, "fixture-dry-run", max_findings=max_findings))
        return 0 if len(findings) >= 3 else 2

    reference_blob = build_reference_blob()
    cron_findings, cron_inv = audit_cron_outputs(allow)
    file_findings, file_inv = audit_file_sinks(allow, reference_blob)
    vault_findings, vault_inv = audit_vault_orphans(allow)
    producer_inventory = {**cron_inv, **file_inv, **vault_inv}
    new_findings = audit_new_producers(producer_inventory, old_state, allow)
    findings = cron_findings + file_findings + vault_findings + new_findings

    new_state = {
        "updated_at": now_utc().isoformat(),
        "producer_inventory": producer_inventory,
        "last_counts": {
            "cron_output": len(cron_findings),
            "file_sink": len(file_findings),
            "vault_orphan": len(vault_findings),
            "new_producer": len(new_findings),
            "total": len(findings),
        },
    }
    if not args.dry_run:
        atomic_write_json(STATE_PATH, new_state)
    triage = create_triage_card(findings, dry_run=args.dry_run)
    mode = "dry-run" if args.dry_run else "live"
    report = format_json(findings, triage, mode, dry_run=args.dry_run, state_written=not args.dry_run) if args.json else format_report(findings, triage, mode, max_findings=max_findings)
    if report:
        print(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="scan and report without writing state or creating kanban card")
    ap.add_argument("--fixture", action="store_true", help="run built-in pre-fix fixture; must produce >=3 findings")
    ap.add_argument("--json", action="store_true", help="emit machine-readable full findings; never truncates findings")
    ap.add_argument("--max-findings", type=int, default=DEFAULT_MAX_FINDINGS_PER_SECTION, metavar="N", help="max findings per human report section; use -1 for all (default: env NO_BLACK_HOLES_MAX_FINDINGS or 30)")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(f"🔴 NO-BLACK-HOLES DETECTOR ERROR: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
