// Investor panel: ai-hedge-fund's five investor personas (Buffett, Munger, Graham, Lynch,
// Druckenmiller) judge each single stock from its SEC-filed history (docs/snapshots.json, built by
// tools/snapshot.py), plus the original's post-earnings drift rule read from the market's reaction.
//
// As in the original, an unchanged snapshot never pays for a second model call: a stock is judged
// again only when a new filing changes its numbers, its price moves more than 15% since the last
// verdict, or Dongmin asks with "/panel refresh". Every verdict is kept with its price so the panel
// can be graded later (the original grades at each persona's horizon: one year, Druckenmiller six months).
import { PERSONAS } from "./personas.js";

const MODEL = "claude-opus-5-5";
export const PANEL_TIMES = ["23:20"];          // UTC, weekdays: after the research job (22:41 UTC)
const REPRICE = 0.15;
const NAMES = { buffett: "Buffett", munger: "Munger", graham: "Graham", lynch: "Lynch", druckenmiller: "Druckenmiller" };
const DOT = { bullish: "🟢", neutral: "⚪", bearish: "🔴" };
const SIGN = { bullish: 1, neutral: 0, bearish: -1 };
const SCHEMA = {
  type: "object",
  properties: { signal: { type: "string", enum: ["bullish", "bearish", "neutral"] },
                confidence: { type: "integer" }, reasoning: { type: "string" } },
  required: ["signal", "confidence", "reasoning"], additionalProperties: false,
};
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const big = (v) => v == null ? "n/a" : Math.abs(v) >= 1e12 ? `$${(v / 1e12).toFixed(2)}T` : `$${(v / 1e9).toFixed(1)}B`;

/** The prompt: the original's snapshot text, plus the latest close (the original's live runs add the day's context). */
export function personaUser(s) {
  if (s.kind === "etf") {
    return `${s.text}\n\nLatest close: $${Number(s.price).toFixed(2)} on ${s.price_date}.`;
  }
  const pe = s.pe_now == null ? "n/a (no profit over the last year)" : s.pe_now.toFixed(1);
  const cur = s.currency && s.currency !== "USD" ? s.currency + " " : "$";
  const cap = s.currency && s.currency !== "USD" ? `${s.currency} ${(s.market_cap_now / 1e12).toFixed(1)} trillion` : big(s.market_cap_now);
  return `${s.text}\n\nLatest close (after the newest filing): ${cur}${Number(s.price).toFixed(2)} on ${s.price_date}. ` +
    `Market cap at that price: ${cap}. P/E at that price: ${pe}.`;
}

export function panelParams(name, s, model) {
  return {
    model: model || MODEL, max_tokens: 1500,
    system: [{ type: "text", text: PERSONAS[name].system, cache_control: { type: "ephemeral" } }],
    output_config: { effort: "low", format: { type: "json_schema", schema: SCHEMA } },
    messages: [{ role: "user", content: personaUser(s) }],
  };
}

async function ask(client, name, s, model) {
  const params = panelParams(name, s, model);
  const opts = { timeout: 45000, maxRetries: 1 };
  try {
    const res = /haiku/.test(params.model) ? await client.messages.create(params, opts)
      : await client.beta.messages.create({ ...params, betas: ["server-side-fallback-2026-07-01"], fallbacks: "default" }, opts);
    if (res.stop_reason === "refusal") return { signal: "neutral", confidence: 0, reasoning: "Abstained.", abstained: true };
    const j = JSON.parse((res.content || []).filter((b) => b.type === "text").map((b) => b.text).join(""));
    const c = Math.max(0, Math.min(100, Math.round(Number(j.confidence) || 0)));
    return SIGN[j.signal] === undefined ? { signal: "neutral", confidence: 0, reasoning: "Abstained.", abstained: true }
      : { signal: j.signal, confidence: c, reasoning: String(j.reasoning || "").slice(0, 600) };
  } catch (e) {
    // The original's failure contract: a failed call abstains, it never becomes a fake neutral view.
    return { signal: "neutral", confidence: 0, reasoning: "Abstained (the model call failed).", abstained: true };
  }
}

/** -100..+100: the confidence-weighted average view; abstentions do not count. */
export function consensus(verdicts) {
  const v = Object.values(verdicts).filter((x) => !x.abstained);
  if (!v.length) return { score: 0, label: "no view" };
  const score = Math.round(v.reduce((a, x) => a + SIGN[x.signal] * x.confidence, 0) / v.length);
  return { score, label: score >= 25 ? "leans bullish" : score <= -25 ? "leans bearish" : "mixed" };
}

