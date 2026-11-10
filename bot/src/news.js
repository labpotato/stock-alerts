// Fast news flashes for Dongmin's watchlist (2026-11-10).
// Every minute the Worker's cron reads a few free feeds (Yahoo Finance, Google News, SEC 8-K, the
// Fed), keeps only headlines that look market-moving for his names, asks Claude for one line on
// what it means, and sends a short flash to Telegram with what his own rule says about the ticker.
// Pure logic lives here so it is tested without the network. State (which headlines were already
// handled, today's count) lives in one Workers KV key; without the NEWS_KV binding news is off.

export const NEWS_MODEL = "claude-opus-5-5";   // override with the NEWS_MODEL var (e.g. claude-haiku-5-5)
export const MAX_AGE_MIN = 20;                 // older headlines are not news any more
export const MAX_PER_RUN = 3;
export const MAX_PER_DAY = 40;                 // also caps Claude calls per day
export const SEEN_KEEP = 800;

// What each watched name is called in headlines. ETFs are matched through the market words below.
export const WATCH = {
  NVDA: ["nvidia", "jensen huang"],
  GOOGL: ["alphabet", "google", "waymo", "deepmind"],
  TSLA: ["tesla", "elon musk"],
  PLTR: ["palantir"],
  BE: ["bloom energy"],
  SOXX: ["semiconductor", "chipmaker", "chip stocks", "tsmc", "asml", "broadcom", "micron"],
  QQQ: ["nasdaq"],
  VOO: ["s&p 500", "wall street", "stock market", "stocks"],
  GLDM: ["gold price", "gold hits", "gold falls", "gold rises", "bullion"],
  "005930.KS": ["samsung electronics", "삼성전자"],
  "000660.KS": ["sk hynix", "sk하이닉스", "하이닉스"],
  "USD/KRW": ["원/달러", "원달러", "환율", "korean won"],
};
export const MACRO = ["federal reserve", "fomc", "powell", "rate cut", "rate hike", "interest rate", "cpi",
  "inflation", "jobs report", "payrolls", "unemployment", "tariff", "treasury yield", "recession",
  "shutdown", "bank of korea", "기준금리", "연준"];
// A headline must also carry one of these: the words of events, not opinion pieces.
export const TRIGGERS = ["earnings", "results", "revenue", "profit", "guidance", "forecast", "outlook", "beats",
  "misses", "raises", "cuts", "slashes", "downgrade", "upgrade", "price target", "acquire", "acquisition",
  "merger", "deal", "buyback", "dividend", "split", "lawsuit", "sues", "probe", "investigation", "sec ",
  "antitrust", "ban", "export", "recall", "halt", "plunge", "tumble", "sink", "slump", "soar", "surge",
  "jump", "rally", "record", "crash", "selloff", "sell-off", "resign", "steps down", "ceo", "layoff",
  "bankrupt", "default", "approval", "contract", "order", "launch", "delay", "strike", "sanction",
  "hikes", "holds rates", "decision", "minutes", "속보", "실적", "급등", "급락", "인수", "합병", "공시"];
export const SOURCES = ["reuters", "bloomberg", "wsj", "wall street journal", "cnbc", "financial times", "ft.com",
  "associated press", "ap news", "barron", "marketwatch", "yahoo finance", "the information", "nikkei",
  "investor's business daily", "business wire", "pr newswire", "globenewswire", "sec", "federal reserve",
  "연합뉴스", "한국경제", "매일경제", "yonhap", "korea herald", "seeking alpha", "benzinga", "the verge", "axios",
  "investopedia", "motley fool", "zacks"];
export const CIK = { "1045810": "NVDA", "1652044": "GOOGL", "1318605": "TSLA", "1321655": "PLTR", "1664703": "BE" };

