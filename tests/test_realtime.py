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
    assert "⏳ <b>Likely range</b>" in conf[0] and "What really followed this signal" in conf[0]   # predictions on every chart
    assert "⏳ <b>Likely range</b>" in fade[0] and "What really followed" not in fade[0]
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


def test_evening_check_writes_the_settled_block_but_sends_nothing(make_watch):
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    assert w.CFG["evening_messages"] is False
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now = et(2026, 11, 4, 16, 30)
    w.evening(state)
    assert w.SENT == []
    assert w.read_status()["settled"]["tickers"]["BOTZ"]["k_state"] == "BUY"


def test_crypto_is_read_around_the_clock():
    """BTC-USD and ETH-USD (Dongmin 2026-11-16) have their own always-open market, not the US session."""
    import watch as w
    assert w.market("BTC-USD") == "crypto" and w.market("ETH-USD") == "crypto"
    assert w.market("SPCX") == "us"
    import json, os
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
    assert {"SPCX", "BTC-USD", "ETH-USD"} <= set(cfg["tickers"])
    assert {"BTC-USD", "ETH-USD"} <= set(cfg["not_equity"])


def test_a_signal_never_repeats_on_the_same_bar_across_the_day_reset(make_watch):
    """22-23 Nov: ETH-USD's RSI BUY went out at 00:01 and again at 05:01 UTC, because the day list resets
    at midnight New York while the reference bar had not changed. The bar memory survives the reset."""
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now, m.prices = et(2026, 11, 3, 18, 0), {"BOTZ": 32.50}
    res, sess, _ = w.poll(state)
    w.decide(res[0], [], state, sess)
    m.now, m.prices = et(2026, 11, 4, 9, 45), {"BOTZ": 32.60}
    res, sess, _ = w.poll(state)
    first = w.decide(res[0], [], state, sess)
    assert any("BOTZ" in t.split("\n")[0] for t, _ in first)
    bar = next(o for o in res[0] if o["t"] == "BOTZ")["d"]["last"]
    assert state["fired"]["BOTZ:BUY:k"] == bar
    state["day"] = "2026-11-03"                         # force poll's real day reset on the next reading
    m.now, m.prices = et(2026, 11, 4, 10, 15), {"BOTZ": 32.65}
    res, sess, _ = w.poll(state)
    assert state["sent"] == [] and state["fired"]["BOTZ:BUY:k"] == bar      # reset cleared the day, kept the bar
    assert not [t for t, _ in w.decide(res[0], [], state, sess) if "BOTZ" in t.split("\n")[0]]


def test_market_wide_notes_need_us_stocks_and_the_benchmark_and_come_once_a_day(make_watch):
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    coin = lambda t, mv: {"t": t, "px": 100 * (1 + mv / 100), "d": {"c": [100.0], "sigma": 0.03, "closes60": [100.0] * 6}}
    rows = [coin("BTC-USD", -6), coin("ETH-USD", -7)]
    w.CFG["tickers"] = ["QQQ", "BOTZ", "BTC-USD", "ETH-USD"]
    w.CFG["not_equity"] = ["GLDM", "BTC-USD", "ETH-USD"]
    keys = {a["key"] for a in w.anomalies(rows, None)}
    assert "anom:breadth:ALL" not in keys and "anom:market" not in keys      # overnight: only the coins
    monkey = {"t": "x"}
    state = {"sent": [], "pending": {}, "obs": {}}
    w.anomaly_message = lambda a, rows, sess: (a["key"], None)
    mk = lambda k, kind: {"key": k, "kind": kind, "ticker": None, "text": k}
    out = w.decide([], [mk("anom:market", "market"), mk("anom:group1:AI", "group1")], state, "REGULAR")
    assert [t for t, _ in out] == ["anom:market"]                           # one market-wide note
    assert w.decide([], [mk("anom:group5:AI", "group5")], state, "REGULAR") == []


def test_one_reading_goes_out_as_one_message_plus_one_album(make_watch, monkeypatch):
    """25 Nov: 'why are you sending separate msgs for the stock and crypto'. Everything found in one
    reading is now one text, then the charts as one album."""
    w, m = make_watch(tickers=("QQQ", "BOTZ"))
    m.now = et(2026, 11, 23, 9, 0)
    texts, albums, photos = [], [], []
    monkeypatch.setattr(w, "send", lambda t: texts.append(t))
    monkeypatch.setattr(w, "send_album", lambda items: (albums.append(items) or True))
    monkeypatch.setattr(w, "send_photo", lambda png, cap: (photos.append(cap) or True))
    outbox = [("🟢⚪ <b>BUY — NVDA</b>\nNVIDIA Corp\nWhy...", b"png1"),
              ("🟢⚪ <b>BUY — BTC-USD</b>\nBitcoin\nWhy...", b"png2"),
              ("⚠️ <b>Unusual move — ETH-USD</b>\nDown 6%.", None)]
    w.notify_all(outbox)
    assert len(texts) == 1 and texts[0].startswith("📣 <b>3 alerts in this reading</b>")
    assert "BUY — NVDA" in texts[0] and "BUY — BTC-USD" in texts[0] and "Unusual move — ETH-USD" in texts[0]
    assert albums == [[("🟢⚪ BUY — NVDA", b"png1"), ("🟢⚪ BUY — BTC-USD", b"png2")]] and photos == []
    # A single finding still goes out as before (chart with its explanation as the caption).
    w.notify_all([("⚠️ <b>Unusual move — XLV</b>\nUp 3%.", b"png")])
    assert photos == ["⚠️ <b>Unusual move — XLV</b>\nUp 3%."]
    # Over Telegram's limit the text splits between alerts, never inside one.
    long = [(f"<b>T{i}</b>\n" + "x" * 900, None) for i in range(6)]
    plan = w.batch_plan(long)
    assert len(plan["texts"]) == 2 and all(w.plain_len(t) <= 3900 for t in plan["texts"])
    assert sum(t.count("<b>T") for t in plan["texts"]) == 6


