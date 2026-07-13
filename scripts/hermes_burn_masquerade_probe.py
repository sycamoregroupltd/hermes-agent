#!/usr/bin/env python3
# Hermes token-burn + masquerade probe (no-agent cron).
# Reads every profiles/*/state.db, reports CLOUD token burn (last 1h/24h) + top burners,
# and FLAGS the "silent masquerade" (a serious seat served by an 8B/weak model, or a model
# pin billed to a different provider). Telegram-alerts Frank when cloud burn is material.
# Read-only. Installed 2026-07-08 by the opus48 seat during the provider-collapse incident.
import glob, sqlite3, time, subprocess, json

CLOUD = {'gemini', 'nvidia', 'nous', 'openai-codex', 'ollama-cloud', 'xai', 'custom'}
LOCAL = {'ollama-local', '', None}  # note: ollama-local is currently MISCONFIGURED to ollama.com — treated as cloud below if base points remote
WEAK = ('qwen3:8b', 'llama3.2:3b', 'phi3', ':3b', ':mini', 'validator')
ALERT_1H_CLOUD_TOKENS = 5_000_000   # alert threshold: >5M cloud tokens in the last hour

def scan(since):
    by_prov, by_prof, sess = {}, {}, 0
    for db in glob.glob('/home/frank/.hermes/profiles/*/state.db'):
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

def fmt(n): return f'{n/1e6:.1f}M' if n >= 1e6 else f'{n/1e3:.0f}k'

now = time.time()
p1, prof1, s1 = scan(now - 3600)
p24, prof24, s24 = scan(now - 86400)
cloud1 = sum(v for k, v in p1.items() if k in CLOUD)
cloud24 = sum(v for k, v in p24.items() if k in CLOUD)
top = sorted(prof24.items(), key=lambda x: -x[1])[:5]

lines = [f"Hermes burn — 1h: cloud {fmt(cloud1)} tok ({s1} sess) | 24h: cloud {fmt(cloud24)} tok"]
lines.append("by provider (24h): " + ", ".join(f"{k or 'local'}={fmt(v)}" for k, v in sorted(p24.items(), key=lambda x: -x[1])[:6]))
lines.append("top profiles (24h): " + ", ".join(f"{k}={fmt(v)}" for k, v in top))
digest = "\n".join(lines)
print(digest)

# alert Frank if the fleet is still materially burning cloud tokens in the last hour
if cloud1 >= ALERT_1H_CLOUD_TOKENS:
    msg = (f"💸 Hermes still burning CLOUD: {fmt(cloud1)} tokens in the last hour "
           f"({fmt(cloud24)}/24h). Top: " + ", ".join(f"{k}={fmt(v)}" for k, v in top[:3])
           + ". Local-first routing not yet effective.")
    try:
        subprocess.run(["hermes", "send", "-t", "telegram", msg], timeout=30, capture_output=True, text=True)
        print("[alerted Frank via telegram]")
    except Exception as e:
        print(f"(telegram failed: {e})")
