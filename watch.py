# Keltner(MA3) + RSI14 watcher on real-time prices, including pre-market and after-hours,
# with anomaly detection and a public status feed. Trades nothing.
import json, os, re, io, sys, time, math, random, uuid, subprocess, urllib.request, urllib.error, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import signals as SG                                         # noqa: E402  shared with the post-close run
import orders as OR
import hedge as HG                                          # noqa: E402
from signals import ema, atr, rsi, states, edges             # noqa: E402,F401

TOK, CHAT = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", "")
MODE = os.environ.get("MODE", "poll")           # poll | test | digest | buy-watch | settled
CFG = json.load(open(os.environ.get("CONFIG", os.path.join(HERE, "config.json"))))
DATA = os.environ.get("DATA_DIR", ".")
STATE, CACHE = os.path.join(DATA, "state.json"), os.path.join(DATA, "bars_cache.json")
STATUS = os.path.join(DATA, "docs", "status.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-alerts/2)"}
AN = CFG["anomaly"]
THIN = set(CFG.get("thin", []))
# "prev_close" (default since 2026-11-05): the daily rule at the live price, against the last settled close.
# "last_poll": a cross between two consecutive real-time readings (whipsawed on volatile days).
REALTIME = CFG.get("cross_reference", "prev_close") == "last_poll"
CONFIRM_THIN = CFG.get("confirm_thin_extended", True)


# ---------------------------------------------------------------- holdings (secret)
def _holdings_secret():
    raw = os.environ.get("HOLDINGS_JSON", "").strip()
    if not raw:
        return None
    try:
        h = json.loads(raw)
        return h if "tickers" in h else None
    except Exception as e:
        print(f"HOLDINGS_JSON is not valid JSON: {e}", file=sys.stderr)
        return None


def _holdings_file():
    """holdings.json in the PRIVATE repo the post-close run keeps up to date (config holdings_repo),
    read with the read-only HOLDINGS_TOKEN. Its content is never printed or written anywhere."""
    repo = os.environ.get("HOLDINGS_REPO", "").strip() or CFG.get("holdings_repo", "")
    tok = os.environ.get("HOLDINGS_TOKEN", "").strip()
    if not repo or not tok:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/holdings.json",
                                 headers={"Authorization": f"Bearer {tok}", "User-Agent": "stock-alerts",
                                          "Accept": "application/vnd.github.raw+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            h = json.load(r)
        return h if isinstance(h, dict) and "tickers" in h else None
    except Exception as e:
        print(f"shared holdings file not read ({type(e).__name__}); using the secret", file=sys.stderr)
        return None


def choose_holdings(shared, secret):
    """The newer of the shared file and the secret, by as_of; the shared file wins a tie."""
    if shared and secret:
        return (shared, "shared file") if str(shared.get("as_of", "")) >= str(secret.get("as_of", "")) \
            else (secret, "secret")
    if shared:
        return shared, "shared file"
    return (secret, "secret") if secret else (None, "none")


def holdings():
    h, src = choose_holdings(_holdings_file(), _holdings_secret())
    global HOLDINGS_SOURCE
    HOLDINGS_SOURCE = src
    return h


HOLDINGS_SOURCE = "none"
H0 = holdings()
H = H0


def apply_split_holdings(state):
    """A split after the holdings date multiplies that ticker's share count by k, so the book,
    the AI-group weight and order sizes stay right until the secret is updated."""
    global H
    if not H0:
        H = H0
        return
    h = json.loads(json.dumps(H0))
    as_of = str(h.get("as_of") or "")
    for t, m in (state.get("splits") or {}).items():
        info = h.get("tickers", {}).get(t)
        if not info or not info.get("shares"):
            continue
        for sd, v in m.items():
            if sd > as_of:
                info["shares"] = info["shares"] * v["k"]
    H = h


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
         f"?range={rng}&interval={iv}" + ("&includePrePost=true" if prepost else "")
         + ("&events=split" if iv == "1d" else ""))
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


# ---------------------------------------------------------------- indicators (see signals.py)
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


def market(t):
    if is_fx(t):
        return "fx"
    if t.endswith(".SI"):
        return "sg"
    if t.endswith(".KS"):
        return "kr"
    return "us"


def split_events(r, day):
    """[(date, k)] from Yahoo's chart events; k = new shares per old share."""
    out = []
    for e in ((r.get("events") or {}).get("splits") or {}).values():
        n, dn = e.get("numerator"), e.get("denominator")
        if e.get("date") is not None and n and dn and n != dn:
            out.append((day(e["date"]), n / dn))
    return sorted(out)


def daily_bars(t, ref_px=None):
    """Yahoo 1y daily bars as dicts, split-adjusted (signals.adjust_splits), after the shared
    hygiene rules (signals.hygiene). ref_px is the live price, used for a split effective today."""
    r = chart(t, "1y", "1d")
    q = r["indicators"]["quote"][0]
    meta = r["meta"]
    off = meta.get("gmtoffset", 0)
    day = lambda ts: dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date()
    vol = q.get("volume") or [None] * len(r["timestamp"])
    bars = [{"date": day(ts), "o": o, "h": h, "l": l, "c": c, "v": v}
            for ts, o, h, l, c, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], vol)]
    fx = is_fx(t)
    splits = [] if fx else split_events(r, day)
    # Splits confirmed from the issuer while Yahoo lags (config "splits": {T: [[date, k], ...]}).
    have = {sd for sd, _ in splits}
    for sd, k in (CFG.get("splits") or {}).get(t, []):
        sd = dt.date.fromisoformat(sd)
        if sd not in have:
            splits.append((sd, float(k)))
    splits.sort()
    if splits:
        # With no live price passed in, Yahoo's latest price stands in (the official close, in the evening).
        bars, applied = SG.adjust_splits(bars, splits, ref_px or meta.get("regularMarketPrice"))
        for sd, k in applied:
            print(f"  {t}: applied {SG.split_text(k)} of {sd} to the older bars", file=sys.stderr)
    SPLITS_SEEN[t] = splits
    kept, dropped = SG.hygiene(bars, volume_rule=not fx,
                               flat_rule=t not in CFG.get("keep_flat_bars", []),
                               partial_tape=not fx)
    for d, why_ in dropped[-3:]:
        print(f"  {t}: dropped {d} ({why_})", file=sys.stderr)
    return kept, meta, day


SPLITS_SEEN = {}


def settled_rows(t, settled=False, ref_px=None):
    """Daily OHLC through the last COMPLETED session.
    settled=False (real-time polls): the live price stands in for the current session's bar.
    settled=True  (evening check): today's bar is the official close, never an after-hours price."""
    bars, meta, day = daily_bars(t, ref_px)
    if not bars:
        return None
    off = meta.get("gmtoffset", 0)
    sess = session_of(meta)
    today_x = dt.datetime.fromtimestamp(time.time() + off, dt.timezone.utc).date()
    mt = meta.get("regularMarketTime")
    last_session = day(mt) if mt is not None else None
    if market(t) in ("sg", "kr"):
        # Asian listings: a bar dated today Singapore time is not settled when the US closes.
        sgt = (dt.datetime.fromtimestamp(time.time(), dt.timezone.utc) + dt.timedelta(hours=8)).date()
        bars = [b for b in bars if b["date"] < sgt]
    if settled and market(t) == "us":
        if bars and last_session == bars[-1]["date"] and sess not in ("PRE", "REGULAR"):
            if meta.get("regularMarketPrice"):
                bars[-1] = dict(bars[-1], c=meta["regularMarketPrice"])   # the official close
        while bars and last_session is not None and bars[-1]["date"] > last_session:
            bars = bars[:-1]                                              # early row, not traded yet
    elif sess in ("PRE", "REGULAR", "POST"):
        # The live price always stands in for the CURRENT session's bar, so that bar is removed.
        if bars and bars[-1]["date"] == today_x:
            bars = bars[:-1]
    elif last_session is not None and not settled:
        # Market shut: the live price IS the latest session, so its bar (and any early row
        # Yahoo adds for a day that has not traded yet) is removed rather than counted twice.
        while bars and bars[-1]["date"] >= last_session:
            bars = bars[:-1]
    elif last_session is not None:
        while bars and bars[-1]["date"] > last_session:
            bars = bars[:-1]
    if len(bars) < 40:
        return None
    bars = bars[-SG.WINDOW:]
    return {"rows": [(b["o"], b["h"], b["l"], b["c"]) for b in bars],
            "dates": [b["date"].isoformat() for b in bars], "last": bars[-1]["date"].isoformat()}


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


