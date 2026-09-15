#!/usr/bin/env python3
"""
BSC (four.meme) execution gates — the checks that must pass BEFORE a buy is sent.

Mirrors executor.py (Robinhood/Pons). It exists because bsc_buy.py declared
MAX_TRADE_USD = 25.0 and NOTHING ENFORCED IT: there was no preflight, no daily cap,
no open-position cap and no honeypot gate. The constant read like a limit and was
decoration.

Every gate here is a HARD FAIL, not a warning. The standing rule on this project is
that a buy is never armed on a chain whose exit path is unproven; the corollary is
that a proven exit still does not license an unbounded buy.

    python3 bsc_executor.py --check 0x<token> --usd 25
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
from bsc_buy import (TOKEN_MANAGER, quote_of, quote_tradeable, quote_label,   # noqa: E402
                     quote_buy, buy_min_out, build_buy, MAX_TRADE_USD)
from bsc_selltest import rpc, post_batch, prove                                # noqa: E402

MAX_DAILY_USD = 150.0
MAX_OPEN = 3
BNB_USD = 620.0
POSITIONS = os.path.join(DATA, "bsc_positions.json")
JOURNAL = os.path.join(DATA, "bsc_exits.jsonl")
SIM_FROM = "0x1111111111111111111111111111111111111111"


def _today_spend():
    """(usd_spent_today, positions_opened_today) from the journal — never memory."""
    if not os.path.exists(JOURNAL):
        return 0.0, 0
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    spent, opened = 0.0, 0
    for line in open(JOURNAL):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("action") != "OPEN":
            continue
        ts = r.get("ts")
        if not ts:
            continue
        if datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") == day:
            spent += float(r.get("entry_usd") or 0)
            opened += 1
    return spent, opened


def _open_positions():
    if not os.path.exists(POSITIONS):
        return 0
    try:
        return len(json.load(open(POSITIONS)))
    except Exception:  # noqa: BLE001
        return 0


def preflight(token, usd, slippage_bps=300, log=print):
    """
    (fails, warns, info). A non-empty `fails` means DO NOT SEND.

    Ordered cheapest-first so an obvious reject costs no RPC, with the honeypot
    proof last because it is the expensive one.
    """
    fails, warns, info = [], [], {}
    token = token.lower()

    # 1. size and budget -- pure local reads
    if usd > MAX_TRADE_USD:
        fails.append(f"size ${usd:.2f} exceeds hard cap ${MAX_TRADE_USD:.2f}")
    spent, opened = _today_spend()
    info["spent_today"], info["opened_today"] = spent, opened
    if spent + usd > MAX_DAILY_USD:
        fails.append(f"daily cap: ${spent:.2f} spent + ${usd:.2f} > ${MAX_DAILY_USD:.2f}")
    nopen = _open_positions()
    info["open_positions"] = nopen
    if nopen >= MAX_OPEN:
        fails.append(f"already {nopen} open positions (max {MAX_OPEN})")

    # 2. is this even reachable? ~17% of four.meme launches are, measured
    quote = quote_of(rpc, token)
    ok, why = quote_tradeable(quote)
    info["quote"] = quote_label(quote)
    if not ok:
        fails.append(why)
        return fails, warns, info          # nothing below can work

    # 3. will the buy fill, and at what price? min_out=0 is a blank cheque
    wei = int(usd / BNB_USD * 1e18)
    expected = quote_buy(post_batch, token, SIM_FROM, wei)
    if not expected:
        fails.append("buy does not fill at this size — no minTokensOut brackets")
        return fails, warns, info
    info["expected_tokens"] = expected
    info["min_out"] = buy_min_out(expected, slippage_bps)
    info["price_per_token_usd"] = usd / (expected / 1e18)

    # 4. CAN IT BE SOLD? This is the gate that matters most and the one a buy-only
    #    path never asks. Proven by state-override simulation, not assumed.
    sellable, detail = prove(token, log=lambda *a, **k: None)
    info["sell_proof"] = detail
    if not sellable:
        fails.append(f"SELL DOES NOT SIMULATE ({detail}) — this is a honeypot "
                     f"until proven otherwise")
    return fails, warns, info


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", required=True, help="token address")
    ap.add_argument("--usd", type=float, default=MAX_TRADE_USD)
    ap.add_argument("--slippage-bps", type=int, default=300)
    a = ap.parse_args()

    print(f"preflight  {a.check}  ${a.usd:.2f}\n")
    fails, warns, info = preflight(a.check, a.usd, a.slippage_bps)
    for k, v in info.items():
        if k == "expected_tokens":
            print(f"  {k:<22} {v/1e18:,.4f}")
        elif k == "min_out":
            print(f"  {k:<22} {v/1e18:,.4f}  ({a.slippage_bps}bps floor)")
        elif k == "price_per_token_usd":
            print(f"  {k:<22} ${v:.10f}")
        else:
            print(f"  {k:<22} {v}")
    print()
    for w in warns:
        print(f"  WARN  {w}")
    if fails:
        print(f"  BLOCKED — {len(fails)} gate(s) failed:")
        for f in fails:
            print(f"    ✗ {f}")
        sys.exit(1)
    print("  ALL GATES PASS — safe to build. Still requires an explicit sign.")
