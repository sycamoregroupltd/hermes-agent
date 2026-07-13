#!/usr/bin/env python3
"""Audit, distill, and safely quarantine stale fleet report artifacts.

Default mode is read-only.  The retention loop is intentionally two-phase:

1. scan + distill evidence into compact durable artifacts;
2. only then, with ``--apply`` and a reviewed candidate manifest, move stale raw
   files into a recoverable quarantine (never irreversible delete).

This script deliberately preserves canonical source-of-truth reports,
approvals/delegated-authority files, compact REFLECTION.md state, and anything
that looks like secrets/credentials or live runtime state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

ROOTS = [
    Path("/home/frank/uaa-rules"),
    Path("/home/frank/.hermes/cron/output"),
    Path("/home/frank/jarvis/workspace/memory/artifacts"),
]
PROFILE_ROOT = Path("/home/frank/.hermes/profiles")
OUT_DIR = Path("/home/frank/uaa-rules/report-retention")
QUARANTINE_ROOT = Path("/home/frank/.trash/fleet-report-retention")
NOW = time.time()
DAY = 86400
MAX_TEXT_SAMPLE = 12_000

CANONICAL_KEEP_NAMES = {
    "FLEET-STATUS.md",
    "FLEET-REFLECTION-REPORT.md",
    "SELF-IMPROVEMENT-LOOP.md",
    "PENDING-FRANK-TRIAGE.md",
    "approvals-registry.md",
    "delegated-authority.md",
    "behaviour.md",
    "behaviour-rules-6-10-summary.md",
    "latest.md",
    "latest.json",
    "retention-manifest.json",
    "distilled.md",
    "cron-hook.json",
}

SENSITIVE_MARKERS = (
    "/.ssh/",
    "/secrets/",
    "/credentials/",
    "/.env",
    "credential",
    "secret",
    "token",
    "api_key",
    "auth_token",
)


@dataclass
class FileRec:
    path: str
    size: int
    age_days: float
    kind: str
    action: str
    reason: str
    integration_status: str
    evidence_artifact: str | None = None
    quarantine_path: str | None = None


def human_size(n: int) -> str:
    units = ["B", "K", "M", "G", "T"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            return f"{x:.1f}{u}"
        x /= 1024
    return f"{x:.1f}T"


def is_sensitive_path(path: Path) -> bool:
    lowered = str(path).lower()
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def iter_roots(include_reflections: bool = True) -> Iterable[Path]:
    yield from ROOTS
    if include_reflections:
        # REFLECTION.md files are compact state and should be scanned/protected,
        # not bulk-cleaned.  Yield the profile root so the audit proves coverage.
        yield PROFILE_ROOT


def kind_for(path: Path) -> str:
    s = str(path)
    name = path.name.lower()
    if name == "reflection.md":
        return "reflection-state"
    if "/cron/output/" in s:
        return "cron-output"
    if name.endswith((".tar.gz", ".tgz", ".zip")):
        return "bulk-archive"
    if name.endswith((".json", ".jsonl")):
        return "structured-evidence"
    if name.endswith((".md", ".txt")):
        return "report"
    return "other"


def classify(path: Path, size: int, age_days: float, kind: str) -> tuple[str, str, str]:
    if is_sensitive_path(path):
        return "keep", "sensitive-path-never-touched", "preserved"
    if path.name in CANONICAL_KEEP_NAMES:
        return "keep", "canonical-live-report", "preserved"
    if kind == "reflection-state":
        return "keep", "compact-profile-reflection-state", "preserved"
    if "/report-retention/" in str(path):
        return "keep", "retention-source-of-truth", "preserved"
    if kind == "bulk-archive" and size > 50_000_000 and age_days >= 1:
        return (
            "review-trash-after-manifest",
            "large retirement/archive artifact; preserve manifest then quarantine if no active task references it",
            "pending-distill",
        )
    if kind == "cron-output" and age_days > 14:
        return (
            "trash-after-summary",
            "cron output older than 14d; keep aggregated job status not raw run",
            "pending-distill",
        )
    if kind == "cron-output" and size > 75_000 and age_days > 2:
        return "summarize-then-trash", "large noisy cron output older than 2d", "pending-distill"
    if kind in {"report", "structured-evidence"} and age_days > 30 and size > 100_000:
        return (
            "summarize-then-archive-or-trash",
            "old large report; distill signal before retention action",
            "pending-distill",
        )
    return "keep", "within retention window or small", "preserved"


def scan() -> list[FileRec]:
    out: list[FileRec] = []
    skip_dirs = {".git", "node_modules", "__pycache__", "home", "lsp"}
    for root in iter_roots():
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in skip_dirs
                and not (root == PROFILE_ROOT and Path(dirpath) != PROFILE_ROOT and d not in set())
            ]
            if root == PROFILE_ROOT:
                # Keep profile scan bounded: profile/*/REFLECTION.md only.
                depth = len(Path(dirpath).relative_to(root).parts)
                if depth >= 1:
                    dirnames[:] = []
                    filenames = [fn for fn in filenames if fn == "REFLECTION.md"]
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                age_days = (NOW - st.st_mtime) / DAY
                kind = kind_for(p)
                action, reason, integration_status = classify(p, st.st_size, age_days, kind)
                out.append(
                    FileRec(
                        str(p),
                        st.st_size,
                        round(age_days, 2),
                        kind,
                        action,
                        reason,
                        integration_status,
                    )
                )
    return out


def text_sample(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"read_error: {exc}"
    if len(raw) <= MAX_TEXT_SAMPLE:
        sample = raw
    else:
        half = MAX_TEXT_SAMPLE // 2
        sample = raw[:half] + b"\n\n--- snip middle ---\n\n" + raw[-half:]
    return sample.decode("utf-8", errors="replace")


def archive_summary(path: Path) -> str:
    try:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
                names = [i.filename for i in infos[:40]]
                return f"zip entries={len(infos)} sample={names}"
        with tarfile.open(path) as tf:
            members = tf.getmembers()
            names = [m.name for m in members[:40]]
            return f"tar entries={len(members)} sample={names}"
    except Exception as exc:  # noqa: BLE001 - audit should not abort on one archive.
        return f"archive_summary_error: {type(exc).__name__}: {exc}"


def distilled_section(rec: FileRec) -> str:
    path = Path(rec.path)
    digest = hashlib.sha256(rec.path.encode()).hexdigest()[:12]
    header = [
        f"### {path.name}",
        f"- path: {rec.path}",
        f"- size: {rec.size} ({human_size(rec.size)})",
        f"- age_days: {rec.age_days}",
        f"- action: {rec.action}",
        f"- reason: {rec.reason}",
        f"- integration_status: distilled:{digest}",
        "",
    ]
    if rec.kind == "bulk-archive":
        body = archive_summary(path)
    else:
        body = text_sample(path)
    return "\n".join(header) + "```text\n" + body[:MAX_TEXT_SAMPLE] + "\n```\n"


def write_outputs(rows: Sequence[FileRec]) -> tuple[Path, Path, Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = [r for r in rows if r.action != "keep"]
    candidates.sort(key=lambda r: r.size, reverse=True)
    distilled_lines = [
        "# Fleet report retention distilled evidence",
        "",
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(NOW))}",
        "",
        "Policy: integrate/distill first; only reviewed batches may move raw files to recoverable quarantine.",
        "",
    ]
    for rec in candidates[:80]:
        if rec.integration_status != "distilled-and-quarantined":
            rec.integration_status = "distilled"
        rec.evidence_artifact = str(OUT_DIR / "distilled.md")
        distilled_lines.append(distilled_section(rec))

    by_action: dict[str, dict[str, int]] = {}
    by_kind: dict[str, dict[str, int]] = {}
    for r in rows:
        slot = by_action.setdefault(r.action, {"count": 0, "bytes": 0})
        slot["count"] += 1
        slot["bytes"] += r.size
        kslot = by_kind.setdefault(r.kind, {"count": 0, "bytes": 0})
        kslot["count"] += 1
        kslot["bytes"] += r.size

    payload = {
        "generated_at_epoch": NOW,
        "roots": [str(p) for p in iter_roots()],
        "file_count": len(rows),
        "total_bytes": sum(r.size for r in rows),
        "by_action": by_action,
        "by_kind": by_kind,
        "candidates": [asdict(r) for r in candidates[:500]],
    }
    latest_json = OUT_DIR / "latest.json"
    latest_md = OUT_DIR / "latest.md"
    manifest_json = OUT_DIR / "retention-manifest.json"
    distilled_md = OUT_DIR / "distilled.md"
    cron_hook = OUT_DIR / "cron-hook.json"
    latest_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    manifest_json.write_text(json.dumps({"candidates": payload["candidates"]}, indent=2, sort_keys=True) + "\n")
    distilled_md.write_text("\n".join(distilled_lines) + "\n")
    cron_hook.write_text(
        json.dumps(
            {
                "name": "fleet-report-retention-sweep",
                "profile": None,
                "schedule": "0 6 * * *",
                "script": "fleet_report_retention_quiet.sh",
                "no_agent": True,
                "deliver": "local",
                "activation": "review-required before adding to /home/frank/.hermes/cron/jobs.json",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    lines = [
        "# Fleet report retention audit",
        "",
        f"- generated_at_epoch: {NOW:.0f}",
        f"- scanned_files: {len(rows)}",
        f"- scanned_bytes: {human_size(payload['total_bytes'])}",
        f"- manifest: {manifest_json}",
        f"- distilled_evidence: {distilled_md}",
        f"- cron_hook_spec: {cron_hook}",
        "",
        "## Action summary",
    ]
    for action, meta in sorted(by_action.items()):
        lines.append(f"- {action}: {meta['count']} files / {human_size(meta['bytes'])}")
    lines += ["", "## Top cleanup candidates", ""]
    for r in candidates[:30]:
        lines.append(
            f"- {human_size(r.size)} | age={r.age_days}d | {r.action} | {r.integration_status} | {r.path} | {r.reason}"
        )
    lines += [
        "",
        "Policy: integrate/distill first, then trash stale raw bulk into recoverable quarantine; this default audit is read-only.",
    ]
    latest_md.write_text("\n".join(lines) + "\n")
    return latest_json, latest_md, manifest_json, distilled_md


def quarantine_target(path: Path, batch_dir: Path) -> Path:
    rel_hash = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    safe_name = str(path).lstrip("/").replace("/", "__")
    return batch_dir / f"{rel_hash}__{safe_name}"


def apply_quarantine(rows: Sequence[FileRec], limit: int, include_review_trash: bool) -> list[FileRec]:
    eligible_actions = {"summarize-then-trash", "trash-after-summary"}
    if include_review_trash:
        eligible_actions.add("review-trash-after-manifest")
    candidates = [
        r
        for r in rows
        if r.action in eligible_actions
        and r.integration_status == "distilled"
        and not is_sensitive_path(Path(r.path))
    ]
    candidates.sort(key=lambda r: (r.age_days, r.size), reverse=True)
    batch = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(NOW))
    batch_dir = QUARANTINE_ROOT / batch
    moved: list[FileRec] = []
    for rec in candidates[:limit]:
        src = Path(rec.path)
        if not src.exists():
            continue
        batch_dir.mkdir(parents=True, exist_ok=True)
        dst = quarantine_target(src, batch_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        rec.quarantine_path = str(dst)
        rec.integration_status = "distilled-and-quarantined"
        moved.append(rec)
    if moved:
        (batch_dir / "MOVE-MANIFEST.json").write_text(
            json.dumps([asdict(r) for r in moved], indent=2, sort_keys=True) + "\n"
        )
    return moved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="move eligible distilled candidates to recoverable quarantine")
    parser.add_argument("--limit", type=int, default=0, help="max files to quarantine when --apply is set; default 0 means dry-run")
    parser.add_argument(
        "--include-review-trash",
        action="store_true",
        help="allow manifest-reviewed large archive candidates to be quarantined; default only handles summarized cron output",
    )
    parser.add_argument("--quiet", action="store_true", help="only print material cleanup/risk; still writes latest artifacts")
    args = parser.parse_args(argv)

    rows = scan()
    latest_json, latest_md, manifest_json, distilled_md = write_outputs(rows)
    moved: list[FileRec] = []
    if args.apply and args.limit > 0:
        moved = apply_quarantine(rows, args.limit, args.include_review_trash)
        # Re-write outputs with quarantine paths/status after the move.
        latest_json, latest_md, manifest_json, distilled_md = write_outputs(rows)

    candidates = [r for r in rows if r.action != "keep"]
    if args.quiet and not candidates and not moved:
        print("[SILENT]")
        return 0
    print(
        "RETENTION_AUDIT_PASS "
        f"files={len(rows)} candidates={len(candidates)} moved={len(moved)} "
        f"latest={latest_md} manifest={manifest_json} distilled={distilled_md}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())