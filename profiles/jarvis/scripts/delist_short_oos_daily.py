#!/usr/bin/env python3
"""delist-short-oos-daily — D-v2 forward-accrual shadow harness (SHADOW ONLY, no execution).

Kanban t_a4cbc566; ledger row 25 (H5v2-EVENT-SHORTS-TAIL-CONTROL).
Prereg (FROZEN regime, no re-tuning — any parameter change = new prereg + ledger row):
  strategies/pre-registrations/2026-08-02-event-shorts-v2-tail-control-prereg-fable.md
Results note: research/2026-08-02-h5v2-tail-control-results-fable.md
D-v2 verdict was WEAK near-CONFIRM: only DSR fails (0.933 @x2 slip vs 0.95 bar).
The preregistered cure is FORWARD EVENT ACCRUAL at the frozen regime. This cron:

  1. Polls Binance CMS catalog 161 for new "Binance Will Delist ... on YYYY-MM-DD"
     spot-delisting batch announcements (ms releaseDate), dedupes vs the frozen H5
     catalog + persisted forward catalog.
  2. For each new token-event with a still-listed liquid perp (frozen venue priority
     HL > Bybit > Binance-perp, entry-day $vol >= $1M), scores the frozen regime once
     the 72h window completes: entry = open of first 5m bar >= ann+30min; hard stop
     +2,000bps adverse filled at max(stop, crossing-bar CLOSE) + slippage {50,100,200}bps
     (headline 100); funding-exit when cumulative paid > 750bps; isolated-1x floor
     -10,000bps; exit priority earliest; net of 16bps taker RT (stress 28).
  3. Full idempotent recompute each run; appends nothing blindly — the whole forward
     ledger is rescored from immutable inputs (venue APIs + frozen artifacts).
  4. Recomputes the family DSR (N=16 charged trials, per prereg) on the pooled
     frozen-53 + forward-scored per-event nets @x2 slip, plus week-clustered
     bootstrap CI. When DSR >= 0.95 AND pooled week-CI excludes 0, emits:
       "D-V2 PROMOTION BAR CROSSED — assemble packet"
     (packet assembly is seat work, not this cron's.)

no_agent contract: stdout EMPTY on a clean day (no new events, nothing scored, no
faults). Event/alert lines otherwise. Any operational failure (CMS unreachable,
kline refetch failure on a completed window) reaches the EXIT CODE — a missing day
is MISSING, never fabricated. Liveness is the exit code, never softened stdout.

Test hook: env DELIST_OOS_CMS_URL overrides the CMS endpoint (point at an
unreachable host to verify the fail path exits non-zero).

Artifacts (vault-autocommitted):
  research/artifacts/h5v2-oos-2026-08-02/{oos_event_ledger.csv,status.json,
                                          oos_event_catalog.json}
"""
import csv
import datetime
import io
import json
import math
import os
import random
import re
import statistics as st
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/home/frank/.hermes/scripts")
from second_brain_writer import write_text_atomic

VAULT = "/home/frank/obsidian/sycode-trading"
ART = os.environ.get("DELIST_OOS_ART_DIR",
                     f"{VAULT}/research/artifacts/h5v2-oos-2026-08-02")
FROZEN_V2 = f"{VAULT}/research/artifacts/h5v2-tail-control-2026-08-02/dv2_legs_raw.json"
FROZEN_CAT = f"{VAULT}/research/artifacts/h5-event-shorts-2026-08-02/delist_events_parsed.json"

# --- frozen regime (prereg 2026-08-02, ledger row 25 — DO NOT TUNE) -----------
H72 = 72 * 3600 * 1000
STOP_BPS = 2000.0
FUND_EXIT_BPS = 750.0
LIQ_FLOOR = -10000.0
SLIPS = [50.0, 100.0, 200.0]      # stop-fill slippage stress; 100 = headline (x2)
COST_RT = 16.0                    # taker RT bps (stress 28 reported)
MIN_VOL24_USD = 1_000_000
VENUE_PRIORITY = ("hyperliquid", "bybit", "binance_perp")
# --- inference (frozen: analyze_v2.py conventions) ----------------------------
N_TRIALS = 16                     # DSR family charge per prereg
B = 2000
DSR_BAR = 0.95
SETTLE_BUFFER_MS = 2 * 3600 * 1000  # score only after window + 2h (funding settled)

