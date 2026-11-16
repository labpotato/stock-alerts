"""Telegram's "/" menu for the bot, built from config.json so it follows the watchlist.

Prints the JSON body for Telegram's setMyCommands: the general commands, then one /panel_<ticker>
per ticker the investor panel covers (ticker lower-cased, dots and dashes as underscores, the same
slug the bot's panel.js accepts). Usage: python3 tools/bot_commands.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from snapshot import panel_tickers  # noqa: E402

BASE = [("list", "Your watchlist: price, today's move, signal state"),
        ("panel", "Investor panel grid (Buffett, Munger, Graham, Lynch, Druckenmiller)"),
        ("mood", "Market mood: what investors are saying now"),
        ("help", "What I can do")]


def slug(t):
    return re.sub(r"[^a-z0-9]", "_", t.lower())


def commands(cfg):
    names = cfg.get("names") or {}
    tickers, _ = panel_tickers()
    out = [{"command": c, "description": d} for c, d in BASE]
    for t in tickers:
        name = names.get(t) or t
        out.append({"command": f"panel_{slug(t)}"[:32], "description": f"Panel: {t} · {name}"[:256]})
    return out[:100]                                   # Telegram's maximum


if __name__ == "__main__":
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    print(json.dumps({"commands": commands(cfg)}))
