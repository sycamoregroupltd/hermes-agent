#!/usr/bin/env python3
"""Run Hermes security audit and route high/critical CVE findings to kanban.

Empty stdout means no high/critical findings. Set SECURITY_AUDIT_FIXTURE to a
file path for deterministic parser/routing tests.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERMES = "/home/frank/.local/bin/hermes"
BOARD = "jarvis-os"
ASSIGNEE = "devops"
CREATED_BY = "weekly-security-audit"
SEVERITY_RE = re.compile(r"\b(CRITICAL|HIGH)\b", re.IGNORECASE)
VULN_RE = re.compile(r"\b(CVE-\d{4}-\d{4,}|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})\b", re.IGNORECASE)
# Legacy prefix matcher (kept for backwards-compat with synthetic fixtures that
# use `package: ...`); the REAL `hermes security audit` output places the
# package on the vuln line as `name==version`, which this NEVER matched -> "unknown".
PKG_RE = re.compile(r"(?:package|pkg|dependency|name)[:=]\s*([A-Za-z0-9_.+/@-]+)", re.IGNORECASE)
# Matches the `name==version` token that appears on the same line as the GHSA/CVE
# in real audit output, e.g. `HIGH  mlflow==2.19.0  GHSA-8c7q-86fq-vvmh`.
# Name starts with an identifier char; version may contain digits/dots/letters/+!~-.
PKG_TOKEN_RE = re.compile(r"\b([A-Za-z0-9_][A-Za-z0-9_./-]*)==([A-Za-z0-9][A-Za-z0-9_.+!~-]*)")


@dataclass(frozen=True)
class Finding:
    vuln_id: str
    severity: str
    package: str
    context: str


def audit_text() -> tuple[int, str]:
    fixture = os.environ.get("SECURITY_AUDIT_FIXTURE")
    if fixture:
        return 1, Path(fixture).read_text()
    proc = subprocess.run(
        [HERMES, "security", "audit", "--fail-on", "high"],
        text=True,
        capture_output=True,
        timeout=180,
    )
    return proc.returncode, (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")


def extract_findings(text: str) -> list[Finding]:
    findings: dict[str, Finding] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        ids = VULN_RE.findall(line)
        if not ids:
            continue
        start = max(0, i - 3)
        end = min(len(lines), i + 4)
        window_lines = lines[start:end]
        window = "\n".join(window_lines)

        severity_candidates: list[tuple[int, str]] = []
        for j, candidate in enumerate(window_lines, start=start):
            for match in re.finditer(r"\b(CRITICAL|HIGH|MEDIUM|LOW|MODERATE)\b", candidate, re.IGNORECASE):
                severity_candidates.append((abs(j - i), match.group(1).upper()))
        if not severity_candidates:
            continue
        severity = sorted(severity_candidates, key=lambda item: item[0])[0][1]
        if severity not in {"HIGH", "CRITICAL"}:
            continue

        # Real audit output carries the package as `name==version` on the SAME
        # line as the GHSA/CVE. Extract it directly from the vuln line; fall back
        # to the legacy prefix matcher (used by some fixtures) only if needed.
        pkg_token_match = PKG_TOKEN_RE.search(line)
        pkg_prefix_match = PKG_RE.search(line) if not pkg_token_match else None
        if pkg_token_match:
            package = pkg_token_match.group(1)
        elif pkg_prefix_match:
            package = pkg_prefix_match.group(1)
        else:
            package = "unknown"

        for vuln_id in ids:
            key = vuln_id.upper()
            findings[key] = Finding(vuln_id=key, severity=severity, package=package, context=window.strip()[:1800])
    return sorted(findings.values(), key=lambda f: (f.severity != "CRITICAL", f.vuln_id))


def create_card(f: Finding) -> str | None:
    title = f"P1 SECURITY AUDIT: {f.severity} {f.vuln_id} in {f.package}"
    body = "\n".join([
        "Auto-routed by weekly-security-audit from Hermes security audit output.",
        f"Vulnerability: {f.vuln_id}",
        f"Severity: {f.severity}",
        f"Package: {f.package}",
        "Boundary: audit/remediation planning only; no production deploy, credentials/secrets, money, irreversible data ops, or new spend without the standing critical gates.",
        "",
        "Acceptance: reproduce the finding with `/home/frank/.local/bin/hermes security audit --fail-on high`, identify the smallest safe dependency/remediation path, run focused verification, and route review-required if code/config changes are made.",
        "",
        "Source context:",
        f.context,
    ])
    proc = subprocess.run(
        [
            HERMES, "kanban", "--board", BOARD, "create", title,
            "--assignee", ASSIGNEE,
            "--priority", "90",
            "--idempotency-key", f"weekly-security-audit:{f.vuln_id}",
            "--created-by", CREATED_BY,
            "--body", body,
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        print(f"SECURITY_AUDIT_ROUTE_FAIL {f.vuln_id}: {proc.stderr.strip() or proc.stdout.strip()}")
        return None
    try:
        data = json.loads(proc.stdout)
    except Exception:
        print(f"SECURITY_AUDIT_ROUTE_FAIL {f.vuln_id}: non-json create output {proc.stdout.strip()[:300]}")
        return None
    return data.get("task_id") or data.get("id")


def main() -> int:
    rc, text = audit_text()
    findings = extract_findings(text)
    if not findings:
        if rc not in (0, 1):
            print(f"SECURITY_AUDIT_ERROR rc={rc}\n{text[-2000:]}")
            return rc
        return 0
    routed = []
    for finding in findings:
        card_id = create_card(finding)
        routed.append({"vuln_id": finding.vuln_id, "severity": finding.severity, "package": finding.package, "card_id": card_id})
    print("SECURITY_AUDIT_HIGH_CRITICAL_ROUTED " + json.dumps(routed, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
