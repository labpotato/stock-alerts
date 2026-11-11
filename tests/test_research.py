import json
import types

import pytest

import tools.research as R


def fake_run(stdout, code=0):
    return lambda args, timeout=600: types.SimpleNamespace(stdout=stdout, stderr="", returncode=code)


def test_only_watched_tickers_are_kept_and_old_ones_drop(monkeypatch):
    today = R.dt.date(2026, 11, 10)
    out = {"new": [{"doc_id": "1"}], "reports": [{"filing": {"name": "Hon. A B", "district": "NY1", "filed": "2026-11-09", "url": "u"},
            "report": {"rows": [{"ticker": "NVDA", "type_name": "purchase", "date": "2026-10-20", "amount": "$1,001 - $15,000", "owner_name": "spouse"},
                                {"ticker": "MCD", "type_name": "sale", "date": "2026-10-20", "amount": "$1,001 - $15,000"}]}}]}
    monkeypatch.setattr(R, "run", fake_run(json.dumps(out)))
    old = [{"member": "Hon. C D", "ticker": "TSLA", "date": "2026-08-01", "filed": "2026-09-01"},     # older than 45 days
           {"member": "Hon. E F", "ticker": "GOOGL", "date": "2026-10-30", "filed": "2026-11-05"}]
    rows, note = R.congress(old, today)
    assert [r["ticker"] for r in rows] == ["NVDA", "GOOGL"]
    assert rows[0]["owner"] == "spouse" and "1 trades in watched" in note


def test_a_failed_fetch_keeps_what_we_had(monkeypatch):
    monkeypatch.setattr(R, "run", fake_run("", code=2))
    rows, note = R.congress([{"ticker": "NVDA", "filed": "2026-11-09"}], R.dt.date(2026, 11, 10))
    assert rows == [{"ticker": "NVDA", "filed": "2026-11-09"}] and note.startswith("failed")


def test_fundamentals_need_the_sec_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    rows, note = R.fundamentals()
    assert rows is None and "SEC_USER_AGENT" in note
