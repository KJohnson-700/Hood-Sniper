#!/usr/bin/env python3
"""
Holder-style wallet index: score wallets by WHAT THEY BOUGHT, not by what they closed.

Why this exists. `trader_index.py` scores realized P&L on CLOSED round trips inside a
short window. It validated beautifully -- profitable wallets stayed profitable
out-of-sample (58-62% vs 15-19%, permutation 0/5000) -- and then FAILED to transfer:
tokens those wallets bought did not outperform (p=0.43). The diagnosis: those wallets
are fast FLIPPERS (median 5 closed round trips in 34h). A scalper can be persistently
profitable without the tokens they touch ever running. Separately, of 58 GMGN
"smart_degen" wallets, the 15 our index had seen ALL had zero closed round trips and a
median of 4 OPEN positions -- holders are invisible to a closed-trip metric.

So this scores the thing we actually want to follow:

    wallet score = how the tokens it BOUGHT performed AFTER it bought them

Forward-only: for each buy at block B, the multiple is the max price strictly AFTER B
divided by the price paid. No look-ahead -- a buy is never credited with a peak that
happened before it. Curve BUY/SELL events carry raw quote and token amounts, so
price = quote/token directly (both 18-dec, decimals cancel) with no pool reads at all.

    python3 holder_index.py --scan 1500000      # collect events (resumable)
    python3 holder_index.py --build             # score wallets
"""
import argparse, json, os, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
DATA = os.path.join(os.path.dirname(HERE), "data")
EVENTS = os.path.join(DATA, "holder_events.jsonl")
INDEX = os.path.join(DATA, "holder_index.json")
STATE = os.path.join(DATA, "holder_scan_state.json")

import trader_index as T
import launch_monitor as M

ROUTERS = getattr(T, "ROUTERS", set())
CHUNK = 4000


def _state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:  # noqa: BLE001
            pass
    return {"ranges": [], "cursor": None}


def scan(lo, hi, log=print):
    """
    Stream curve buys/sells to disk. Memory stays flat regardless of range.

    Records a `cursor` after every chunk, so --update can resume from chain head
    without rescanning, and an interrupted run loses at most one chunk instead of
    the whole scan.
    """
    st = _state()
    n = 0
    with open(EVENTS, "a") as f:
        b = lo
        while b < hi:
            to = min(b + CHUNK, hi)
            r = T.rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                       "topics": [[T.T_CURVE_BUY, T.T_CURVE_SELL]]}])
            if "result" not in r:
                b = to
                continue
            for lg in r["result"]:
                if len(lg["topics"]) < 3:
                    continue
                w = "0x" + lg["topics"][2][-40:]
                if w in ROUTERS:
                    continue
                d = lg["data"][2:]
                if len(d) < 128:
                    continue
                q = int(d[0:64], 16); t = int(d[64:128], 16)
                buy = lg["topics"][0] == T.T_CURVE_BUY
                # BUY: word0=quote in, word1=tokens out. SELL: word0=tokens in,
                # word1=quote out. Verified on chain against tx.value.
                if buy:
                    if t == 0: continue
                    price = q / t
                else:
                    if q == 0: continue
                    price = t / q
                f.write(json.dumps([int(lg["blockNumber"], 16), lg["address"].lower(),
                                    w, price, 1 if buy else 0]) + "\n")
                n += 1
            b = to
            # persist progress every chunk -- an interrupt costs one chunk, not the run
            st["cursor"] = b
            json.dump(st, open(STATE, "w"))
            if (b - lo) % (CHUNK * 25) == 0:
                log(f"  {b}  events={n}", flush=True)
    st["ranges"].append([lo, hi]); st["cursor"] = hi
    json.dump(st, open(STATE, "w"))
    log(f"scan done: {n} new events", flush=True)
    return n


MAX_CATCHUP = 400_000     # cap one --update so a long gap spreads over runs


def update(log=print):
    """
    Extend the index forward from the cursor to chain head, then rebuild.

    This is what keeps the star column current. Capped per run so a laptop that
    slept for a day walks the backlog forward instead of one run stalling for
    hours -- the same shape as the BSC snapshot loop.
    """
    st = _state()
    head = M.head_block()
    cur = st.get("cursor")
    if not cur:
        rngs = st.get("ranges") or []
        cur = max((r[1] for r in rngs), default=head - 200_000)
    lo = cur + 1
    hi = min(head, lo + MAX_CATCHUP)
    if hi <= lo:
        log(f"already at head ({head})")
        return 0
    log(f"update {lo} -> {hi} ({hi-lo:,} blocks, {head-hi:,} behind head)", flush=True)
    n = scan(lo, hi, log=log)
    build(log=log)
    return n


MIN_BUY_PRICE = 0.0


def build(min_picks=3, log=print):
    """Group by token, take the forward max AFTER each buy, aggregate per wallet."""
    by_token = defaultdict(list)
    n = 0
    for line in open(EVENTS):
        try:
            blk, tok, w, px, buy = json.loads(line)
        except Exception:
            continue
        if px <= 0:
            continue
        by_token[tok].append((blk, w, px, buy))
        n += 1
    log(f"events {n:,} across {len(by_token):,} tokens", flush=True)

    picks = defaultdict(list)          # wallet -> [forward multiple]
    last_blk = {}                      # wallet -> most recent block it traded in
    for tok, evs in by_token.items():
        if len(evs) < 10:              # untraded dust: no forward path to speak of
            continue
        evs.sort(key=lambda x: x[0])
        # suffix max price, strictly AFTER each index -- this is the forward-only part
        n_e = len(evs)
        suffix = [0.0] * (n_e + 1)
        for i in range(n_e - 1, -1, -1):
            suffix[i] = max(suffix[i + 1], evs[i][2])
        for i, (blk, w, px, buy) in enumerate(evs):
            if not buy:
                continue
            fwd = suffix[i + 1]        # strictly after
            if fwd <= 0:
                continue
            picks[w].append(fwd / px)
            if blk > last_blk.get(w, 0):
                last_blk[w] = blk

    out = {}
    for w, ms in picks.items():
        if len(ms) < min_picks:
            continue
        ms_s = sorted(ms)
        out[w] = {"picks": len(ms),
                  "median_fwd": ms_s[len(ms_s) // 2],
                  "mean_fwd": sum(ms) / len(ms),
                  "hit2x": sum(1 for m in ms if m >= 2.0) / len(ms),
                  "hit5x": sum(1 for m in ms if m >= 5.0) / len(ms),
                  # LAST SEEN. Without this a wallet's score is timeless and the
                  # smart-money list rots invisibly: measured 2026-09-23, the 243
                  # live entries had a MEDIAN 316.7 hours since their last trade,
                  # only 1 of 243 had traded in the past 15 minutes, and the star
                  # column was therefore empty on every row. Good wallets, dead list.
                  "last_block": last_blk.get(w, 0)}
    json.dump(out, open(INDEX, "w"))
    log(f"scored {len(out):,} wallets with >={min_picks} picks -> {INDEX}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", type=int)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--update", action="store_true",
                    help="extend from the stored cursor to chain head, then rebuild")
    ap.add_argument("--min-picks", type=int, default=3)
    a = ap.parse_args()
    if a.scan:
        head = M.head_block()
        scan(head - a.scan, head)
    if a.update:
        update()
    if a.build:
        build(a.min_picks)


if __name__ == "__main__":
    main()
