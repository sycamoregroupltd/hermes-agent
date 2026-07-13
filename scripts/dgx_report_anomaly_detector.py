#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
# dgx_report_anomaly_detector.py
"""
Enhanced Fleet-Wide Automated Anomaly Detection & Routing (Enhanced ACRADR) — Phase 1 core.

Parses the fleet report files produced by the Jarvis/Hermes cron layer, applies the
enhanced regex rule matrix, and enriches each detected anomaly with git + system
diagnostic context. Designed to run as a `no_agent: true` cron job so it never
spends tokens and never hangs.

Every parse path is DETERMINISTIC and LOCAL. There is no network/LLM call in the
detection path, so there is nothing to "fall back from" — the zero-token guarantee is
structural. Any optional enrichment (git log, system metrics) is wrapped so a failure
degrades to a stub rather than aborting the run. This is the robust-fallback design
the parent proposal (t_03e2fea5 §4 Feature 4) requires: even if a future maintainer
adds an LLM enrichment hook, the regex detector still produces 100% coverage when the
provider is down. A `--simulate-provider-outage` flag exercises that path explicitly.

Report sources (verified against live cron output):
  - Health Canary:   ~/.hermes/profiles/jarvis/cron/output/health_canary.jsonl
       * gateway records:        {"gateway_running": <bool>}
       * freshness records:      {"source": "data-freshness-probe",
                                   "data_freshness": {"overall": "ok"|"degraded",
                                                      "pipelines": {name: {"status": "stale"|...}}}}
  - Fusion Calibration: per-cron-job md files headed "# Cron Job: fusion-calibration-report"
       tokens: "Clean win rate: NN.N%", "Sample-weighted MCE: Xpp",
               "DATA-INTEGRITY WARNING", "parsing-error"
  - News Catalyst:       per-cron-job md files headed "# Cron Job: news-sentiment-catalyst"
       tokens: "running close to limit", "timeout", "Exception"
  - Fusion Engine:       per-cron-job md files headed "# Cron Job: run-signal-fusion"
       tokens: "database error", "failed to write", "fill rate < 85%"

Classification is content-driven (reads the "# Cron Job:" header) with a filename
fallback, so the detector works regardless of which cron job id wrote each file.

Exit code 0 if no anomalies (or only non-blocking); 2 if any blocking (critical)
anomaly was detected, so the wrapper cron can fail-closed / route to #critical-alerts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────

HOME = Path(os.environ.get("HOME", "/home/frank"))
DEFAULT_SCAN_ROOT = HOME / ".hermes" / "profiles" / "jarvis" / "cron" / "output"
DEFAULT_REPORTS_DIR = DEFAULT_SCAN_ROOT  # reports are spread across per-job subdirs
HEALTH_CANARY = DEFAULT_SCAN_ROOT / "health_canary.jsonl"

# Git repos to scan for recent-commit enrichment, keyed by report class.
# Verified paths on the DGX; each is best-effort and failures degrade to a stub.
GIT_REPOS = {
    "fusion_calibration": HOME / "sycode-trading",
    "fusion_engine": HOME / "sycode-trading",
    "news_catalyst": HOME / "sycode-trading",
    "health_canary": HOME / ".hermes",
    "data_freshness": HOME / ".hermes",
}

GIT_SINCE = os.environ.get("ACRADR_GIT_SINCE", "24 hours ago")
GIT_MAX_COMMITS = int(os.environ.get("ACRADR_GIT_MAX", "25"))

# ── Rule matrix ───────────────────────────────────────────────────────────────
# Each entry: report_class -> list of (rule_id, regex, is_blocking)
# blocking anomalies go to #critical-alerts (Frank-gated), non-blocking to routine channels.

GATEWAY_RULES = [
    ("health.gateway_down", re.compile(r'"gateway_running"\s*:\s*false', re.I), True),
]

# data-freshness-probe records embed data_freshness.overall + per-pipeline status
FRESHNESS_RULES = [
    ("freshness.stale_overall", re.compile(r'"overall"\s*:\s*"degraded"', re.I), False),
    ("freshness.overall_failed", re.compile(r'"overall"\s*:\s*"failed"', re.I), False),
    ("freshness.pipeline_stale",
     re.compile(r'"status"\s*:\s*"stale"', re.I), False),
]

CALIBRATION_RULES = [
    # win_rate < 20.0% — anchored to "Clean win rate" only. The report also prints a
    # "Raw win rate (fan-out inflated — do not use)" that is explicitly not actionable,
    # and a per-conviction "Win Rate" table; matching those would be false positives.
    ("calibration.win_rate_low",
     re.compile(r'Clean[\w ]*?win[_ ]?[Rr]ate\D*?(\d+(?:\.\d+)?)\s*%', re.I), False,
     lambda m: float(m.group(1)) < 20.0),
    # calibration_error > 20.0pp  (report prints "Sample-weighted MCE: Xpp")
    ("calibration.mce_high",
     re.compile(r'[Mm]ean\s*[Cc]alibration\s*[Ee]rror\D*?(\d+(?:\.\d+)?)\s*pp', re.I), False,
     lambda m: float(m.group(1)) > 20.0),
    # weighted MCE alias used by the v2 report ("Sample-weighted MCE: Xpp")
    ("calibration.mce_high_v2",
     re.compile(r'[Ss]ample-weighted\s*MCE\D*?(\d+(?:\.\d+)?)\s*pp', re.I), False,
     lambda m: float(m.group(1)) > 20.0),
    ("calibration.parsing_error", re.compile(r'parsing-error', re.I), False),
    # Data integrity warnings mean sections may be empty -> treat as degraded telemetry
    ("calibration.data_integrity", re.compile(r'DATA-INTEGRITY WARNING', re.I), False),
]

NEWS_CATALYST_RULES = [
    ("news.close_to_limit", re.compile(r'running close to limit', re.I), False),
    ("news.timeout", re.compile(r'\btimeout\b', re.I), False),
    ("news.exception", re.compile(r'\bException\b', re.I), False),
]

FUSION_ENGINE_RULES = [
    ("fusion.database_error", re.compile(r'database error', re.I), True),
    ("fusion.failed_to_write", re.compile(r'failed to write', re.I), True),
    ("fusion.fill_rate_low",
     re.compile(r'fill rate\D*?(\d+(?:\.\d+)?)\s*%', re.I), True,
     lambda m: float(m.group(1)) < 85.0),
]


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Anomaly:
    report_class: str
    rule_id: str
    severity: str          # "critical" | "warning"
    source_file: str
    source_line: Optional[int]
    snippet: str
    git_context: list = field(default_factory=list)
    system_metrics: dict = field(default_factory=dict)
    fallback_used: bool = False   # True when enrichment degraded (provider/LLM outage sim)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fallback_used"] = self.fallback_used
        return d


# ── Enrichment helpers (all best-effort; never raise out of the detector) ────

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_recent_commits(repo: Path, since: str = GIT_SINCE, limit: int = GIT_MAX_COMMITS) -> list:
    """Return recent oneline commit subjects, or a stub on any failure.

    This is the Git Metadata Enrichment hook from the Enhanced ACRADR spec (§2 Feature 2).
    Best-effort: a missing repo or git failure yields a single stub line rather than
    aborting the whole scan.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "log", f'--since="{since}"', "--oneline", "-n", str(limit)],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return [f"(no commits in last {since} in {repo.name})"]
        return [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
    except Exception:
        return [f"(git enrichment failed for {repo.name})"]


def system_metrics() -> dict:
    """Attach active CPU / Memory / GPU(VRAM) metrics at the moment of detection.

    Best-effort: any missing dependency/tool yields a stub key rather than a crash.
    """
    metrics: dict = {}
    # CPU + memory via psutil (verified present: psutil 7.2.2)
    try:
        import psutil
        metrics["cpu_percent"] = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        metrics["mem_percent"] = vm.percent
        metrics["mem_used_gb"] = round(vm.used / (1024 ** 3), 2)
        metrics["mem_total_gb"] = round(vm.total / (1024 ** 3), 2)
    except Exception as e:
        metrics["cpu_error"] = f"{type(e).__name__}: psutil unavailable"

    # GPU / VRAM via nvidia-smi (verified present at /usr/bin/nvidia-smi)
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            gpus = []
            for i, raw_line in enumerate(r.stdout.strip().splitlines()):
                line = raw_line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 3:
                    continue
                gpu = {"gpu": i, "util_percent": None, "vram_used_mb": None, "vram_total_mb": None}
                try:
                    gpu["util_percent"] = int(parts[0])
                except ValueError:
                    gpu["util_percent"] = None
                # Some DGX drivers report VRAM as "[N/A]" — keep util, mark VRAM unknown
                if parts[1] not in ("[N/A]", "N/A", ""):
                    try:
                        gpu["vram_used_mb"] = int(parts[1])
                    except ValueError:
                        pass
                if parts[2] not in ("[N/A]", "N/A", ""):
                    try:
                        gpu["vram_total_mb"] = int(parts[2])
                    except ValueError:
                        pass
                gpus.append(gpu)
            metrics["gpus"] = gpus
        else:
            metrics["gpu_error"] = (r.stderr or "nvidia-smi produced no output").strip()[:120]
    except Exception as e:
        metrics["gpu_error"] = f"{type(e).__name__}: nvidia-smi unavailable"

    return metrics


# ── Classifier ────────────────────────────────────────────────────────────────

def classify_file(path: Path, header: Optional[str]) -> Optional[str]:
    """Return one of the report classes, or None if not a tracked report.

    Preference: explicit "# Cron Job:" header -> filename pattern -> None.
    """
    if header:
        hl = header.lower()
        if "fusion-calibration" in hl:
            return "fusion_calibration"
        if "news-sentiment" in hl or "news_catalyst" in hl:
            return "news_catalyst"
        if "run-signal-fusion" in hl or "signal-fusion" in hl:
            return "fusion_engine"
    name = path.name.lower()
    if name == "health_canary.jsonl" or name.endswith(".jsonl"):
        # The only .jsonl report fed to the detector is the health canary
        # (gateway + data-freshness-probe records). Treat all .jsonl as such.
        return "health_canary"
    if "calibration" in name:
        return "fusion_calibration"
    if "news_sentiment" in name or "news_catalyst" in name:
        return "news_catalyst"
    if "fusion_engine" in name or "signal_fusion" in name:
        return "fusion_engine"
    return None


def _match_rules(text: str, rules) -> list:
    """Apply a rule list. Rules are (id, compiled_re, blocking) or
    (id, compiled_re, blocking, threshold_pred). threshold_pred receives the
    match and must return True to fire."""
    hits = []
    for rule in rules:
        rid, rx, blocking = rule[0], rule[1], rule[2]
        pred = rule[3] if len(rule) > 3 else None
        for m in rx.finditer(text):
            if pred is not None and not pred(m):
                continue
            snippet = m.group(0)
            line_no = text.count("\n", 0, m.start()) + 1
            hits.append((rid, blocking, snippet, line_no))
            break  # one hit per rule is enough
    return hits


def _latest_jsonl_records(path: Path, max_lines: int = 10000):
    """Return (latest_gateway_obj, latest_freshness_obj) from a health_canary.jsonl.

    The canary file is APPEND-ONLY and historical — a gateway blip from weeks ago
    must NOT re-fire today. We evaluate CURRENT STATE only: the most recent
    gateway record and the most recent data-freshness-probe record. Returns
    (None, None) if neither class of record is present.

    `degraded`/`failed` in a freshness record is a WARNING (routine/deduped to
    #fleet-reports), NOT critical — a single stale feed is recoverable and is
    already handled by the data-freshness-kanban-sidecar. Only gateway-down is
    critical (the Hermes gateway itself is dead -> everything blind).
    """
    last_gw = None
    last_fw = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, raw in enumerate(fh):
                if i > max_lines:
                    break
                raw_strip = raw.strip()
                if not raw_strip:
                    continue
                try:
                    obj = json.loads(raw_strip)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj.get("gateway_running"), bool):
                    last_gw = obj
                df = obj.get("data_freshness")
                if isinstance(df, dict):
                    last_fw = obj
    except Exception:
        pass
    return last_gw, last_fw


