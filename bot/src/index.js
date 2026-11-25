// Cloudflare Worker entry: Telegram sends each message here (webhook).
import Anthropic from "@anthropic-ai/sdk";
import { answer, dispatchAlerter, dispatchWorkflow, holdingsFor, isAllowed, researchDue } from "./bot.js";
import { buildList } from "./list.js";
import { feedCheck, recentNews, runMood, runNews } from "./news.js";
import { enqueuePanel, loadPanel, panelCommand, stepPanel } from "./panel.js";
import { runPremarket } from "./premarket.js";

export default {
  async fetch(request, env, ctx) {
    if (request.method === "GET" && new URL(request.url).pathname === "/news") {
      // The dashboard's news panel: the last flashes (public headlines only).
      return new Response(JSON.stringify(await recentNews(env.NEWS_KV)), {
        headers: { "content-type": "application/json", "access-control-allow-origin": "*", "cache-control": "max-age=30" },
      });
    }
    if (request.method === "GET" && new URL(request.url).pathname === "/feeds") {
      // Which public feeds answer from here (for checking after a deploy).
      return new Response(JSON.stringify(await feedCheck((u, o) => fetch(u, o), Date.now()), null, 1), {
        headers: { "content-type": "application/json", "cache-control": "max-age=120" },
      });
    }
    if (request.method === "GET" && new URL(request.url).pathname === "/panel") {
      // The investor panel's verdicts (public companies only, no holdings), for the dashboard and the post-close run.
      const st = env.NEWS_KV ? await loadPanel(env.NEWS_KV) : {};
      return new Response(JSON.stringify({ as_of: st.as_of, stocks: st.stocks || {} }), {
        headers: { "content-type": "application/json", "access-control-allow-origin": "*", "cache-control": "max-age=300" },
      });
    }
    if (request.method !== "POST") return new Response("ok");
    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }
    // Only Telegram (secret header) and only Dongmin's own chat. Everyone else is ignored silently.
    if (!isAllowed(update, env, request.headers.get("X-Telegram-Bot-Api-Secret-Token"))) {
      return new Response("ok");
    }
    const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
    // Reply to Telegram at once so it never re-sends the message; answer in the background.
    const doFetch = (u, o) => fetch(u, o);
    const mood = () => runMood(env, { fetch: doFetch, client, kv: env.NEWS_KV }, true);
    const panel = (arg) => panelCommand(env, { fetch: doFetch, client, kv: env.NEWS_KV }, arg);
    const list = () => buildList(env, doFetch, holdingsFor);
    ctx.waitUntil(answer(env, update.message, { client, fetch: doFetch, mood, panel, list }));
    return new Response("ok");
  },

  // Every minute (wrangler.toml [triggers]): check the news feeds; every 10th minute also start
  // one alerter run on GitHub.
  async scheduled(controller, env, ctx) {
    const doFetch = (u, o) => fetch(u, o);
    if (new Date(controller.scheduledTime).getUTCMinutes() % 10 === 0) ctx.waitUntil(dispatchAlerter(env, doFetch));
    if (researchDue(controller.scheduledTime)) ctx.waitUntil(dispatchWorkflow(env, doFetch, "research.yml"));
    ctx.waitUntil(runPremarket(env, { fetch: doFetch, holdingsFor, now: () => controller.scheduledTime })
      .then((r) => { if (r.sent) console.log("premarket sent", r.day); })
      .catch((e) => console.log("premarket error:", e && e.message)));
    if (env.NEWS_KV && env.NEWS !== "off") {
      const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
      ctx.waitUntil(runNews(env, { fetch: doFetch, client, kv: env.NEWS_KV })
        .then((r) => { if (r.sent && r.sent.length) console.log("news sent:", r.sent.length); })
        .catch((e) => console.log("news error:", e && e.message)));
      ctx.waitUntil(runMood(env, { fetch: doFetch, client, kv: env.NEWS_KV, now: () => controller.scheduledTime })
        .catch((e) => console.log("mood error:", e && e.message)));
      if (env.SNAP_URL && env.PANEL !== "off") {
        const pdeps = { fetch: doFetch, client, kv: env.NEWS_KV, now: () => controller.scheduledTime };
        ctx.waitUntil(enqueuePanel(env, pdeps, { slot: true }).then(() => stepPanel(env, pdeps))
          .then((r) => { if (r && !r.idle) console.log("panel:", JSON.stringify(r).slice(0, 300)); })
          .catch((e) => console.log("panel error:", e && e.message)));
      }
    }
  },
};
