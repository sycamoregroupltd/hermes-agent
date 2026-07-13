#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies (the CANONICAL-COPY RULE, t_41acb465).
# Profile-local shims under ~/.hermes/profiles/<profile>/scripts/ execv() this file.
"""
Fleet fallback-chain validator — REWORK (t_58d8843f, supersedes t_ac67af00).

DAILY proof that every fallback rung on every profile is actually live —
not a flag, a real LLM completion via `hermes -p <profile> chat -q 'Reply OK'
--toolsets '' --provider <prov> -m <model>`.

CHANGES from v1 (t_ac67af00):
  1) CORE REDESIGN — rung isolation via billing_provider, not answer-box.
     After each probe, reads the profile's state.db to check the ACTUAL
     billing_provider and model. Only certifies LIVE if the served provider
     matches the pinned one — casts FALLBACK when a silent fallback occurred.
  2) False-positive DEAD_CONN — 'Auxiliary title generation failed: Connection
     error' is benign on HEALTHY runs. classify_output now checks LIVE reply
     FIRST, and scans DEAD markers ONLY on stderr (not the answer box region).
  3) SKIPPED (budget-exhausted) rungs never count as DEAD in any exit path.
  4) Wired to cron (dgx-fleet-chain-validator daily + sidecar drain).

Exit codes (cron-facing):
  0  — at least one rung probed and acceptable (all-green OR known-dead-only)
  1  — at least one expected-live rung is DEAD (drives the #critical-alerts sidecar)
  2  — harness failure (could not parse configs / hermes CLI missing)
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml  # PyYAML — hermes depends on it
except ImportError:  # pragma: no cover — hermes env always has it
    yaml = None

# Canonical paths — NOT derived from $HERMES_HOME (which points to a profile
# dir under cron/kanban contexts). Hardcode against Path.home() so the script
# is location-stable regardless of the dispatcher's env override.
HERMES_BIN = os.environ.get("HERMES_BIN", str(Path.home() / ".local" / "bin" / "hermes"))
PROFILES_DIR = Path.home() / ".hermes" / "profiles"
CRON_OUTPUT_DIR = Path.home() / ".hermes" / "profiles" / "jarvis" / "cron" / "output"

# Canonical smoke prompt — short, deterministic, forces a real completion.
SMOKE_PROMPT = "Reply with exactly the single word OK."
# How long a single rung probe may take before we call it TIMEOUT.
PER_RUNG_TIMEOUT_S = int(os.environ.get("FCV_RUNG_TIMEOUT", "120"))
# Total wall budget so the daily cron never runs away.
TOTAL_BUDGET_S = int(os.environ.get("FCV_TOTAL_BUDGET", "1800"))

# --- Liveness classification -------------------------------------------------

# Strings that indicate the request never reached the model (dead key / dead
# provider / resolver gave up). These appear in stderr or the banner BEFORE any
# model output. rc=0 in these cases is a known hermes quirk — the CLI exits 0
# after printing an auth/empty-key warning.
DEAD_KEY_MARKERS = (
    "Provider resolver returned an empty API key",
    "empty API key",
    "Provider resolver returned",       # any resolver abort
    "Goodbye!",                          # hermes exits without reaching model
    "Missing API key",
    "api key is required",
    "Unauthorized",                       # 401 surfaced as banner text
    "HTTP 401", "401 Unauthorized",
    "Invalid API key",
)
# Strings that indicate connectivity failure (DNS / refused / TLS / 5xx leak).
# IMPORTANT: only scanned against stderr, NOT the combined stdout+stderr, to
# avoid false-positives from benign "Auxiliary title generation failed:
# Connection error" messages that appear on healthy runs.
DEAD_CONN_MARKERS = (
    "Connection error",
    "Connection refused",
    "Connection reset",
    "Name or service not known",
    "Could not resolve",
    "HTTPSConnectionPool",
    "Max retries exceeded",
    "RemoteDisconnected",
    "EOF occurred",
)
# A genuine model reply — present in the answer box when the rung is live.
LIVE_REPLY_MARKERS = (
    "OK",            # our exact requested reply
)
# Garbage = rc=0 and no DEAD markers and no LIVE reply — suggests the rung
# answered with something irrelevant or an empty box.
GARBAGE_HINTS = (
    "⚠ Auxiliary title generation failed",   # benign: the answer box IS populated
    "⚠",
)

RUNG_STATES = ("LIVE", "FALLBACK", "DEAD_KEY", "DEAD_CONN", "TIMEOUT", "GARBAGE", "ERROR", "SKIPPED")

# Soft failures worth ONE retry before alerting (2026-07-12): free-tier rungs
# flake on single-shot probes — on 07-12 the stepfun rung returned GARBAGE on
# one profile while LIVE on 30+ other profiles in the same run, tripping an
# @here critical for a transient. DEAD_KEY is deterministic (resolver/auth) —
# retrying it would only waste budget, so it is excluded.
SOFT_RETRY_STATES = ("GARBAGE", "TIMEOUT", "DEAD_CONN", "ERROR")


@dataclass
class RungProbe:
    profile: str
    rung_index: int           # 0 = primary model.provider/default, 1+ = fallback_providers[i]
    provider: str
    model: str
    base_url: str | None = None
    state: str = "SKIPPED"
    duration_s: float = 0.0
    rc: int | None = None
    reply_snippet: str = ""
    failure_marker: str = ""
    is_primary: bool = False          # True for the model.* rung, False for fallback_providers[*]
    expected_live: bool = True        # unless overridden in EXPECTED_DEAD rungs
    # billing_provider evidence from state.db (populated after probe).
    actual_billing_provider: str | None = None
    actual_model: str | None = None

    def label(self) -> str:
        prim = "PRIMARY" if self.is_primary else f"FB#{self.rung_index}"
        return f"{self.profile}/{prim} {self.provider}/{self.model}"


@dataclass
class ScanReport:
    generated_at: str
    probes: list[RungProbe] = field(default_factory=list)

    @property
    def any_expected_live_dead(self) -> bool:
        """True when an expected-live rung is actually DEAD (not LIVE, FALLBACK,
        or SKIPPED). FALLBACK is not counted as dead — it means the pinned
        provider silently fell thru but something DID serve. SKIPPED is budget
        exhaustion, not a liveness problem."""
        return any(
            p.state not in ("LIVE", "FALLBACK", "SKIPPED") and p.expected_live
            for p in self.probes
        )

    @property
    def dead_rungs(self) -> list[RungProbe]:
        return [p for p in self.probes if p.state not in ("LIVE", "FALLBACK", "SKIPPED")]


# --- Rungs Frank expects to be live vs. accepts as known-dead ---------------
#
# Empty = every rung is expected_live. Add lower-cased "profile/provider/model"
# keys only for rungs that are KNOWN dead and should NOT trip the critical-alert
# (i.e. dead-but-expected). Any rung NOT in this set that comes back dead DOES
# trip #critical-alerts. Keeping this minimal is the point: silence is only safe
# when the dead rung is on this list.

EXPECTED_DEAD: set[tuple[str, str, str]] = set(
    {
        # Format: (profile_lower, provider_lower, model_lower)
        # Example that prompted this tool (left as documentation, not asserted):
        # ("jarvis", "custom", "openai/gpt-oss-120b")  # Groq key invalid — known dead today
    }
)


# --- Config parsing ---------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        # Minimal fallback: read as text, hand-parse the bits we need.
        return _minimal_config_parse(path)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _minimal_config_parse(path: Path) -> dict[str, Any]:
    """Hand-rolled fallback when PyYAML is unavailable. Only extracts the
    fallback_providers list and provider base_urls — enough for the validator."""
    cfg: dict[str, Any] = {"fallback_providers": [], "providers": {}, "model": {}}
    text = path.read_text(encoding="utf-8")
    # very rough — only used if PyYAML missing in a degenerate env
    cur_section: str | None = None
    cur_provider: str | None = None
    cur_fb: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and line.endswith(":"):
            cur_section = line[:-1]
            cur_provider = None
            cur_fb = None
            if cur_section == "fallback_providers":
                cfg["fallback_providers"] = []
        elif cur_section == "fallback_providers" and line.startswith("  - "):
            cur_fb = {}
            cfg["fallback_providers"].append(cur_fb)
        elif cur_section == "fallback_providers" and cur_fb is not None:
            m = re.match(r"\s*([\w]+):\s*(.+?)\s*$", line)
            if m:
                cur_fb[m.group(1)] = m.group(2).strip("'\"")
        elif cur_section == "providers" and line.startswith("  ") and not line.startswith("    "):
            m = re.match(r"\s*([\w-]+):\s*$", line)
            if m:
                cur_provider = m.group(1)
                cfg["providers"][cur_provider] = {}
        elif cur_section == "providers" and cur_provider:
            m = re.match(r"\s+([\w_]+):\s*(.+?)\s*$", line)
            if m:
                cfg["providers"][cur_provider][m.group(1)] = m.group(2).strip("'\"")
        elif cur_section == "model":
            m = re.match(r"\s+([\w_]+):\s*(.+?)\s*$", line)
            if m:
                cfg["model"][m.group(1)] = m.group(2).strip("'\"")
    return cfg


def extract_rungs(profile_name: str, config: dict[str, Any]) -> list[RungProbe]:
    """Build the full rung list for a profile: primary rung + each fallback_provider."""
    rungs: list[RungProbe] = []

    model_cfg = config.get("model", {}) or {}
    if isinstance(model_cfg, dict):
        provider = model_cfg.get("provider")
        default_model = model_cfg.get("default")
        if provider and default_model:
            # Resolve provider base_url from the providers map, model.base_url override, or None.
            base_url = model_cfg.get("base_url") or (config.get("providers", {}) or {}).get(provider, {}).get("base_url")
            rungs.append(RungProbe(
                profile=profile_name, rung_index=0,
                provider=str(provider), model=str(default_model),
                base_url=str(base_url) if base_url else None,
                is_primary=True,
            ))

    fb_list = config.get("fallback_providers") or []
    if isinstance(fb_list, list):
        for i, fb in enumerate(fb_list, start=1):
            if not isinstance(fb, dict):
                continue
            prov = fb.get("provider")
            mdl = fb.get("model")
            if not (prov and mdl):
                continue
            base_url = fb.get("base_url") or (config.get("providers", {}) or {}).get(prov, {}).get("base_url")
            rungs.append(RungProbe(
                profile=profile_name, rung_index=i,
                provider=str(prov), model=str(mdl),
                base_url=str(base_url) if base_url else None,
                is_primary=False,
            ))
    return rungs


def list_profiles() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(
        p.name
        for p in PROFILES_DIR.iterdir()
        if p.is_dir() and (p / "config.yaml").exists() and not p.name.startswith(".")
    )


# --- billing_provider verification (core redesign) --------------------------

def _get_latest_billing(state_db: Path, started_at_floor: float | None = None) -> tuple[str | None, str | None]:
    """Read the most recent session's billing_provider and model from a profile
    state.db. Returns (billing_provider, model) or (None, None) on any failure.

    Uses read-only URI mode to avoid WAL-lock conflicts with a running hermes
    process. The most recently created session is the probe we just ran."""
    if not state_db.is_file():
        return None, None
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        try:
            if started_at_floor is not None:
                row = conn.execute(
                    "SELECT billing_provider, model FROM sessions WHERE started_at >= ? ORDER BY started_at DESC LIMIT 1",
                    (started_at_floor - 2.0,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT billing_provider, model FROM sessions ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            return (row[0], row[1]) if row else (None, None)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None, None


# --- Smoke probe -----------------------------------------------------------

def classify_output(rc: int, stdout: str, stderr: str, timed_out: bool, snippet: str) -> tuple[str, str]:
    """Return (state, failure_marker). Separated stdout/stderr for precision:

    1. Check LIVE reply FIRST (from the answer-box snippet) before looking for
       DEAD markers. This prevents benign "Connection error" in auxiliary title
       generation from false-classifying healthy runs as DEAD_CONN.
    2. DEAD markers are scanned ONLY against stderr and the pre-answer text of stdout,
       ignoring any benign messages appearing after/outside the answer box.
    """
    if timed_out:
        return "TIMEOUT", "probe exceeded PER_RUNG_TIMEOUT"

    # Check LIVE reply FIRST — if the answer box has "OK", the rung is live
    # regardless of any aux warnings in stderr/stdout. Case-insensitive
    # (2026-07-12): models replying "Ok"/"ok" are live completions, not
    # garbage — a case-sensitive match misclassified them as GARBAGE and
    # tripped false @here criticals.
    if re.search(r"(?<![A-Za-z])OK(?![A-Za-z])", snippet, re.IGNORECASE):
        return "LIVE", ""

    # Get pre-answer text of stdout to exclude post-box warnings/errors
    matches = list(re.finditer(r"[─━::_]{6,}", stdout))
    if len(matches) >= 2:
        pre_answer_stdout = stdout[:matches[-2].start()]
    elif len(matches) == 1:
        pre_answer_stdout = stdout[:matches[0].start()]
    else:
        pre_answer_stdout = stdout

    # Combine stderr and pre-answer stdout to scan for DEAD markers
    scan_text = (stderr + "\n" + pre_answer_stdout).lower()

    for marker in DEAD_KEY_MARKERS:
        if marker.lower() in scan_text:
            return "DEAD_KEY", marker
    for marker in DEAD_CONN_MARKERS:
        if marker.lower() in scan_text:
            return "DEAD_CONN", marker

    # rc != 0 with no live marker and no known error → generic ERROR
    if rc != 0:
        return "ERROR", f"rc={rc} no live marker in stdout nor stderr"
    # rc == 0, no live marker, no known error marker → garbage / empty reply box
    return "GARBAGE", "no recognizable OK reply in answer box"


def _answer_snippet(combined: str) -> str:
    """Pull the content of the Hermes answer box so 'OK' matching targets the
    real reply, not the prompt echo or banner."""
    # The answer box ends with a divider like "───...───" or "─...─". Grab the
    # last box-delimited region; if none, fall back to the tail of combined.
    regions = re.split(r"[─━::_]{6,}", combined)
    if len(regions) >= 2:
        return regions[-2].strip()
    return combined.strip().splitlines()[-1].strip() if combined.strip() else ""


def probe_rung(rung: RungProbe) -> RungProbe:
    cmd = [
        HERMES_BIN, "-p", rung.profile, "chat",
        "-q", SMOKE_PROMPT, "--toolsets", "",
        "--provider", rung.provider, "-m", rung.model,
    ]
    started = datetime.datetime.now(datetime.timezone.utc)
    # Filter out our parent session ID so the subprocess is forced to start
    # a new session, ensuring we can isolate its state.db entries correctly.
    sub_env = {**os.environ, "SYSTEMD_PAGER": "cat", "PAGER": "cat"}
    sub_env.pop("HERMES_SESSION_ID", None)
    sub_env.pop("SESSION_ID", None)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=PER_RUNG_TIMEOUT_S,
            env=sub_env,
        )
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
        combined = stdout_text + "\n" + stderr_text
        snippet = _answer_snippet(combined)
        state, marker = classify_output(proc.returncode, stdout_text, stderr_text, False, snippet)
        rung.state = state
        rung.failure_marker = marker
        rung.rc = proc.returncode
        rung.reply_snippet = snippet[:160]

        # --- billing_provider verification (fix #1) ---
        # Only trust LIVE if the actual billing_provider matches the pinned
        # provider. A pinned probe can silently fall back (e.g. nvidia→nous)
        # while still returning "OK". Read the probe profile's state.db to
        # get ground truth.
        state_db = PROFILES_DIR / rung.profile / "state.db"
        actual_bp, actual_model = _get_latest_billing(state_db, started.timestamp())
        rung.actual_billing_provider = actual_bp
        rung.actual_model = actual_model

        if state == "LIVE":
            if actual_bp is None or actual_bp.lower() != rung.provider.lower():
                # The pinned provider did NOT serve — silent fallback detected.
                rung.state = "FALLBACK"
                rung.failure_marker = (
                    f"pinned_provider={rung.provider} but billing_provider={actual_bp or 'None'}"
                    + (f" actual_model={actual_model}" if actual_model else "")
                )

    except subprocess.TimeoutExpired:
        rung.state = "TIMEOUT"
        rung.failure_marker = f"timeout after {PER_RUNG_TIMEOUT_S}s"
        rung.rc = None
        rung.reply_snippet = ""
    except Exception as exc:  # pragma: no cover — defensive
        rung.state = "ERROR"
        rung.failure_marker = f"exception: {type(exc).__name__}: {exc}"[:240]
        rung.rc = None
    finally:
        rung.duration_s = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
    rung.expected_live = (
        # Resolve against EXPECTED_DEAD set membership.
        (rung.profile.lower(), rung.provider.lower(), rung.model.lower()) not in EXPECTED_DEAD
    )
    return rung


# --- Fixture mode (self-test) ----------------------------------------------

def _fixture_probes() -> list[RungProbe]:
    """Synthetic probes with known states — no live hermes invocation. Proves the
    classifier and the alert/able routing without spending tokens."""
    return [
        RungProbe(profile="pytest-prof", rung_index=0, provider="nvidia",
                  model="z-ai/glm-5.2", is_primary=True, state="LIVE",
                  actual_billing_provider="nvidia", actual_model="z-ai/glm-5.2",
                  reply_snippet="OK", duration_s=1.0, rc=0,
                  expected_live=True),
        RungProbe(profile="pytest-prof", rung_index=1, provider="nous",
                  model="deepseek/deepseek-v4-flash", state="LIVE",
                  actual_billing_provider="nous", actual_model="deepseek/deepseek-v4-flash",
                  reply_snippet="OK", duration_s=1.0, rc=0, expected_live=True),
        RungProbe(profile="pytest-prof", rung_index=2, provider="custom",
                  model="openai/gpt-oss-120b", state="DEAD_KEY",
                  failure_marker="Provider resolver returned an empty API key",
                  duration_s=0.2, rc=0, expected_live=True),
        RungProbe(profile="pytest-prof", rung_index=3, provider="nvidia",
                  model="deepseek-ai/deepseek-v4-pro", state="GARBAGE",
                  reply_snippet="", failure_marker="",  # empty answer box
                  duration_s=0.2, rc=0, expected_live=True),
        RungProbe(profile="pytest-prof", rung_index=4, provider="nvidia",
                  model="z-ai/glm-5.2", state="FALLBACK",
                  actual_billing_provider="nous", actual_model="deepseek/deepseek-v4-flash",
                  failure_marker="pinned_provider=nvidia but billing_provider=nous actual_model=deepseek/deepseek-v4-flash",
                  duration_s=1.0, rc=0, expected_live=True),
        RungProbe(profile="pytest-prof", rung_index=5, provider="nous",
                  model="deepseek/deepseek-v4-flash", state="SKIPPED",
                  failure_marker="total budget exhausted",
                  duration_s=0.0, rc=None, expected_live=True),
    ]


# --- Output formatting ------------------------------------------------------

def format_table(probes: Iterable[RungProbe]) -> str:
    lines = []
    lines.append("FLEET FALLBACK-CHAIN VALIDATOR — per-rung liveness")
    lines.append(f"generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    lines.append("")
    header = (f"{'PROFILE':<22} {'RUNG':<9} {'PROVIDER':<14} {'MODEL':<32} "
              f"{'STATE':<10} {'ACTUAL_BP':<12} {'EXP':<5} {'MARKER':<28}")
    lines.append(header)
    lines.append("-" * len(header))
    for p in probes:
        exp = "LIVE" if p.expected_live else "dead"
        marker = (p.failure_marker or "")[:28]
        lines.append(
            f"{p.profile:<22} {'PRIMARY' if p.is_primary else ('FB#'+str(p.rung_index)):<9} "
            f"{p.provider:<14} {p.model:<32} {p.state:<10} "
            f"{(p.actual_billing_provider or '-'):<12} {exp:<5} {marker:<28}"
        )
    return "\n".join(lines)


def format_critical_alert(dead_expected_live: list[RungProbe]) -> str:
    """Distinct block the #critical-alerts cron job forwards."""
    lines = []
    lines.append("@here FLEET CRITICAL — fallback rung(s) expected live are DEAD:")
    lines.append("")
    for p in dead_expected_live:
        lines.append(f"  • {p.label()}  state={p.state}  marker={p.failure_marker}")
        if p.actual_billing_provider:
            lines.append(f"    actual_billing_provider={p.actual_billing_provider} actual_model={p.actual_model or '-'}")
    lines.append("")
    lines.append("Investigate keys / provider status; this is the class that previously caused silent fleet outages.")
    return "\n".join(lines)


