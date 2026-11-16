"""Fundamentals snapshots for the investor panel, ported from ai-hedge-fund (virattt, MIT) to free data.

ai-hedge-fund's investor personas read a point-in-time "FundamentalsSnapshot": up to eight
trailing-twelve-month periods, newest first, with market cap, P/E, ROE, margins, debt/equity,
current ratio, revenue growth, EPS, book value and free cash flow per share. The original buys those
numbers from Financial Datasets; this builds the same table from what companies file with the SEC
(XBRL company facts) and Yahoo's split-adjusted daily closes, both free.

It also ports the original's PEAD (post-earnings-announcement drift) rule. The original fires on a
consensus BEAT or MISS, which needs paid analyst estimates; here the surprise is read from the
market's own reaction: the stock's move over the three sessions around the earnings 8-K (item 2.02),
minus SPY's move. A clear reaction (2% or more either way) gives a drift view for 60 sessions.

Writes docs/snapshots.json (public company data only, no holdings). Needs SEC_USER_AGENT.
Usage: python3 tools/snapshot.py [--offline DIR] [--today YYYY-MM-DD]
"""
import argparse
import http.cookiejar
import math
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".claude", "skills", "trader-analyst"))
import facts as FA  # noqa: E402  (day-trader-skill's SEC reader, MIT)

OUT = os.path.join(ROOT, "docs", "snapshots.json")
CONFIG = os.path.join(ROOT, "config.json")
NO_FUNDAMENTALS = {"GLDM": "gold", "SGOV": "Treasury bills", "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum"}
N_PERIODS = 8
DRIFT_MIN_PCT = 2.0        # abnormal 3-session reaction that counts as a clear surprise
DRIFT_SESSIONS = 60        # the original's horizon (Bernard & Thomas: drift runs to about the next print)
DRIFT_FRESH_DAYS = 10      # an 8-K older than this is no longer news
YAHOO_UA = {"User-Agent": "Mozilla/5.0 (stock-alerts snapshot)"}

CONCEPTS = dict(FA.CONCEPTS)
CONCEPTS.update({
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "dil_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
})
INSTANTS = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "liabilities": ["Liabilities"],
    "liab_equity": ["LiabilitiesAndStockholdersEquity"],
    "assets_cur": ["AssetsCurrent"],
    "liab_cur": ["LiabilitiesCurrent"],
}


def _d(s):
    return dt.date.fromisoformat(s)


# ---------------------------------------------------------------- XBRL to quarters
def flow_quarters(cf, key, unit="USD"):
    """{end: value} three-month values for a flow concept: direct 3-month facts, then year-to-date
    differences (cash-flow statements report 6- and 9-month totals), then fiscal year minus three
    quarters. Concepts are tried in order and fill each other's gaps."""
    out = {}
    for concept in CONCEPTS[key]:
        per = FA.periods(FA.facts_for(cf, concept, unit))
        if not per:
            continue
        q = {}
        for (s, e), f in per.items():
            if 80 <= (_d(e) - _d(s)).days <= 100:
                q[e] = f["val"]
        by_start = defaultdict(list)
        for (s, e), f in per.items():
            by_start[s].append((e, f["val"]))
        for s, rows in by_start.items():
            rows.sort()
            prev_end, prev_val = _d(s) - dt.timedelta(days=1), 0
            for e, v in rows:
                if 80 <= (_d(e) - prev_end).days <= 100 and e not in q:
                    q[e] = v - prev_val
                prev_end, prev_val = _d(e), v
        for (s, e), f in per.items():
            if 350 <= (_d(e) - _d(s)).days <= 380 and e not in q:
                inside = sorted(k for k in q if _d(s) <= _d(k) < _d(e))
                if len(inside) == 3:
                    q[e] = f["val"] - sum(q[k] for k in inside)
        for e, v in q.items():
            out.setdefault(e, v)
    return out