def test_a_new_listing_shows_its_price_until_it_has_enough_days_for_signals(make_watch):
    """ANT (Anthropic) listed in November 2026 with a few days of trading: price-only on the board."""
    import json
    w, m = make_watch(tickers=("QQQ", "BOTZ", "ANT"))
    import os as _os
    _os.makedirs(_os.path.dirname(w.BOARD), exist_ok=True)
    w.NEW_LISTINGS.clear()
    w.NEW_LISTINGS["ANT"] = {"px": 81.905, "prev": 78.0, "sess": "REGULAR"}
    w.write_board([], {"generated_utc": "2026-11-25T15:00:00+00:00", "session": "REGULAR", "tickers": {}})
    e = json.load(open(w.BOARD))["tickers"]["ANT"]
    assert e["keltner"] == "NEW" and e["price"] == 81.905 and e["day_pct"] == 5.01 and e["rsi"] is None
    import json as _j, os
    cfg = _j.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
    assert "ANT" in cfg["tickers"] and "ANT" in cfg["ai_group"]          # Dongmin 2026-11-25


def _twin_rows(w, m, monkeypatch):
    real = m.chart
    w.chart = lambda t, *a, **k: real("QQQ" if t == "IQQ" else t, *a, **k)
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now, m.prices = et(2026, 11, 4, 15, 50), {"QQQ": 600.0, "IQQ": 600.0}
    res, sess, _ = w.poll(state)
    return res[0], sess, state


def test_iqq_and_qqq_are_one_report(make_watch, monkeypatch):
    """Dongmin, 25 Nov: IQQ and QQQ are the same index, so one fire is reported once (under QQQ)."""
    w, m = make_watch(tickers=("QQQ", "IQQ"))
    rows, sess, state = _twin_rows(w, m, monkeypatch)
    for o in rows:
        o["ev"]["k"] = "BUY"
    msgs = w.decide(rows, [], state, sess)
    heads = [t.split("\n")[0] for t, _ in msgs]
    assert len(msgs) == 1 and "QQQ" in heads[0] and "IQQ" not in heads[0]
    assert "also covers IQQ" in msgs[0][0] and "tracks the same index" in msgs[0][0]
    assert "IQQ:BUY:k" in state["sent"]          # recorded, so it stays quiet for the day
    # QQQ already told today; IQQ crossing later on its own stays quiet too
    for o in rows:
        o["ev"]["k"] = "BUY" if o["t"] == "IQQ" else "NONE"
    state["sent"].remove("IQQ:BUY:k")
    state["fired"].pop("IQQ:BUY:k", None)
    assert w.decide(rows, [], state, sess) == []


def test_iqq_alone_still_reports(make_watch, monkeypatch):
    w, m = make_watch(tickers=("QQQ", "IQQ"))
    rows, sess, state = _twin_rows(w, m, monkeypatch)
    for o in rows:
        o["ev"]["k"] = "BUY" if o["t"] == "IQQ" else "NONE"
    msgs = w.decide(rows, [], state, sess)
    assert len(msgs) == 1 and "IQQ" in msgs[0][0].split("\n")[0]


def test_signal_shows_the_algorithm_with_its_numbers_and_the_panel(make_watch):
    w, m = make_watch(tickers=("QQQ", "GLDM"))
    w._PANEL = {"GLDM": {"at": "2026-11-24", "price": 80, "verdicts": {
        "buffett": {"signal": "bullish", "confidence": 70, "reasoning": "Gold is a store of value."},
        "munger": {"signal": "neutral", "confidence": 40, "reasoning": "Too hard."},
        "graham": {"signal": "bearish", "confidence": 20, "reasoning": "No earnings."},
        "lynch": {"abstained": True, "signal": "neutral", "confidence": 0, "reasoning": ""},
        "druckenmiller": {"signal": "bullish", "confidence": 60, "reasoning": "Trend up."}}}}
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now, m.prices = et(2026, 11, 4, 15, 50), {"GLDM": 80.38}
    res, sess, _ = w.poll(state)
    text = next(t for t, _ in w.decide(res[0], [], state, sess) if "GLDM" in t.split("\n")[0])
    assert "My algorithm (Rule A)" in text and "Price range BUY" in text and "✅" in text
    assert "Fires? <b>" in text.split("\n")[0]
    assert "Investor panel" in text and "leans bullish" in text and "agrees with this BUY" in text
    assert "/panel_gldm" in text and "Strongest view, Buffett" in text
    assert text.index("My algorithm") < text.index("Investor panel") < text.index("Your plan")
