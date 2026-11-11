"""Time-machine odds for an alert chart (idea from day-trader-skill's trader-timemachine, MIT).

Two honest numbers, both from the stock's own past year of settled daily closes:
  - fan(): 10,000 replays of the next HORIZON sessions, each built from 5-day blocks of the
    stock's own daily returns (blocks keep a little of real momentum and volatility clustering).
    Percentile paths as ratios to today's price, and the chance of ending higher, up 5%+ or
    down 5%+. A simulation of what its past year would make likely, not a forecast: it knows
    nothing about earnings, news or a change of regime.
  - record(): every past bar in the window where the same leg fired the same side, and what the
    close did HORIZON sessions later. The real record, small samples included.
"""
import math
import random

import signals as SG

HORIZON = 20       # sessions ahead (about a month)
REPLAYS = 10000
BLOCK = 5
SEED = 19
PCTS = (10, 25, 50, 75, 90)


def fan(closes, horizon=HORIZON, n=REPLAYS, block=BLOCK, seed=SEED):
    c = [x for x in closes if x and x > 0]
    if len(c) < 60:
        return None
    lr = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    starts = len(lr) - block + 1
    rnd = random.Random(seed)
    paths = []
    for _ in range(n):
        s, path = 0.0, []
        while len(path) < horizon:
            i = rnd.randrange(starts)
            for r in lr[i:i + block]:
                s += r
                path.append(s)
                if len(path) == horizon:
                    break
        paths.append(path)
    out = {f"p{p}": [] for p in PCTS}
    for j in range(horizon):
        col = sorted(x[j] for x in paths)
        for p in PCTS:
            out[f"p{p}"].append(round(math.exp(col[min(n - 1, int(n * p / 100))]), 5))
    end = [x[-1] for x in paths]
    up5, dn5 = math.log(1.05), math.log(0.95)
    return {**out, "horizon": horizon, "replays": n, "days": len(c),
            "p_up": round(100 * sum(e > 0 for e in end) / n, 1),
            "p_up5": round(100 * sum(e >= up5 for e in end) / n, 1),
            "p_dn5": round(100 * sum(e <= dn5 for e in end) / n, 1)}


def record(rows, horizon=HORIZON):
    """Past fires of each leg and side in these bars, and the move HORIZON sessions later."""
    s = SG.series(rows)
    out = {}
    for j in range(2, len(s) - horizon):
        p, q = s[j - 1], s[j]
        if None in (p["ma3"], p["up"], p["lo"], p["rsi"], q["ma3"], q["up"], q["lo"], q["rsi"]):
            continue
        k, r = SG.states(p["c"], p["ma3"], p["up"], p["lo"], p["rsi"],
                         q["c"], q["ma3"], q["up"], q["lo"], q["rsi"])
        for leg, st in (("k", k), ("r", r)):
            if st in ("BUY", "SELL"):
                out.setdefault(f"{leg}:{st}", []).append(round((s[j + horizon]["c"] / q["c"] - 1) * 100, 2))
    res = {}
    for key, moves in out.items():
        m = sorted(moves)
        res[key] = {"n": len(m), "higher": sum(x > 0 for x in m), "median": m[len(m) // 2]}
    return res


def summary(rec, legs, side):
    """The record of whichever fired leg has the most past fires, or None if neither fired before."""
    found = [rec[f"{l}:{side}"] for l in legs if rec.get(f"{l}:{side}")]
    return max(found, key=lambda x: x["n"]) if found else None