def share_quarters(cf):
    """{end: diluted weighted shares} (3-month values; a fiscal-year value stands in for Q4)."""
    out = {}
    for concept in CONCEPTS["dil_shares"]:
        per = FA.periods(FA.facts_for(cf, concept, "shares"))
        for (s, e), f in per.items():
            days = (_d(e) - _d(s)).days
            if 80 <= days <= 100 or (350 <= days <= 380 and e not in out):
                out[e] = f["val"]
    return out


def instant(cf, keys):
    out = {}
    for concept in keys:
        latest = {}
        for f in FA.facts_for(cf, concept, "USD"):
            if "start" in f or f.get("form", "").split("/")[0] not in ("10-Q", "10-K", "20-F", "40-F"):
                continue
            if f["end"] not in latest or f.get("filed", "") > latest[f["end"]].get("filed", ""):
                latest[f["end"]] = f
        for e, f in latest.items():
            out.setdefault(e, f["val"])
    return out


def first_filed(cf):
    """{period end: the date it was first filed} from the revenue or net-income facts."""
    out = {}
    for key in ("revenue", "net_income"):
        for concept in CONCEPTS[key]:
            for f in FA.facts_for(cf, concept, "USD"):
                if f.get("form", "").split("/")[0] in ("10-Q", "10-K") and f.get("filed"):
                    if f["end"] not in out or f["filed"] < out[f["end"]]:
                        out[f["end"]] = f["filed"]
    return out


def ttm(q, end):
    """Sum of the four quarters ending at `end`, or None if any is missing."""
    ends = sorted(k for k in q if _d(k) <= _d(end) and (_d(end) - _d(k)).days < 300)
    if not ends or ends[-1] != end or len(ends) < 4:
        return None
    four = ends[-4:]
    return sum(q[k] for k in four) if (_d(four[-1]) - _d(four[0])).days < 300 else None


def adjust_splits(shares):
    """Make older share counts comparable after a split (a jump of 1.8x or more between quarters)."""
    ends = sorted(shares)
    out, k = dict(shares), 1.0
    for a, b in reversed(list(zip(ends, ends[1:]))):
        r = shares[b] / shares[a] if shares[a] else 1
        if r >= 1.8 or r <= 0.55:
            k *= r
        out[a] = shares[a] * k
    return out


# ---------------------------------------------------------------- the snapshot
def price_on(closes, day):
    """First close on or after `day` (the market's first chance to react), else the last one."""
    later = [c for d, c in closes if d >= day]
    return later[0] if later else (closes[-1][1] if closes else None)


def build(cf, closes, sector=None, ticker="", n=N_PERIODS):
    """Company snapshot from SEC XBRL company facts."""
    return build_series(xbrl_series(cf), closes, sector, ticker, n)


def xbrl_series(cf):
    rev = flow_quarters(cf, "revenue")
    cost = flow_quarters(cf, "cost_of_revenue")
    gp = flow_quarters(cf, "gross_profit")
    for e in rev:
        if e not in gp and e in cost:
            gp[e] = rev[e] - cost[e]
    op, ni = flow_quarters(cf, "operating_income"), flow_quarters(cf, "net_income")
    ocf, capex = flow_quarters(cf, "ocf"), flow_quarters(cf, "capex")
    sh = adjust_splits(share_quarters(cf))
    eq, li = instant(cf, INSTANTS["equity"]), instant(cf, INSTANTS["liabilities"])
    le = instant(cf, INSTANTS["liab_equity"])
    for e in le:
        if e not in li and e in eq:
            li[e] = le[e] - eq[e]
    ac, lc = instant(cf, INSTANTS["assets_cur"]), instant(cf, INSTANTS["liab_cur"])
    return {"rev": rev, "gp": gp, "op": op, "ni": ni, "ocf": ocf, "capex": capex, "sh": sh,
            "eq": eq, "li": li, "ac": ac, "lc": lc, "filed": first_filed(cf)}


