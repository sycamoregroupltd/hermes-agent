#!/usr/bin/env python3
"""Audit Hermes fleet profiles against Jarvis native baseline.

Script-only watchdog semantics:
- Writes markdown + JSON reports under /home/frank/uaa-rules/.
- Prints only when actionable drift exists; empty stdout means no drift.
- Read-only: does not modify profile configs or credentials.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception as exc:  # pragma: no cover
    print(f"FLEET PROFILE BASELINE AUDIT ERROR: PyYAML unavailable: {exc}")
    raise SystemExit(2)

ROOT = Path("/home/frank")
HERMES = ROOT / ".hermes"
PROFILES = HERMES / "profiles"
OUT_MD = ROOT / "uaa-rules" / "FLEET-PROFILE-CONFIG-AUDIT.md"
OUT_JSON = ROOT / "uaa-rules" / "fleet-profile-config-audit.json"
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

CORE_PROFILE_HINTS = {
    "jarvis",
    "jarvis-os-pm",
    "sycode-ai-pm",
    "sycode-trading-pm",
    "upero-pm",
    "guardian",
    "os-reviewer",
    "self-improve-engineer",
    "nervous-system-engineer",
    "system-optimizer",
    "workforce-scaler",
}

CODE_ROLE_TOKENS = (
    "builder",
    "integrator",
    "devops",
    "architect",
    "reviewer",
    "engineer",
    "migrator",
)
PM_ROLE_TOKENS = ("-pm", "pm")

OPTIONAL_CAPABILITY_ENVS = {
    "github_token": ["GITHUB_TOKEN"],
    "browserbase": ["BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"],
    "browser_use": ["BROWSER_USE_API_KEY"],
    "fal_image_video": ["FAL_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"],
    "nous_portal": ["NOUS_BASE_URL"],
}


def get(data: dict[str, Any], path: str, default: Any = "<missing>") -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_yaml(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        data = yaml.safe_load(path.read_text())
        return (data if isinstance(data, dict) else {}), None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def is_code_profile(name: str) -> bool:
    return any(tok in name for tok in CODE_ROLE_TOKENS)


def is_core_profile(name: str) -> bool:
    return name in CORE_PROFILE_HINTS or name.endswith("-pm")


def env_key_present(profile: str, key: str) -> bool:
    # Do not print or parse secret values. Presence-only across profile and root .env.
    candidates = [PROFILES / profile / ".env", HERMES / ".env"]
    needle = key + "="
    for path in candidates:
        if not path.exists():
            continue
        try:
            for line in path.read_text(errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith(needle) and stripped[len(needle):].strip():
                    return True
        except Exception:
            continue
    return False


profiles: list[dict[str, Any]] = []
findings: list[dict[str, Any]] = []
summary: dict[str, Counter[str]] = defaultdict(Counter)

for cfg in sorted(PROFILES.glob("*/config.yaml")):
    name = cfg.parent.name
    data, err = load_yaml(cfg)
    row: dict[str, Any] = {
        "profile": name,
        "path": str(cfg),
        "yaml_error": err,
        "core": is_core_profile(name),
        "code_profile": is_code_profile(name),
        "model": get(data, "model.default"),
        "provider": get(data, "model.provider"),
        "compression_in_place": get(data, "compression.in_place"),
        "compression_abort_on_summary_failure": get(data, "compression.abort_on_summary_failure"),
        "compression_codex_gpt55_autoraise": get(data, "compression.codex_gpt55_autoraise"),
        "fallback_providers": get(data, "fallback_providers", []),
        "checkpoints_enabled": get(data, "checkpoints.enabled"),
        "approvals_mode": get(data, "approvals.mode"),
        "kanban_dispatch_in_gateway": get(data, "kanban.dispatch_in_gateway"),
        "delegation_max_async_children": get(data, "delegation.max_async_children"),
        "curator_consolidate": get(data, "curator.consolidate"),
    }
    profiles.append(row)
    for key in [
        "provider",
        "compression_in_place",
        "compression_abort_on_summary_failure",
        "compression_codex_gpt55_autoraise",
        "checkpoints_enabled",
        "approvals_mode",
        "kanban_dispatch_in_gateway",
        "delegation_max_async_children",
        "curator_consolidate",
    ]:
        summary[key][json.dumps(row[key], sort_keys=True, default=str)] += 1
    summary["fallback_empty"]["true" if not row["fallback_providers"] else "false"] += 1

    if err:
        findings.append({"severity": "ERROR", "profile": name, "issue": "invalid config yaml", "detail": err, "path": str(cfg)})
        continue

    # Proven safe native defaults to consider. Missing/false is drift, not immediate breakage.
    if row["core"] or row["code_profile"]:
        if row["compression_in_place"] is not True:
            findings.append({"severity": "WARN", "profile": name, "issue": "compression.in_place not enabled", "detail": "v0.17 in-place compaction avoids session-id churn/search gaps", "path": str(cfg)})
        if row["compression_abort_on_summary_failure"] is not True:
            findings.append({"severity": "WARN", "profile": name, "issue": "compression.abort_on_summary_failure not enabled", "detail": "safer behavior preserves context on aux summary failure", "path": str(cfg)})
        if not row["fallback_providers"]:
            findings.append({"severity": "WARN", "profile": name, "issue": "fallback_providers empty", "detail": "provider/rate/auth failures will not use configured fallback chain", "path": str(cfg)})
    if row["code_profile"] and row["checkpoints_enabled"] is not True:
        findings.append({"severity": "INFO", "profile": name, "issue": "checkpoints disabled on code-writing profile", "detail": "native /rollback safety net is not enabled", "path": str(cfg)})

# Optional capability gap summary for Jarvis only; credentials remain human-gated.
capability_status: dict[str, bool] = {}
for cap, keys in OPTIONAL_CAPABILITY_ENVS.items():
    capability_status[cap] = all(env_key_present("jarvis", key) for key in keys)

counts = Counter(f["severity"] for f in findings)
profiles_by_issue: dict[str, list[str]] = defaultdict(list)
for f in findings:
    profiles_by_issue[f["issue"]].append(f["profile"])

payload = {
    "generated": NOW,
    "profile_count": len(profiles),
    "counts": dict(counts),
    "summary": {k: dict(v) for k, v in summary.items()},
    "capability_status": capability_status,
    "findings": findings,
    "profiles": profiles,
}
OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

lines = [
    "# Fleet Profile Config Audit",
    "",
    f"Generated: {NOW}",
    f"Profiles scanned: {len(profiles)}",
    "",
    "## Finding Counts",
]
for sev in ["ERROR", "WARN", "INFO"]:
    lines.append(f"- {sev}: {counts.get(sev, 0)}")

lines += ["", "## Optional Capability Presence (Jarvis/root env, presence-only)"]
for cap, ok in sorted(capability_status.items()):
    lines.append(f"- {cap}: {'configured' if ok else 'missing'}")

lines += ["", "## Config Distribution"]
for key in [
    "provider",
    "compression_in_place",
    "compression_abort_on_summary_failure",
    "compression_codex_gpt55_autoraise",
    "fallback_empty",
    "checkpoints_enabled",
    "approvals_mode",
    "kanban_dispatch_in_gateway",
    "delegation_max_async_children",
    "curator_consolidate",
]:
    lines.append(f"### {key}")
    for val, n in summary[key].most_common(10):
        lines.append(f"- {val}: {n}")
    lines.append("")

lines += ["## Findings by Issue"]
if not findings:
    lines.append("- none")
else:
    for issue, profs in sorted(profiles_by_issue.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        preview = ", ".join(profs[:25])
        suffix = f" … +{len(profs)-25} more" if len(profs) > 25 else ""
        lines.append(f"- {issue}: {len(profs)} profile(s): {preview}{suffix}")

lines += ["", "## Recommended Rollout Order"]
lines += [
    "1. Core PM/reviewer profiles: enable compression hardening + fallback after backup.",
    "2. Code-writing profiles: enable checkpoints in addition to compression/fallback.",
    "3. Generated/test profile families: review for archival or baseline sync; do not mutate blindly.",
    "4. Optional credentials: configure only with Frank-provided tokens/OAuth approval; this audit reports presence only.",
]
OUT_MD.write_text("\n".join(lines) + "\n")

# Watchdog output: only actionable drift, not full report.
if counts.get("ERROR", 0) or counts.get("WARN", 0):
    print(f"Fleet profile baseline audit: ERROR={counts.get('ERROR',0)}, WARN={counts.get('WARN',0)}, INFO={counts.get('INFO',0)}, report={OUT_MD}")
    for issue, profs in sorted(profiles_by_issue.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:8]:
        print(f"- {issue}: {len(profs)} profile(s)")
