#!/usr/bin/env bash
# HL desk hourly mark — no-agent cron. Writes DESK-MARK.md (Frank-readable) +
# appends DESK-MARK.jsonl (dashboard/journal consumer). Exit code IS liveness.
set -u
DESK=/home/frank/dgx-fable-orchestrator/state/hl-live-desk
PY=/home/frank/hl-maker-measurement/.venv/bin/python
WALLET=0x62d250e94005a4B892c83cc180CE5C4e6404d747

# Shim-integrity check (added 2026-08-15 after 3 desk crons were found never-fired due to
# missing profile shims — incl. the time-exit judge precondition). Exit 1 = red = alert.
SHIM_FAIL=0
for s in hl-desk-time-exit.sh hl-desk-watchman-guard.sh hl-desk-research-triggers.sh \
         hl-candle-recorder-guard.sh hl-ctx-snapshot.sh hl-desk-mark.sh; do
  if [ ! -x "/home/frank/.hermes/profiles/jarvis/scripts/$s" ]; then
    echo "SHIM MISSING: profiles/jarvis/scripts/$s — its cron is NOT running"
    SHIM_FAIL=1
  fi
done

"$PY" - <<'EOF'
import json, subprocess, datetime, os, sys
DESK='/home/frank/dgx-fable-orchestrator/state/hl-live-desk'
W='0x62d250e94005a4B892c83cc180CE5C4e6404d747'
def api(body):
    r=subprocess.run(['curl','-4','-s','-m','15','-X','POST','https://api.hyperliquid.xyz/info',
        '-H','Content-Type: application/json','-d',json.dumps(body)],capture_output=True,text=True)
    if r.returncode!=0 or not r.stdout: sys.exit(1)
    return json.loads(r.stdout)
now=datetime.datetime.now(datetime.timezone.utc)
ch=api({"type":"clearinghouseState","user":W})
fills=api({"type":"userFills","user":W})
DESK_START_MS=1786819560000  # 2026-08-15 18:46Z desk restart
sess=[f for f in fills if f['time']>=DESK_START_MS]
closed=sum(float(f.get('closedPnl',0)) for f in sess)
fees=sum(float(f.get('fee',0)) for f in sess)
eq=float(ch['marginSummary']['accountValue'])
pos=[{'coin':p['position']['coin'],'szi':p['position']['szi'],'entry':p['position']['entryPx'],
      'lev':p['position']['leverage']['value'],'uPnL':float(p['position']['unrealizedPnl'])}
     for p in ch['assetPositions']]
# oid -> actor attribution from gateway ledger
actors={}
try:
    for line in open(f'{DESK}/orders.jsonl'):
        try: r=json.loads(line)
        except: continue
        a=r.get('actor');
        for k in ('oid','oids'):
            v=r.get(k)
            if isinstance(v,int): actors[v]=a
            if isinstance(v,list): [actors.__setitem__(x,a) for x in v if isinstance(x,int)]
except FileNotFoundError: pass
by_actor={}
for f in sess:
    a=actors.get(f.get('oid'),'unattributed')
    d=by_actor.setdefault(a,{'fills':0,'closedPnl':0.0,'fees':0.0})
    d['fills']+=1; d['closedPnl']+=float(f.get('closedPnl',0)); d['fees']+=float(f.get('fee',0))
row={'ts':now.isoformat(timespec='seconds'),'equity':eq,'session_closed_pnl':round(closed,4),
     'session_fees':round(fees,4),'session_net':round(closed-fees,4),'session_fills':len(sess),
     'positions':pos,'by_actor':by_actor,'halt':os.path.exists(f'{DESK}/DESK_HALT')}
with open(f'{DESK}/DESK-MARK.jsonl','a') as f: f.write(json.dumps(row)+'\n')
with open(f'{DESK}/DESK-MARK.md','w') as f:
    f.write(f"# DESK MARK — {row['ts']} (hourly no-agent cron; jsonl history alongside)\n\n")
    f.write(f"Equity **${eq:.2f}** | session net (since 18:46Z restart) **${row['session_net']:+.2f}** ")
    f.write(f"(closed {closed:+.2f}, fees {fees:.2f}, {len(sess)} fills) | halt: {row['halt']}\n\n")
    f.write("Positions: " + (", ".join(f"{p['coin']} {p['szi']}@{p['entry']} lev {p['lev']}x uPnL {p['uPnL']:+.2f}" for p in pos) or "FLAT") + "\n\n")
    for a,d in sorted(by_actor.items()):
        f.write(f"- {a}: {d['fills']} fills, closedPnl {d['closedPnl']:+.3f}, fees {d['fees']:.3f}\n")
print(f"mark ok eq={eq:.2f} net={row['session_net']:+.2f} pos={len(pos)}")
EOF
rc=$?
[ "$SHIM_FAIL" -eq 1 ] && exit 1
exit $rc