# endpoint override (test hook): when set, NO fallback — the fail path must fail.
if "DELIST_OOS_CMS_URL" in os.environ:
    CMS_ENDPOINTS = [os.environ["DELIST_OOS_CMS_URL"]]
else:
    CMS_ENDPOINTS = [
        "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"]
CMS_PAGES = 3
CMS_PAGE_SIZE = 50

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0'}
TITLE_RE = re.compile(r'^Binance Will Delist\s+(.+?)\s+on\s+(\d{4}-\d{2}-\d{2})')

alerts = []        # operational faults -> exit 1
events_out = []    # informational lines (new events / newly scored) -> exit 0


# ---------------------------------------------------------------- http helpers
def _get(url, tries=3, timeout=25):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))


def _post(url, payload, tries=3, timeout=25):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={**UA, 'Content-Type': 'application/json'})
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))


# ------------------------------------------------- venue fetchers (venue_lib)
def binance_klines(base, start_ms, end_ms, interval='5m'):
    out, cur = [], start_ms
    for _ in range(40):
        d = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={base}USDT&interval={interval}"
                 f"&startTime={cur}&endTime={end_ms}&limit=1500")
        if not d or isinstance(d, dict):
            break
        out += [{'t': r[0], 'o': float(r[1]), 'h': float(r[2]), 'l': float(r[3]),
                 'c': float(r[4]), 'qv': float(r[7])} for r in d]
        if len(d) < 1500:
            break
        cur = d[-1][0] + 1
        time.sleep(0.25)
    return out


def bybit_klines(base, start_ms, end_ms, interval='5'):
    out, cur = [], start_ms
    for _ in range(40):
        d = _get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={base}USDT"
                 f"&interval={interval}&start={cur}&end={end_ms}&limit=1000")
        if not d or d.get('retCode') != 0:
            break
        rows = d['result']['list']
        if not rows:
            break
        rows = sorted(rows, key=lambda r: int(r[0]))
        out += [{'t': int(r[0]), 'o': float(r[1]), 'h': float(r[2]), 'l': float(r[3]),
                 'c': float(r[4]), 'qv': float(r[6])} for r in rows]
        nxt = int(rows[-1][0]) + 1
        if nxt >= end_ms or len(rows) < 1000:
            break
        cur = nxt
        time.sleep(0.25)
    dedup = {r['t']: r for r in out}
    return [dedup[t] for t in sorted(dedup)]


def hl_klines(base, start_ms, end_ms, interval='5m'):
    d = _post("https://api.hyperliquid.xyz/info",
              {'type': 'candleSnapshot', 'req': {'coin': base, 'interval': interval,
                                                 'startTime': start_ms, 'endTime': end_ms}})
    if not d or not isinstance(d, list):
        return []
    return [{'t': r['t'], 'o': float(r['o']), 'h': float(r['h']), 'l': float(r['l']),
             'c': float(r['c']), 'qv': float(r['v']) * float(r['c'])} for r in d]


def binance_funding(base, start_ms, end_ms):
    d = _get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={base}USDT"
             f"&startTime={start_ms}&endTime={end_ms}&limit=1000")
    if not d or isinstance(d, dict):
        return []
    return [{'t': r['fundingTime'], 'rate': float(r['fundingRate'])} for r in d]


def bybit_funding(base, start_ms, end_ms):
    out, end = [], end_ms
    for _ in range(10):
        d = _get(f"https://api.bybit.com/v5/market/funding/history?category=linear"
                 f"&symbol={base}USDT&startTime={start_ms}&endTime={end}&limit=200")
        if not d or d.get('retCode') != 0:
            break
        rows = d['result']['list']
        if not rows:
            break
        out += [{'t': int(r['fundingRateTimestamp']), 'rate': float(r['fundingRate'])} for r in rows]
        oldest = min(int(r['fundingRateTimestamp']) for r in rows)
        if oldest <= start_ms or len(rows) < 200:
            break
        end = oldest - 1
        time.sleep(0.25)
    dedup = {r['t']: r for r in out}
    return [dedup[t] for t in sorted(dedup)]


