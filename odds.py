"""Time-machine range and signal odds for alert charts (idea from day-trader-skill's trader-timemachine, MIT).

Revised 2026-11-11 after an out-of-sample check (tools/odds_check.py, 15 tickers, 2004-2026, 11,241
dated forecasts, each built only from the year before it):
  - The RANGE is reliable: the 80% band held 79% of real 20-session outcomes and the 50% band 52%.
    It is too narrow in stress years (2007-08, 2018, 2022: about 67-69%).
  - The DIRECTION was not: the chance of "ends higher" from replaying last year's trend had no
    link to what happened (correlation 0.01; 36% predicted and 71% predicted both came true about
    60% of the time). Worse, at signal days it leaned against the signal: after a BUY it said 51%
    higher, reality was 62%.
So fan() now removes last year's trend and uses only its volatility (the range), and the odds in a
message come from history(): what really followed each Rule A leg over 22 years (tools/sig_record.py,
stored in odds_history.json), next to the same numbers for an ordinary day.
"""
import json
import math
import os
import random

HORIZON = 20       # sessions ahead (about a month)
REPLAYS = 10000
BLOCK = 5
SEED = 19
PCTS = (10, 25, 50, 75, 90)
HIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odds_history.json")
MIN_TICKER_N = 10  # a ticker's own long record is used from this many past fires


def fan(closes, horizon=HORIZON, n=REPLAYS, block=BLOCK, seed=SEED):
    """Percentile paths (ratios to today's price) of 10,000 replays of 5-day blocks of the past
    year's daily moves, with that year's average move removed: the width is real, the tilt is not."""
    c = [x for x in closes if x and x > 0]
    if len(c) < 60:
        return None
    lr = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    mu = sum(lr) / len(lr)
    lr = [x - mu for x in lr]
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
    return {**out, "horizon": horizon, "replays": n, "days": len(c), "v": 2}


_HIST = None


def _hist():
    global _HIST
    if _HIST is None:
        try:
            _HIST = json.load(open(HIST_FILE))
        except (OSError, ValueError):
            _HIST = {}
    return _HIST


def history(ticker, legs, side):
    """The long record for the fired leg(s): this ticker's own when it has enough fires, else all
    backtest tickers pooled. Several legs: the one with the most fires. None without data."""
    h = _hist()
    best = None
    for leg in legs:
        key = f"{leg}:{side}"
        own = (h.get("per_ticker") or {}).get(f"{ticker}|{key}")
        pick = (dict(own, scope="own") if own and own["n"] >= MIN_TICKER_N
                else dict((h.get("signals") or {}).get(key) or {}, scope="pooled"))
        rank = lambda x: (x["scope"] == "own", x["n"])
        if pick.get("n") and (best is None or rank(pick) > rank(best)):
            best = dict(pick, leg=leg)
    if best:
        best["base"] = h.get("all_days")
        best["since"] = best.get("since") or (h.get("from") or "")[:4]
        best["tickers"] = len(h.get("tickers") or [])
    return best
