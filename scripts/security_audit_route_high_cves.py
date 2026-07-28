#!/usr/bin/env python3
"""Run Hermes security audit and route high/critical CVE findings to kanban.

Empty stdout means no high/critical findings. Set SECURITY_AUDIT_FIXTURE to a
file path for deterministic parser/routing tests.

Router fan-out contract
------------------------
A package with many CVEs (e.g. an orphaned ``mlflow==2.19.0`` exposing ~45
advisories) is ONE root cause, not N. The router therefore fans out ONE card
per affected package keyed on its HIGHEST severity, never one card per
vuln_id. This avoids the 2026-07-13 incident where a single orphaned package
produced ~45 cards.

Append-on-existing
------------------
If an open (``ready``/``running``/``todo``/``blocked``/``review``) card for the same
package already exists, the router does NOT spawn a second card. Instead it
appends a comment listing any NEW advisories discovered in this run (plus the
full advisory set for provenance), so the board shows one accumulating card per
package across runs rather than a flood of near-duplicates.

Idempotency
-----------
* Open cards use the package-scoped key ``weekly-security-audit:pkg:<name>`` so a
  re-run does not recreate the same package's card after it is resolved.
* When a package's findings drop to zero on a later run, the router auto-closes
  any previously-closed-awaiting-resolution card for that package
  (``weekly-security-audit-resolved:<pkg>``) instead of leaving it stale/active.

Verification
------------
* ``--selftest`` asserts the per-package collapse (one multi-vuln package ->
  one issue; MODERATE/LOW/UNKNOWN ignored).
* ``--selftest-dedup`` asserts the create-vs-append decision (append with only
  the NEW advisories when an open card already lists some).
* ``--dry-run`` (with ``SECURITY_AUDIT_FIXTURE`` and optional
  ``SECURITY_AUDIT_OPEN_CARDS``) prints the planned create/append actions
  without mutating the board.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERMES = "/home/frank/.local/bin/hermes"
BOARD = "jarvis-os"
ASSIGNEE = "devops"
CREATED_BY = "weekly-security-audit"

# --- Resilience: bounded retry + same-day failure alert -------------------
# This script runs as a no_agent cron job; the scheduler delivers a non-zero
# exit to `discord:#critical-alerts` as an error alert. Therefore a
# *successful* routing run MUST exit 0 (so the run is marked healthy and its
# summary reaches #critical-alerts as a normal message, not buried in an error
# wrapper), while a *genuine* operational failure (audit feed/API down, kanban
# unreachable after retries) MUST exit 1 with a clear banner so it is alerted
# the SAME day instead of waiting 7 days for the next weekly tick. Transient
# blips are absorbed by bounded retries so they never cause a spurious weekly
# failure. Prior to this change, main() unconditionally returned 1 whenever any
# HIGH/CRITICAL findings existed -- so every healthy findings-week was
# mislabeled "error" and a real failure was indistinguishable from a good run.
AUDIT_TIMEOUT = 180
AUDIT_RETRY_ATTEMPTS = 3
AUDIT_RETRY_BASE_DELAY = 5.0
KANBAN_RETRY_ATTEMPTS = 3
KANBAN_RETRY_BASE_DELAY = 2.0

# Severity ranking (higher number = more severe). Used to pick the worst
# severity per package and to sort output.
SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "MODERATE": 2, "LOW": 1, "UNKNOWN": 0}

# Test/dry-run affordance: if this env var points to a JSON file listing
# pseudo-open cards ({id, title, idempotency_key, body}), existing_open_card
# and advisories_on_card read from it instead of the live `hermes` binary. This
# lets --dry-run prove create/append/noop transitions without touching the board.
OPEN_CARDS_ENV = "SECURITY_AUDIT_OPEN_CARDS"
OPEN_CARD_STATUSES = ("ready", "running", "todo", "blocked", "review")


def _open_cards_override() -> list[dict] | None:
    path = os.environ.get(OPEN_CARDS_ENV)
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return None
    return data if isinstance(data, list) else None

# A vuln id line in the real `hermes security audit` output looks like:
#   HIGH      pyarrow==18.1.0  GHSA-rgxp-2hwp-jwgg
# The severity token, package==version, and one or more GHSA/CVE ids sit on the
# same line. Match the package token as ``name==version`` or ``name``.
VULN_RE = re.compile(r"\b(CVE-\d{4}-\d{4,}|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})\b", re.IGNORECASE)
PKG_TOKEN_RE = re.compile(r"\b([A-Za-z0-9_.\-/]+)==[A-Za-z0-9_.\-+!~*]+")
# The first whitespace-delimited token on the line is the severity.
LEADING_SEV_RE = re.compile(r"^\s*([A-Za-z]+)\s")


@dataclass(frozen=True)
class Finding:
    vuln_id: str
    severity: str
    package: str
    context: str


def severity_from_line(line: str) -> str | None:
    """Extract the severity from a vuln line using the leading token.

    Falls back to scanning the line for any known severity word. Returns the
    canonical (upper-cased) severity or None when no severity is present.
    """
    m = LEADING_SEV_RE.match(line)
    candidates: list[str] = []
    if m and m.group(1).upper() in SEVERITY_RANK:
        candidates.append(m.group(1).upper())
    candidates += [s.upper() for s in re.findall(r"\b(CRITICAL|HIGH|MEDIUM|MODERATE|LOW|UNKNOWN)\b", line)]
    if not candidates:
        return None
    return max(candidates, key=lambda s: SEVERITY_RANK[s])


def extract_findings(text: str) -> list[Finding]:
    """Return one Finding per vuln_id, capturing package + severity from its line.

    The router dedupes later by package. This function is intentionally
    per-vuln (so context is precise) and lets the caller collapse by package.
    """
    findings: dict[str, Finding] = {}
    for line in text.splitlines():
        ids = VULN_RE.findall(line)
        if not ids:
            continue
        severity = severity_from_line(line)
        if severity is None or severity not in {"HIGH", "CRITICAL"}:
            continue
        pkg_match = PKG_TOKEN_RE.search(line)
        package = pkg_match.group(1) if pkg_match else "unknown"
        context = line.strip()[:1800]
        for vuln_id in ids:
            key = vuln_id.upper()
            # Keep the first (stable) occurrence; severities are consistent per id.
            findings.setdefault(key, Finding(vuln_id=key, severity=severity, package=package, context=context))
    return sorted(findings.values(), key=lambda f: (f.severity != "CRITICAL", f.vuln_id))


@dataclass(frozen=True)
class PackageIssue:
    package: str
    severity: str  # highest severity among the package's findings
    vuln_ids: tuple[str, ...]
    context: str

    @property
    def key(self) -> str:
        return f"weekly-security-audit:pkg:{self.package}"


def group_by_package(findings: list[Finding]) -> list[PackageIssue]:
    """Collapse per-vuln findings into one issue per package (max severity)."""
    by_pkg: dict[str, PackageIssue] = {}
    for f in findings:
        existing = by_pkg.get(f.package)
        if existing is None:
            by_pkg[f.package] = PackageIssue(
                package=f.package,
                severity=f.severity,
                vuln_ids=(f.vuln_id,),
                context=f.context,
            )
            continue
        merged_sev = (
            f.severity if SEVERITY_RANK[f.severity] > SEVERITY_RANK[existing.severity] else existing.severity
        )
        by_pkg[f.package] = PackageIssue(
            package=f.package,
            severity=merged_sev,
            vuln_ids=tuple(sorted(set(existing.vuln_ids + (f.vuln_id,)))),
            context=existing.context,
        )
    return sorted(by_pkg.values(), key=lambda p: (p.severity != "CRITICAL", p.package))


def _run_hermes(args: list[str], timeout: int = 30,
                attempts: int = KANBAN_RETRY_ATTEMPTS,
                base_delay: float = KANBAN_RETRY_BASE_DELAY) -> tuple[int, dict | None, str]:
    """Run a hermes CLI subcommand, retrying transient subprocess failures.

    Returns ``(rc, parsed_json_or_None, err_text)``. Only *transient* failures
    (subprocess timeout / exception -- e.g. a momentary network or API blip)
    are retried; a non-zero rc is a valid business outcome (e.g. a kanban create
    was rejected) and is returned as-is so callers can decide. After exhausting
    attempts on a transient failure we return a sentinel rc (``-1`` timeout,
    ``-2`` other) so callers can flag an operational error rather than silently
    succeeding.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                [HERMES, *args],
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            data = None
            if proc.stdout:
                try:
                    data = json.loads(proc.stdout)
                except Exception:
                    data = None
            return proc.returncode, data, (proc.stderr or "").strip() or (proc.stdout or "").strip()
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return -1, None, f"timeout after {timeout}s"
        except Exception as exc:  # transient: network/API blip, OOM, etc.
            last_exc = exc
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return -2, None, str(exc)
    return -2, None, str(last_exc)


