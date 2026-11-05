"""Spec tests 5-7, adapted to the choices made on 2026-11-05:
  - a live reading is judged against the last settled close (the daily rule at the live price);
  - pre- and post-market highs and lows count in the live bar, so they can widen the band;
  - the evening check, from settled closes only, catches what real-time readings miss."""
import signals as SG
from conftest import et


def test_5_live_readings_are_judged_against_the_last_settled_close(make_watch):
    """Since 2026-11-05 (cross_reference = prev_close) a real-time reading asks: if today closed at
    this price, would the daily rule fire? A 3 Nov after-hours print is not the reference on 4 Nov."""
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    assert w.REALTIME is False
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now, m.prices = et(2026, 11, 3, 18, 0), {"BOTZ": 32.50}
    res, sess, _ = w.poll(state)
    assert sess == "POST"
    w.decide(res[0], [], state, sess)
    m.now, m.prices = et(2026, 11, 4, 9, 45), {"BOTZ": 32.60}
    res, sess, _ = w.poll(state)
    botz = next(o for o in res[0] if o["t"] == "BOTZ")
    assert abs(botz["ref"]["px"] - 32.32) < 0.01                       # the 3 Nov settled close
    assert botz["ev"]["k"] == "BUY"                                    # 3 Nov MA3 under the band, now above and rising
    w.decide(res[0], [], state, sess)
    assert "BOTZ:BUY:k" in state["sent"]
    # A second reading the same day does not repeat it.
    m.now, m.prices = et(2026, 11, 4, 10, 15), {"BOTZ": 32.65}
    res, sess, _ = w.poll(state)
    assert not [t for t, _ in w.decide(res[0], [], state, sess) if "BOTZ" in t.split("\n")[0]]
    # The evening check, from settled closes only, confirms it.
    m.now, m.prices = et(2026, 11, 4, 16, 30), {}
    msgs = w.settled_pass(state)
    heads = [t.split("\n")[0] for t, _ in msgs]
    assert any("BOTZ" in h and "CONFIRMED" in h for h in heads), heads
    st = w.read_status()["settled"]["tickers"]["BOTZ"]
    assert st["k_state"] == "BUY" and st["knife_edge"] is True


def test_6_extended_hours_highs_count_in_the_live_bar(make_watch):
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    m.now = et(2026, 11, 4, 18, 0)                       # after hours
    r = w.settled_rows("BOTZ")
    d = w.derive(r["rows"], r["dates"])
    d["ag"], d["al"] = w.rsi_acc([x[3] for x in r["rows"]])
    m.prices, m.extended_high = {"BOTZ": 32.80}, {"BOTZ": 33.60}
    lv = w.live("BOTZ")
    assert lv["sess"] == "POST" and lv["hi"] == 33.60      # the after-hours high is used
    with_ext = w.evaluate(d, lv["px"], lv["hi"], lv["lo"])
    regular_only = w.evaluate(d, lv["px"], 32.80, lv["lo"])
    assert with_ext["up"] - with_ext["lo"] > regular_only["up"] - regular_only["lo"]


def test_7_hygiene_keeps_open_equals_high_and_drops_zero_volume():
    import datetime as dt
    D = lambda n: dt.date(2026, 10, n)
    bars = [
        {"date": D(1), "o": 10.0, "h": 10.0, "l": 9.5, "c": 9.8, "v": 1000},   # open == high: real bar
        {"date": D(2), "o": 9.8, "h": 10.1, "l": 9.7, "c": 10.0, "v": 0},      # no volume: dropped
        {"date": D(3), "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 500},  # all four equal: dropped
        {"date": D(5), "o": 10.0, "h": 10.3, "l": 9.9, "c": 10.2, "v": 1200},
    ]
    kept, dropped = SG.hygiene(bars)
    assert [b["date"] for b in kept] == [D(1), D(5)]
    assert {d for d, _ in dropped} == {D(2), D(3)}
    # A partial tape on the newest row (under 30 % of the prior session's volume) is held back.
    kept, dropped = SG.hygiene(bars + [{"date": D(6), "o": 10.2, "h": 10.4, "l": 10.1, "c": 10.3, "v": 300}])
    assert kept[-1]["date"] == D(5) and any("partial" in why for _, why in dropped)
    # Currencies have no volume on Yahoo: the volume rules are off for them.
    fx_kept, _ = SG.hygiene([dict(b, v=0) for b in bars], volume_rule=False)
    assert len(fx_kept) == 3


def test_real_time_messages_are_labelled_and_never_size_an_order(make_watch):
    w, m = make_watch(tickers=("QQQ", "GLDM"))
    state = {"sent": [], "pending": {}, "obs": {}}
    # One reading at 4 Nov's closing level, compared with the 3 Nov close: GLDM crosses up.
    m.now, m.prices = et(2026, 11, 4, 15, 50), {"GLDM": 80.38}
    res, sess, _ = w.poll(state)
    msgs = w.decide(res[0], [], state, sess)
    texts = [t for t, _ in msgs if "GLDM" in t.split("\n")[0]]
    assert texts, "expected a real-time GLDM BUY"
    assert "REAL-TIME" in texts[0] and "Fires:" not in texts[0]


def test_evening_check_confirmed_and_faded(make_watch):
    w, m = make_watch(tickers=("QQQ", "GLDM"))
    m.now = et(2026, 11, 4, 16, 30)
    state = {"day": "2026-11-04", "sent": ["GLDM:BUY:k", "QQQ:BUY:k"], "pending": {}, "obs": {}}
    msgs = w.settled_pass(state)
    heads = {t.split("\n")[0]: t for t, _ in msgs}
    conf = [t for h, t in heads.items() if "CONFIRMED at close" in h and "GLDM" in h]
    fade = [t for h, t in heads.items() if "FADED by close" in h and "QQQ" in h]
    assert conf and fade, list(heads)
    assert "Fires:" in conf[0]
    assert "KNIFE-EDGE" in fade[0] and "below by 0.0001%" in fade[0]
    assert "Fires:</b> nothing" in fade[0]
    s = w.read_status()
    assert s["schema"] == 2 and s["settled"]["date"] == "2026-11-04"
    assert set(s["intraday_sent"]) == {"GLDM:BUY:k", "QQQ:BUY:k"}
    for k in ("close", "ma3", "upper", "lower", "rsi", "k_state", "r_state", "signal",
              "margin_k_pct", "margin_r_pts", "knife_edge"):
        assert k in s["settled"]["tickers"]["QQQ"]
    assert w.settled_pass(state) == []                  # once per day


def test_evening_check_waits_for_the_close(make_watch):
    w, m = make_watch(tickers=("QQQ",))
    m.now = et(2026, 11, 4, 15, 0)
    assert w.settled_pass({"sent": []}) == []