def _gw_anomalies(last_gw, provider_outage: bool, source_file: str) -> list:
    if last_gw is None:
        return []
    line = json.dumps(last_gw)
    out = []
    for rid, rx, blocking in GATEWAY_RULES:
        if rx.search(line):
            out.append(Anomaly(
                report_class="health_canary",
                rule_id=rid,
                severity="critical" if blocking else "warning",
                source_file=source_file,
                source_line=None,
                snippet=line.strip()[:400],
                git_context=git_recent_commits(GIT_REPOS["health_canary"]),
                system_metrics=system_metrics(),
                fallback_used=provider_outage,
            ))
    return out


def _freshness_anomalies(last_fw, provider_outage: bool, source_file: str) -> list:
    if last_fw is None:
        return []
    df = last_fw.get("data_freshness", {})
    if not isinstance(df, dict):
        return []
    overall = df.get("overall")
    out = []
    if overall in ("degraded", "failed"):
        line = json.dumps(df)
        for rid, rx, blocking in FRESHNESS_RULES:
            if rx.search(line):
                out.append(Anomaly(
                    report_class="health_canary",
                    rule_id=rid,
                    severity="critical" if blocking else "warning",
                    source_file=source_file,
                    source_line=None,
                    snippet=f"overall={overall} {line.strip()[:300]}",
                    git_context=git_recent_commits(GIT_REPOS["data_freshness"]),
                    system_metrics=system_metrics(),
                    fallback_used=provider_outage,
                ))
    # per-pipeline stale feeds
    for pname, pstate in (df.get("pipelines") or {}).items():
        if isinstance(pstate, dict) and pstate.get("status") == "stale":
            line = json.dumps(pstate)
            for rid, rx, blocking in FRESHNESS_RULES:
                if rx.search(line):
                    out.append(Anomaly(
                        report_class="health_canary",
                        rule_id=rid,
                        severity="critical" if blocking else "warning",
                        source_file=source_file,
                        source_line=None,
                        snippet=f"pipeline {pname}: {line.strip()[:300]}",
                        git_context=git_recent_commits(GIT_REPOS["data_freshness"]),
                        system_metrics=system_metrics(),
                        fallback_used=provider_outage,
                    ))
    return out