def _list_open_cards(status: str, timeout: int = 30) -> list[dict]:
    """Return open security-audit cards for a given status (no mutation)."""
    rc, data, _err = _run_hermes(
        ["kanban", "--board", BOARD, "list", "--status", status, "--json"], timeout=timeout
    )
    if rc != 0 or not data:
        return []
    return [c for c in data if (c.get("created_by") or "") == CREATED_BY]


def existing_open_card(issue: PackageIssue, timeout: int = 30) -> str | None:
    """Return the id of an open card for this package.

    Matches on the package-scoped idempotency key OR on the package parsed
    from a legacy/current router title. Returns None when no open card exists.
    """
    override = _open_cards_override()
    if override is not None:
        cards = override
    else:
        cards_by_status = []
        for status in OPEN_CARD_STATUSES:
            cards_by_status += _list_open_cards(status, timeout=timeout)
        cards = cards_by_status
    for card in cards:
        key = (card.get("idempotency_key") or "").lower()
        if key == issue.key.lower():
            return card["id"]
        # Fall back to title parse in case the key wasn't stored.
        if _parse_package_from_title(card.get("title", "") or "") == issue.package:
            return card["id"]
    return None


def _render_body(issue: PackageIssue) -> str:
    sev = issue.severity
    vuln_list = ", ".join(issue.vuln_ids)
    return "\n".join([
        "Auto-routed by weekly-security-audit from Hermes security audit output.",
        f"Package: {issue.package}",
        f"Highest severity: {sev}",
        f"Advisory count: {len(issue.vuln_ids)}",
        f"Advisories: {vuln_list}",
        "Boundary: audit/remediation planning only; no production deploy, credentials/secrets, money, irreversible data ops, or new spend without the standing critical gates.",
        "",
        "Acceptance: reproduce the finding with `/home/frank/.local/bin/hermes security audit --fail-on high`, identify the smallest safe dependency/remediation path, run focused verification, and route review-required if code/config changes are made.",
        "",
        "Source context:",
        issue.context,
    ])


