"""Korean listings: KRX hours, KRW prices, and the bar that is still forming during the session."""
import copy, datetime as dt

from conftest import fixture

KST = dt.timezone(dt.timedelta(hours=9))


def krx_chart(now, live=None):
    """A Yahoo-shaped KRX daily chart built from real bars, dated in Korea time, with a forming
    bar for today while the session is open."""
    def chart(t, rng, iv, prepost=False):
        r = copy.deepcopy(fixture("QQQ"))
        q = r["indicators"]["quote"][0]
        d = dt.datetime.fromtimestamp(now, KST).date()
        at = lambda h, m: dt.datetime(d.year, d.month, d.day, h, m, tzinfo=KST).timestamp()
        r["timestamp"] = [int(dt.datetime(*dt.datetime.fromtimestamp(ts, dt.timezone.utc).timetuple()[:3], 9, 0, tzinfo=KST).timestamp())
                          for ts in r["timestamp"]]
        r["timestamp"].append(int(at(9, 0)))                  # today's bar, still forming
        for k in ("open", "high", "low", "close", "volume"):
            q[k].append(q[k][-1])
        r["meta"].update({"gmtoffset": 32400, "regularMarketTime": now, "regularMarketPrice": live or q["close"][-1],
                          "chartPreviousClose": q["close"][-2], "longName": "Samsung Electronics Co., Ltd.",
                          "currentTradingPeriod": {"pre": {"start": at(8, 30), "end": at(9, 0)},
                                                   "regular": {"start": at(9, 0), "end": at(15, 30)},
                                                   "post": {"start": at(15, 30), "end": at(18, 0)}}})
        return r
    return chart


def test_korean_tickers_are_watched_on_their_own_market(make_watch, monkeypatch):
    w, m = make_watch()
    monkeypatch.setitem(w.CFG, "include_info_tickers", True)
    monkeypatch.setitem(w.CFG, "info_tickers", ["005930.KS", "000660.KS"])
    assert w.market("005930.KS") == "kr" and w.market("QQQ") == "us"
    assert "005930.KS" in w.universe() and "000660.KS" in w.universe()
    assert w.money("005930.KS", 61234.0) == "₩61,234"


def test_the_forming_korean_bar_is_not_counted_as_settled(make_watch, monkeypatch):
    w, m = make_watch()
    monkeypatch.setitem(w.CFG, "currency", {"005930.KS": "KRW"})
    now = dt.datetime(2026, 11, 5, 11, 0, tzinfo=KST).timestamp()     # KRX open
    m.now = now
    w.chart = krx_chart(now)
    r = w.settled_rows("005930.KS")
    assert r["last"] < "2026-11-05"                                  # today's bar removed
    lv = w.live("005930.KS")
    assert lv["sess"] == "REGULAR"


def test_korean_market_is_checked_while_the_us_market_is_shut(make_watch, monkeypatch):
    w, m = make_watch(tickers=("QQQ",))
    monkeypatch.setitem(w.CFG, "include_info_tickers", True)
    monkeypatch.setitem(w.CFG, "info_tickers", ["005930.KS"])
    monkeypatch.setitem(w.CFG, "currency", {"005930.KS": "KRW"})
    now = dt.datetime(2026, 11, 5, 11, 0, tzinfo=KST).timestamp()     # 21:00 New York the day before
    m.now = now
    us = m.chart
    kr = krx_chart(now)
    w.chart = lambda t, *a, **k: kr(t, *a, **k) if t.endswith(".KS") else us(t, *a, **k)
    res, sess, errs = w.poll({"sent": [], "pending": {}, "obs": {}})
    assert sess == "CLOSED"                                          # US shut
    priced = [o["t"] for o in res[0]]
    assert priced == ["005930.KS"], (priced, errs)                   # Korea still checked
    assert res[0][0]["sess"] == "REGULAR"
