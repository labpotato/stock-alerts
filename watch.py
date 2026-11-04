# Keltner(MA3) + RSI14 watcher on real-time prices, including pre-market and after-hours,
# with anomaly detection and a public status feed. Trades nothing.
import json, os, re, io, sys, time, math, random, uuid, subprocess, urllib.request, urllib.error, datetime as dt

TOK, CHAT = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", "")
MODE = os.environ.get("MODE", "poll")           # poll | test | digest
CFG = json.load(open("config.json"))
STATE, CACHE = "state.json", "bars_cache.json"
STATUS = "docs/status.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-alerts/2)"}
AN = CFG["anomaly"]
THIN = set(CFG.get("thin", []))
# "last_poll": a signal is a cross between two consecutive real-time readings (the rule).
# "prev_close": the original definition, a cross versus the previous settled daily close.
REALTIME = CFG.get("cross_reference", "last_poll") == "last_poll"
CONFIRM_THIN = CFG.get("confirm_thin_extended", True)


# ---------------------------------------------------------------- holdings (secret)
def holdings():
    raw = os.environ.get("HOLDINGS_JSON", "").strip()
    if not raw:
        return None
    try:
        h = json.loads(raw)
        if "tickers" not in h:
            return None
        return h
    except Exception as e:
        print(f"HOLDINGS_JSON is not valid JSON: {e}", file=sys.stderr)
        return None


H = holdings()


# ---------------------------------------------------------------- http
def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(1.5 + 2.5 * i + random.random())
    raise last


_MEMO = {}


def chart(t, rng, iv, prepost=False):
    """A reversed currency pair (e.g. KRW->SGD) is computed from its partner (SGD->KRW).
    Yahoo quotes KRWSGD=X rounded to 0.0010, which makes every tick look like a 5% jump,
    and computing both directions from one source keeps mirror pairs exactly consistent."""
    inv = (CFG.get("fx", {}).get(t) or {}).get("invert_of")
    src = inv or t
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{src}"
         f"?range={rng}&interval={iv}" + ("&includePrePost=true" if prepost else ""))
    if u not in _MEMO:
        _MEMO[u] = get(u)["chart"]["result"][0]
    return invert_chart(_MEMO[u]) if inv else _MEMO[u]


def invert_chart(res):
    r = json.loads(json.dumps(res))
    inv = lambda x: (1.0 / x) if x else None
    for q in (r.get("indicators") or {}).get("quote") or []:
        o, h, l, c = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or []
        q["open"], q["close"] = [inv(x) for x in o], [inv(x) for x in c]
        q["high"], q["low"] = [inv(x) for x in l], [inv(x) for x in h]   # the low becomes the high
    m = r.get("meta") or {}
    for k in ("regularMarketPrice", "chartPreviousClose", "previousClose",
              "regularMarketDayHigh", "regularMarketDayLow"):
        if m.get(k):
            m[k] = 1.0 / m[k]
    m.pop("longName", None); m.pop("shortName", None)
    return r


# ---------------------------------------------------------------- indicators
def ema(c, n=20):
    k, e = 2 / (n + 1), [c[0]]
    for x in c[1:]:
        e.append(x * k + e[-1] * (1 - k))
    return e


def atr(rows, n=14):
    c = [r[3] for r in rows]
    tr = [rows[0][1] - rows[0][2]] + [
        max(rows[i][1] - rows[i][2], abs(rows[i][1] - c[i-1]), abs(rows[i][2] - c[i-1]))
        for i in range(1, len(rows))]
    a = [None] * (n - 1) + [sum(tr[:n]) / n]
    for i in range(n, len(tr)):
        a.append((a[-1] * (n - 1) + tr[i]) / n)
    return a


