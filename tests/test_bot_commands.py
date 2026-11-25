"""Telegram's "/" menu (tools/bot_commands.py), built from config.json."""
import json
import os
import re

import tools.bot_commands as B

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_menu_has_the_general_commands_and_one_panel_command_per_covered_ticker():
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    cmds = B.commands(cfg)
    names = [c["command"] for c in cmds]
    assert names[:5] == ["list", "panel", "mood", "holdings", "help"]
    assert {"panel_nvda", "panel_soxx", "panel_spcx", "panel_005930_ks", "panel_000660_ks"} <= set(names)
    assert "panel_gldm" not in names and "panel_btc_usd" not in names           # no company financials
    assert not any("usdsgd" in n for n in names)
    for c in cmds:                                                               # Telegram's rules
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c["command"]) and 1 <= len(c["description"]) <= 256
    assert len(cmds) <= 100 and len(set(names)) == len(names)
    assert "Samsung Electronics" in next(c["description"] for c in cmds if c["command"] == "panel_005930_ks")
    assert B.slug("005930.KS") == "005930_ks" and B.slug("BTC-USD") == "btc_usd"
