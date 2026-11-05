"""One-time: save real Yahoo bars as test fixtures. Runs on GitHub Actions (Yahoo is reachable there).

Daily: 1 year of daily bars ending on the 2026-11-04 session, for the tickers the tests check.
Intraday: 1-minute bars with pre- and post-market for 3-4 Nov, where Yahoo still serves them.
Saved exactly as Yahoo returns them (the chart 'result' object), so tests parse the real format."""
import json, os, sys, time, urllib.request, datetime as dt

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "fixtures")
UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-alerts-fixtures/1)"}
END = int(dt.datetime(2026, 11, 5, 5, 0, tzinfo=dt.timezone.utc).timestamp())   # midnight ET after 4 Nov
START = int(dt.datetime(2025, 11, 1, tzinfo=dt.timezone.utc).timestamp())
DAILY = ["QQQ", "BOTZ", "GLDM", "GOOGL", "USDSGD=X", "SGDKRW=X"]
MINUTE = ["QQQ", "BOTZ"]


def get(url):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)["chart"]["result"][0]
        except Exception as e:
            if i == 3:
                raise
            print(f"retry {url}: {e}", file=sys.stderr)
            time.sleep(3 + 3 * i)


def main():
    os.makedirs(OUT, exist_ok=True)
    for t in DAILY:
        r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
                f"?period1={START}&period2={END}&interval=1d")
        json.dump(r, open(os.path.join(OUT, f"{t}_1d.json"), "w"))
        last = dt.datetime.fromtimestamp(r["timestamp"][-1], dt.timezone.utc).date()
        print(f"{t}: {len(r['timestamp'])} daily bars, last {last}")
    p1 = int(dt.datetime(2026, 11, 3, 8, 0, tzinfo=dt.timezone.utc).timestamp())
    for t in MINUTE:
        try:
            r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
                    f"?period1={p1}&period2={END}&interval=1m&includePrePost=true")
            json.dump(r, open(os.path.join(OUT, f"{t}_1m.json"), "w"))
            print(f"{t}: {len(r.get('timestamp') or [])} one-minute bars")
        except Exception as e:
            print(f"{t}: 1-minute bars unavailable ({e}); intraday tests use built readings")


if __name__ == "__main__":
    main()