def hl_funding(base, start_ms, end_ms):
    out, cur = [], start_ms
    for _ in range(20):
        d = _post("https://api.hyperliquid.xyz/info",
                  {'type': 'fundingHistory', 'coin': base, 'startTime': cur, 'endTime': end_ms})
        if not d or not isinstance(d, list) or not d:
            break
        out += [{'t': r['time'], 'rate': float(r['fundingRate'])} for r in d]
        nxt = max(r['time'] for r in d) + 1
        if nxt >= end_ms or len(d) < 500:
            break
        cur = nxt
        time.sleep(0.25)
    dedup = {r['t']: r for r in out}
    return [dedup[t] for t in sorted(dedup)]


FETCHERS = {'hyperliquid': hl_klines, 'bybit': bybit_klines, 'binance_perp': binance_klines}
FUNDING = {'hyperliquid': hl_funding, 'bybit': bybit_funding, 'binance_perp': binance_funding}


# ---------------------------------------------------- frozen regime scorer
def funding_paid_path(fr, entry_ms):
    cum, out = 0.0, []
    for r in sorted(fr, key=lambda x: x['t']):
        if r['t'] < entry_ms:
            continue
        cum += r['rate'] * 1e4
        out.append((r['t'], cum))
    return out


def score_regime(bars, fr, entry_ms):
    """Frozen D-v2 regime, identical to h5v2 score_dv2.py score_regime (primary arm)."""
    win = [b for b in bars if entry_ms <= b['t'] < entry_ms + H72]
    if not win:
        return None
    e = win[0]
    if e['t'] - entry_ms > 3600 * 1000:
        return None
    entry_px = e['o']
    stop_px = entry_px * (1 + STOP_BPS / 1e4)

    stop_bar = next((b for b in win if b['h'] >= stop_px), None)
    fpath = funding_paid_path(fr, e['t'])
    fund_trig_ts = next((ts for ts, cum in fpath if -cum > FUND_EXIT_BPS), None)
    fund_bar = next((b for b in win if b['t'] >= fund_trig_ts), None) if fund_trig_ts else None

    cands = [('base72h', win[-1], None)]
    if stop_bar is not None:
        cands.append(('stop', stop_bar, stop_px))
    if fund_bar is not None:
        cands.append(('funding_exit', fund_bar, None))
    reason, xbar, spx = min(cands, key=lambda c: (c[1]['t'], 0 if c[0] == 'stop' else 1))

    def fund_to(ts):
        return sum(r['rate'] for r in fr if e['t'] <= r['t'] <= ts + 5 * 60 * 1000) * 1e4

    out = {}
    for s in SLIPS:
        if reason == 'stop':
            fill = max(spx, xbar['c']) * (1 + s / 1e4)
            f_bps = fund_to(xbar['t'])
        elif reason == 'funding_exit':
            fill = xbar['o']
            f_bps = fund_to(xbar['t'])
        else:
            fill = xbar['c']
            f_bps = fund_to(xbar['t'])
        gross = (entry_px - fill) / entry_px * 1e4
        pre = gross + f_bps
        out[int(s)] = {'gross_bps': round(gross, 1), 'funding_bps': round(f_bps, 2),
                       'net_bps': round(max(pre, LIQ_FLOOR) - COST_RT, 1),
                       'net28': round(max(pre, LIQ_FLOOR) - 28.0, 1),
                       'liq_floor_hit': pre < LIQ_FLOOR}
    out['exit_reason'] = reason
    out['exit_ts'] = xbar['t']
    out['entry_px'] = entry_px
    out['entry_ts'] = e['t']
    if reason == 'stop':
        out['stop_px'] = stop_px
        out['crossing_close_vs_stop_bps'] = round((xbar['c'] - stop_px) / entry_px * 1e4, 1)
    return out


def score_short_vol24(kl, entry_ms):
    """Entry-day $vol per H5 convention (first 24h of bars from entry)."""
    bars = [b for b in kl if entry_ms <= b['t'] < entry_ms + H72]
    if not bars or bars[0]['t'] - entry_ms > 3600 * 1000:
        return None
    return sum(b['qv'] for b in bars if b['t'] < entry_ms + 24 * 3600 * 1000)


# ---------------------------------------------------- inference (analyze_v2)
rng = random.Random(42)


