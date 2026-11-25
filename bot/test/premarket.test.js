import { test } from "node:test";
import assert from "node:assert/strict";
import { premarketDue, runPremarket } from "../src/premarket.js";

test("09:20 New York on a trading day, in both winter and summer time", () => {
  assert.equal(premarketDue(Date.parse("2026-11-27T14:20:00Z")), "2026-11-27");   // EST, Friday
  assert.equal(premarketDue(Date.parse("2026-06-15T13:20:00Z")), "2026-06-15");   // EDT, Monday
  assert.equal(premarketDue(Date.parse("2026-11-27T13:20:00Z")), null);           // 08:20 EST
});

test("weekends and NYSE holidays are skipped", () => {
  assert.equal(premarketDue(Date.parse("2026-11-28T14:20:00Z")), null);           // Saturday
  assert.equal(premarketDue(Date.parse("2026-11-26T14:20:00Z")), null);           // Thanksgiving
});

test("sends the list and the panel once a day", async () => {
  const kv = new Map();
  const env = { TG_TOKEN: "x", TG_CHAT: "1", CONFIG_URL: "c", BOARD_URL: "b", SNAP_URL: "s",
    NEWS_KV: { get: async (k) => kv.get(k) ?? null, put: async (k, v) => { kv.set(k, v); } } };
  const sent = [];
  const fetch = async (u, o) => {
    if (String(u).includes("api.telegram.org")) { sent.push(JSON.parse(o.body).text); return { ok: true, status: 200 }; }
    const body = u === "c" ? { tickers: ["NVDA"], names: {} } : u === "b" ? { tickers: { NVDA: { price: 180, keltner: "NONE", momentum: "NONE", rsi: 50 } } } : { stocks: {} };
    return { ok: true, json: async () => body };
  };
  const deps = { fetch, holdingsFor: async () => null, now: () => Date.parse("2026-11-27T14:20:00Z") };
  const r = await runPremarket(env, deps);
  assert.equal(r.sent, true);
  assert.equal(sent.length, 2);
  assert.match(sent[0], /opens in 10 minutes/);
  assert.match(sent[0], /NVDA/);
  assert.match(sent[1], /Investor panel/);
  assert.equal((await runPremarket(env, deps)).sent, false);
  assert.equal(sent.length, 2);
});