const UA = "stock-alerts-news (+https://github.com/labpotato/stock-alerts)";
const gnews = (q, loc) => `https://news.google.com/rss/search?q=${encodeURIComponent(q + " when:1h")}&${loc}`;
// Google News refuses requests from Cloudflare Workers (HTTP 503, seen 2026-11-10), so each Google
// search has a Bing News twin. Whichever answers is used; the same headline is only handled once.
const bing = (q, extra = "") => `https://www.bing.com/news/search?q=${encodeURIComponent(q)}&qft=${encodeURIComponent('sortbydate="1"')}&format=rss${extra}`;
const Q_STOCKS = 'Nvidia OR Tesla OR Alphabet OR Palantir OR "Bloom Energy"';
const Q_MACRO = '"Federal Reserve" OR FOMC OR CPI OR "jobs report" OR tariffs OR "stock market"';
const Q_KOREA = "삼성전자 OR SK하이닉스 OR 환율";
export const FEEDS = [
  { id: "yahoo", url: "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,GOOGL,TSLA,PLTR,BE&region=US&lang=en-US" },
  { id: "gnews", url: gnews(Q_STOCKS, "hl=en-US&gl=US&ceid=US:en") },
  { id: "bnews", url: bing(Q_STOCKS) },
  { id: "macro", url: gnews(Q_MACRO, "hl=en-US&gl=US&ceid=US:en") },
  { id: "bmacro", url: bing(Q_MACRO) },
  { id: "korea", url: gnews(Q_KOREA, "hl=ko&gl=KR&ceid=KR:ko") },
  { id: "bkorea", url: bing(Q_KOREA, "&mkt=ko-KR") },
  { id: "sec", url: "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom" },
  { id: "fed", url: "https://www.federalreserve.gov/feeds/press_all.xml", always: true },
  // Less formal sources (Dongmin 2026-11-10): faster and noisier. Their flashes are marked.
  { id: "sa", url: "https://seekingalpha.com/market_currents.xml", source: "Seeking Alpha" },
  { id: "inv", url: bing("site:investopedia.com (stocks OR markets)"), source: "Investopedia" },
];
export const LOOSE = ["seeking alpha", "benzinga", "reddit", "investopedia", "motley fool", "zacks"];

// What people are saying: read only for the market-mood brief, never for single flashes.
// Reddit answers 429 when several feeds are asked for at once, so its feeds are read one after another.
const REDDIT_UA = "web:stock-alerts-news:v1 (personal market-mood reader)";
export const MOOD_FEEDS = [
  { id: "wsb", url: "https://www.reddit.com/r/wallstreetbets/hot/.rss?limit=25", source: "Reddit r/wallstreetbets", ua: REDDIT_UA },
  { id: "rstocks", url: "https://www.reddit.com/r/stocks+investing/hot/.rss?limit=40", source: "Reddit r/stocks and r/investing", ua: REDDIT_UA },
  { id: "sa", url: "https://seekingalpha.com/market_currents.xml", source: "Seeking Alpha" },
  { id: "inv", url: bing("site:investopedia.com (stocks OR markets)"), source: "Investopedia" },
  { id: "press", url: bing('"stock market" OR "Wall Street" OR stocks') },
  { id: "gpress", url: gnews('"stock market" OR "Wall Street" OR stocks', "hl=en-US&gl=US&ceid=US:en").replace("when%3A1h", "when%3A6h") },
];

/** Run fn over feeds: ordinary feeds together, Reddit's one after another. */
async function eachFeed(feeds, fn) {
  const serial = feeds.filter((f) => f.ua), rest = feeds.filter((f) => !f.ua);
  return Promise.all([...rest.map(fn), (async () => { for (const f of serial) await fn(f); })()]);
}
export const MOOD_TIMES = ["13:30", "01:00"];   // UTC: an hour before the US open (9:30 pm Singapore), and 9 am Singapore

