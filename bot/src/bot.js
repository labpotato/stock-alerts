// Telegram Q&A bot for Dongmin: forwards his questions to Claude with the alerter's live data.
// Pure logic lives here so it can be tested without the network; index.js is the Worker entry.

export const MODEL = "claude-opus-5-5";
export const TELEGRAM_LIMIT = 4000;           // Telegram's hard cap is 4096 characters
export const TIME_BUDGET_MS = 26000;          // Cloudflare lets waitUntil() run 30 s after replying
export const MAX_CONTINUATIONS = 2;           // pause_turn resumes after server-side web search

// Frozen, so the prompt cache can reuse it. Live data goes in the user turn, never here.
export const SYSTEM = `You are the assistant inside Dongmin's Telegram stock-alert bot. Dongmin is in Singapore and trades US stocks and ETFs through moomoo. He asks short questions on his phone.

How to answer
- Plain words, short. This is a phone chat: a few short paragraphs or simple lines, no tables, no markdown symbols (no **, #, |). Emoji are fine for structure.
- Reply in the language he writes in (English or Korean).
- Lead with the answer, then the one or two reasons that matter.
- Avoid jargon. If you must use a term, say what it means in a few words.

What you know
- Each question comes with a snapshot from his GitHub alerter (status.json): live prices, both signal measures per ticker, distances to the next signal, unusual moves, today's real-time alerts, and the evening check (settled close-to-close states). It also carries his holdings plan when available.
- Always check the snapshot's timestamp ("generated_utc") before talking about prices. Say how old it is if more than about 30 minutes old, or if the market is closed.
- Use web search for news, earnings, macro events or anything after the snapshot. Name the source briefly. Do not search for things the snapshot already answers.

His rules (the same ones both his systems use)
- Two measures per ticker. Price range (Keltner): EMA20 of close plus or minus 2 x ATR14. BUY when the 3-day average price climbs back above the bottom of the range after being at or below it, and the 3-day average is itself higher than at the last close (a range bottom that falls under a falling price is not a BUY); SELL when the close falls back inside after being above the top. Momentum (RSI14): BUY when it climbs back over 30 from below; SELL when it drops back under 70 from above.
- Both measures saying the same side within 3 sessions = STRONG. Opposite sides on the same day = CONFLICT, which fires nothing.
- REAL-TIME alerts (every ~10 minutes, pre-market and after hours included) apply the daily rule at the live price against yesterday's close: 'if it closed here'. They are early warnings. The evening check after the US close (CONFIRMED / FADED / CLOSE SIGNAL) and his post-close email are the official record, and only a signal at the close fires an order.
- KNIFE-EDGE: a signal decided by under 0.05% of the band or 0.3 momentum points; a tiny move would flip it.
- Order gate: a BUY fires only if the ticker is under its target and, for AI-group names (GOOGL, NVDA, SOXX, TSLA, PLTR, BE, AIPO, BOTZ, IQQ), only while the AI group is at or under 30% of the book. A SELL fires only if over target. A SELL on an under-target name fires nothing and pauses adds. Orders under $200 are within band. IQQ never arms an order. Buys are paid from cash first (the cash figure includes his USD money-market fund), then SGOV down to its 8% floor.
- Currency pairs follow the same two measures. Explain them as which money to change into which.

Limits
- You cannot trade, place orders or change anything. Never say or imply that something was bought or sold.
- Never invent a number. If the snapshot or a search does not have it, say so.
- This is information, not financial advice; the decision is his. Do not repeat that line every time, only when he asks what he should do.
- Keep his holdings private: use them to answer, but do not list them in full unless he asks.`;

export function isAllowed(update, env, secretHeader) {
  if (!env.TG_WEBHOOK_SECRET || secretHeader !== env.TG_WEBHOOK_SECRET) return false;
  const msg = update && update.message;
  if (!msg || !msg.chat) return false;
  return String(msg.chat.id) === String(env.TG_CHAT);
}

const r = (x, d = 4) => (typeof x === "number" && isFinite(x) ? Number(x.toPrecision(d + 2)) : x ?? null);

