"""Rule H: a temporary hedge when a sector you are heavy in crashes (Dongmin 2026-11-06).
Pure functions, no network. Sizes come from the HOLDINGS_JSON secret and go to Telegram only;
nothing here is ever written to a file, because the repository is public.

Trigger (both):
  1. the group is at least `min_weight_pct` of the book;
  2. the group's proxy ETF fell at least `z` times its usual daily move today, or is at least
     `dd20_pct` below its 20-session high, AND it is below the bottom of its price range.
Split hedge (Dongmin 2026-11-06): each stock is hedged against the index it follows most closely
(highest R^2 of daily returns): chip-like names against SOXX with SOXS (-3x), the rest against QQQ
with PSQ (-1x). A leg opens when ITS index crashes.
Size per leg: `ratio` x the leg's beta-weighted exposure, divided by the fund's leverage.
Exit per leg (first to happen): a Rule A BUY on its index; the index back above the middle of its
      range; the index 5 % above where the leg started; the leg's time limit (20 sessions -1x, 5 -3x).
"""


def beta(member, proxy, min_n=20):
    """Beta of member to proxy from aligned daily closes {date: close}. None if too little data."""
    days = sorted(set(member) & set(proxy))
    rm, rp = [], []
    for a, b in zip(days, days[1:]):
        if member[a] and member[b] and proxy[a] and proxy[b]:
            rm.append(member[b] / member[a] - 1)
            rp.append(proxy[b] / proxy[a] - 1)
    if len(rp) < min_n:
        return None
    mm, mp = sum(rm) / len(rm), sum(rp) / len(rp)
    var = sum((x - mp) ** 2 for x in rp)
    if not var:
        return None
    return sum((x - mp) * (y - mm) for x, y in zip(rp, rm)) / var


def r2(member, proxy, min_n=20):
    """Share of the member's daily moves explained by the proxy (squared correlation)."""
    days = sorted(set(member) & set(proxy))
    rm, rp = [], []
    for a, b in zip(days, days[1:]):
        if member[a] and member[b] and proxy[a] and proxy[b]:
            rm.append(member[b] / member[a] - 1)
            rp.append(proxy[b] / proxy[a] - 1)
    n = len(rp)
    if n < min_n:
        return None
    mm, mp = sum(rm) / n, sum(rp) / n
    cov = sum((x - mp) * (y - mm) for x, y in zip(rp, rm))
    vp, vm = sum((x - mp) ** 2 for x in rp), sum((y - mm) ** 2 for y in rm)
    return (cov * cov) / (vp * vm) if vp and vm else None


def assign(member_closes, proxy_closes, default):
    """{ticker: proxy}: each member goes to the proxy with the highest R^2; `default` when unknown."""
    out = {}
    for t, mc in member_closes.items():
        best, best_r = default, -1.0
        for p, pc in proxy_closes.items():
            r = r2(mc, pc) if mc else None
            if r is not None and r > best_r:
                best, best_r = p, r
        out[t] = best
    return out


def crash(px, prev_close, sigma, closes20, lower, cfg):
    """Is the proxy in a crash? Returns (bool, plain reason)."""
    if not px or not prev_close or lower is None:
        return False, ""
    move = px / prev_close - 1
    z = move / sigma if sigma else 0
    hi = max(closes20) if closes20 else None
    dd = (px / hi - 1) if hi else 0
    # A crash is a FALL in progress: a deep drawdown only counts on a down day (5 Nov fix: SOXX was
    # 10 % under its 20-day high but up 4.7 % with momentum back over 30 when a hedge was suggested).
    shock = z <= -cfg["z"] or (dd <= -cfg["dd20_pct"] / 100 and move < 0)
    if not (shock and px < lower):
        return False, ""
    bits = []
    if z <= -cfg["z"]:
        bits.append(f"down {abs(move) * 100:.1f}% today, about {abs(z):.0f}x its usual daily move")
    if dd <= -cfg["dd20_pct"] / 100:
        bits.append(f"{abs(dd) * 100:.1f}% below its 20-day high")
    bits.append("below the bottom of its normal range")
    return True, ", ".join(bits)


def exposure(values, betas, default_beta=1.0):
    """Beta-weighted exposure, in proxy dollars. values/betas: {ticker: ...}."""
    tot, used = 0.0, {}
    for t, v in values.items():
        b = betas.get(t)
        b = default_beta if b is None else max(0.0, b)
        used[t] = b
        tot += v * b
    return tot, used


def size(expo, cfg, lev=1):
    return cfg["ratio"] * expo / lev


def should_exit(leg, proxy_px, proxy_signal_buy, proxy_mid, sessions):
    """leg: the stored leg. Returns a plain reason, or None to keep it."""
    if proxy_signal_buy:
        return f"your Rule A gave a BUY on {leg['proxy']}"
    if proxy_mid is not None and proxy_px > proxy_mid:
        return f"{leg['proxy']} is back above the middle of its normal range"
    if proxy_px >= leg["proxy_px"] * (1 + leg["rebound_pct"] / 100):
        return f"{leg['proxy']} rebounded {leg['rebound_pct']:.0f}% from where the hedge started"
    if sessions >= leg["max_sessions"]:
        return (f"the {leg['max_sessions']}-session limit for a {leg['lev']}x fund is up"
                + (" (a 3x fund held longer drifts away from 3x the fall)" if leg["lev"] > 1 else ""))
    return None