export function panelSlot(t, times = PANEL_TIMES) {
  const d = new Date(t), hm = d.toISOString().slice(11, 16), wd = d.getUTCDay();
  return times.includes(hm) && wd >= 1 && wd <= 5 ? d.toISOString().slice(0, 10) : null;
}

export function detailMessage(t, e, s) {
  const c = consensus(e.verdicts);
  const p = (s && s.periods && s.periods[0]) || {};
  const basis = e.kind === "etf" || (s && s.kind === "etf") ? "read as a basket of its holdings"
    : `latest quarter to ${esc(p.period || "?")}`;
  const lines = [`🏛️ <b>Investor panel — ${esc(t)}</b>${e.name ? " · " + esc(e.name) : ""}`,
    `Judged ${esc(e.at)} at ${Number(e.price).toFixed(2)} · ${basis}`, ""];
  for (const n of Object.keys(PERSONAS)) {
    const v = e.verdicts[n];
    if (!v) continue;
    lines.push(`${v.abstained ? "▫️" : DOT[v.signal]} <b>${NAMES[n]}</b> ${v.abstained ? "abstained" : `${v.signal} ${v.confidence}`}` +
      (v.abstained ? "" : `: ${esc(v.reasoning)}`), "");
  }
  lines.push(`<b>Panel:</b> ${c.label} (${c.score >= 0 ? "+" : ""}${c.score} on −100 to +100).`);
  if (s && s.drift && s.drift.view && s.drift.view !== "none" && s.drift.fresh) lines.push(driftLine(t, s.drift));
  lines.push("", "<i>AI opinions in the styles of famous investors (from ai-hedge-fund), read from SEC filings. " +
    "No proven track record yet: each verdict is kept and graded after a year. Not a signal, and never an order.</i>");
  return lines.join("\n");
}

export function summaryMessage(st, only) {
  const rows = Object.entries(st.stocks || {}).filter(([t]) => !only || only.includes(t));
  if (!rows.length) return "🏛️ <b>Investor panel</b>\nNo verdicts yet. They arrive after the first SEC snapshot (the research job needs the SEC_USER_AGENT secret).";
  const lines = ["🏛️ <b>Investor panel</b> · B Buffett · M Munger · G Graham · L Lynch · D Druckenmiller", ""];
  for (const [t, e] of rows) {
    const c = consensus(e.verdicts);
    const dots = Object.keys(PERSONAS).map((n) => `${NAMES[n][0]}${e.verdicts[n] ? (e.verdicts[n].abstained ? "▫️" : DOT[e.verdicts[n].signal]) : "·"}`).join(" ");
    lines.push(`<b>${esc(t)}</b>  ${dots}  → ${c.label} (${c.score >= 0 ? "+" : ""}${c.score})`);
  }
  const nc = Object.entries(st.not_covered || {});
  if (nc.length && !only) lines.push("", `Not covered: ${nc.map(([t, why]) => `${esc(t)} (${esc(why.split(":")[0])})`).join(", ")}: no company financials to judge.`);
  lines.push("", "Reasons: /panel NVDA (any ticker). Fresh verdicts on demand: /panel refresh. ETFs are judged as a basket of their holdings.",
    "<i>AI opinions in famous investors' styles, from SEC filings; no proven record yet. Never an order.</i>");
  return lines.join("\n");
}

export function driftLine(t, d) {
  const until = d.until_sessions ? ` for about ${d.until_sessions} trading days` : "";
  return `${d.view === "up" ? "📈" : "📉"} <b>Earnings drift:</b> after its earnings report (filed ${esc(d.filed)}), ${esc(t)} moved ` +
    `${d.reaction_pct >= 0 ? "+" : ""}${d.reaction_pct}% in three sessions vs the S&P 500's ${d.spy_pct >= 0 ? "+" : ""}${d.spy_pct}%. ` +
    `The post-earnings drift rule leans <b>${d.view.toUpperCase()}</b>${until}.`;
}

export function driftMessage(t, d) {
  return [driftLine(t, d), "",
    "<i>Prices have tended to keep drifting in the direction of an earnings surprise, but the effect has " +
    "weakened a lot in recent decades, especially in large companies. A tilt, not a signal, and never an order.</i>"].join("\n");
}

