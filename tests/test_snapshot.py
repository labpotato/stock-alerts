"""Snapshot builder for the investor panel (ai-hedge-fund port on free SEC + Yahoo data)."""
import datetime as dt
import json

import tools.snapshot as S

# Bloom Energy's real operating cash flow facts (SEC companyconcept, fetched 2026-11-16): the 10-Qs
# report year-to-date totals, so the quarters must be differenced.
BE_OCF = [
    {"start": "2025-01-01", "end": "2025-03-31", "val": -110682000, "accn": "a1", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-04-30"},
    {"start": "2025-01-01", "end": "2025-06-30", "val": -323793000, "accn": "a2", "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-07-31"},
    {"start": "2025-01-01", "end": "2025-09-30", "val": -304124000, "accn": "a3", "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-10-28"},
    {"start": "2025-01-01", "end": "2025-12-31", "val": 113949000, "accn": "a4", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-09"},
    {"start": "2026-01-01", "end": "2026-03-31", "val": 73610000, "accn": "a5", "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2026-04-29"},
    {"start": "2026-01-01", "end": "2026-06-30", "val": 300042000, "accn": "a6", "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-07-28"},
    {"start": "2026-01-01", "end": "2026-09-30", "val": 272895000, "accn": "a7", "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-10-22"},
]


def cf_of(**concepts):
    return {"facts": {"us-gaap": {k: {"units": {u: rows}} for k, (u, rows) in concepts.items()}}}


def test_year_to_date_cash_flows_become_quarters_and_trailing_year_matches_the_filings():
    q = S.flow_quarters(cf_of(NetCashProvidedByUsedInOperatingActivities=("USD", BE_OCF)), "ocf")
    assert q["2025-06-30"] == -213111000 and q["2025-12-31"] == 418073000 and q["2026-09-30"] == -27147000
    # TTM to Sep 2026 = FY2025 + 9M 2026 - 9M 2025, straight from the filed totals.
    assert S.ttm(q, "2026-09-30") == 113949000 + 272895000 - (-304124000)


def synthetic(years=(2023, 2024, 2025, 2026), split_after="2025-06-30"):
    """A tidy company: revenue +10%/quarter-ish, 3-month income facts, YTD cash flows, balance sheet."""
    qe = ["03-31", "06-30", "09-30", "12-31"]
    starts = ["01-01", "04-01", "07-01", "10-01"]
    rev, ni, gp, op, ocf, capex, sh, eq, li, ac, lc = ([] for _ in range(11))
    n, cum_o, cum_c = 0, 0, 0
    for y in years:
        cum_o = cum_c = 0
        for i in range(4):
            if y == 2026 and i == 3:
                break
            n += 1
            e, s = f"{y}-{qe[i]}", f"{y}-{starts[i]}"
            filed = (dt.date.fromisoformat(e) + dt.timedelta(days=30)).isoformat()
            form = "10-K" if i == 3 else "10-Q"
            row = lambda v, st=s: {"start": st, "end": e, "val": v, "accn": f"x{n}", "form": form, "filed": filed}
            r = 1000e6 * 1.05 ** n
            rev.append(row(r)); ni.append(row(0.2 * r)); gp.append(row(0.6 * r)); op.append(row(0.3 * r))
            cum_o += 0.25 * r; cum_c += 0.05 * r
            ocf.append(row(cum_o, f"{y}-01-01")); capex.append(row(cum_c, f"{y}-01-01"))
            shares = 1000e6 * (10 if e > split_after else 1)                       # a 10-for-1 split
            sh.append(row(shares))
            inst = lambda v: {"end": e, "val": v, "accn": f"x{n}", "form": form, "filed": filed}
            eq.append(inst(5000e6 + 100e6 * n)); li.append(inst(2500e6)); ac.append(inst(3000e6)); lc.append(inst(1500e6))
    return cf_of(RevenueFromContractWithCustomerExcludingAssessedTax=("USD", rev), NetIncomeLoss=("USD", ni),
                 GrossProfit=("USD", gp), OperatingIncomeLoss=("USD", op),
                 NetCashProvidedByUsedInOperatingActivities=("USD", ocf), PaymentsToAcquirePropertyPlantAndEquipment=("USD", capex),
                 WeightedAverageNumberOfDilutedSharesOutstanding=("shares", sh), StockholdersEquity=("USD", eq),
                 Liabilities=("USD", li), AssetsCurrent=("USD", ac), LiabilitiesCurrent=("USD", lc))


def closes(start="2023-01-01", end="2026-11-13", px=10.0):
    d, out = dt.date.fromisoformat(start), []
    while d <= dt.date.fromisoformat(end):
        if d.weekday() < 5:
            out.append((d.isoformat(), px))
        d += dt.timedelta(days=1)
    return out


def test_the_snapshot_has_the_originals_table_split_adjusted_and_hashes_only_the_fundamentals():
    s = S.build(synthetic(), closes(), "Semiconductors", "TEST")
    p = s["periods"]
    assert len(p) == 8 and p[0]["period"] == "2026-09-30" and p[0]["filed"] == "2026-10-30"
    assert abs(p[0]["gross_m"] - 0.6) < 1e-9 and abs(p[0]["net_m"] - 0.2) < 1e-9 and abs(p[0]["current"] - 2) < 1e-9
    assert abs(p[0]["fcf_ps"] / p[0]["eps"] - 1.0) < 1e-9                      # FCF (0.25-0.05 of revenue) = net income
    assert all(abs(x["shares"] - 10000e6) < 1 for x in p)                     # pre-split counts scaled 10x
    assert abs(p[0]["rev_growth"] - (1.05 ** 4 - 1)) < 1e-6
    assert s["text"].startswith("Company: TEST  |  Sector: Semiconductors")
    assert "period | filed | mktcap | P/E | ROE | gross_m | op_m | net_m | L/E | curr | rev_gr | EPS | BVPS | FCF/sh" in s["text"]
    assert S.build(synthetic(), closes(px=99.0), "Semiconductors", "TEST")["hash"] != s["hash"]   # filed-date prices are fundamentals
    assert S.build(synthetic(), closes(), "Semiconductors", "TEST")["hash"] == s["hash"]


def test_earnings_drift_reads_the_markets_reaction_to_the_8k():
    sub = {"filings": {"recent": {"form": ["10-Q", "8-K", "8-K"], "items": ["", "2.02,9.01", "5.02"],
                                  "filingDate": ["2026-11-05", "2026-11-04", "2026-10-01"],
                                  "accessionNumber": ["a", "b", "c"], "primaryDocument": ["x", "y", "z"]}}}
    stock = [("2026-11-03", 100.0), ("2026-11-04", 104.0), ("2026-11-05", 106.0), ("2026-11-06", 108.0), ("2026-11-09", 107.0)]
    spy = [(d, 500.0 * (1.01 if d >= "2026-11-06" else 1)) for d, _ in stock]
    d = S.earnings_drift(sub, stock, spy, "2026-11-09")
    assert d["filed"] == "2026-11-04" and d["window"] == ["2026-11-03", "2026-11-06"]
    assert d["reaction_pct"] == 8.0 and d["spy_pct"] == 1.0 and d["abnormal_pct"] == 7.0
    assert d["view"] == "up" and d["fresh"] and d["until_sessions"] == 60
    assert S.earnings_drift(sub, stock[:3], spy[:3], "2026-11-05")["status"].startswith("waiting")
    flat = S.earnings_drift(sub, [(x, 100.0) for x, _ in stock], spy, "2026-11-09")
    assert flat["view"] == "none"


def ts_rows(name, values, start="2024-03-31"):
    ends = []
    d = dt.date.fromisoformat(start)
    for i in range(len(values)):
        y, m = d.year + (d.month - 1 + 3 * i) // 12, (d.month - 1 + 3 * i) % 12 + 1
        last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).isoformat()
        ends.append(last)
    return {"meta": {"type": [name]}, name: [{"asOfDate": e, "periodType": "3M", "currencyCode": "KRW",
                                               "reportedValue": {"raw": v}} for e, v in zip(ends, values)]}


def test_korean_listings_come_from_yahoos_quarterly_series_in_won():
    n = 9
    js = {"timeseries": {"result": [
        ts_rows("quarterlyTotalRevenue", [70e12 + 5e12 * i for i in range(n)]),
        ts_rows("quarterlyGrossProfit", [28e12 + 2e12 * i for i in range(n)]),
        ts_rows("quarterlyOperatingIncome", [8e12 + 1e12 * i for i in range(n)]),
        ts_rows("quarterlyNetIncomeCommonStockholders", [6e12 + 1e12 * i for i in range(n)]),
        ts_rows("quarterlyDilutedAverageShares", [6.7e9] * n),
        ts_rows("quarterlyStockholdersEquity", [400e12] * n),
        ts_rows("quarterlyTotalLiabilitiesNetMinorityInterest", [100e12] * n),
        ts_rows("quarterlyCurrentAssets", [200e12] * n), ts_rows("quarterlyCurrentLiabilities", [80e12] * n),
        ts_rows("quarterlyOperatingCashFlow", [15e12] * n), ts_rows("quarterlyCapitalExpenditure", [-10e12] * n)]}}
    import tools.snapshot as SN
    series = SN.yahoo_series(js)
    assert series["capex"]["2024-03-31"] == 10e12                      # Yahoo's negative capex turned positive
    snap = SN.build_series(series, closes(px=100000.0), None, "005930.KS", currency="KRW", note="Source: Yahoo")
    p = snap["periods"][0]
    assert snap["kind"] == "company" and len(snap["periods"]) == 6 and p["period"] == "2026-03-31"
    assert p["filed"] is None and abs(p["current"] - 2.5) < 1e-9
    assert abs(p["fcf_ps"] - 4 * 5e12 / 6.7e9) < 1e-6
    assert "Figures in KRW" in snap["text"] and "Source: Yahoo" in snap["text"]


def test_an_etf_is_read_as_a_basket_of_its_holdings():
    q = {"topHoldings": {"holdings": [{"symbol": "NVDA", "holdingName": "NVIDIA", "holdingPercent": {"raw": 0.091}},
                                      {"symbol": "AVGO", "holdingPercent": {"raw": 0.085}}],
                         "equityHoldings": {"priceToEarnings": {"raw": 31.2}, "priceToBook": {"raw": 7.1},
                                            "threeYearEarningsGrowth": {"raw": 0.24}, "medianMarketCap": {"raw": 150000}},
                         "sectorWeightings": [{"technology": {"raw": 0.97}}, {"industrials": {"raw": 0.02}}]},
         "fundProfile": {"categoryName": "Technology", "feesExpensesInvestment": {"annualReportExpenseRatio": {"raw": 0.0035}}},
         "defaultKeyStatistics": {"totalAssets": {"raw": 14e9}, "threeYearAverageReturn": {"raw": 0.21}}}
    e = S.etf_snapshot("SOXX", q)
    assert e["kind"] == "etf" and e["pe_now"] == 31.2
    assert "This is an exchange-traded fund, not a single company" in e["text"]
    assert "Top holdings (share of the fund): NVDA 9.1%, AVGO 8.5%" in e["text"]
    assert "P/E 31.20" in e["text"] and "3-year earnings growth: 24.0%" in e["text"] and "expense ratio 0.4%" in e["text"]
    q["topHoldings"]["equityHoldings"]["priceToEarnings"] = {"raw": 31.9}                 # daily noise: same hash
    assert S.etf_snapshot("SOXX", q)["hash"] == e["hash"]
    assert S.etf_snapshot("X", {}) is None


def test_every_watched_ticker_is_covered_or_says_why_not():
    tickers, skip = S.panel_tickers()
    assert {"NVDA", "SPCX", "SOXX", "VOO", "005930.KS", "000660.KS"} <= set(tickers)
    assert set(skip) >= {"GLDM", "BTC-USD", "ETH-USD"} and "gold" in skip["GLDM"]
    assert not any(t.endswith("=X") for t in tickers)
