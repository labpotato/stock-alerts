"""Daily research facts for the news judge and the post-close run.

Runs two tools from the day-trader-skill pack (vendored in .claude/skills, MIT):
  - trader-analyst/facts.py: each single stock's last filed quarter from SEC XBRL
    (revenue, EPS and margins against a year earlier). Needs SEC_USER_AGENT
    ("Name email"), which SEC requires; skipped without it.
  - trader-pelosi/ptr.py: new House trade disclosures (PTRs), kept only where a
    member traded one of the watched tickers.

Writes docs/research.json (public data only: no holdings) and keeps the Congress
tracker's memory in research_state/. Usage: python3 tools/research.py
"""
import datetime as dt
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, ".claude", "skills")
OUT = os.path.join(ROOT, "docs", "research.json")
STATE = os.path.join(ROOT, "research_state")
STOCKS = ["NVDA", "GOOGL", "TSLA", "PLTR", "BE"]      # companies that file with the SEC
KEEP_DAYS = 45                                       # Congress trades older than this drop off


def watched():
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    return sorted(set(cfg["tickers"]))


def run(args, timeout=600):
    p = subprocess.run([sys.executable, "-I"] + args, capture_output=True, text=True, timeout=timeout, cwd=STATE)
    if p.stderr.strip():
        print(p.stderr.strip()[-500:], file=sys.stderr)
    return p


def fundamentals():
    if not os.environ.get("SEC_USER_AGENT", "").strip():
        return None, "skipped: SEC_USER_AGENT is not set"
    tmp = os.path.join(STATE, "metrics.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    run([os.path.join(SKILLS, "trader-analyst", "facts.py"), *STOCKS, "--no-filings", "--metrics", tmp])
    if not os.path.exists(tmp):
        return None, "failed: no metrics written"
    rows = json.load(open(tmp)).get("companies", [])
    keep = ("ticker", "period_end", "revenue_growth_yoy", "eps_growth_yoy", "net_income_growth_yoy",
            "gross_margin", "operating_margin", "net_margin", "eps_ttm")
    return [{k: r.get(k) for k in keep} for r in rows], f"ok: {len(rows)} companies"


def congress(prev, today):
    tickers = set(watched())
    p = run([os.path.join(SKILLS, "trader-pelosi", "ptr.py"), "new", "--read", "--json",
             "--state", os.path.join(STATE, "pelosi.json"), "--pdf-dir", os.path.join(STATE, "ptr-pdfs"),
             "--today", today.isoformat()], timeout=1500)
    if p.returncode == 2 or not p.stdout.strip():
        return prev, "failed: " + (p.stderr.strip().splitlines() or ["no output"])[-1][:160]
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return prev, "failed: unreadable output"
    found = []
    for rep in data.get("reports", []):
        r, f = rep.get("report") or {}, rep.get("filing") or {}
        for row in r.get("rows", []):
            if row.get("ticker") in tickers:
                found.append({"member": f.get("name") or r.get("name"), "district": f.get("district"),
                              "ticker": row["ticker"], "action": row.get("type_name"), "date": row.get("date"),
                              "filed": f.get("filed"), "amount": row.get("amount"), "owner": row.get("owner_name"),
                              "days_to_disclose": row.get("days_to_disclose"), "url": f.get("url")})
    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    seen, out = set(), []
    for x in found + prev:
        k = (x.get("member"), x.get("ticker"), x.get("date"), x.get("action"), x.get("amount"))
        if k in seen or (x.get("filed") or "") < cutoff:
            continue
        seen.add(k)
        out.append(x)
    out.sort(key=lambda x: x.get("filed") or "", reverse=True)
    return out[:40], f"ok: {len(data.get('new', []))} new filings read, {len(found)} trades in watched tickers"


def main():
    os.makedirs(STATE, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date()
    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT))
        except json.JSONDecodeError:
            old = {}
    fund, fnote = fundamentals()
    cong, cnote = congress(old.get("congress", []), today)
    doc = {"as_of": today.isoformat(),
           "fundamentals": fund if fund is not None else old.get("fundamentals", []),
           "congress": cong,
           "notes": {"fundamentals": fnote, "congress": cnote,
                     "source": "day-trader-skill trader-analyst and trader-pelosi (MIT); public SEC and House Clerk data. "
                               "Congress trades are disclosed up to 45 days late: public and old, not inside information."}}
    with open(OUT, "w") as fh:
        json.dump(doc, fh, indent=1)
    print(json.dumps(doc["notes"]))


if __name__ == "__main__":
    main()
