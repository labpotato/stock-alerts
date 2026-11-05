"""The four changes of 2026-11-05 evening, after a -3 % market day sent 9 signals and 12
unusual-move messages. Numbers are the real 5 Nov readings."""
import signals as SG


def test_a_band_falling_under_a_falling_average_is_not_a_buy():
    # QQQ: 4 Nov settled MA3 686.107 just under the lower band 686.11; 5 Nov the band fell to
    # 676.53 while the 3-day average fell to 678.70. Before the rule change this was a BUY.
    k, _ = SG.states(691.95, 686.107, 738.17, 686.11, 38.5, 658.86, 678.70, 737.59, 676.53, 29.2)
    assert k == "WATCH BUY"
    # A real recovery: the 3-day average rises back above the band.
    k, _ = SG.states(30.0, 32.270, 34.9, 32.277, 31.0, 32.6, 32.467, 34.8, 32.193, 33.0)
    assert k == "BUY"


def test_b_real_time_uses_the_last_settled_close(make_watch):
    w, _ = make_watch()
    assert w.REALTIME is False and w.CFG["cross_reference"] == "prev_close"


def _row(t, pc, px, sig, c3=None):
    c = [c3 or pc, pc, pc]
    return {"t": t, "px": px, "d": {"c": c, "sigma": sig / 100, "closes60": [pc] * 3 + c},
            "ev": {"px": px, "t": t}, "sess": "REGULAR"}


def test_c_on_a_big_market_day_one_market_message_and_only_stocks_that_beat_or_lag_it(make_watch):
    w, _ = make_watch(tickers=("VOO", "QQQ", "NVDA", "TSLA", "XLP"), benchmark="VOO")
    rows = [_row("VOO", 100, 96.92, 0.688), _row("QQQ", 100, 95.22, 1.1), _row("NVDA", 100, 93.27, 2.079),
            _row("TSLA", 100, 90.46, 1.95), _row("XLP", 100, 99.78, 1.236)]
    a = w.anomalies(rows, rows[0])
    keys = [x["key"] for x in a]
    assert "anom:market" in keys
    assert "anom:move:TSLA" in keys                      # -9.5 % against a -3.1 % market
    for t in ("VOO", "QQQ", "NVDA", "XLP"):
        assert f"anom:move:{t}" not in keys, keys
    assert "anom:breadth:ALL" not in keys                # the market message replaces it
    tsla = next(x for x in a if x["key"] == "anom:move:TSLA")
    assert "worse than the market" in tsla["text"]


def test_c_on_a_calm_market_day_moves_are_judged_on_their_own(make_watch):
    w, _ = make_watch(tickers=("VOO", "TSLA"), benchmark="VOO")
    rows = [_row("VOO", 100, 100.2, 0.688), _row("TSLA", 100, 90.0, 1.95)]
    keys = [x["key"] for x in w.anomalies(rows, rows[0])]
    assert "anom:move:TSLA" in keys and "anom:market" not in keys


def test_d_second_measure_later_the_same_day_is_a_short_strong_upgrade(make_watch):
    w, _ = make_watch(tickers=("QQQ", "XLF"))
    state = {"sent": ["XLF:SELL:r"], "pending": {}, "obs": {}}
    ev = {"k": "SELL", "r": "NONE", "px": 58.04, "ma3": 58.14, "up": 58.32, "lo": 54.63, "rsi": 61.4, "t": "XLF"}
    o = {"t": "XLF", "ev": ev, "ref": {"px": 59.2, "ma3": 58.8, "up": 57.98, "lo": 54.9, "rsi": 71.3},
         "d": {"c": [59, 59.5, 59.2]}, "edge": {}, "sess": "REGULAR"}
    msgs = w.decide([o], [], state, "REGULAR")
    assert len(msgs) == 1
    text, png = msgs[0]
    assert text.startswith("🔴🔴 <b>Now STRONG SELL — XLF</b>") and png is None
    assert "agreeing with momentum earlier today" in text
    assert len(text) < 600
    assert "XLF:SELL:k" in state["sent"]
