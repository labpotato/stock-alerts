"""Spec tests 1-4 and 8: the close-to-close state on real Yahoo bars, run through the same
pipeline the evening check uses (fetch -> hygiene -> official close -> 220-bar window)."""
import datetime as dt

import pytest

import signals as SG
from conftest import et, fixture_bars
from reference import bands


def settled(w, m, t, when=None):
    m.now = when or et(2026, 11, 4, 16, 30)
    r = w.settled_rows(t, settled=True)
    return SG.settled_state(r["rows"], r["dates"]), r


def test_1_qqq_settled_4nov_is_not_a_buy_and_is_a_knife_edge(make_watch):
    w, m = make_watch()
    st, r = settled(w, m, "QQQ")
    assert st["bar_date"] == "2026-11-04"
    assert st["ma3"] == pytest.approx(686.107, abs=0.001)
    assert st["lower"] == pytest.approx(686.11, abs=0.005)
    assert st["ma3"] <= st["lower"]                     # missed by a hair
    assert st["k_state"] != "BUY"
    assert st["knife_edge"] is True


def test_2_botz_buy_3nov_to_4nov_on_a_knife_edge(make_watch):
    w, m = make_watch()
    st, r = settled(w, m, "BOTZ")
    p = st["prev"]
    assert p["date"] == "2026-11-03" and st["bar_date"] == "2026-11-04"
    assert p["ma3"] == pytest.approx(32.270, abs=0.0005)
    assert p["lower"] == pytest.approx(32.277, abs=0.0005)
    assert p["ma3"] < p["lower"]
    assert st["ma3"] == pytest.approx(32.467, abs=0.0005)
    assert st["lower"] == pytest.approx(32.193, abs=0.0005)
    assert st["k_state"] == "BUY"
    assert abs(st["prior_k_pct"]) < 0.05                # 3 Nov margin about 0.02 %
    assert st["knife_edge"] is True


def test_3_gldm_settled_4nov_is_a_keltner_buy(make_watch):
    w, m = make_watch()
    st, _ = settled(w, m, "GLDM")
    assert st["bar_date"] == "2026-11-04"
    assert st["k_state"] == "BUY"


def test_4_googl_rsi_buy_on_a_knife_edge(make_watch):
    w, m = make_watch()
    st, _ = settled(w, m, "GOOGL")
    assert st["prev"]["rsi"] < 30
    assert st["rsi"] == pytest.approx(30.2, abs=0.05)
    assert st["r_state"] == "BUY"
    assert st["knife_edge"] is True


@pytest.mark.parametrize("t", ["QQQ", "BOTZ", "GLDM", "GOOGL", "USDSGD=X", "SGDKRW=X"])
def test_8_bands_match_the_independent_reference(t):
    fx = t.endswith("=X")
    kept, _ = SG.hygiene(fixture_bars(t), volume_rule=not fx, partial_tape=not fx)
    kept = [b for b in kept if b["date"] <= dt.date(2026, 11, 4)][-SG.WINDOW:]
    rows = [(b["o"], b["h"], b["l"], b["c"]) for b in kept]
    mine, ref = SG.series(rows), bands(rows)
    checked = 0
    for j, row in enumerate(mine):
        for key in ("up", "lo", "ma3", "rsi"):
            if ref[key][j] is None:
                assert row[key] is None or key in ("up", "lo")   # warm-up bars
                continue
            assert row[key] == pytest.approx(ref[key][j], abs=1e-6), (t, j, key)
            checked += 1
    assert checked > 4 * 200
