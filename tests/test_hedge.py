"""Rule H, split hedge (2026-11-06): a temporary hedge when a sector you are heavy in crashes, each
stock hedged against the index it follows most closely. Made-up holdings only; the repo is public."""
import datetime as dt

import hedge as HG

HOLD = {"as_of": "2026-11-05", "cash_usd": 3000, "sgov_floor_pct": 8, "min_order_usd": 200, "ai_cap_pct": 30,
        "tickers": {"NVDA": {"shares": 20, "target_pct": 15, "group": "ai"},
                    "TSLA": {"shares": 10, "target_pct": 10, "group": "ai"},
                    "VOO": {"shares": 15, "target_pct": 40, "group": "core"},
                    "SGOV": {"shares": 30, "target_pct": 8, "group": "cash"}}}
DATES = [f"2026-09-{d:02d}" for d in range(1, 31)] + [f"2026-10-{d:02d}" for d in range(1, 31)]
QR = [0.012 if i % 2 else -0.010 for i in range(len(DATES))]          # QQQ daily moves
SR = [0.015 if i % 3 == 0 else -0.007 for i in range(len(DATES))]     # SOXX: a different pattern


def _series(start, rets):
    c, x = [], start
    for r in rets:
        x *= 1 + r
        c.append(x)
    return {"dates": DATES, "c": c}


def _row(t, hist, px, lo=0, up=0, sigma=0.02, k="WATCH BUY", r="WATCH BUY"):
    return {"t": t, "px": px, "sess": "REGULAR", "d": {"c": hist["c"][-3:], "hist": hist, "sigma": sigma},
            "ev": {"px": px, "lo": lo, "up": up, "k": k, "r": r, "t": t}}


def _crashing(t, hist, drop=0.95, sigma=0.011):
    last = hist["c"][-1]
    return _row(t, hist, last * drop, lo=last * 0.97, up=last * 1.05, sigma=sigma)


def _setup(make_watch, soxx_crash=True, qqq_crash=True):
    w, m = make_watch(tickers=("QQQ", "SOXX", "NVDA", "TSLA", "VOO"), holdings=HOLD)
    w.CTX.update({"today": dt.date(2026, 11, 6), "usdsgd": 1.28, "paused": {}, "closes": {"SGOV": 100.5}})
    w._inverse_price = lambda t: {"PSQ": 40.0, "SOXS": 10.0}.get(t)
    q, s = _series(600, QR), _series(150, SR)
    rows = [_crashing("QQQ", q) if qqq_crash else _row("QQQ", q, q["c"][-1], q["c"][-1] * 0.9, q["c"][-1] * 1.1),
            _crashing("SOXX", s, sigma=0.015) if soxx_crash else _row("SOXX", s, s["c"][-1], s["c"][-1] * 0.9, s["c"][-1] * 1.1),
            _row("NVDA", _series(180, [1.5 * x for x in SR]), 190.0),       # follows chips
            _row("TSLA", _series(400, [2.0 * x for x in QR]), 350.0),       # follows the Nasdaq-100
            _row("VOO", _series(600, [0.8 * x for x in QR]), 600.0)]
    return w, rows


def test_r2_assigns_each_stock_to_the_index_it_follows():
    q, s = _series(600, QR), _series(150, SR)
    cd = lambda h: dict(zip(h["dates"], h["c"]))
    home = HG.assign({"NVDA": cd(_series(1, [1.5 * x for x in SR])), "TSLA": cd(_series(1, [2 * x for x in QR]))},
                     {"QQQ": cd(q), "SOXX": cd(s)}, "QQQ")
    assert home == {"NVDA": "SOXX", "TSLA": "QQQ"}


def test_crash_needs_a_big_fall_and_a_break_below_the_range():
    cfg = {"z": 2.5, "dd20_pct": 10}
    assert HG.crash(95, 100, 0.011, [100] * 20, 97, cfg)[0] is True
    assert HG.crash(95, 100, 0.011, [100] * 20, 94, cfg)[0] is False
    assert HG.crash(99, 100, 0.011, [100] * 20, 99.5, cfg)[0] is False


