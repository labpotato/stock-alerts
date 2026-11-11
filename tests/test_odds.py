"""Time-machine range and signal odds on alerts (Dongmin 2026-11-11), as revised after the out-of-sample
check: the fan shows the range only (trend removed), the odds come from what really followed each signal."""
import os
import random

import pytest

import odds as OD
from conftest import et


def walk(n=250, drift=0.0, seed=1):
    rnd, c = random.Random(seed), [100.0]
    for _ in range(n):
        c.append(c[-1] * (1 + drift + rnd.gauss(0, 0.02)))
    return c


def test_the_fan_is_repeatable_ordered_widening_and_has_no_trend():
    a = OD.fan(walk())
    assert a == OD.fan(walk()) and a["v"] == 2                         # seed 19: anyone can repeat it
    assert a["replays"] == 10000 and len(a["p50"]) == 20
    assert all(a["p10"][j] <= a["p25"][j] <= a["p50"][j] <= a["p75"][j] <= a["p90"][j] for j in range(20))
    assert a["p90"][-1] - a["p10"][-1] > a["p90"][0] - a["p10"][0]     # the fan widens with time
    up, down = OD.fan(walk(drift=0.006)), OD.fan(walk(drift=-0.006))
    assert abs(up["p50"][-1] - 1) < 0.02 and abs(down["p50"][-1] - 1) < 0.02   # last year's trend is not projected
    assert OD.fan(walk(n=30)) is None


def test_the_long_record_prefers_the_tickers_own_history_then_the_pooled_one():
    nvda = OD.history("NVDA", ("k", "r"), "BUY")
    assert nvda and nvda["n"] >= 10 and nvda["base"]["n"] > 50000 and nvda["since"] == "2010"
    pooled = OD.history("AIPO", ("k",), "BUY")                         # no backtest history of its own
    assert pooled["scope"] == "pooled" and pooled["n"] == OD._hist()["signals"]["k:BUY"]["n"]
    assert OD.history("NVDA", (), "BUY") is None


def test_a_signal_message_carries_the_range_and_the_long_record_and_the_chart_draws(make_watch):
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now, m.prices = et(2026, 11, 3, 18, 0), {"BOTZ": 32.50}
    res, sess, _ = w.poll(state)
    w.decide(res[0], [], state, sess)
    m.now, m.prices = et(2026, 11, 4, 9, 45), {"BOTZ": 32.60}
    res, sess, _ = w.poll(state)
    botz = next(o for o in res[0] if o["t"] == "BOTZ")
    text, _ = w.signal_message(botz, "BUY", ["k"], [], sess)
    assert "⏳ <b>Likely range</b>: in 20 sessions, 8 in 10 of 10,000 replays of BOTZ's past-year moves end between" in text
    assert "📜 <b>What really followed this signal</b>" in text and "On an ordinary day: higher 61%" in text
    assert "ends higher in" not in text                                 # the fan's direction odds are gone
    # The 10-minute alerter run skips the drawing (it installs matplotlib only when it draws);
    # the tests workflow installs matplotlib, so every pull request checks the chart.
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    w.mpl = lambda: plt
    box, _ = w.odds_text("BOTZ", botz["d"], "BUY", ["k"])
    png = w.price_chart("BOTZ", "Global X Robotics & AI ETF", botz["d"], botz["ev"], "BUY at $32.60", "#2e7d32",
                        "recovered into its normal range", odds=box)
    assert png and png[:4] == b"\x89PNG"
    out = os.environ.get("ODDS_PNG")
    if out:
        open(out, "wb").write(png)


def test_every_price_chart_and_its_message_carry_the_range(make_watch):
    """Dongmin 2026-11-11: predictions on every chart, not only on signals."""
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now, m.prices = et(2026, 11, 4, 9, 45), {"BOTZ": 32.60}
    res, sess, _ = w.poll(state)
    botz = next(o for o in res[0] if o["t"] == "BOTZ")
    text, _ = w.mixed_message(botz, sess)
    assert "⏳ <b>Likely range</b>" in text and "What really followed" not in text   # no side, no record
    a = {"ticker": "BOTZ", "title": "Unusual move — BOTZ", "chart_head": "Unusual move", "note": "x", "text": "y"}
    text, _ = w.anomaly_message(a, res[0], sess)
    assert "⏳ <b>Likely range</b>" in text
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    drawn = []
    real = plt.Axes.fill_between if hasattr(plt, "Axes") else None
    w.mpl = lambda: plt
    import matplotlib.axes
    orig = matplotlib.axes.Axes.fill_between
    matplotlib.axes.Axes.fill_between = lambda self, *x, **k: (drawn.append(k.get("label")), orig(self, *x, **k))[1]
    try:
        png = w.price_chart("BOTZ", "Global X Robotics & AI ETF", botz["d"], botz["ev"], "Big move", "#ef6c00", "x")
    finally:
        matplotlib.axes.Axes.fill_between = orig
    assert png and "80% of replays" in drawn                              # the fan is drawn without being asked