def build_series(S, closes, sector=None, ticker="", n=N_PERIODS, currency="USD", note=None):
    """The original's FundamentalsSnapshot from quarterly series. S["filed"] maps a period end to its
    filing date; without one (Yahoo data) the price ~45 days after the quarter end stands in."""
    rev, gp, op, ni, ocf, capex, sh = (S[k] for k in ("rev", "gp", "op", "ni", "ocf", "capex", "sh"))
    eq, li, ac, lc, filed = (S[k] for k in ("eq", "li", "ac", "lc", "filed"))
    div = lambda a, b: (a / b) if a is not None and b else None
    rows = []
    for e in sorted(rev, reverse=True):
        r_t, n_t = ttm(rev, e), ttm(ni, e)
        if r_t is None or n_t is None or e not in sh:
            continue
        year_ago = [k for k in rev if 350 <= (_d(e) - _d(k)).days <= 380]
        r_prev = ttm(rev, year_ago[0]) if year_ago else None
        o_t, c_t = ttm(ocf, e), ttm(capex, e)
        f_day = filed.get(e)
        px_day = f_day or (_d(e) + dt.timedelta(days=45)).isoformat()
        px = price_on(closes, px_day) if px_day <= (closes[-1][0] if closes else "") else (closes[-1][1] if closes else None)
        mcap = px * sh[e] if px else None
        rows.append({
            "period": e, "filed": f_day, "market_cap": mcap,
            "pe": div(mcap, n_t) if n_t and n_t > 0 else None,
            "roe": div(n_t, eq.get(e)), "gross_m": div(ttm(gp, e), r_t), "op_m": div(ttm(op, e), r_t),
            "net_m": div(n_t, r_t), "de": div(li.get(e), eq.get(e)), "current": div(ac.get(e), lc.get(e)),
            "rev_growth": (r_t / r_prev - 1) if r_prev else None,
            "eps": n_t / sh[e], "bvps": div(eq.get(e), sh[e]),
            "fcf_ps": (o_t - c_t) / sh[e] if o_t is not None and c_t is not None else None,
            "shares": sh[e], "net_income_ttm": n_t,
        })
        if len(rows) == n:
            break
    if not rows:
        return None
    vals = lambda k: [r[k] for r in rows if r[k] is not None]
    avg = lambda a: sum(a) / len(a) if a else None
    gm = vals("gross_m")
    years = (_d(rows[0]["period"]) - _d(rows[-1]["period"])).days / 365.25
    b0, b1 = rows[-1]["bvps"], rows[0]["bvps"]
    four = [r for r in rows if 350 <= (_d(rows[0]["period"]) - _d(r["period"])).days <= 380]
    snap = {"ticker": ticker, "sector": sector, "periods": rows, "kind": "company", "currency": currency, "note": note,
            "roe_avg": avg(vals("roe")), "net_margin_avg": avg(vals("net_m")),
            "gross_margin_trend": (gm[0] - gm[-1]) if len(gm) > 1 else None,
            "bvps_cagr": ((b1 / b0) ** (1 / years) - 1) if b0 and b1 and b0 > 0 and b1 > 0 and years >= 0.9 else None,
            "debt_to_equity_latest": rows[0]["de"], "market_cap_latest": rows[0]["market_cap"],
            "eps_growth_yoy": (rows[0]["eps"] / four[0]["eps"] - 1) if four and four[0]["eps"] and four[0]["eps"] > 0 else None}
    snap["text"] = render(snap)
    snap["hash"] = hashlib.sha256(snap["text"].encode()).hexdigest()[:16]
    return snap


def _fmt(v):
    """The original's number style: ratios to two decimals, big numbers in B / M."""
    if v is None:
        return "n/a"
    if abs(v) >= 1e9:
        return f"{v / 1e9:,.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:,.1f}M"
    return f"{v:,.2f}"