async function send(env, doFetch, text) {
  await doFetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TG_CHAT, text, parse_mode: "HTML", link_preview_options: { is_disabled: true } }),
  });
}

export async function loadPanel(kv) {
  try { return JSON.parse((await kv.get("panel")) || "null") || { stocks: {}, drift_sent: [], history: [] }; }
  catch { return { stocks: {}, drift_sent: [], history: [] }; }
}

// The work runs in the every-minute cron, at most PER_STEP stocks a minute, saving after each stock:
// a Telegram webhook only gets about 30 seconds after its reply, and one cron run shares a limit of
// 50 outside requests with the news check, so 25 model calls cannot happen in one go.
export const PER_STEP = 3;

/** Queue a panel run. opts: { force: re-judge every stock, slot: only at the daily slot, only: [tickers] }. */
export async function enqueuePanel(env, deps, opts = {}) {
  const { kv, now = () => Date.now() } = deps;
  if (!kv) return { off: "no NEWS_KV binding" };
  const t = now();
  if (opts.slot) {
    const slot = panelSlot(t);
    if (!slot) return { skipped: "not a panel time" };
    if ((await kv.get("panel_slot")) === slot) return { skipped: "already ran" };
    await kv.put("panel_slot", slot);
  }
  const q = { force: !!opts.force, only: opts.only || null, todo: null, changed: [], silent: !!opts.silent,
              summary: !!opts.summary, at: t };
  await kv.put("panel_queue", JSON.stringify(q));
  return { queued: true };
}

/** One cron step: judge up to PER_STEP stocks from the queue; when the queue is done, send the messages. */
export async function stepPanel(env, deps) {
  const { fetch: doFetch, client, kv, now = () => Date.now() } = deps;
  if (!kv) return { off: "no NEWS_KV binding" };
  let q;
  try { q = JSON.parse((await kv.get("panel_queue")) || "null"); } catch { q = null; }
  if (!q) return { idle: true };
  const t = now();
  let snaps;
  try { const r = await doFetch(env.SNAP_URL, { cf: { cacheTtl: 60 } }); snaps = r.ok ? await r.json() : null; } catch { snaps = null; }
  if (!snaps || !snaps.stocks || !Object.keys(snaps.stocks).length) {
    await kv.delete("panel_queue");
    if (q.summary) await send(env, doFetch, "🏛️ No SEC snapshots yet, so no panel. The research job builds them on weekdays after the US close.");
    return { skipped: "no snapshots yet" };
  }
  if (!q.todo) q.todo = Object.keys(snaps.stocks).filter((tk) => !q.only || q.only.includes(tk));
  const st = await loadPanel(kv);
  const day = new Date(t).toISOString().slice(0, 10);
  let judged = 0;
  while (q.todo.length && judged < PER_STEP) {
    const tk = q.todo.shift(), s = snaps.stocks[tk];
    if (!s) continue;
    const old = st.stocks[tk];
    const moved = old && old.price && Math.abs(s.price / old.price - 1) > REPRICE;
    if (!q.force && old && old.hash === s.hash && !moved) continue;      // unchanged numbers: free
    const names = Object.keys(PERSONAS);
    const got = await Promise.all(names.map((n) => ask(client, n, s, env.PANEL_MODEL)));
    const verdicts = Object.fromEntries(names.map((n, i) => [n, got[i]]));
    st.stocks[tk] = { name: s.name, kind: s.kind || "company", hash: s.hash, price: s.price, price_date: s.price_date, at: day, verdicts,
                      why: !old ? "first" : old.hash !== s.hash ? "new filing" : moved ? "price moved" : "asked" };
    st.history = (st.history || []).concat([{ t: tk, at: day, price: s.price,
      v: Object.fromEntries(names.map((n) => [n, [verdicts[n].signal, verdicts[n].confidence]])) }]).slice(-400);
    q.changed.push(tk);
    judged += 1;
    st.as_of = snaps.as_of;
    await kv.put("panel", JSON.stringify(st));                            // saved stock by stock
  }
  st.not_covered = snaps.not_covered || {};
  if (q.todo.length) {
    await kv.put("panel_queue", JSON.stringify(q));
    return { progress: q.changed.length, left: q.todo.length };
  }
  await kv.delete("panel_queue");
  const drifts = [];
  for (const [tk, s] of Object.entries(snaps.stocks)) {
    const d = s.drift, key = `${tk}:${d && d.filed}`;
    if (d && d.fresh && (d.view === "up" || d.view === "down") && !(st.drift_sent || []).includes(key)) {
      st.drift_sent = (st.drift_sent || []).concat([key]).slice(-60);
      drifts.push([tk, d]);
    }
  }
  st.as_of = snaps.as_of;
  await kv.put("panel", JSON.stringify(st));
  const msgs = [];
  if (q.summary || q.changed.length > 2) { if (q.changed.length || q.summary) msgs.push(summaryMessage(st)); }
  else for (const tk of q.changed) msgs.push(detailMessage(tk, st.stocks[tk], snaps.stocks[tk]));
  for (const [tk, d] of drifts) if (!q.changed.includes(tk)) msgs.push(driftMessage(tk, d));
  if (!q.silent) for (const m of msgs) await send(env, doFetch, m);
  return { changed: q.changed, drifts: drifts.map(([tk]) => tk), msgs, done: true };
}

