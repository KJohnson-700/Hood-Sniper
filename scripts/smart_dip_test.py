#!/usr/bin/env python3
"""
Does dip-timing beat immediate entry GIVEN a validated selection?

Non-circular by construction: the "smart" wallet set is fixed from the TRAIN
block window (persistence_dump.json), and only tokens that graduated STRICTLY
AFTER that window are scored. A wallet cannot have earned its label from the
tokens it is being tested on.

Writes data/smart_dip.jsonl: one record per token with its forward price path
and the number of DISTINCT train-profitable wallets that bought it.
"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "smart_dip.jsonl")
import paper_trade_logger as L
import launch_monitor as M

BLOCK_TIME = 0.101
LATENCY = 30
WINDOW = int(120 * 60 / BLOCK_TIME)

def smart_set():
    d = json.load(open(os.path.join(DATA, "persistence_dump.json")))
    return {w.lower() for w, t in d["train"].items()
            if t.get("closed", 0) >= 3 and (t.get("pnl_eth") or 0) > 0}

def main():
    toks = json.load(open(sys.argv[1]))
    smart = smart_set()
    print(f"train-profitable set: {len(smart)} wallets", flush=True)
    done = set()
    if os.path.exists(OUT):
        for l in open(OUT):
            try: done.add(json.loads(l)["token"])
            except Exception: pass
    todo = [t for t in toks if t["token"] not in done]
    print(f"{len(toks)} clean tokens, {len(done)} cached, doing {len(todo)}", flush=True)
    with open(OUT, "a") as f:
        for i, r in enumerate(todo, 1):
            try:
                pool = L.find_pool(r["token"], r["grad_block"])
                if not pool: continue
                sw = L.pool_swaps(pool["pool_id"], pool["init_block"],
                                  pool["init_block"] + WINDOW,
                                  pool["token_is_currency1"])
                if len(sw) < 20: continue
                base = pool["init_block"] + LATENCY
                path = [(s["block"] - base, s["price"]) for s in sw if s["block"] >= base]
                if len(path) < 20: continue
                # who bought on the curve before/around graduation?
                buyers = set()
                lg = M.get_logs({"fromBlock": hex(max(0, r["grad_block"] - 300_000)),
                                 "toBlock": hex(r["grad_block"] + 2000),
                                 "address": r["curve"],
                                 "topics": [M.T_CURVE_BUY]}) or []
                for x in lg:
                    if len(x["topics"]) >= 3:
                        buyers.add("0x" + x["topics"][2][-40:].lower())
                n_smart = len(buyers & smart)
                f.write(json.dumps({"token": r["token"], "grad_block": r["grad_block"],
                                    "n_buyers": len(buyers), "n_smart": n_smart,
                                    "n": len(path), "path": path}) + "\n")
                f.flush()
            except Exception:
                continue
            if i % 10 == 0: print(f"  {i}/{len(todo)}", flush=True)
    print("done", flush=True)

if __name__ == "__main__":
    main()