def build_cache(tickers, market_date=None, ref_px=None):
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
            sr = settled_rows(t, ref_px=(ref_px or {}).get(t))
            if not sr:
                raise ValueError("fewer than 40 usable daily bars")
            d = derive(sr["rows"], sr["dates"])
            d["ag"], d["al"] = rsi_acc([r[3] for r in sr["rows"]])
            d["last"] = sr["last"]
            today_ = dt.datetime.fromtimestamp(time.time(), dt.timezone.utc).date()
            d["splits"] = [[sd.isoformat(), k] for sd, k in SPLITS_SEEN.get(t, [])
                           if (today_ - sd).days <= 60]
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


CCY_SIGN = {"USD": "$", "SGD": "S$", "KRW": "₩"}


def ccy(t):
    return CFG.get("currency", {}).get(t, "USD")


def money(t, x):
    if is_fx(t):
        return f"{num(t, x)} {FX[t]['quote']}"
    c = ccy(t)
    return CCY_SIGN.get(c, c + " ") + (f"{x:,.0f}" if c == "KRW" else f"{x:,.2f}")


def rate(t, x):
    """The headline price: '1 USD = 1.3012 SGD' for a currency, '$183.00' for a stock."""
    return f"1 {FX[t]['base']} = {num(t, x)} {FX[t]['quote']}" if is_fx(t) else money(t, x)


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
SESSION_PLAIN = {"PRE": "pre-market", "REGULAR": "market open",
                 "POST": "after hours", "CLOSED": "market closed"}


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


SYM = {"USD": "US$", "SGD": "S$", "KRW": "₩"}


def fx_amount(cur, x, whole=False):
    sym = SYM.get(cur, cur + " ")
    if cur == "KRW" or whole:
        return f"{sym}{x:,.0f}"
    return f"{sym}{x:,.4f}" if x < 10 else f"{sym}{x:,.2f}"


def fx_buys(t, frm, to, pair_rate):
    """What a round amount of `frm` buys in `to` at a pair rate (quote per 1 base)."""
    per = pair_rate if FX[t]["base"] == frm else 1 / pair_rate
    unit = 1_000_000 if frm == "KRW" else 1000         # amounts you would actually change
    return unit, per * unit


def fx_do(t, side, ev=None):
    """Plain words: which currency is favoured to buy with which, and how good the rate is."""
    frm, to = fx_action(t, side)
    line = (f"👉 <b>Buying {CURRENCY.get(to, to)} with {CURRENCY.get(frm, frm)} is favoured now</b> "
            f"({frm} → {to})")
    if not ev or not ev.get("px") or not ev.get("up") or not ev.get("lo"):
        return line
    unit, now = fx_buys(t, frm, to, ev["px"])
    _, usual = fx_buys(t, frm, to, (ev["up"] + ev["lo"]) / 2)
    pct = (now / usual - 1) * 100
    edge = (f"<b>{pct:.1f}% more</b> {to} than on a typical day" if pct >= 0.1
            else "about the same as a typical day")
    return (line + f"\n{fx_amount(frm, unit, whole=True)} buys <b>{fx_amount(to, now)}</b> now, versus about "
                   f"{fx_amount(to, usual)} on a typical day (the middle of its normal range): {edge}.")


CTX = {"closes": {}, "usdsgd": None, "paused": {}, "today": None}


def plan_text(t, side):
    """What your holdings plan says this signal means, in plain words. REAL-TIME alerts only
    say whether a matching close would fire; only the evening check sizes an order."""
    if is_fx(t):
        frm, to = fx_action(t, side)
        return (f"<b>A good time to buy {CURRENCY.get(to, to)} with {CURRENCY.get(frm, frm)}.</b> "
                f"Your {frm} gets more {to} than it usually does.")
    if H is None:
        return "Your holdings aren't loaded, so I can't say whether this triggers an order."
    if OR.shape(H) == "v2":
        txt = OR.gate_text(H, t, side, CTX["closes"], CTX["usdsgd"], set(CFG["ai_group"]),
                           CTX["today"] or dt.date.today(), CTX["paused"].get(t), CFG.get("never_arm", []))
        return txt[0].upper() + txt[1:] + "."
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


def fx_reason(t, side):
    """One plain sentence on why, without the measures' names."""
    one = lambda c: (lambda n: n[:-1] if n.endswith("dollars") else n)(CURRENCY.get(c, c))
    b, q = one(FX[t]["base"]), one(FX[t]["quote"])
    if side == "SELL":
        return f"The {b} has been unusually strong against the {q} and is starting to fall back."
    return f"The {b} has been unusually weak against the {q} and is starting to recover."


def fx_signal_message(o, side, legs, earlier, sess):
    """Currency alerts, direct (Dongmin 2026-11-09): which money to change into which, and how good it is."""
    t, ev = o["t"], o["ev"]
    frm, to = fx_action(t, side)
    strong = (set(legs) | set(earlier)) == {"k", "r"}
    unit, now = fx_buys(t, frm, to, ev["px"])
    _, usual = fx_buys(t, frm, to, (ev["up"] + ev["lo"]) / 2)
    pct = (now / usual - 1) * 100
    lines = [f"💱 <b>I would {'definitely ' if strong else ''}buy {CURRENCY.get(to, to)} "
             f"with {CURRENCY.get(frm, frm)} now</b>",
             f"{fx_amount(frm, unit, whole=True)} buys <b>{fx_amount(to, now)}</b> now"
             + (f", {pct:.1f}% more than on a typical day." if pct >= 0.1 else ", about a typical day's rate."),
             "", fx_reason(t, side) + (" Both measures agree." if strong else ""),
             "", f"<i>{when_text()} · REAL-TIME · alert only, nothing has been traded.</i>"]
    return "\n".join(lines), fx_chart(o, side)


def fx_chart(o, side=None):
    """The exchange-rate chart that goes with every currency message."""
    t, ev = o["t"], o["ev"]
    if side:
        frm, to = fx_action(t, side)
        return price_chart(t, full_name(t, o), o["d"], ev, f"I would buy {to} with {frm}",
                           "#2e7d32" if side == "BUY" else "#c62828", f"{to} favoured")
    return price_chart(t, full_name(t, o), o["d"], ev, "Big move", "#ef6c00", "unusual move today")


