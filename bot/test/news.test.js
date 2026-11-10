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

test("the flash shows the headline, the honest view, who is hit, the sectors, and his rule", () => {
  const item = { title: "Nvidia cuts guidance", source: "Reuters", ts: NOW - 120000, link: "https://r.test/1", tickers: ["NVDA"] };
  const j = { send: true, importance: 5, lean: "down", primary: [{ name: "NVDA", effect: "bad", why: "Lower sales forecast" }],
              secondary: [{ name: "SOXX", effect: "bad", why: "Nvidia is a top holding" }, { name: "AMD", effect: "good", why: "Could win share" },
                          { name: "NVDA", effect: "bad", why: "dup" }],
              sectors: [{ name: "Semiconductors", effect: "bad", why: "AI chip demand in doubt" }, { name: "AI infrastructure", effect: "mixed", why: "Cheaper chips, slower build-out" }],
              take: "Nvidia lowered its sales forecast.",
              opinion: "A real change in outlook, not noise. Probably not priced in." };
  const text = flash(item, j, STATUS, NOW);
  assert.match(text, /^🚨 <b>NVDA<\/b> · Reuters · 21:48 SGT \(2 min ago\)/);
  assert.match(text, /🔴 Nvidia lowered its sales forecast\./);
  assert.match(text, /💬 <b>My honest view:<\/b> A real change in outlook, not noise\. Probably not priced in\./);
  assert.match(text, /🎯 <b>Directly hit<\/b>\n🔴 <b>NVDA<\/b>: BAD\. Lower sales forecast\n/);
  assert.match(text, /↪️ <b>Also affected<\/b>\n🔴 <b>SOXX<\/b>: BAD\. Nvidia is a top holding\n🟢 <b>AMD<\/b>: GOOD\. Could win share\n\n/);   // NVDA not repeated
  assert.match(text, /🏭 <b>Sectors<\/b>\n🔴 <b>Semiconductors<\/b>: BAD\. AI chip demand in doubt\n🟡 <b>AI infrastructure<\/b>: MIXED\. Cheaper chips, slower build-out/);
  assert.ok(flash(item, { ...j, primary: ["NVDA"], secondary: ["SOXX"], sectors: ["Chips"] }, STATUS, NOW).includes("• <b>SOXX</b>"));   // older plain lists still show
  assert.match(text, /📊 Your rule: NVDA \$178\.67 \(-2\.1% today\): below its normal range, momentum 27 \(sold off hard\)/);
  assert.match(text, /<a href="https:\/\/r\.test\/1">Read it<\/a>/);
  assert.equal(ruleLine(STATUS, "TSLA"), null);
});

test("the Claude request is structured, low effort, with a frozen cacheable system prompt", () => {
  const p = newsParams({ title: "x", source: "Reuters", tickers: ["NVDA"] });
  assert.equal(p.model, "claude-opus-5-5");
  assert.equal(p.output_config.effort, "low");
  assert.equal(p.output_config.format.type, "json_schema");
  for (const k of ["primary", "secondary", "sectors", "opinion"]) assert.ok(p.output_config.format.schema.required.includes(k), k);
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
                     [{ send: true, importance: 4, lean: "down", primary: ["TSLA"], secondary: [], sectors: ["Electric vehicles"], take: "A large recall.", opinion: "Costly but routine." }]);
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
  assert.equal(recent[0].opinion, "Costly but routine.");
  assert.deepEqual(recent[0].sectors, [{ name: "Electric vehicles", effect: "", why: "" }]);
  r = await runNews(ENV, { ...h2.deps });                         // same feed again: nothing new
  assert.equal(h2.tg.length, 1);
});

