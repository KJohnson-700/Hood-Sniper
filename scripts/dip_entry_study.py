#!/usr/bin/env python3
"""
"Migrated first-dip buy" -- does waiting for a pullback beat buying at graduation?

The idea: after a token migrates, do NOT buy immediately. Track the running ATH
and buy the first time price falls D% from it.

Two ways this can be a mirage, both controlled for here:

1. LOOK-AHEAD. The ATH must be the high seen SO FAR, never the high of the whole
   window. Computing "the dip from the peak" over a completed window is trivially
   profitable and completely unrealisable. An earlier entry-delay analysis in this
   project reversed its conclusion once it was made forward-only. Every decision
   below uses only swaps at or before the decision block.

2. SELECTION. Waiting for a dip silently drops every token that ran without one,
   so the dip strategy trades a DIFFERENT, smaller set. Comparing its average to
   the buy-now average across different sets is meaningless. This reports the
   matched subset -- buy-now restricted to exactly the tokens where a dip
   triggered -- so both arms trade the same tokens.

Orientation matters: pool_swaps is called with token_is_currency1, without which
half the ERC-20-quoted pools replay with an inverted price series.

    python3 dip_entry_study.py 200        # sample size; resumable
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "dip_paths.jsonl")
SRC = os.path.join(DATA, "paper_trades.jsonl")

import paper_trade_logger as L

BLOCK_TIME = 0.101
WINDOW_MIN = 120          # how long we watch after migration
LATENCY_BLOCKS = 30       # ~3s, the automated-entry latency measured earlier


def collect(limit):
    rows = [json.loads(l) for l in open(SRC) if '"grad_block"' in l]
    seen, uniq = set(), []
    for r in rows:
        t = r.get("token")
        if t and t not in seen:
            seen.add(t)
            uniq.append(r)
    done = set()
    if os.path.exists(OUT):
        for l in open(OUT):
            try:
                done.add(json.loads(l)["token"])
            except Exception:  # noqa: BLE001
                pass
    todo = [r for r in uniq if r["token"] not in done]
    random.seed(5)
    random.shuffle(todo)
    todo = todo[:limit]
    print(f"{len(uniq)} unique graduations, {len(done)} cached, fetching {len(todo)}", flush=True)
    win = int(WINDOW_MIN * 60 / BLOCK_TIME)
    n = 0
    with open(OUT, "a") as f:
        for i, r in enumerate(todo, 1):
            try:
                pool = L.find_pool(r["token"], r["grad_block"])
                if not pool:
                    continue
                sw = L.pool_swaps(pool["pool_id"], pool["init_block"],
                                  pool["init_block"] + win,
                                  pool["token_is_currency1"])
                if len(sw) < 20:
                    continue
                base = pool["init_block"] + LATENCY_BLOCKS
                path = [(s["block"] - base, s["price"]) for s in sw if s["block"] >= base]
                if len(path) < 20:
                    continue
                f.write(json.dumps({"token": r["token"], "curve": r.get("curve"),
                                    "c1": pool["token_is_currency1"],
                                    "n": len(path), "path": path}) + "\n")
                f.flush()
                n += 1
            except Exception:  # noqa: BLE001
                continue
            if i % 20 == 0:
                print(f"  {i}/{len(todo)} ({n} usable)", flush=True)
    print(f"done: {n} new paths", flush=True)


if __name__ == "__main__":
    collect(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
