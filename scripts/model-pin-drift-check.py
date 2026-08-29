#!/usr/bin/env python3
# model-pin-drift-check.py — fleet served-model pin drift monitor (kanban t_f21d5a0b).
#
# WHY (2026-08-03 repin, card t_f21d5a0b): the fleet was repinned (68 config files)
# to the DATED build deepseek/deepseek-v4-flash-0731 on nous because Nous ran a 7-day
# 90%-off promo on that specific build. A floating alias can drift onto a non-discounted
# build. This check reads session_model_usage across the fleet state DBs (read-only) and
# reports any (model, billing_provider) served inside the window that is NOT expected,
# excluding the DELIBERATE exceptions:
#     aux/judge/free tiers      : tencent/hy3:free and any model ending in ":free"
#                                 (stepfun/*:free, poolside/*:free, inclusionai/*:free, ...)
#     codex/grok/claude seats   : billing providers openai-codex, xai-oauth, anthropic,
#                                 gemini, copilot, ollama-cloud, custom, auto, "" (blank).
#     DECLARED per-profile pins : any model a profile's config.yaml declares as its
#                                 model.default or delegation.model (e.g. jarvis is
#                                 deliberately pinned to z-ai/glm-5.3-flash, jarvis-voice
#                                 to openai/gpt-5.6-luna). A config-declared default is a
#                                 DELIBERATE choice, not silent drift.
#     expected fleet pin        : deepseek/deepseek-v4-flash-0731 on nous.
#
# A row whose model IS the pin is accepted regardless of provider (blank/custom provider
# on the pin is a billing-attribution quirk, not drift — flagging it would flood alerts).
# A DIFFERENT model served on a nous billing provider that is neither a declared default
# nor an exception (e.g. the bare floating alias deepseek/deepseek-v4-flash, or an
# undeclared stealth/ox-alpha) IS drift: we are either paying list price or losing the
# discounted build — exactly the two failure classes the card names.
#
# CONTRACT:
#   * READ-ONLY — opens SQLite in immutable mode, never writes the DB.
#   * exit 0 = clean (no drift in window), exit 1 = drift found.
#   * machine-parseable summary line on stdout, prefixed "MODEL-PIN: ".
#   * Every handled path exits 0 or 1 deliberately. For cron wiring see the caller
#     (stack-health-audit.sh): the WRAPPER must exit 0 and signal via stdout/spool so a
#     --no-agent cron doesn't spam on every tick (kanban-audit-chain-monitor.sh pattern).
#   * Env overrides exist so a RED drill can point at a scratch copy / scratch window
#     without touching production:
#       MODEL_PIN_DB            space/comma/glob-separated state.db paths. Default:
#                               main ~/.hermes/state.db + all profiles/*/state.db
#                               (the fleet now records usage per-profile; the main DB
#                               went stale ~2026-08-18).
#       MODEL_PIN_EXPECTED      expected model (default deepseek/deepseek-v4-flash-0731)
#       MODEL_PIN_PROVIDER      expected billing provider (default nous)
#       MODEL_PIN_WINDOW_HOURS  window hours (default 168 = 7 days)
#       MODEL_PIN_EXTRA_ALLOW   extra allow tokens, comma-separated "model[|provider]"
#       MODEL_PIN_NO_CONFIG     set to 1 to skip reading config.yaml declared defaults
#                               (used to prove the checker flags a non-declared model)
#
# GOTCHA (proven 2026-08-03): a missing id in a disk model_metadata cache is NOT proof of
# absence — probe the id live before concluding a model doesn't exist. This checker only
# reads what was actually SERVED (recorded in session_model_usage); it never infers
# existence from a cache.
import os
import re
import sys
import glob
import time
import sqlite3

# Derive the fleet home from this script's own location (/home/frank/.hermes/scripts/)
# rather than trusting the ambient HERMES_HOME, which in a profile session is pinned to
# the profile dir (e.g. .../profiles/devops) — and that dir also contains a config.yaml,
# so an override can't be disambiguated reliably. The script location is authoritative.
# A RED drill overrides MODEL_PIN_DB instead (pointing at a scratch copy), never HERMES_HOME.
_HERE = os.path.dirname(os.path.abspath(__file__))
HERMES_HOME = os.path.dirname(_HERE)          # .../.hermes


def default_dbs():
    dbs = [os.path.join(HERMES_HOME, "state.db")]
    dbs += sorted(glob.glob(os.path.join(HERMES_HOME, "profiles/*/state.db")))
    return dbs


def env_split(key, default):
    v = os.environ.get(key)
    return v if v is not None else default


def expand_dbs(raw):
    out = []
    for tok in raw.replace(",", " ").split():
        out += glob.glob(tok)
    seen = set()
    uniq = []
    for p in out:
        p = os.path.abspath(p)
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            uniq.append(p)
    return uniq


# Lightweight extractor for the YAML keys that declare a deliberate model pin:
#   model:\n  default: <model>     (profile/root main model)
#   delegation:\n  model: <model>  (subagent/delegation model)
# Deliberate per-profile pins are NOT drift, so we fold them into the expected set.
_RE_DEFAULT = re.compile(r"^\s*default:\s*(.+?)\s*$")
_RE_DELEG_MODEL = re.compile(r"^\s*model:\s*(.+?)\s*$")


