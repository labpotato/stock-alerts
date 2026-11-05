"""Shared fixtures. No test touches the network: watch.chart is replaced by a fake market built
from real Yahoo bars saved in tests/fixtures (see tools/make_fixtures.py)."""
import copy, datetime as dt, importlib, json, os, sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)
ET = dt.timezone(dt.timedelta(hours=-5))        # 3-4 Nov 2026 are on US standard time


def et(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


def fixture(t, kind="1d"):
    return json.load(open(os.path.join(FIX, f"{t}_{kind}.json")))


def fixture_bars(t):
    """Real daily bars as dicts with a date, the same shape signals.hygiene takes."""
    r = fixture(t)
    q, off = r["indicators"]["quote"][0], r["meta"].get("gmtoffset", 0)
    out = []
    for i, ts in enumerate(r["timestamp"]):
        out.append({"date": dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date(),
                    "o": q["open"][i], "h": q["high"][i], "l": q["low"][i], "c": q["close"][i],
                    "v": (q.get("volume") or [None] * len(r["timestamp"]))[i]})
    return out


class FakeMarket:
    """Serves Yahoo-shaped chart results from the fixtures, as they would look at `now`."""

    def __init__(self):
        self.now = et(2026, 11, 4, 16, 30)
        self.prices = {}          # live price per ticker
        self.extended_high = {}   # an extended-hours 1-minute bar high, per ticker

    def _day(self):
        return dt.datetime.fromtimestamp(self.now, ET).date()

    def _periods(self):
        d = self._day()
        at = lambda h, m: dt.datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp()
        return {"pre": {"start": at(4, 0), "end": at(9, 30)},
                "regular": {"start": at(9, 30), "end": at(16, 0)},
                "post": {"start": at(16, 0), "end": at(20, 0)}}

    def _last_trade(self):
        p = self._periods()["regular"]
        if self.now >= p["end"]:
            return p["end"] - 60
        if self.now >= p["start"]:
            return self.now
        return p["start"] - 17.5 * 3600 - 60        # previous day's 16:00

    def chart(self, t, rng, iv, prepost=False):
        r = copy.deepcopy(fixture(t))
        mt = self._last_trade()
        last_day = dt.datetime.fromtimestamp(mt, ET).date()
        today = self._day()
        q = r["indicators"]["quote"][0]
        keep = [i for i, ts in enumerate(r["timestamp"])
                if dt.datetime.fromtimestamp(ts, ET).date() <= max(last_day, today if self.now >= self._periods()["regular"]["start"] else last_day)]
        r["timestamp"] = [r["timestamp"][i] for i in keep]
        for k in ("open", "high", "low", "close", "volume"):
            q[k] = [q[k][i] for i in keep]
        last_close = q["close"][-1]
        px = self.prices.get(t, last_close)
        r["meta"].update({"gmtoffset": -18000, "regularMarketTime": mt, "currentTradingPeriod": self._periods(),
                          "regularMarketPrice": last_close if iv == "1d" else px,
                          "chartPreviousClose": q["close"][-2]})
        r["meta"].pop("marketState", None)
        if iv == "1d":
            return r
        hi = self.extended_high.get(t)
        n = 30
        ts = [self.now - 60 * (n - 1 - i) for i in range(n)]
        highs = [px] * n
        if hi is not None:
            highs[-1] = hi
        return {"timestamp": ts, "meta": r["meta"],
                "indicators": {"quote": [{"open": [px] * n, "high": highs, "low": [px] * n,
                                          "close": [px] * n, "volume": [100] * n}]}}


@pytest.fixture
def make_watch(tmp_path, monkeypatch):
    """Import watch.py against a temp data dir, a test config, a fake market and a fake Telegram."""
    def _make(tickers=("QQQ", "BOTZ", "GLDM", "GOOGL"), holdings=None, fx=None, benchmark="QQQ"):
        cfg = json.load(open(os.path.join(ROOT, "config.json")))
        cfg["tickers"], cfg["benchmark"] = list(tickers), benchmark
        cfg["fx"] = fx if fx is not None else {}
        cfg["include_info_tickers"] = False
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg))
        monkeypatch.setenv("CONFIG", str(p))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TG_TOKEN", "")
        monkeypatch.setenv("TG_CHAT", "")
        monkeypatch.setenv("MODE", "poll")
        monkeypatch.setenv("HOLDINGS_JSON", json.dumps(holdings) if holdings else "")
        import watch
        w = importlib.reload(watch)
        m = FakeMarket()
        w.chart = m.chart
        w.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network in tests"))
        w.mpl = lambda: None
        w.SENT = []
        w.send = lambda text: w.SENT.append(text)
        w.send_photo = lambda png, cap: (w.SENT.append(cap) or True)
        w.time.sleep = lambda s: None
        monkeypatch.setattr(w.time, "time", lambda: m.now)
        return w, m
    return _make
