"""Rule H (2026-11-06): a temporary hedge when a sector you are heavy in crashes.
Made-up holdings only; the repository is public."""
import datetime as dt

import hedge as HG

HOLD = {"as_of": "2026-11-05", "cash_usd": 3000, "sgov_floor_pct": 8, "min_order_usd": 200, "ai_cap_pct": 30,
        "tickers": {"NVDA": {"shares": 20, "target_pct": 15, "group": "ai"},
                    "TSLA": {"shares": 10, "target_pct": 10, "group": "ai"},
                    "VOO": {"shares": 15, "target_pct": 40, "group": "core"},
                    "SGOV": {"shares": 30, "target_pct": 8, "group": "cash"}}}
DATES = [f"2026-09-{d:02d}" for d in range(1, 31)] + [f"2026-10-{d:02d}" for d in range(1, 31)]


def _hist(start, step, wiggle):
    c, x = [], start
    for i in range(len(DATES)):
        x *= 1 + step + (wiggle if i % 2 else -wiggle)
        c.append(x)
    return {"dates": DATES, "c": c}


def _row(t, hist, px, lo, up, sigma, k="WATCH BUY", r="WATCH BUY"):
    c = hist["c"]
    return {"t": t, "px": px, "sess": "REGULAR",
            "d": {"c": c[-3:], "hist": hist, "sigma": sigma},
            "ev": {"px": px, "lo": lo, "up": up, "k": k, "r": r, "t": t}}


def _setup(make_watch):
    w, m = make_watch(tickers=("QQQ", "NVDA", "TSLA", "VOO"), holdings=HOLD)
    w.CTX.update({"today": dt.date(2026, 11, 6), "usdsgd": 1.28, "paused": {},
                  "closes": {"SGOV": 100.5}})
    w._inverse_price = lambda t: {"PSQ": 40.0, "SQQQ": 20.0}.get(t)
    q = _hist(600, 0.001, 0.01)
    rows = [_row("QQQ", q, q["c"][-1] * 0.95, q["c"][-1] * 0.97, q["c"][-1] * 1.05, 0.011),
            _row("NVDA", _hist(180, 0.001, 0.015), 190.0, 0, 0, 0.02),
            _row("TSLA", _hist(400, 0.001, 0.02), 350.0, 0, 0, 0.02),
            _row("VOO", _hist(600, 0.001, 0.008), 600.0, 0, 0, 0.007)]
    return w, rows


def test_beta_reads_the_sensitivity_to_the_proxy():
    p = _hist(100, 0.0, 0.01)
    pc = dict(zip(p["dates"], p["c"]))
    m = {d: 50 * (1 + 2 * (c / pc[p["dates"][0]] - 1)) for d, c in pc.items()}  # roughly 2x the moves
    assert 1.8 < HG.beta(m, pc) < 2.2


def test_crash_needs_a_big_fall_and_a_break_below_the_range():
    cfg = {"z": 2.5, "dd20_pct": 10}
    assert HG.crash(95, 100, 0.011, [100] * 20, 97, cfg)[0] is True          # -5 % = 4.5x usual
    assert HG.crash(95, 100, 0.011, [100] * 20, 94, cfg)[0] is False         # still inside its range
    assert HG.crash(99, 100, 0.011, [100] * 20, 99.5, cfg)[0] is False       # a normal day


def test_a_crash_on_a_heavy_group_suggests_a_half_hedge_once_then_exits(make_watch):
    w, rows = _setup(make_watch)
    state = {"sent": [], "pending": {}, "obs": {}}
    msgs = w.hedge_messages(rows, state, "REGULAR")
    assert len(msgs) == 1
    m = msgs[0]
    assert "HEDGE CHECK — AI group" in m and "of PSQ" in m and "of SQQQ" in m
    assert "Remove it when" in m and "Nothing has been traded" in m
    expo = float(m.split("= $")[1].split(" ")[0].replace(",", ""))
    psq = float(m.split("Buy ≈$")[1].split(" ")[0].replace(",", ""))
    assert abs(psq - 0.5 * expo) <= 1                                        # half the beta-weighted exposure
    assert state["hedge"]["AI group"]["on"] == "2026-11-06"
    assert w.hedge_messages(rows, state, "REGULAR") == []                    # not repeated
    # The next session QQQ recovers above the middle of its range: one HEDGE OFF message.
    w.CTX["today"] = dt.date(2026, 11, 9)
    q = rows[0]
    q["px"] = (q["ev"]["lo"] + q["ev"]["up"]) / 2 * 1.01
    q["ev"]["k"] = q["ev"]["r"] = "NONE"
    msgs = w.hedge_messages(rows, state, "REGULAR")
    assert len(msgs) == 1 and "HEDGE OFF" in msgs[0] and "middle of its normal range" in msgs[0]
    assert "AI group" not in state["hedge"]


def test_no_hedge_when_the_group_is_small_or_holdings_are_missing(make_watch):
    w, rows = _setup(make_watch)
    w.CFG["hedge"]["groups"][0]["min_weight_pct"] = 90
    assert w.hedge_messages(rows, {"sent": []}, "REGULAR") == []
    w.H = None
    w.CFG["hedge"]["groups"][0]["min_weight_pct"] = 25
    assert w.hedge_messages(rows, {"sent": []}, "REGULAR") == []


def test_the_3x_time_limit_is_flagged_once_and_the_1x_hedge_stays(make_watch):
    w, rows = _setup(make_watch)
    state = {"sent": []}
    w.hedge_messages(rows, state, "REGULAR")
    q = rows[0]
    q["px"] *= 1.01                                                          # still low, no exit yet
    for i, day in enumerate((9, 10, 11, 12, 13)):
        w.CTX["today"] = dt.date(2026, 11, day)
        msgs = w.hedge_messages(rows, state, "REGULAR")
        if i < 4:
            assert msgs == [], (day, msgs)
    assert len(msgs) == 1 and "3x time limit" in msgs[0]
    assert "AI group" in state["hedge"]
    w.CTX["today"] = dt.date(2026, 11, 16)
    assert w.hedge_messages(rows, state, "REGULAR") == []
