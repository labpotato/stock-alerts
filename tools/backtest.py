"""Backtest of Rule A (Keltner on MA3 with the rising-MA3 condition + RSI14 30/70 recross).
Signals come from the alerter's own signals.py. Entries and exits are at the NEXT session's open.

Usage: python tools/backtest.py data.json [repo_dir]   ->  results.json in the current directory
data.json: {TICKER: {"dates": ["YYYY-MM-DD", ...], "bars": [[open, high, low, close], ...]}}, daily,
split-adjusted, oldest first, with SPY (the trading calendar), VOO and QQQ present. The run of
2026-11-10 used Shibui daily prices for 15 tickers; its summary is docs/backtest.json."""
import json, sys, math, random
import numpy as np
sys.path.insert(0, sys.argv[2] if len(sys.argv) > 2 else '/home/user/stock-alerts-repo')
import signals as SG

D = json.load(open(sys.argv[1]))
H = (5, 10, 20, 60)

def sig(t, rising=True):
    rows = [tuple(b) for b in D[t]['bars']]
    s = SG.series(rows)
    k = ['NA'] * len(s); r = ['NA'] * len(s)
    for i in range(20, len(s)):
        p, q = s[i-1], s[i]
        if p['ma3'] is None or p['lo'] is None or p['rsi'] is None: continue
        kk, rr = SG.states(p['c'], p['ma3'], p['up'], p['lo'], p['rsi'], q['c'], q['ma3'], q['up'], q['lo'], q['rsi'])
        if not rising and kk != 'BUY' and p['ma3'] <= p['lo'] and q['ma3'] > q['lo']:
            kk = 'BUY'                      # the rule as it was before 5 Nov 2026
        k[i], r[i] = kk, rr
    return k, r

def fwd(t):
    o = np.array([b[0] for b in D[t]['bars']]); c = np.array([b[3] for b in D[t]['bars']])
    out = {}
    for h in H:
        f = np.full(len(c), np.nan)
        f[:len(c)-h-1] = c[1+h:] / o[1:len(c)-h] - 1      # buy next open, hold h sessions
        out[h] = f
    return out

def event_study(tickers, pick, label, reps=2000, seed=7):
    rng = np.random.default_rng(seed)
    res = {}
    for h in H:
        ex, hits, n, perm = [], [], 0, np.zeros(reps)
        for t in tickers:
            k, r = SIG[t]; f = FWD[t][h]
            valid = np.where(~np.isnan(f))[0]; valid = valid[valid >= 250]
            idx = np.array([i for i in valid if pick(k[i], r[i])], dtype=int)
            if len(idx) == 0: continue
            base = f[valid].mean()
            ex += list(f[idx] - base); hits += list(f[idx] > 0); n += len(idx)
            draws = rng.choice(valid, size=(reps, len(idx)))
            perm += (f[draws] - base).sum(axis=1)
        ex = np.array(ex); m = ex.mean() if n else float('nan')
        p = float((np.abs(perm / max(n, 1)) >= abs(m)).mean()) if n else float('nan')
        res[h] = dict(n=n, excess_pct=round(m * 100, 2), hit_pct=round(100 * np.mean(hits), 1) if n else None, p=round(p, 3))
    return res

def stats(eq, dates):
    eq = np.array(eq); r = eq[1:] / eq[:-1] - 1
    yrs = (np.datetime64(dates[-1]) - np.datetime64(dates[0])).astype(int) / 365.25
    cagr = (eq[-1] / eq[0]) ** (1 / yrs) - 1
    dd = (eq / np.maximum.accumulate(eq) - 1).min()
    sh = r.mean() / r.std() * math.sqrt(252) if r.std() else 0
    return dict(cagr_pct=round(cagr * 100, 2), max_dd_pct=round(dd * 100, 1), sharpe=round(sh, 2),
                vol_pct=round(r.std() * math.sqrt(252) * 100, 1), total_x=round(eq[-1] / eq[0], 2))

def in_out(t, legs='KR', start=250):
    """All-in at the next open after a BUY, all-out at the next open after a SELL; cash earns 0."""
    k, r = SIG[t]; b = D[t]['bars']; n = len(b)
    eq, pos, cash, sh, trades, days_in = [], 0, 1.0, 0.0, 0, 0
    pend = None
    for i in range(start, n):
        o, c = b[i][0], b[i][3]
        if pend == 'BUY' and not pos: sh, cash, pos, trades = cash / o, 0.0, 1, trades + 1
        elif pend == 'SELL' and pos: cash, sh, pos = sh * o, 0.0, 0
        pend = None
        buy = ('K' in legs and k[i] == 'BUY') or ('R' in legs and r[i] == 'BUY')
        sell = ('K' in legs and k[i] == 'SELL') or ('R' in legs and r[i] == 'SELL')
        if buy and not sell: pend = 'BUY'
        elif sell and not buy: pend = 'SELL'
        days_in += pos
        eq.append(cash + sh * c)
    bh = [b[i][3] / b[start][0] for i in range(start, n)]
    d = D[t]['dates'][start:]
    s, h = stats(eq, d), stats(bh, d)
    s.update(trades=trades, in_market_pct=round(100 * days_in / len(eq)))
    return s, h, eq, bh, d