def rsi(c, n=14):
    g = [0.0] + [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    l = [0.0] + [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[1:n+1]) / n, sum(l[1:n+1]) / n
    out = [None] * n + [100 - 100 / (1 + ag / al) if al else 100.0]
    for i in range(n + 1, len(c)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out.append(100 - 100 / (1 + ag / al) if al else 100.0)
    return out


def states(prev_c, prev_ma3, prev_up, prev_lo, prev_rsi, c, ma3, up, lo, rs):
    """The two legs, exactly as the rules define them. prev_* is the last settled bar."""
    k = "NONE"
    if prev_ma3 is not None and prev_ma3 <= prev_lo and ma3 > lo:
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


def robust_sigma(rets):
    """MAD-scaled sigma. A single crash day barely moves the median, so it stays visible.
    Plain stdev is inflated BY the outlier and hides it (GOOGL, 2026-11-02: z=-0.1 on stdev)."""
    if len(rets) < 8:
        return None
    med = sorted(rets)[len(rets) // 2]
    mad = sorted(abs(x - med) for x in rets)[len(rets) // 2]
    s = mad * 1.4826
    return s if s > 1e-5 else None


# ---------------------------------------------------------------- daily bar cache
def session_of(meta, now=None):
    """PRE / REGULAR / POST / CLOSED from Yahoo's own trading periods. DST-proof."""
    now = time.time() if now is None else now
    ctp = meta.get("currentTradingPeriod") or {}
    for name, key in (("PRE", "pre"), ("REGULAR", "regular"), ("POST", "post")):
        p = ctp.get(key) or {}
        s, e = p.get("start"), p.get("end")
        if s is not None and e is not None and s <= now < e:
            return name
    if meta.get("marketState") in ("PRE", "REGULAR", "POST"):
        return meta["marketState"]
    return "CLOSED"


def settled_rows(t):
    """Daily OHLC through the last COMPLETED session (today's running bar removed)."""
    r = chart(t, "1y", "1d")
    q = r["indicators"]["quote"][0]
    meta = r["meta"]
    off = meta.get("gmtoffset", 0)
    day = lambda ts: dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date()
    rows, dates = [], []
    for ts, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None in (o, h, l, c):
            continue
        rows.append((o, h, l, c))
        dates.append(day(ts))
    if not rows:
        return None
    sess = session_of(meta)
    today_x = dt.datetime.fromtimestamp(time.time() + off, dt.timezone.utc).date()
    mt = meta.get("regularMarketTime")
    last_session = day(mt) if mt is not None else None
    # The live price always stands in for the CURRENT session's bar, so that bar is removed here:
    #  - market open (pre, regular, after hours): today's bar, which is still forming;
    #  - market shut: the most recent session's bar, because the live price IS that session.
    # Keeping it while shut would count the same close twice and shift every reading.
    if sess in ("PRE", "REGULAR", "POST"):
        if dates[-1] == today_x:
            rows, dates = rows[:-1], dates[:-1]
    elif last_session is not None:
        # Also drop any early row Yahoo adds for a day that has not traded yet.
        while dates and dates[-1] >= last_session:
            rows, dates = rows[:-1], dates[:-1]
    if len(rows) < 40:
        return None
    return {"rows": rows[-220:], "dates": [x.isoformat() for x in dates[-220:]],
            "last": dates[-1].isoformat()}


def derive(rows, dates=None):
    """Everything a live poll needs, computed once per day from settled bars."""
    c = [r[3] for r in rows]
    e, a, rv = ema(c), atr(rows), rsi(c)
    i = len(c) - 1
    N = min(60, len(c) - 20)   # history kept for the alert charts (needs ~20 days of warm-up)
    hist = {
        "dates": (dates or [""] * len(c))[-N:],
        "c": [sig6(x) for x in c[-N:]],
        "up": [sig6(e[j] + 2 * a[j]) for j in range(len(c) - N, len(c))],
        "lo": [sig6(e[j] - 2 * a[j]) for j in range(len(c) - N, len(c))],
        "ma3": [sig6(sum(c[j-2:j+1]) / 3) for j in range(len(c) - N, len(c))],
        "rsi": [round(rv[j], 2) for j in range(len(c) - N, len(c))],
    } if N >= 20 else None
    rets = [c[j] / c[j-1] - 1 for j in range(max(1, len(c) - 60), len(c))]
    return {
        "c": c[-3:],                       # last three settled closes (MA3 needs two of them)
        "h": rows[-1][1], "l": rows[-1][2],
        "ema": e[i], "atr": a[i],
        "up": e[i] + 2 * a[i], "lo": e[i] - 2 * a[i],
        "ma3": sum(c[-3:]) / 3,
        "rsi": rv[i],
        "ag": None, "al": None,            # filled below
        "sigma": robust_sigma(rets),
        "closes60": c[-6:],                # for 3-day and 5-day run checks
        "hist": hist,
    }


def rsi_acc(c, n=14):
    g = [0.0] + [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    l = [0.0] + [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[1:n+1]) / n, sum(l[1:n+1]) / n
    for i in range(n + 1, len(c)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return ag, al


def build_cache(tickers, market_date=None):
    """Refresh each ticker's settled-bar state at most ONCE per market day.
    Attempts are tracked per ticker, so one failing symbol cannot force the
    other fifteen to refetch on every poll."""
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    fresh, errs = 0, []
    for t in tickers:
        have = cache.get(t)
        if (isinstance(have, dict) and market_date and have.get("_tried") == market_date
                and ("hist" in have or "ema" not in have)):
            if "ema" not in have:
                errs.append(f"{t}: {have.get('_err', 'no bars')} (retry tomorrow)")
            continue
        try:
            sr = settled_rows(t)
            if not sr:
                raise ValueError("fewer than 40 usable daily bars")
            d = derive(sr["rows"], sr["dates"])
            d["ag"], d["al"] = rsi_acc([r[3] for r in sr["rows"]])
            d["last"] = sr["last"]
            d["_tried"] = market_date
            cache[t] = d
            fresh += 1
        except Exception as e:
            errs.append(f"{t}: {e}")
            keep = have if isinstance(have, dict) and "ema" in have else {}
            keep = dict(keep)
            keep["_tried"] = market_date
            keep["_err"] = str(e)[:120]
            cache[t] = keep
    json.dump(cache, open(CACHE, "w"))
    return cache, fresh, errs


# ---------------------------------------------------------------- live price
def live(t):
    """Latest trade including pre and post market, plus the prior settled close."""
    r = chart(t, "1d", "1m", prepost=True)
    meta = r["meta"]
    sess = session_of(meta)
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    px = vol = None
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes, vols = q.get("close") or [], q.get("volume") or []
    for i in range(len(closes) - 1, -1, -1):
        if closes[i] is not None:
            px, vol = closes[i], (vols[i] if i < len(vols) else None)
            break
    if sess == "REGULAR" and meta.get("regularMarketPrice") is not None:
        px = meta["regularMarketPrice"]
    if px is None:
        px = meta.get("regularMarketPrice")
    hi = max([x for x in (q.get("high") or []) if x is not None] or [px or 0]) if px else None
    lowv = [x for x in (q.get("low") or []) if x is not None]
    lo = min(lowv) if lowv else px
    mt = meta.get("regularMarketTime")
    off = meta.get("gmtoffset", 0)
    mdate = (dt.datetime.fromtimestamp(mt + off, dt.timezone.utc).date().isoformat()
             if mt is not None else None)
    edate = dt.datetime.fromtimestamp(time.time() + off, dt.timezone.utc).date().isoformat()
    name = meta.get("longName") or meta.get("shortName")
    return {"px": px, "prev": prev, "sess": sess, "hi": hi, "lo": lo, "mdate": mdate, "edate": edate,
            "name": name,
            "n": sum(1 for x in closes if x is not None)}


# ---------------------------------------------------------------- evaluate
def evaluate(d, px, hi, lo, ref=None):
    """Bands, MA3 and RSI at the live price. A cross is judged against `ref`, the previous
    real-time reading (px, ma3, up, lo, rsi). With no reference, the last settled close is used."""
    pc = d["c"][-1]
    hi = max(hi or px, px, pc) if px else pc
    lo = min(lo or px, px, pc) if px else pc
    tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
    a = (d["atr"] * 13 + tr) / 14
    e = px * (2 / 21) + d["ema"] * (1 - 2 / 21)
    up, lod = e + 2 * a, e - 2 * a
    ma3 = (d["c"][-2] + d["c"][-1] + px) / 3 if len(d["c"]) >= 2 else px
    g, l = max(px - pc, 0), max(pc - px, 0)
    ag = (d["ag"] * 13 + g) / 14
    al = (d["al"] * 13 + l) / 14
    rs = 100 - 100 / (1 + ag / al) if al else 100.0
    if ref:
        k, r = states(ref["px"], ref["ma3"], ref["up"], ref["lo"], ref["rsi"], px, ma3, up, lod, rs)
    else:
        k, r = states(pc, d["ma3"], d["up"], d["lo"], d["rsi"], px, ma3, up, lod, rs)
    return {"k": k, "r": r, "rsi": rs, "up": up, "lo": lod, "ma3": ma3, "px": px, "prev": pc}


def obs(ev):
    return {k: ev[k] for k in ("px", "ma3", "up", "lo", "rsi")}


def trigger(d, side, ref=None, span=0.45, n=600):
    """Lowest price (SELL: highest) on the next reading that would print `side`, per leg."""
    pc = d["c"][-1]
    out = {}
    for j in range(n + 1):
        X = pc * (1 - span) + (pc * (1 + span) - pc * (1 - span)) * j / n
        ev = evaluate(d, X, max(pc, X), min(pc, X), ref)
        for leg, key in (("K", "k"), ("R", "r")):
            if ev[key] == side:
                cur = out.get(leg)
                if side == "BUY":
                    out[leg] = X if cur is None else min(cur, X)
                else:
                    out[leg] = X if cur is None else max(cur, X)
    return out


# ---------------------------------------------------------------- plain-language helpers
NAMES = CFG.get("names", {})
FX = CFG.get("fx", {})


def is_fx(t):
    return t in FX


def label(t):
    return FX[t]["label"] if is_fx(t) else t


def num(t, x):
    """Enough digits to be useful: 1,052.40 KRW, 1.3012 SGD, 0.000951 SGD."""
    if not is_fx(t):
        return f"{x:,.2f}"
    ax = abs(x)
    return f"{x:,.2f}" if ax >= 100 else (f"{x:,.4f}" if ax >= 1 else f"{x:.6f}")


def money(t, x):
    return f"{num(t, x)} {FX[t]['quote']}" if is_fx(t) else "$" + f"{x:,.2f}"


def rate(t, x):
    """The headline price: '1 USD = 1.3012 SGD' for a currency, '$183.00' for a stock."""
    return f"1 {FX[t]['base']} = {num(t, x)} {FX[t]['quote']}" if is_fx(t) else "$" + f"{x:,.2f}"


def sess_text(t, sess):
    if is_fx(t):
        return "currency market open" if sess != "CLOSED" else "currency market closed"
    return SESSION_PLAIN.get(sess, sess)


def fx_open(now=None):
    """Currencies trade from Sunday ~21:00 UTC to Friday ~22:00 UTC. A reading in the gap
    returns Friday's last rate, which cannot cross anything, so the edges are harmless."""
    u = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc)
    wd, h = u.weekday(), u.hour            # Monday = 0
    if wd == 5:
        return False
    if wd == 6 and h < 21:
        return False
    if wd == 4 and h >= 22:
        return False
    return True


def sig6(x):
    return float(f"{x:.6g}") if x is not None else None
SESSION_PLAIN = {"PRE": "pre-market, before the US open", "REGULAR": "market open",
                 "POST": "after hours, after the US close", "CLOSED": "market closed"}


def full_name(t, o=None):
    if is_fx(t):
        return FX[t]["name"]
    return NAMES.get(t) or (o or {}).get("name") or t


def when_text():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        sg = now.astimezone(ZoneInfo("Asia/Singapore"))
        ny = now.astimezone(ZoneInfo("America/New_York"))
        return f"{sg.day} {sg:%b %H:%M} Singapore · {ny:%H:%M} New York"
    except Exception:
        return f"{now:%d %b %H:%M} UTC"


def order_plain(order):
    m = re.search(r"(BUY|SELL)\s+(\d+)", order or "")
    if not m:
        return order
    return f"{'buy' if m.group(1) == 'BUY' else 'sell'} {m.group(2)} shares"


CURRENCY = {"USD": "US dollars", "SGD": "Singapore dollars", "KRW": "Korean won"}


def fx_action(t, side):
    """Which money to change into which. BUY on 'USD->SGD' means USD is cheap: turn SGD into USD.
    SELL means USD is expensive: turn USD into SGD. Mirror pairs therefore give the same instruction."""
    b, q = FX[t]["base"], FX[t]["quote"]
    frm, to = (q, b) if side == "BUY" else (b, q)
    return frm, to


def fx_do(t, side):
    frm, to = fx_action(t, side)
    return f"👉 <b>Change your {CURRENCY.get(frm, frm)} into {CURRENCY.get(to, to)}</b> ({frm} → {to})"


def plan_text(t, side):
    """What your holdings plan says this signal means, in plain words."""
    if is_fx(t):
        frm, to = fx_action(t, side)
        return (f"<b>Change {frm} into {to}.</b> Right now your {CURRENCY.get(frm, frm)} buy more "
                f"{CURRENCY.get(to, to)} than they usually do.")
    if H is None:
        return "Your holdings aren't loaded, so I can't say whether this triggers an order."
    cf = H["tickers"].get(t)
    if not cf or cf.get("target", 0) == 0:
        return "You don't hold this and it isn't in your plan. For information only."
    order = cf.get("order", "")
    cap = H.get("ai_group_now_pct")
    if side == "BUY":
        if "SELL" in order:
            return "No order. You already own more of this than your target, so a buy signal doesn't add more."
        if "BUY" not in order:
            return "No order. You already hold about your target amount."
        if t in CFG["ai_group"] and cap is not None and cap > CFG["ai_cap_pct"]:
            return (f"No order. Your AI stocks are {cap}% of your money, above your "
                    f"{CFG['ai_cap_pct']}% limit, so buys in this group are on hold.")
        return f"<b>This would trigger your prepared order: {order_plain(order)}.</b>"
    if "SELL" in order:
        return f"<b>This would trigger your prepared order: {order_plain(order)}.</b>"
    if "BUY" in order:
        return "No order. You own less of this than your target, so you don't sell. It does mean: hold off buying more for now."
    return "No order. You already hold about your target amount."


# The two measures, in words.
#   "k" = price range  (Keltner channel around the 3-day average)
#   "r" = momentum     (RSI, a 0-100 score of how hard the price has been rising or falling)
LEG = {"k": "Price range", "r": "Momentum"}


def leg_state(leg, ev):
    t = ev.get("t", "")
    s = ev[leg]
    if leg == "k":
        return {"WATCH SELL": f"above the top of its normal range ({money(t, ev['up'])}), running hot",
                "WATCH BUY": f"below the bottom of its normal range ({money(t, ev['lo'])}), unusually low",
                "NONE": "inside its normal range"}.get(s, s)
    return {"WATCH SELL": f"overheated (score {ev['rsi']:.0f}, above 70)",
            "WATCH BUY": f"sold off hard (score {ev['rsi']:.0f}, below 30)",
            "NONE": f"normal (score {ev['rsi']:.0f})"}.get(s, s)


def why(leg, side, ref, ev):
    t = ev.get("t", "")
    px = ev["px"]
    if leg == "k" and side == "SELL":
        return (f"<b>Price range says SELL.</b> It had climbed above the top of its normal price "
                f"range ({money(t, ref['up'])}), which is unusually high for it. It has now fallen back "
                f"inside, to {money(t, px)}. Slipping back into the range after running hot is your sell signal.")
    if leg == "k" and side == "BUY":
        return (f"<b>Price range says BUY.</b> Its 3-day average price had sunk to the bottom of its "
                f"normal range or lower ({money(t, ref['lo'])}), which is unusually low for it. The 3-day "
                f"average has now climbed back above it ({money(t, ev['ma3'])} vs {money(t, ev['lo'])}). "
                f"Recovering back into the range is your buy signal.")
    if leg == "r" and side == "SELL":
        return (f"<b>Momentum says SELL.</b> Its momentum score (0 to 100; high means it has been "
                f"rising hard) was {ref['rsi']:.0f}, above 70, so it was overheated. It has now cooled "
                f"to {ev['rsi']:.0f}. Dropping back under 70 is your sell signal.")
    return (f"<b>Momentum says BUY.</b> Its momentum score (0 to 100; low means it has been falling "
            f"hard) was {ref['rsi']:.0f}, below 30, so it was heavily sold off. It has now recovered "
            f"to {ev['rsi']:.0f}. Climbing back over 30 is your buy signal.")


def signal_message(o, side, legs, earlier, sess, held=False):
    """One message for one ticker: verdict from the two measures, reasons, plan, chart."""
    t, ev, ref = o["t"], o["ev"], o["ref"]
    said = set(legs) | set(earlier)
    strong = said == {"k", "r"}
    verdict = f"STRONG {side}" if strong else side
    dot = "🟢" if side == "BUY" else "🔴"
    combo = lambda l: side if l in said else "—"
    icons = "".join(dot if l in said else "⚪" for l in ("k", "r"))
    lines = [f"{icons} <b>{verdict} — {label(t)}</b>",
             esc(full_name(t, o)),
             f"<b>{rate(t, ev['px'])}</b> · {when_text()} · {sess_text(t, sess)}",
             ""]
    if is_fx(t):
        lines += [fx_do(t, side), ""]
    lines += [
             f"Price range <b>{combo('k')}</b> + Momentum <b>{combo('r')}</b>  →  <b>{verdict}</b>",
             ""]
    for l in ("k", "r"):
        if l in legs:
            lines.append(why(l, side, ref, ev))
        elif l in earlier:
            lines.append(f"<b>{LEG[l]} already said {side} earlier today.</b> Now both measures agree.")
        else:
            lines.append(f"{LEG[l]} is {leg_state(l, ev)}, so it doesn't add to this signal.")
        lines.append("")
    if sess in ("PRE", "POST"):
        lines.append("<i>This is an extended-hours price. Fewer people trade outside normal hours, "
                     "so prices can jump around more.</i>")
    if held:
        lines.append("<i>Checked on two readings 10 minutes apart, to rule out a one-off odd trade.</i>")
    lines += [f"<b>{'What it means' if is_fx(t) else 'Your plan'}:</b> {plan_text(t, side)}", "",
              "<i>Alert only. Nothing has been traded.</i>"]
    note = {"SELL": "fell back inside its normal range" if "k" in legs else "momentum cooled below 70",
            "BUY": "recovered into its normal range" if "k" in legs else "momentum recovered above 30"}[side]
    head = (f"{verdict}: change {fx_action(t, side)[0]} into {fx_action(t, side)[1]}" if is_fx(t)
            else f"{verdict} at {money(t, ev['px'])}")
    png = price_chart(t, full_name(t, o), o["d"], ev, head,
                      "#2e7d32" if side == "BUY" else "#c62828", note)
    return "\n".join(lines).replace("\n\n\n", "\n\n"), png


def mixed_message(o, sess):
    t, ev, ref = o["t"], o["ev"], o["ref"]
    kside, rside = ev["k"], ev["r"]
    lines = [f"⚠️ <b>MIXED — {label(t)}</b>", esc(full_name(t, o)),
             f"<b>{rate(t, ev['px'])}</b> · {when_text()} · {sess_text(t, sess)}", "",
             f"Price range <b>{kside}</b> + Momentum <b>{rside}</b>  →  <b>the two measures disagree</b>", "",
             why("k", kside, ref, ev), "", why("r", rside, ref, ev), "",
             ("<b>What it means:</b> the two measures disagree, so <b>don't change money on this one</b>."
              if is_fx(t) else "<b>Your plan:</b> your rules say a mixed signal triggers nothing."), "",
             "<i>Alert only. Nothing has been traded.</i>"]
    png = price_chart(t, full_name(t, o), o["d"], ev, "MIXED: measures disagree", "#ef6c00",
                      "the two measures point opposite ways")
    return "\n".join(lines), png


def near_buy_message(o, sess, lvl, leg):
    t, ev = o["t"], o["ev"]
    pct = (lvl / ev["px"] - 1) * 100
    if leg == "k":
        how = (f"<b>Price range:</b> its 3-day average price ({money(t, ev['ma3'])}) is at or below the bottom of "
               f"its normal range ({money(t, ev['lo'])}), which is unusually low for it. If the price climbs to "
               f"<b>{money(t, lvl)}</b>, the 3-day average lifts back above the bottom. That recovery is your buy signal.")
        other = f"Momentum is {leg_state('r', ev)}."
    else:
        how = (f"<b>Momentum:</b> its score is {ev['rsi']:.0f}, below 30, so it has been sold off hard. If the "
               f"price climbs to <b>{money(t, lvl)}</b>, the score recovers above 30. That is your buy signal.")
        other = f"Price range: {leg_state('k', ev)}."
    lines = [f"🔵 <b>Close to a BUY — {label(t)}</b>", esc(full_name(t, o)),
             f"<b>{rate(t, ev['px'])}</b> · {when_text()} · {sess_text(t, sess)}", "",
             f"<b>Not a signal yet.</b> A BUY appears if the price reaches <b>{money(t, lvl)}</b> "
             f"({pct:+.1f}% from now).", "", how, "", other, "",
             (f"<b>If it happens:</b> {fx_do(t, 'BUY')}" if is_fx(t)
              else f"<b>If it happens, your plan:</b> {plan_text(t, 'BUY')}"), "",
             "<i>Sent because you asked. Nothing has been traded.</i>"]
    png = price_chart(t, full_name(t, o), o["d"], ev, f"Close to a BUY: {pct:+.1f}% away", "#1565c0",
                      "not a signal yet", target=lvl, target_label="BUY if it reaches")
    return "\n".join(lines), png


def near_buy(rows, sess):
    """On request: one message per ticker within reach of a BUY, nearest first."""
    lim = CFG.get("near_pct", 5.0)
    cand = []
    for o in rows:
        cap = CFG.get("near_pct_fx", 1.0) if is_fx(o["t"]) else lim
        for leg, key in (("k", "K"), ("r", "R")):
            lvl = (o.get("tb") or {}).get(key)
            if lvl is not None and 0 <= (lvl / o["px"] - 1) * 100 <= cap:
                cand.append((lvl / o["px"] - 1, o, lvl, leg))
    best = {}
    for c in sorted(cand, key=lambda x: x[0]):
        best.setdefault(c[1]["t"], c)
    out = [near_buy_message(o, o.get("sess", sess), lvl, leg)
           for _, o, lvl, leg in sorted(best.values(), key=lambda x: x[0])]
    if not out:
        out = [(f"🔵 <b>Close to a BUY</b>\n{when_text()} · {SESSION_PLAIN.get(sess, sess)}\n\n"
                f"No ticker is within {lim:.0f}% of a BUY right now.\n\n<i>Sent because you asked.</i>", None)]
    return out


# ---------------------------------------------------------------- unusual movement
def anomalies(rows, bench):
    """Each finding is its own message: dict(key, kind, ticker, text)."""
    out = []
    for o in rows:
        t, d, px, sig = o["t"], o["d"], o["px"], o["d"].get("sigma")
        pc = d["c"][-1]
        mv = (px / pc - 1) * 100
        if not sig:
            continue
        z = (px / pc - 1) / sig
        o["z"] = z
        usual = sig * 100
        floor = AN.get("abs_floor_pct_fx", 1.0) if is_fx(t) else AN["abs_floor_pct"]
        big = abs(z) >= AN["sigma_single"]
        mid = abs(z) >= AN["sigma_single_with_abs"] and abs(mv) >= floor
        if big or mid:
            out.append({"key": f"anom:move:{t}", "kind": "move", "ticker": t, "shade": 0,
                        "title": f"Unusual move — {label(t)}", "chart_head": "Unusual move",
                        "note": f"{'up' if mv > 0 else 'down'} {abs(mv):.1f}% today",
                        "text": (f"<b>{rate(t, px)}</b>, {'up' if mv > 0 else 'down'} <b>{abs(mv):.1f}%</b> today.\n\n"
                                 f"On a normal day it moves about {usual:.1f}% either way, so today's move is "
                                 f"roughly <b>{abs(z):.0f} times bigger than usual</b>. Something may be "
                                 f"happening with this one; worth checking the news.")})
        c6 = d.get("closes60") or []
        if len(c6) >= 3:
            run = px / c6[-3] - 1
            if abs(run / sig) >= AN["sigma_run3"] and abs(run * 100) >= floor:
                out.append({"key": f"anom:run:{t}", "kind": "run", "ticker": t, "shade": 3,
                            "title": f"Unusual 3-day {'rise' if run > 0 else 'drop'} — {label(t)}",
                            "chart_head": f"Unusual 3-day {'rise' if run > 0 else 'drop'}",
                            "note": f"{'up' if run > 0 else 'down'} {abs(run*100):.1f}% in 3 trading days",
                            "text": (f"<b>{rate(t, px)}</b>, {'up' if run > 0 else 'down'} <b>{abs(run*100):.1f}%</b> "
                                     f"over the last 3 trading days.\n\nIt normally moves about {usual:.1f}% "
                                     f"a day, so a 3-day move this size is rare for it.")})
    ai = [o for o in rows if o["t"] in CFG["ai_group"]]
    bn = CFG["benchmark"]
    if ai and bench:
        g1 = sum((o["px"] / o["d"]["c"][-1] - 1) * 100 for o in ai) / len(ai)
        b1 = (bench["px"] / bench["d"]["c"][-1] - 1) * 100
        if abs(g1 - b1) >= AN["group_vs_bench_1d_pts"]:
            out.append({"key": "anom:group1:AI", "kind": "group1", "ticker": None, "period": 1,
                        "title": "Your AI stocks are moving apart from the market",
                        "text": (f"Today your AI stocks averaged <b>{g1:+.1f}%</b> while the S&amp;P 500 "
                                 f"({bn}) moved <b>{b1:+.1f}%</b>, a gap of {abs(g1-b1):.1f} points.\n\n"
                                 f"When a whole group moves this differently from the market, money is "
                                 f"usually flowing {'into' if g1 > b1 else 'out of'} that theme.")})
        def r5(o):
            c = o["d"].get("closes60") or []
            return (o["px"] / c[-5] - 1) * 100 if len(c) >= 5 else None
        gv = [r5(o) for o in ai if r5(o) is not None]
        bv = r5(bench)
        if gv and bv is not None:
            g5 = sum(gv) / len(gv)
            if abs(g5 - bv) >= AN["group_vs_bench_5d_pts"]:
                out.append({"key": "anom:group5:AI", "kind": "group5", "ticker": None, "period": 5,
                            "title": "Your AI stocks have split from the market this week",
                            "text": (f"Over the last 5 trading days your AI stocks averaged <b>{g5:+.1f}%</b> "
                                     f"while the S&amp;P 500 ({bn}) moved <b>{bv:+.1f}%</b>, a gap of "
                                     f"{abs(g5-bv):.1f} points.\n\nMoney appears to be moving "
                                     f"{'into' if g5 > bv else 'out of'} AI-related names.")})
    moves = [(o["px"] / o["d"]["c"][-1] - 1) * 100 for o in rows if not is_fx(o["t"])]
    if moves:
        dn = sum(1 for m in moves if m <= -AN["breadth_move_pct"])
        if dn / len(moves) >= AN["breadth_frac"]:
            out.append({"key": "anom:breadth:ALL", "kind": "breadth", "ticker": None, "period": 1,
                        "title": "Broad drop across your list",
                        "text": (f"<b>{dn} of your {len(moves)} stocks</b> are down more than "
                                 f"{AN['breadth_move_pct']:.0f}% today.\n\nWhen nearly everything falls "
                                 f"together it is usually the whole market under pressure, not one company.")})
    return out


def anomaly_message(a, rows, sess):
    head = f"⚠️ <b>{esc(a['title'])}</b>"
    if a["ticker"]:
        o = next(x for x in rows if x["t"] == a["ticker"])
        body = [head, esc(full_name(a["ticker"], o)), f"{when_text()} · {sess_text(a['ticker'], o.get('sess', sess))}",
                "", a["text"], "", "<i>A heads-up, not a buy or sell signal on its own.</i>"]
        png = price_chart(a["ticker"], full_name(a["ticker"], o), o["d"], o["ev"], a["chart_head"],
                          "#ef6c00", a["note"], shade=a.get("shade", 0))
    else:
        body = [head, f"{when_text()} · {SESSION_PLAIN.get(sess, sess)}", "", a["text"], "",
                "<i>A heads-up, not a buy or sell signal on its own.</i>"]
        png = group_chart(rows, a["title"], a.get("period", 1))
    return "\n".join(body), png


# ---------------------------------------------------------------- telegram
def send(text):
    if not TOK or not CHAT:
        print("no telegram credentials; message not sent:\n" + text)
        return
    body = json.dumps({"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOK}/sendMessage",
                                 data=body, headers={"Content-Type": "application/json"})
    for i in range(3):
        try:
            urllib.request.urlopen(req, timeout=25).read()
            return
        except urllib.error.HTTPError as e:
            print(f"telegram {e.code}: {e.read()[:300]}", file=sys.stderr)
            if e.code < 500:
                return
        except Exception as e:
            print(f"telegram: {e}", file=sys.stderr)
        time.sleep(2 + 2 * i)


def send_photo(png, caption):
    if not TOK or not CHAT:
        print("no telegram credentials; photo not sent. caption:\n" + caption)
        return True
    b = "----stockalerts" + uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", CHAT), ("caption", caption), ("parse_mode", "HTML")):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOK}/sendPhoto", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    for i in range(3):
        try:
            urllib.request.urlopen(req, timeout=40).read()
            return True
        except urllib.error.HTTPError as e:
            print(f"telegram photo {e.code}: {e.read()[:300]}", file=sys.stderr)
            if e.code < 500:
                return False
        except Exception as e:
            print(f"telegram photo: {e}", file=sys.stderr)
        time.sleep(2 + 2 * i)
    return False


def plain_len(html):
    return len(re.sub(r"<[^>]+>", "", html).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))


def notify(text, png=None):
    """Chart with the explanation as its caption; Telegram caps captions at 1024 characters."""
    if png is not None:
        if plain_len(text) <= 1000:
            if send_photo(png, text):
                return
        else:
            if send_photo(png, text.split("\n", 1)[0]):
                send(text)
                return
    send(text)


# ---------------------------------------------------------------- charts
_PLT = None


def mpl():
    """Import matplotlib, installing it only on the runs that actually need a chart."""
    global _PLT
    if _PLT is not None:
        return _PLT or None
    try:
        import matplotlib
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "matplotlib"],
                       check=False, timeout=240, capture_output=True)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        _PLT = plt
    except Exception as e:
        print(f"charts unavailable: {e}", file=sys.stderr)
        _PLT = False
    return _PLT or None


def _ticks(ax, dates, n):
    step = max(1, n // 6)
    idx = list(range(0, n, step))
    lab = []
    for i in idx:
        try:
            lab.append(dt.date.fromisoformat(dates[i]).strftime("%-d %b"))
        except Exception:
            lab.append("")
    ax.set_xticks(idx + [n])
    ax.set_xticklabels(lab + ["now"], fontsize=8)


def price_chart(t, name, d, ev, headline, color, note, shade=0, target=None, target_label=""):
    """Top: price, its normal range (shaded) and 3-day average. Bottom: momentum score."""
    h = d.get("hist")
    plt = mpl() if h else None
    if not plt:
        return None
    try:
        n = len(h["c"])
        xs, xt = list(range(n)), n
        x2 = xs + [xt]
        up, lo = h["up"] + [ev["up"]], h["lo"] + [ev["lo"]]
        fig = plt.figure(figsize=(9, 6.6), dpi=110)
        gs = fig.add_gridspec(2, 1, height_ratios=[3, 1.25], hspace=0.07)
        ax, ax2 = fig.add_subplot(gs[0]), None
        ax2 = fig.add_subplot(gs[1], sharex=ax)
        ax.fill_between(x2, lo, up, color="#90caf9", alpha=0.30, lw=0, label="normal price range")
        ax.plot(x2, up, color="#64b5f6", lw=0.8)
        ax.plot(x2, lo, color="#64b5f6", lw=0.8)
        ax.plot(x2, h["ma3"] + [ev["ma3"]], color="#8d6e63", lw=1.1, ls="--", label="3-day average")
        ax.plot(xs, h["c"], color="#263238", lw=1.8, label="daily closing price")
        ax.plot([xs[-1], xt], [h["c"][-1], ev["px"]], color="#263238", lw=1.4, ls=":")
        if shade:
            ax.axvspan(xt - shade - 0.5, xt + 0.5, color=color, alpha=0.10)
        if target is not None:
            ax.axhline(target, color=color, ls="--", lw=1.2)
            ax.text(xs[len(xs) // 3], target, f" {target_label} {money(t, target)} ", color=color, fontsize=9,
                    fontweight="bold", va="bottom", bbox=dict(fc="white", ec="none", alpha=0.8, pad=1))
        ax.scatter([xt], [ev["px"]], s=130, color=color, zorder=6, edgecolor="white", linewidth=1.8)
        lo_y, hi_y = min(lo + h["c"]), max(up + h["c"])
        above = ev["px"] > (lo_y + hi_y) / 2
        ax.annotate(f"{money(t, ev['px'])} now", xy=(xt, ev["px"]),
                    xytext=(-95, -34 if above else 24), textcoords="offset points",
                    fontsize=9, color=color, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.35", fc="white", ec=color, lw=0.9),
                    arrowprops=dict(arrowstyle="->", color=color, lw=1.2))
        ax.set_title(f"{label(t)}  ·  {name}", loc="left", fontsize=12, fontweight="bold", pad=22)
        ax.text(0, 1.02, f"{headline}  ·  {note}", transform=ax.transAxes, fontsize=11,
                color=color, fontweight="bold")
        ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=3)
        if target is not None:   # keep the target line on the chart even if it is outside recent prices
            y0, y1 = ax.get_ylim()
            ax.set_ylim(min(y0, target * 0.99), max(y1, target * 1.01))
        ax.set_ylabel(f"{FX[t]['quote']} per 1 {FX[t]['base']}" if is_fx(t) else "price ($)")
        ax.grid(alpha=0.18)
        ax.margins(x=0.02)
        plt.setp(ax.get_xticklabels(), visible=False)
        r = h["rsi"] + [ev["rsi"]]
        ax2.axhspan(70, 100, color="#ef5350", alpha=0.10)
        ax2.axhspan(0, 30, color="#66bb6a", alpha=0.10)
        ax2.axhline(70, ls=":", c="#c62828", lw=0.8)
        ax2.axhline(30, ls=":", c="#2e7d32", lw=0.8)
        ax2.plot(x2, r, color="#5e35b1", lw=1.4)
        ax2.scatter([xt], [ev["rsi"]], s=55, color=color, zorder=6, edgecolor="white", linewidth=1.2)
        ax2.text(0.5, 86, "overheated (above 70)", fontsize=7.5, color="#c62828")
        ax2.text(0.5, 7, "sold off hard (below 30)", fontsize=7.5, color="#2e7d32")
        ax2.set_ylim(0, 100)
        ax2.set_ylabel("momentum\nscore", fontsize=9)
        ax2.grid(alpha=0.18)
        _ticks(ax2, h["dates"], n)
        fig.text(0.01, 0.005, "Not financial advice. Nothing has been traded.", fontsize=7, color="#888")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        print(f"chart {t}: {e}", file=sys.stderr)
        return None


def group_chart(rows, title, period):
    plt = mpl()
    if not plt:
        return None
    try:
        vals = []
        for o in [x for x in rows if not is_fx(x["t"])]:
            c = o["d"].get("closes60") or []
            base = o["d"]["c"][-1] if period == 1 else (c[-5] if len(c) >= 5 else None)
            if base:
                vals.append((o["t"], (o["px"] / base - 1) * 100))
        vals.sort(key=lambda x: x[1])
        bn = CFG["benchmark"]
        ai = set(CFG["ai_group"])
        fig, ax = plt.subplots(figsize=(8, 0.42 * len(vals) + 1.6), dpi=110)
        cols = ["#263238" if t == bn else ("#7e57c2" if t in ai else "#90a4ae") for t, _ in vals]
        ax.barh([t for t, _ in vals], [v for _, v in vals], color=cols)
        bv = next((v for t, v in vals if t == bn), None)
        if bv is not None:
            ax.axvline(bv, color="#263238", ls="--", lw=1)
            ax.text(bv, len(vals) - 0.4, f" S&P 500 ({bn})", fontsize=8, va="bottom")
        ax.axvline(0, color="#999", lw=0.8)
        for i, (t, v) in enumerate(vals):
            ax.text(v, i, f" {v:+.1f}% ", va="center", ha="left" if v >= 0 else "right", fontsize=8)
        ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
        ax.set_xlabel("change today (%)" if period == 1 else "change over 5 trading days (%)")
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(color="#7e57c2", label="AI group"), Patch(color="#90a4ae", label="others"),
                           Patch(color="#263238", label="S&P 500")], fontsize=8, frameon=False, loc="lower right")
        ax.grid(axis="x", alpha=0.18)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        print(f"group chart: {e}", file=sys.stderr)
        return None


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def table(hdr, rows, widths):
    """Monospace table. Telegram renders <pre> fixed-width, which is the only way to align."""
    line = lambda cs: "".join(str(c)[:w].ljust(w) if i == 0 else str(c)[:w].rjust(w)
                              for i, (c, w) in enumerate(zip(cs, widths)))
    body = [line(hdr), "-" * sum(widths)] + [line(r) for r in rows]
    return "<pre>" + esc("\n".join(body)) + "</pre>"


SESS_LABEL = {"PRE": "pre-market", "REGULAR": "open", "POST": "after hours", "CLOSED": "closed"}


def digest(rows, sess, alerts):
    """The visual watchlist, grouped by state."""
    buckets = {"BUY": [], "SELL": [], "WATCH BUY": [], "WATCH SELL": [], "QUIET": []}
    for o in rows:
        k, r = o["ev"]["k"], o["ev"]["r"]
        if "BUY" in (k, r):
            buckets["BUY"].append(o)
        elif "SELL" in (k, r):
            buckets["SELL"].append(o)
        elif "WATCH SELL" in (k, r):
            buckets["WATCH SELL"].append(o)
        elif "WATCH BUY" in (k, r):
            buckets["WATCH BUY"].append(o)
        else:
            buckets["QUIET"].append(o)
    parts = [f"📋 <b>Watchlist</b> · {SESS_LABEL.get(sess, sess)} · "
             f"{dt.datetime.now(dt.timezone.utc):%d %b %H:%M} UTC"]
    icon = {"BUY": "🟢", "SELL": "🔴", "WATCH BUY": "🔵", "WATCH SELL": "🟠", "QUIET": "⚪"}
    for name in ("BUY", "SELL", "WATCH SELL", "WATCH BUY", "QUIET"):
        b = buckets[name]
        if not b:
            continue
        if name == "QUIET":
            parts.append(f"{icon[name]} <b>Quiet ({len(b)})</b>  " +
                         ", ".join(o["t"] for o in sorted(b, key=lambda x: x["t"])))
            continue
        hdr = ["", "PRICE", "DAY%", "RSI", "TO TRIG"]
        wd = [6, 10, 7, 5, 12]
        tr = []
        # Nearest to its trigger first: that is the one worth looking at.
        for o in sorted(b, key=lambda x: abs(x["near"]) if x.get("near") is not None else 9e9):
            near = o.get("near")
            tr.append([label(o["t"])[:6], num(o["t"], o['ev']['px']),
                       f"{(o['ev']['px']/o['d']['c'][-1]-1)*100:+.2f}",
                       f"{o['ev']['rsi']:.0f}",
                       f"{near:+.1f}%" if near is not None else "—"])
        parts.append(f"{icon[name]} <b>{name} ({len(b)})</b>\n" + table(hdr, tr, wd))
        if name in ("BUY", "SELL"):
            for o in sorted(b, key=lambda x: x["t"]):
                parts.append(f"   <b>{o['t']}</b> → {plan_text(o['t'], name)}")
    if alerts:
        parts.append("⚠️ <b>Unusual movement</b>\n" + "\n".join("• " + esc(a["title"]) for a in alerts))
    parts.append("<i>Real-time states at the time of this message. Nothing has been traded.</i>")
    parts.append(f"Dashboard: {CFG.get('pages_url', 'https://labpotato.github.io/stock-alerts/')}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- status feed
def write_status(rows, sess, alerts, errs):
    os.makedirs("docs", exist_ok=True)
    # Public file: signal states and prices only. No share counts, targets or order sizes.
    out = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "session": sess,
        "settled_close_date": None,
        "tickers": {},
        "anomalies": [a["title"] for a in alerts],
        "errors": errs,
        "note": "Real-time states: a signal is a cross between consecutive readings. Nothing is traded.",
        "cross_reference": "last_poll" if REALTIME else "prev_close",
    }
    days = sorted({o["d"].get("last") for o in rows if o["d"].get("last")})
    out["settled_close_date"] = days[-1] if days else None
    for o in rows:
        ev, d = o["ev"], o["d"]
        out["tickers"][o["t"]] = {
            "label": label(o["t"]), "kind": "currency" if is_fx(o["t"]) else "stock",
            "price": sig6(ev["px"]),
            "prev_close": sig6(d["c"][-1]),
            "day_pct": round((ev["px"] / d["c"][-1] - 1) * 100, 2),
            "rsi": round(ev["rsi"], 1),
            "keltner": ev["k"], "momentum": ev["r"],
            "upper": sig6(ev["up"]), "lower": sig6(ev["lo"]),
            "ma3": sig6(ev["ma3"]),
            "buy_trigger": {k: sig6(v) for k, v in (o.get("tb") or {}).items()},
            "sell_trigger": {k: sig6(v) for k, v in (o.get("ts") or {}).items()},
            "nearest_trigger_pct": o.get("near"),
            "sigma_pct": round(d["sigma"] * 100, 3) if d.get("sigma") else None,
            "z": round(o["z"], 2) if o.get("z") is not None else None,
        }
    json.dump(out, open(STATUS, "w"), indent=1)


# ---------------------------------------------------------------- main
def poll(state):
    tickers = CFG["tickers"]
    bench = CFG["benchmark"]
    # Cheap session probe: one call decides whether a full sweep is worth it.
    try:
        probe = live(bench)
    except Exception as e:
        print(f"probe failed: {e}", file=sys.stderr)
        return None, "probe failed", []
    sess = probe["sess"]
    today = probe.get("edate") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if state.get("day") != today:
        # New exchange day: alerts may repeat, but the last real-time reading carries over,
        # so an after-hours cross is not reported again at the next pre-market.
        keep = state.get("obs")
        state_fxv = state.get("fx_version")
        state.clear()
        state.update({"day": today, "sent": [], "pending": {}, "obs": keep or {},
                      "fx_version": state_fxv})
    state.setdefault("obs", {})
    if state.get("fx_version") != 2:
        for t in FX:
            state["obs"].pop(t, None)
        state["fx_version"] = 2
    want_list = MODE in ("digest", "buy-watch")   # only when you ask for it
    # Stocks follow the US session; currencies trade almost around the clock on weekdays.
    stocks = tickers if (sess != "CLOSED" or want_list) else []
    fxs = list(FX) if (fx_open() or want_list) else []
    if not stocks and not fxs:
        print("stock and currency markets both shut, nothing to do (1 call)")
        return None, sess, []

    rows, errs = [], []
    if stocks:
        cache, fresh, cerrs = build_cache(stocks, f"{today}:{'shut' if sess == 'CLOSED' else 'open'}")
        errs += cerrs
    if fxs:
        # Currency days roll over at midnight UTC, not New York, so they get their own refresh key.
        ukey = dt.datetime.fromtimestamp(time.time(), dt.timezone.utc).strftime("%Y-%m-%d")
        cache, fresh, cerrs = build_cache(fxs, f"fx2:{ukey}:{'open' if fx_open() else 'shut'}")
        errs += cerrs
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    for t in stocks + fxs:
        d = cache.get(t)
        if not isinstance(d, dict) or "ema" not in d:
            errs.append(f"{t}: no cached bars")
            continue
        try:
            lv = probe if t == bench else live(t)
            tsess = lv["sess"] if is_fx(t) else sess
            px = lv["px"]
            if px is None:
                errs.append(f"{t}: no price")
                continue
            # Reject an obviously broken extended-hours print on a thin name.
            pc = d["c"][-1]
            mv = abs(px / pc - 1)
            if tsess in ("PRE", "POST") and t in THIN and mv > 0.15 and lv["n"] < 5:
                errs.append(f"{t}: suspect {mv*100:.0f}% extended print on {lv['n']} bars, ignored")
                continue
            ref = state["obs"].get(t) if REALTIME else None
            ev = evaluate(d, px, lv["hi"], lv["lo"], ref)
            ev["t"] = t
            if ref is None:   # the comparison point, in the same shape, for the explanations
                ref = {"px": pc, "ma3": d["ma3"], "up": d["up"], "lo": d["lo"], "rsi": d["rsi"]}
            nxt = obs(ev) if REALTIME else None
            tb, ts = trigger(d, "BUY", nxt), trigger(d, "SELL", nxt)
            near = None
            for side, dd in (("BUY", tb), ("SELL", ts)):
                for v in dd.values():
                    p = (v / px - 1) * 100
                    if near is None or abs(p) < abs(near):
                        near = round(p, 1)
            rows.append({"t": t, "d": d, "ev": ev, "ref": ref, "px": px, "name": lv.get("name"), "sess": tsess,
                         "tb": tb, "ts": ts, "near": near})
        except Exception as e:
            errs.append(f"{t}: {e}")
    if not rows:
        return None, sess, errs
    b = next((o for o in rows if o["t"] == bench), None)
    alerts = anomalies(rows, b if stocks else None)
    write_status(rows, sess, alerts, errs)
    return (rows, sess, alerts, errs, want_list), sess, errs


def still_crossed(o, side, leg):
    ev = o["ev"]
    if leg == "k":
        return ev["px"] <= ev["up"] if side == "SELL" else ev["ma3"] > ev["lo"]
    return ev["rsi"] <= 70 if side == "SELL" else ev["rsi"] >= 30


def decide(rows, alerts, state, sess):
    """Returns the list of (text, png) messages to send, and records them so none repeats.
    Memory is per ticker, per side, per measure, per trading day:
      - each measure can say SELL (or BUY) for a ticker at most once a day;
      - if both say it at once you get one STRONG message;
      - if the second measure agrees later the same day you get one upgrade to STRONG."""
    sent, pending = state["sent"], state["pending"]
    by_t = {o["t"]: o for o in rows}
    # "t:SIDE" is the older record format (one entry per side); honour it so an update mid-day
    # never re-sends something already sent.
    done = lambda t, side, l: f"{t}:{side}:{l}" in sent or f"{t}:{side}" in sent
    out = []

    # 1. Thin names in extended hours: send a cross only if the next reading still agrees.
    #    Both measures confirming for one ticker still makes ONE message.
    confirmed = {}
    for key in list(pending):
        pending.pop(key)
        parts = key.split(":")
        if len(parts) != 3:
            continue
        t, side, leg = parts
        o = by_t.get(t)
        if o is not None and not done(t, side, leg) and still_crossed(o, side, leg):
            confirmed.setdefault((t, side), []).append(leg)
    for (t, side), legs in confirmed.items():
        earlier = [l for l in ("k", "r") if l not in legs and done(t, side, l)]
        for l in legs:
            sent.append(f"{t}:{side}:{l}")
        out.append(signal_message(by_t[t], side, sorted(legs), earlier, by_t[t].get("sess", sess), held=True))

    # 2. New signals at this reading, one message per ticker.
    for o in rows:
        t, ev = o["t"], o["ev"]
        if {ev["k"], ev["r"]} == {"BUY", "SELL"}:
            key = f"{t}:MIXED:x"
            if key not in sent:
                sent.append(key)
                out.append(mixed_message(o, o.get("sess", sess)))
            continue
        for side in ("BUY", "SELL"):
            legs = [l for l in ("k", "r") if ev[l] == side and not done(t, side, l)
                    and f"{t}:{side}:{l}" not in pending]
            if not legs:
                continue
            if CONFIRM_THIN and o.get("sess", sess) in ("PRE", "POST") and t in THIN:
                for l in legs:
                    pending[f"{t}:{side}:{l}"] = sess
                print(f"{t} {side}: thin name in extended hours, waiting one more reading")
                continue
            earlier = [l for l in ("k", "r") if l not in legs and done(t, side, l)]
            for l in legs:
                sent.append(f"{t}:{side}:{l}")
            out.append(signal_message(o, side, legs, earlier, o.get("sess", sess)))

    # 3. Unusual movement, one message each.
    for a in alerts:
        if a["key"] in sent:
            continue
        sent.append(a["key"])
        out.append(anomaly_message(a, rows, sess))

    # 4. This reading becomes the reference for the next one.
    for o in rows:
        state["obs"][o["t"]] = obs(o["ev"])
    return out


def main():
    if MODE == "test":
        try:
            lv = live(CFG["benchmark"])
            cache, fresh, errs = build_cache([CFG["benchmark"]],
                                             f"{lv.get('edate')}:{'shut' if lv['sess'] == 'CLOSED' else 'open'}")
            d = cache[CFG["benchmark"]]
            ev = evaluate(d, lv["px"], lv["hi"], lv["lo"])
            ev["t"] = CFG["benchmark"]
            msg = (f"{full_name(CFG['benchmark'])} is at ${lv['px']:,.2f}. "
                   f"Price range: {leg_state('k', ev)}. Momentum: {leg_state('r', ev)}. "
                   f"Market: {SESSION_PLAIN.get(lv['sess'], lv['sess'])}.")
            png = price_chart(CFG["benchmark"], full_name(CFG["benchmark"]), d, ev,
                              "SAMPLE CHART: not a signal", "#546e7a", "price now")
        except Exception as e:
            msg, png = f"⚠️ price fetch failed: {e}", None
        hs = "set" if H else "NOT SET (alerts will say so)"
        notify("✅ <b>Stock alerts are working.</b>\n\n" + esc(msg) +
               f"\n\nHoldings secret: {hs}\nTickers watched: {len(CFG['tickers'])}\n\n"
               "<i>This is what an alert chart looks like. It is a sample, not a signal.</i>", png)
        return

    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    state.setdefault("sent", [])
    state.setdefault("pending", {})
    state.setdefault("obs", {})

    res, sess, errs = poll(state)
    if res is None:
        json.dump(state, open(STATE, "w"))
        if errs and len(errs) >= len(CFG["tickers"]):
            sys.exit("every ticker failed: " + "; ".join(errs[:4]))
        return
    rows, sess, alerts, errs, want_list = res

    outbox = decide(rows, alerts, state, sess)
    # Save memory BEFORE sending, so a crash mid-send can never cause a repeat.
    json.dump(state, open(STATE, "w"))
    for text, png in outbox:
        notify(text, png)
        time.sleep(0.7)
    if MODE == "digest":
        send(digest(rows, sess, alerts))
    if MODE == "buy-watch":
        for text, png in near_buy(rows, sess):
            notify(text, png)
            time.sleep(0.7)

    print(f"session {sess} · {len(rows)} priced · {len(outbox)} message(s) · {len(errs)} error(s)")
    for e in errs[:10]:
        print("  " + e, file=sys.stderr)
    if len(errs) >= len(CFG["tickers"]):
        sys.exit("every ticker failed")


if __name__ == "__main__":
    main()