def test_both_indexes_crash_one_message_with_a_soxs_leg_and_a_psq_leg(make_watch):
    w, rows = _setup(make_watch)
    state = {"sent": []}
    msgs = w.hedge_messages(rows, state, "REGULAR")
    assert len(msgs) == 1
    m = msgs[0]
    soxs = m.split("Buy ≈$")[1] if "of SOXS" in m.split("Buy ≈$")[1] else m.split("Buy ≈$")[2]
    psq = m.split("Buy ≈$")[1] if "of PSQ" in m.split("Buy ≈$")[1] else m.split("Buy ≈$")[2]
    assert "covers NVDA" in soxs and "SOXX-equivalent" in soxs and "(3x)" in soxs and "5 sessions" in soxs
    assert "covers TSLA" in psq and "QQQ-equivalent" in psq and "20 sessions" in psq
    expo = float(soxs.split("≈$")[1].split(" ")[0].replace(",", ""))
    amount = float(soxs.split(" ")[0].replace(",", ""))
    assert abs(amount - 0.5 * expo / 3) <= 1                                 # half the exposure, 3x fund
    assert set(state["hedge"]["AI group"]["legs"]) == {"QQQ", "SOXX"}
    assert w.hedge_messages(rows, state, "REGULAR") == []                    # not repeated


def test_a_chip_only_crash_opens_only_the_soxs_leg(make_watch):
    w, rows = _setup(make_watch, qqq_crash=False)
    state = {"sent": []}
    m = w.hedge_messages(rows, state, "REGULAR")[0]
    assert "of SOXS" in m and "of PSQ" not in m
    assert set(state["hedge"]["AI group"]["legs"]) == {"SOXX"}


def test_each_leg_exits_on_its_own_and_the_3x_leg_after_5_sessions(make_watch):
    w, rows = _setup(make_watch)
    state = {"sent": []}
    w.hedge_messages(rows, state, "REGULAR")
    msgs = []
    for day in (9, 10, 11, 12, 13):
        w.CTX["today"] = dt.date(2026, 11, day)
        msgs += w.hedge_messages(rows, state, "REGULAR")
    assert len(msgs) == 1 and "HEDGE OFF" in msgs[0] and "SOXS" in msgs[0] and "5-session limit" in msgs[0]
    assert "PSQ hedge stays" in msgs[0]
    assert set(state["hedge"]["AI group"]["legs"]) == {"QQQ"}
    q = rows[0]                                                               # QQQ recovers above mid-range
    q["px"] = (q["ev"]["lo"] + q["ev"]["up"]) / 2 * 1.01
    q["ev"]["k"] = q["ev"]["r"] = "NONE"
    w.CTX["today"] = dt.date(2026, 11, 16)
    msgs = w.hedge_messages(rows, state, "REGULAR")
    assert len(msgs) == 1 and "PSQ" in msgs[0] and "middle of its normal range" in msgs[0]
    assert state["hedge"]["AI group"]["legs"] == {}        # SOXX still crashing, but its leg timed out: no re-open


def test_a_hedge_stored_before_the_split_becomes_its_qqq_leg(make_watch):
    w, rows = _setup(make_watch, qqq_crash=False, soxx_crash=False)
    old = {"AI group": {"on": "2026-11-06", "days": ["2026-11-06"], "proxy_px": 655.78, "rebound_pct": 5,
                        "max_sessions": 20, "max_sessions_3x": 5}}
    state = {"sent": [], "hedge": old}
    w.hedge_messages(rows, state, "REGULAR")
    leg = state["hedge"]["AI group"]["legs"]["QQQ"]
    assert leg["fund"] == "PSQ" and leg["proxy_px"] == 655.78


def test_no_hedge_when_the_group_is_small_or_holdings_are_missing(make_watch):
    w, rows = _setup(make_watch)
    w.CFG["hedge"]["groups"][0]["min_weight_pct"] = 90
    assert w.hedge_messages(rows, {"sent": []}, "REGULAR") == []
    w.H = None
    w.CFG["hedge"]["groups"][0]["min_weight_pct"] = 25
    assert w.hedge_messages(rows, {"sent": []}, "REGULAR") == []
