"""Stock splits and reverse splits. On 5 Nov 2026 SOXX split and the alerter, holding daily bars
cached before the split, reported a 67 % crash. These tests replay that on GOOGL fixture bars."""
import datetime as dt

import signals as SG
from conftest import et

SPLIT_TS = et(2026, 11, 5, 9, 30)
HOLD = {"as_of": "2026-11-04", "cash_usd": 1000, "sgov_floor_pct": 8, "min_order_usd": 200, "ai_cap_pct": 30,
        "tickers": {"GOOGL": {"shares": 10, "target_pct": 20, "group": "ai"},
                    "QQQ": {"shares": 5, "target_pct": 30, "group": "core"}}}


def _poll(w, state):
    w.SPLIT_NOTES.clear()
    res, sess, errs = w.poll(state)
    return res, sess, errs


def _setup(make_watch, **split):
    w, m = make_watch(tickers=("QQQ", "GOOGL"), holdings=HOLD)
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now = et(2026, 11, 5, 4, 30)                     # pre-market, before the split shows up
    res, _, _ = _poll(w, state)
    close = next(o for o in res[0] if o["t"] == "GOOGL")["d"]["c"][-1]
    m.now = et(2026, 11, 5, 8, 0)
    m.splits = {"GOOGL": dict({"date": SPLIT_TS, "n": 3, "d": 1}, **split)}
    m.prices = {"GOOGL": close / 3 * 1.01}           # +1 % in real terms
    return w, m, state, close


def _check_fixed(w, state, res, close):
    g = next(o for o in res[0] if o["t"] == "GOOGL")
    assert abs(g["d"]["c"][-1] - close / 3) < 1e-6 * close   # history now in post-split prices
    assert abs(g["px"] / g["d"]["c"][-1] - 1.01) < 1e-9
    alerts = res[2]
    assert not any("GOOGL" in (a.get("title", "") + a.get("text", "")) for a in alerts), alerts
    assert any("3-for-1 split" in n for n in w.SPLIT_NOTES)
    assert state["splits"]["GOOGL"] == {"2026-11-05": {"k": 3.0}}
    assert w.H["tickers"]["GOOGL"]["shares"] == 30 and w.H0["tickers"]["GOOGL"]["shares"] == 10


def test_split_yahoo_has_not_adjusted_yet_is_adjusted_here(make_watch):
    w, m, state, close = _setup(make_watch, adjusted=False)
    res, _, errs = _poll(w, state)
    _check_fixed(w, state, res, close)
    assert "+1.0%" in w.SPLIT_NOTES[0]
    # The note is sent once, and the next poll sees nothing unusual.
    res, _, _ = _poll(w, state)
    assert not w.SPLIT_NOTES


def test_split_already_adjusted_by_yahoo(make_watch):
    w, m, state, close = _setup(make_watch, adjusted=True)
    res, _, _ = _poll(w, state)
    _check_fixed(w, state, res, close)


def test_unconfirmed_split_like_jump_is_held_with_one_note(make_watch):
    w, m, state, close = _setup(make_watch, adjusted=False, event=False)
    res, _, errs = _poll(w, state)
    assert not any(o["t"] == "GOOGL" for o in res[0])
    assert any("GOOGL: held" in e for e in errs)
    assert len(w.SPLIT_NOTES) == 1 and "hasn't confirmed" in w.SPLIT_NOTES[0]
    res, _, _ = _poll(w, state)
    assert not w.SPLIT_NOTES                           # not repeated
    assert w.H["tickers"]["GOOGL"]["shares"] == 10     # nothing scaled without a split


def test_evening_check_uses_split_adjusted_settled_bars(make_watch):
    w, m, state, close = _setup(make_watch, adjusted=False)
    m.now = et(2026, 11, 5, 16, 30)
    r = w.settled_rows("GOOGL", settled=True)
    assert abs(r["rows"][-1][3] - close / 3) < 1e-6 * close


def test_adjust_splits_rules():
    D = lambda n: dt.date(2026, 10, n)
    bars = [{"date": D(i), "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000} for i in (1, 2, 5)]
    # 1-for-10 reverse split effective the 5th; the post-split bar opens near 1000.
    raw = bars[:2] + [{"date": D(5), "o": 1001.0, "h": 1010.0, "l": 995.0, "c": 1004.0, "v": 100}]
    out, applied = SG.adjust_splits(raw, [(D(5), 0.1)])
    assert applied == [(D(5), 0.1)] and out[0]["c"] == 1000.0 and out[0]["v"] == 100
    # History Yahoo already adjusted is not adjusted twice.
    adj = [dict(b, c=1000.0, o=1000.0) for b in raw[:2]] + raw[2:]
    out, applied = SG.adjust_splits(adj, [(D(5), 0.1)])
    assert applied == [] and out[0]["c"] == 1000.0
    # A split effective today, no post-split bar yet: the live price is the reference.
    out, applied = SG.adjust_splits(bars[:2], [(D(5), 4.0)], ref_px=25.3)
    assert applied and out[-1]["c"] == 25.0
    out, applied = SG.adjust_splits(bars[:2], [(D(5), 4.0)], ref_px=None)
    assert applied == []


def test_split_like_only_matches_big_jumps_near_split_ratios():
    assert SG.split_like(450.41, 146.78) == 3.0
    assert SG.split_like(10.0, 100.4) == 0.1
    assert SG.split_like(450.41, 144.36) == 3.0   # 3-for-1 plus a -3.9 % real move (SOXX, 5 Nov 13:10)
    assert SG.split_like(100, 45) is None       # -55 %: not within 7 % of a 2-for-1 or 5-for-2
    assert SG.split_like(100, 80) is None       # a 20 % move is never checked
    assert SG.split_text(3) == "3-for-1 split" and SG.split_text(0.1) == "1-for-10 reverse split"


def test_split_listed_in_config_when_yahoo_has_no_event_yet(make_watch):
    w, m, state, close = _setup(make_watch, adjusted=False, event=False)
    w.CFG["splits"] = {"GOOGL": [["2026-11-05", 3]]}
    res, _, errs = _poll(w, state)
    _check_fixed(w, state, res, close)
    assert not any("GOOGL: held" in e for e in errs)


def test_listed_split_is_checked_even_when_the_real_move_is_large(make_watch):
    w, m, state, close = _setup(make_watch, adjusted=False, event=False)
    w.CFG["splits"] = {"GOOGL": [["2026-11-05", 3]]}
    m.prices = {"GOOGL": close / 3 * 0.85}           # 3-for-1 and a real -15 % move
    res, _, errs = _poll(w, state)
    g = next(o for o in res[0] if o["t"] == "GOOGL")
    assert abs(g["d"]["c"][-1] - close / 3) < 1e-6 * close
    assert any("-15.0%" in n for n in w.SPLIT_NOTES)