def _build_create_args(issue: PackageIssue) -> list[str]:
    sev = issue.severity
    title = f"P1 SECURITY AUDIT: {sev} {issue.package} ({len(issue.vuln_ids)} advisories)"
    return [
        "kanban", "--board", BOARD, "create", title,
        "--assignee", ASSIGNEE,
        "--priority", "90",
        "--idempotency-key", issue.key,
        "--created-by", CREATED_BY,
        "--body", _render_body(issue),
        "--json",
    ]


def create_card(issue: PackageIssue) -> str | None:
    """Create the kanban card for a package issue. Returns the new task id."""
    rc, data, err = _run_hermes(_build_create_args(issue))
    if rc != 0 or not data:
        print(f"SECURITY_AUDIT_ROUTE_FAIL {issue.package}: {err[:300]}")
        return None
    return data.get("task_id") or data.get("id")


def append_comment(card_id: str, issue: PackageIssue, new_ids: set[str]) -> bool:
    """Append a comment to an existing open card listing new advisories.

    Always includes the FULL advisory set for provenance, then highlights only
    the advisories newly discovered in this run. Returns True on success.
    """
    if new_ids:
        new_line = "New advisories this run: " + ", ".join(sorted(new_ids))
    else:
        new_line = "No new advisories this run; re-confirming existing set."
    body = "\n".join([
        f"weekly-security-audit re-run for {issue.package} (highest severity {issue.severity}).",
        f"Full advisory set ({len(issue.vuln_ids)}): " + ", ".join(issue.vuln_ids),
        new_line,
        "Source context:",
        issue.context,
    ])
    rc, _data, err = _run_hermes(
        ["kanban", "--board", BOARD, "comment", card_id, body], timeout=20
    )
    if rc != 0:
        print(f"SECURITY_AUDIT_COMMENT_FAIL {issue.package} ({card_id}): {err[:200]}")
        return False
    return True