def render(s):
    lines = [f"Company: {s['ticker']}" + (f"  |  Sector: {s['sector']}" if s.get("sector") else "")
             + (f"  |  Figures in {s['currency']}" if s.get("currency", "USD") != "USD" else ""),
             "All figures below were publicly filed by their filing dates. "
             "Treat the most recent filing shown as the present."]
    if s.get("note"):
        lines.append(s["note"])
    lines += ["", "Summary:",
             f"  Market cap (latest filed): {_fmt(s['market_cap_latest'])}",
             f"  ROE avg: {_fmt(s['roe_avg'])}  |  Net margin avg: {_fmt(s['net_margin_avg'])}",
             f"  Gross margin trend (latest-oldest): {_fmt(s['gross_margin_trend'])}",
             f"  Book value/share CAGR: {_fmt(s['bvps_cagr'])}",
             f"  Liabilities/equity (latest): {_fmt(s['debt_to_equity_latest'])}", "",
             "History (trailing-twelve-month periods, newest first):",
             "period | filed | mktcap | P/E | ROE | gross_m | op_m | net_m | L/E | curr | rev_gr | EPS | BVPS | FCF/sh"]
    for p in s["periods"]:
        lines.append(" | ".join([p["period"], p["filed"] or "?"] + [_fmt(p[k]) for k in (
            "market_cap", "pe", "roe", "gross_m", "op_m", "net_m", "de", "current", "rev_growth", "eps", "bvps", "fcf_ps")]))
    return "\n".join(lines)


# ---------------------------------------------------------------- earnings drift (PEAD, market-reaction version)
def earnings_drift(submissions, closes, spy, today):
    """The newest earnings 8-K and the market's reaction: close of the 3rd session from the filing
    day versus the close before it, minus SPY over the same window."""
    rec = (submissions.get("filings") or {}).get("recent") or {}
    for i, form in enumerate(rec.get("form", [])):
        if form == "8-K" and "2.02" in ((rec.get("items") or [""] * (i + 1))[i] or ""):
            day = rec["filingDate"][i]
            break
    else:
        return None
    out = {"filed": day, "age_days": (_d(today) - _d(day)).days}
    before = [(d, c) for d, c in closes if d < day]
    after = [(d, c) for d, c in closes if d >= day]
    sb, sa = [(d, c) for d, c in spy if d < day], [(d, c) for d, c in spy if d >= day]
    if not before or len(after) < 3 or not sb or len(sa) < 3:
        out["status"] = "waiting for three sessions after the filing"
        return out
    move = (after[2][1] / before[-1][1] - 1) * 100
    mkt = (sa[2][1] / sb[-1][1] - 1) * 100
    abn = move - mkt
    out.update({"reaction_pct": round(move, 2), "spy_pct": round(mkt, 2), "abnormal_pct": round(abn, 2),
                "window": [before[-1][0], after[2][0]]})
    if abs(abn) < DRIFT_MIN_PCT:
        out["status"], out["view"] = "no clear surprise", "none"
    else:
        out["status"], out["view"] = "clear reaction", "up" if abn > 0 else "down"
        out["until_sessions"] = DRIFT_SESSIONS
    out["fresh"] = out["age_days"] <= DRIFT_FRESH_DAYS
    return out


# ---------------------------------------------------------------- Yahoo (non-SEC companies and ETFs)
class Yahoo:
    """Yahoo's free endpoints. The fund-holdings endpoint wants a session cookie and a 'crumb', fetched
    once the way common Yahoo download tools do; the financial time series need neither."""
    def __init__(self, offline=None):
        self.offline, self.crumb = offline, None
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, url, name=None, raw=False):
        if self.offline:
            return json.load(open(os.path.join(self.offline, name)))
        for i in range(3):
            try:
                with self.op.open(urllib.request.Request(url, headers=YAHOO_UA), timeout=30) as r:
                    body = r.read().decode()
                return body if raw else json.loads(body)
            except Exception:
                if i == 2:
                    raise
                time.sleep(2 + 3 * i)

    def summary(self, t, modules):
        if not self.offline and not self.crumb:
            try:
                self.op.open(urllib.request.Request("https://fc.yahoo.com", headers=YAHOO_UA), timeout=20)
            except Exception:
                pass                                   # it answers 404 but sets the cookie
            self.crumb = self.get("https://query2.finance.yahoo.com/v1/test/getcrumb", raw=True).strip()
        url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{t}?modules={','.join(modules)}"
               f"&crumb={urllib.request.quote(self.crumb or '')}")
        return self.get(url, f"summary-{t}.json")["quoteSummary"]["result"][0]


