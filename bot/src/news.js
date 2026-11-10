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
  "연합뉴스", "한국경제", "매일경제", "yonhap", "korea herald", "seeking alpha", "benzinga", "the verge", "axios"];
export const CIK = { "1045810": "NVDA", "1652044": "GOOGL", "1318605": "TSLA", "1321655": "PLTR", "1664703": "BE" };

const UA = "stock-alerts-news (+https://github.com/labpotato/stock-alerts)";
const gnews = (q, loc) => `https://news.google.com/rss/search?q=${encodeURIComponent(q + " when:1h")}&${loc}`;
export const FEEDS = [
  { id: "yahoo", url: "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,GOOGL,TSLA,PLTR,BE&region=US&lang=en-US" },
  { id: "gnews", url: gnews('Nvidia OR Tesla OR Alphabet OR Palantir OR "Bloom Energy"', "hl=en-US&gl=US&ceid=US:en") },
  { id: "macro", url: gnews('"Federal Reserve" OR FOMC OR CPI OR "jobs report" OR tariffs OR "stock market"', "hl=en-US&gl=US&ceid=US:en") },
  { id: "korea", url: gnews("삼성전자 OR SK하이닉스 OR 환율", "hl=ko&gl=KR&ceid=KR:ko") },
  { id: "sec", url: "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom" },
  { id: "fed", url: "https://www.federalreserve.gov/feeds/press_all.xml", always: true },
];

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
export function parseFeed(xml, feedId, minTs = 0) {
  const out = [];
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
    const ts = Date.parse(clean(tag(block, atom ? "updated" : "pubDate")));
    if (!Number.isFinite(ts) || ts < minTs) continue;
    let title = clean(tag(block, "title"));
    let link = clean(tag(block, "link"));
    if (!link) {
      const m = block.match(/<link[^>]*href="([^"]+)"/);
      link = m ? m[1].replace(/&amp;/g, "&") : "";
    }
    let source = clean(tag(block, "source"));
    const id = clean(tag(block, atom ? "id" : "guid")) || link || title;
    if (feedId === "sec") {                       // "8-K - Tesla, Inc. (0001318605) (Filer)"
      const m = title.match(/\((\d{10})\)/);
      const t = m && CIK[String(Number(m[1]))];
      if (!t) continue;
      const items = [...clean(clean(tag(block, "summary"))).matchAll(/Item [\d.]+: ([^I]+?)(?= Item|$)/g)].map((x) => x[1].trim());
      title = `${t} filed an 8-K with the SEC` + (items.length ? `: ${items.slice(0, 3).join("; ")}` : "");
      source = "SEC";
    } else if (feedId === "fed") {
      source = "Federal Reserve";
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
export const NEWS_SYSTEM = `You write one-line market flashes for Dongmin, a private investor in Singapore, inside his Telegram alert bot. He holds US stocks and ETFs: NVDA, GOOGL, TSLA, PLTR, BE, the AI/robotics ETFs SOXX, AIPO, BOTZ and IQQ, the index ETFs VOO and QQQ, sector ETFs XLV, XLF, XLB, XLP, ITA, gold (GLDM), and he watches Samsung Electronics and SK hynix and the SGD, USD and KRW exchange rates.

For each headline decide whether it is real, new, market-moving information for something he holds or watches, and if so say in one plain sentence what it means for him.

Rules
- importance: 5 = likely to move the name or the market several percent (earnings surprise, guidance change, major regulatory or legal action, a Fed decision, a large deal); 4 = clearly material; 3 = worth knowing today; 2 = minor; 1 = noise, opinion, a recap of a move that already happened, a listicle, a price-target shuffle with nothing new, or not about his names.
- send is true only for importance 3 or more.
- take: one sentence, at most 28 words, plain words, no jargon. Say what happened in effect and which way it leans for the holding (good, bad or unclear, and why). Do not tell him to buy or sell: his own rules decide that. Never invent a number or a fact that is not in the headline.
- lean: "up", "down" or "unclear" for the affected holding.
- tickers: the affected tickers from his list, most affected first, at most 3.
- Write take in English, unless the headline is in Korean: then write it in Korean.`;

export const NEWS_SCHEMA = {
  type: "object",
  properties: {
    send: { type: "boolean" },
    importance: { type: "integer" },
    lean: { type: "string", enum: ["up", "down", "unclear"] },
    tickers: { type: "array", items: { type: "string" } },
    take: { type: "string" },
  },
  required: ["send", "importance", "lean", "tickers", "take"],
  additionalProperties: false,
};

export function newsParams(item, model) {
  return {
    model: model || NEWS_MODEL,
    max_tokens: 1500,
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
  const tks = (j.tickers && j.tickers.length ? j.tickers : item.tickers).slice(0, 3);
  const mins = Math.max(0, Math.round((now - item.ts) / 60000));
  const lines = [`${icon} <b>${esc(tks.join(" · ") || "MARKET")}</b> · ${esc(item.source || "news")} · ${sgTime(item.ts)} SGT (${mins} min ago)`,
    `<b>${esc(item.title)}</b>`, "", `${arrow} ${esc(j.take)}`];
  const rules = tks.map((t) => ruleLine(status, t)).filter(Boolean).slice(0, 2);
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
      return r.ok ? parseFeed(await r.text(), f.id, t - MAX_AGE_MIN * 60000) : [];
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
      st.recent = [{ ts: c.ts, title: c.title, source: c.source, link: c.link, tickers: (j.tickers || []).slice(0, 3),
                     take: j.take, lean: j.lean, importance: j.importance }, ...(st.recent || [])].slice(0, 30);
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
