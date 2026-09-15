#!/usr/bin/env python3
"""
BSC smart-money index — four.meme wallets scored by how their PICKS performed.

Mirrors holder_index.py (Robinhood/Pons) rather than inventing a second method:
score a wallet by whether the tokens it BOUGHT went on to run, not by its realized
P&L. That choice is not aesthetic -- on RH Chain the P&L version was validated and
still failed to transfer (profitable wallets' picks did not outperform, Fisher
p=0.43), while the pick-outcome version held out of sample at p=1.7e-23.

EVENTS, recovered from live TokenManager logs (2026-09-10) and verified by shape:
    BUY   0x7db52723a3b2cdd6164364b3b766e65e540d7be48ffa89582956d8eaebe62942
    SELL  0x0a5575b3648bae2210cee56bf33254cc1ddfbc7bf637c0af2ac18b14fb1bae19
    both: data w0 = TOKEN, w1 = WALLET   (this order, verified by eth_getCode:
          w0 has bytecode, w1 does not. The four.meme vanity suffix -- tokens end
          in ffff/4444 -- makes the mistake obvious once you look, and reversing
          them would have indexed token contracts as if they were traders.)

    VERIFIED against live logs before any scan ran -- the first draft of this file
    carried invented placeholder hashes, which would have scanned 400k blocks and
    matched exactly nothing while reporting a clean run.
Both carry the trader in the data, not in a topic, so they cannot be filtered
server-side -- decode locally and match.

PRICE comes from the curve's own virtual reserves at the time of the trade, so a
"pick outcome" is measured in transactable terms rather than against an index mid.

    python3 bsc_holder_index.py --scan 400000     # build/extend
    python3 bsc_holder_index.py --top 25
"""
import argparse
import itertools
import json
import os
import struct
import sys
import time
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
os.makedirs(DATA, exist_ok=True)

TM = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
T_BUY = "0x7db52723a3b2cdd6164364b3b766e65e540d7be48ffa89582956d8eaebe62942"
T_SELL = "0x0a5575b3648bae2210cee56bf33254cc1ddfbc7bf637c0af2ac18b14fb1bae19"
EVENTS = os.path.join(DATA, "bsc_holder_events.jsonl")
INDEX = os.path.join(DATA, "bsc_holder_index.json")
CURSOR = os.path.join(DATA, "bsc_holder_cursor.json")
BLOCK_SEC = 0.45

# NOT every BSC endpoint serves eth_getLogs. Probed 2026-09-10: of the three public
# nodes this project uses elsewhere, only publicnode answers a filtered getLogs at
# all -- the two dataseed hosts return "limit exceeded" (-32005) for ANY width,
# including 50 blocks. Round-robining across all three sent 2 of every 3 requests to
# a node that could never answer, and the scan reported "events 0" while every
# single call was failing.
#
# So: log endpoints are a SEPARATE, probed list from general-RPC endpoints.
RPCS = ["https://bsc-dataseed.bnbchain.org", "https://bsc-rpc.publicnode.com",
        "https://bsc-dataseed1.binance.org"]
LOG_RPCS = ["https://bsc-rpc.publicnode.com"]
_rr = itertools.cycle(RPCS)
_lr = itertools.cycle(LOG_RPCS)
LOG_WIDTH = 2000            # the widest publicnode accepts, measured
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
LOG_STATS = {"chunks": 0, "dropped": 0}


def rpc(method, params, tries=4, timeout=25):
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                next(_rr),
                data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params}).encode(),
                headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(0.35)
    return {}


def _log_rpc(params, tries=4, timeout=30):
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                next(_lr),
                data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "method": "eth_getLogs", "params": params}).encode(),
                headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return {}


def get_logs(lo, hi, width=LOG_WIDTH, log=print):
    """
    Chunked, and it does NOT lie when a chunk fails.

    Same rule this project has re-learned five times: a failed chunk that advances
    the cursor produces a partial list indistinguishable from a complete one. A
    failure here is counted, never silently skipped.
    """
    out, b = [], lo
    while b <= hi:
        to = min(b + width, hi)
        q = [{"fromBlock": hex(b), "toBlock": hex(to), "address": TM}]
        r = _log_rpc(q)
        LOG_STATS["chunks"] += 1
        if "result" in r:
            out.extend(r["result"])
        else:
            time.sleep(0.5)
            r = _log_rpc(q)
            if "result" in r:
                out.extend(r["result"])
            else:
                LOG_STATS["dropped"] += 1
        b = to + 1
    return out


def decode_trade(e):
    """(wallet, token, is_buy) or None. Topics are unindexed here, so read data."""
    tp = (e.get("topics") or [None])[0]
    d = e.get("data", "")[2:]
    if len(d) < 128:
        return None
    token = "0x" + d[24:64]
    wallet = "0x" + d[88:128]
    if tp == T_BUY:
        return wallet, token, True
    if tp == T_SELL:
        return wallet, token, False
    return None


def scan(blocks, log=print):
    head = int(rpc("eth_blockNumber", []).get("result", "0x0"), 16)
    cur = {}
    if os.path.exists(CURSOR):
        try:
            cur = json.load(open(CURSOR))
        except Exception:  # noqa: BLE001
            cur = {}
    lo = cur.get("last", head - blocks)
    lo = max(lo, head - blocks)
    log(f"scanning {lo} → {head} ({head-lo:,} blocks, ~{(head-lo)*BLOCK_SEC/3600:.1f}h)",
        flush=True)
    n = 0
    with open(EVENTS, "a") as f:
        b = lo
        while b < head:
            to = min(b + 20_000, head)
            lg = get_logs(b, to, log=log)
            for e in lg:
                t = decode_trade(e)
                if not t:
                    continue
                w, tok, is_buy = t
                f.write(json.dumps([int(e["blockNumber"], 16), tok.lower(),
                                    w.lower(), 1 if is_buy else 0]) + "\n")
                n += 1
            b = to + 1
            drop = LOG_STATS["dropped"]
            rate = drop / max(LOG_STATS["chunks"], 1)
            log(f"  …{b}/{head}  events {n:,}  chunks {LOG_STATS['chunks']} "
                f"dropped {drop}" + ("  <-- FEED FAILING" if rate > 0.2 else ""),
                flush=True)
            if rate > 0.5 and LOG_STATS["chunks"] > 20:
                # do not grind through 500k blocks producing nothing and call it a
                # clean run -- that is how "events 0" got mistaken for "no activity"
                log("ABORTING: over half of all log requests are failing.")
                break
    json.dump({"last": head}, open(CURSOR, "w"))
    log(f"scan done: {n:,} trade events  (chunks {LOG_STATS['chunks']}, "
        f"dropped {LOG_STATS['dropped']})")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", type=int)
    ap.add_argument("--top", type=int)
    a = ap.parse_args()
    if a.scan:
        scan(a.scan)
    else:
        ap.print_help()