export function compactStatus(s) {
  // Keep only what answers questions; the full file is ~3x larger.
  if (!s || typeof s !== "object") return null;
  const tickers = {};
  for (const [t, v] of Object.entries(s.tickers || {})) {
    const buy = Object.values(v.buy_trigger || {});
    const sell = Object.values(v.sell_trigger || {});
    tickers[v.label || t] = {
      price: r(v.price), day_pct: v.day_pct, rsi: v.rsi,
      price_range: v.keltner, momentum: v.momentum,
      range_low: r(v.lower), range_high: r(v.upper), ma3: r(v.ma3),
      buy_at: buy.length ? r(Math.min(...buy)) : null,
      sell_at: sell.length ? r(Math.max(...sell)) : null,
      nearest_signal_pct: v.nearest_trigger_pct ?? null,
      knife_edge: v.knife_edge ?? null,
    };
  }
  const out = {
    generated_utc: s.generated_utc, session: s.session, settled_close_date: s.settled_close_date,
    tickers, unusual_moves: s.anomalies || [], todays_realtime_alerts: s.intraday_sent || [],
    errors: (s.errors || []).slice(0, 5),
  };
  if (s.settled) {
    const st = {};
    for (const [t, v] of Object.entries(s.settled.tickers || {})) {
      st[v.label || t] = { close: r(v.close), signal: v.signal, price_range: v.k_state, momentum: v.r_state,
                           margin_range_pct: v.margin_k_pct, margin_momentum_pts: v.margin_r_pts,
                           knife_edge: v.knife_edge };
    }
    out.evening_check = { date: s.settled.date, tickers: st };
  }
  return out;
}

function parseHoldings(text) {
  try {
    const h = typeof text === "string" ? JSON.parse(text) : text;
    if (!h || typeof h !== "object" || !h.tickers) return null;
    delete h._note; delete h._shape;
    return h;
  } catch {
    return null;
  }
}

// The newer (by as_of) of the shared holdings.json in the private repo and the HOLDINGS_JSON secret.
export async function holdingsFor(env, doFetch) {
  const secret = env.HOLDINGS_JSON ? parseHoldings(env.HOLDINGS_JSON) : null;
  let shared = null;
  if (env.HOLDINGS_TOKEN && env.HOLDINGS_REPO && doFetch) {
    try {
      const res = await doFetch(`https://api.github.com/repos/${env.HOLDINGS_REPO}/contents/holdings.json`, {
        headers: { authorization: `Bearer ${env.HOLDINGS_TOKEN}`, accept: "application/vnd.github.raw+json",
                   "user-agent": "stock-alerts-bot", "x-github-api-version": "2022-11-28" },
      });
      if (res.ok) shared = parseHoldings(await res.text());
    } catch {
      shared = null;
    }
  }
  if (shared && secret) return String(shared.as_of || "") >= String(secret.as_of || "") ? shared : secret;
  return shared || secret;
}

export function buildUserContent(msg, status, holdings) {
  const parts = [];
  parts.push("Alerter snapshot (status.json, compacted):\n" + JSON.stringify(status ?? { unavailable: true }));
  parts.push("Holdings plan (private):\n" + (holdings ? JSON.stringify(holdings) : "not available"));
  const quoted = msg.reply_to_message && (msg.reply_to_message.text || msg.reply_to_message.caption);
  if (quoted) parts.push("He is replying to this earlier bot message:\n" + quoted.slice(0, 3000));
  parts.push("His question:\n" + (msg.text || msg.caption || "").trim());
  return parts.map((text) => ({ type: "text", text }));
}

export function requestParams(messages) {
  return {
    model: MODEL,
    max_tokens: 2000,
    system: [{ type: "text", text: SYSTEM, cache_control: { type: "ephemeral" } }],
    // Chat is latency-sensitive and must finish inside the platform's time window.
    output_config: { effort: "low" },
    tools: [{ type: "web_search_20260209", name: "web_search", max_uses: 3 }],
    // If a safety classifier declines, the API re-runs the request on a suitable model.
    betas: ["server-side-fallback-2026-07-01"],
    fallbacks: "default",
    messages,
  };
}

export function textOf(response) {
  return (response.content || [])
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("")
    .trim();
}

