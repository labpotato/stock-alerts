# stock-alerts

A GitHub Actions job that watches Dongmin's tickers and sends Telegram alerts. It trades nothing.

Rules are **Keltner on MA3** and **RSI 30/70 recross**, applied to **real-time prices**. The bands,
MA3 and RSI are recalculated at the live price on every reading, and each reading asks: *if today
closed at this price, would the daily rule fire against yesterday's settled close?* Pre-market and
after-hours prices count the same way. A Keltner BUY also needs the 3-day average to be higher than at
the last close, so a band that falls under a falling price is not a BUY (rule added 2026-11-05).

`config.json` → `"cross_reference"`: `"prev_close"` (the default since 2026-11-05) or `"last_poll"`
(a cross between two consecutive readings, which whipsawed on volatile days).

## What it sends

Every message is about **one ticker** (or one group finding) and comes with a chart.

| When | What |
|---|---|
| A measure gives a BUY or SELL at the live price | One message: ticker, full name, price at that moment, which of the two measures agree (e.g. SELL + SELL → STRONG SELL), the reasons in plain words, what your plan says, and a chart |
| A ticker moves far more than it usually does | One "unusual move" message with a chart. On a day the market itself moves unusually, one "whole market" message instead, and a ticker only if it moves unusually *compared with the market* |
| Your AI stocks split from the S&P 500, or most of the list falls together | One group message with a bar chart |

**Rule H, split hedge check (2026-11-06).** When a group you are heavy in (the AI group, at least 25%
of your money) crashes, one 🛡️ message suggests a temporary hedge of half its beta-weighted exposure,
each stock against the index it follows most closely (highest R² of daily moves): chip-like names
(e.g. NVDA, BE, AIPO, SOXX) against SOXX with **SOXS** (-3x, 5 sessions at most), the rest (GOOGL, TSLA,
PLTR, BOTZ, IQQ) against QQQ with **PSQ** (-1x, up to 20 sessions). A leg opens when its own index is
down at least 2.5x its usual daily move or 10% below its 20-day high, and below the bottom of its range,
so a chip-only crash opens only the SOXS leg. Each leg gets its own 🛡️ HEDGE OFF message at the first
of: a Rule A BUY on its index, the index back above the middle of its range, +5% from the start, or its
time limit; a leg that times out is not reopened until that crash is over. Sized from the holdings
secret, Telegram only; settings in `config.json` → `hedge`. It never trades.

The full list is **not** sent automatically. Run the workflow with `digest` if you want it once.

**No repeats.** Each measure can give a BUY or SELL for a ticker once per trading day. If both give
it at the same moment you get one STRONG message; if the second agrees later the same day you get one
short "Now STRONG" line (no second chart), and then nothing more for that ticker and side until the next trading day.

## Holdings: one shared private file (2026-11-06)

Both the alerter and the Claude post-close run read **`holdings.json` in the private repo
`labpotato/portfolio-private`** (same shape as `holdings.example.json`). The post-close run keeps it in
step with its holdings doc, so a trade reported there reaches the alerter and the Telegram bot at its
next run. The alerter reads it with the read-only `HOLDINGS_TOKEN` secret and uses the newer (by
`as_of`) of that file and the old `HOLDINGS_JSON` secret, which stays as a fallback. Nothing from it is
ever printed, committed or published here.

## Dashboard (2026-11-10)

**https://labpotato.github.io/stock-alerts/** is a terminal-style page (`docs/index.html`): one card per
ticker with the live price, a 60-session chart with its normal range, the momentum gauge and what the
rule says; currencies; today's alerts; unusual moves; the latest news flashes (from the Worker's
`/news`); and the backtest summary (`docs/backtest.json`, produced by `tools/backtest.py`). It reads
`docs/board.json`, which every alerter run refreshes, and reloads itself every minute. Public prices
and signal states only: no holdings.

## News flashes (2026-11-10)

The Cloudflare Worker (`bot/src/news.js`) checks free feeds **every minute**: Yahoo Finance and Google
News for the single stocks, a macro query (Fed, CPI, jobs, tariffs), Korean news for Samsung
Electronics, SK hynix and the won, the SEC's live 8-K feed for NVDA, GOOGL, TSLA, PLTR and BE, and the
Federal Reserve's press releases. A rule filter keeps only headlines from known sources that read
like events (earnings, guidance, deals, lawsuits, bans, rate decisions...). Claude then writes one
line on what it means and rates it 1-5; 3 and above is sent to Telegram with what your rule currently
says about the ticker. Each headline is handled once (memory in a Workers KV namespace the deploy
workflow creates). At most 40 headlines a day go to Claude (`NEWS_MAX_PER_DAY`), which caps the cost;
`NEWS_MODEL` picks the model and `NEWS = "off"` in `wrangler.toml` turns the feature off.

