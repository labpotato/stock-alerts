"""Order gate and sizing from the HOLDINGS_JSON secret. Pure functions, no network.

Nothing computed here is ever written to a file: the repository is public. Results go to
Telegram only.

Secret shape (v2), documented with zeros in holdings.example.json:
  as_of, cash_usd, sgov_floor_pct (8), min_order_usd (200), ai_cap_pct (30),
  tickers: {T: {shares, target_pct, group}}   group in core / ai / diversifier / cash / sgd
The older shape (per ticker 'order' text plus 'ai_group_now_pct') is still understood, so the
alerts keep working until the secret is updated.
"""
import datetime as dt
import math

STALE_DAYS = 30


def shape(h):
    if not h or "tickers" not in h:
        return None
    return "v2" if any("target_pct" in v for v in h["tickers"].values()) else "v1"


def stale(h, today):
    try:
        return (today - dt.date.fromisoformat(h["as_of"])).days > STALE_DAYS
    except Exception:
        return True          # no usable date: treat as stale rather than size from it


def is_sgd(t, info):
    return (info or {}).get("group") == "sgd" or t.endswith(".SI")


def book(h, closes, usdsgd, ai_group):
    """Values in USD at the given closes. usdsgd = SGD per 1 USD.
    Returns total, per-ticker values, AI-group share in %, and tickers that could not be priced."""
    values, missing = {}, []
    for t, info in h["tickers"].items():
        sh = info.get("shares") or 0
        if not sh:
            continue
        px = closes.get(t)
        if px is None:
            missing.append(t)
            continue
        v = sh * px
        if is_sgd(t, info):
            if not usdsgd:
                missing.append(t)
                continue
            v = v / usdsgd
        values[t] = v
    total = sum(values.values()) + (h.get("cash_usd") or 0)
    ai = sum(v for t, v in values.items() if t in ai_group)
    return {"total": total, "values": values, "ai_pct": (ai / total * 100) if total else None,
            "missing": missing}


def armed(h, t, closes, usdsgd, ai_group):
    """The one order a matching signal would fire: target $ = target % x total assets,
    gap = target $ - value, shares = round(|gap| / close). Under the minimum -> within band."""
    info = h["tickers"].get(t) or {}
    b = book(h, closes, usdsgd, ai_group)
    px = closes.get(t)
    if px is None or not b["total"]:
        return {"ok": False, "reason": "no price for sizing"}
    tgt = info.get("target_pct")
    if tgt is None:
        return {"ok": False, "reason": "no target"}
    unit = px / usdsgd if is_sgd(t, info) and usdsgd else px
    value = b["values"].get(t, 0.0)
    gap = tgt / 100 * b["total"] - value
    n = round(abs(gap) / unit)
    cost = n * unit
    side = "BUY" if gap > 0 else "SELL"
    within = cost < (h.get("min_order_usd") or 200) or n == 0
    return {"ok": True, "side": side, "shares": n, "cost": cost, "gap": gap, "unit": unit,
            "within_band": within, "book": b}


def funding(h, cost, b, closes, sgov="SGOV"):
    """Cash first, then SGOV down to its floor. Returns (text, fundable_cost)."""
    cash = h.get("cash_usd") or 0
    if cost <= cash:
        return "paid from cash", cost
    short = cost - cash
    floor = (h.get("sgov_floor_pct") or 8) / 100 * b["total"]
    sg_val = b["values"].get(sgov, 0.0)
    avail = max(0.0, sg_val - floor)
    sg_px = closes.get(sgov)
    if short <= avail and sg_px:
        n = math.ceil(short / sg_px)
        return f"${cash:,.0f} cash + sell {n} SGOV (≈${n*sg_px:,.0f})", cost
    return None, cash + avail


def fires_line(h, t, side, closes, usdsgd, ai_group, today, paused=None, never_arm=()):
    """Plain-language 'Fires:' line for a settled (close-confirmed) signal. Telegram only."""
    if shape(h) is None:
        return "holdings not loaded, so I can't size this", False
    if stale(h, today):
        return "holdings stale, update the secret", False
    if t in never_arm:
        return f"{t} never arms an order", False
    info = h["tickers"].get(t)
    if not info or (not info.get("shares") and not info.get("target_pct")):
        return "not held and not in the plan, information only", False
    if not info.get("target_pct"):
        return "held without a target, so it never arms an order", False
    a = armed(h, t, closes, usdsgd, ai_group)
    if not a["ok"]:
        return a["reason"], False
    b = a["book"]
    if side == "BUY":
        if a["side"] != "BUY":
            return "nothing: already at or above target", False
        if paused:
            return f"nothing: adds are paused since {paused} (a sell signal on an under-target name)", False
        cap = h.get("ai_cap_pct") or 30
        if t in ai_group and b["ai_pct"] is not None and b["ai_pct"] > cap:
            return f"nothing: the AI group is {b['ai_pct']:.1f}% of the book, over the {cap}% cap", False
        if a["within_band"]:
            return f"nothing: within band (the gap is under ${h.get('min_order_usd') or 200})", False
        how, fundable = funding(h, a["cost"], b, closes)
        if how:
            return f"BUY {a['shares']} shares (≈${a['cost']:,.0f}), {how}", True
        n = int(fundable // a["unit"])
        if n <= 0 or n * a["unit"] < (h.get("min_order_usd") or 200):
            return "nothing: no cash or SGOV above its floor to pay for it", False
        return (f"BUY {n} shares (≈${n*a['unit']:,.0f}), the most cash and SGOV above its "
                f"{h.get('sgov_floor_pct') or 8}% floor can fund (target would be {a['shares']})"), True
    if a["side"] != "SELL":
        return "nothing: under target, so it doesn't sell. Adds pause until the state returns to NONE", False
    if a["within_band"]:
        return f"nothing: within band (the gap is under ${h.get('min_order_usd') or 200})", False
    return f"SELL {a['shares']} shares (≈${a['cost']:,.0f})", True


def gate_text(h, t, side, closes, usdsgd, ai_group, today, paused=None, never_arm=()):
    """For REAL-TIME alerts: whether a matching close would fire an order, without sizing it."""
    line, fires = fires_line(h, t, side, closes, usdsgd, ai_group, today, paused, never_arm)
    if fires:
        return f"would fire your {side} if it holds to the close; your post-close email sizes it"
    return line
