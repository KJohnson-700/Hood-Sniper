#!/usr/bin/env python3
"""
Early-winner index — wallets that bought big runners while they were still cheap.

WHAT THIS ASKS that the holder index does not: not "did this wallet's picks 2x"
but "did this wallet get into something that went on to run HARD, while it was
still under $12k". Those are different questions. A wallet that reliably catches
2x moves is a good scalper; a wallet that catches the $500k runners early is what
Slim is actually hunting.

HYGIENE IS NOT OPTIONAL HERE. The first pass produced a leaderboard whose top three
entries were CONTRACTS -- including a vanity address (0xb1000000...) -- exactly the
failure that put routers and bots on the smart-money list before. So every candidate
is checked for bytecode and for an implausible nonce before it can be listed.

DATA CAVEATS, stated because they change how much weight this carries:
  * holder_events logs the CURVE address, not the token. Grouping is fine; do not
    treat the id as an ERC20.
  * Only curves the monitor independently verified as Pons are used. Unrestricted,
    the topic matches other contracts and produces $500B "market caps".
  * The window is whatever holder_events covers -- about 6 days, not an arbitrary
    date range.

    python3 early_winners.py --scan            # build
    python3 early_winners.py --scan --min-run 250000 --max-entry 12000
"""
import argparse
import json
import os
import sys
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
import launch_monitor as M  # noqa: E402

EVENTS = os.path.join(DATA, "holder_events.jsonl")
FEED = os.path.join(DATA, "monitor_feed.jsonl")
OUT = os.path.join(DATA, "early_winners_index.json")
ETH = 2450.0
SUPPLY = 1e9


def verified_curves(log=print):
    """Only curves the monitor itself saw and tagged as Pons."""
    known = {}
    for line in open(FEED):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        c = r.get("curve")
        if c and (r.get("venue") or "pons") == "pons":
            known[c] = r.get("symbol") or known.get(c)
    return known


def scan(min_run=500_000, max_entry=12_000, min_events=5, log=print):
    known = verified_curves(log)
    lo = max_entry / (SUPPLY * ETH)
    hi = min_run / (SUPPLY * ETH)
    ev = defaultdict(list)
    for line in open(EVENTS):
        try:
            b, t, w, p, buy = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if p > 0 and t in known:
            ev[t].append((b, w, p, buy))
    log(f"  verified curves with prices: {len(ev):,}")

    winners, early = {}, Counter()
    tok_of = defaultdict(set)
    for t, rows in ev.items():
        if len(rows) < min_events:
            continue
        ps = sorted(r[2] for r in rows)
        # p95 rather than max: one tiny trade produces a garbage ratio, and the
        # first version of this reported $508 BILLION market caps because of it
        if ps[int(len(ps) * 0.95)] < hi:
            continue
        winners[t] = known.get(t)
        for b, w, p, buy in rows:
            if buy and p <= lo:
                early[w] += 1
                tok_of[w].add(t)
    log(f"  curves reaching >=${min_run:,}: {len(winners):,}")
    log(f"  wallets that got in under ${max_entry:,}: {len(early):,}")
    if not early:
        return {}

    cands = list(early)
    codes, nonces = {}, {}
    # 12 wallets = 24 requests. Sending 25 pairs meant 50 requests in one batch,
    # which the endpoint silently truncated -- 25 of 41 came back unverified.
    for i in range(0, len(cands), 12):
        ch = cands[i:i + 12]
        res = M.rpc_batch([("eth_getCode", [w, "latest"]) for w in ch]
                          + [("eth_getTransactionCount", [w, "latest"]) for w in ch])
        n = len(ch)
        for j, w in enumerate(ch):
            # None means UNREADABLE, which is not the same as "no bytecode".
            # Defaulting to "0x" made the check FAIL OPEN: when the batch dropped,
            # every candidate passed as an EOA and two known contracts walked
            # straight back onto the smart list.
            cr = (res[j] or {}).get("result")
            nr = (res[n + j] or {}).get("result")
            codes[w] = cr if cr is not None else None
            nonces[w] = M.call_int(nr) if nr is not None else None

    out, rejected = {}, Counter()
    for w in cands:
        code, nonce = codes.get(w), nonces.get(w)
        if code is None or nonce is None:
            rejected["unverified(rpc failed)"] += 1
            continue                       # cannot vouch for it -> do not list it
        if len(code) > 2:
            rejected["contract"] += 1
            continue
        if nonce > 5000:
            rejected["bot(nonce>5k)"] += 1
            continue
        out[w.lower()] = {"wallet": w.lower(), "winners": len(tok_of[w]),
                          "early_buys": early[w], "nonce": nonces.get(w),
                          "tokens": sorted(tok_of[w])[:8],
                          "source": "early_winner"}
    log(f"  rejected: {dict(rejected)}")
    log(f"  kept: {len(out)}")
    json.dump(out, open(OUT, "w"), indent=1)
    log(f"  -> {OUT}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--min-run", type=int, default=500_000)
    ap.add_argument("--max-entry", type=int, default=12_000)
    a = ap.parse_args()
    if a.scan:
        r = scan(a.min_run, a.max_entry)
        for w, v in sorted(r.items(), key=lambda kv: -kv[1]["winners"])[:15]:
            print(f"    {w}  winners={v['winners']:<3} buys={v['early_buys']:<3} "
                  f"nonce={v['nonce']}")
    else:
        ap.print_help()