def portfolio(targets, mode, start_date, band=0.01, cost=0.0005):
    """mode: hold | monthly | gated (BUY signal lets an underweight name be topped up from cash;
    SELL signal lets an overweight name be trimmed). Trades at the next open."""
    ts = [t for t in targets if t != 'CASH']
    cal = D['SPY']['dates']; i0 = cal.index(start_date)
    px = {t: {d: b for d, b in zip(D[t]['dates'], D[t]['bars'])} for t in ts}
    sg = {t: {d: (k, r) for d, k, r in zip(D[t]['dates'], *SIG[t])} for t in ts}
    b0 = {t: px[t][cal[i0]][0] for t in ts}
    sh = {t: targets[t] / b0[t] for t in ts}; cash = targets.get('CASH', 0.0)
    eq, pend, ntr, last_m = [], [], 0, cal[i0][:7]
    for d in cal[i0:]:
        tot_o = cash + sum(sh[t] * px[t][d][0] for t in ts)
        for t, side in pend:
            v = sh[t] * px[t][d][0]; gap = targets[t] * tot_o - v
            if side == 'BUY' and gap > band * tot_o:
                amt = min(gap, cash)
                if amt > 0: sh[t] += amt * (1 - cost) / px[t][d][0]; cash -= amt; ntr += 1
            elif side == 'SELL' and -gap > band * tot_o:
                sh[t] -= (-gap) / px[t][d][0]; cash += (-gap) * (1 - cost); ntr += 1
        pend = []
        if mode == 'monthly' and d[:7] != last_m:
            for t in ts:
                v = sh[t] * px[t][d][0]; gap = targets[t] * tot_o - v
                sh[t] += gap / px[t][d][0]; cash -= gap; cash -= abs(gap) * cost; ntr += 1
        last_m = d[:7]
        if mode == 'gated':
            for t in ts:
                k, r = sg[t][d]
                b_, s_ = 'BUY' in (k, r), 'SELL' in (k, r)
                if b_ and not s_: pend.append((t, 'BUY'))
                elif s_ and not b_: pend.append((t, 'SELL'))
        eq.append(cash + sum(sh[t] * px[t][d][3] for t in ts))
    return stats(eq, cal[i0:]) | dict(trades=ntr), eq, cal[i0:]

T = list(D)
SIG = {t: sig(t) for t in T}; FWD = {t: fwd(t) for t in T}
OLD = {t: sig(t, rising=False) for t in T}
out = {}
either = lambda side: (lambda k, r: side in (k, r) and not ({'BUY', 'SELL'} <= {k, r}))
out['events'] = {
    'BUY (either measure)': event_study(T, either('BUY'), 'buy'),
    'BUY price range (Keltner)': event_study(T, lambda k, r: k == 'BUY', 'kbuy'),
    'BUY momentum (RSI)': event_study(T, lambda k, r: r == 'BUY', 'rbuy'),
    'SELL (either measure)': event_study(T, either('SELL'), 'sell'),
    'SELL price range (Keltner)': event_study(T, lambda k, r: k == 'SELL', 'ksell'),
    'SELL momentum (RSI)': event_study(T, lambda k, r: r == 'SELL', 'rsell'),
    'WATCH BUY (below range, no signal yet)': event_study(T, lambda k, r: k == 'WATCH BUY', 'wb'),
}
SIG_NEW = SIG; SIG = OLD
out['events']['BUY price range, OLD rule (before 5 Nov)'] = event_study(T, lambda k, r: k == 'BUY', 'kold')
SIG = SIG_NEW
out['in_out'] = {}; curves = {}
for t in T:
    s, h, eq, bh, d = in_out(t)
    out['in_out'][t] = dict(rule=s, hold=h, since=d[0])
    curves[t] = dict(dates=d[::5], rule=[round(x, 4) for x in eq[::5]], hold=[round(x, 4) for x in bh[::5]])
tg = dict(VOO=.30, QQQ=.10, GOOGL=.075, NVDA=.075, TSLA=.075, SOXX=.075, XLV=.05, XLF=.05, XLB=.025, XLP=.025, GLD=.05, CASH=.10)
start = '2011-09-12'
out['portfolio'] = {}; pc = {}
for mode in ('hold', 'monthly', 'gated'):
    s, eq, d = portfolio(tg, mode, start)
    out['portfolio'][mode] = s; pc[mode] = [round(x, 4) for x in eq[::5]]
for b in ('VOO', 'QQQ'):
    i0 = D[b]['dates'].index(start); bars = D[b]['bars'][i0:]
    eq = [x[3] / bars[0][0] for x in bars]
    out['portfolio'][b] = stats(eq, D[b]['dates'][i0:]); pc[b] = [round(x, 4) for x in eq[::5]]
pc['dates'] = d[::5]
# a diversified basket without the two hindsight winners (NVDA, TSLA)
tg2 = dict(VOO=.40, QQQ=.15, GOOGL=.05, SOXX=.05, XLV=.075, XLF=.075, XLB=.025, XLP=.025, GLD=.05, CASH=.10)
out['portfolio_no_winners'] = {m: portfolio(tg2, m, start)[0] for m in ('hold', 'monthly', 'gated')}
out['meta'] = dict(tickers=T, data_through=D['SPY']['dates'][-1], portfolio_start=start, targets=tg,
                   note='Split-adjusted prices without dividends; survivors only; next-open fills; 5 bps cost in the portfolio test.')
json.dump(dict(results=out, curves=curves, pcurves=pc), open('results.json', 'w'))
def show(d, ind=0):
    for k, v in d.items():
        if isinstance(v, dict) and any(isinstance(x, dict) for x in v.values()): print(' ' * ind + str(k)); show(v, ind + 2)
        else: print(' ' * ind + f'{k}: {v}')
show(out)
