#!/usr/bin/env python3
"""
Do the two surviving signals STACK -- low creator tax x smart-money participation?

Point-in-time smart set, per token: a wallet counts as "smart" for token T at
block B only if its profitable closed round trips STARTED at least MARGIN blocks
before B. Using the whole-index label would be circular (a wallet is profitable
partly because of T itself); the margin also gives those earlier positions time
to have actually closed before B.
"""
import json, os, sys
from collections import defaultdict
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
DATA=os.path.join(os.path.dirname(HERE),"data")
OUT=os.path.join(DATA,"stack.jsonl")
import launch_monitor as M

MARGIN = 100_000          # ~2.8h of blocks before the token, so priors are settled
MIN_CLOSED = 3

def main():
    ix=json.load(open(os.path.join(DATA,"trader_index.json")))
    pos=ix["positions"]
    # per wallet: list of (first_blk, pnl_eth, closed?)
    hist=defaultdict(list)
    for k,p in pos.items():
        w,_t=k.split("|")
        if p["qin"] < 10**15 or p["buys"] < 1:   # sell-only leg guard
            continue
        closed = p["qout"]>0 and p["sells"]>=1 and p["tok"]<=10**15
        if not closed: continue
        hist[w.lower()].append((p["first_blk"], (p["qout"]-p["qin"])/1e18))
    print(f"wallets with closed round trips: {len(hist)}", flush=True)

    tax=json.load(open("/private/tmp/claude-501/-Users-mainfolder/42e9f951-0852-4ac4-b34f-2a160144053a/scratchpad/tax.json"))
    t2c={}
    for l in open(os.path.join(DATA,"paper_trades.jsonl")):
        try: r=json.loads(l)
        except: continue
        if r.get("token") and r.get("curve"): t2c.setdefault(r["token"],(r["curve"],r["grad_block"]))
    paths={}
    for f in ("dip_paths.jsonl","smart_dip.jsonl"):
        for l in open(os.path.join(DATA,f)):
            r=json.loads(l); paths.setdefault(r["token"],r)
    todo=[t for t in tax if t in t2c and t in paths]
    done=set()
    if os.path.exists(OUT):
        for l in open(OUT):
            try: done.add(json.loads(l)["token"])
            except Exception: pass
    todo=[t for t in todo if t not in done]
    print(f"tokens to score: {len(todo)}", flush=True)

    with open(OUT,"a") as f:
        for i,t in enumerate(todo,1):
            curve,gblk=t2c[t]
            # who is "smart" AS OF this token?
            smart={w for w,legs in hist.items()
                   if sum(1 for b,_ in legs if b < gblk-MARGIN) >= MIN_CLOSED
                   and sum(p for b,p in legs if b < gblk-MARGIN) > 0}
            lg=M.get_logs({"fromBlock":hex(max(0,gblk-300_000)),"toBlock":hex(gblk+2000),
                           "address":curve,"topics":[M.T_CURVE_BUY]}) or []
            buyers={"0x"+x["topics"][2][-40:].lower() for x in lg if len(x["topics"])>=3}
            f.write(json.dumps({"token":t,"grad_block":gblk,"tax":tax[t]["tax"],
                                "fee":tax[t]["fee"],"n_buyers":len(buyers),
                                "n_smart":len(buyers&smart),
                                "smart_pool":len(smart)})+"\n"); f.flush()
            if i%20==0: print(f"  {i}/{len(todo)}", flush=True)
    print("done", flush=True)

if __name__=="__main__":
    main()