Google News refuses requests from Cloudflare Workers (HTTP 503), so every Google search has a Bing
News twin; whichever answers is used. Seeking Alpha and Investopedia headlines are read too; flashes from these (and from Motley Fool,
Zacks, Benzinga) are marked "(less reliable source)".

## Market mood (2026-11-10)

Twice a day (9:30 pm Singapore, an hour before the US open, and 9 am Singapore, after the close) the
Worker reads what is "hot" on Reddit (r/wallstreetbets, r/stocks, r/investing), Seeking Alpha,
Investopedia and the press, and Claude writes a short brief: the general mood on a fear-to-greed scale,
what people are talking about, which names keep coming up, what is probably noise, and an honest view.
Send `/mood` to the bot for one on demand. This is crowd talk, not evidence: it is kept apart from the
rule signals and never changes them. `GET /feeds` on the Worker shows which sources answered.

## Two kinds of signal

| | REAL-TIME alert | Evening check |
|---|---|---|
| When | every ~10 minutes: pre-market, market hours, after hours | once per trading day, after 16:15 New York time |
| Prices | live, including pre- and post-market | settled daily bars and the official close only |
| A signal is | a cross from yesterday's close to the live price | a cross from yesterday's close to today's close |
| Label | "REAL-TIME, pre-market / market open / after hours" | ✅ CONFIRMED at close · ⚠️ FADED by close · 🔔 CLOSE SIGNAL (not seen intraday) · ⚖️ CONFLICT at close |
| Orders | says whether a matching close *would* fire your order | the only place an order is sized: "Fires: BUY 12 shares (≈$960), paid from cash" |

**Since 2026-11-06 the evening check sends no Telegram messages** (`config.json` → `evening_messages: false`): it still runs and writes the settled block in `status.json` for the post-close run's cross-check.

The evening check uses exactly the same data and rules as the Claude post-close run (see `SYNC.md`),
so the two agree. It tells you whether each real-time alert held to the close, and it catches crosses
the real-time readings missed. On 4 Nov, for example, QQQ crossed during the day and faded, and BOTZ
crossed only on the close.

**KNIFE-EDGE.** If a signal was decided by less than 0.05 % of the band (price range) or 0.3 points
(momentum), the message and `status.json` say so: a tiny move would have flipped it.

## Currencies

USD→SGD, SGD→USD, SGD→KRW and KRW→SGD follow the same two measures and the same no-repeat rule.
They are checked whenever currency markets are open (Sunday evening to Friday evening, UTC), including
while the US stock market is shut. Prices read as "1 USD = 1.3121 SGD", and the plan line says what it
means in practice, e.g. "a good moment to sell USD for SGD".

Mirror pairs move as opposites, so a SELL on USD→SGD usually arrives with a BUY on SGD→USD at the same
time. They can occasionally differ at the margin, because the averages of a rate and of its reciprocal
are not exact mirrors. Unusual-move alerts for currencies use a 1% floor instead of 4%, because
currencies move far less than stocks.

## Staying on around the clock

GitHub's own schedule is best-effort and in practice dropped most runs (7 in two days instead of
about 300). So the bot's Cloudflare Worker has a timer that starts the alerter every 10 minutes
through GitHub's on-demand trigger, which is reliable. It needs one more repository secret,
`GH_DISPATCH_TOKEN`: a fine-grained GitHub token for this repository only, with "Actions: Read and
write". The GitHub schedule stays as a fallback; extra runs are harmless because alerts never repeat.

US stocks are checked in their sessions (pre-market and after hours included), the Korean listings
(Samsung Electronics, SK hynix) in KRX hours, and the currency pairs whenever currency markets are
open. The Korean listings are information only: they alert, but never arm an order.

## Coverage

Runs every 10 minutes, around the clock. The job asks Yahoo which session is live and exits after a
single request when US markets are shut, so a closed-market run costs almost nothing. Pre-market
(04:00 ET) and after-hours (to 20:00 ET) are covered, because those are tradeable.

Liquid names alert on the first reading that crosses, in every session. For the thin ETFs listed under
`"thin"` in `config.json`, an extended-hours cross is sent only if the next reading is still on the far
side of the line, so one odd print cannot trigger an alert; an implausible print is discarded outright.
Each ticker alerts at most once per side per day.

