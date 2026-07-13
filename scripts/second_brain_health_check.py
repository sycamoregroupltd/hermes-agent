#!/usr/bin/env python3
"""Run the canonical two-vault second-brain audit and catalog drift check."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from second_brain_writer import write_json_atomic


FLEET = Path("/home/frank/obsidian-fleet-vault")
SYCODE = Path("/home/frank/obsidian/sycode-trading")
PROFILES = Path("/home/frank/.hermes/profiles")
SKILLS = Path("/home/frank/.hermes/skills")
QUARANTINED_PROFILES = Path("/home/frank/.hermes/quarantined-profiles")
QUARANTINED_SKILLS = Path("/home/frank/.hermes/quarantined-skills")
AUDITOR = FLEET / "System" / "Scripts" / "audit_second_brain.py"
CATALOG_GENERATOR = FLEET / "System" / "Scripts" / "generate_knowledge_catalogs.py"
RETRIEVAL_EVALUATOR = FLEET / "System" / "Scripts" / "evaluate_second_brain_retrieval.py"
RETRIEVAL_QUERIES = FLEET / "System" / "Evaluations" / "retrieval-queries.yaml"
WRITER_AUDITOR = Path("/home/frank/.hermes/scripts/audit_scheduled_knowledge_writers.py")
WRITER_EXCEPTIONS = Path("/home/frank/.hermes/scripts/scheduled-writer-exceptions.json")
AGENT_ADOPTION_AUDITOR = FLEET / "System" / "Scripts" / "audit_dgx_agent_knowledge_adoption.py"
DEFAULT_REPORT_DIR = FLEET / "Operations" / "second-brain-health"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-on",
        choices=("never", "critical", "high", "medium", "low"),
        default="high",
        help="Exit non-zero when this severity or worse is present.",
    )
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args()
    report_dir = args.report_dir.expanduser().resolve()

    missing = [str(path) for path in (FLEET, SYCODE, PROFILES, SKILLS, AUDITOR, CATALOG_GENERATOR, RETRIEVAL_EVALUATOR, RETRIEVAL_QUERIES, WRITER_AUDITOR, WRITER_EXCEPTIONS, AGENT_ADOPTION_AUDITOR) if not path.exists()]
    if missing:
        print(f"SECOND_BRAIN_HEALTH_BLOCKED missing={json.dumps(missing)}")
        return 2

    report_dir.mkdir(parents=True, exist_ok=True)
    json_report = report_dir / "latest.json"
    markdown_report = report_dir / "latest.md"
    audit = run(
        [
            sys.executable,
            str(AUDITOR),
            "--root",
            f"fleet={FLEET}",
            "--root",
            f"sycode={SYCODE}",
            "--profiles-dir",
            str(PROFILES),
            "--skills-dir",
            str(SKILLS),
            "--json-out",
            str(json_report),
            "--markdown-out",
            str(markdown_report),
            "--fail-on",
            "never",
        ]
    )
    if audit.returncode != 0 or not json_report.is_file():
        detail = (audit.stderr or audit.stdout).strip()[-1000:]
        print(f"SECOND_BRAIN_HEALTH_BLOCKED audit_runner_failed={json.dumps(detail)}")
        return 2

    report = json.loads(json_report.read_text(encoding="utf-8"))
    catalogs = run(
        [
            sys.executable,
            str(CATALOG_GENERATOR),
            "--profiles-dir",
            str(PROFILES),
            "--skills-dir",
            str(SKILLS),
            "--quarantined-profiles-dir",
            str(QUARANTINED_PROFILES),
            "--quarantined-skills-dir",
            str(QUARANTINED_SKILLS),
            "--output",
            str(FLEET),
            "--date",
            dt.date.today().isoformat(),
            "--check",
        ]
    )
    catalog_ok = catalogs.returncode == 0
    retrieval_json = report_dir / "retrieval-latest.json"
    retrieval = run(
        [
            sys.executable,
            str(RETRIEVAL_EVALUATOR),
            "--root",
            f"fleet={FLEET}",
            "--root",
            f"sycode={SYCODE}",
            "--queries",
            str(RETRIEVAL_QUERIES),
            "--json-out",
            str(retrieval_json),
        ]
    )
    retrieval_ok = retrieval.returncode == 0
    writer_json = report_dir / "scheduled-writers-latest.json"
    writers = run(
        [
            sys.executable,
            str(WRITER_AUDITOR),
            "--profiles-dir",
            str(PROFILES),
            "--shared-scripts-dir",
            "/home/frank/.hermes/scripts",
            "--exceptions",
            str(WRITER_EXCEPTIONS),
            "--json-out",
            str(writer_json),
        ]
    )
    writers_ok = writers.returncode == 0
    adoption_json = report_dir / "agent-adoption-latest.json"
    adoption = run([sys.executable, str(AGENT_ADOPTION_AUDITOR), "--json-out", str(adoption_json)])
    adoption_ok = adoption.returncode == 0
    runner_report = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "audit_status": report.get("status"),
        "audit_summary": report.get("summary"),
        "catalogs_current": catalog_ok,
        "catalog_check": (catalogs.stdout or catalogs.stderr).strip(),
        "retrieval_current": retrieval_ok,
        "retrieval_check": (retrieval.stdout or retrieval.stderr).strip()[-2000:],
        "scheduled_writers_current": writers_ok,
        "scheduled_writer_check": (writers.stdout or writers.stderr).strip()[-2000:],
        "agent_adoption_current": adoption_ok,
        "agent_adoption_check": (adoption.stdout or adoption.stderr).strip()[-2000:],
        "canonical_json": str(json_report),
        "operator_markdown": str(markdown_report),
    }
    write_json_atomic(report_dir / "runner-latest.json", runner_report)

    severities = report.get("summary", {}).get("by_severity", {})
    threshold = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    fail_rank = threshold[args.fail_on] if args.fail_on != "never" else -1
    has_failure = args.fail_on != "never" and any(
        count and threshold[severity] <= fail_rank for severity, count in severities.items()
    )
    if not catalog_ok or not retrieval_ok or not writers_ok or not adoption_ok:
        has_failure = True
    status = "GREEN" if not report.get("summary", {}).get("findings") and catalog_ok and retrieval_ok and writers_ok and adoption_ok else (
        "BLOCKED" if severities.get("critical") or severities.get("high") or not catalog_ok or not retrieval_ok or not writers_ok or not adoption_ok else "DEGRADED"
    )
    print(
        "SECOND_BRAIN_HEALTH_"
        f"{status} findings={report.get('summary', {}).get('findings', 0)} "
        f"severity={json.dumps(severities, sort_keys=True)} catalogs_current={str(catalog_ok).lower()} retrieval_current={str(retrieval_ok).lower()} scheduled_writers_current={str(writers_ok).lower()} agent_adoption_current={str(adoption_ok).lower()} "
        f"report={markdown_report} json={json_report}"
    )
    return 2 if has_failure else 0


if __name__ == "__main__":
    sys.exit(main())
