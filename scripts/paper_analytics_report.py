#!/usr/bin/env python3
"""Generate the paper-only Sycode analytics note from read-only JSON endpoints.

The report is deliberately deterministic: all endpoint reads must succeed before
the canonical note is atomically replaced. The read credential is loaded only
from the protected environment, never printed or placed in argv; the curl
wrapper moves it into a mode-600 config for each request.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from second_brain_writer import write_markdown_atomic


JOB_ID = "35fd9cd6d157"
DEFAULT_WRAPPER = Path("/home/frank/.hermes/scripts/sycode-token-safe-curl.sh")
DEFAULT_CREDENTIAL_FILE = Path("/home/frank/.hermes/secrets/sycode-credential.env")
FALLBACK_CREDENTIAL_FILE = Path("/home/frank/.hermes/.env")
DEFAULT_BASE_URL = "http://localhost:3001/api/openclaw"
DEFAULT_OUTPUT_DIR = Path("/home/frank/obsidian/quant-team/analytics")
ENDPOINTS = {
    "predictions": "/ml/predictions/recent?limit=50",
    "signals": "/signals/journey/stats",
    "strategies": "/strategies/enabled?limit=20",
    "market": "/market-context",
}


def read_token_environment() -> dict[str, str]:
    """Return a child environment with only the required read credential loaded.

    Environment values win. Otherwise python-dotenv parses the established
    protected credential files without printing, copying, or changing them.
    """
    child = os.environ.copy()
    token = child.get("SYCODE_READ_TOKEN") or child.get("OPENCLAW_READ_TOKEN")
    if not token:
        try:
            from dotenv import dotenv_values
        except ImportError as exc:
            raise RuntimeError("python-dotenv is required for protected no-agent credential loading") from exc
        configured = Path(child.get("SYCODE_CREDENTIAL_ENV_FILE", str(DEFAULT_CREDENTIAL_FILE)))
        for path in (configured, FALLBACK_CREDENTIAL_FILE):
            if not path.is_file():
                continue
            values = dotenv_values(path)
            token = values.get("SYCODE_READ_TOKEN") or values.get("OPENCLAW_READ_TOKEN")
            if token:
                break
    if not token:
        raise RuntimeError("Sycode read credential is unavailable in the protected environment")
    child["SYCODE_READ_TOKEN"] = str(token)
    return child


def fetch_json(wrapper: Path, url: str, child_environment: dict[str, str]) -> Any:
    """Fetch one Sycode endpoint without putting a credential value in argv."""
    result = subprocess.run(
        [str(wrapper), "-sS", "--fail-with-body", url],
        text=True,
        capture_output=True,
        check=False,
        env=child_environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RuntimeError(f"read-only endpoint failed ({result.returncode}): {url}: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"endpoint returned invalid JSON: {url}: {exc}") from exc


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def display(value: Any, default: str = "unknown") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def compact_json(value: Any, limit: int = 1400) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return rendered if len(rendered) <= limit else rendered[: limit - 15] + "…[truncated]"


def prediction_summary(payload: Any) -> tuple[list[str], list[str]]:
    predictions = as_list(as_dict(payload).get("predictions"))
    normalized = []
    for item in predictions:
        if not isinstance(item, dict):
            continue
        outcome = item.get("resolved_outcome")
        actual = as_dict(item.get("actualOutputs"))
        if not outcome and isinstance(actual.get("actual_is_winner"), bool):
            outcome = "win" if actual["actual_is_winner"] else "loss"
        normalized.append((item, str(outcome or "pending").lower()))
    outcomes = Counter(outcome for _, outcome in normalized)
    wins = outcomes["win"]
    losses = outcomes["loss"]
    resolved = wins + losses
    pending = max(0, len(predictions) - resolved)
    rate = (wins / resolved * 100.0) if resolved else None
    lines = [
        f"- Predictions returned: **{len(predictions)}**; resolved: **{resolved}** "
        f"({wins}W/{losses}L); pending or unscored: **{pending}**.",
        f"- Resolved win rate: **{rate:.1f}%**." if rate is not None else "- Resolved win rate: **not measurable** (zero resolved outcomes).",
    ]
    by_model: dict[str, Counter[str]] = defaultdict(Counter)
    for item, outcome in normalized:
        model = display(first(item, "model_name", "model", "modelName", "modelType", default="unspecified"))
        by_model[model][outcome] += 1
    for model, counts in sorted(by_model.items())[:8]:
        model_resolved = counts["win"] + counts["loss"]
        model_rate = counts["win"] / model_resolved * 100.0 if model_resolved else None
        suffix = f"{model_rate:.1f}% ({counts['win']}W/{counts['loss']}L)" if model_rate is not None else "unmeasured"
        lines.append(f"- `{model}`: {suffix}; pending {counts['pending']}.")
    findings = []
    if resolved == 0:
        findings.append("⚠️ Prediction outcomes are unresolved, so model accuracy cannot be measured.")
    elif rate is not None and rate < 50.0:
        findings.append(f"⚠️ Resolved prediction win rate is below 50% ({rate:.1f}%).")
    return lines, findings


def signal_summary(payload: Any) -> tuple[list[str], list[str]]:
    data = as_dict(payload)
    stages = as_dict(first(data, "stages", "stageCounts", "journeyStages", "byStage", default={}))
    approved = integer(first(data, "approved", "approvedCount", default=first(stages, "APPROVED", "approved", default=0)))
    rejected = integer(first(data, "rejected", "rejectedCount", default=first(stages, "REJECTED", "rejected", default=0)))
    expired = integer(first(data, "expired", "expiredCount", default=first(stages, "EXPIRED", "expired", default=0)))
    active = integer(first(data, "active", "activeSignals", "activeCount", "totalActive", default=0))
    total = integer(first(data, "total", "totalSignals", "totalJourneys", default=approved + rejected + expired + active))
    rate_value = first(data, "approvalRate", "approval_rate", default=None)
    approval_rate = number(rate_value, -1.0)
    if approval_rate < 0 and total:
        approval_rate = approved / total * 100.0
    elif 0 <= approval_rate <= 1 and rate_value is not None:
        approval_rate *= 100.0
    avg_seconds = number(first(data, "avgValidationTime", "averageValidationTime", "avg_validation_time", default=0))
    lines = [
        f"- Active: **{active}**; approved: **{approved}**; rejected: **{rejected}**; expired: **{expired}**; total: **{total}**.",
        f"- Approval rate: **{approval_rate:.2f}%**." if approval_rate >= 0 else "- Approval rate: **not supplied**.",
    ]
    if avg_seconds:
        lines.append(f"- Average validation time: **{avg_seconds:.1f}s**.")
    findings = []
    if total and approved == 0:
        findings.append("⚠️ No signal journey is approved; confirm that this is intended validation or regime gating.")
    return lines, findings


def strategy_summary(payload: Any) -> tuple[list[str], list[str]]:
    data = as_dict(payload)
    strategies = [item for item in as_list(data.get("strategies")) if isinstance(item, dict)]
    lines = [f"- Enabled strategies returned: **{len(strategies)}**."]
    disabled = []
    for item in strategies[:10]:
        name = display(first(item, "name", "displayName", "id", default="unnamed"))
        enabled = first(item, "isEnabled", "enabled", "is_enabled", default=True)
        engine = display(first(item, "engine", "strategyType", "type", default="unspecified"))
        lines.append(f"- {name}: enabled={display(enabled)}, engine={engine}.")
        if enabled is False:
            disabled.append(name)
    findings = [f"⚠️ The enabled-strategy endpoint returned disabled entries: {', '.join(disabled)}."] if disabled else []
    return lines, findings


def market_summary(payload: Any) -> tuple[list[str], list[str]]:
    data = as_dict(payload)
    if isinstance(data.get("context"), dict):
        data = data["context"]
    fear_value = first(data, "fearGreedIndex", "fear_greed_index", "fearGreed", default=None)
    if isinstance(fear_value, dict):
        fear_label = display(first(fear_value, "classification", "label", default="unknown"))
        fear_number = first(fear_value, "value", "index", default=None)
    else:
        fear_number = fear_value
        fear_label = display(first(data, "fearGreedLabel", "fear_greed_label", "fearGreedClassification", default="unknown"))
    regime_value = first(data, "marketRegime", "market_regime", "regime", default="unknown")
    if isinstance(regime_value, dict):
        regime = display(first(regime_value, "name", "regime", "label", default="unknown"))
        confidence = first(regime_value, "confidence", default=first(data, "regimeConfidence", default=None))
    else:
        regime = display(regime_value)
        confidence = first(data, "regimeConfidence", "regime_confidence", "confidence", default=None)
    lines = [
        f"- Fear/Greed: **{display(fear_number)}** ({fear_label}).",
        f"- Market regime: **{regime}**; confidence: **{display(confidence)}**.",
        f"- BTC dominance: **{display(first(data, 'btcDominance', 'btc_dominance', default=None))}**; "
        f"altcoin season: **{display(first(data, 'altcoinSeasonIndex', 'altcoin_season_index', default=None))}**.",
        f"- Data quality: **{display(first(data, 'dataQuality', 'data_quality', default=None))}**.",
    ]
    findings = []
    if fear_number is not None and number(fear_number, 100.0) < 30:
        findings.append(f"⚠️ Fear/Greed is below 30 ({display(fear_number)}); retain conservative paper-only interpretation.")
    if regime.upper() in {"RISK_OFF", "TRANSITIONING"}:
        findings.append(f"⚠️ Market regime is {regime}; directional signals may be less reliable.")
    return lines, findings


def render_report(payloads: dict[str, Any], generated_at: dt.datetime) -> str:
    sections = []
    findings: list[str] = []
    for heading, key, summarizer in (
        ("ML performance", "predictions", prediction_summary),
        ("Signal health", "signals", signal_summary),
        ("Strategy health", "strategies", strategy_summary),
        ("Market context", "market", market_summary),
    ):
        lines, section_findings = summarizer(payloads[key])
        sections.extend([f"## {heading}", "", *lines, ""])
        findings.extend(section_findings)
    recommendations = findings or ["All deterministic checks are healthy; no advisory action is required."]
    evidence = [f"- `{key}`: `{compact_json(payloads[key])}`" for key in ENDPOINTS]
    return "\n".join(
        [
            f"# Paper analytics report — {generated_at:%Y-%m-%d %H:%M UTC}",
            "",
            "> Read-only analytics evidence. This generator cannot place, amend, or authorize trades.",
            "",
            *sections,
            "## Recommendations",
            "",
            *[f"- {item}" for item in recommendations],
            "",
            "## Source snapshot",
            "",
            *evidence,
        ]
    )


def write_report(payloads: dict[str, Any], output: Path, generated_at: dt.datetime) -> Path:
    day = generated_at.date().isoformat()
    return write_markdown_atomic(
        output,
        render_report(payloads, generated_at),
        title=f"Paper analytics report — {day}",
        type="task-evidence",
        status="active",
        created=day,
        updated=day,
        confidence="medium",
        tags=["sycode-trading", "paper-mode", "analytics", "generated"],
        sources=list(ENDPOINTS.values()),
        source_job_id=JOB_ID,
        safety="paper-only-read-only",
        generated=True,
        generator="paper_analytics_report.py",
    )


def fixture_payloads() -> dict[str, Any]:
    return {
        "predictions": {"predictions": [{"model": "direction_quality", "resolved_outcome": "win"}, {"model": "direction_quality", "resolved_outcome": "loss"}]},
        "signals": {"active": 2, "approved": 1, "rejected": 1, "expired": 3, "total": 5, "approvalRate": 0.2},
        "strategies": {"strategies": [{"name": "Fixture strategy", "isEnabled": True, "mode": "paper"}]},
        "market": {"fearGreedIndex": 42, "fearGreedLabel": "FEAR", "marketRegime": "NEUTRAL", "regimeConfidence": 60, "btcDominance": 54.2, "altcoinSeasonIndex": 40, "dataQuality": "fixture"},
    }


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="paper-analytics-test-") as temporary:
        output = Path(temporary) / "analytics" / "2026-07-13.md"
        write_report(fixture_payloads(), output, dt.datetime(2026, 7, 13, 5, 7, tzinfo=dt.timezone.utc))
        text = output.read_text(encoding="utf-8")
        assert text.startswith("---\n") and text.count("---") == 2
        assert 'type: "task-evidence"' in text and "generated: true" in text
        assert 'safety: "paper-only-read-only"' in text
        assert "50.0%" in text and "Fixture strategy" in text
        assert not list(output.parent.glob(".*.incoming-*"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--wrapper", type=Path, default=DEFAULT_WRAPPER)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print(json.dumps({"status": "pass", "mode": "self-test"}))
        return 0
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    base = args.base_url.rstrip("/")
    child_environment = read_token_environment()
    payloads = {key: fetch_json(args.wrapper, base + endpoint, child_environment) for key, endpoint in ENDPOINTS.items()}
    output = args.output or DEFAULT_OUTPUT_DIR / f"{generated_at.date().isoformat()}.md"
    written = write_report(payloads, output, generated_at)
    print(json.dumps({"status": "written", "path": str(written), "generated_at": generated_at.isoformat(), "safety": "paper-only-read-only"}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