// ---------------------------------------------------------------- parsing (no XML library)
const ENT = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&apos;": "'" };
const clean = (s) => (s || "").replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1").replace(/<[^>]+>/g, " ")
  .replace(/&(amp|lt|gt|quot|#39|apos);/g, (m) => ENT[m]).replace(/\s+/g, " ").trim();
const tag = (block, name) => {
  const a = block.indexOf("<" + name);
  if (a < 0) return "";
  const b = block.indexOf(">", a), c = block.indexOf("</" + name + ">", b);
  return b < 0 || c < 0 ? "" : block.slice(b + 1, c);
};

/** RSS <item> or Atom <entry> blocks -> [{id, title, link, ts, source}] */
/** Hours US Pacific time is behind UTC at this moment (8 in winter, 7 in summer), in ms. */
export function pacificOffsetMs(ts) {
  const y = new Date(ts).getUTCFullYear();
  const nthSunday = (month, n) => { const d = new Date(Date.UTC(y, month, 1)); return 1 + ((7 - d.getUTCDay()) % 7) + 7 * (n - 1); };
  const start = Date.UTC(y, 2, nthSunday(2, 2), 10), end = Date.UTC(y, 10, nthSunday(10, 1), 9);
  return (ts >= start - 8 * 3600000 && ts < end - 7 * 3600000 ? 7 : 8) * 3600000;
}

export function parseFeed(xml, feedId, minTs = 0, fixedSource = "") {
  const out = [];
  const isBing = xml.indexOf("bing.com/news/apiclick") >= 0;
  const atom = xml.indexOf("<entry") >= 0 && xml.indexOf("<item") < 0;
  const open = atom ? "<entry" : "<item", close = atom ? "</entry>" : "</item>";
  let i = 0;
  for (;;) {
    const a = xml.indexOf(open, i);
    if (a < 0) break;
    const b = xml.indexOf(close, a);
    if (b < 0) break;
    const block = xml.slice(a, b);
    i = b + close.length;
    // Read the time first and skip old items before any other work: the Worker's free plan
    // allows about 10 ms of CPU per run.
    let ts = Date.parse(clean(tag(block, atom ? "updated" : "pubDate")) || clean(tag(block, "published")));
    if (isBing) ts += pacificOffsetMs(ts);        // Bing prints US Pacific clock time but labels it GMT
    if (!Number.isFinite(ts) || ts < minTs) continue;
    let title = clean(tag(block, "title"));
    let link = clean(tag(block, "link"));
    if (!link) {
      const m = block.match(/<link[^>]*href="([^"]+)"/);
      link = m ? m[1].replace(/&amp;/g, "&") : "";
    }
    let source = clean(tag(block, "source"));
    if (isBing) {
      const m = link.match(/[?&]url=([^&]+)/);
      if (m) { try { link = decodeURIComponent(m[1]); } catch { /* keep the Bing link */ } }
      source = clean(tag(block, "News:Source")).replace(/ on MSN$/i, "");
    }
    const id = (isBing ? link : clean(tag(block, atom ? "id" : "guid"))) || link || title;
    if (feedId === "sec") {                       // "8-K - Tesla, Inc. (0001318605) (Filer)"
      const m = title.match(/\((\d{10})\)/);
      const t = m && CIK[String(Number(m[1]))];
      if (!t) continue;
      const items = [...clean(clean(tag(block, "summary"))).matchAll(/Item [\d.]+: ([^I]+?)(?= Item|$)/g)].map((x) => x[1].trim());
      title = `${t} filed an 8-K with the SEC` + (items.length ? `: ${items.slice(0, 3).join("; ")}` : "");
      source = "SEC";
    } else if (feedId === "fed") {
      source = "Federal Reserve";
    } else if (fixedSource) {
      source = fixedSource;
    } else if (!source) {
      const m = title.match(/ - ([^-]{2,40})$/);   // Google News: "Headline - Source"
      source = m ? m[1].trim() : (feedId === "yahoo" ? "Yahoo Finance" : "");
    }
    if (source && title.endsWith(" - " + source)) title = title.slice(0, -(source.length + 3));
    if (title) out.push({ id: `${feedId}:${id}`.slice(0, 120), title, link, ts, source, feed: feedId });
  }
  return out;
}

// ---------------------------------------------------------------- the rule filter
const norm = (s) => s.toLowerCase().replace(/[^a-z0-9가-힣%&/ ]+/g, " ").replace(/\s+/g, " ").trim();
const hasWord = (text, w) => {
  if (/[가-힣&/]/.test(w) || w.endsWith(" ")) return text.includes(w.trim());
  const i = text.indexOf(w);
  if (i < 0) return false;
  const before = i === 0 ? " " : text[i - 1], after = text[i + w.length] || " ";
  return !/[a-z0-9]/.test(before) && !/[a-z0-9]/.test(after === "s" ? (text[i + w.length + 1] || " ") : after);
};

/** Which watched names a headline is about, and whether it reads like an event. */
export function classify(item) {
  const t = " " + norm(item.title) + " ";
  const tickers = Object.entries(WATCH).filter(([, ws]) => ws.some((w) => hasWord(t, w))).map(([k]) => k);
  const macro = MACRO.some((w) => hasWord(t, w));
  const trigger = TRIGGERS.some((w) => hasWord(t, w));
  const src = (item.source || "").toLowerCase();
  const trusted = SOURCES.some((s) => src.includes(s));
  const official = item.feed === "sec" || item.feed === "fed";
  const specific = tickers.some((k) => !["VOO", "QQQ"].includes(k));
  const pass = official || (trusted && trigger && (specific || macro || tickers.length > 0));
  return { tickers, macro, trigger, trusted, pass };
}

export function pickCandidates(items, seen, now) {
  const out = [], titles = new Set();
  for (const it of items.sort((a, b) => b.ts - a.ts)) {
    if (seen.has(it.id) || now - it.ts > MAX_AGE_MIN * 60000 || it.ts - now > 5 * 60000) continue;
    const key = norm(it.title).slice(0, 60);
    if (titles.has(key) || seen.has("t:" + key)) continue;
    const c = classify(it);
    if (!c.pass) continue;
    titles.add(key);
    out.push({ ...it, ...c, tkey: "t:" + key });
  }
  return out;
}

// ---------------------------------------------------------------- his rule, from status.json
const PLAIN = { "WATCH BUY": "below its normal range", "WATCH SELL": "above its normal range", BUY: "BUY signal today",
  SELL: "SELL signal today", NONE: "inside its normal range" };
export function ruleLine(status, ticker) {
  const v = status && status.tickers && status.tickers[ticker];
  if (!v) return null;
  const mom = { "WATCH BUY": "sold off hard", "WATCH SELL": "overheated", BUY: "BUY signal today", SELL: "SELL signal today" }[v.momentum];
  const price = typeof v.price === "number" ? `$${v.price.toLocaleString("en-US", { maximumFractionDigits: 2 })}` : "";
  const day = typeof v.day_pct === "number" ? ` (${v.day_pct >= 0 ? "+" : ""}${v.day_pct.toFixed(1)}% today)` : "";
  return `${v.label || ticker} ${price}${day}: ${PLAIN[v.keltner] || v.keltner}, momentum ${Math.round(v.rsi)}` + (mom ? ` (${mom})` : "");
}

// ---------------------------------------------------------------- Claude: one line on what it means
export const NEWS_SYSTEM = `You write short market flashes for Dongmin, a private investor in Singapore, inside his Telegram alert bot. He holds US stocks and ETFs: NVDA, GOOGL, TSLA, PLTR, BE, the AI/robotics ETFs SOXX, AIPO, BOTZ and IQQ, the index ETFs VOO and QQQ, sector ETFs XLV, XLF, XLB, XLP, ITA, gold (GLDM), and he watches Samsung Electronics (005930.KS) and SK hynix (000660.KS) and the SGD, USD and KRW exchange rates.

For each headline decide whether it is real, new, market-moving information, and if so tell him what it means, which tickers and sectors it hits, and what you honestly think.

Rules
- importance: 5 = likely to move the name or the market several percent (earnings surprise, guidance change, major regulatory or legal action, a Fed decision, a large deal); 4 = clearly material; 3 = worth knowing today; 2 = minor; 1 = noise, opinion, a recap of a move that already happened, a listicle, a price-target shuffle with nothing new, or not about anything he holds or watches.
- send is true only for importance 3 or more.
- take: one sentence, at most 28 words, plain words. What happened, in effect.
- opinion: your honest view in one or two sentences, at most 45 words. Be direct and take a side: is this a big deal or overblown, likely priced in already or not, a one-day story or one that changes the outlook. Say how confident you are when it is a judgement call. If the headline alone is too thin to judge, say so plainly instead of guessing. This is your reading of the news, not an order: do not tell him to buy or sell, because his own rules decide trades.
- primary: the tickers hit directly, most affected first, at most 3. Use tickers from his list when they are the ones hit; otherwise the real ticker of the company in the headline.
- secondary: tickers hit indirectly (suppliers, customers, competitors, ETFs that hold the name, the same theme), at most 4, preferring the ones he holds. Empty if there are none.
- sectors: the sectors or themes affected, at most 3, in plain words (for example "Semiconductors", "AI infrastructure", "Banks", "Gold", "Korean exporters").
- For every primary, secondary and sector entry give effect: "good" or "bad" for that name specifically, or "mixed" only when it truly cuts both ways. The same news is often good for one name and bad for another (a competitor's loss is a rival's gain; a strong dollar hurts exporters and helps importers): judge each one on its own. Add why: at most 12 plain words on how it is hit.
- lean: "up", "down" or "unclear" for the primary tickers.
- Never invent a number or a fact that is not in the headline. Knowledge of which companies are suppliers, customers or competitors is fine.
- Write take and opinion in English, unless the headline is in Korean: then write them in Korean.`;

const HIT = {
  type: "object",
  properties: { name: { type: "string" }, effect: { type: "string", enum: ["good", "bad", "mixed"] }, why: { type: "string" } },
  required: ["name", "effect", "why"],
  additionalProperties: false,
};
/** One affected name, from either the current {name, effect, why} or an older plain string. */
const asHit = (x) => (typeof x === "string" ? { name: x, effect: "", why: "" } : { name: String(x.name || ""), effect: x.effect || "", why: x.why || "" });
const EFFECT = { good: "🟢", bad: "🔴", mixed: "🟡" };
const hitLine = (h) => `${EFFECT[h.effect] || "•"} <b>${esc(h.name)}</b>${h.effect ? `: ${h.effect.toUpperCase()}` : ""}${h.why ? `. ${esc(h.why)}` : ""}`;

export const NEWS_SCHEMA = {
  type: "object",
  properties: {
    send: { type: "boolean" },
    importance: { type: "integer" },
    lean: { type: "string", enum: ["up", "down", "unclear"] },
    primary: { type: "array", items: HIT },
    secondary: { type: "array", items: HIT },
    sectors: { type: "array", items: HIT },
    take: { type: "string" },
    opinion: { type: "string" },
  },
  required: ["send", "importance", "lean", "primary", "secondary", "sectors", "take", "opinion"],
  additionalProperties: false,
};

export function newsParams(item, model) {
  return {
    model: model || NEWS_MODEL,
    max_tokens: 2000,
    system: [{ type: "text", text: NEWS_SYSTEM, cache_control: { type: "ephemeral" } }],
    output_config: { effort: "low", format: { type: "json_schema", schema: NEWS_SCHEMA } },
    messages: [{ role: "user", content: `Source: ${item.source || "unknown"}\nHeadline: ${item.title}\nHis tickers matched by keyword: ${item.tickers.join(", ") || "none (macro)"}` }],
  };
}

export async function judge(client, item, model) {
  const params = newsParams(item, model);
  const opts = { timeout: 20000, maxRetries: 0 };
  // Opus, Sonnet and Fable can decline a request; the server-side fallback re-runs it. Haiku has none.
  const res = /haiku/.test(params.model)
    ? await client.messages.create(params, opts)
    : await client.beta.messages.create({ ...params, betas: ["server-side-fallback-2026-07-01"], fallbacks: "default" }, opts);
  if (res.stop_reason === "refusal") return null;
  const text = (res.content || []).filter((b) => b.type === "text").map((b) => b.text).join("");
  try {
    const j = JSON.parse(text);
    return j && typeof j.take === "string" ? j : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- the flash
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const sgTime = (ts) => new Date(ts + 8 * 3600000).toISOString().slice(11, 16);

export function flash(item, j, status, now) {
  const icon = j.importance >= 5 ? "🚨" : j.importance === 4 ? "⚡" : "📰";
  const arrow = { up: "🟢", down: "🔴", unclear: "⚪" }[j.lean] || "⚪";
  const primH = (j.primary && j.primary.length ? j.primary : j.tickers && j.tickers.length ? j.tickers : item.tickers).map(asHit).slice(0, 3);
  const prim = primH.map((h) => h.name);
  const secH = (j.secondary || []).map(asHit).filter((h) => !prim.includes(h.name)).slice(0, 4);
  const sec = secH.map((h) => h.name);
  const secsH = (j.sectors || []).map(asHit).slice(0, 3);
  const mins = Math.max(0, Math.round((now - item.ts) / 60000));
  const loose = LOOSE.some((x) => (item.source || "").toLowerCase().includes(x));
  const lines = [`${icon} <b>${esc(prim.join(" · ") || "MARKET")}</b> · ${esc(item.source || "news")}${loose ? " (less reliable source)" : ""} · ${sgTime(item.ts)} SGT (${mins} min ago)`,
    `<b>${esc(item.title)}</b>`, "", `${arrow} ${esc(j.take)}`];
  if (j.opinion) lines.push("", `💬 <b>My honest view:</b> ${esc(j.opinion)}`);
  if (primH.length) lines.push("", "🎯 <b>Directly hit</b>", ...primH.map(hitLine));
  if (secH.length) lines.push("", "↪️ <b>Also affected</b>", ...secH.map(hitLine));
  if (secsH.length) lines.push("", "🏭 <b>Sectors</b>", ...secsH.map(hitLine));
  const rules = prim.concat(sec).map((t) => ruleLine(status, t)).filter(Boolean).slice(0, 3);
  if (rules.length) lines.push("", ...rules.map((r) => `📊 Your rule: ${esc(r)}`));
  if (item.link) lines.push("", `<a href="${esc(item.link)}">Read it</a>`);
  return lines.join("\n");
}

// ---------------------------------------------------------------- one cron run
/** deps: { fetch, client, kv, now }. Returns the list of flashes it sent. */
export async function runNews(env, deps) {
  const { fetch: doFetch, client, kv, now = () => Date.now() } = deps;
  if (!kv) return { off: "no NEWS_KV binding" };
  const t = now(), day = new Date(t).toISOString().slice(0, 10);
  let st;
  try { st = JSON.parse((await kv.get("news")) || "null"); } catch { st = null; }
  const first = !st;
  st = st || { seen: [], day, count: 0 };
  if (st.day !== day) { st.day = day; st.count = 0; }
  const seen = new Set(st.seen);

  const texts = await Promise.all(FEEDS.map(async (f) => {
    try {
      const r = await doFetch(f.url, { headers: { "user-agent": UA, accept: "application/rss+xml, application/atom+xml, application/xml" } });
      return r.ok ? parseFeed(await r.text(), f.id, t - MAX_AGE_MIN * 60000, f.source) : [];
    } catch { return []; }
  }));
  const cands = pickCandidates(texts.flat(), seen, t);
  if (!cands.length) return { sent: [], candidates: 0 };

  const sent = [];
  const mark = (c) => { st.seen.push(c.id, c.tkey); };
  if (first) {
    cands.forEach(mark);                          // first run: learn what is already out, send nothing
  } else {
    let status = null;
    for (const c of cands) {
      if (sent.length >= MAX_PER_RUN || st.count >= (Number(env.NEWS_MAX_PER_DAY) || MAX_PER_DAY)) break;
      mark(c);
      st.count += 1;                              // counts Claude calls, so the day's cost is capped
      let j = null;
      try { j = await judge(client, c, env.NEWS_MODEL); } catch (e) { console.log("news judge error:", e && e.name, e && e.status); }
      if (!j || !j.send || j.importance < 3) continue;
      if (!status && env.STATUS_URL) {
        try { const r = await doFetch(env.STATUS_URL); status = r.ok ? await r.json() : {}; } catch { status = {}; }
      }
      const text = flash(c, j, status, t);
      await doFetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: env.TG_CHAT, text, parse_mode: "HTML", link_preview_options: { is_disabled: true } }),
      });
      sent.push(text);
      st.recent = [{ ts: c.ts, title: c.title, source: c.source, link: c.link, tickers: (j.primary || []).map(asHit).slice(0, 3),
                     secondary: (j.secondary || []).map(asHit).slice(0, 4), sectors: (j.sectors || []).map(asHit).slice(0, 3),
                     take: j.take, opinion: j.opinion, lean: j.lean, importance: j.importance }, ...(st.recent || [])].slice(0, 30);
    }
  }
  st.seen = st.seen.slice(-SEEN_KEEP);
  await kv.put("news", JSON.stringify(st));
  return { sent, candidates: cands.length, first };
}


