// Tests for the Telegram Q&A bot. No network: Claude and Telegram are fakes.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { answer, compactStatus, dispatchAlerter, isAllowed, requestParams, splitMessage, SYSTEM, MODEL } from "../src/bot.js";

const STATUS = JSON.parse(readFileSync(new URL("../../docs/status.json", import.meta.url)));
const ENV = { TG_TOKEN: "t0k", TG_CHAT: "111", TG_WEBHOOK_SECRET: "s3cret",
              STATUS_URL: "https://example.invalid/status.json",
              HOLDINGS_JSON: JSON.stringify({ as_of: "2026-11-04", cash_usd: 9876.5, tickers: { GLDM: { shares: 4321 } } }) };
const MSG = { message_id: 7, chat: { id: 111 }, text: "How close is GLDM to a buy?" };

function harness(responses, opts = {}) {
  const calls = [], tg = [];
  let clock = 0;
  const client = { beta: { messages: { create: async (params, options) => {
    calls.push({ params: structuredClone(params), options });
    clock += opts.stepMs ?? 2000;
    const r = responses.shift();
    if (r instanceof Error) throw r;
    return r;
  } } } };
  const fetch = async (url, init) => {
    if (url === ENV.STATUS_URL) return { ok: true, json: async () => STATUS };
    tg.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
    return { ok: true, json: async () => ({ ok: true }) };
  };
  return { calls, tg, deps: { client, fetch, now: () => clock } };
}
const reply = (text, stop = "end_turn", extra = []) => ({ stop_reason: stop, content: [...extra, { type: "text", text }] });
const sentTexts = (tg) => tg.filter((x) => x.url.endsWith("/sendMessage")).map((x) => x.body.text);

test("only Dongmin's chat, and only with Telegram's secret header, is answered", () => {
  const up = { message: MSG };
  assert.equal(isAllowed(up, ENV, "s3cret"), true);
  assert.equal(isAllowed(up, ENV, "wrong"), false);
  assert.equal(isAllowed(up, ENV, null), false);
  assert.equal(isAllowed({ message: { ...MSG, chat: { id: 999 } } }, ENV, "s3cret"), false);
  assert.equal(isAllowed({ edited_message: MSG }, ENV, "s3cret"), false);
  assert.equal(isAllowed(up, { ...ENV, TG_WEBHOOK_SECRET: "" }, ""), false);   // no secret configured: refuse
});

test("the request uses Opus 5.5, web search, the default refusal fallback, and the live snapshot", async () => {
  const h = harness([reply("GLDM needs about +1.4% for a buy.")]);
  const sent = await answer(ENV, MSG, h.deps);
  assert.deepEqual(sent, ["GLDM needs about +1.4% for a buy."]);
  const p = h.calls[0].params;
  assert.equal(p.model, MODEL);
  assert.equal(p.model, "claude-opus-5-5");
  assert.equal(p.fallbacks, "default");
  assert.deepEqual(p.betas, ["server-side-fallback-2026-07-01"]);
  assert.equal(p.tools[0].type, "web_search_20260209");
  assert.equal(p.output_config.effort, "low");
  assert.equal(p.system[0].text, SYSTEM);                    // frozen system prompt: cacheable
  assert.equal(p.thinking, undefined);                       // Opus 5.5: no disabled thinking
  const ctx = p.messages[0].content.map((b) => b.text).join("\n");
  assert.match(ctx, /generated_utc/);
  assert.match(ctx, /4321/);                                 // holdings go to Claude...
  assert.ok(!SYSTEM.includes("4321"));                       // ...in the user turn, not the system prompt
  assert.match(ctx, /How close is GLDM to a buy\?/);
  assert.ok(h.calls[0].options.timeout <= 26000 && h.calls[0].options.maxRetries === 0);
  const sendMsg = h.tg.find((x) => x.url.endsWith("/sendMessage"));
  assert.equal(sendMsg.body.chat_id, 111);
  assert.equal(sendMsg.body.reply_parameters.message_id, 7);
});

test("a paused web-search turn is resumed by sending back the partial assistant turn", async () => {
  const paused = { stop_reason: "pause_turn", content: [{ type: "server_tool_use", id: "x", name: "web_search", input: {} }] };
  const h = harness([paused, reply("News: none material today.")]);
  const sent = await answer(ENV, MSG, h.deps);
  assert.deepEqual(sent, ["News: none material today."]);
  assert.equal(h.calls.length, 2);
  const m = h.calls[1].params.messages;
  assert.equal(m.length, 2);
  assert.equal(m[1].role, "assistant");
  assert.equal(m[1].content[0].type, "server_tool_use");
});