TS_TYPES = {"rev": "TotalRevenue", "gp": "GrossProfit", "op": "OperatingIncome", "ni": "NetIncomeCommonStockholders",
            "sh": "DilutedAverageShares", "eq": "StockholdersEquity", "li": "TotalLiabilitiesNetMinorityInterest",
            "ac": "CurrentAssets", "lc": "CurrentLiabilities", "ocf": "OperatingCashFlow", "capex": "CapitalExpenditure"}


def yahoo_series(ts_json):
    """Quarterly series from Yahoo's fundamentals time series. Capital expenditure comes negative there."""
    S = {k: {} for k in TS_TYPES}
    S["filed"] = {}
    by_type = {}
    for res in (ts_json.get("timeseries") or {}).get("result") or []:
        typ = ((res.get("meta") or {}).get("type") or [""])[0]
        by_type[typ] = res.get(typ) or []
    for k, name in TS_TYPES.items():
        for row in by_type.get("quarterly" + name, []):
            if row and row.get("reportedValue") and row.get("periodType") == "3M":
                v = row["reportedValue"]["raw"]
                S[k][row["asOfDate"]] = -v if k == "capex" else v
    if not S["ni"]:
        for row in by_type.get("quarterlyNetIncome", []):
            if row and row.get("reportedValue"):
                S["ni"][row["asOfDate"]] = row["reportedValue"]["raw"]
    return S


ANNUAL = ("TotalRevenue", "OperatingIncome", "NetIncomeCommonStockholders", "DilutedEPS")


def annual_lines(ts_json, currency):
    """Fiscal-year revenue, operating income, net income and EPS, oldest first, for the history check."""
    by = {}
    for res in (ts_json.get("timeseries") or {}).get("result") or []:
        typ = ((res.get("meta") or {}).get("type") or [""])[0]
        if typ.startswith("annual"):
            for row in res.get(typ) or []:
                if row and row.get("reportedValue"):
                    by.setdefault(row["asOfDate"], {})[typ[6:]] = row["reportedValue"]["raw"]
    if not by:
        return None
    big = lambda v: "n/a" if v is None else (f"{v / 1e12:,.1f}T" if abs(v) >= 1e12 else f"{v / 1e9:,.1f}B")
    out = []
    for end in sorted(by):
        r = by[end]
        out.append(f"  FY to {end}: revenue {big(r.get('TotalRevenue'))}, operating income {big(r.get('OperatingIncome'))}, "
                   f"net income {big(r.get('NetIncomeCommonStockholders'))}, EPS "
                   + ("n/a" if r.get("DilutedEPS") is None else f"{r['DilutedEPS']:,.2f}"))
    return f"Fiscal years ({currency}), oldest first:\n" + "\n".join(out)


def yahoo_company(t, Y, closes, today):
    types = ",".join(["quarterly" + v for v in list(TS_TYPES.values()) + ["NetIncome"]] + ["annual" + v for v in ANNUAL])
    p1 = int(time.mktime((_d(today) - dt.timedelta(days=6 * 365)).timetuple()))
    js = Y.get(f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{t}"
               f"?type={types}&period1={p1}&period2={int(time.time()) + 86400}", f"timeseries-{t}.json")
    cur = "KRW" if t.endswith(".KS") else "SGD" if t.endswith(".SI") else "USD"
    note = "Source: Yahoo Finance quarterly figures (filing dates not given; prices ~45 days after each quarter end)."
    years = annual_lines(js, cur)
    return build_series(yahoo_series(js), closes, None, t, currency=cur, note=note + ("\n" + years if years else ""))


def _raw(x):
    return x.get("raw") if isinstance(x, dict) else x


