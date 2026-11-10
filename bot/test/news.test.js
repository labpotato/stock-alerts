// Tests for the news flashes. No network: feeds, Claude, Telegram and KV are fakes.
import { test } from "node:test";
import assert from "node:assert/strict";
import { classify, flash, newsParams, parseFeed, pickCandidates, ruleLine, runNews, FEEDS, NEWS_SYSTEM } from "../src/news.js";

const NOW = Date.parse("2026-11-10T13:50:00Z");
const rss = (items) => `<?xml version="1.0"?><rss><channel>${items.map((i) => `<item><title><![CDATA[${i.title}]]></title><link>${i.link || "https://x.test/a"}</link><guid>${i.guid}</guid><pubDate>${new Date(i.ts).toUTCString()}</pubDate>${i.source ? `<source url="https://s.test">${i.source}</source>` : ""}</item>`).join("")}</channel></rss>`;
const atom = `<feed><entry><title>8-K - Tesla, Inc. (0001318605) (Filer)</title><link rel="alternate" href="https://www.sec.gov/Archives/x.htm"/><summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-11-10 &lt;br&gt;Item 2.02: Results of Operations and Financial Condition &lt;br&gt;Item 9.01: Financial Statements and Exhibits</summary><updated>2026-11-10T08:46:55-05:00</updated><id>urn:tag:sec.gov,2008:accession-number=0001</id></entry><entry><title>8-K - Cosmos Health Inc. (0001474167) (Filer)</title><link href="https://www.sec.gov/y"/><summary>Item 8.01: Other Events</summary><updated>2026-11-10T08:45:13-05:00</updated><id>urn:2</id></entry></feed>`;
const STATUS = { tickers: { NVDA: { label: "NVDA", price: 178.67, day_pct: -2.1, keltner: "WATCH BUY", momentum: "WATCH BUY", rsi: 27.4 } } };

test("RSS and Atom are parsed; SEC filings are kept only for watched companies", () => {
  const items = parseFeed(rss([{ title: "Nvidia cuts guidance - Reuters", guid: "g1", ts: NOW - 60000 }]), "gnews");
  assert.equal(items.length, 1);
  assert.equal(items[0].title, "Nvidia cuts guidance");
  assert.equal(items[0].source, "Reuters");
  assert.equal(items[0].ts, NOW - 60000);
  const sec = parseFeed(atom, "sec");
  assert.equal(sec.length, 1);                                   // Cosmos Health is not on the list
  assert.match(sec[0].title, /^TSLA filed an 8-K with the SEC: Results of Operations/);
  assert.equal(sec[0].link, "https://www.sec.gov/Archives/x.htm");
});

test("the rule filter passes events from trusted sources and drops opinion and unknown sites", () => {
  const c = (title, source, feed = "gnews") => classify({ title, source, feed });
  assert.equal(c("Nvidia cuts revenue guidance after export ban", "Reuters").pass, true);
  assert.deepEqual(c("Nvidia cuts revenue guidance after export ban", "Reuters").tickers.includes("NVDA"), true);
  assert.equal(c("Why I am still bullish on Nvidia", "Reuters").pass, false);            // no event word
  assert.equal(c("Nvidia cuts guidance", "Some Crypto Blog").pass, false);               // unknown source
  assert.equal(c("Fed cuts interest rate by a quarter point", "CNBC").pass, true);       // macro
  assert.equal(c("삼성전자 3분기 실적 발표", "연합뉴스").pass, true);
  assert.equal(c("Googleplex cafeteria wins award", "Reuters").tickers.includes("GOOGL"), false);  // whole words only
  assert.equal(c("TSLA filed an 8-K with the SEC", "SEC", "sec").pass, true);
});

test("old, already-seen and duplicate headlines are not candidates", () => {
  const mk = (id, title, ageMin) => ({ id, title, source: "Reuters", feed: "gnews", ts: NOW - ageMin * 60000, link: "" });
  const items = [mk("a", "Tesla recall hits 1 million cars", 2), mk("b", "Tesla recall hits 1 million cars", 3),
                 mk("c", "Palantir wins army contract", 45), mk("d", "Nvidia earnings beat", 1)];
  const got = pickCandidates(items, new Set(["d"]), NOW).map((x) => x.id);
  assert.deepEqual(got, ["a"]);
});