/** The last flashes, for the dashboard. Public news only. */
export async function recentNews(kv) {
  if (!kv) return [];
  try { return (JSON.parse((await kv.get("news")) || "{}").recent) || []; } catch { return []; }
}


// ---------------------------------------------------------------- market mood brief
export const MOOD_SYSTEM = `You read what investors are saying right now and write a short market-mood brief for Dongmin, a private investor in Singapore, inside his Telegram alert bot. He holds NVDA, GOOGL, TSLA, PLTR, BE, the ETFs SOXX, AIPO, BOTZ, IQQ, VOO, QQQ, XLV, XLF, XLB, XLP, ITA, gold (GLDM), and watches Samsung Electronics and SK hynix.

You get post and headline titles from Reddit (r/wallstreetbets, r/stocks, r/investing), Seeking Alpha, Investopedia and the financial press. These are noisy and partly unreliable: your job is to read the crowd, not to trust it.

Rules
- mood: one of "fearful", "nervous", "mixed", "hopeful", "greedy" for the general mentality. score: 0 (extreme fear) to 100 (extreme greed).
- headline: the mood in at most 12 plain words.
- talk: the 3 or 4 things people are talking about most, each at most 16 words, most common first.
- tickers: up to 6 tickers that come up most. name is the ticker; effect is "good" if the crowd is bullish on it, "bad" if bearish, "mixed" if split; why is how people feel about it, at most 12 words. Prefer his holdings when they are being discussed.
- noise: one sentence, at most 30 words, on what looks like hype, panic or repetition rather than information.
- view: your honest opinion in at most 45 words. Take a side: is the crowd right, late, or overreacting? Say how sure you are. This is a reading of sentiment, not advice to buy or sell.
- Use only what the titles show. Do not invent prices, numbers or events. If the titles are too few or one-sided to judge, say so in view.
- Plain words, no jargon.`;

