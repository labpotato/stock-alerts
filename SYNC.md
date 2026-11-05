# Conventions shared with the post-close run

The GitHub alerter (`watch.py`) and the Claude post-close run must give the same answer on the
same settled data. Everything below is shared. **Change it in both systems or not at all.** The
code for each rule lives in `signals.py` (maths) and `orders.py` (order gate and sizing). The
tests in `tests/` check the rules on real bars.

## 1. Settled daily data

| Rule | Value |
|---|---|
| Source | Yahoo chart API, `range=1y&interval=1d` |
| Window | the last **220** settled bars after hygiene |
| Close | the official regular-session close (Yahoo `regularMarketPrice` once the session has ended), never an after-hours price |
| Drop a row if | `volume == 0`, or `open == high == low == close` |
| Keep a row if | only `open == high` (it is a real bar; thin ETFs have many) |
| Partial tape | drop the newest row if its volume is under **30 %** of the prior session's |
| Missing volume | unknown, not zero: the row is kept |
| Currencies | no volume on Yahoo, so the two volume rules are off |
| `ES3.SI` | flat bars kept (thin SGX listing) |
| `.KS` / `.SI` | a bar dated today Singapore time is not settled |
| Reversed currency pairs | `SGDUSD = 1 / USDSGD`, `KRWSGD = 1 / SGDKRW`, high and low swapped. Yahoo quotes `KRWSGD=X` rounded to 0.0010, which is unusable |
| Currency settled day | the last completed bar of Yahoo's currency day (London time) |

On 2026-11-04 the four test tickers had 20 real bars with `open == high` (BOTZ alone had 12).
The old post-close rule would have deleted them; this rule keeps them.

## 2. Formulas

- **EMA20** of close, seeded at the first close of the window: `e[0] = c[0]`, `e = c*2/21 + e*(19/21)`.
- **ATR14**, Wilder: true range `TR[0] = H - L`, then `max(H-L, |H-Cprev|, |L-Cprev|)`. Seed = mean of
  the first 14 true ranges; then `ATR = (13*ATR + TR) / 14`.
- **Bands** = EMA20 ± 2 × ATR14. **MA3** = mean of the last 3 closes.
- **RSI14**, Wilder on closes: seed = mean of the first 14 gains and losses; then `(13*avg + x) / 14`.

## 3. Signals

| Leg | BUY | SELL | WATCH BUY | WATCH SELL |
|---|---|---|---|---|
| Keltner | prior MA3 ≤ prior lower AND MA3 > lower | prior close > prior upper AND close ≤ upper | close < lower | close > upper |
| RSI | prior < 30 AND current ≥ 30 | prior > 70 AND current ≤ 70 | < 30 | > 70 |

- **STRONG** = both legs fired the same side within the last 3 sessions, at least one today.
- **CONFLICT** = opposite legs today. It fires nothing.

## 4. Deciding margin and knife-edge

- Keltner BUY margin `(MA3 − lower) / lower`; Keltner SELL margin `(upper − close) / upper`.
- RSI margin `|RSI − 30|` or `|RSI − 70|`, in points, for the side in play.
- A rule "prior condition AND current condition" is a **KNIFE-EDGE** when one of the two is within the
  threshold of flipping while the other holds. Thresholds: **0.05 %** (Keltner), **0.3 points** (RSI).
  This catches both QQQ on 4 Nov (current MA3 0.0001 % below the band, no BUY) and BOTZ on 4 Nov
  (BUY, but the 3 Nov prior condition held by only 0.02 %).

## 5. Order gate and sizing

- **BUY** fires only if the ticker is under target and, for AI-group names, the AI group is at or under
  **30 %** of the book. **SELL** fires only if over target.
- A SELL on an under-target name fires nothing and **pauses adds** until both legs are back to NONE.
- Orders under **$200** are "within band". **IQQ never arms an order.**
- Target $ = target % × total assets at settled closes. Total assets = USD holdings + cash + ES3
  (SGD) converted at the USD→SGD settled close. Gap = target $ − value; shares = round(|gap| / close).
- BUY funding: cash first, then SGOV down to its **8 %** floor; if still short, the largest fundable size.
- The AI-group share is computed from the holdings secret at current prices, never stored.
- Holdings `as_of` older than **30 days**: every Fires line says "holdings stale, update the secret".
- Only a signal **at the close** sizes or fires an order.

AI group: `GOOGL, NVDA, SOXX, TSLA, PLTR, BE, AIPO, BOTZ, IQQ` (`config.json` → `ai_group`).

## 6. Where the two systems are allowed to differ

The alerter also sends **REAL-TIME** alerts between closes. These follow rules the post-close run
does not, by Dongmin's choice on 2026-11-05:

| Real-time rule | Effect |
|---|---|
| A signal is a cross between two consecutive real-time readings (~10 minutes apart) | A cross that reverses before the close (QQQ, 4 Nov) alerts in real time but not at the close |
| The last reading, including after hours, carries over to the next morning | An after-hours move can pre-empt the next day's cross (BOTZ, 3-4 Nov) |
| Pre- and post-market highs and lows count in the live bar | The live band can be wider than the settled band |

These differences are expected, and each one is labelled. The **evening check** (settled pass) runs
after the official close using settled bars only, so it matches the post-close run exactly. It tells
Dongmin whether each real-time alert was **CONFIRMED at close** or **FADED by close**, and it reports
any **CLOSE SIGNAL (not seen intraday)**.

## 7. The settled block (`docs/status.json`, `"schema": 2`)

Written once per trading day by the evening check. The post-close run reads it and reports every
ticker where the two systems disagree, with the reason. Field names are stable:

```
"settled": {
  "date": "YYYY-MM-DD",
  "tickers": {
    "QQQ": {"bar_date", "close", "ma3", "upper", "lower", "rsi",
            "k_state", "r_state", "signal", "margin_k_pct", "margin_r_pts", "knife_edge",
            "label", "kind"}
  }
},
"intraday_sent": ["QQQ:BUY:k", ...]
```

`intraday_sent` keys are `TICKER:SIDE:leg`, with `k` = Keltner and `r` = RSI. The block includes the
currency pairs. It never holds share counts, cash, targets or order sizes; held-but-unwatched tickers
(SGOV, ES3) are left out of it.