test("noise is judged but not sent, the daily cap stops Claude calls, and no KV means off", async () => {
  const h = harness([ITEM], [{ send: false, importance: 1, lean: "unclear", primary: [], secondary: [], sectors: [], take: "Recap.", opinion: "Noise." }]);
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

// ---------------------------------------------------------------- market mood
import { moodMessage, moodSlot, runMood, MOOD_FEEDS, LOOSE, pacificOffsetMs } from "../src/news.js";
import { answer } from "../src/bot.js";

const redditAtom = (titles) => `<feed xmlns="http://www.w3.org/2005/Atom">${titles.map((t, i) => `<entry><author><name>/u/x</name></author><content type="html">&lt;div&gt;long body&lt;/div&gt;</content><id>t3_${i}</id><link href="https://www.reddit.com/r/wallstreetbets/comments/${i}/"/><updated>${new Date(NOW - i * 600000).toISOString()}</updated><published>${new Date(NOW - i * 600000).toISOString()}</published><title>${t}</title></entry>`).join("")}</feed>`;
const MOOD = { mood: "nervous", score: 32, headline: "Dip buyers are losing their nerve", talk: ["Is the AI trade over", "Nvidia export curbs"],
               tickers: [{ name: "NVDA", effect: "bad", why: "Worried about China sales" }], noise: "Lots of loss screenshots.", view: "The crowd is late; most of this is already in prices." };

test("mood slots: an hour before the US open on weekdays, and the Singapore morning after a US day", () => {
  assert.equal(moodSlot(Date.parse("2026-11-10T13:30:20Z")), "2026-11-10T13:30");      // Tuesday
  assert.equal(moodSlot(Date.parse("2026-11-10T13:31:00Z")), null);
  assert.equal(moodSlot(Date.parse("2026-11-14T13:30:00Z")), null);                    // Saturday
  assert.equal(moodSlot(Date.parse("2026-11-14T01:00:00Z")), "2026-11-14T01:00");      // Saturday morning: recaps Friday
  assert.equal(moodSlot(Date.parse("2026-11-16T01:00:00Z")), null);                    // Monday morning: no US session before it
});

function moodHarness(now) {
  const store = {}, tg = [], calls = [];
  const kv = { get: async (k) => store[k] ?? null, put: async (k, v) => { store[k] = v; } };
  const create = async (params) => { calls.push(params); return { stop_reason: "end_turn", content: [{ type: "text", text: JSON.stringify(MOOD) }] }; };
  const client = { messages: { create }, beta: { messages: { create } } };
  const fetch = async (url, init) => {
    if (url.startsWith("https://api.telegram.org")) { tg.push(JSON.parse(init.body)); return { ok: true }; }
    if (url === MOOD_FEEDS.find((f) => f.id === "wsb").url) return { ok: true, status: 200, text: async () => redditAtom(Array.from({ length: 12 }, (_, i) => `Is the AI trade finally over? part ${i} of my losses`)) };
    if (url === MOOD_FEEDS.find((f) => f.id === "sa").url) return { ok: true, status: 200, text: async () => rss([{ title: "Nvidia slides as export curbs widen", guid: "s1", ts: NOW - 3600000 }]) };
    return { ok: false, status: 403, text: async () => "" };                         // Reddit often refuses cloud servers
  };
  return { store, tg, calls, deps: { fetch, client, kv, now: () => now } };
}

test("the mood brief is sent once per slot, from whatever sources answer", async () => {
  const at = Date.parse("2026-11-10T13:30:05Z");
  const h = moodHarness(at);
  let r = await runMood(ENV, h.deps);
  assert.equal(h.tg.length, 1);
  const text = h.tg[0].text;
  assert.match(text, /^🧠 <b>Market mood: NERVOUS<\/b> 😟 · 21:30 SGT/);
  assert.match(text, /fear ▓▓▓░░░░░░░ greed {2}\(32\/100\)/);
  assert.match(text, /💬 <b>My honest view:<\/b> The crowd is late/);
  assert.match(text, /🔴 <b>NVDA<\/b>: BEARISH\. Worried about China sales/);
  assert.match(text, /Reddit r\/wallstreetbets 12/);
  assert.match(text, /Seeking Alpha 1/);
  assert.match(h.calls[0].messages[0].content, /## Reddit r\/wallstreetbets\n- Is the AI trade finally over/);
  assert.equal(r.status.rstocks, "http 403");
  r = await runMood(ENV, h.deps);                                                    // the same minute again
  assert.equal(h.tg.length, 1);
  assert.deepEqual(await runMood(ENV, { ...h.deps, now: () => at + 5 * 60000 }), { skipped: "not a mood time" });
});

test("/mood builds the brief on request, and loose sources are marked on flashes", async () => {
  const h = moodHarness(Date.parse("2026-11-10T03:17:00Z"));
  let asked = 0;
  const mood = async () => { asked += 1; return runMood(ENV, h.deps, true); };
  const sent = await answer({ TG_TOKEN: "t", TG_CHAT: "111" }, { message_id: 1, chat: { id: 111 }, text: "/mood" }, { client: h.deps.client, fetch: h.deps.fetch, mood });
  assert.equal(asked, 1);
  assert.equal(h.tg.length, 1);                                                      // the brief itself, no extra reply
  assert.equal(sent.length, 0);
  assert.ok(LOOSE.includes("seeking alpha"));
  const text = flash({ title: "Nvidia cuts guidance", source: "Seeking Alpha", ts: NOW, link: "", tickers: ["NVDA"] },
    { importance: 4, lean: "down", primary: ["NVDA"], secondary: [], sectors: [], take: "x", opinion: "y" }, {}, NOW);
  assert.match(text, /Seeking Alpha \(less reliable source\)/);
});

test("Bing News items: real link, real source, and Pacific clock time corrected to UTC", () => {
  const xml = `<rss><channel><item><title>Nvidia cuts guidance</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=abc&amp;url=https%3a%2f%2fwww.fool.com%2fx%2f&amp;c=1&amp;mkt=en-us</link><pubDate>Tue, 10 Nov 2026 06:15:00 GMT</pubDate><News:Source>The Motley Fool on MSN</News:Source></item></channel></rss>`;
  const [it] = parseFeed(xml, "bnews");
  assert.equal(it.link, "https://www.fool.com/x/");
  assert.equal(it.source, "The Motley Fool");
  assert.equal(new Date(it.ts).toISOString(), "2026-11-10T14:15:00.000Z");            // winter: 8 hours
  assert.equal(pacificOffsetMs(Date.parse("2026-07-01T12:00:00Z")) / 3600000, 7);      // summer: 7 hours
  assert.equal(it.id, "bnews:https://www.fool.com/x/");                                // stable across fetches
});

// ---------------------------------------------------------------- major news only, no repeats
import { similar, MAX_SENDS_PER_DAY } from "../src/news.js";
const V = (o) => ({ send: true, importance: 4, repeat: false, lean: "down", primary: [{ name: "NVDA", effect: "bad", why: "x" }], secondary: [], sectors: [], take: "t", opinion: "o", ...o });
const prior = (recent, extra = {}) => JSON.stringify({ seen: [], day: "2026-11-10", count: 0, recent, ...extra });

test("only major news is sent: a 3 is judged but stays quiet", async () => {
  const h = harness([ITEM], [V({ importance: 3 })]);
  h.store.news = prior([]);
  await runNews(ENV, h.deps);
  assert.equal(h.calls.length, 1);
  assert.equal(h.tg.length, 0);
  assert.match(h.calls[0].system[0].text, /only wants major news/);
});

test("the same story from another outlet is dropped without asking Claude", async () => {
  assert.ok(similar("Nvidia falls 4% as board probe of Jensen Huang weighs on shares", "Nvidia shares slide as board opens probe into CEO Jensen Huang"));
  assert.ok(!similar("Nvidia cuts revenue guidance", "Tesla recall hits 1 million cars"));
  const h = harness([{ title: "Nvidia slashes revenue guidance for next quarter - Bloomberg", guid: "b1", ts: NOW - 30000 }], []);
  h.store.news = prior([{ ts: NOW - 3600000, title: "Nvidia cuts revenue guidance for next quarter", tickers: [] }]);
  await runNews(ENV, h.deps);
  assert.equal(h.calls.length, 0);
  assert.equal(h.tg.length, 0);
});

test("Claude sees what was already sent; a repeat, a second 4 on the same stock, or the daily cap stay quiet", async () => {
  let h = harness([ITEM], [V({ repeat: true, importance: 5 })]);
  h.store.news = prior([{ ts: NOW - 3600000, title: "Board opens probe into Jensen Huang", tickers: [] }]);
  await runNews(ENV, h.deps);
  assert.match(h.calls[0].messages[0].content, /already sent in the last 12 hours:\n- Board opens probe into Jensen Huang/);
  assert.equal(h.tg.length, 0);

  h = harness([ITEM], [V({})]);                                   // NVDA flashed 2 hours ago: a 4 waits
  h.store.news = prior([{ ts: NOW - 2 * 3600000, title: "Something else entirely about chips", tickers: [{ name: "NVDA" }] }]);
  await runNews(ENV, h.deps);
  assert.equal(h.tg.length, 0);
  h = harness([ITEM], [V({ importance: 5 })]);                    // ... but a 5 gets through
  h.store.news = prior([{ ts: NOW - 2 * 3600000, title: "Something else entirely about chips", tickers: [{ name: "NVDA" }] }]);
  await runNews(ENV, h.deps);
  assert.equal(h.tg.length, 1);

  h = harness([ITEM], [V({})]);                                   // six flashes already today
  h.store.news = prior([], { sends: MAX_SENDS_PER_DAY });
  await runNews(ENV, h.deps);
  assert.equal(h.tg.length, 0);
});