test("replying to an alert passes the alert's text along with the question", async () => {
  const h = harness([reply("ok")]);
  await answer(ENV, { ...MSG, text: "why?", reply_to_message: { text: "🔴🔴 STRONG SELL — XLV" } }, h.deps);
  const ctx = h.calls[0].params.messages[0].content.map((b) => b.text).join("\n");
  assert.match(ctx, /STRONG SELL — XLV/);
  assert.match(ctx, /why\?/);
});

test("refusals, timeouts and API errors give a plain message, never an empty one", async () => {
  let h = harness([{ stop_reason: "refusal", content: [] }]);
  assert.deepEqual(await answer(ENV, MSG, h.deps), ["I can't help with that one."]);
  const slow = Object.assign(new Error("Request timed out."), { name: "APIConnectionTimeoutError" });
  h = harness([slow]);
  assert.match((await answer(ENV, MSG, h.deps))[0], /25-second window/);
  h = harness([Object.assign(new Error("overloaded"), { name: "APIError", status: 529 })]);
  assert.match((await answer(ENV, MSG, h.deps))[0], /couldn't reach Claude/);
  // Out of time before a continuation: stop rather than overrun the 30-second limit.
  const paused = { stop_reason: "pause_turn", content: [] };
  h = harness([paused, reply("late")], { stepMs: 24000 });
  assert.match((await answer(ENV, MSG, h.deps))[0], /25-second window/);
  assert.equal(h.calls.length, 1);
});

test("/start and /help answer without calling Claude", async () => {
  const h = harness([]);
  const sent = await answer(ENV, { ...MSG, text: "/start" }, h.deps);
  assert.equal(h.calls.length, 0);
  assert.match(sent[0], /Ask me anything/);
});

test("long answers are split under Telegram's limit, on line breaks", () => {
  const long = Array.from({ length: 300 }, (_, i) => `line ${i} ` + "x".repeat(20)).join("\n");
  const parts = splitMessage(long);
  assert.ok(parts.length > 1);
  assert.ok(parts.every((p) => p.length <= 4000));
  assert.equal(parts.join("\n").replace(/\s+/g, ""), long.replace(/\s+/g, ""));
});

test("the compacted snapshot keeps the answer-bearing fields of the real status.json", () => {
  const c = compactStatus(STATUS);
  assert.equal(c.generated_utc, STATUS.generated_utc);
  assert.ok(Object.keys(c.tickers).length === Object.keys(STATUS.tickers).length);
  const any = Object.values(c.tickers)[0];
  for (const k of ["price", "price_range", "momentum", "range_low", "range_high", "rsi"]) assert.ok(k in any, k);
  assert.ok(JSON.stringify(c).length < JSON.stringify(STATUS).length);
});

test("nothing from the holdings secret reaches the Telegram message unless Claude wrote it", async () => {
  const h = harness([reply("GLDM is about 1.4% from a buy.")]);
  await answer(ENV, MSG, h.deps);
  const out = JSON.stringify(h.tg);
  assert.ok(!out.includes("9876.5") && !out.includes("4321"));
});

test("the heartbeat starts one alerter run on GitHub, and does nothing without a token", async () => {
  const calls = [];
  const fake = async (url, init) => { calls.push({ url, init }); return { status: 204, text: async () => "" }; };
  assert.deepEqual(await dispatchAlerter({ GH_DISPATCH_TOKEN: "ghp_x" }, fake), { ok: true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.github.com/repos/labpotato/stock-alerts/actions/workflows/alert.yml/dispatches");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.authorization, "Bearer ghp_x");
  assert.ok(calls[0].init.headers["user-agent"]);                     // GitHub rejects requests without one
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main", inputs: { mode: "poll" } });
  calls.length = 0;
  assert.equal((await dispatchAlerter({}, fake)).ok, false);
  assert.equal(calls.length, 0);
  const denied = async () => ({ status: 403, text: async () => "Resource not accessible" });
  assert.deepEqual(await dispatchAlerter({ GH_DISPATCH_TOKEN: "bad" }, denied), { ok: false, status: 403 });
});
