// /list: the watchlist as one message, from the public config.json and docs/board.json (the last
// reading of every ticker, kept between sessions). Held tickers get a 💼 mark from the private
// holdings file; share counts are never shown here.
const STATE = { BUY: "🟢", SELL: "🔴", "WATCH BUY": "🔵", "WATCH SELL": "🟠", NONE: "⚪" };
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export function groupOf(t, cfg, snaps) {
  if ((cfg.fx || {})[t]) return "Currencies";
  if (t.endsWith("-USD")) return "Crypto";
  if (t.endsWith(".KS") || t.endsWith(".SI")) return "Korea and Singapore";
  const s = (snaps || {})[t];
  if (s && s.kind === "etf") return "ETFs";
  if (s && s.kind === "company") return "Stocks";
  return (cfg.not_equity || []).includes(t) ? "ETFs" : "Stocks";
}

function price(t, v, cfg) {
  if (v == null || v.price == null) return "—";
  const cur = (cfg.currency || {})[t];
  const p = Number(v.price);
  if ((cfg.fx || {})[t]) return p.toFixed(p < 0.01 ? 6 : 4);
  if (cur === "KRW") return `₩${Math.round(p).toLocaleString("en-US")}`;
  return `${cur === "SGD" ? "S$" : "$"}${p.toLocaleString("en-US", { maximumFractionDigits: p >= 1000 ? 0 : 2, minimumFractionDigits: p >= 1000 ? 0 : 2 })}`;
}

export function listMessage(cfg, board, snaps, held) {
  const all = [...cfg.tickers, ...(cfg.info_tickers || []).filter((t) => !cfg.tickers.includes(t)), ...Object.keys(cfg.fx || {})];
  const groups = {};
  for (const t of all) (groups[groupOf(t, cfg, snaps)] ||= []).push(t);
  const order = ["Stocks", "ETFs", "Korea and Singapore", "Crypto", "Currencies"];
  const lines = [`📋 <b>Your watchlist</b> · ${all.length} tickers`, "Range · momentum: 🟢 BUY 🔴 SELL 🔵 below range / sold off 🟠 above range / overheated ⚪ normal", ""];
  for (const g of order) {
    if (!groups[g]) continue;
    lines.push(`<b>${g}</b>`);
    for (const t of groups[g]) {
      const v = (board.tickers || {})[t];
      const label = (cfg.fx || {})[t] ? (cfg.fx[t].label || t) : t;
      if (!v) { lines.push(`${(held || new Set()).has(t) ? "💼 " : ""}${esc(label)} — no reading yet`); continue; }
      const d = typeof v.day_pct === "number" ? ` ${v.day_pct >= 0 ? "▲" : "▼"}${Math.abs(v.day_pct).toFixed(1)}%` : "";
      lines.push(`${(held || new Set()).has(t) ? "💼 " : ""}<b>${esc(label)}</b> ${price(t, v, cfg)}${d} ${STATE[v.keltner] || "⚪"}${STATE[v.momentum] || "⚪"}` +
        (typeof v.rsi === "number" ? ` ${Math.round(v.rsi)}` : ""));
    }
    lines.push("");
  }
  const when = board.generated_utc ? new Date(board.generated_utc) : null;
  lines.push(`<i>💼 = you hold it. Last readings${when ? " as of " + when.toISOString().slice(0, 16).replace("T", " ") + " UTC" : ""}; ` +
    "US stocks update while their market is open. /panel_nvda etc. for the investor panel.</i>");
  return lines.join("\n");
}

export async function buildList(env, doFetch, holdingsFor) {
  const get = async (u) => { try { const r = await doFetch(u, { cf: { cacheTtl: 60 } }); return r.ok ? await r.json() : null; } catch { return null; } };
  const [cfg, board, snaps] = await Promise.all([get(env.CONFIG_URL), get(env.BOARD_URL), get(env.SNAP_URL)]);
  if (!cfg) return "📋 I couldn't read the watchlist just now. Try again in a minute.";
  let held = new Set();
  try { const h = holdingsFor ? await holdingsFor(env, doFetch) : null; held = new Set(Object.keys((h && h.tickers) || {})); } catch { held = new Set(); }
  return listMessage(cfg, board || {}, (snaps && snaps.stocks) || {}, held);
}
