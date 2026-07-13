#!/usr/bin/env python3
import sys, json
d = json.load(sys.stdin)
preds = d.get('predictions', [])
wins = sum(1 for p in preds if str(p.get('actualMfeFirst', '0')) == '1')
losses = sum(1 for p in preds if str(p.get('actualMfeFirst', '0')) == '0')
total = wins + losses
rate = (wins/total*100) if total > 0 else 0
print(f'ML Predictions: {total} resolved, {wins} wins, {losses} losses, {rate:.1f}% win rate')
models = {}
for p in preds:
    m = p.get('modelType', p.get('model_name', 'unknown'))
    outcome = str(p.get('actualMfeFirst', ''))
    if m not in models:
        models[m] = {'wins': 0, 'losses': 0, 'total': 0}
    if outcome in ('1', '0'):
        models[m]['total'] += 1
        if outcome == '1':
            models[m]['wins'] += 1
        else:
            models[m]['losses'] += 1
for m, v in sorted(models.items()):
    wr = (v['wins']/v['total']*100) if v['total'] > 0 else 0
    flag = ' WARNING' if wr < 50 else ''
    print(f'  {m}: {v["wins"]}/{v["total"]} = {wr:.1f}%{flag}')
# Avg confidence for wins vs losses
avg_conf_win = sum(float(p['predictedProbability']) for p in preds if str(p.get('actualMfeFirst','0')) == '1' and p.get('predictedProbability')) / wins if wins else 0
avg_conf_lose = sum(float(p['predictedProbability']) for p in preds if str(p.get('actualMfeFirst','0')) == '0' and p.get('predictedProbability')) / losses if losses else 0
avg_conf_all = sum(float(p['predictedProbability']) for p in preds if p.get('predictedProbability')) / len(preds) if preds else 0
print(f'  Avg confidence: {avg_conf_all*100:.1f}% (wins: {avg_conf_win*100:.1f}%, losses: {avg_conf_lose*100:.1f}%)')
