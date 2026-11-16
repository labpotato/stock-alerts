import { test } from "node:test";
import assert from "node:assert/strict";
import { groupOf, listMessage } from "../src/list.js";
import { answer } from "../src/bot.js";

const CFG = { tickers: ["NVDA", "SOXX", "GLDM", "BTC-USD", "SPCX"], info_tickers: ["005930.KS"],
              not_equity: ["GLDM", "BTC-USD"], currency: { "005930.KS": "KRW" },
              fx: { "USDSGD=X": { label: "USD\u2192SGD" } } };
const BOARD = { generated_utc: "2026-11-16T03:58:00Z", tickers: {
  NVDA: { price: 160.95, day_pct: -3.15, keltner: "WATCH BUY", momentum: "WATCH BUY", rsi: 24.3 },
  SOXX: { price: 154.3, day_pct: 1.2, keltner: "NONE", momentum: "NONE", rsi: 48 },
  "005930.KS": { price: 98000, day_pct: 0.5, keltner: "BUY", momentum: "NONE", rsi: 41 },
  "BTC-USD": { price: 64932.1, day_pct: -1.0, keltner: "WATCH BUY", momentum: "WATCH BUY", rsi: 23 },
  "USDSGD=X": { price: 1.2882, day_pct: 0.11, keltner: "NONE", momentum: "NONE", rsi: 65.5 } } };
const SNAPS = { NVDA: { kind: "company" }, SOXX: { kind: "etf" } };

test("/list groups the watchlist with prices, today's move, both measures and held marks", () => {
  assert.equal(groupOf("SOXX", CFG, SNAPS), "ETFs");
  assert.equal(groupOf("BTC-USD", CFG, SNAPS), "Crypto");
  assert.equal(groupOf("USDSGD=X", CFG, SNAPS), "Currencies");
  const text = listMessage(CFG, BOARD, SNAPS, new Set(["NVDA"]));
  assert.match(text, /^📋 <b>Your watchlist<\/b> · 7 tickers/);
  assert.match(text, /<b>Stocks<\/b>\n💼 <b>NVDA<\/b> \$160\.95 ▼3\.1% 🔵🔵 24/);
  assert.match(text, /<b>ETFs<\/b>\n<b>SOXX<\/b> \$154\.30 ▲1\.2% ⚪⚪ 48\n<b>GLDM<\/b>|GLDM — no reading yet/);
  assert.match(text, /<b>005930\.KS<\/b> ₩98,000 ▲0\.5% 🟢⚪ 41/);
  assert.match(text, /<b>BTC-USD<\/b> \$64,932 ▼1\.0% 🔵🔵 23/);
  assert.match(text, /<b>USD→SGD<\/b> 1\.2882/);
  assert.match(text, /SPCX — no reading yet/);
  assert.ok(!/shares/i.test(text.replace("share counts", "")));
});

test("/list is answered as HTML without asking Claude", async () => {
  const sent = [], calls = [];
  const fetch = async (u, init) => { sent.push(JSON.parse(init.body)); return { ok: true }; };
  const client = { beta: { messages: { create: async () => { calls.push(1); } } } };
  await answer({ TG_TOKEN: "t" }, { message_id: 1, chat: { id: 111 }, text: "/list" },
    { client, fetch, list: async () => listMessage(CFG, BOARD, SNAPS, new Set()) });
  assert.equal(calls.length, 0);
  assert.equal(sent[0].parse_mode, "HTML");
  assert.match(sent[0].text, /Your watchlist/);
});
