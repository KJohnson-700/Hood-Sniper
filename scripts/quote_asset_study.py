#!/usr/bin/env python3
"""
Does the QUOTE ASSET of a Pons graduation predict the outcome?

Origin: in the live paper-trade log, trades whose est_slippage_pct came back
exactly 0.0 hit TP 38.0% of the time vs 6.4% for everything else (Fisher
p=2.7e-25). That zero was a BUG -- price_impact() assumes an 18-decimal ETH
quote, so non-ETH-quoted pools underflow to 0 -- but the bug happened to flag
non-ETH pools, and those are the ones that run. This measures the real thing:
resolve each pool's quote symbol on chain and compare outcomes.

Resumable: appends to data/quote_study.jsonl, skips tokens already resolved.
"""
import json, os, sys, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from investigate import pool_state
import launch_monitor as M

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "quote_study.jsonl")
SRC = os.path.join(DATA, "paper_trades.jsonl")

def main(limit=400):
    rows = [json.loads(l) for l in open(SRC) if '"outcome"' in l]
    s = [r for r in rows if r.get("outcome") not in (None, "OPEN")
         and r.get("net_mult") is not None]
    done = set()
    if os.path.exists(OUT):
        for l in open(OUT):
            try: done.add(json.loads(l)["token"])
            except Exception: pass
    todo = [r for r in s if r["token"] not in done]
    random.seed(7); random.shuffle(todo)          # unbiased sample, not newest-first
    todo = todo[:limit]
    print(f"{len(s)} settled trades, {len(done)} already resolved, probing {len(todo)}")
    head = M.head_block()
    n = 0
    with open(OUT, "a") as f:
        for i, r in enumerate(todo, 1):
            try:
                st = pool_state(r["token"], max(0, r["grad_block"] - 300),
                                r["grad_block"] + 6000, head, 25.0)
            except Exception as e:
                st = None
            rec = {"token": r["token"], "ts": r["ts_utc"],
                   "quote": (st or {}).get("quote_symbol"),
                   "true_slip": (st or {}).get("slippage_pct"),
                   "liq_usd": (st or {}).get("active_liq_usd"),
                   "logged_slip": r["est_slippage_pct"],
                   "outcome": r["outcome"], "net_mult": r["net_mult"],
                   "peak_mult": r["peak_mult"], "pnl_usd": r["pnl_usd"],
                   "stake_usd": r["stake_usd"]}
            f.write(json.dumps(rec) + "\n"); f.flush()
            n += 1
            if i % 25 == 0: print(f"  {i}/{len(todo)}  (last quote={rec['quote']})", flush=True)
    print(f"done, {n} newly resolved")

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