export const MOOD_SCHEMA = {
  type: "object",
  properties: {
    mood: { type: "string", enum: ["fearful", "nervous", "mixed", "hopeful", "greedy"] },
    score: { type: "integer" },
    headline: { type: "string" },
    talk: { type: "array", items: { type: "string" } },
    tickers: { type: "array", items: HIT },
    noise: { type: "string" },
    view: { type: "string" },
  },
  required: ["mood", "score", "headline", "talk", "tickers", "noise", "view"],
  additionalProperties: false,
};

/** Which mood slot (UTC "HH:MM") is due at time t, or null. Slots follow US trading days. */
export function moodSlot(t, times = MOOD_TIMES) {
  const d = new Date(t), hm = d.toISOString().slice(11, 16), wd = d.getUTCDay();   // 0 = Sunday
  if (!times.includes(hm)) return null;
  const morning = Number(hm.slice(0, 2)) < 12;                 // the morning slot recaps the previous US day
  if (morning ? (wd === 0 || wd === 1) : (wd === 0 || wd === 6)) return null;
  return d.toISOString().slice(0, 16);
}

export async function gatherMood(doFetch, t) {
  const seen = new Set(), bySource = {}, status = {};
  await eachFeed(MOOD_FEEDS, async (f) => {
    try {
      const r = await doFetch(f.url, { headers: { "user-agent": f.ua || UA, accept: "application/atom+xml, application/rss+xml, application/xml" } });
      if (!r.ok) { status[f.id] = "http " + r.status; return; }
      const items = parseFeed(await r.text(), f.id, t - 24 * 3600000, f.source).slice(0, 25);
      status[f.id] = items.length + " items";
      for (const it of items) {
        const key = norm(it.title).slice(0, 60);
        if (seen.has(key) || it.title.length < 12) continue;
        seen.add(key);
        (bySource[it.source || "Press"] ||= []).push(it.title.slice(0, 160));
      }
    } catch (e) { status[f.id] = "error"; }
  });
  return { bySource, status, count: seen.size };
}