def signal_message(o, side, legs, earlier, sess, held=False):
    """One message for one ticker: verdict from the two measures, reasons, plan, chart."""
    if is_fx(o["t"]):
        return fx_signal_message(o, side, legs, earlier, sess)
    t, ev, ref = o["t"], o["ev"], o["ref"]
    said = set(legs) | set(earlier)
    strong = said == {"k", "r"}
    verdict = f"STRONG {side}" if strong else side
    dot = "🟢" if side == "BUY" else "🔴"
    combo = lambda l: side if l in said else "—"
    icons = "".join(dot if l in said else "⚪" for l in ("k", "r"))
    title = (f"{'STRONG: ' if strong else ''}Buy {fx_action(t, side)[1]} with {fx_action(t, side)[0]}"
             f" ({label(t)} {side})" if is_fx(t) else f"{verdict} — {label(t)}")
    lines = [f"{icons} <b>{title}</b>",
             esc(full_name(t, o)),
             f"<b>{rate(t, ev['px'])}</b> · {when_text()} · REAL-TIME, {sess_text(t, sess)}",
             ""]
    if is_fx(t):
        lines += [fx_do(t, side, ev), ""]
    e = o.get("edge") or {}
    if any(e.get(f"knife_{l}") for l in legs):
        lines += [knife_line(e, legs), ""]
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
    head = (f"{verdict}: buy {fx_action(t, side)[1]} with {fx_action(t, side)[0]}" if is_fx(t)
            else f"{verdict} at {money(t, ev['px'])}")
    png = price_chart(t, full_name(t, o), o["d"], ev, head,
                      "#2e7d32" if side == "BUY" else "#c62828", note)
    return "\n".join(lines).replace("\n\n\n", "\n\n"), png


def upgrade_message(o, side, legs, earlier, sess):
    """The second measure agrees later the same day: one short line, no second full alert."""
    t, ev = o["t"], o["ev"]
    if is_fx(t):
        frm, to = fx_action(t, side)
        unit, now = fx_buys(t, frm, to, ev["px"])
        return (f"💱 <b>Now I would definitely buy {CURRENCY.get(to, to)} with {CURRENCY.get(frm, frm)}</b>\n"
                f"{fx_amount(frm, unit, whole=True)} buys <b>{fx_amount(to, now)}</b> now. Both measures agree.\n\n"
                f"<i>{when_text()} · REAL-TIME · alert only.</i>")
    dot = "🟢" if side == "BUY" else "🔴"
    new, old = LEG[legs[0]], LEG[earlier[0]]
    lines = [f"{dot}{dot} <b>Now STRONG {side} — {label(t)}</b>",
             f"<b>{rate(t, ev['px'])}</b> · {when_text()} · REAL-TIME, {sess_text(t, sess)}", "",
             f"{new} now also says {side}, agreeing with {old.lower()} earlier today. "
             f"Both measures say {side}."]
    e = o.get("edge") or {}
    if any(e.get(f"knife_{l}") for l in legs):
        lines += ["", knife_line(e, legs)]
    lines += ["", f"<b>{'What it means' if is_fx(t) else 'Your plan'}:</b> {plan_text(t, side)}"]
    return "\n".join(lines)


def knife_line(e, legs):
    bits = []
    if "k" in legs and e.get("knife_k"):
        bits.append(f"price range decided by {min(abs(e['margin_k_pct']), abs(e.get('prior_k_pct', 9e9))):.3f}%")
    if "r" in legs and e.get("knife_r"):
        bits.append(f"momentum decided by {e['margin_r_pts']:.2f} points")
    return f"⚠️ <b>KNIFE-EDGE</b>: {', '.join(bits)}. A tiny move would flip it."