def wk(ms):
    d = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def clustered_boot(vals, clusters, nboot=B):
    from collections import defaultdict
    byc = defaultdict(list)
    for v, c in zip(vals, clusters):
        byc[c].append(v)
    keys = list(byc)
    means = []
    for _ in range(nboot):
        s = []
        for _ in keys:
            s += byc[rng.choice(keys)]
        means.append(sum(s) / len(s))
    means.sort()
    return (sum(vals) / len(vals), means[int(0.025 * nboot)], means[int(0.975 * nboot) - 1],
            sum(1 for x in means if x <= 0) / nboot)


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_ppf(p):
    lo, hi = -10, 10
    for _ in range(80):
        mid = (lo + hi) / 2
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def dsr(vals, n_trials=N_TRIALS):
    n = len(vals)
    if n < 3 or st.pstdev(vals) == 0:
        return 0.0
    sr = st.mean(vals) / st.pstdev(vals)
    g3 = sum((v - st.mean(vals)) ** 3 for v in vals) / n / st.pstdev(vals) ** 3
    g4 = sum((v - st.mean(vals)) ** 4 for v in vals) / n / st.pstdev(vals) ** 4
    em = 0.5772156649
    mx = (1 - em) * norm_ppf(1 - 1.0 / n_trials) + em * norm_ppf(1 - 1.0 / (n_trials * math.e))
    sr0 = mx * math.sqrt(1.0 / (n - 1))
    denom = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    return norm_cdf(((sr - sr0) * math.sqrt(n - 1)) / denom)


# ------------------------------------------------------------------ CMS poll
def fetch_cms_articles():
    """Returns list of {code,title,releaseDate} or None on total failure."""
    arts = []

    def walk(o):
        if isinstance(o, dict):
            if 'code' in o and 'title' in o and 'releaseDate' in o:
                arts.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    got_any = False
    for base in CMS_ENDPOINTS:
        arts.clear()
        for page in range(1, CMS_PAGES + 1):
            d = _get(f"{base}?type=1&pageNo={page}&pageSize={CMS_PAGE_SIZE}&catalogId=161")
            if d is None:
                break
            walk(d)
        if arts:
            got_any = True
            break
    if not got_any:
        return None
    dedup = {}
    for a in arts:
        dedup[a['code']] = {'code': a['code'], 'title': a['title'],
                            'releaseDate': int(a['releaseDate'])}
    return list(dedup.values())


