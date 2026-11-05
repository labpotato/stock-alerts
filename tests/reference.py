"""Independent reference implementation of the shared formulas, written from the rule text in
SYNC.md rather than copied from signals.py, so a mistake in one is caught by the other."""


def ema20(closes):
    alpha = 2.0 / 21.0
    out, cur = [], None
    for x in closes:
        cur = x if cur is None else alpha * x + (1.0 - alpha) * cur   # seeded at the first close
        out.append(cur)
    return out


def true_ranges(bars):
    out = []
    for i, (o, h, l, c) in enumerate(bars):
        if i == 0:
            out.append(h - l)
        else:
            pc = bars[i - 1][3]
            out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def atr14(bars):
    tr = true_ranges(bars)
    out = [None] * len(tr)
    if len(tr) < 14:
        return out
    cur = sum(tr[0:14]) / 14.0                 # seed: mean of the first 14 true ranges
    out[13] = cur
    for i in range(14, len(tr)):
        cur = cur + (tr[i] - cur) / 14.0       # Wilder smoothing, algebraically (13*a + tr)/14
        out[i] = cur
    return out


def rsi14(closes):
    out = [None] * len(closes)
    if len(closes) < 15:
        return out
    ups = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    dns = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    au, ad = sum(ups[:14]) / 14.0, sum(dns[:14]) / 14.0
    out[14] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    for i in range(15, len(closes)):
        au = (au * 13.0 + ups[i - 1]) / 14.0
        ad = (ad * 13.0 + dns[i - 1]) / 14.0
        out[i] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    return out


def bands(bars):
    closes = [b[3] for b in bars]
    e, a = ema20(closes), atr14(bars)
    up = [None if a[i] is None else e[i] + 2 * a[i] for i in range(len(bars))]
    lo = [None if a[i] is None else e[i] - 2 * a[i] for i in range(len(bars))]
    ma3 = [None if i < 2 else (closes[i] + closes[i - 1] + closes[i - 2]) / 3.0 for i in range(len(bars))]
    return {"up": up, "lo": lo, "ma3": ma3, "rsi": rsi14(closes)}