# ── Core scan ───────────────────────────────────────────────────────────────

def scan_text_report(path: Path, report_class: str, header: Optional[str],
                     provider_outage: bool) -> list:
    """Scan a single markdown/text report file and return Anomaly objects."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if report_class == "health_canary":
        last_gw, last_fw = _latest_jsonl_records(path)
        anomalies = _gw_anomalies(last_gw, provider_outage, str(path))
        anomalies += _freshness_anomalies(last_fw, provider_outage, str(path))
        return anomalies

    rules_map = {
        "fusion_calibration": CALIBRATION_RULES,
        "news_catalyst": NEWS_CATALYST_RULES,
        "fusion_engine": FUSION_ENGINE_RULES,
    }
    raw_hits = _match_rules(text, rules_map.get(report_class, []))

    anomalies: list = []
    for rid, blocking, snippet, line_no in raw_hits:
        severity = "critical" if blocking else "warning"
        repo = GIT_REPOS.get(report_class)
        git_ctx = git_recent_commits(repo) if repo else []
        metrics = system_metrics()
        anomalies.append(Anomaly(
            report_class=report_class,
            rule_id=rid,
            severity=severity,
            source_file=str(path),
            source_line=line_no,
            snippet=snippet[:400],
            git_context=git_ctx,
            system_metrics=metrics,
            fallback_used=provider_outage,
        ))
    return anomalies


def discover_reports(scan_root: Path) -> list:
    """Yield (path, header) for every candidate report under scan_root."""
    candidates = []
    # health_canary.jsonl sits at the scan root (testable: relative to scan_root,
    # not the global constant, so a tmp scan dir does not pull in live data).
    hc = scan_root / "health_canary.jsonl"
    if hc.exists():
        candidates.append((hc, None))
    # walk subdirs for markdown cron outputs
    try:
        for md in sorted(scan_root.rglob("*.md")):
            header = None
            try:
                with md.open(encoding="utf-8", errors="replace") as fh:
                    first = fh.readline()
                    if first.startswith("# Cron Job:"):
                        header = first[len("# Cron Job:"):].strip()
            except Exception:
                pass
            candidates.append((md, header))
    except Exception:
        pass
    return candidates


def run_detection(scan_root: Path = DEFAULT_REPORTS_DIR,
                  provider_outage: bool = False,
                  limit: Optional[int] = None) -> list:
    """Full scan across all discovered reports. Returns list[Anomaly]."""
    anomalies: list = []
    candidates = discover_reports(scan_root)
    for path, header in candidates:
        report_class = classify_file(path, header)
        if report_class is None:
            continue
        found = scan_text_report(path, report_class, header, provider_outage)
        anomalies.extend(found)
        if limit is not None and len(anomalies) >= limit:
            break
    return anomalies


# ── Reporting (stdout for cron delivery) ─────────────────────────────────────

SEVERITY_TO_CHANNEL = {
    "critical": "discord:#critical-alerts (1521973787363508325)",
    "warning": "discord:#fleet-reports / #quant-reports (per class)",
}

REPORT_CLASS_TO_CHANNEL = {
    "health_canary": "discord:#critical-alerts (1521973787363508325)",
    "fusion_engine": "discord:#critical-alerts (1521973787363508325)",
    "data_freshness": "discord:#fleet-reports (1521973775761936646)",
    "fusion_calibration": "discord:#quant-reports (1521973779457118449)",
    "news_catalyst": "discord:#fleet-reports (1521973775761936646)",
}


def render_report(anomalies: list, provider_outage: bool) -> str:
    now = utcnow_iso()
    lines = []
    lines.append(f"# Enhanced ACRADR Anomaly Report — {now}")
    lines.append("")
    if provider_outage:
        lines.append("> ⚠ ZERO-TOKEN FALLBACK ACTIVE: provider/LLM enrichment disabled; "
                     "deterministic regex parsing used for 100% coverage.")
    if not anomalies:
        lines.append("✅ No anomalies detected across scanned fleet reports.")
        return "\n".join(lines)

    critical = [a for a in anomalies if a.severity == "critical"]
    warning = [a for a in anomalies if a.severity == "warning"]
    lines.append(f"Detected: {len(anomalies)} anomaly(ies) "
                 f"({len(critical)} critical, {len(warning)} warning)")
    lines.append("")

    for sev, group in (("critical", critical), ("warning", warning)):
        if not group:
            continue
        lines.append(f"## {sev.upper()} — {len(group)}")
        lines.append("")
        for a in group:
            channel = REPORT_CLASS_TO_CHANNEL.get(a.report_class, SEVERITY_TO_CHANNEL[sev])
            lines.append(f"- **[{a.report_class}] {a.rule_id}** → {channel}")
            lines.append(f"  - file: `{a.source_file}`"
                         + (f":{a.source_line}" if a.source_line else ""))
            lines.append(f"  - match: `{a.snippet[:200]}`")
            if a.git_context:
                lines.append(f"  - recent commits ({len(a.git_context)}):")
                for c in a.git_context[:5]:
                    lines.append(f"      • {c}")
            if a.system_metrics:
                sm = a.system_metrics
                bits = []
                if "cpu_percent" in sm:
                    bits.append(f"cpu={sm['cpu_percent']}%")
                if "mem_percent" in sm:
                    bits.append(f"mem={sm['mem_percent']}% ({sm['mem_used_gb']}/{sm['mem_total_gb']}GB)")
                if "gpus" in sm:
                    for g in sm["gpus"]:
                        bits.append(f"gpu{g['gpu']}={g['util_percent']}% vram={g['vram_used_mb']}/{g['vram_total_mb']}MB")
                if bits:
                    lines.append(f"  - system: {', '.join(bits)}")
            lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Enhanced ACRADR core detection script")
    parser.add_argument("--scan-root", type=str, default=str(DEFAULT_REPORTS_DIR),
                        help="Directory tree to scan for report files")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of markdown")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N anomalies (for tests / smoke runs)")
    parser.add_argument("--simulate-provider-outage", action="store_true",
                        help="Flag enrichment as zero-token fallback (provider outage path)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress stdout report (still returns exit code)")
    args = parser.parse_args(argv)

    provider_outage = args.simulate_provider_outage
    try:
        anomalies = run_detection(Path(args.scan_root), provider_outage=provider_outage,
                                  limit=args.limit)
    except Exception as e:  # never let the watchdog itself crash silently
        sys.stderr.write("ACRADR detector crashed: %s\n%s\n" %
                         (e, traceback.format_exc()))
        return 3

    if args.json:
        out = {
            "generated_at": utcnow_iso(),
            "fallback_used": provider_outage,
            "anomaly_count": len(anomalies),
            "anomalies": [a.to_dict() for a in anomalies],
        }
        if not args.quiet:
            print(json.dumps(out, indent=2, default=str))
    else:
        if not args.quiet:
            print(render_report(anomalies, provider_outage))

    has_critical = any(a.severity == "critical" for a in anomalies)
    return 2 if has_critical else 0


if __name__ == "__main__":
    sys.exit(main())
