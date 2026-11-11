"""Out-of-sample check of odds.fan (block bootstrap of the past 250 daily log returns, 5-day blocks,
20-session horizon). At every 5th session from bar 250, build the fan from the PREVIOUS 250 closes only,
then score it against the realised close 20 sessions later.

Baselines: coin (P(up)=0.5); no-trend fan (same blocks, past-year mean removed, so only volatility is used).
Usage: python3 tools/odds_check.py data.json .   (data.json: daily OHLC bars per ticker, as in the 2026-11-10 backtest)
"""
import json, sys, math
import numpy as np
sys.path.insert(0, sys.argv[2])
import signals as SG

D = json.load(open(sys.argv[1]))
H, LOOK, BLOCK, N, STEP = 20, 219, 5, 10000, 5
rng = np.random.default_rng(19)
rows = []
for t, v in D.items():
    c = np.array([b[3] for b in v['bars']], float)
    dates = v['dates']
    lr = np.diff(np.log(c))
    s = SG.series([tuple(b) for b in v['bars']])
    for i in range(LOOK, len(c) - H, 1):
        sig = None
        p, q = s[i - 1], s[i]
        if None not in (p['ma3'], p['lo'], p['rsi'], q['ma3'], q['lo'], q['rsi']):
            k, r = SG.states(p['c'], p['ma3'], p['up'], p['lo'], p['rsi'], q['c'], q['ma3'], q['up'], q['lo'], q['rsi'])
            sig = 'BUY' if 'BUY' in (k, r) else 'SELL' if 'SELL' in (k, r) else None
        if (i - LOOK) % STEP and not sig:
            continue
        past = lr[i - LOOK:i]                      # returns up to and including day i (close i known)
        bs = np.convolve(past, np.ones(BLOCK), 'valid')        # every 5-day block sum
        draws = bs[rng.integers(0, len(bs), size=(N, H // BLOCK))].sum(1)
        mu = past.mean() * H
        draws0 = draws - mu                        # the same fan with last year's trend removed
        out = math.log(c[i + H] / c[i])
        q10, q25, q75, q90 = np.percentile(draws, [10, 25, 75, 90])
        rows.append(dict(t=t, d=dates[i], y=int(dates[i][:4]), sig=sig, grid=(i - LOOK) % STEP == 0,
                         p_up=(draws > 0).mean(), p_up0=(draws0 > 0).mean(),
                         p_up5=(draws >= math.log(1.05)).mean(), p_dn5=(draws <= math.log(0.95)).mean(),
                         in80=q10 <= out <= q90, in50=q25 <= out <= q75, below=out < q10, above=out > q90,
                         up=out > 0, up5=out >= math.log(1.05), dn5=out <= math.log(0.95), out=out))

def brier(p, y):
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))

def report(R, name):
    y = np.array([r['up'] for r in R], float)
    p = np.array([r['p_up'] for r in R]); p0 = np.array([r['p_up0'] for r in R])
    b, b0, bc = brier(p, y), brier(p0, y), brier(np.full(len(y), .5), y)
    base = np.full(len(y), y.mean())
    o = {"name": name, "n": len(R), "actual_up": round(y.mean() * 100, 1), "pred_up": round(p.mean() * 100, 1),
         "in80": round(100 * np.mean([r['in80'] for r in R]), 1), "in50": round(100 * np.mean([r['in50'] for r in R]), 1),
         "below10": round(100 * np.mean([r['below'] for r in R]), 1), "above90": round(100 * np.mean([r['above'] for r in R]), 1),
         "pred_up5": round(100 * np.mean([r['p_up5'] for r in R]), 1), "act_up5": round(100 * np.mean([r['up5'] for r in R]), 1),
         "pred_dn5": round(100 * np.mean([r['p_dn5'] for r in R]), 1), "act_dn5": round(100 * np.mean([r['dn5'] for r in R]), 1),
         "brier": round(b, 4), "brier_notrend": round(b0, 4), "brier_coin": round(bc, 4),
         "brier_hindsight_rate": round(brier(base, y), 4),
         "skill_vs_coin_pct": round((1 - b / bc) * 100, 2), "skill_vs_notrend_pct": round((1 - b / b0) * 100, 2),
         "corr_p_vs_outcome": round(float(np.corrcoef(p, y)[0, 1]), 3)}
    return o

grid = [r for r in rows if r['grid']]
out = {"pooled": report(grid, "all tickers, every 5th session")}
# calibration of P(up)
bins = [0, .40, .45, .50, .55, .60, .65, 1.01]
cal = []
for a, z in zip(bins[:-1], bins[1:]):
    R = [r for r in grid if a <= r['p_up'] < z]
    if len(R) >= 30:
        cal.append({"bin": f"{a:.2f}-{min(z,1):.2f}", "n": len(R), "pred": round(100 * np.mean([r['p_up'] for r in R]), 1),
                    "actual": round(100 * np.mean([r['up'] for r in R]), 1)})
out["calibration_p_up"] = cal
out["per_ticker"] = [report([r for r in grid if r['t'] == t], t) for t in D]
for side in ("BUY", "SELL"):
    R = [r for r in rows if r['sig'] == side]
    out[f"at_{side}_signals"] = report(R, f"days a Rule A {side} leg fired")
yrs = sorted({r['y'] for r in grid})
out["by_year_skill_vs_coin"] = {y: report([r for r in grid if r['y'] == y], str(y))["skill_vs_coin_pct"] for y in yrs}
out["by_year_in80"] = {y: report([r for r in grid if r['y'] == y], str(y))["in80"] for y in yrs}
json.dump(out, open(sys.argv[1].replace('data.json', 'odds_check.json'), 'w'), indent=1)
print(json.dumps({k: out[k] for k in ("pooled", "calibration_p_up", "at_BUY_signals", "at_SELL_signals",
                                      "by_year_skill_vs_coin", "by_year_in80")}, indent=1))
for x in out["per_ticker"]:
    print(x["name"], x["n"], "up", x["actual_up"], "pred", x["pred_up"], "in80", x["in80"], "skill", x["skill_vs_coin_pct"], "vs-notrend", x["skill_vs_notrend_pct"])
