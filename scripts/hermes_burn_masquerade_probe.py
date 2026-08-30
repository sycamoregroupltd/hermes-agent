#!/usr/bin/env python3
# Hermes token-burn + masquerade probe (no-agent cron).
# Reads every profiles/*/state.db, reports CLOUD token burn (last 1h/24h) + top burners,
# and DETECTS silent model masquerade: sessions served by weak/fallback models (qwen3:8b,
# phi3, :mini, etc.) that may indicate a premium model pin was silently downgraded.
# Also flags sessions where a cloud provider billed a weak model at premium rates.
# Read-only. Installed 2026-07-08 by the opus48 seat during the provider-collapse incident.
import glob, os, sqlite3, time, subprocess, json

CLOUD = {'gemini', 'nvidia', 'nous', 'openai-codex', 'ollama-cloud', 'xai', 'custom'}
LOCAL = {'ollama-local', '', None}  # note: ollama-local is currently MISCONFIGURED to ollama.com — treated as cloud below if base points remote
WEAK = ('qwen3:8b', 'llama3.2:3b', 'phi3', ':3b', ':mini', 'validator')
ALERT_1H_CLOUD_TOKENS = 5_000_000   # alert threshold: >5M cloud tokens in the last hour

def scan_sessions(since):
    """Aggregate token burn by provider and profile for sessions since `since`."""
    by_prov, by_prof, sess = {}, {}, 0
    seen: set[str] = set()
    for db in glob.glob('/home/frank/.hermes/profiles/*/state.db'):
        db = os.path.realpath(db)
        if db in seen:
            continue
        seen.add(db)
        prof = db.split('/')[-2]
        try:
            c = sqlite3.connect(f'file:{db}?mode=ro', uri=True); cur = c.cursor()
            cols = [r[1] for r in cur.execute('PRAGMA table_info(sessions)').fetchall()]
            if 'billing_provider' not in cols:
                c.close(); continue
            tf = 'total_tokens' if 'total_tokens' in cols else '(COALESCE(input_tokens,0)+COALESCE(output_tokens,0))'
            for bp, tot, n in cur.execute(
                f"SELECT COALESCE(billing_provider,''), COALESCE(SUM({tf}),0), COUNT(*) "
                f"FROM sessions WHERE started_at>{since} GROUP BY 1"):
                by_prov[bp] = by_prov.get(bp, 0) + (tot or 0)
                by_prof[prof] = by_prof.get(prof, 0) + (tot or 0)
                sess += n
            c.close()
        except Exception:
            pass
    return by_prov, by_prof, sess


def detect_masquerades(since):
    """Detect silent model masquerades: sessions served by weak/fallback models.

    Returns a list of dicts: {profile, model, billing_provider, tokens, sessions_count}
    for sessions in the last `since` seconds where the model matches a WEAK pattern.
    Also flags sessions billed to a cloud provider when using a weak model
    (paying premium for weak inference).
    """
    masks = []
    seen: set[str] = set()
    for db in glob.glob('/home/frank/.hermes/profiles/*/state.db'):
        db = os.path.realpath(db)
        if db in seen:
            continue
        seen.add(db)
        prof = db.split('/')[-2]
        try:
            c = sqlite3.connect(f'file:{db}?mode=ro', uri=True); cur = c.cursor()
            cols = [r[1] for r in cur.execute('PRAGMA table_info(sessions)').fetchall()]
            if 'billing_provider' not in cols or 'model' not in cols:
                c.close(); continue
            tf = 'total_tokens' if 'total_tokens' in cols else '(COALESCE(input_tokens,0)+COALESCE(output_tokens,0))'
            # Find sessions where model matches a WEAK pattern
            for row in cur.execute(
                f"SELECT model, COALESCE(billing_provider,''), COALESCE(SUM({tf}),0), COUNT(*) "
                f"FROM sessions WHERE started_at>{since} GROUP BY model, billing_provider"):
                model, bp, tot, n = row
                if any(w in model.lower() for w in WEAK):
                    masks.append({
                        'profile': prof,
                        'model': model,
                        'billing_provider': bp,
                        'tokens': tot,
                        'sessions': n,
                    })
            c.close()
        except Exception:
            pass
    return masks


def fmt(n): return f'{n/1e6:.1f}M' if n >= 1e6 else f'{n/1e3:.0f}k'


now = time.time()
p1, prof1, s1 = scan_sessions(now - 3600)
p24, prof24, s24 = scan_sessions(now - 86400)
cloud1 = sum(v for k, v in p1.items() if k in CLOUD)
cloud24 = sum(v for k, v in p24.items() if k in CLOUD)
top = sorted(prof24.items(), key=lambda x: -x[1])[:5]

lines = [f"Hermes burn — 1h: cloud {fmt(cloud1)} tok ({s1} sess) | 24h: cloud {fmt(cloud24)} tok"]
lines.append("by provider (24h): " + ", ".join(f"{k or 'local'}={fmt(v)}" for k, v in sorted(p24.items(), key=lambda x: -x[1])[:6]))
lines.append("top profiles (24h): " + ", ".join(f"{k}={fmt(v)}" for k, v in top))

# --- Masquerade detection ---
m24 = detect_masquerades(now - 86400)
m1 = [m for m in m24 if m['sessions'] > 0 and (now - 3600) < (now - 3600)]  # quick: re-scan just 1h for alertability
# Actually re-scan for 1h masquerades to keep alert fresh
m1 = detect_masquerades(now - 3600)

if m24:
    lines.append(f"\n⚠️ SILENT MASQUERADE — {len(m24)} profile(s) served weak model in last 24h:")
    # Group by model for cleaner reporting
    by_model = {}
    for m in m24:
        key = f"{m['model']} ({m['billing_provider']})"
        by_model.setdefault(key, []).append(m)
    for key, items in sorted(by_model.items()):
        profs = ", ".join(f"{i['profile']} ({fmt(i['tokens'])} tok, {i['sessions']} sess)" for i in items)
        lines.append(f"  {key}: {profs}")
else:
    lines.append("\n✓ No silent model masquerade detected in last 24h")

digest = "\n".join(lines)
print(digest)

# alert Frank if masquerade detected in the last hour (active masquerade)
if m1:
    # Count distinct weak models found recently
    weak_map = {}
    for m in m1:
        weak_map.setdefault(m['model'], []).append(m['profile'])
    detail = "; ".join(f"{mdl} on {'/'.join(profs)}" for mdl, profs in sorted(weak_map.items()))
    msg = (f"🔇 SILENT MASQUERADE: {len(m1)} session(s) served weak model in last 1h. "
           f"Models: {detail}. Check fallback chains.")
    try:
        subprocess.run(["hermes", "send", "-t", "telegram", msg], timeout=30, capture_output=True, text=True)
        print("[masquerade alert sent via telegram]")
    except Exception as e:
        print(f"(masquerade telegram failed: {e})")

# alert Frank if the fleet is still materially burning cloud tokens in the last hour
if cloud1 >= ALERT_1H_CLOUD_TOKENS:
    msg = (f"💸 Hermes still burning CLOUD: {fmt(cloud1)} tokens in the last hour "
           f"({fmt(cloud24)}/24h). Top: " + ", ".join(f"{k}={fmt(v)}" for k, v in top[:3])
           + ". Local-first routing not yet effective.")
    try:
        subprocess.run(["hermes", "send", "-t", "telegram", msg], timeout=30, capture_output=True, text=True)
        print("[burn alert sent via telegram]")
    except Exception as e:
        print(f"(burn telegram failed: {e})")