def route_decision(issue: PackageIssue, existing_ids: set[str] | None) -> tuple[str, set[str]]:
    """Decide create vs append for one issue.

    ``existing_ids`` is the set of advisory ids already recorded on an open
    card for this package (None when no open card exists). Returns
    ``("create", set())``, ``("append", {new_ids})``, or ``("noop", set())``
    when an open card already contains every advisory in this run.
    """
    if existing_ids is None:
        return "create", set()
    new_ids = set(issue.vuln_ids) - existing_ids
    if new_ids:
        return "append", new_ids
    return "noop", set()


def advisories_on_card(card_id: str) -> set[str]:
    """Parse the advisory ids already recorded on an open card's body/comments."""
    override = _open_cards_override()
    if override is not None:
        card = next((c for c in override if c.get("id") == card_id), None)
        blob = json.dumps(card) if card else ""
    else:
        rc, data, _err = _run_hermes(["kanban", "--board", BOARD, "show", card_id, "--json"], timeout=20)
        blob = json.dumps(data) if (rc == 0 and data) else ""
    if not blob:
        return set()
    return {v.upper() for v in VULN_RE.findall(blob)}


def _parse_package_from_title(title: str) -> str | None:
    """Extract the affected package from a security-audit card title.

    Handles both router title shapes:
      * legacy  : ``P1 SECURITY AUDIT: CRITICAL GHSA-xxxx in mlflow``
      * current : ``P1 SECURITY AUDIT: CRITICAL mlflow (3 advisories)``
    """
    m = re.search(r"\bin\s+([A-Za-z0-9_.\-/]+)(?:\s|$|\(|\))", title)
    if m:
        return m.group(1)
    m = re.search(r"AUDIT:\s+\w+\s+([A-Za-z0-9_.\-/]+)", title)
    if m:
        return m.group(1)
    return None


def auto_close_resolved(current_packages: set[str]) -> tuple[list[str], list[str]]:
    """Auto-close residual security-audit cards for packages no longer vulnerable.

    Enumerates open (``running``) cards the router created (``created_by ==
    weekly-security-audit``); any whose package is absent from the current
    findings is completed with a provenance summary instead of being left
    stale/active. Returns ``(closed_ids, documented_orphans)`` where
    documented_orphans are blocked/review cards we leave for a human but flag.
    """
    closed: list[str] = []
    documented: list[str] = []

    def _classify(status: str) -> None:
        rc, data, _err = _run_hermes(
            ["kanban", "--board", BOARD, "list", "--status", status, "--json"], timeout=30
        )
        if rc != 0 or not data:
            return
        for card in data:
            if (card.get("created_by") or "") != CREATED_BY:
                continue
            pkg = _parse_package_from_title(card.get("title", ""))
            if not pkg or pkg in current_packages:
                continue
            if status == "running":
                rc2, _, err2 = _run_hermes(
                    ["kanban", "--board", BOARD, "complete", card["id"],
                     "--summary",
                     f"Auto-closed by weekly-security-audit: {pkg} no longer has high/critical findings in latest audit.",
                     "--metadata",
                     json.dumps({"auto_closed": True, "package": pkg, "reason": "findings_dropped_to_zero"})],
                    timeout=20,
                )
                if rc2 == 0:
                    closed.append(card["id"])
                else:
                    print(f"SECURITY_AUTOCLOSE_FAIL {pkg} ({card['id']}): {err2[:200]}")
            else:
                # blocked / review: do not fight a human decision; document only.
                documented.append(card["id"])
                print(f"SECURITY_AUDIT_ORPHAN_DOCUMENTED {pkg} ({card['id']}, {status}): no longer in findings; needs human close")

    _classify("running")
    for st in ("blocked", "review"):
        _classify(st)
    return closed, documented