def etf_snapshot(t, q):
    """An ETF read as one business made of its holdings: top holdings, the holdings' valuation and
    growth, sector mix, cost and track record (Yahoo topHoldings, fundProfile, defaultKeyStatistics)."""
    th, fp, ks = q.get("topHoldings") or {}, q.get("fundProfile") or {}, q.get("defaultKeyStatistics") or {}
    eh = th.get("equityHoldings") or {}
    hold = [(h.get("symbol") or h.get("holdingName"), _raw(h.get("holdingPercent"))) for h in th.get("holdings") or []]
    sect = sorted(((k, _raw(v)) for d in th.get("sectorWeightings") or [] for k, v in d.items()), key=lambda x: -(x[1] or 0))
    ratios = {k: _raw(eh.get(k)) for k in ("priceToEarnings", "priceToBook", "priceToSales", "priceToCashflow",
                                           "medianMarketCap", "threeYearEarningsGrowth")}
    # Yahoo's fund "price to X" fields are reciprocals (earnings per $ of price): VOO's 0.041 is a P/E of ~24.
    for k in ("priceToEarnings", "priceToBook", "priceToSales", "priceToCashflow"):
        v = ratios[k]
        ratios[k] = None if not v else (round(1 / v, 2) if 0 < v < 1 else v)   # 0 means Yahoo has no figure
    if not hold and not any(ratios.values()):
        return None
    fee = _raw(((fp.get("feesExpensesInvestment") or {}).get("annualReportExpenseRatio")))
    pct = lambda v: "n/a" if v is None else f"{v * 100:.1f}%"
    num = lambda v: "n/a" if v is None else (f"{v / 1e9:,.1f}B" if abs(v) >= 1e9 else f"{v:,.2f}")
    lines = [f"Fund: {t}" + (f"  |  Category: {fp.get('categoryName')}" if fp.get("categoryName") else ""),
             "This is an exchange-traded fund, not a single company. Judge it as one business made of its "
             "holdings: the quality, growth and price of what it owns, and how concentrated it is.", "",
             "Top holdings (share of the fund): " + ", ".join(f"{s} {pct(w)}" for s, w in hold[:10]),
             f"Top 10 together: {pct(sum(w or 0 for _, w in hold[:10]))}",
             "Holdings' valuation (fund-weighted): " + f"P/E {num(ratios['priceToEarnings'])} | P/B {num(ratios['priceToBook'])} | "
             f"P/S {num(ratios['priceToSales'])} | P/CF {num(ratios['priceToCashflow'])} | "
             f"median market cap {num(ratios['medianMarketCap'] * 1e6 if ratios['medianMarketCap'] else None)}",
             f"Holdings' 3-year earnings growth: {pct(ratios['threeYearEarningsGrowth'])}",
             "Sectors: " + ", ".join(f"{k} {pct(v)}" for k, v in sect[:5] if v),
             f"Fund: expense ratio {pct(fee)}, total assets {num(_raw(ks.get('totalAssets')))}, "
             f"3-year average return {pct(_raw(ks.get('threeYearAverageReturn')))}, 5-year {pct(_raw(ks.get('fiveYearAverageReturn')))}"]
    text = "\n".join(lines)
    # The hash ignores day-to-day ratio noise: holdings rounded to 1 point, P/E and growth to 5.
    r5 = lambda v: None if v is None else int(round(v / 5.0))
    key = json.dumps([[s, round((w or 0) * 100)] for s, w in hold[:10]] +
                     [r5(ratios["priceToEarnings"]), r5((ratios["threeYearEarningsGrowth"] or 0) * 100)])
    return {"ticker": t, "kind": "etf", "periods": [], "text": text, "hash": hashlib.sha256(key.encode()).hexdigest()[:16],
            "holdings": hold[:10], "ratios": ratios, "pe_now": ratios["priceToEarnings"]}


