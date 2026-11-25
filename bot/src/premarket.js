// Before the US open (Dongmin, 25 Nov): /list and the /panel grid arrive on their own at 09:20
// New York time on NYSE trading days, ten minutes before the bell. Once a day, guarded in KV.
import { buildList } from "./list.js";
import { loadPanel, summaryMessage } from "./panel.js";

export const PREMARKET_AT = "09:20";   // New York time; daylight saving handled by the time zone
// NYSE full-day closures (nyse.com holiday calendar). Extend each year.
export const NYSE_HOLIDAYS = new Set([
  "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
  "2026-09-07", "2026-11-26", "2026-12-25",
  "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
  "2027-09-06", "2027-11-25", "2027-12-24",
]);

export function nyClock(t) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", weekday: "short", hourCycle: "h23",
  }).formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, hm: `${p.hour}:${p.minute}`, wd: p.weekday };
}

/** The New York date when this minute is the pre-market slot on a trading day, else null. */
export function premarketDue(t, at = PREMARKET_AT) {
  const c = nyClock(t);
  if (c.hm !== at || c.wd === "Sat" || c.wd === "Sun" || NYSE_HOLIDAYS.has(c.day)) return null;
  return c.day;
}

async function send(env, doFetch, text) {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await doFetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: env.TG_CHAT, text, parse_mode: "HTML", link_preview_options: { is_disabled: true } }),
      });
      if (r.ok || r.status < 500) return r.ok;
    } catch { /* retry */ }
  }
  return false;
}

export async function runPremarket(env, deps) {
  const day = premarketDue(deps.now());
  if (!day || !env.NEWS_KV) return { sent: false };
  const key = `premarket:${day}`;
  if (await env.NEWS_KV.get(key)) return { sent: false, already: true };
  await env.NEWS_KV.put(key, "1", { expirationTtl: 3 * 86400 });
  const list = await buildList(env, deps.fetch, deps.holdingsFor);
  await send(env, deps.fetch, `🔔 <b>US market opens in 10 minutes</b>\n\n${list}`);
  const st = await loadPanel(env.NEWS_KV);
  await send(env, deps.fetch, summaryMessage(st));
  return { sent: true, day };
}