export function splitMessage(text, limit = TELEGRAM_LIMIT) {
  const out = [];
  let rest = text;
  while (rest.length > limit) {
    let cut = rest.lastIndexOf("\n", limit);
    if (cut < limit * 0.5) cut = rest.lastIndexOf(" ", limit);
    if (cut < limit * 0.5) cut = limit;
    out.push(rest.slice(0, cut).trimEnd());
    rest = rest.slice(cut).trimStart();
  }
  if (rest) out.push(rest);
  return out;
}

export const HELP = `Ask me anything about your stocks, signals or currencies, in English or Korean.

Examples:
• Why did XLV get a SELL?
• How close is GLDM to a buy?
• Is there news on GOOGL today?
• What would fire if QQQ closed at 700?

Tip: reply to one of my alerts to ask about that alert.
I can't trade or change anything; I only answer.`;

/**
 * Answer one message. deps: { client, fetch, now } so tests can run with fakes.
 * Returns the list of Telegram texts it sent.
 */
export async function answer(env, msg, deps) {
  const { client, fetch: doFetch, now = () => Date.now() } = deps;
  const start = now();
  const sent = [];
  const send = async (text) => {
    sent.push(text);
    await doFetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: msg.chat.id, text, reply_parameters: { message_id: msg.message_id },
                             link_preview_options: { is_disabled: true } }),
    });
  };
  const question = (msg.text || msg.caption || "").trim();
  if (!question) return sent;
  if (/^\/(start|help)\b/i.test(question)) {
    await send(HELP);
    return sent;
  }
  doFetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendChatAction`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: msg.chat.id, action: "typing" }),
  }).catch(() => {});

  let status = null;
  try {
    const res = await doFetch(env.STATUS_URL, { cf: { cacheTtl: 60 } });
    if (res.ok) status = compactStatus(await res.json());
  } catch {
    status = null;
  }

  const messages = [{ role: "user", content: buildUserContent(msg, status, await holdingsFor(env, doFetch)) }];
  let response;
  try {
    for (let i = 0; ; i++) {
      const left = TIME_BUDGET_MS - (now() - start);
      if (left < 3000) throw new Error("time budget");
      response = await client.beta.messages.create(requestParams(messages), { timeout: left, maxRetries: 0 });
      if (response.stop_reason !== "pause_turn" || i >= MAX_CONTINUATIONS) break;
      // Web search paused the turn: send back what it has so far and let the server resume.
      messages.push({ role: "assistant", content: response.content });
    }
  } catch (e) {
    const timedOut = /time budget|timed? ?out|abort/i.test(String(e && (e.name + " " + e.message)));
    await send(timedOut
      ? "⏳ That one needed more time than my 25-second window allows. Try asking a narrower question, or without a news search."
      : "⚠️ I couldn't reach Claude just now. Please try again in a minute.");
    console.log("claude error:", e && e.name, e && e.status);
    return sent;
  }

  if (response.stop_reason === "refusal") {
    await send("I can't help with that one.");
    return sent;
  }
  let text = textOf(response) || "I don't have an answer for that.";
  if (response.stop_reason === "max_tokens") text += "\n\n(cut short; ask me to continue)";
  for (const part of splitMessage(text)) await send(part);
  return sent;
}


// ---------------------------------------------------------------- alerter heartbeat
// GitHub's own schedule is best-effort and dropped most runs (7 in 2 days instead of ~300),
// so this Worker's cron starts the alerter every 10 minutes through workflow_dispatch instead.
export async function dispatchAlerter(env, doFetch) {
  if (!env.GH_DISPATCH_TOKEN) {
    console.log("heartbeat skipped: GH_DISPATCH_TOKEN not set");
    return { ok: false, reason: "no token" };
  }
  const repo = env.GH_REPO || "labpotato/stock-alerts";
  const res = await doFetch(`https://api.github.com/repos/${repo}/actions/workflows/alert.yml/dispatches`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "stock-alerts-heartbeat",
      "content-type": "application/json",
    },
    body: JSON.stringify({ ref: "main", inputs: { mode: "poll" } }),
  });
  if (res.status !== 204) {
    const text = await res.text().catch(() => "");
    console.log("heartbeat failed:", res.status, text.slice(0, 200));
    return { ok: false, status: res.status };
  }
  return { ok: true };
}
