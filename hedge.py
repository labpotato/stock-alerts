"""Rule H: a temporary hedge when a sector you are heavy in crashes (Dongmin 2026-11-06).
Pure functions, no network. Sizes come from the HOLDINGS_JSON secret and go to Telegram only;
nothing here is ever written to a file, because the repository is public.

Trigger (both):
  1. the group is at least `min_weight_pct` of the book;
  2. the group's proxy ETF fell at least `z` times its usual daily move today, or is at least
     `dd20_pct` below its 20-session high, AND it is below the bottom of its price range.
Size: hedge `ratio` of the beta-weighted group exposure; -1x inverse dollars = ratio x exposure,
      -3x dollars = that / 3.
Exit (first to happen): a Rule A BUY on the proxy; the proxy back above the middle of its range;
      the proxy 5 % above where the hedge started; the time limit (20 sessions -1x, 5 sessions -3x).
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


def crash(px, prev_close, sigma, closes20, lower, cfg):
    """Is the proxy in a crash? Returns (bool, plain reason)."""
    if not px or not prev_close or lower is None:
        return False, ""
    move = px / prev_close - 1
    z = move / sigma if sigma else 0
    hi = max(closes20) if closes20 else None
    dd = (px / hi - 1) if hi else 0
    shock = z <= -cfg["z"] or dd <= -cfg["dd20_pct"] / 100
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


def size(expo, cfg):
    d1 = cfg["ratio"] * expo
    return {"inverse": d1, "inverse3": d1 / 3}


def should_exit(h, proxy_px, proxy_signal_buy, proxy_mid, sessions):
    """h: the stored hedge. Returns a plain reason, or None to keep it."""
    if proxy_signal_buy:
        return "your Rule A gave a BUY on the sector ETF"
    if proxy_mid is not None and proxy_px > proxy_mid:
        return "the sector ETF is back above the middle of its normal range"
    if proxy_px >= h["proxy_px"] * (1 + h["rebound_pct"] / 100):
        return f"the sector ETF rebounded {h['rebound_pct']:.0f}% from where the hedge started"
    if sessions >= h["max_sessions"]:
        return f"the {h['max_sessions']}-session limit for a -1x hedge is up"
    if sessions >= h["max_sessions_3x"]:
        return f"KEEP_1X:{h['max_sessions_3x']}"
    return None
