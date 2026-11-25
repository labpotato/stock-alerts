// /holdings in Telegram (Dongmin, 25 Nov): see the holdings the alerter and the bot use, and change
// share counts or cash. Every change is shown as old -> new and saved only after /confirm, to
// holdings.json in the PRIVATE repo (HOLDINGS_REPO), which both readers take when its as_of is newest.
// Nothing here goes to the public repo, a log, or a GET route.
export const PENDING_KEY = "holdings:pending";
const TTL = 3600;
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export const USAGE = "To change: <code>/holdings NVDA 6, VOO 12, cash 405.20</code> (absolute numbers; 0 sells out). " +
  "I show old → new and save only after /confirm. Targets and groups stay as they are.";

/** "NVDA 6, voo 12.5 cash 405.20" -> {shares: {NVDA: 6, VOO: 12.5}, cash: 405.2}, {error}, or null (no change asked). */
export function parseChanges(text) {
  const body = text.replace(/^\/holdings(@\S+)?/i, "").trim();
  if (!body) return null;
  const shares = {};
  let cash = null;
  const re = /([A-Za-z0-9][A-Za-z0-9.\-=^]*)\s*[:=]?\s*\$?(-?[\d,]*\.?\d+)/g;
  let m;
  while ((m = re.exec(body))) {
    const k = m[1].toUpperCase(), v = Number(m[2].replace(/,/g, ""));
    if (!Number.isFinite(v) || v < 0) return { error: `"${m[0]}" isn't a number of 0 or more.` };
    if (k === "CASH") cash = v; else shares[k] = v;
  }
  const leftover = body.replace(re, "").replace(/[,;\s]+/g, "");
  if (leftover) return { error: `I couldn't read "${leftover}".` };
  if (!Object.keys(shares).length && cash == null) return { error: "I found no ticker and number." };
  return { shares, cash };
}

/** Merge, never replace: targets, groups and the top-level settings are kept. */
export function applyChanges(h, ch, day) {
  const out = JSON.parse(JSON.stringify(h));
  out.tickers ||= {};
  for (const [t, n] of Object.entries(ch.shares || {})) {
    if (out.tickers[t]) out.tickers[t].shares = n;
    else out.tickers[t] = { shares: n, target_pct: null, group: null };
  }
  if (ch.cash != null) out.cash_usd = ch.cash;
  out.as_of = day;
  return out;
}

export function diffMessage(h, ch, day, known) {
  const lines = ["📝 <b>Holdings change</b> (not saved yet)", ""];
  for (const [t, n] of Object.entries(ch.shares || {})) {
    const old = h.tickers && h.tickers[t];
    lines.push(`<b>${esc(t)}</b>: ${old ? old.shares : "not held"} → ${n}` +
      (old ? "" : (known.has(t) ? " (new: no target or group yet)" : " ⚠️ not on your watchlist; check the ticker")));
  }
  if (ch.cash != null) lines.push(`<b>Cash</b>: $${h.cash_usd ?? "?"} → $${ch.cash}`);
  lines.push("", `Date becomes ${day} (was ${esc(h.as_of || "?")}).`, "Reply /confirm to save or /cancel. This expires in an hour.");
  return lines.join("\n");
}

export function showMessage(h) {
  if (!h) return "💼 I can't read your holdings right now.";
  const lines = [`💼 <b>Your holdings</b> as of ${esc(h.as_of || "?")}`, ""];
  for (const [t, v] of Object.entries(h.tickers || {})) {
    lines.push(`<b>${esc(t)}</b> ${v.shares ?? "?"} shares · target ${v.target_pct ?? "—"}% · ${esc(v.group ?? "no group")}`);
  }
  lines.push(`<b>Cash</b> $${h.cash_usd ?? "?"}`, "", USAGE);
  return lines.join("\n");
}

