import { test } from "node:test";
import assert from "node:assert/strict";
import { PERSONAS } from "../src/personas.js";
import { consensus, panelCommand, panelParams, panelSlot, personaUser, runPanel, stepPanel, summaryMessage, PER_STEP } from "../src/panel.js";
import { answer } from "../src/bot.js";

const SNAP = (hash = "h1", price = 180, drift = null) => ({
  name: "NVIDIA CORP", hash, price, price_date: "2026-11-13", market_cap_now: 4.4e12, pe_now: 45.2,
  text: "Company: NVDA  |  Sector: Semiconductors\nSummary:\n  ROE avg: 0.80", periods: [{ period: "2026-07-26" }], drift,
});
const DRIFT = { filed: "2026-11-12", view: "up", fresh: true, reaction_pct: 8, spy_pct: 1, abnormal_pct: 7, until_sessions: 60 };

function harness(snaps, verdict = { signal: "bullish", confidence: 70, reasoning: "Wonderful business, fair price." }) {
  const store = {}, tg = [], calls = [];
  const kv = { get: async (k) => store[k] ?? null, put: async (k, v) => { store[k] = v; }, delete: async (k) => { delete store[k]; } };
  const create = async (p) => { calls.push(p); return { stop_reason: "end_turn", content: [{ type: "text", text: JSON.stringify(verdict) }] }; };
  const client = { messages: { create }, beta: { messages: { create } } };
  let current = snaps;
  const fetch = async (url, init) => {
    if (url.startsWith("https://api.telegram.org")) { tg.push(JSON.parse(init.body)); return { ok: true }; }
    if (url === "https://s.test/snapshots.json") return { ok: true, json: async () => current };
    return { ok: false };
  };
  return { store, tg, calls, set: (s) => { current = s; }, deps: { fetch, client, kv, now: () => Date.parse("2026-11-16T23:20:00Z") } };
}
const ENV = { TG_TOKEN: "t", TG_CHAT: "111", SNAP_URL: "https://s.test/snapshots.json" };

test("the five personas are the original's, verbatim, with their horizons", () => {
  assert.deepEqual(Object.keys(PERSONAS), ["buffett", "munger", "graham", "lynch", "druckenmiller"]);
  assert.match(PERSONAS.buffett.system, /^You are Warren Buffett, evaluating a single company as a\nlong-term business owner/);
  assert.match(PERSONAS.munger.system, /too-hard pile/);
  assert.equal(PERSONAS.druckenmiller.horizon, 126);
  assert.equal(PERSONAS.graham.horizon, 252);
  const p = panelParams("lynch", SNAP());
  assert.equal(p.system[0].text, PERSONAS.lynch.system);
  assert.deepEqual(p.output_config.format.schema.required, ["signal", "confidence", "reasoning"]);
  assert.match(personaUser(SNAP()), /Latest close \(after the newest filing\): \$180\.00 on 2026-11-13\. Market cap at that price: \$4\.40T\. P\/E at that price: 45\.2\./);
});

