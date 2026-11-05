"""Order gate and sizing (spec item 7), and the rule that holdings never reach a public file."""
import datetime as dt
import json
import os

import orders as OR
from conftest import et

AI = {"GOOGL", "NVDA", "BOTZ", "IQQ"}
TODAY = dt.date(2026, 11, 4)
CLOSES = {"GLDM": 80.0, "GOOGL": 290.0, "BOTZ": 32.8, "SGOV": 100.0, "ES3.SI": 4.0, "IQQ": 50.0, "NVDA": 200.0}
USDSGD = 1.30


def book(**over):
    # Made-up numbers. Never use real holdings in tests: this repository is public.
    h = {"as_of": "2026-11-01", "cash_usd": 1100, "sgov_floor_pct": 8, "min_order_usd": 200, "ai_cap_pct": 30,
         "tickers": {"GLDM": {"shares": 0, "target_pct": 7, "group": "diversifier"},
                     "GOOGL": {"shares": 11, "target_pct": 6, "group": "ai"},
                     "BOTZ": {"shares": 12, "target_pct": 2, "group": "ai"},
                     "NVDA": {"shares": 0, "target_pct": 15, "group": "ai"},
                     "SGOV": {"shares": 25, "target_pct": 10, "group": "cash"},
                     "ES3.SI": {"shares": 400, "target_pct": 4, "group": "sgd"},
                     "IQQ": {"shares": 18, "group": "ai"}}}
    h.update(over)
    return h


def line(h, t, side, paused=None):
    return OR.fires_line(h, t, side, CLOSES, USDSGD, AI, TODAY, paused, ["IQQ"])


def test_book_converts_es3_and_computes_the_ai_share_live():
    b = OR.book(book(), CLOSES, USDSGD, AI)
    assert b["values"]["ES3.SI"] == 400 * 4.0 / 1.30
    assert b["total"] == sum(b["values"].values()) + 1100
    assert b["ai_pct"] > 30


def test_buy_under_target_is_sized_and_paid_from_cash():
    a = OR.armed(book(), "GLDM", CLOSES, USDSGD, AI)
    assert a["side"] == "BUY" and a["shares"] == round(a["gap"] / 80.0)
    text, fires = line(book(), "GLDM", "BUY")
    assert fires and text.startswith(f"BUY {a['shares']} shares") and "paid from cash" in text


def test_buy_falls_back_to_sgov_above_its_floor():
    text, fires = line(book(cash_usd=150), "GLDM", "BUY")
    assert fires and "SGOV" in text and "cash" in text


def test_buy_reports_the_largest_fundable_size_when_short():
    h = book(cash_usd=250)
    h["tickers"]["SGOV"]["shares"] = 6
    text, fires = line(h, "GLDM", "BUY")
    assert fires and "the most cash and SGOV" in text and "target would be" in text


def test_ai_cap_blocks_buys_in_the_ai_group():
    text, fires = line(book(), "NVDA", "BUY")
    assert not fires and "over the 30% cap" in text


def test_sell_over_target_and_sell_under_target():
    a = OR.armed(book(), "GOOGL", CLOSES, USDSGD, AI)
    text, fires = line(book(), "GOOGL", "SELL")
    assert fires and a["side"] == "SELL" and text.startswith(f"SELL {a['shares']} shares")
    text, fires = line(book(), "GLDM", "SELL")
    assert not fires and "under target" in text and "pause" in text.lower()


def test_paused_adds_and_within_band_and_iqq_and_stale():
    assert "paused" in line(book(), "GLDM", "BUY", paused="2026-11-03")[0]
    h = book()
    h["tickers"]["GLDM"]["target_pct"] = 1            # a gap of about $106: under the $200 minimum
    assert "within band" in line(h, "GLDM", "BUY")[0]
    assert line(book(), "IQQ", "BUY") == ("IQQ never arms an order", False)
    assert line(book(as_of="2026-09-01"), "GLDM", "BUY") == ("holdings stale, update the secret", False)


def test_old_secret_shape_is_still_understood():
    old = {"ai_group_now_pct": 40.0, "tickers": {"VOO": {"shares": 2, "target": 20, "order": "BUY 3"}}}
    assert OR.shape(old) == "v1" and OR.shape(book()) == "v2"


def test_holdings_never_reach_a_public_file(make_watch, tmp_path):
    h = book(cash_usd=98765)
    h["tickers"]["GLDM"]["shares"] = 4321
    h["tickers"]["QQQ"] = {"shares": 6543, "target_pct": 12, "group": "core"}
    w, m = make_watch(tickers=("QQQ", "GLDM"), holdings=h)
    state = {"sent": [], "pending": {}, "obs": {}}
    m.now = et(2026, 11, 4, 15, 50)
    res, sess, _ = w.poll(state)
    w.decide(res[0], [], state, sess)
    m.now = et(2026, 11, 4, 16, 30)
    msgs = w.settled_pass(state)
    json.dump(state, open(w.STATE, "w"))
    public = ""
    for f in (w.STATE, w.CACHE, w.STATUS):
        if os.path.exists(f):
            public += open(f).read()
    for secret in ("98765", "4321", "6543"):
        assert secret not in public, f"{secret} leaked into a published file"
    assert any("Fires:" in t for t, _ in msgs)          # the sizing does reach Telegram