export function moodParams(bySource, model) {
  const body = Object.entries(bySource).map(([src, titles]) => `## ${src}\n` + titles.slice(0, 30).map((x) => "- " + x).join("\n")).join("\n\n");
  return {
    model: model || NEWS_MODEL,
    max_tokens: 3000,
    system: [{ type: "text", text: MOOD_SYSTEM, cache_control: { type: "ephemeral" } }],
    output_config: { effort: "low", format: { type: "json_schema", schema: MOOD_SCHEMA } },
    messages: [{ role: "user", content: "Titles from the last day, grouped by where they were posted:\n\n" + body }],
  };
}

const MOOD_ICON = { fearful: "😱", nervous: "😟", mixed: "😐", hopeful: "🙂", greedy: "🤑" };
export function moodMessage(j, g, t) {
  const n = Math.max(0, Math.min(10, Math.round(j.score / 10)));
  const srcs = Object.entries(g.bySource).map(([k, v]) => `${k} ${v.length}`).join(" · ");
  const lines = [`🧠 <b>Market mood: ${esc(j.mood.toUpperCase())}</b> ${MOOD_ICON[j.mood] || ""} · ${sgTime(t)} SGT`,
    `fear ${"▓".repeat(n)}${"░".repeat(10 - n)} greed  (${j.score}/100)`, `<b>${esc(j.headline)}</b>`, "",
    "<b>What people are talking about</b>", ...j.talk.slice(0, 4).map((x) => "• " + esc(x))];
  if (j.tickers.length) lines.push("", "<b>Names coming up</b>", ...j.tickers.slice(0, 6).map((x) => hitLine(asHit(x)).replace(/: (GOOD|BAD|MIXED)/, (m, e) => `: ${{ GOOD: "BULLISH", BAD: "BEARISH", MIXED: "SPLIT" }[e]}`)));
  lines.push("", `🔇 <b>Probably noise:</b> ${esc(j.noise)}`, "", `💬 <b>My honest view:</b> ${esc(j.view)}`, "",
    `<i>Read from ${g.count} post and headline titles (${esc(srcs)}). Crowd talk, partly unreliable; not advice.</i>`);
  return lines.join("\n");
}