# --- Reporting sidecar for #critical-alerts ---------------------------------

ALERT_SIDECAR = CRON_OUTPUT_DIR / "fleet_chain_alert.txt"


def write_alert_sidecar(dead_expected_live: list[RungProbe]) -> Path | None:
    """The daily cron runs the validator once with deliver=#fleet-reports.
    Critical alerts go to a SEPARATE channel — write the alert text to a sidecar
    file. A second cron job (fleet_chain_alert_dispatcher) picks up the file
    and delivers its contents to #critical-alerts only when non-empty.
    Returns the path if an alert was written, None otherwise (silent when green)."""
    if not dead_expected_live:
        # Silent-when-green: remove stale sidecar so the dispatcher stays quiet.
        try:
            ALERT_SIDECAR.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    ALERT_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    ALERT_SIDECAR.write_text(format_critical_alert(dead_expected_live), encoding="utf-8")
    return ALERT_SIDECAR


# --- Main -------------------------------------------------------------------

def run_scan(profiles: list[str], fixture: bool = False) -> ScanReport:
    report = ScanReport(generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    budget_left = TOTAL_BUDGET_S
    # Duplicate-rung probe cache (2026-07-12): chains commonly repeat the same
    # (provider, model) at several rungs (e.g. PRIMARY == FB#1 on most fleet
    # profiles). Probing the identical target twice per profile only burns
    # budget and was a major driver of "total budget exhausted" SKIPPED
    # coverage holes. Deliberately keyed per-profile: provider keys are
    # profile-local (profiles/<name>/.env), so cross-profile reuse could mask
    # a missing key on one profile.
    probe_cache: dict[tuple[str, str, str, str], RungProbe] = {}

    if fixture:
        report.probes = _fixture_probes()
        return report

    for profile in profiles:
        cfg_path = PROFILES_DIR / profile / "config.yaml"
        if not cfg_path.exists():
            # Skip profiles with no config (e.g. `default` proxy profile)
            continue
        try:
            cfg = _load_yaml(cfg_path)
        except Exception as exc:  # pragma: no cover
            # Record a synthetic ERROR probe so the table still lists the profile.
            err = RungProbe(
                profile=profile, rung_index=0, provider="?", model="?",
                state="ERROR", failure_marker=f"config parse failed: {exc}"[:160],
            )
            report.probes.append(err)
            continue
        for rung in extract_rungs(profile, cfg):
            if budget_left <= 0:
                rung.state = "SKIPPED"
                rung.failure_marker = "total budget exhausted"
                report.probes.append(rung)
                continue

            cache_key = (profile, rung.provider.lower(), rung.model.lower(), rung.base_url or "")
            cached = probe_cache.get(cache_key)
            if cached is not None:
                # Identical (provider, model, base_url) already probed for this
                # profile this run — reuse the outcome, charge no budget.
                rung.state = cached.state
                rung.duration_s = 0.0
                rung.rc = cached.rc
                rung.reply_snippet = cached.reply_snippet
                rung.failure_marker = cached.failure_marker
                rung.actual_billing_provider = cached.actual_billing_provider
                rung.actual_model = cached.actual_model
                rung.expected_live = (
                    (rung.profile.lower(), rung.provider.lower(), rung.model.lower())
                    not in EXPECTED_DEAD
                )
                report.probes.append(rung)
                continue

            try:
                probe_rung(rung)
                # Single retry on soft failures — see SOFT_RETRY_STATES.
                if rung.state in SOFT_RETRY_STATES:
                    budget_left -= max(0, int(rung.duration_s))
                    if budget_left > 0:
                        first_state = rung.state
                        probe_rung(rung)
                        if rung.state in ("LIVE", "FALLBACK"):
                            rung.failure_marker = (
                                f"{rung.failure_marker} recovered-on-retry (first={first_state})"
                            ).strip()[:160]
                        else:
                            rung.failure_marker = (
                                f"{rung.failure_marker} [persisted; first={first_state}]"
                            ).strip()[:160]
            finally:
                budget_left -= max(0, int(rung.duration_s))
            probe_cache[cache_key] = rung
            report.probes.append(rung)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fleet fallback-chain validator")
    parser.add_argument("--profile", action="append", default=None,
                        help="restrict to one or more profiles (repeatable)")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in fixture instead of probing the live fleet")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    args = parser.parse_args(argv)

    if args.self_test:
        report = run_scan([], fixture=True)
    else:
        if not Path(HERMES_BIN).exists():
            print(f"fleet_chain_validator: hermes not found at {HERMES_BIN}", file=sys.stderr)
            return 2
        profiles = args.profile if args.profile else list_profiles()
        if not profiles:
            print("fleet_chain_validator: no profiles found", file=sys.stderr)
            return 2
        # Deterministic daily rotation (2026-07-12): under budget exhaustion
        # the tail of the alphabetical profile list was NEVER probed (every
        # daily run started at 'builder' and exhausted around the same point,
        # leaving sycode-*/trading-*/upero-*/yorkstone-* rungs permanently
        # SKIPPED — a silent coverage hole). Rotating the start offset by day
        # guarantees every profile is probed within a few days even when one
        # run's budget cannot cover the whole fleet. Explicit --profile runs
        # are never rotated.
        if args.profile is None and len(profiles) > 1:
            offset = datetime.date.today().toordinal() % len(profiles)
            profiles = profiles[offset:] + profiles[:offset]
        report = run_scan(profiles)

    if args.json:
        payload = {
            "generated_at": report.generated_at,
            "probes": [asdict(p) for p in report.probes],
            "any_expected_live_dead": report.any_expected_live_dead,
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(format_table(report.probes))

    # Fixture self-test: assert all fixture states are correctly classified.
    if args.self_test:
        states = {p.rung_index: p.state for p in report.probes}
        assert states[0] == "LIVE", f"live rung 0 misclassified: {states}"
        assert states[1] == "LIVE", f"live rung 1 misclassified: {states}"
        assert states[2] == "DEAD_KEY", f"fixture dead-key rung not flagged: {states}"
        assert states[3] == "GARBAGE", f"garbage rung misclassified: {states}"
        assert states[4] == "FALLBACK", f"fallback rung not flagged: {states}"
        assert states[5] == "SKIPPED", f"skipped rung misclassified: {states}"
        print("\nSELF-TEST PASS: fixture dead rung flagged, live rungs silent-correct, "
              "garbage classified, fallback detected, skipped ignored.")
        # Self-test always exits 0 — the fixture's DEAD_KEY is intentional test data.
        return 0

    # Write the alert sidecar (silent when green) for the live path only.
    if not args.self_test and not args.json:
        dead_expected_live = [p for p in report.probes if p.state not in ("LIVE", "FALLBACK", "SKIPPED") and p.expected_live]
        write_alert_sidecar(dead_expected_live)
        if dead_expected_live:
            print("\n" + format_critical_alert(dead_expected_live))
            return 1
        # all-green silent path
        return 0
    return 0 if not report.any_expected_live_dead else 1


if __name__ == "__main__":
    sys.exit(main())