test("each stock is judged once per filing; a new filing, a 15% move or 'refresh' re-judges it", async () => {
  const h = harness({ as_of: "2026-11-16", stocks: { NVDA: SNAP() } });
  let r = await runPanel(ENV, h.deps, { slot: true });
  assert.deepEqual(r.changed, ["NVDA"]);
  assert.equal(h.calls.length, 5);
  assert.match(h.tg[0].text, /^🏛️ <b>Investor panel — NVDA<\/b> · NVIDIA CORP/);
  assert.match(h.tg[0].text, /🟢 <b>Buffett<\/b> bullish 70: Wonderful business/);
  assert.match(h.tg[0].text, /<b>Panel:<\/b> leans bullish \(\+70/);
  r = await runPanel(ENV, h.deps, { slot: true });                                   // same slot
  assert.equal(r.skipped, "already ran");
  r = await runPanel(ENV, h.deps, {});                                              // unchanged numbers: free
  assert.deepEqual(r.changed, []);
  assert.equal(h.calls.length, 5);
  h.set({ stocks: { NVDA: SNAP("h1", 160) } });                                       // -11%: still free
  assert.deepEqual((await runPanel(ENV, h.deps, {})).changed, []);
  h.set({ stocks: { NVDA: SNAP("h1", 150) } });                                       // -17%: judged again
  assert.deepEqual((await runPanel(ENV, h.deps, {})).changed, ["NVDA"]);
  h.set({ stocks: { NVDA: SNAP("h2", 150) } });                                       // a new filing
  assert.deepEqual((await runPanel(ENV, h.deps, {})).changed, ["NVDA"]);
  assert.equal(JSON.parse(h.store.panel).history.length, 3);                          // kept for grading
  assert.equal(panelSlot(Date.parse("2026-11-14T23:20:00Z")), null);                  // Saturday
});

test("a fresh earnings drift is sent once; failed calls abstain instead of faking a view", async () => {
  const h = harness({ stocks: { NVDA: SNAP("h1", 180, DRIFT) } });
  await runPanel(ENV, h.deps, {});
  assert.match(h.tg[0].text, /📈 <b>Earnings drift:<\/b> after its earnings report \(filed 2026-11-12\), NVDA moved \+8% in three sessions/);
  await runPanel(ENV, h.deps, { force: true });
  assert.equal(h.tg.filter((m) => /Earnings drift/.test(m.text) && !/Investor panel/.test(m.text)).length, 0);
  const bad = harness({ stocks: { NVDA: SNAP() } }, { signal: "maybe", confidence: 300 });
  await runPanel(ENV, bad.deps, {});
  const v = JSON.parse(bad.store.panel).stocks.NVDA.verdicts;
  assert.ok(Object.values(v).every((x) => x.abstained));
  assert.deepEqual(consensus(v), { score: 0, label: "no view" });
});

test("/panel shows the grid, /panel NVDA the reasons, and many changes arrive as one message", async () => {
  const h = harness({ stocks: { NVDA: SNAP(), TSLA: SNAP("t"), PLTR: SNAP("p") } });
  await runPanel(ENV, h.deps, {});
  assert.equal(h.tg.length, 1);
  assert.match(h.tg[0].text, /<b>NVDA<\/b>  B🟢 M🟢 G🟢 L🟢 D🟢  → leans bullish \(\+70\)/);
  const sent = await answer(ENV, { message_id: 1, chat: { id: 111 }, text: "/panel NVDA" },
    { client: h.deps.client, fetch: h.deps.fetch, panel: (a) => panelCommand(ENV, h.deps, a) });
  assert.match(sent[0], /Investor panel — NVDA/);
  assert.equal(h.tg.at(-1).parse_mode, "HTML");
  assert.match((await panelCommand(ENV, h.deps, ""))[0], /Reasons: \/panel NVDA/);
  assert.match((await panelCommand(ENV, h.deps, "AAPL"))[0], /No panel verdict for AAPL/);
  assert.match(summaryMessage({ stocks: {} }), /SEC_USER_AGENT/);
});

test("/panel refresh answers at once and the cron judges two stocks a minute, saving as it goes", async () => {
  const stocks = { NVDA: SNAP("a"), GOOGL: SNAP("b"), TSLA: SNAP("c"), PLTR: SNAP("d"), BE: SNAP("e") };
  const h = harness({ stocks });
  const reply = await panelCommand(ENV, h.deps, "refresh");
  assert.match(reply[0], /Judging every stock and ETF you watch now/);
  assert.equal(h.calls.length, 0);                                     // nothing heavy inside the webhook
  let r = await stepPanel(ENV, h.deps);
  assert.equal(PER_STEP, 3);
  assert.deepEqual([r.progress, r.left], [3, 2]);
  assert.equal(h.calls.length, 15);
  assert.equal(Object.keys(JSON.parse(h.store.panel).stocks).length, 3);   // already saved
  r = await stepPanel(ENV, h.deps);
  assert.ok(r.done);
  assert.equal(h.tg.length, 1);
  assert.match(h.tg[0].text, /<b>BE<\/b>/);
  assert.deepEqual(await stepPanel(ENV, h.deps), { idle: true });
  assert.match((await panelCommand(ENV, h.deps, "@stock_alerts_bot"))[0], /Investor panel/);   // "/panel@bot" in groups
});

test("a plain /panel before any verdict starts the first panel instead of saying nothing", async () => {
  const h = harness({ stocks: { NVDA: SNAP() } });
  assert.match((await panelCommand(ENV, h.deps, ""))[0], /judging every stock and ETF you watch now/);
  assert.match((await panelCommand(ENV, h.deps, ""))[0], /being judged right now/);
  await stepPanel(ENV, h.deps);
  assert.match(h.tg[0].text, /<b>NVDA<\/b>  B🟢/);
});

test("ETFs are judged as a basket, Korean stocks in won, and gold and crypto are listed as not covered", async () => {
  const etf = { name: "iShares Semiconductor ETF", kind: "etf", hash: "e", price: 154.3, price_date: "2026-11-13",
                text: "Fund: SOXX\nThis is an exchange-traded fund, not a single company.", periods: [] };
  const kr = { ...SNAP("k", 98000), name: "Samsung Electronics", currency: "KRW", market_cap_now: 580e12 };
  assert.match(personaUser(etf), /^Fund: SOXX[\s\S]*Latest close: \$154\.30 on 2026-11-13\.$/);
  assert.match(personaUser(kr), /KRW 98000\.00 on 2026-11-13\. Market cap at that price: KRW 580\.0 trillion/);
  const h = harness({ stocks: { SOXX: etf }, not_covered: { GLDM: "gold: no company financials to judge", "BTC-USD": "Bitcoin: no company financials to judge" } });
  await runPanel(ENV, h.deps, {});
  assert.match(h.tg[0].text, /read as a basket of its holdings/);
  assert.match(summaryMessage(JSON.parse(h.store.panel)), /Not covered: GLDM \(gold\), BTC-USD \(Bitcoin\): no company financials to judge\./);
});