def parse_delist_articles(articles):
    """-> list of {token, ann_ts_ms, ann_iso, delist_date, article_code, title}"""
    out = []
    for a in articles:
        m = TITLE_RE.match(a['title'].strip())
        if not m:
            continue
        toks = re.split(r',\s*|\s+and\s+', m.group(1))
        for t in toks:
            t = t.strip().upper()
            if not re.fullmatch(r'[A-Z0-9]{1,12}', t):
                continue
            ts = int(a['releaseDate'])
            out.append({'token': t, 'ann_ts_ms': ts,
                        'ann_iso': datetime.datetime.fromtimestamp(
                            ts / 1000, datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
                        'delist_date': m.group(2), 'article_code': a['code'],
                        'title': a['title'], 'event_type': 'spot_full_delist'})
    return out


# ------------------------------------------------------------------- main
def main() -> int:
    os.makedirs(ART, exist_ok=True)
    now_ms = int(time.time() * 1000)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # frozen inputs (immutable, git-committed)
    frozen_cat = json.load(open(FROZEN_CAT))
    frozen_codes = {e['article_code'] for arm in ('spot', 'futures') for e in frozen_cat.get(arm, [])}
    dv2 = json.load(open(FROZEN_V2))
    frozen_ok = [r for r in dv2 if r['status'] == 'ok']
    frozen_nets = [r['dv2']['100']['net_bps'] for r in frozen_ok]
    frozen_wks = [wk(r['dv2']['entry_ts']) for r in frozen_ok]

    # persisted forward catalog (append-only union; survives CMS pagination roll-off)
    cat_path = os.path.join(ART, "oos_event_catalog.json")
    catalog = json.load(open(cat_path)) if os.path.exists(cat_path) else []
    known = {(e['article_code'], e['token']) for e in catalog}

    # ---- 1. CMS poll (fail loud: unreachable = MISSING day, exit 1) ----------
    articles = fetch_cms_articles()
    if articles is None:
        print("ALERT delist-oos: Binance CMS catalog 161 UNREACHABLE — today's poll is "
              "MISSING, not empty. Forward accrual is blind until this is fixed "
              "(t_a4cbc566, ledger row 25).")
        # still write status so staleness is visible in the vault
        _write_status(now_iso, cms_ok=False, ledger_rows=_load_prev_ledger(),
                      pooled=None, frozen_n=len(frozen_nets))
        return 1

    new_events = [e for e in parse_delist_articles(articles)
                  if e['article_code'] not in frozen_codes
                  and (e['article_code'], e['token']) not in known]
    for e in new_events:
        catalog.append(e)
        events_out.append(f"NEW DELIST EVENT {e['token']} ann={e['ann_iso']} "
                          f"delist={e['delist_date']} article={e['article_code'][:8]} — "
                          f"shadow window opens {(e['ann_ts_ms'] + 30*60*1000)}ms "
                          f"(scored after 72h+buffer)")
    if new_events:
        write_text_atomic(cat_path, json.dumps(
            sorted(catalog, key=lambda e: (e['ann_ts_ms'], e['token'])), indent=1))

    # ---- 2. idempotent full recompute of the forward ledger ------------------
    prev = _load_prev_ledger()
    prev_status = {(r['article_code'], r['token']): r.get('status', '') for r in prev}
    rows = []
    for e in sorted(catalog, key=lambda e: (e['ann_ts_ms'], e['token'])):
        row = {'token': e['token'], 'article_code': e['article_code'],
               'ann_iso': e['ann_iso'], 'ann_ts_ms': e['ann_ts_ms'],
               'delist_date': e['delist_date'], 'venue': '', 'status': '',
               'entry_ts': '', 'entry_px': '', 'exit_reason': '', 'exit_ts': '',
               'vol24_usd': '', 'gross100_bps': '', 'funding100_bps': '',
               'net50_bps': '', 'net100_bps': '', 'net200_bps': '', 'net28_100_bps': '',
               'stop_px': '', 'crossing_close_vs_stop_bps': '', 'scored_at': ''}
        entry_req = e['ann_ts_ms'] + 30 * 60 * 1000
        if now_ms < entry_req + H72 + SETTLE_BUFFER_MS:
            row['status'] = 'accruing'
            rows.append(row)
            continue
        fetch_end = entry_req + H72 + 12 * 3600 * 1000
        scored = False
        for venue in VENUE_PRIORITY:
            kl = FETCHERS[venue](e['token'], e['ann_ts_ms'] - 3600 * 1000, fetch_end)
            if not kl or len(kl) <= 50:
                continue
            vol24 = score_short_vol24(kl, entry_req)
            if vol24 is None:
                continue                      # coverage inadequate on this venue
            row['venue'] = venue
            row['vol24_usd'] = round(vol24)
            if vol24 < MIN_VOL24_USD:
                row['status'] = 'excluded_volume'
                scored = True
                break
            fr = FUNDING[venue](e['token'], entry_req - 3600 * 1000, fetch_end)
            prim = score_regime(kl, fr, entry_req)
            if prim is None:
                row['status'] = 'coverage_failed'
                scored = True
                break
            row.update({'status': 'ok', 'entry_ts': prim['entry_ts'],
                        'entry_px': prim['entry_px'], 'exit_reason': prim['exit_reason'],
                        'exit_ts': prim['exit_ts'],
                        'gross100_bps': prim[100]['gross_bps'],
                        'funding100_bps': prim[100]['funding_bps'],
                        'net50_bps': prim[50]['net_bps'], 'net100_bps': prim[100]['net_bps'],
                        'net200_bps': prim[200]['net_bps'],
                        'net28_100_bps': prim[100]['net28'],
                        'stop_px': prim.get('stop_px', ''),
                        'crossing_close_vs_stop_bps': prim.get('crossing_close_vs_stop_bps', ''),
                        'scored_at': now_iso})
            scored = True
            break
        if not scored:
            # window complete but no venue returned adequate bars: could be genuine
            # no_venue (never had a liquid perp) or a fetch fault. A token whose
            # window completed and was NEVER scored ok before -> no_venue (clean).
            # A previously-ok token that now fails refetch -> operational fault.
            if prev_status.get((e['article_code'], e['token'])) == 'ok':
                row['status'] = 'refetch_failed'
                alerts.append(f"ALERT delist-oos: {e['token']} previously scored ok but "
                              f"venue refetch FAILED — ledger row degraded, investigate.")
            else:
                row['status'] = 'no_venue'
        if (row['status'] == 'ok'
                and prev_status.get((e['article_code'], e['token'])) not in ('ok',)):
            events_out.append(
                f"SCORED {e['token']} @{row['venue']} exit={row['exit_reason']} "
                f"net@100bps={row['net100_bps']}bps (50/200: {row['net50_bps']}/"
                f"{row['net200_bps']}) funding={row['funding100_bps']}bps")
        rows.append(row)

    fields = list(rows[0].keys()) if rows else [
        'token', 'article_code', 'ann_iso', 'ann_ts_ms', 'delist_date', 'venue', 'status',
        'entry_ts', 'entry_px', 'exit_reason', 'exit_ts', 'vol24_usd', 'gross100_bps',
        'funding100_bps', 'net50_bps', 'net100_bps', 'net200_bps', 'net28_100_bps',
        'stop_px', 'crossing_close_vs_stop_bps', 'scored_at']
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    write_text_atomic(os.path.join(ART, "oos_event_ledger.csv"), buf.getvalue())

    # ---- 3. running family DSR on pooled frozen + forward set ---------------
    fwd_ok = [r for r in rows if r['status'] == 'ok']
    pooled_nets = frozen_nets + [float(r['net100_bps']) for r in fwd_ok]
    pooled_wks = frozen_wks + [wk(int(r['entry_ts'])) for r in fwd_ok]
    mean, lo, hi, p = clustered_boot(pooled_nets, pooled_wks)
    d = dsr(pooled_nets)
    pooled = {'n': len(pooled_nets), 'n_frozen': len(frozen_nets), 'n_forward': len(fwd_ok),
              'mean_net_bps': round(mean, 1), 'ci95_wk': [round(lo, 1), round(hi, 1)],
              'p_gt0_wk': round(p, 4), 'dsr_n16': round(d, 4),
              'dsr_bar': DSR_BAR, 'ci_excludes_0': bool(lo > 0),
              'promotion_bar_crossed': bool(d >= DSR_BAR and lo > 0)}
    if pooled['promotion_bar_crossed']:
        events_out.append(
            f"D-V2 PROMOTION BAR CROSSED — assemble packet: DSR(N=16)={d:.4f} >= {DSR_BAR} "
            f"AND pooled week-CI [{lo:.0f},{hi:.0f}] excludes 0 "
            f"(n={len(pooled_nets)} = {len(frozen_nets)} frozen + {len(fwd_ok)} forward). "
            f"Packet = seat work per t_a4cbc566; regime stays FROZEN.")

    _write_status(now_iso, cms_ok=True, ledger_rows=rows, pooled=pooled,
                  frozen_n=len(frozen_nets))

    for line in alerts + events_out:
        print(line)
    return 1 if alerts else 0


def _load_prev_ledger():
    p = os.path.join(ART, "oos_event_ledger.csv")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def _write_status(now_iso, cms_ok, ledger_rows, pooled, frozen_n):
    from collections import Counter
    counts = Counter(r.get('status', '') for r in ledger_rows)
    write_text_atomic(os.path.join(ART, "status.json"), json.dumps({
        'last_run': now_iso, 'cms_ok': cms_ok,
        'card': 't_a4cbc566', 'ledger_row': 25,
        'regime': 'FROZEN — prereg 2026-08-02-event-shorts-v2-tail-control (stop 2000bps '
                  'crossing-bar-close fill +{50,100,200}bps slip, funding-exit 750bps, '
                  '1x-isolated floor -10000bps, 72h, HL>Bybit>Binance-perp, 16bps RT)',
        'forward_event_counts': dict(counts),
        'pooled_dv2_at_slip100': pooled,
        'promotion_rule': f'DSR(N={N_TRIALS}) >= {DSR_BAR} AND pooled week-CI excludes 0 '
                          '-> alert "D-V2 PROMOTION BAR CROSSED"; packet is seat work',
        'frozen_baseline': {'n': frozen_n, 'source': FROZEN_V2,
                            'dsr_at_freeze': 0.933, 'mean_at_freeze_bps': 1012.6},
        'prereg': 'strategies/pre-registrations/2026-08-02-event-shorts-v2-tail-control-prereg-fable.md',
        'results_note': 'research/2026-08-02-h5v2-tail-control-results-fable.md',
        'consumers': ['research/2026-08-01-ACTIVE-GOAL-winning-patterns-mission-fable.md',
                      'kanban t_a4cbc566 promotion packet (on bar cross)'],
    }, indent=1))


if __name__ == '__main__':
    sys.exit(main())
