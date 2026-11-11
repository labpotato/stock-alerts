"""Long-history record of each Rule A leg and side, 20 sessions later, pooled over the backtest tickers,
plus the all-days base rate for comparison. Close-to-close from the signal day's close.
Usage: python3 tools/sig_record.py data.json . odds_history.json"""
import json, sys, math
import numpy as np
sys.path.insert(0, sys.argv[2]); import signals as SG
D = json.load(open(sys.argv[1])); H = 20
rec, base, per = {}, [], {}
for t, v in D.items():
    c = [b[3] for b in v['bars']]; s = SG.series([tuple(b) for b in v['bars']])
    for i in range(30, len(c) - H):
        m = c[i + H] / c[i] - 1
        base.append(m)
        p, q = s[i - 1], s[i]
        if None in (p['ma3'], p['lo'], p['rsi'], q['ma3'], q['lo'], q['rsi']): continue
        k, r = SG.states(p['c'], p['ma3'], p['up'], p['lo'], p['rsi'], q['c'], q['ma3'], q['up'], q['lo'], q['rsi'])
        for leg, st in (('k', k), ('r', r)):
            if st in ('BUY', 'SELL'):
                rec.setdefault(f'{leg}:{st}', []).append(m); per.setdefault((t, f'{leg}:{st}'), []).append(m)
def summ(x):
    x = np.array(x); return {"n": len(x), "higher": round(100 * (x > 0).mean(), 1), "up5": round(100 * (x >= .05).mean(), 1),
        "down5": round(100 * (x <= -.05).mean(), 1), "median": round(100 * float(np.median(x)), 2),
        "p10": round(100 * float(np.percentile(x, 10)), 2), "p90": round(100 * float(np.percentile(x, 90)), 2)}
out = {"horizon": H, "tickers": sorted(D), "from": min(v['dates'][0] for v in D.values()), "to": max(v['dates'][-1] for v in D.values()),
       "all_days": summ(base), "signals": {k: summ(v) for k, v in rec.items()},
       "per_ticker": {f"{t}|{k}": dict(summ(v), since=D[t]['dates'][0][:4]) for (t, k), v in per.items() if len(v) >= 10}}
json.dump(out, open(sys.argv[3] if len(sys.argv) > 3 else 'signal_record.json', 'w'), indent=1)
print(json.dumps({k: out[k] for k in ('from', 'to', 'all_days', 'signals')}, indent=1)); print(len(out['per_ticker']), 'ticker rows with n>=10')