Readings are about 10 minutes apart (GitHub's schedule can slip to 15–20), so a cross that happens and
reverses between two readings is not seen.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `TG_TOKEN` | BotFather token |
| `TG_CHAT` | Telegram chat id |
| `HOLDINGS_JSON` | the shape in `holdings.example.json` (zeros only), with your real numbers |

`HOLDINGS_JSON` shape: `as_of` (date the shares were last checked), `cash_usd`, `sgov_floor_pct` (8),
`min_order_usd` (200), `ai_cap_pct` (30), and per ticker `shares`, `target_pct` (% of total assets),
`group` (core / ai / diversifier / cash / sgd). The evening check sizes each order from it, and
computes the AI-group share live, so no number needs updating by hand. If `as_of` is more than 30 days
old, every Fires line says "holdings stale, update the secret". The older shape (an `order` text per
ticker) still works until you switch.

**Share counts, targets and order sizes live only in `HOLDINGS_JSON`.** This repository is public, so
they must never be committed. `config.json` holds only the ticker list and thresholds, and the
published dashboard deliberately omits position sizes. Without the secret the job still runs and
still sends signals; it just says the holdings gate is unavailable instead of "Fires: SELL 7".

Also set **Settings → Actions → General → Workflow permissions → Read and write**, so the job can
save `state.json`, `bars_cache.json` and `docs/status.json`.

## Dashboard and machine-readable feed

Enable **Settings → Pages → Source: Deploy from a branch → `main` / `/docs`**. That publishes:

- `https://labpotato.github.io/stock-alerts/` — the visual watchlist, grouped by state
- `https://labpotato.github.io/stock-alerts/status.json` — the same data as JSON

`status.json` is the handoff to the scheduled Claude chats. Both the post-close and the open+1h runs
can read it with a single WebFetch, at no credit cost, instead of recomputing states or holding a
stale copy. It carries `generated_utc`, `session` and `settled_close_date`, so a reader can always
tell how fresh it is.

## Anomaly thresholds

Volatility per ticker is estimated with a **median absolute deviation**, not a standard deviation.
A standard deviation is inflated by the very outlier it is meant to detect: on 2026-11-02 GOOGL's
-12.6% day scored only -4.2 sigma on stdev but -7.9 sigma on MAD. Thresholds in `config.json` were
calibrated against 80 sessions of real data for these 16 names, and fire roughly **once a week** for
single-day moves and **once a week** for three-session slides. Several names sliding together are
collapsed into one message rather than one alert each.

## The settled block for the post-close run

`docs/status.json` (`"schema": 2`) carries a `settled` block written by the evening check: per ticker
the close, MA3, bands, RSI, both leg states, the combined signal, the deciding margins and the
knife-edge flag, plus `intraday_sent`, the real-time alerts sent that day. The post-close run reads it
and reports any ticker where the two systems disagree. Field names are listed in `SYNC.md` and are
kept stable.

## Ask Claude in Telegram

Message the bot a question ("why did XLV get a SELL?", "news on GOOGL?", "how close is GLDM to a buy?")
and Claude (Opus 5.5) answers in the same chat, using the alerter's live `status.json`, your holdings
secret and, when it helps, a web search. Reply to an alert to ask about that alert. It answers only
your chat, can't trade or change anything, and costs a few US cents per question on your Anthropic
API account (billed separately from a Claude subscription).

It runs as a Cloudflare Worker (`bot/`), deployed by `.github/workflows/bot-deploy.yml`, which copies
the bot's secrets from this repository's secrets. Needed: `CLOUDFLARE_API_TOKEN`,
`CLOUDFLARE_ACCOUNT_ID`, `ANTHROPIC_API_KEY` (plus the existing `TG_TOKEN`, `TG_CHAT`,
`HOLDINGS_JSON`). After changing any of them, re-run that workflow. The free Cloudflare plan allows
about 25 seconds per answer; a question that needs longer says so instead of going silent.

## Testing and upkeep

- `python -m pytest -q tests` runs on every PR and before every scheduled run; a failing test stops
  the run rather than send wrong alerts. Tests use real Yahoo bars saved in `tests/fixtures` (built
  once by `tools/make_fixtures.py` on GitHub) and never touch the network.

- **Actions → Stock alerts → Run workflow**, `mode: test` sends a test message; `mode: digest`
  sends the watchlist immediately whatever the market is doing.
- Edit `config.json` to change the ticker list or thresholds. Update the `HOLDINGS_JSON` secret
  whenever Plan v2 changes, or the "Fires:" line goes stale.
- If every price fetch fails the job exits non-zero, so GitHub emails you. Silence means no signal,
  which is the normal case most days.
