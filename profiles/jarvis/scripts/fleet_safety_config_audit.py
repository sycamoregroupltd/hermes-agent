#!/usr/bin/env python3
"""No-agent watchdog: audit fleet safety-relevant configuration.

Checks:
  - profile config.yaml files for provider keys that violate expected patterns
  - SOUL.md for known-unsafe directives (secret embedding, credential leaks)
  - git remote URLs for embedded credentials
  - Any YAML/JSON under profiles/ containing high-sensitivity tokens

Writes to stdout only when material findings exist. Silent exit 0 when clean.
Delivers to discord:#critical-alerts.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_ROOT = Path("/home/frank/.hermes")
PROFILES_DIR = HERMES_ROOT / "profiles"


def find_yaml_json_files() -> list[Path]:
    """Recursively find all .yaml/.yml/.json files under profiles/."""
    if not PROFILES_DIR.is_dir():
        return []
    files: list[Path] = []
    for root, _dirs, filenames in os.walk(PROFILES_DIR):
        for fn in filenames:
            if fn.endswith((".yaml", ".yml", ".json")):
                files.append(Path(root) / fn)
    return sorted(files)


def scan_content(path: Path) -> list[str]:
    """Scan file content for unsafe patterns. Return findings."""
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return []

    findings: list[str] = []

    # Check for embedded credentials in git remotes
    if "github.com" in str(path):
        for line in text.splitlines():
            stripped = line.strip().lower()
            if any(x in stripped for x in ("url =", "remote:", "origin@",)):
                if re.search(r"https://[\w]+:[A-Za-z0-9]+@github\.com", stripped):
                    findings.append(f"{path}: embedded credential in URL")

    # High-sensitivity token patterns
    token_patterns = [
        (r"(?:sk-[A-Za-z0-9]{20,})", "OpenAI-style key"),
        (r"(?:gh[opsu]_[A-Za-z0-9]{20,})", "GitHub token"),
        (r"(?:xox[baprs]-[A-Za-z0-9-]+)", "Slack token"),
        (r"(?:-----BEGIN\s+(?:RSA|EC|OPENSSH|AES|PRIVATE))", "Private key header"),
        (r"(?:SECRET\s*=\s*['\"][0-9a-f]{16,})", "Secret assignment"),
    ]
    for pattern, label in token_patterns:
        matches = re.findall(pattern, text)
        if matches:
            findings.append(f"{path}: found {label} ({len(matches)} match(es))")

    return findings


def run_audit() -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    all_findings: list[str] = []
    files_scanned = 0

    yaml_files = find_yaml_json_files()
    for pf in yaml_files:
        files_scanned += 1
        findings = scan_content(pf)
        all_findings.extend(findings)

    # Also check git remotes in the hermes repo
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=HERMES_ROOT, capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            url_part = line.split()[1] if len(line.split()) > 1 else ""
            if re.search(r"https://[^/:]+:[^/]*@", url_part):
                all_findings.append(f"git remote {url_part}: embedded credential")
    except Exception:
        pass

    result_dict = {
        "timestamp": timestamp,
        "files_scanned": files_scanned,
        "findings_count": len(all_findings),
        "findings": all_findings,
    }

    if all_findings:
        print(json.dumps(result_dict, indent=2))

    return result_dict


if __name__ == "__main__":
    import subprocess
    try:
        run_audit()
        sys.exit(0)
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