def declared_models():
    """Return set of models declared as deliberate pins across root+profile config.yaml."""
    out = set()
    configs = [os.path.join(HERMES_HOME, "config.yaml")]
    configs += sorted(glob.glob(os.path.join(HERMES_HOME, "profiles/*/config.yaml")))
    for cfg in configs:
        if not os.path.isfile(cfg):
            continue
        try:
            with open(cfg, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        # track top-level block we are inside; only accept model.default and
        # delegation.model (skip auxiliary.*.model which are aux, handled separately)
        block = None
        for raw in lines:
            line = raw.rstrip("\n")
            if line and not line[0].isspace() and not line.startswith("#"):
                block = line.rstrip(":")
            if block in ("model", "delegation"):
                m = _RE_DEFAULT.match(line) or _RE_DELEG_MODEL.match(line)
                if m and line.strip().split(":")[0] in ("default", "model"):
                    val = m.group(1).strip()
                    if val and not val.startswith(("#", "'", '"')):
                        out.add(val)
    return out


def read_rows(db_path, window_hours, now):
    """Return (rows, total) for one DB. rows = [(model, provider, last_seen)]. None if unreadable."""
    rows = []
    total = 0
    try:
        uri = "file:%s?mode=ro&immutable=1" % db_path
        con = sqlite3.connect(uri, uri=True, timeout=5)
        cur = con.cursor()
        cur.execute(
            "SELECT model, billing_provider, MAX(last_seen) "
            "FROM session_model_usage "
            "WHERE last_seen > ? "
            "GROUP BY model, billing_provider", (now - window_hours * 3600,))
        for model, provider, last_seen in cur.fetchall():
            total += 1
            rows.append((model, provider or "", last_seen))
        con.close()
    except (sqlite3.Error, OSError, ValueError):
        return None
    return rows, total


def is_acceptable(model, provider, expected_model, expected_provider,
                  declared, extra_allow):
    # expected pin model is fine on any provider (attribution quirk vs drift)
    if model == expected_model:
        return True
    # deliberate exception: aux/judge/free tiers
    if model == "tencent/hy3:free" or model.endswith(":free"):
        return True
    # deliberate exception: codex/grok/claude/gemini/copilot seats + blank/unattributed.
    # Provider-based (seat billing accounts) OR model-name-based (claude/codex/grok
    # seats can be billed through nous and still be the deliberate seat — e.g.
    # anthropic/claude-fable-5|nous is Frank's claude seat routed via nous).
    seat_providers = {
        "openai-codex", "xai-oauth", "anthropic", "gemini", "copilot",
        "ollama-cloud", "custom", "auto", "",
    }
    if provider in seat_providers:
        return True
    ml = model.lower()
    if "claude" in ml or "grok" in ml or ml.startswith("anthropic/"):
        return True
    if provider == "openai-codex" or ml.startswith("gpt-5") or "codex" in ml:
        return True
    # deliberate per-profile pins declared in config.yaml (not silent drift)
    if model in declared:
        return True
    # operator-declared allowances ("model" or "model|provider")
    for tok in extra_allow:
        if "|" in tok:
            m, p = tok.split("|", 1)
            if model == m and provider == p:
                return True
        elif model == tok:
            return True
    return False


def main():
    now = time.time()
    expected_model = env_split("MODEL_PIN_EXPECTED", "deepseek/deepseek-v4-flash-0731")
    expected_provider = env_split("MODEL_PIN_PROVIDER", "nous")
    try:
        window_hours = float(env_split("MODEL_PIN_WINDOW_HOURS", "168"))
    except ValueError:
        window_hours = 168.0
    raw_dbs = env_split("MODEL_PIN_DB", " ".join(default_dbs()))
    dbs = expand_dbs(raw_dbs)
    extra_allow = [t for t in env_split("MODEL_PIN_EXTRA_ALLOW", "").split(",") if t]
    declared = set()
    if os.environ.get("MODEL_PIN_NO_CONFIG") != "1":
        declared = declared_models()

    drift = []           # (db, model, provider, last_seen)
    scanned_dbs = []
    skipped_dbs = []
    total_rows = 0
    for db in dbs:
        res = read_rows(db, window_hours, now)
        if res is None:
            skipped_dbs.append(os.path.basename(db))
            continue
        scanned_dbs.append(db)
        rows, db_total = res
        total_rows += db_total
        for model, provider, last_seen in rows:
            if not is_acceptable(model, provider, expected_model, expected_provider,
                                 declared, extra_allow):
                drift.append((db, model, provider, last_seen))

    drift.sort(key=lambda r: r[3], reverse=True)
    if drift:
        print("MODEL-PIN: DRIFT window=%gh rows=%d scanned=%d drift=%d"
              % (window_hours, total_rows, len(scanned_dbs), len(drift)))
        for db, model, provider, last_seen in drift[:20]:
            print("  %s|%s served on nous-billed (db=%s, last=%s)"
                  % (model, provider, os.path.basename(db),
                     time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(last_seen))))
        if skipped_dbs:
            print("  (unreadable/missing table: %s)" % ", ".join(sorted(skipped_dbs)))
        return 1

    print("MODEL-PIN: CLEAN window=%gh rows=%d scanned=%d skipped=%d pin=%s|%s"
          % (window_hours, total_rows, len(scanned_dbs), len(skipped_dbs),
             expected_model, expected_provider))
    if skipped_dbs:
        print("  (skipped %d unreadable/missing-table DBs: %s)"
              % (len(skipped_dbs), ", ".join(sorted(skipped_dbs)[:5])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