def mixed_message(o, sess):
    t, ev, ref = o["t"], o["ev"], o["ref"]
    if is_fx(t):
        b, q = FX[t]["base"], FX[t]["quote"]
        return (f"💱 <b>No clear direction between {CURRENCY.get(b, b)} and {CURRENCY.get(q, q)}</b>\n"
                f"The two measures disagree, so neither is favoured. No need to change money.\n\n"
                f"<i>{when_text()} · REAL-TIME · alert only.</i>"), None
    kside, rside = ev["k"], ev["r"]
    lines = [f"⚠️ <b>MIXED — {label(t)}</b>", esc(full_name(t, o)),
             f"<b>{rate(t, ev['px'])}</b> · {when_text()} · {sess_text(t, sess)}", "",
             f"Price range <b>{kside}</b> + Momentum <b>{rside}</b>  →  <b>the two measures disagree</b>", "",
             why("k", kside, ref, ev), "", why("r", rside, ref, ev), "",
             ("<b>What it means:</b> the two measures disagree, so <b>neither currency is favoured</b>; no need to change money."
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
def market_move(bench):
    """The benchmark's own move today and over 3 days, in %, and whether today's move is unusual."""
    if not bench or not bench["d"].get("sigma"):
        return None
    d, px, sig = bench["d"], bench["px"], bench["d"]["sigma"]
    b1 = (px / d["c"][-1] - 1) * 100
    c6 = d.get("closes60") or []
    b3 = (px / c6[-3] - 1) * 100 if len(c6) >= 3 else None
    return {"b1": b1, "b3": b3, "unusual": abs(b1 / 100 / sig) >= AN["sigma_single_with_abs"]}


def anomalies(rows, bench):
    """Each finding is its own message: dict(key, kind, ticker, text).
    When the market itself (the benchmark) has an unusual day, one market message is sent and a
    stock gets its own message only if it moves unusually compared with the market."""
    out = []
    mk = market_move(bench)
    rel = bool(mk and mk["unusual"])
    bn = CFG["benchmark"]
    if rel:
        moves = [(o["px"] / o["d"]["c"][-1] - 1) * 100 for o in rows if o["t"] in CFG["tickers"]]
        side = "down" if mk["b1"] < 0 else "up"
        n = sum(1 for m in moves if (m < 0) == (mk["b1"] < 0) and abs(m) >= AN["breadth_move_pct"])
        out.append({"key": "anom:market", "kind": "breadth", "ticker": None, "period": 1,
                    "title": f"The whole market is {side} sharply today",
                    "text": (f"The S&amp;P 500 ({bn}) is <b>{mk['b1']:+.1f}%</b> today, an unusually big day for "
                             f"the market itself. <b>{n} of your {len(moves)} stocks</b> are {side} more than "
                             f"{AN['breadth_move_pct']:.0f}%.\n\nWhen everything moves together it is the market, "
                             f"not one company. I'll only message you about a stock today if it moves "
                             f"unusually <i>compared with the market</i>.")})
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
        if is_fx(t) and (FX.get(t) or {}).get("invert_of"):
            continue                                 # the mirror of a pair already checked: same move
        # Only stocks are judged against the stock market; gold, bonds and currencies on their own.
        vs = rel and not is_fx(t) and t not in CFG.get("not_equity", [])
        if vs and t == bn:
            continue
        x = mv - mk["b1"] if vs else mv               # the part of the move the market doesn't explain
        zx = x / 100 / sig
        big = abs(zx) >= AN["sigma_single"]
        mid = abs(zx) >= AN["sigma_single_with_abs"] and abs(x) >= floor
        if (big or mid) and vs:
            out.append({"key": f"anom:move:{t}", "kind": "move", "ticker": t, "shade": 0,
                        "title": f"Unusual move — {label(t)}", "chart_head": "Unusual move vs the market",
                        "note": f"{'up' if mv > 0 else 'down'} {abs(mv):.1f}% today, market {mk['b1']:+.1f}%",
                        "text": (f"<b>{rate(t, px)}</b>, {'up' if mv > 0 else 'down'} <b>{abs(mv):.1f}%</b> today, "
                                 f"while the market is {mk['b1']:+.1f}%.\n\nSo it is moving "
                                 f"<b>{abs(x):.1f} points {'better' if x > 0 else 'worse'} than the market</b>, "
                                 f"about {abs(zx):.0f} times its usual daily move. Something specific may be "
                                 f"happening with this one; worth checking the news.")})
        elif big or mid:
            out.append({"key": f"anom:move:{t}", "kind": "move", "ticker": t, "shade": 0,
                        "title": f"Unusual move — {label(t)}", "chart_head": "Unusual move",
                        "note": f"{'up' if mv > 0 else 'down'} {abs(mv):.1f}% today",
                        "text": (f"<b>{rate(t, px)}</b>, {'up' if mv > 0 else 'down'} <b>{abs(mv):.1f}%</b> today.\n\n"
                                 f"On a normal day it moves about {usual:.1f}% either way, so today's move is "
                                 f"roughly <b>{abs(z):.0f} times bigger than usual</b>. Something may be "
                                 f"happening with this one; worth checking the news.")})
        c6 = d.get("closes60") or []
        if len(c6) >= 3:
            raw = run = px / c6[-3] - 1
            if vs and mk["b3"] is not None:
                run = raw - mk["b3"] / 100            # compared with the market's own 3-day move
            if abs(run / sig) >= AN["sigma_run3"] and abs(run * 100) >= floor:
                run = raw
                out.append({"key": f"anom:run:{t}", "kind": "run", "ticker": t, "shade": 3,
                            "title": f"Unusual 3-day {'rise' if run > 0 else 'drop'} — {label(t)}",
                            "chart_head": f"Unusual 3-day {'rise' if run > 0 else 'drop'}",
                            "note": f"{'up' if run > 0 else 'down'} {abs(run*100):.1f}% in 3 trading days",
                            "text": (f"<b>{rate(t, px)}</b>, {'up' if run > 0 else 'down'} <b>{abs(run*100):.1f}%</b> "
                                     f"over the last 3 trading days.\n\nIt normally moves about {usual:.1f}% "
                                     f"a day, so a 3-day move this size is rare for it.")})
    ai = [o for o in rows if o["t"] in CFG["ai_group"]]
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
    moves = [(o["px"] / o["d"]["c"][-1] - 1) * 100 for o in rows if o["t"] in CFG["tickers"]]
    if moves and not rel:
        dn = sum(1 for m in moves if m <= -AN["breadth_move_pct"])
        if dn / len(moves) >= AN["breadth_frac"]:
            out.append({"key": "anom:breadth:ALL", "kind": "breadth", "ticker": None, "period": 1,
                        "title": "Broad drop across your list",
                        "text": (f"<b>{dn} of your {len(moves)} stocks</b> are down more than "
                                 f"{AN['breadth_move_pct']:.0f}% today.\n\nWhen nearly everything falls "
                                 f"together it is usually the whole market under pressure, not one company.")})
    return out


def fx_move_message(a, o):
    """Unusual currency move, direct: which way and what S$1,000 (etc.) buys now versus yesterday."""
    t, px = o["t"], o["px"]
    b, q = FX[t]["base"], FX[t]["quote"]
    prev = o["d"]["c"][-1]
    unit, now = fx_buys(t, b, q, px)
    _, before = fx_buys(t, b, q, prev)
    up = px > prev
    strong, weak = (b, q) if up else (q, b)
    sw = CURRENCY.get(strong, strong); ww = CURRENCY.get(weak, weak)
    return (f"💱 <b>Big move: the {ww.rstrip('s') if ww.endswith('dollars') else ww} fell against the "
            f"{sw.rstrip('s') if sw.endswith('dollars') else sw}</b>\n"
            f"{fx_amount(b, unit, whole=True)} buys <b>{fx_amount(q, now)}</b> now, versus "
            f"{fx_amount(q, before)} at the last close ({(now / before - 1) * 100:+.1f}%). An unusually big move "
            f"for this pair.\n\n<b>I would wait:</b> your rule has not given a signal yet. I'll tell you the "
            f"moment it says to change money.\n\n<i>{when_text()} · a heads-up, not a signal.</i>")


def anomaly_message(a, rows, sess):
    head = f"⚠️ <b>{esc(a['title'])}</b>"
    if a["ticker"] and is_fx(a["ticker"]) and a.get("kind") == "move":
        o = next(x for x in rows if x["t"] == a["ticker"])
        return fx_move_message(a, o), (fx_chart(o) if o.get("ev", {}).get("up") else None)
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
        for o in [x for x in rows if x["t"] in CFG["tickers"]]:
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
def read_status():
    try:
        return json.load(open(STATUS))
    except Exception:
        return {}


def intraday_keys(state, day=None):
    """Real-time signal keys sent on a given exchange day ('T:SIDE:leg'; older 'T:SIDE')."""
    if day is None or state.get("day") == day:
        sent = state.get("sent", [])
    elif (state.get("last_day") or {}).get("day") == day:
        sent = state["last_day"].get("sent", [])
    else:
        sent = []
    return [k for k in sent if not k.startswith("anom:") and ":MIXED" not in k]


def write_status(rows, sess, alerts, errs, state=None):
    os.makedirs(os.path.dirname(STATUS), exist_ok=True)
    # Public file: signal states and prices only. No share counts, cash, targets or order sizes.
    prev = read_status()
    out = {
        "schema": 2,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "session": sess,
        "settled_close_date": None,
        "tickers": {},
        "anomalies": [a["title"] for a in alerts],
        "errors": errs,
        "note": ("Real-time states: the daily rule at the live price, against the last settled close. "
                 "Nothing is traded.") if not REALTIME else
                "Real-time states: a signal is a cross between consecutive readings. Nothing is traded.",
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
            "margin_k_pct": (o.get("edge") or {}).get("margin_k_pct"),
            "margin_r_pts": (o.get("edge") or {}).get("margin_r_pts"),
            "knife_edge": (o.get("edge") or {}).get("knife_edge"),
        }
    # The settled block is written only by the evening check; real-time runs carry it forward.
    if "settled" in prev:
        out["settled"] = prev["settled"]
    out["intraday_sent"] = intraday_keys(state or {})
    json.dump(out, open(STATUS, "w"), indent=1)
    write_board(rows, out)


BOARD = os.path.join(DATA, "docs", "board.json")


def write_board(rows, status):
    """The dashboard's data (docs/index.html): every ticker's latest reading, kept across runs so
    the page is complete even when only one market is open, plus 60 sessions of closes and bands
    for the small charts. Public prices and signal states only, never holdings."""
    try:
        board = json.load(open(BOARD))
    except Exception:
        board = {}
    tick = board.get("tickers") or {}
    for o in rows:
        t, h = o["t"], (o["d"].get("hist") or {})
        e = dict(status["tickers"][t])
        e.update({"name": full_name(t, o), "as_of": status["generated_utc"], "session": o.get("sess"),
                  "hist": {"d": (h.get("dates") or [])[-60:], "c": (h.get("c") or [])[-60:],
                           "lo": (h.get("lo") or [])[-60:], "up": (h.get("up") or [])[-60:]}})
        tick[t] = e
    keep = set(universe()) | set(FX)
    board = {"generated_utc": status["generated_utc"], "session": status["session"],
             "tickers": {t: v for t, v in tick.items() if t in keep},
             "anomalies": status.get("anomalies", []), "alerts_today": status.get("intraday_sent", []),
             "errors": status.get("errors", []), "ai_group": CFG.get("ai_group", [])}
    json.dump(board, open(BOARD, "w"), separators=(",", ":"))


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
        # The evening check may run after midnight, so yesterday's alert list is kept for it.
        last = {"day": state.get("day"), "sent": state.get("sent", [])}
        keep = {k: state.get(k) for k in ("obs", "fx_version", "settled_done", "paused", "splits", "hedge")}
        state.clear()
        state.update({"day": today, "sent": [], "pending": {}, "last_day": last})
        state.update({k: v for k, v in keep.items() if v is not None})
    state.setdefault("obs", {})
    if state.get("fx_version") != 2:
        for t in FX:
            state["obs"].pop(t, None)
        state["fx_version"] = 2
    want_list = MODE in ("digest", "buy-watch")   # only when you ask for it
    # Stocks follow the US session; currencies trade almost around the clock on weekdays;
    # optional SGX / KRX listings follow their own exchange's session.
    us_all = [t for t in universe() if market(t) == "us"]
    stocks = us_all if (sess != "CLOSED" or want_list) else []
    fxs = list(FX) if (fx_open() or want_list) else []
    own, own_live = [], {}
    for t in [t for t in universe() if market(t) in ("sg", "kr")]:
        try:
            lv = live(t)
            if lv["sess"] != "CLOSED" or want_list:
                own.append(t)
                own_live[t] = lv
        except Exception as e:
            print(f"{t}: {e}", file=sys.stderr)
    if not stocks and not fxs and not own:
        print("stock and currency markets both shut, nothing to do (1 call)")
        return None, sess, []

    rows, errs = [], []
    if stocks:
        # Holdings that are not watched (SGOV, ES3) are priced too, for the AI-group weight.
        cache, fresh, cerrs = build_cache(stocks + valuation_only(),
                                          f"{today}:{'shut' if sess == 'CLOSED' else 'open'}")
        errs += cerrs
    for t in own:
        cache, fresh, cerrs = build_cache([t], f"{own_live[t]['edate']}:{own_live[t]['sess'] == 'CLOSED'}")
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
    set_context(cache, state)
    for t in stocks + fxs + own:
        d = cache.get(t)
        if not isinstance(d, dict) or "ema" not in d:
            errs.append(f"{t}: no cached bars")
            continue
        try:
            lv = probe if t == bench else (own_live.get(t) or live(t))
            tsess = lv["sess"] if market(t) != "us" else sess
            px = lv["px"]
            if px is None:
                errs.append(f"{t}: no price")
                continue
            pc = d["c"][-1]
            # A jump that matches a split ratio: the cached bars may predate a split.
            listed = [sd for sd, _ in (CFG.get("splits") or {}).get(t, [])
                      if sd > str(d.get("last") or "") and sd <= today]
            if not is_fx(t) and (SG.split_like(pc, px) or (listed and abs(px / pc - 1) > SG.SPLIT_MOVE)):
                d = split_check(t, d, px, state, today)
                if d is None:
                    errs.append(f"{t}: held, {pc:.4g} -> {px:.4g} looks like a split Yahoo has not confirmed")
                    continue
                pc = d["c"][-1]
            # Reject an obviously broken extended-hours print on a thin name.
            mv = abs(px / pc - 1)
            if tsess in ("PRE", "POST") and t in THIN and mv > 0.15 and lv["n"] < 5:
                errs.append(f"{t}: suspect {mv*100:.0f}% extended print on {lv['n']} bars, ignored")
                continue
            ref = state["obs"].get(t) if REALTIME else None
            ev = evaluate(d, px, lv["hi"], lv["lo"], ref)
            ev["t"] = t
            if ref is None:   # the comparison point, in the same shape, for the explanations
                ref = {"px": pc, "ma3": d["ma3"], "up": d["up"], "lo": d["lo"], "rsi": d["rsi"]}
            pv = {"c": ref["px"], "ma3": ref["ma3"], "up": ref["up"], "lo": ref["lo"], "rsi": ref["rsi"]}
            cv = {"c": ev["px"], "ma3": ev["ma3"], "up": ev["up"], "lo": ev["lo"], "rsi": ev["rsi"]}
            edge = edges(pv, cv)
            nxt = obs(ev) if REALTIME else None
            tb, ts = trigger(d, "BUY", nxt), trigger(d, "SELL", nxt)
            near = None
            for side, dd in (("BUY", tb), ("SELL", ts)):
                for v in dd.values():
                    p = (v / px - 1) * 100
                    if near is None or abs(p) < abs(near):
                        near = round(p, 1)
            rows.append({"t": t, "d": d, "ev": ev, "ref": ref, "px": px, "name": lv.get("name"), "sess": tsess,
                         "tb": tb, "ts": ts, "near": near, "edge": edge})
        except Exception as e:
            errs.append(f"{t}: {e}")
    if not rows:
        return None, sess, errs
    b = next((o for o in rows if o["t"] == bench), None)
    alerts = anomalies(rows, b if stocks else None)
    write_status(rows, sess, alerts, errs, state)
    return (rows, sess, alerts, errs, want_list), sess, errs


def universe():
    """Watched tickers: config 'tickers', plus the information-only list when switched on."""
    u = list(CFG["tickers"])
    if CFG.get("include_info_tickers"):
        u += [t for t in CFG.get("info_tickers", []) if t not in u]
    return u


def valuation_only():
    """Held tickers that are not watched (e.g. SGOV, ES3.SI): priced for the book, never alerted."""
    if not H:
        return []
    return [t for t in H.get("tickers", {}) if t not in universe() and not is_fx(t)
            and (H["tickers"][t].get("shares") or 0)]


SPLIT_NOTES = []


def record_split(t, sd, k, state, px=None, old_close=None):
    """Remember a split once; returns the plain-language note to send, or None if already told."""
    m = state.setdefault("splits", {}).setdefault(t, {})
    if sd in m:
        return None
    m[sd] = {"k": k}
    state.setdefault("obs", {}).pop(t, None)   # the last reading was in pre-split prices
    nm = esc(full_name(t))
    k_txt = f"{k:g}" if k >= 1 else f"1/{1/k:g}"
    lines = [f"✂️ <b>{esc(label(t))} — {nm}</b>",
             f"{SG.split_text(k).capitalize()}, effective {sd}.", ""]
    if k >= 1:
        lines.append(f"Each old share became {k:g} shares at about 1/{k:g} of the price. Nothing was lost.")
    else:
        lines.append(f"Every {1/k:g} old shares became 1 share at about {1/k:g}x the price. Nothing was lost.")
    if px and old_close:
        lines.append(f"Now {money(t, px)}. The last close of {money(t, old_close)} is "
                     f"{money(t, old_close / k)} in new shares, so the real move is "
                     f"{(px / (old_close / k) - 1) * 100:+.1f}%.")
    lines += ["", "I've adjusted the price history, so the price range and momentum are back to normal.",
              f"Ignore any 'unusual move' or drop alert for {esc(label(t))} today; the split caused it."]
    info = (H0 or {}).get("tickers", {}).get(t) or {}
    if info.get("shares") and str(H0.get("as_of") or "") < sd:
        lines.append(f"\nYour holdings secret is dated {esc(str(H0.get('as_of')))}, before the split, so I'm "
                     f"counting your {esc(label(t))} shares x{k_txt} until you update it (new share count and as_of date).")
    return "\n".join(lines)


def split_check(t, d, px, state, today):
    """The live price is a split ratio away from the cached close. Refetch the daily bars with the
    live price as the post-split reference (signals.adjust_splits). Returns the fresh bars once
    they agree with the live price; otherwise None, and the ticker is held with a one-time note."""
    pc = d["c"][-1]
    guess = SG.split_like(pc, px)
    cache, _, _ = build_cache([t], f"split:{today}:{sig6(px)}", ref_px={t: px})
    nd = cache.get(t)
    if isinstance(nd, dict) and "ema" in nd and not SG.split_like(nd["c"][-1], px):
        sp = nd.get("splits") or []
        sd, k = (sp[-1][0], sp[-1][1]) if sp else (today, guess)
        note = record_split(t, sd, k, state, px, pc)
        if note:
            SPLIT_NOTES.append(note)
        CTX.setdefault("closes", {})[t] = nd["c"][-1]
        apply_split_holdings(state)
        return nd
    key = f"{t}:SPLITHOLD"
    if key not in state.setdefault("sent", []):
        state["sent"].append(key)
        SPLIT_NOTES.append(
            f"⚠️ <b>{esc(label(t))} — {esc(full_name(t))}</b>\n"
            f"Price {money(t, px)} against {money(t, pc)} at the last close ({(px / pc - 1) * 100:+.0f}%).\n\n"
            f"That matches a {SG.split_text(guess)}, but Yahoo hasn't confirmed one yet. I'm holding "
            f"signals and unusual-move alerts for {esc(label(t))} until it does. "
            f"If there was no split, this is a real move: check the news.")
    return None


def set_context(cache, state):
    """Prices for the order gate. Only public prices go in here; holdings stay in memory."""
    for t, v in cache.items():
        for sd, k in (v.get("splits") or []) if isinstance(v, dict) else []:
            if (CTX_TODAY() - dt.date.fromisoformat(sd)).days <= 10:
                note = record_split(t, sd, k, state)
                if note:
                    SPLIT_NOTES.append(note)
    apply_split_holdings(state)
    CTX["closes"] = {t: v["c"][-1] for t, v in cache.items() if isinstance(v, dict) and v.get("c")}
    u = cache.get("USDSGD=X")
    CTX["usdsgd"] = u["c"][-1] if isinstance(u, dict) and u.get("c") else CTX.get("usdsgd")
    CTX["paused"] = dict(state.get("paused") or {})
    try:
        from zoneinfo import ZoneInfo
        CTX["today"] = dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        CTX["today"] = dt.date.today()


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
            if is_fx(t) and not earlier:
                # SGD->KRW SELL and KRW->SGD BUY both mean "buy won with SGD": tell it once a day.
                act = "fx:{}>{}".format(*fx_action(t, side))
                if act in sent:
                    for l in legs:
                        sent.append(f"{t}:{side}:{l}")
                    continue
                sent.append(act)
            for l in legs:
                sent.append(f"{t}:{side}:{l}")
            if earlier:     # the other measure already sent the full alert today: a short upgrade
                out.append((upgrade_message(o, side, legs, earlier, o.get("sess", sess)),
                            fx_chart(o, side) if is_fx(t) else None))
            else:
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



# ---------------------------------------------------------------- Rule H: hedge a crashing sector
def _closes_by_date(d):
    h = d.get("hist") or {}
    return {k: v for k, v in zip(h.get("dates") or [], h.get("c") or []) if k and v}


def _inverse_price(t):
    try:
        return live(t)["px"]
    except Exception as e:
        print(f"{t}: {e}", file=sys.stderr)
        return None


def _shares(dollars, px):
    return f" (about {dollars / px:.0f} shares at {money('QQQ', px)})" if px else ""


def _migrate_hedge(rec):
    """A hedge stored before the split (one QQQ/PSQ record) becomes its QQQ leg."""
    if rec and "legs" not in rec:
        return {"legs": {"QQQ": {"proxy": "QQQ", "fund": "PSQ", "lev": 1, "on": rec["on"], "days": rec["days"],
                                 "proxy_px": rec["proxy_px"], "rebound_pct": rec["rebound_pct"],
                                 "max_sessions": rec["max_sessions"]}}}
    return rec


def hedge_messages(rows, state, sess):
    """REAL-TIME split-hedge suggestions and their exits, sized from the secret. Telegram only."""
    out = []
    groups = (CFG.get("hedge") or {}).get("groups") or []
    if not groups or H is None or OR.shape(H) != "v2" or OR.stale(H, CTX.get("today") or dt.date.today()):
        return out
    by_t = {o["t"]: o for o in rows}
    today = (CTX.get("today") or dt.date.today()).isoformat()
    store = state.setdefault("hedge", {})
    off_today = state.setdefault("hedge_off", {})
    minimum = H.get("min_order_usd") or 200
    for g in groups:
        name = g["name"]
        members = CFG.get(g["members"], []) if isinstance(g["members"], str) else g["members"]
        rec = _migrate_hedge(store.get(name)) or {"legs": {}}
        store[name] = rec
        legs = {L["proxy"]: L for L in g["legs"]}
        live_px = {p: by_t[p] for p in legs if p in by_t and by_t[p].get("sess") in ("REGULAR", "PRE", "POST")}
        # 1. exits of open legs, each against its own index
        for p, leg in list(rec["legs"].items()):
            po = live_px.get(p)
            if po is None:
                continue
            ev = po["ev"]
            if leg["days"][-1] != today:
                leg["days"].append(today)
            why = HG.should_exit(leg, po["px"], "BUY" in (ev["k"], ev["r"]), (ev["up"] + ev["lo"]) / 2,
                                 len(leg["days"]) - 1)
            if why:
                rec["legs"].pop(p)
                off_today[f"{name}:{p}"] = today
                if "session limit" in why:      # timed out mid-crash: no new leg until this crash ends
                    rec.setdefault("wait", {})[p] = True
                first = leg["on"] == today and len(leg["days"]) == 1
                out.append(f"🛡️ <b>HEDGE OFF — {esc(name)}: {leg['fund']}</b>\n{when_text()} · REAL-TIME\n\n"
                           f"{esc(p)} {money(p, po['px'])}. "
                           + (f"<b>Ignore today's {leg['fund']} suggestion</b> (if you bought it, sell it): {why}."
                              if first else f"<b>Sell the {leg['fund']} hedge</b>: {why}.")
                           + (f" The {', '.join(sorted(rec['legs'][q]['fund'] for q in rec['legs']))} hedge stays "
                              f"until its own exit." if rec["legs"] else "")
                           + "\n\n<i>Alert only. Nothing has been traded.</i>")
        # 2. which index's leg crashed now
        crashed = {}
        for p, L in legs.items():
            po = live_px.get(p)
            if po is None or p in rec["legs"] or off_today.get(f"{name}:{p}") == today:
                continue
            if "BUY" in (po["ev"]["k"], po["ev"]["r"]):        # your Rule A already says it is recovering
                continue
            d = po["d"]
            hit, reason = HG.crash(po["px"], d["c"][-1], d.get("sigma"), (d.get("hist") or {}).get("c", [])[-20:],
                                   po["ev"]["lo"], g)
            if not hit:
                rec.get("wait", {}).pop(p, None)      # the crash that timed a leg out is over
            elif not rec.get("wait", {}).get(p):
                crashed[p] = reason
        if not crashed:
            continue
        closes = dict(CTX.get("closes") or {})
        closes.update({o["t"]: o["px"] for o in rows if not is_fx(o["t"])})
        b = OR.book(H, closes, CTX.get("usdsgd"), set(members))
        if not b["total"]:
            continue
        vals = {t: v for t, v in b["values"].items() if t in members}
        gv = sum(vals.values())
        w = gv / b["total"] * 100
        if w < g["min_weight_pct"]:
            continue
        pcs = {p: _closes_by_date(by_t[p]["d"]) for p in legs if p in by_t}
        mcs = {t: _closes_by_date(by_t[t]["d"]) if t in by_t else {} for t in vals}
        home = HG.assign(mcs, pcs, g["legs"][0]["proxy"])
        plan, total = [], 0.0
        for p, reason in crashed.items():
            L = legs[p]
            mine = {t: v for t, v in vals.items() if home.get(t) == p}
            if not mine:
                continue
            betas = {t: HG.beta(mcs[t], pcs[p]) for t in mine}
            expo, used = HG.exposure(mine, betas)
            dollars = HG.size(expo, g, L["lev"])
            if dollars < minimum:
                continue
            fpx = _inverse_price(L["fund"])
            plan.append((p, L, reason, mine, used, expo, dollars, fpx))
            total += dollars
        if not plan:
            continue
        cap = H.get("ai_cap_pct") or 30
        trim = gv - cap / 100 * b["total"]
        fund_txt, _ = OR.funding(H, total, b, closes)
        lines = [f"🛡️ <b>HEDGE CHECK — {esc(name)}</b>",
                 f"{when_text()} · REAL-TIME", "",
                 f"Your {esc(name)} is <b>{w:.0f}%</b> of your money."]
        for p, L, reason, *_ in plan:
            lines.append(f"{esc(p)} is at {money(p, live_px[p]['px'])}: {reason}.")
        lines += ["", f"<b>Rule H suggests a temporary hedge of half the exposure, each stock against the "
                      f"index it follows most closely:</b>"]
        for p, L, reason, mine, used, expo, dollars, fpx in plan:
            names = ", ".join(sorted(mine, key=lambda t: -mine[t]))
            lines.append(f"• <b>Buy ≈${dollars:,.0f} of {L['fund']}</b>{_shares(dollars, fpx)}"
                         f"{' (3x)' if L['lev'] == 3 else ''}: covers {names} "
                         f"(${sum(mine.values()):,.0f}, ≈${expo:,.0f} of {p}-equivalent). "
                         f"Hold {L['max_sessions']} sessions at most.")
        lines.append(f"Funding for ≈${total:,.0f}: " + (fund_txt + "." if fund_txt else
                     "not enough cash and SGOV above its floor for all of it."))
        lines += [(f"• Or the no-tax permanent fix: trim the group back to your {cap:.0f}% cap by selling "
                   f"≈${trim:,.0f}." if trim > minimum else
                   f"• Trimming is not needed for the cap: the group is within {cap:.0f}%."), ""]
        for p, L, *_ in plan:
            po = live_px[p]
            mid = (po["ev"]["up"] + po["ev"]["lo"]) / 2
            lines.append(f"<b>Sell the {L['fund']}</b> when your Rule A gives a BUY on {p}, {p} is back above "
                         f"{money(p, mid)} (middle of its range), {p} reaches "
                         f"{money(p, po['px'] * (1 + g['rebound_pct'] / 100))} (+{g['rebound_pct']:.0f}%), or after "
                         f"{L['max_sessions']} sessions.")
            rec["legs"][p] = {"proxy": p, "fund": L["fund"], "lev": L["lev"], "on": today, "days": [today],
                              "proxy_px": po["px"], "rebound_pct": g["rebound_pct"],
                              "max_sessions": L["max_sessions"]}
        lines += ["I'll message you when each one happens.", "",
                  "<i>A hedge reduces losses if the fall continues and costs some of the rebound if it doesn't. "
                  "A 3x fund is for days, not weeks.</i>", "", "<i>Alert only. Nothing has been traded.</i>"]
        out.append("\n".join(lines))
    return out


# ---------------------------------------------------------------- evening check (settled pass)
SETTLED_FROM = (16, 15)        # New York time; the official close is normally in by then


def CTX_TODAY():
    return dt.datetime.fromtimestamp(time.time(), dt.timezone.utc).date()


def et_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.fromtimestamp(time.time(), ZoneInfo("America/New_York"))
    except Exception:
        return dt.datetime.fromtimestamp(time.time() - 5 * 3600, dt.timezone.utc)


def _as_ev(t, st):
    return {"t": t, "px": st["close"], "ma3": st["ma3"], "up": st["upper"], "lo": st["lower"],
            "rsi": st["rsi"], "k": st["k_state"], "r": st["r_state"]}


def _as_ref(st):
    p = st["prev"]
    return {"px": p["close"], "ma3": p["ma3"], "up": p["upper"], "lo": p["lower"], "rsi": p["rsi"]}


def margin_text(leg, side, st, t):
    """The deciding margin at the close, in words. Positive = the rule was met by that much."""
    if leg == "k" and side == "BUY":
        m = (st["ma3"] - st["lower"]) / st["lower"] * 100
        return (f"3-day average {money(t, st['ma3'])} vs bottom of range {money(t, st['lower'])}: "
                f"{'above' if m > 0 else 'below'} by {abs(m):.4f}%")
    if leg == "k" and side == "SELL":
        m = (st["upper"] - st["close"]) / st["upper"] * 100
        return (f"close {money(t, st['close'])} vs top of range {money(t, st['upper'])}: "
                f"{'inside' if m >= 0 else 'still above'} by {abs(m):.4f}%")
    if side == "BUY":
        return f"momentum score {st['rsi']:.2f}: {'over' if st['rsi'] >= 30 else 'under'} 30 by {abs(st['rsi']-30):.2f} points"
    return f"momentum score {st['rsi']:.2f}: {'under' if st['rsi'] <= 70 else 'over'} 70 by {abs(st['rsi']-70):.2f} points"


def fires_settled(t, side, closes, usdsgd, state, day):
    """The only place an order is sized. Telegram only, never written to a file."""
    if H is None:
        return "holdings not loaded, so I can't size this", False
    if OR.shape(H) == "v2":
        return OR.fires_line(H, t, side, closes, usdsgd, set(CFG["ai_group"]),
                             dt.date.fromisoformat(day), (state.get("paused") or {}).get(t),
                             CFG.get("never_arm", []))
    return re.sub(r"<[^>]+>", "", plan_text(t, side)), "trigger your prepared order" in plan_text(t, side)


def settled_message(kind, t, side, st, r, iv, sv, closes, usdsgd, state, day):
    ev, ref = _as_ev(t, st), _as_ref(st)
    head = {"CONFIRMED": f"✅ <b>CONFIRMED at close — {label(t)}</b>",
            "FADED": f"⚠️ <b>FADED by close — {label(t)}</b>",
            "CLOSE": f"🔔 <b>CLOSE SIGNAL (not seen intraday) — {label(t)}</b>",
            "CONFLICT": f"⚖️ <b>CONFLICT at close — {label(t)}</b>"}[kind]
    verdict = st["signal"] if kind in ("CONFIRMED", "CLOSE") else side
    combo = lambda l: (st[f"{l}_state"] if st[f"{l}_state"] in ("BUY", "SELL") else "—")
    lines = [head, esc(full_name(t)),
             f"<b>Official close {money(t, st['close'])}</b> · {st['bar_date']} · evening check", ""]
    if kind == "CONFLICT":
        lines += [f"Price range <b>{combo('k')}</b> + Momentum <b>{combo('r')}</b>  →  <b>the two measures disagree</b>", "",
                  why("k", st["k_state"], ref, ev), "", why("r", st["r_state"], ref, ev), "",
                  "<b>Fires:</b> nothing. A conflict fires nothing."]
    elif kind == "FADED":
        lines += [f"Today's real-time <b>{side}</b> did not hold to the close.", ""]
        for l in sorted(iv):
            lines.append(f"{LEG[l]}: {margin_text(l, side, st, t)}, so no {side} at the close.")
        lines += ["", "<b>Fires:</b> nothing. Only a signal at the close fires an order."]
    else:
        lines += [f"Price range <b>{combo('k')}</b> + Momentum <b>{combo('r')}</b>  →  <b>{verdict}</b>", ""]
        lines.append("Today's real-time alert held to the close." if kind == "CONFIRMED"
                     else "No real-time reading caught this; it appears on the official close.")
        lines.append("")
        for l in sorted(sv):
            lines += [why(l, side, ref, ev), f"<i>Deciding margin: {margin_text(l, side, st, t)}.</i>", ""]
        if "STRONG" in verdict and len(sv) < 2:
            lines += ["The other measure gave the same signal within the last 3 sessions, so this counts as STRONG.", ""]
        line, fires = fires_settled(t, side, closes, usdsgd, state, st["bar_date"])
        if side == "SELL" and "under target" in line:
            state.setdefault("paused", {})[t] = st["bar_date"]
        lines.append(f"<b>Fires:</b> {esc(line)}")
    e = {"margin_k_pct": st["margin_k_pct"], "margin_r_pts": st["margin_r_pts"],
         "prior_k_pct": st["prior_k_pct"], "knife_k": st["knife_k"], "knife_r": st["knife_r"]}
    legs = (iv | sv) or {"k", "r"}
    if any(e.get(f"knife_{l}") for l in legs):
        lines += ["", knife_line(e, legs)]
    lines += ["", "<i>Nothing has been traded.</i>"]
    color = {"CONFIRMED": "#2e7d32" if side == "BUY" else "#c62828", "FADED": "#757575",
             "CLOSE": "#1565c0", "CONFLICT": "#ef6c00"}[kind]
    png = None
    try:
        d = derive(r["rows"][:-1], r["dates"][:-1])
        note = {"CONFIRMED": "held to the close", "FADED": "faded by the close",
                "CLOSE": "seen only at the close", "CONFLICT": "measures disagree"}[kind]
        png = price_chart(t, full_name(t), d, ev, f"{kind.title()}: {verdict}", color, note)
    except Exception as ex:
        print(f"settled chart {t}: {ex}", file=sys.stderr)
    return "\n".join(lines).replace("\n\n\n", "\n\n"), png


def settled_pass(state, manual=False):
    """Once per trading day after the official close: close-to-close states from settled bars
    only (no live or extended-hours price), compared with today's real-time alerts."""
    now = et_now()
    day = now.date().isoformat()
    done = state.setdefault("settled_done", [])
    if day in done:
        return []
    if not manual and ((now.hour, now.minute) < SETTLED_FROM or now.weekday() >= 5):
        return []
    sr = settled_rows(CFG["benchmark"], settled=True)
    if not sr or sr["last"] != day:
        print(f"evening check: the {day} close is not in yet (latest settled {sr and sr['last']})")
        return []
    res, notes = {}, []
    for t in universe() + list(FX) + valuation_only():
        try:
            r = sr if t == CFG["benchmark"] else settled_rows(t, settled=True)
            if not r:
                notes.append(f"{t}: too few settled bars")
                continue
            res[t] = (SG.settled_state(r["rows"], r["dates"]), r)
        except Exception as e:
            notes.append(f"{t}: {e}")
    closes = {t: v[0]["close"] for t, v in res.items()}
    usdsgd = closes.get("USDSGD=X")
    seen = {}
    for k in intraday_keys(state, day):
        p = k.split(":")
        if len(p) >= 2:
            seen.setdefault((p[0], p[1]), set()).update({p[2]} if len(p) == 3 else {"k", "r"})
    out = []
    paused = state.setdefault("paused", {})
    for t in [t for t in universe() if market(t) == "us"]:
        if t not in res:
            continue
        st, r = res[t]
        if st["bar_date"] != day:
            notes.append(f"{t}: no settled bar for {day} (latest {st['bar_date']})")
            continue
        if st["k_state"] == "NONE" and st["r_state"] == "NONE":
            paused.pop(t, None)                 # adds resume once the state is back to NONE
        if st["signal"] == "CONFLICT":
            out.append(settled_message("CONFLICT", t, "", st, r, set(), set(), closes, usdsgd, state, day))
            continue
        for side in ("BUY", "SELL"):
            iv = seen.get((t, side), set())
            sv = {l for l in ("k", "r") if st[f"{l}_state"] == side}
            kind = ("CONFIRMED" if iv and sv else "FADED" if iv else "CLOSE" if sv else None)
            if kind:
                out.append(settled_message(kind, t, side, st, r, iv, sv, closes, usdsgd, state, day))
    write_settled(res, day, state, notes)
    done.append(day)
    state["settled_done"] = done[-10:]
    print(f"evening check {day}: {len(res)} settled, {len(out)} message(s), {len(notes)} note(s)")
    for n in notes[:10]:
        print("  " + n, file=sys.stderr)
    return out


def write_settled(res, day, state, notes):
    """The block the post-close run reads. Prices and states only; no holdings data."""
    os.makedirs(os.path.dirname(STATUS), exist_ok=True)
    out = read_status()
    out["schema"] = 2
    hide = set(valuation_only())
    out["settled"] = {"date": day, "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                      "notes": notes, "tickers": {}}
    for t, (st, _r) in res.items():
        if t in hide:
            continue
        out["settled"]["tickers"][t] = {
            "label": label(t), "kind": "currency" if is_fx(t) else "stock", "bar_date": st["bar_date"],
            "close": sig6(st["close"]), "ma3": sig6(st["ma3"]), "upper": sig6(st["upper"]),
            "lower": sig6(st["lower"]), "rsi": round(st["rsi"], 3),
            "k_state": st["k_state"], "r_state": st["r_state"], "signal": st["signal"],
            "margin_k_pct": st["margin_k_pct"], "margin_r_pts": st["margin_r_pts"],
            "knife_edge": st["knife_edge"]}
    out["intraday_sent"] = intraday_keys(state, day)
    json.dump(out, open(STATUS, "w"), indent=1)


def main():
    if MODE == "announce":                  # a one-off message, e.g. "the algorithm was updated"
        note = os.environ.get("NOTE", "").strip()
        if note:
            send(note)
        return
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
        hs = (f"from the {HOLDINGS_SOURCE}, as of {esc(str(H.get('as_of')))}" if H
              else "NOT SET (alerts will say so)")
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

    if MODE == "settled":                   # the 16:20 / 16:50 New York runs, or a manual run
        evening(state, manual=os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch")
        return

    res, sess, errs = poll(state)
    if res is None:
        json.dump(state, open(STATE, "w"))
        evening(state)
        if errs and len(errs) >= len(CFG["tickers"]):
            sys.exit("every ticker failed: " + "; ".join(errs[:4]))
        return
    rows, sess, alerts, errs, want_list = res

    outbox = [(n, None) for n in SPLIT_NOTES] + decide(rows, alerts, state, sess)
    try:
        outbox += [(m, None) for m in hedge_messages(rows, state, sess)]
    except Exception as e:                     # a hedge check must never stop the alerts
        print(f"hedge check failed: {e}", file=sys.stderr)
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
    if MODE == "poll":
        evening(state)
    if len(errs) >= len(CFG["tickers"]):
        sys.exit("every ticker failed")


def evening(state, manual=False):
    """Try the evening check. Any run after 16:15 New York time attempts it until it succeeds,
    so a late or skipped GitHub schedule cannot lose the day."""
    apply_split_holdings(state)
    try:
        msgs = settled_pass(state, manual=manual)
    except Exception as e:
        print(f"evening check failed: {e}", file=sys.stderr)
        return
    json.dump(state, open(STATE, "w"))      # record 'done' before sending, so nothing repeats
    if not CFG.get("evening_messages", True):
        # Dongmin 2026-11-06: real-time alerts only. The evening check still runs and writes the
        # settled block in status.json (the post-close run's cross-check), but sends nothing.
        print(f"evening check done: {len(msgs)} message(s) not sent (evening_messages off)")
        return
    for text, png in msgs:
        notify(text, png)
        time.sleep(0.7)


if __name__ == "__main__":
    main()