def panel_tickers():
    """Every watched ticker the panel can judge, and the ones it cannot (with the reason)."""
    c = json.load(open(CONFIG))
    want = list(c["tickers"]) + [t for t in c.get("info_tickers", []) if t not in c["tickers"]]
    fx = set((c.get("fx") or {}).keys())
    out, skip = [], {}
    for t in want:
        if t in fx:
            continue
        if t in NO_FUNDAMENTALS:
            skip[t] = f"{NO_FUNDAMENTALS[t]}: no company financials to judge"
        else:
            out.append(t)
    return out, skip


# ---------------------------------------------------------------- fetching
def yahoo_closes(t, offline=None):
    if offline:
        path = os.path.join(offline, f"yahoo-{t}.json")
        r = json.load(open(path))["chart"]["result"][0]
    else:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=5y&interval=1d"
        req = urllib.request.Request(url, headers=YAHOO_UA)
        for i in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    r = json.load(resp)["chart"]["result"][0]
                break
            except Exception:
                if i == 2:
                    raise
                time.sleep(2 + 3 * i)
    off = (r.get("meta") or {}).get("gmtoffset", 0)
    q = r["indicators"]["quote"][0]["close"]
    return [(dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date().isoformat(), c)
            for ts, c in zip(r["timestamp"], q) if c]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline")
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    today = a.today or dt.date.today().isoformat()
    ua = FA.user_agent(config=None)
    if not ua and not a.offline:
        print("SEC_USER_AGENT is not set: no snapshots (the SEC requires a contact name and email)")
        return 0
    fetch = FA.Fetcher(ua=ua, offline=a.offline)
    ciks = FA.ticker_ciks(fetch)
    Y = Yahoo(a.offline)
    spy = yahoo_closes("SPY", a.offline)
    tickers, skip = panel_tickers()
    out = {"as_of": today, "source": "SEC XBRL company facts, Yahoo Finance closes, financial time series and fund "
           "holdings; layout from ai-hedge-fund (MIT)", "stocks": {}, "errors": {}, "not_covered": skip}
    for t in tickers:
        try:
            closes = yahoo_closes(t, a.offline)
            snap, cf = None, None
            if t in ciks:                                   # US-listed: SEC filings first
                try:                                        # (some ETF trusts are listed too, without statements)
                    cik, name = ciks[t]
                    cf = fetch.json(FA.FACTS_URL % cik, f"companyfacts-CIK{cik}.json")
                    sub = fetch.json(FA.SUBMISSIONS_URL % cik, f"submissions-CIK{cik}.json")
                    snap = build(cf, closes, sub.get("sicDescription"), t)
                    if snap:
                        snap["name"] = name
                        snap["drift"] = earnings_drift(sub, closes, spy, today)
                except (FA.FactsError, KeyError, ValueError):
                    snap = None
            if snap is None and (t.endswith(".KS") or t.endswith(".SI") or t in ciks):
                try:                                        # Korean / Singapore listings, or SEC data too thin (new listings)
                    snap = yahoo_company(t, Y, closes, today)
                except Exception:
                    snap = None
            if snap is None:
                q = Y.summary(t, ["topHoldings", "fundProfile", "defaultKeyStatistics", "price"])
                snap = etf_snapshot(t, q)
                if snap:
                    snap["name"] = ((q.get("price") or {}).get("longName")) or t
            if not snap:
                raise ValueError("no company financials or fund holdings found")
            snap.setdefault("name", t)
            snap["price"] = closes[-1][1]
            snap["price_date"] = closes[-1][0]
            if snap["kind"] == "company":
                last = snap["periods"][0]
                snap["market_cap_now"] = closes[-1][1] * last["shares"]
                snap["pe_now"] = snap["market_cap_now"] / last["net_income_ttm"] if last["net_income_ttm"] > 0 else None
            out["stocks"][t] = snap
        except Exception as e:  # one odd ticker must not stop the others
            out["errors"][t] = f"{type(e).__name__}: {e}"[:200]
    json.dump(out, open(OUT, "w"), indent=1)
    print(json.dumps({"stocks": list(out["stocks"]), "errors": out["errors"], "not_covered": list(skip)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
