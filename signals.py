"""Signal maths shared with the Claude post-close run. Pure functions: no network, no state.

Every convention here is listed in SYNC.md. Change it in both systems or not at all.
"""

KNIFE_K = 0.0005   # Keltner knife-edge: a deciding margin under 0.05 % of the band
KNIFE_R = 0.3      # RSI knife-edge: a deciding margin under 0.3 points
WINDOW = 220       # settled daily bars kept


# ---------------------------------------------------------------- indicators
def ema(c, n=20):
    """EMA of closes, seeded at the first close of the window."""
    k, e = 2 / (n + 1), [c[0]]
    for x in c[1:]:
        e.append(x * k + e[-1] * (1 - k))
    return e


def atr(rows, n=14):
    """Wilder ATR. rows are (open, high, low, close). The first true range is high - low;
    the seed is the mean of the first 14 true ranges."""
    c = [r[3] for r in rows]
    tr = [rows[0][1] - rows[0][2]] + [
        max(rows[i][1] - rows[i][2], abs(rows[i][1] - c[i-1]), abs(rows[i][2] - c[i-1]))
        for i in range(1, len(rows))]
    a = [None] * (n - 1) + [sum(tr[:n]) / n]
    for i in range(n, len(tr)):
        a.append((a[-1] * (n - 1) + tr[i]) / n)
    return a