def audit_text() -> tuple[int, str]:
    """Run the Hermes security audit, retrying transient failures.

    ``hermes security audit --fail-on high`` returns 0 when there are no
    findings and 1 when HIGH/CRITICAL findings exist -- both are *valid*
    outcomes. An rc >= 2 (or a subprocess timeout/exception) indicates an
    operational failure of the audit command itself and is retried; if it
    persists we raise so ``main`` can emit a same-day failure alert instead of
    silently reporting no findings.
    """
    fixture = os.environ.get("SECURITY_AUDIT_FIXTURE")
    if fixture:
        return 1, Path(fixture).read_text()
    last_exc: Exception | None = None
    for attempt in range(1, AUDIT_RETRY_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                [HERMES, "security", "audit", "--fail-on", "high"],
                text=True,
                capture_output=True,
                timeout=AUDIT_TIMEOUT,
            )
            rc = proc.returncode
            out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            if rc >= 2:
                last_exc = RuntimeError(f"hermes security audit exited {rc}: {out[-500:]}")
                if attempt < AUDIT_RETRY_ATTEMPTS:
                    time.sleep(AUDIT_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    continue
                raise last_exc
            return rc, out
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt < AUDIT_RETRY_ATTEMPTS:
                time.sleep(AUDIT_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                continue
            raise
    raise last_exc if last_exc else RuntimeError("audit_text: unreachable")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()
    if "--selftest-dedup" in argv:
        return _selftest_dedup()
    dry_run = "--dry-run" in argv

    # Operational failure (audit feed/API down) is retried inside audit_text();
    # if it still fails we emit a clear banner and exit 1 so the scheduler
    # delivers it to #critical-alerts the SAME day (no 7-day silent gap).
    try:
        rc, text = audit_text()
    except Exception as exc:
        print(
            "SECURITY_AUDIT_OPERATIONAL_FAILURE "
            f"security audit feed/API unavailable after {AUDIT_RETRY_ATTEMPTS} retries: {exc}"
        )
        return 1

    findings = extract_findings(text)
    if not findings:
        # Healthy: no HIGH/CRITICAL findings to route. Exit 0 so the run is
        # marked ok and (since we print a one-line liveness note) delivered
        # normally to #critical-alerts as proof the job is alive.
        print("SECURITY_AUDIT_NO_HIGH_CRITICAL_FINDINGS")
        return 0

    issues = group_by_package(findings)
    routed = []
    operational_error = False
    for issue in issues:
        card_id = existing_open_card(issue)
        existing_ids = None
        if card_id is not None:
            existing_ids = advisories_on_card(card_id)
        action, new_ids = route_decision(issue, existing_ids)
        if dry_run:
            routed.append({
                "package": issue.package,
                "severity": issue.severity,
                "advisory_count": len(issue.vuln_ids),
                "vuln_ids": list(issue.vuln_ids),
                "existing_card": card_id,
                "action": action,
                "new_advisories": sorted(new_ids),
            })
            continue
        if action == "create":
            cid = create_card(issue)
            if cid is None:
                operational_error = True
        elif action == "append":
            assert card_id is not None  # route_decision("append") only when an open card exists
            ok = append_comment(card_id, issue, new_ids)
            cid = card_id if ok else None
            if not ok:
                operational_error = True
        else:  # noop
            cid = card_id
        routed.append({
            "package": issue.package,
            "severity": issue.severity,
            "advisory_count": len(issue.vuln_ids),
            "vuln_ids": list(issue.vuln_ids),
            "existing_card": card_id,
            "action": action,
            "new_advisories": sorted(new_ids),
            "card_id": cid,
        })
    closed, documented = (set(), set()) if dry_run else auto_close_resolved({i.package for i in issues})
    print("SECURITY_AUDIT_HIGH_CRITICAL_ROUTED " + json.dumps(routed, sort_keys=True))
    if dry_run:
        print("SECURITY_AUDIT_DRY_RUN true (no cards created/modified)")
    if closed:
        print("SECURITY_AUDIT_AUTOCLOSED " + json.dumps(sorted(closed), sort_keys=True))
    if documented:
        print("SECURITY_AUDIT_ORPHANS_DOCUMENTED " + json.dumps(sorted(documented), sort_keys=True))

    # Successful routing (cards created/appended, or no-ops) is a HEALTHY run:
    # exit 0 so the scheduler marks it ok and delivers the summary to
    # #critical-alerts as a normal message. Only a genuine operational error
    # (audit/kanban unreachable after retries) exits 1, which the scheduler
    # surfaces as a same-day failure alert. This removes the prior
    # false-positive where ANY findings run exited 1 and was mislabeled "error".
    if operational_error:
        print(
            "SECURITY_AUDIT_OPERATIONAL_FAILURE one or more card create/append "
            "calls failed after retries (see SECURITY_AUDIT_ROUTE_FAIL / "
            "SECURITY_AUDIT_COMMENT_FAIL lines above)"
        )
        return 1
    return 0


def _selftest() -> int:
    """Deterministic parser/routing test. Does NOT create real kanban cards.

    Builds a synthetic multi-vuln audit block and asserts the router collapses a
    single multi-vuln package into ONE issue, while separate packages stay
    separate. Exits 0 on pass, 1 on failure. This is what the SECURITY_AUDIT_FIXTURE
    acceptance criterion validates.
    """
    sample = "\n".join([
        "Found 45 known vulnerability finding(s) across 326 component(s):",
        "",
        "[venv]",
        "  CRITICAL  mlflow==2.19.0  GHSA-8C7Q-86FQ-VVMH",
        "           MLflow: arbitrary file write / potential RCE via crafted model",
        "  CRITICAL  mlflow==2.19.0  GHSA-GQ3W-7JJ3-X7GR",
        "           MLflow: path traversal in artifact download",
        "  HIGH      mlflow==2.19.0  GHSA-G35P-PX32-WHV6",
        "           MLflow: environment variable injection in AI Gateway",
        "  MODERATE  mlflow==2.19.0  GHSA-XXXX-YYYY-ZZZZ",
        "           MLflow: another fabricated advisory (below HIGH, must be ignored)",
        "  MODERATE  mlflow==2.19.0  GHSA-LOW1-LOW1-LOW1",
        "           MLflow: moderate (should be ignored, below HIGH threshold)",
        "  HIGH      pyarrow==18.1.0  GHSA-rgxp-2hwp-jwgg",
        "           Apache Arrow: use-after-free when reading IPC file",
        "  HIGH      GitPython==3.1.50  GHSA-2f96-g7mh-g2hx",
        "           GitPython: Command Injection via git long-option prefix abbreviation bypass of CVE-2026-42215 blocklist",
        "  HIGH      starlette==1.0.1  GHSA-82w8-qh3p-5jfq",
        "           Starlette: request.form() limits silently ignored",
        "",
        "No other issues.",
    ])
    findings = extract_findings(sample)
    issues = group_by_package(findings)

    # mlflow is one package with 3 HIGH/CRITICAL advisories (4th is MODERATE).
    mlflow = next((i for i in issues if i.package == "mlflow"), None)
    gitpython = next((i for i in issues if i.package == "GitPython"), None)
    pyarrow = next((i for i in issues if i.package == "pyarrow"), None)
    starlette = next((i for i in issues if i.package == "starlette"), None)

    failures = []
    # Acceptance: a multi-vuln package yields ONE issue, not N.
    if mlflow is None:
        failures.append("mlflow issue missing (parser failed to extract package)")
    else:
        if len(mlflow.vuln_ids) != 3:
            failures.append(f"mlflow advisories expected 3 (HIGH/CRITICAL only), got {len(mlflow.vuln_ids)}: {mlflow.vuln_ids}")
        if mlflow.severity != "CRITICAL":
            failures.append(f"mlflow severity expected CRITICAL (max), got {mlflow.severity}")
    if len(issues) != 4:
        failures.append(f"total issues expected 4 (GitPython, mlflow, pyarrow, starlette), got {len(issues)}: {[i.package for i in issues]}")
    if gitpython is None or pyarrow is None or starlette is None:
        failures.append("GitPython, pyarrow, or starlette issue missing")
    if gitpython is not None:
        if gitpython.vuln_ids != ("GHSA-2F96-G7MH-G2HX",):
            failures.append(f"GitPython should route only the GHSA line, got {gitpython.vuln_ids}")
        if gitpython.package == "unknown":
            failures.append("GitPython description CVE produced package=unknown")
    if any(f.vuln_id == "CVE-2026-42215" for f in findings):
        failures.append("description-only CVE-2026-42215 leaked into findings")
    # MODERATE must NOT have been routed.
    if any(i.severity not in {"HIGH", "CRITICAL"} for i in issues):
        failures.append(f"non-HIGH/CRITICAL severity leaked into issues: {[ (i.package,i.severity) for i in issues]}")

    if failures:
        print("SELFTEST_FAIL")
        for f in failures:
            print(" - " + f)
        return 1
    print("SELFTEST_PASS issues=%d mlflow_advisories=%d gitpython_advisories=%d pyarrow=%s starlette=%s description_cve_ignored=True" % (
        len(issues), len(mlflow.vuln_ids) if mlflow else 0,
        len(gitpython.vuln_ids) if gitpython else 0,
        pyarrow.severity if pyarrow else None, starlette.severity if starlette else None))
    return 0


def _selftest_dedup() -> int:
    """Assert the create-vs-append decision against an already-open card.

    Uses a fixed 2026-07-13 audit block (the mlflow==2.19.0 orphan that
    produced ~45 cards historically) and three existing-card scenarios to prove
    the router collapses one multi-vuln package to ONE card and only APPENDS the
    NEW advisories when an open card already lists some. Exits 0 on pass, 1 fail.
    """
    block = "\n".join([
        "Found 74 known vulnerability finding(s) across 326 component(s):",
        "",
        "[venv]",
        "  CRITICAL  mlflow==2.19.0  GHSA-8C7Q-86FQ-VVMH",
        "  CRITICAL  mlflow==2.19.0  GHSA-GQ3W-7JJ3-X7GR",
        "  HIGH      mlflow==2.19.0  GHSA-G35P-PX32-WHV6",
        "  HIGH      mlflow==2.19.0  GHSA-7QHF-V65M-G5F3",
        "  HIGH      mlflow==2.19.0  GHSA-FH64-R2VC-XVHR",
        "  MODERATE  mlflow==2.19.0  GHSA-MOD1-MOD1-MOD1",
        "  HIGH      pyarrow==18.1.0  GHSA-rgxp-2hwp-jwgg",
        "  HIGH      starlette==1.0.1  GHSA-82w8-qh3p-5jfq",
        "",
    ])
    findings = extract_findings(block)
    issues = group_by_package(findings)
    mlflow = next((i for i in issues if i.package == "mlflow"), None)
    failures = []

    # 1) One multi-vuln package -> exactly ONE issue, max severity, HIGH/CRIT only.
    if mlflow is None:
        failures.append("mlflow issue missing")
    else:
        if len(mlflow.vuln_ids) != 5:
            failures.append(f"mlflow advisories expected 5 (HIGH/CRITICAL), got {len(mlflow.vuln_ids)}: {mlflow.vuln_ids}")
        if mlflow.severity != "CRITICAL":
            failures.append(f"mlflow severity expected CRITICAL, got {mlflow.severity}")

    # 2) No open card -> create.
    if mlflow is not None:
        act, new = route_decision(mlflow, None)
        if act != "create":
            failures.append(f"no existing card should be create, got {act}")

    # 3) Open card already lists 2 of the 5 -> append only the 3 NEW ones.
    existing_ids = {"GHSA-8C7Q-86FQ-VVMH", "GHSA-GQ3W-7JJ3-X7GR"}
    if mlflow is not None:
        act, new = route_decision(mlflow, existing_ids)
        expected_new = {"GHSA-G35P-PX32-WHV6", "GHSA-7QHF-V65M-G5F3", "GHSA-FH64-R2VC-XVHR"}
        if act != "append":
            failures.append(f"partial overlap should be append, got {act}")
        if new != expected_new:
            failures.append(f"new advisories should be the 3 not yet on the card, got {sorted(new)}")

    # 4) Open card lists ALL 5 -> noop (nothing new to report).
    if mlflow is not None:
        act, new = route_decision(mlflow, set(mlflow.vuln_ids))
        if act != "noop":
            failures.append(f"full overlap should be noop, got {act}")
        if new:
            failures.append(f"noop should report no new advisories, got {sorted(new)}")

    # 5) Distinct packages stay separate (different package = different card).
    n_pkgs = len({i.package for i in issues})
    if n_pkgs != 3:
        failures.append(f"expected 3 distinct packages (mlflow, pyarrow, starlette), got {n_pkgs}")

    if failures:
        print("SELFTEST_DEDUP_FAIL")
        for f in failures:
            print(" - " + f)
        return 1
    print("SELFTEST_DEDUP_PASS mlflow_advisories=%d distinct_packages=3" % (
        len(mlflow.vuln_ids) if mlflow else 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