test("the flash shows the headline, Claude's take, and what his rule says", () => {
  const item = { title: "Nvidia cuts guidance", source: "Reuters", ts: NOW - 120000, link: "https://r.test/1", tickers: ["NVDA"] };
  const text = flash(item, { send: true, importance: 5, lean: "down", tickers: ["NVDA"], take: "Lower sales ahead; bad for NVDA." }, STATUS, NOW);
  assert.match(text, /^🚨 <b>NVDA<\/b> · Reuters · 21:48 SGT \(2 min ago\)/);
  assert.match(text, /🔴 Lower sales ahead; bad for NVDA\./);
  assert.match(text, /📊 Your rule: NVDA \$178\.67 \(-2\.1% today\): below its normal range, momentum 27 \(sold off hard\)/);
  assert.match(text, /<a href="https:\/\/r\.test\/1">Read it<\/a>/);
  assert.equal(ruleLine(STATUS, "TSLA"), null);
});

test("the Claude request is structured, low effort, with a frozen cacheable system prompt", () => {
  const p = newsParams({ title: "x", source: "Reuters", tickers: ["NVDA"] });
  assert.equal(p.model, "claude-opus-5-5");
  assert.equal(p.output_config.effort, "low");
  assert.equal(p.output_config.format.type, "json_schema");
  assert.equal(p.system[0].text, NEWS_SYSTEM);
  assert.equal(p.thinking, undefined);
  assert.equal(newsParams({ title: "x", tickers: [] }, "claude-haiku-5-5").model, "claude-haiku-5-5");
});

function harness(feedItems, verdicts) {
  const store = {}, tg = [], calls = [];
  const kv = { get: async (k) => store[k] ?? null, put: async (k, v) => { store[k] = v; } };
  const reply = () => ({ stop_reason: "end_turn", content: [{ type: "text", text: JSON.stringify(verdicts.shift()) }] });
  const create = async (params) => { calls.push(params); return reply(); };
  const client = { messages: { create }, beta: { messages: { create } } };
  const fetch = async (url, init) => {
    if (url.startsWith("https://api.telegram.org")) { tg.push(JSON.parse(init.body)); return { ok: true }; }
    if (url === "https://s.test/status.json") return { ok: true, json: async () => STATUS };
    if (url === FEEDS[1].url) return { ok: true, text: async () => rss(feedItems) };
    return { ok: true, text: async () => "<rss></rss>" };
  };
  return { store, tg, calls, deps: { fetch, client, kv, now: () => NOW } };
}
const ENV = { TG_TOKEN: "t", TG_CHAT: "111", STATUS_URL: "https://s.test/status.json" };
const ITEM = { title: "Nvidia cuts revenue guidance - Reuters", guid: "n1", ts: NOW - 60000 };

test("the first run only learns what is already out; later runs send new, important headlines once", async () => {
  const h = harness([ITEM], []);
  let r = await runNews(ENV, h.deps);
  assert.equal(r.first, true);
  assert.equal(h.tg.length, 0);
  assert.equal(h.calls.length, 0);
  // A new headline appears.
  const h2 = harness([ITEM, { title: "Tesla recall hits 1 million cars - CNBC", guid: "t1", ts: NOW - 30000 }],
                     [{ send: true, importance: 4, lean: "down", tickers: ["TSLA"], take: "A large recall; bad for TSLA." }]);
  h2.store.news = h.store.news;
  r = await runNews(ENV, h2.deps);
  assert.equal(h2.calls.length, 1);
  assert.equal(h2.tg.length, 1);
  assert.equal(h2.tg[0].chat_id, "111");
  assert.match(h2.tg[0].text, /^⚡ <b>TSLA<\/b> · CNBC/);
  assert.deepEqual(h2.calls[0].betas, ["server-side-fallback-2026-07-01"]);
  const recent = JSON.parse(h2.store.news).recent;
  assert.equal(recent.length, 1);
  assert.equal(recent[0].title, "Tesla recall hits 1 million cars");
  r = await runNews(ENV, { ...h2.deps });                         // same feed again: nothing new
  assert.equal(h2.tg.length, 1);
});

test("noise is judged but not sent, the daily cap stops Claude calls, and no KV means off", async () => {
  const h = harness([ITEM], [{ send: false, importance: 1, lean: "unclear", tickers: [], take: "Recap." }]);
  h.store.news = JSON.stringify({ seen: [], day: "2026-11-10", count: 0 });
  await runNews(ENV, h.deps);
  assert.equal(h.calls.length, 1);
  assert.equal(h.tg.length, 0);
  const capped = harness([ITEM], []);
  capped.store.news = JSON.stringify({ seen: [], day: "2026-11-10", count: 40 });
  await runNews(ENV, capped.deps);
  assert.equal(capped.calls.length, 0);
  assert.deepEqual(await runNews(ENV, { ...h.deps, kv: undefined }), { off: "no NEWS_KV binding" });
});
