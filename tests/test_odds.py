"""Time-machine odds on signal alerts (Dongmin 2026-11-11): a fan of 10,000 replays of the stock's
own past year on the chart, the chances in the message, and the signal's real record."""
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


def test_the_fan_is_repeatable_ordered_and_follows_the_stocks_own_drift():
    a, b = OD.fan(walk()), OD.fan(walk())
    assert a == b                                                      # seed 19: anyone can repeat it
    assert a["replays"] == 10000 and len(a["p50"]) == 20
    assert all(a["p10"][j] <= a["p25"][j] <= a["p50"][j] <= a["p75"][j] <= a["p90"][j] for j in range(20))
    assert a["p90"][-1] - a["p10"][-1] > a["p90"][0] - a["p10"][0]     # the fan widens with time
    up, down = OD.fan(walk(drift=0.004)), OD.fan(walk(drift=-0.004))
    assert up["p_up"] > 60 > 40 > down["p_up"]
    assert OD.fan(walk(n=30)) is None                                  # too little history: no fan


def test_the_record_counts_past_fires_and_what_followed():
    rows = [(c, c * 1.01, c * 0.99, c) for c in walk(n=219, seed=4)]
    rec = OD.record(rows)
    assert rec and all(v["n"] >= 1 and 0 <= v["higher"] <= v["n"] for v in rec.values())
    assert set(rec) <= {"k:BUY", "k:SELL", "r:BUY", "r:SELL"}
    best = OD.summary(rec, ("k", "r"), "BUY")
    assert best is None or best["n"] == max(rec.get(k, {"n": 0})["n"] for k in ("k:BUY", "r:BUY"))
    assert OD.summary({}, ("k",), "BUY") is None


def test_a_signal_message_carries_the_odds_and_the_chart_draws_the_fan(make_watch, tmp_path):
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now, m.prices = et(2026, 11, 3, 18, 0), {"BOTZ": 32.50}
    res, sess, _ = w.poll(state)
    w.decide(res[0], [], state, sess)
    m.now, m.prices = et(2026, 11, 4, 9, 45), {"BOTZ": 32.60}
    res, sess, _ = w.poll(state)
    botz = next(o for o in res[0] if o["t"] == "BOTZ")
    assert botz["d"]["fan"]["replays"] == 10000
    text, _ = w.signal_message(botz, "BUY", ["k"], [], sess)
    assert "⏳ <b>Time machine</b> (10,000 replays of BOTZ's own past year, next 20 sessions): ends higher in" in text
    assert "📜 <b>Its real record:</b>" in text
    assert "not a forecast" in text
    # The 10-minute alerter run skips this part (it installs matplotlib only when it draws);
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