def rsi(c, n=14):
    """Wilder RSI on closes."""
    g = [0.0] + [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    l = [0.0] + [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[1:n+1]) / n, sum(l[1:n+1]) / n
    out = [None] * n + [100 - 100 / (1 + ag / al) if al else 100.0]
    for i in range(n + 1, len(c)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out.append(100 - 100 / (1 + ag / al) if al else 100.0)
    return out


def series(rows):
    """Per-bar close, MA3, Keltner upper and lower (EMA20 +/- 2 x ATR14) and RSI14."""
    c = [r[3] for r in rows]
    e, a, r = ema(c), atr(rows), rsi(c)
    out = []
    for j in range(len(c)):
        out.append({
            "c": c[j],
            "ma3": sum(c[j-2:j+1]) / 3 if j >= 2 else None,
            "up": e[j] + 2 * a[j] if a[j] is not None else None,
            "lo": e[j] - 2 * a[j] if a[j] is not None else None,
            "rsi": r[j],
        })
    return out


def states(prev_c, prev_ma3, prev_up, prev_lo, prev_rsi, c, ma3, up, lo, rs):
    """The two legs.
    Keltner BUY  = prior MA3 <= prior lower AND current MA3 > current lower AND current MA3 > prior MA3
                   (the 3-day average itself must rise: a band that falls under a falling price is not
                   a recovery; rule added 2026-11-05).
    Keltner SELL = prior close > prior upper AND current close <= current upper.
    WATCH BUY = close < lower; WATCH SELL = close > upper.
    RSI BUY = prior < 30 and current >= 30; SELL = prior > 70 and current <= 70."""
    k = "NONE"
    if prev_ma3 is not None and prev_ma3 <= prev_lo and ma3 > lo and ma3 > prev_ma3:
        k = "BUY"
    elif prev_c > prev_up and c <= up:
        k = "SELL"
    elif c < lo:
        k = "WATCH BUY"
    elif c > up:
        k = "WATCH SELL"
    r = "NONE"
    if prev_rsi < 30 <= rs:
        r = "BUY"
    elif prev_rsi > 70 >= rs:
        r = "SELL"
    elif rs < 30:
        r = "WATCH BUY"
    elif rs > 70:
        r = "WATCH SELL"
    return k, r


# ---------------------------------------------------------------- deciding margins
def _edge(a_ok, a_m, b_ok, b_m, th):
    """A two-part rule 'A and B' is on a knife-edge if one part is within th of flipping
    while the other part holds (or is itself within th)."""
    return ((abs(a_m) < th and (b_ok or abs(b_m) < th)) or
            (abs(b_m) < th and (a_ok or abs(a_m) < th)))


def edges(prev, cur):
    """prev and cur hold c, ma3, up, lo, rsi. Returns the deciding margins and the knife-edge flags.
    margin_k_pct: Keltner BUY side (MA3 - lower) / lower, or SELL side (upper - close) / upper, in %,
                  for whichever side is in play (the side whose prior condition held, else the nearer).
    margin_r_pts: |RSI - 30| or |RSI - 70| for the side in play, in points."""
    kb_a = (prev["ma3"] - prev["lo"]) / prev["lo"]      # BUY needs <= 0
    kb_b = (cur["ma3"] - cur["lo"]) / cur["lo"]         # BUY needs  > 0
    ks_a = (prev["c"] - prev["up"]) / prev["up"]        # SELL needs > 0
    ks_b = (cur["up"] - cur["c"]) / cur["up"]           # SELL needs >= 0
    knife_k = (_edge(kb_a <= 0, kb_a, kb_b > 0, kb_b, KNIFE_K) or
               _edge(ks_a > 0, ks_a, ks_b >= 0, ks_b, KNIFE_K))
    if kb_a <= 0:
        mk = kb_b
    elif ks_a > 0:
        mk = ks_b
    else:
        mk = kb_b if abs(kb_b) <= abs(ks_b) else ks_b
    rb_a, rb_b = prev["rsi"] - 30, cur["rsi"] - 30      # BUY needs a < 0, b >= 0
    rs_a, rs_b = prev["rsi"] - 70, 70 - cur["rsi"]      # SELL needs a > 0, b >= 0
    knife_r = (_edge(rb_a < 0, rb_a, rb_b >= 0, rb_b, KNIFE_R) or
               _edge(rs_a > 0, rs_a, rs_b >= 0, rs_b, KNIFE_R))
    if rb_a < 0 or cur["rsi"] < 30:          # a BUY is in play
        mr = abs(cur["rsi"] - 30)
    elif rs_a > 0 or cur["rsi"] > 70:        # a SELL is in play
        mr = abs(cur["rsi"] - 70)
    else:
        mr = min(abs(cur["rsi"] - 30), abs(cur["rsi"] - 70))
    return {"margin_k_pct": round(mk * 100, 4), "margin_r_pts": round(mr, 3),
            "knife_k": knife_k, "knife_r": knife_r, "knife_edge": knife_k or knife_r,
            "prior_k_pct": round((kb_a if kb_a <= 0 else ks_a) * 100, 4)}


# ---------------------------------------------------------------- settled data hygiene
def hygiene(bars, volume_rule=True, flat_rule=True, partial_tape=True):
    """bars: list of dicts with date, o, h, l, c, v (oldest first). Returns (kept, dropped).
    Drop a row only if volume == 0 or open == high == low == close. A row with open == high
    alone is a real bar and is kept. Then drop the newest row if its volume is under 30 % of
    the prior session's (a partial tape). Currencies have no volume, so volume rules are off
    for them; thin SGX listings keep flat bars."""
    kept, dropped = [], []
    for b in bars:
        if None in (b["o"], b["h"], b["l"], b["c"]):
            dropped.append((b["date"], "missing price"))
        elif volume_rule and b.get("v") == 0:            # a missing volume is unknown, not zero
            dropped.append((b["date"], "volume 0"))
        elif flat_rule and b["o"] == b["h"] == b["l"] == b["c"]:
            dropped.append((b["date"], "open = high = low = close"))
        else:
            kept.append(b)
    if partial_tape and volume_rule and len(kept) >= 2:
        last, prior = kept[-1].get("v"), kept[-2].get("v")
        if last is not None and prior and last < 0.30 * prior:
            dropped.append((kept[-1]["date"], f"partial tape ({last} vs {prior})"))
            kept = kept[:-1]
    return kept, dropped


# ---------------------------------------------------------------- settled state
def settled_state(rows, dates):
    """Close-to-close state of the newest settled bar, exactly as the post-close run computes it.
    STRONG = both legs fired the same side within the last 3 sessions, at least one today.
    CONFLICT = opposite legs today; it fires nothing."""
    s = series(rows)
    i = len(s) - 1
    legs = {}
    for j in (i - 2, i - 1, i):
        p, q = s[j - 1], s[j]
        legs[j] = states(p["c"], p["ma3"], p["up"], p["lo"], p["rsi"],
                         q["c"], q["ma3"], q["up"], q["lo"], q["rsi"])
    k, r = legs[i]
    today = {x for x in (k, r) if x in ("BUY", "SELL")}
    if today == {"BUY", "SELL"}:
        signal = "CONFLICT"
    elif today:
        side = today.pop()
        k3 = any(legs[j][0] == side for j in legs)
        r3 = any(legs[j][1] == side for j in legs)
        signal = f"STRONG {side}" if (k3 and r3) else side
    else:
        signal = "NONE"
    e = edges(s[i - 1], s[i])
    cur, prev = s[i], s[i - 1]
    return {
        "bar_date": str(dates[i]),
        "close": cur["c"], "ma3": cur["ma3"], "upper": cur["up"], "lower": cur["lo"], "rsi": cur["rsi"],
        "k_state": k, "r_state": r, "signal": signal,
        "margin_k_pct": e["margin_k_pct"], "margin_r_pts": e["margin_r_pts"],
        "knife_edge": e["knife_edge"], "knife_k": e["knife_k"], "knife_r": e["knife_r"],
        "prior_k_pct": e["prior_k_pct"],
        "prev": {"close": prev["c"], "ma3": prev["ma3"], "upper": prev["up"], "lower": prev["lo"],
                 "rsi": prev["rsi"], "date": str(dates[i - 1])},
    }


# ---------------------------------------------------------------- stock splits and reverse splits
SPLIT_MOVE = 0.33   # only a jump of at least a third is checked against split ratios
SPLIT_TOL = 0.07    # within 7 % of a split ratio "looks like" that split (a split day can move too)
_SPLITS = sorted({float(k) for k in (3/2, 2, 5/2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100)})
SPLIT_RATIOS = sorted(set(_SPLITS) | {1 / k for k in _SPLITS})


def split_like(old_px, new_px):
    """The split ratio k (new shares per old share: 3 for a 3-for-1 split, 0.1 for a
    1-for-10 reverse split) that would explain a move from old_px to new_px, else None."""
    import math
    if not old_px or not new_px or old_px <= 0 or new_px <= 0:
        return None
    r = old_px / new_px
    if abs(math.log(r)) < math.log(1 / (1 - SPLIT_MOVE)):
        return None
    best = min(SPLIT_RATIOS, key=lambda k: abs(math.log(r / k)))
    return best if abs(r / best - 1) < SPLIT_TOL else None


def adjust_splits(bars, splits, ref_px=None):
    """bars: oldest first, dicts with date, o, h, l, c, v. splits: [(date, k)].
    Yahoo normally back-adjusts history for splits, but around the split day it can still serve
    unadjusted rows. Rows before a split are divided by k only if they are still unadjusted, that is,
    when the last pre-split close is nearer k x the first post-split price than the price itself.
    The first post-split price is the first post-split bar's open (or close), else ref_px (the live
    price, for a split that takes effect today). Volumes are multiplied by k.
    Returns (bars, applied) where applied lists the (date, k) that were applied here."""
    import math
    applied = []
    for sd, k in sorted(splits):
        if not k or k <= 0 or abs(k - 1) < 1e-9:
            continue
        pre = [b for b in bars if b["date"] < sd]
        post = [b for b in bars if b["date"] >= sd and (b.get("o") or b.get("c"))]
        last = next((b["c"] for b in reversed(pre) if b.get("c")), None)
        ref = (post[0].get("o") or post[0].get("c")) if post else ref_px
        if not last or not ref:
            continue
        r = last / ref
        if abs(math.log(r / k)) < abs(math.log(r)):
            out = []
            for b in bars:
                if b["date"] < sd:
                    b = dict(b, **{f: (b[f] / k if b.get(f) is not None else None) for f in ("o", "h", "l", "c")})
                    if b.get("v") is not None:
                        b["v"] = b["v"] * k
                out.append(b)
            bars = out
            applied.append((sd, k))
    return bars, applied


def split_text(k):
    """'3-for-1 split' or '1-for-10 reverse split'."""
    from fractions import Fraction
    f = Fraction(k).limit_denominator(100)
    if f >= 1:
        return f"{f.numerator}-for-{f.denominator} split"
    return f"{f.numerator}-for-{f.denominator} reverse split"