/** Build and send the mood brief. force = on request (/mood); otherwise only at a due slot, once. */
export async function runMood(env, deps, force = false) {
  const { fetch: doFetch, client, kv, now = () => Date.now() } = deps;
  const t = now();
  const slot = force ? "manual" : moodSlot(t, env.MOOD_TIMES ? String(env.MOOD_TIMES).split(",").map((x) => x.trim()) : MOOD_TIMES);
  if (!slot) return { skipped: "not a mood time" };
  if (!force) {
    if (!kv) return { off: "no NEWS_KV binding" };
    if ((await kv.get("mood")) === slot) return { skipped: "already sent" };
    await kv.put("mood", slot);                    // before the work, so a slow run cannot send twice
  }
  const g = await gatherMood(doFetch, t);
  let text;
  if (g.count < 8) {
    text = `🧠 <b>Market mood</b>\nI couldn't read enough posts to judge the mood right now (${g.count} titles). Sources: ${esc(JSON.stringify(g.status))}`;
  } else {
    const params = moodParams(g.bySource, env.NEWS_MODEL);
    const opts = { timeout: 25000, maxRetries: 0 };
    const res = /haiku/.test(params.model) ? await client.messages.create(params, opts)
      : await client.beta.messages.create({ ...params, betas: ["server-side-fallback-2026-07-01"], fallbacks: "default" }, opts);
    let j = null;
    try { j = JSON.parse((res.content || []).filter((b) => b.type === "text").map((b) => b.text).join("")); } catch { j = null; }
    if (!j || res.stop_reason === "refusal") return { error: "no usable answer" };
    text = moodMessage(j, g, t);
  }
  await doFetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TG_CHAT, text, parse_mode: "HTML", link_preview_options: { is_disabled: true } }),
  });
  return { sent: text, status: g.status };
}

/** Which feeds answer from the Worker, for checking after a deploy. No secrets, public feeds only. */
export async function feedCheck(doFetch, t) {
  const out = {};
  const all = [...FEEDS.map((f) => ({ ...f, id: "flash:" + f.id })), ...MOOD_FEEDS.map((f) => ({ ...f, id: "mood:" + f.id }))];
  await eachFeed(all, async (f) => {
    try {
      const r = await doFetch(f.url, { headers: { "user-agent": f.ua || UA } });
      const items = r.ok ? parseFeed(await r.text(), f.id.split(":")[1], t - 24 * 3600000, f.source) : [];
      out[f.id] = { http: r.status, items: items.length, newest_min_ago: items.length ? Math.round((t - Math.max(...items.map((x) => x.ts))) / 60000) : null };
    } catch (e) { out[f.id] = { error: String(e && e.message).slice(0, 80) }; }
  });
  return out;
}
