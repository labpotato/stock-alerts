# Keltner(MA3) + RSI14 watcher on real-time prices, including pre-market and after-hours,
# with anomaly detection and a public status feed. Trades nothing.
import json, os, sys, time, math, random, urllib.request, urllib.error, datetime as dt

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


def chart(t, rng, iv, prepost=False):
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
         f"?range={rng}&interval={iv}" + ("&includePrePost=true" if prepost else ""))
    res = get(u)["chart"]["result"][0]
    return res


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
    # Today's bar (exchange time) is still forming while any session is open.
    if dates[-1] == today_x and sess in ("PRE", "REGULAR", "POST"):
        rows, dates = rows[:-1], dates[:-1]
    if len(rows) < 40:
        return None
    return {"rows": rows[-220:], "last": dates[-1].isoformat()}


def derive(rows):
    """Everything a live poll needs, computed once per day from settled bars."""
    c = [r[3] for r in rows]
    e, a, rv = ema(c), atr(rows), rsi(c)
    i = len(c) - 1
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
        if isinstance(have, dict) and market_date and have.get("_tried") == market_date:
            if "ema" not in have:
                errs.append(f"{t}: {have.get('_err', 'no bars')} (retry tomorrow)")
            continue
        try:
            sr = settled_rows(t)
            if not sr:
                raise ValueError("fewer than 40 usable daily bars")
            d = derive(sr["rows"])
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
    return {"px": px, "prev": prev, "sess": sess, "hi": hi, "lo": lo, "mdate": mdate, "edate": edate,
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


# ---------------------------------------------------------------- holdings gate
def fires(t, side):
    if H is None:
        return "holdings secret not set"
    cf = H["tickers"].get(t)
    if not cf or cf.get("target", 0) == 0:
        return "no, not held"
    order = cf.get("order", "")
    cap = H.get("ai_group_now_pct")
    if side == "BUY":
        if "SELL" in order:
            return "no, already above target"
        if "BUY" not in order:
            return "no, within band"
        if t in CFG["ai_group"] and cap is not None and cap > CFG["ai_cap_pct"]:
            return f"no, AI group {cap}% over the {CFG['ai_cap_pct']}% cap"
        return f"YES: {order}"
    if "SELL" in order:
        return f"YES: {order}"
    if "BUY" in order:
        return "no, below target; it only pauses adds"
    return "no, within band"


# ---------------------------------------------------------------- anomalies
def anomalies(rows, bench):
    """rows: list of per-ticker dicts already evaluated. Returns a list of alert strings."""
    out = []
    for o in rows:
        d, px, sig = o["d"], o["px"], o["d"].get("sigma")
        pc = d["c"][-1]
        mv = (px / pc - 1) * 100
        if sig:
            z = (px / pc - 1) / sig
            o["z"] = z
            big = abs(z) >= AN["sigma_single"]
            mid = abs(z) >= AN["sigma_single_with_abs"] and abs(mv) >= AN["abs_floor_pct"]
            if big or mid:
                out.append(("move", o["t"],
                            f"{o['t']} {mv:+.1f}% today ({z:+.1f} sigma vs its own range)  ${px:,.2f}"))
            # A slide spread over three sessions never trips the single-day test.
            c6 = d.get("closes60") or []
            if len(c6) >= 3:
                run = px / c6[-3] - 1
                if abs(run / sig) >= AN["sigma_run3"] and abs(run * 100) >= AN["abs_floor_pct"]:
                    out.append(("run", o["t"],
                                f"{o['t']} {run*100:+.1f}% over 3 sessions "
                                f"({run/sig:+.1f} sigma)  ${px:,.2f}"))
    # group rotation
    ai = [o for o in rows if o["t"] in CFG["ai_group"]]
    if ai and bench:
        g1 = sum((o["px"] / o["d"]["c"][-1] - 1) * 100 for o in ai) / len(ai)
        b1 = (bench["px"] / bench["d"]["c"][-1] - 1) * 100
        if abs(g1 - b1) >= AN["group_vs_bench_1d_pts"]:
            out.append(("group1", "AI",
                        f"AI group {g1:+.1f}% vs {CFG['benchmark']} {b1:+.1f}% today"
                        f" ({g1-b1:+.1f} pts apart)"))
        def r5(o):
            c = o["d"].get("closes60") or []
            return (o["px"] / c[-5] - 1) * 100 if len(c) >= 5 else None
        gv = [r5(o) for o in ai if r5(o) is not None]
        bv = r5(bench)
        if gv and bv is not None:
            g5 = sum(gv) / len(gv)
            if abs(g5 - bv) >= AN["group_vs_bench_5d_pts"]:
                out.append(("group5", "AI",
                            f"AI group {g5:+.1f}% vs {CFG['benchmark']} {bv:+.1f}% over 5 sessions"
                            f" ({g5-bv:+.1f} pts apart)"))
    # Several names sliding on the same day is one story. Collapse it.
    runs = [a for a in out if a[0] == "run"]
    if len(runs) >= AN.get("collapse_run_at", 3):
        out = [a for a in out if a[0] != "run"]
        names = ", ".join(a[1] for a in runs)
        out.append(("runmulti", "MANY",
                    f"{len(runs)} names moved more than {AN['abs_floor_pct']}% over 3 sessions: {names}"))
    # breadth
    moves = [(o["px"] / o["d"]["c"][-1] - 1) * 100 for o in rows]
    if moves:
        dn = sum(1 for m in moves if m <= -AN["breadth_move_pct"])
        if dn / len(moves) >= AN["breadth_frac"]:
            out.append(("breadth", "ALL",
                        f"{dn} of {len(moves)} names down more than {AN['breadth_move_pct']}%"))
    return out


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
            tr.append([o["t"], f"{o['ev']['px']:,.2f}",
                       f"{(o['ev']['px']/o['d']['c'][-1]-1)*100:+.2f}",
                       f"{o['ev']['rsi']:.0f}",
                       f"{near:+.1f}%" if near is not None else "—"])
        parts.append(f"{icon[name]} <b>{name} ({len(b)})</b>\n" + table(hdr, tr, wd))
        if name in ("BUY", "SELL"):
            for o in sorted(b, key=lambda x: x["t"]):
                parts.append(f"   <b>{o['t']}</b> → {esc(fires(o['t'], name))}")
    if alerts:
        parts.append("⚠️ <b>Unusual movement</b>\n" + "\n".join("• " + esc(a[2]) for a in alerts))
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
        "anomalies": [a[2] for a in alerts],
        "errors": errs,
        "note": "Real-time states: a signal is a cross between consecutive readings. Nothing is traded.",
        "cross_reference": "last_poll" if REALTIME else "prev_close",
    }
    days = sorted({o["d"].get("last") for o in rows if o["d"].get("last")})
    out["settled_close_date"] = days[-1] if days else None
    for o in rows:
        ev, d = o["ev"], o["d"]
        out["tickers"][o["t"]] = {
            "price": round(ev["px"], 4),
            "prev_close": round(d["c"][-1], 4),
            "day_pct": round((ev["px"] / d["c"][-1] - 1) * 100, 2),
            "rsi": round(ev["rsi"], 1),
            "keltner": ev["k"], "momentum": ev["r"],
            "upper": round(ev["up"], 4), "lower": round(ev["lo"], 4),
            "ma3": round(ev["ma3"], 4),
            "buy_trigger": {k: round(v, 4) for k, v in (o.get("tb") or {}).items()},
            "sell_trigger": {k: round(v, 4) for k, v in (o.get("ts") or {}).items()},
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
        keep = {k: state.get(k) for k in ("saw_regular", "digest_day", "obs")}
        state.clear()
        state.update({"day": today, "sent": [], "pending": {}})
        state.update({k: v for k, v in keep.items() if v is not None})
    state.setdefault("obs", {})
    want_digest = (MODE == "digest") or (
        sess == "CLOSED" and state.get("saw_regular") == today and state.get("digest_day") != today)
    if sess == "CLOSED" and not want_digest:
        print(f"market CLOSED, nothing to do (1 call)")
        return None, sess, []
    if sess == "REGULAR":
        state["saw_regular"] = today

    cache, fresh, cerrs = build_cache(tickers, today)
    rows, errs = [], list(cerrs)
    for t in tickers:
        d = cache.get(t)
        if not isinstance(d, dict) or "ema" not in d:
            errs.append(f"{t}: no cached bars")
            continue
        try:
            lv = probe if t == bench else live(t)
            px = lv["px"]
            if px is None:
                errs.append(f"{t}: no price")
                continue
            # Reject an obviously broken extended-hours print on a thin name.
            pc = d["c"][-1]
            mv = abs(px / pc - 1)
            if sess in ("PRE", "POST") and t in THIN and mv > 0.15 and lv["n"] < 5:
                errs.append(f"{t}: suspect {mv*100:.0f}% extended print on {lv['n']} bars, ignored")
                continue
            ref = state["obs"].get(t) if REALTIME else None
            ev = evaluate(d, px, lv["hi"], lv["lo"], ref)
            nxt = obs(ev) if REALTIME else None
            tb, ts = trigger(d, "BUY", nxt), trigger(d, "SELL", nxt)
            near = None
            for side, dd in (("BUY", tb), ("SELL", ts)):
                for v in dd.values():
                    p = (v / px - 1) * 100
                    if near is None or abs(p) < abs(near):
                        near = round(p, 1)
            rows.append({"t": t, "d": d, "ev": ev, "px": px, "tb": tb, "ts": ts, "near": near})
        except Exception as e:
            errs.append(f"{t}: {e}")
    if not rows:
        return None, sess, errs
    b = next((o for o in rows if o["t"] == bench), None)
    alerts = anomalies(rows, b)
    write_status(rows, sess, alerts, errs)
    return (rows, sess, alerts, errs, want_digest), sess, errs


def main():
    if MODE == "test":
        try:
            lv = live(CFG["benchmark"])
            cache, fresh, errs = build_cache([CFG["benchmark"]], lv.get("edate"))
            d = cache[CFG["benchmark"]]
            ev = evaluate(d, lv["px"], lv["hi"], lv["lo"])
            msg = (f"{CFG['benchmark']} ${lv['px']:,.2f} · RSI {ev['rsi']:.0f} · "
                   f"channel {ev['k']} · momentum {ev['r']} · market {lv['sess']}")
        except Exception as e:
            msg = f"⚠️ price fetch failed: {e}"
        hs = "set" if H else "NOT SET (signals will say so)"
        send("✅ <b>Stock alerts are working.</b>\n\n" + esc(msg) +
             f"\n\nHoldings secret: {hs}\nTickers watched: {len(CFG['tickers'])}")
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
    rows, sess, alerts, errs, want_digest = res

    today = state.get("day")
    by_t = {o["t"]: o for o in rows}
    msgs = []

    def line(o, side, src, extra=""):
        both = "  <b>STRONG</b>" if o["ev"]["k"] == o["ev"]["r"] == side else ""
        tag = {"PRE": " · pre-market", "POST": " · after hours"}.get(sess, "")
        return (f"{'🟢' if side == 'BUY' else '🔴'} <b>{o['t']} {side}</b>{both}{tag}{extra}\n"
                f"${o['ev']['px']:,.2f}  ·  RSI {o['ev']['rsi']:.0f}  ·  {src} rule\n"
                f"Fires: {esc(fires(o['t'], side))}")

    def still_crossed(o, side, leg):
        ev = o["ev"]
        if leg == "k":
            return ev["px"] <= ev["up"] if side == "SELL" else ev["ma3"] > ev["lo"]
        return ev["rsi"] <= 70 if side == "SELL" else ev["rsi"] >= 30

    # 1. Thin names in extended hours: a cross seen on the last poll is sent only if the
    #    price is still on the far side of the line now. One odd print cannot trigger it.
    for key, p in list(state["pending"].items()):
        state["pending"].pop(key)
        t, side = key.split(":")
        o = by_t.get(t)
        if o is None or key in state["sent"] or not isinstance(p, dict):
            continue
        if still_crossed(o, side, p["leg"]):
            state["sent"].append(key)
            msgs.append(line(o, side, {"k": "channel", "r": "momentum"}[p["leg"]],
                             " · held for two readings"))

    # 2. New crosses at this reading.
    for o in rows:
        for leg, src in (("k", "channel"), ("r", "momentum")):
            side = o["ev"][leg]
            if side not in ("BUY", "SELL"):
                continue
            key = f"{o['t']}:{side}"
            if key in state["sent"] or key in state["pending"]:
                break
            if CONFIRM_THIN and sess in ("PRE", "POST") and o["t"] in THIN:
                state["pending"][key] = {"leg": leg, "sess": sess}
                print(f"{key} thin extended-hours cross, waiting one reading")
                break
            state["sent"].append(key)
            msgs.append(line(o, side, src))
            break

    # 3. This reading becomes the reference for the next one.
    for o in rows:
        state["obs"][o["t"]] = obs(o["ev"])

    new_alerts = []
    for kind, who, text in alerts:
        key = f"anom:{kind}:{who}"
        if key in state["sent"]:
            continue
        state["sent"].append(key)
        new_alerts.append(text)

    if msgs:
        send("⚡ <b>Live signal</b>  ·  real-time\n\n" + "\n\n".join(msgs) +
             "\n\n<i>Nothing has been traded.</i>")
    if new_alerts:
        send("⚠️ <b>Unusual movement</b>  ·  " + SESS_LABEL.get(sess, sess) + "\n\n" +
             "\n".join("• " + esc(a) for a in new_alerts) +
             "\n\n<i>Information only. No signal and no order is implied.</i>")
    if want_digest:
        send(digest(rows, sess, alerts))
        state["digest_day"] = today

    json.dump(state, open(STATE, "w"))
    print(f"session {sess} · {len(rows)} priced · {len(msgs)} signal(s) · "
          f"{len(new_alerts)} anomaly alert(s) · {len(errs)} error(s)")
    for e in errs[:10]:
        print("  " + e, file=sys.stderr)
    if len(errs) >= len(CFG["tickers"]):
        sys.exit("every ticker failed")


if __name__ == "__main__":
    main()
