// Cloudflare Worker entry: Telegram sends each message here (webhook).
import Anthropic from "@anthropic-ai/sdk";
import { answer, dispatchAlerter, isAllowed } from "./bot.js";
import { runNews } from "./news.js";

export default {
  async fetch(request, env, ctx) {
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
    ctx.waitUntil(answer(env, update.message, { client, fetch: (u, o) => fetch(u, o) }));
    return new Response("ok");
  },

  // Every minute (wrangler.toml [triggers]): check the news feeds; every 10th minute also start
  // one alerter run on GitHub.
  async scheduled(controller, env, ctx) {
    const doFetch = (u, o) => fetch(u, o);
    if (new Date(controller.scheduledTime).getUTCMinutes() % 10 === 0) ctx.waitUntil(dispatchAlerter(env, doFetch));
    if (env.NEWS_KV && env.NEWS !== "off") {
      const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
      ctx.waitUntil(runNews(env, { fetch: doFetch, client, kv: env.NEWS_KV })
        .then((r) => { if (r.sent && r.sent.length) console.log("news sent:", r.sent.length); })
        .catch((e) => console.log("news error:", e && e.message)));
    }
  },
};