export const sgDate = (t) => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Singapore" }).format(new Date(t));
const gh = (env) => ({
  authorization: `Bearer ${env.HOLDINGS_WRITE_TOKEN || env.HOLDINGS_TOKEN}`, accept: "application/vnd.github+json",
  "user-agent": "stock-alerts-bot", "x-github-api-version": "2022-11-28",
});
const b64 = (s) => { let x = ""; for (const c of new TextEncoder().encode(s)) x += String.fromCharCode(c); return btoa(x); };
const unb64 = (s) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\n/g, "")), (c) => c.charCodeAt(0)));

async function readFile(env, doFetch) {
  const r = await doFetch(`https://api.github.com/repos/${env.HOLDINGS_REPO}/contents/holdings.json`, { headers: gh(env) });
  if (!r.ok) return { status: r.status };
  const j = await r.json();
  return { sha: j.sha, h: JSON.parse(unb64(j.content)) };
}

async function writeFile(env, doFetch, ch, day) {
  for (let i = 0; i < 2; i++) {
    const f = await readFile(env, doFetch);
    if (!f.h) return { ok: false, status: f.status };
    const next = applyChanges(f.h, ch, day);
    const r = await doFetch(`https://api.github.com/repos/${env.HOLDINGS_REPO}/contents/holdings.json`, {
      method: "PUT", headers: { ...gh(env), "content-type": "application/json" },
      body: JSON.stringify({ message: `holdings as of ${day} (Telegram)`, sha: f.sha,
                             content: b64(JSON.stringify(next, null, 2) + "\n") }),
    });
    if (r.ok) return { ok: true, h: next };
    if (r.status !== 409) return { ok: false, status: r.status };     // 409: changed meanwhile, re-read once
  }
  return { ok: false, status: 409 };
}

/** /holdings, /confirm, /cancel. Returns HTML texts to send, or null when the text is not one of them. */
export async function holdingsCommand(env, deps, text) {
  const kv = env.NEWS_KV, now = deps.now ? deps.now() : Date.now();
  if (/^\/cancel\b/i.test(text)) {
    if (!kv || !(await kv.get(PENDING_KEY))) return null;
    await kv.delete(PENDING_KEY);
    return ["Cancelled. Nothing was changed."];
  }
  if (/^\/confirm\b/i.test(text)) {
    const p = kv ? JSON.parse((await kv.get(PENDING_KEY)) || "null") : null;
    if (!p) return ["Nothing to confirm (a change expires after an hour). Send /holdings to start."];
    if (!env.HOLDINGS_REPO || !(env.HOLDINGS_WRITE_TOKEN || env.HOLDINGS_TOKEN)) return ["I have no access to your holdings file."];
    const r = await writeFile(env, deps.fetch, p.ch, p.day);
    if (!r.ok) {
      return [r.status === 403 || r.status === 404
        ? "⛔ GitHub refused the save: my token can read your holdings file but not change it. Give the token " +
          "<b>Contents: Read and write</b> on portfolio-private only (GitHub → Settings → Developer settings → " +
          "Fine-grained tokens → the one saved as HOLDINGS_TOKEN → Edit), then /confirm again within the hour. Nothing was changed."
        : `⚠️ The save failed (GitHub ${r.status}). Nothing was changed; try /confirm again in a minute.`];
    }
    await kv.delete(PENDING_KEY);
    return [`✅ Saved, as of ${p.day}. I use it from now; the alerter from its next run (within 10 minutes).\n\n` +
      "<i>The scheduled email reads its own holdings doc in Cowork, so update that too if the email should match.</i>"];
  }
  if (!/^\/holdings\b/i.test(text)) return null;
  const ch = parseChanges(text);
  const h = await deps.holdingsFor(env, deps.fetch);
  if (!ch) return [showMessage(h)];
  if (ch.error) return [`${esc(ch.error)} ${USAGE}`];
  if (!h) return ["💼 I can't read your current holdings, so I can't show what would change. Try again in a minute."];
  if (!kv) return ["I have nowhere to hold the change until you confirm."];
  const day = sgDate(now);
  await kv.put(PENDING_KEY, JSON.stringify({ ch, day }), { expirationTtl: TTL });
  let known = new Set();
  try { known = deps.known ? await deps.known() : known; } catch { known = new Set(); }
  return [diffMessage(h, ch, day, known)];
}
