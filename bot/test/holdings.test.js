import { test } from "node:test";
import assert from "node:assert/strict";
import { applyChanges, holdingsCommand, parseChanges, PENDING_KEY } from "../src/holdings.js";

// Invented numbers only: nothing here is a real position.
const H = { as_of: "2026-01-02", cash_usd: 100, sgov_floor_pct: 8, min_order_usd: 200, ai_cap_pct: 30,
  tickers: { AAA: { shares: 5, target_pct: 10, group: "core" }, BBB: { shares: 2, target_pct: 4, group: "ai" } } };

test("parses tickers, decimals, cash and rejects junk", () => {
  assert.deepEqual(parseChanges("/holdings aaa 6, BBB: 2.5 cash $1,234.50"), { shares: { AAA: 6, BBB: 2.5 }, cash: 1234.5 });
  assert.deepEqual(parseChanges("/holdings es3.si 100"), { shares: { "ES3.SI": 100 }, cash: null });
  assert.equal(parseChanges("/holdings"), null);
  assert.ok(parseChanges("/holdings AAA lots").error);
});

test("merges: keeps targets, groups and settings; a new ticker gets null target and group", () => {
  const out = applyChanges(H, { shares: { AAA: 7, CCC: 1 }, cash: 50 }, "2026-02-03");
  assert.equal(out.tickers.AAA.shares, 7);
  assert.equal(out.tickers.AAA.target_pct, 10);
  assert.deepEqual(out.tickers.CCC, { shares: 1, target_pct: null, group: null });
  assert.equal(out.cash_usd, 50);
  assert.equal(out.as_of, "2026-02-03");
  assert.equal(out.sgov_floor_pct, 8);
  assert.equal(H.tickers.AAA.shares, 5);   // the original is untouched
});

function fakes(putStatus = 200) {
  const kv = new Map(), puts = [];
  const env = { HOLDINGS_REPO: "o/private", HOLDINGS_TOKEN: "t",
    NEWS_KV: { get: async (k) => kv.get(k) ?? null, put: async (k, v) => { kv.set(k, v); }, delete: async (k) => { kv.delete(k); } } };
  const fetch = async (u, o = {}) => {
    if (o.method === "PUT") { puts.push(JSON.parse(o.body)); return { ok: putStatus < 300, status: putStatus }; }
    return { ok: true, status: 200, json: async () => ({ sha: "abc", content: btoa(JSON.stringify(H)) }) };
  };
  const deps = { fetch, holdingsFor: async () => H, known: async () => new Set(["AAA", "BBB"]),
    now: () => Date.parse("2026-02-03T04:00:00Z") };
  return { env, deps, kv, puts };
}

test("/holdings shows; a change waits for /confirm, then writes a merged file once", async () => {
  const { env, deps, kv, puts } = fakes();
  assert.match((await holdingsCommand(env, deps, "/holdings"))[0], /AAA<\/b> 5 shares/);
  const d = (await holdingsCommand(env, deps, "/holdings AAA 6, ZZZ 1, cash 90"))[0];
  assert.match(d, /AAA<\/b>: 5 → 6/);
  assert.match(d, /ZZZ.*not on your watchlist/);
  assert.match(d, /\$100 → \$90/);
  assert.equal(puts.length, 0);
  assert.ok(kv.get(PENDING_KEY));
  assert.match((await holdingsCommand(env, deps, "/confirm"))[0], /Saved, as of 2026-02-03/);
  assert.equal(puts.length, 1);
  assert.equal(puts[0].sha, "abc");
  const saved = JSON.parse(atob(puts[0].content));
  assert.equal(saved.tickers.AAA.shares, 6);
  assert.equal(saved.tickers.BBB.target_pct, 4);
  assert.equal(saved.cash_usd, 90);
  assert.equal(kv.get(PENDING_KEY), undefined);
  assert.match((await holdingsCommand(env, deps, "/confirm"))[0], /Nothing to confirm/);
});

test("a read-only token is explained and the change is kept for a retry", async () => {
  const { env, deps, kv } = fakes(403);
  await holdingsCommand(env, deps, "/holdings AAA 6");
  assert.match((await holdingsCommand(env, deps, "/confirm"))[0], /Read and write/);
  assert.ok(kv.get(PENDING_KEY));
  assert.match((await holdingsCommand(env, deps, "/cancel"))[0], /Cancelled/);
  assert.equal(await holdingsCommand(env, deps, "/cancel"), null);
});