/** Queue and run to the end in one go (tests, and manual runs outside the Worker's time limits). */
export async function runPanel(env, deps, opts = {}) {
  const e = await enqueuePanel(env, deps, opts);
  if (!e.queued) return e;
  for (let i = 0; i < 50; i += 1) {
    const r = await stepPanel(env, deps);
    if (r.done || r.skipped || r.idle) return r;
  }
  return { error: "panel did not finish" };
}

/** The /panel_<slug> form: "005930.KS" -> "005930_ks". Same rule as tools/bot_commands.py. */
export const slug = (t) => String(t).toLowerCase().replace(/[^a-z0-9]/g, "_");

/** "/panel", "/panel NVDA", "/panel_nvda", "/panel refresh": text replies for the bot.
 *  Tickers that have a snapshot but no verdict yet are judged on request (a ticker added to the
 *  watchlist does not wait for the daily run). */
export async function panelCommand(env, deps, arg) {
  const raw = String(arg || "").replace(/^@\S+/, "").trim();
  const a = raw.toUpperCase();
  const queued = async () => { try { return JSON.parse((await deps.kv.get("panel_queue")) || "null"); } catch { return null; } };
  if (a === "REFRESH") {
    await enqueuePanel(env, deps, { force: true, summary: true });
    return ["🏛️ Judging every stock and ETF you watch now (five investors each, three tickers a minute). The panel arrives here in about 7 minutes."];
  }
  const st = await loadPanel(deps.kv);
  let snaps = {};
  try { const r = await deps.fetch(env.SNAP_URL); snaps = r.ok ? ((await r.json()).stocks || {}) : {}; } catch { snaps = {}; }
  const known = [...new Set([...Object.keys(snaps), ...Object.keys(st.stocks)])];
  const tk = a ? (known.find((t) => t === a) || known.find((t) => slug(t) === slug(raw))) : null;
  if (a && !tk) {
    const nc = (st.not_covered || {})[a] || Object.entries(st.not_covered || {}).find(([t]) => slug(t) === slug(raw));
    if (nc) return [`🏛️ ${esc(a)} has no company financials (gold and coins), so the investor panel cannot judge it.`];
    return [`🏛️ No panel for ${esc(a)}. The panel covers: ${known.join(", ") || "none yet"}.`];
  }
  if (tk && st.stocks[tk]) return [detailMessage(tk, st.stocks[tk], snaps[tk])];
  if (tk) {
    if (!(await queued())) await enqueuePanel(env, deps, { only: [tk] });
    return [`🏛️ ${esc(tk)} has no verdict yet. The five investors are judging it now; their reasons arrive here in about a minute.`];
  }
  const missing = Object.keys(snaps).filter((t) => !st.stocks[t]);
  const out = [];
  if (Object.keys(st.stocks).length) out.push(summaryMessage(st));
  if (missing.length) {
    if (!(await queued())) await enqueuePanel(env, deps, { only: missing, summary: true });
    const mins = Math.max(1, Math.ceil(missing.length / PER_STEP));
    out.push(`🏛️ ${missing.length} ticker${missing.length > 1 ? "s have" : " has"} no verdict yet (${esc(missing.join(", "))}). ` +
      `Judging ${missing.length > 1 ? "them" : "it"} now; the full grid arrives here in about ${mins} minute${mins > 1 ? "s" : ""}.`);
  }
  if (!out.length) out.push(summaryMessage(st));
  return out;
}
