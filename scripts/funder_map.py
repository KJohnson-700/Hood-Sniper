#!/usr/bin/env python3
"""
On-chain first-funder map — who paid for each wallet, without an indexer.

WHY NOT NANSEN. Nansen's related-wallets DOES return a correct first funder on
Robinhood Chain (9/9 resolved), but it costs ~1 credit per address and the plan ran
dry after nine. The week's runners involve ~1,400 distinct devs, so a real cluster
test is not affordable that way. Blockscout, the other obvious route, is
Cloudflare-blocked (403 from any non-browser client).

WHY NOT BINARY-SEARCH ON BALANCE. The neat trick -- bisect eth_getBalance to find
when a wallet went from zero -- needs archive state. Measured: the RH endpoints keep
roughly 1,000 blocks and return "metadata is not found" beyond that.

SO: one forward pass over blocks, recording every value-bearing transfer, building
recipient -> first sender for EVERY address at once. One scan answers the question
for all 1,400 devs instead of 1,400 lookups. Measured throughput ~57 blocks/sec,
so 50k blocks (~84 min of chain) takes about 15 minutes.

THE POINT OF IT: several "independent" one-off devs sharing a funder is ONE operator
wearing fresh addresses. That is invisible to per-wallet scoring, and it is the
hot-wallet-cluster trap this project flagged in Phase 0 and never had the data to
test.

    python3 funder_map.py --scan 50000      # build/extend
    python3 funder_map.py --runners         # cluster-test the runner log
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
import launch_monitor as M  # noqa: E402

MAP = os.path.join(DATA, "funder_map.json")
CURSOR = os.path.join(DATA, "funder_cursor.json")
BATCH = 25


def load():
    try:
        return json.load(open(MAP))
    except Exception:  # noqa: BLE001
        return {}


def scan(blocks, log=print):
    """
    Walk blocks newest-backwards, recording the FIRST inbound transfer per address.

    Backwards on purpose: recent funding is what matters for recent launches, and
    walking back means every block scanned is immediately useful rather than the
    run having to finish before anything is.

    Because it goes backwards, a later-seen sender is an EARLIER funder, so each
    write overwrites -- the last write for an address is the earliest transfer seen.
    """
    fmap = load()
    head = M.head_block()
    lo = head - blocks
    log(f"scanning {head} → {lo} ({blocks:,} blocks, ~{blocks*0.101/60:.0f} min of chain)",
        flush=True)
    t0 = time.time()
    seen = 0
    b = head
    while b > lo:
        nums = [b - i for i in range(BATCH) if b - i > lo]
        if not nums:
            break
        res = M.rpc_batch([("eth_getBlockByNumber", [hex(n), True]) for n in nums])
        for r in res:
            blk = (r or {}).get("result")
            if not blk:
                continue          # a missing block is skipped, never counted as empty
            for t in (blk.get("transactions") or []):
                to = (t.get("to") or "").lower()
                frm = (t.get("from") or "").lower()
                if not to or not frm or to == frm:
                    continue
                try:
                    if int(t.get("value", "0x0"), 16) <= 0:
                        continue      # zero-value calls are not funding
                except Exception:  # noqa: BLE001
                    continue
                fmap[to] = frm        # backwards walk -> last write is earliest
                seen += 1
        b -= BATCH
        if (head - b) % 5000 < BATCH:
            el = time.time() - t0
            # CHECKPOINT. Holding a 25-minute scan in memory and writing once at the
            # end means a crash, a kill or a laptop sleep loses all of it -- and
            # nothing can be tested until it finishes. Writing every 5k blocks makes
            # the run resumable in practice and the partial map immediately usable.
            json.dump(fmap, open(MAP, "w"))
            log(f"  …{head-b:,}/{blocks:,}  transfers {seen:,}  "
                f"addresses {len(fmap):,}  {el:.0f}s  [saved]", flush=True)
    json.dump(fmap, open(MAP, "w"))
    log(f"done: {seen:,} transfers, {len(fmap):,} funded addresses -> {MAP}")
    return fmap


def cluster_runners(days=7, log=print):
    """Do the week's runner devs share funders? Free — pure local join."""
    fmap = load()
    if not fmap:
        log("no funder map — run --scan first")
        return
    try:
        store = json.load(open(os.path.join(DATA, "runner_log.json")))
    except Exception:  # noqa: BLE001
        log("no runner_log.json — run runner_log.py --update first")
        return
    cut = time.time() - days * 86400
    runners = [r for r in store.get("runners", {}).values()
               if (r.get("ts") or 0) >= cut and r.get("dev")]
    devs = {}
    for r in runners:
        devs.setdefault(r["dev"].lower(), []).append(r)
    resolved = {d: fmap[d] for d in devs if d in fmap}
    log(f"runner devs in the last {days}d: {len(devs):,}")
    log(f"  funder known for: {len(resolved):,}  ({100*len(resolved)/max(len(devs),1):.0f}%)")
    if not resolved:
        log("  none of them were funded inside the scanned window — scan further back")
        return
    byf = defaultdict(list)
    for d, f in resolved.items():
        byf[f].append(d)
    shared = {f: ds for f, ds in byf.items() if len(ds) > 1}
    log(f"  SHARED FUNDERS (one operator, several devs): {len(shared)}\n")
    if not shared:
        log("  Every resolved dev has its own funder. On this sample the week's")
        log("  runners are not one actor wearing fresh addresses.")
        return
    tot = sum(len(ds) for ds in shared.values())
    log(f"  {tot} of {len(resolved)} resolved devs sit in {len(shared)} clusters\n")
    for f, ds in sorted(shared.items(), key=lambda kv: -len(kv[1]))[:12]:
        coins = [str(r.get("symbol")) for d in ds for r in devs[d]][:6]
        log(f"  funder {f}  → {len(ds)} devs")
        log(f"     coins: {', '.join(c for c in coins if c and c != 'None')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", type=int)
    ap.add_argument("--runners", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    if a.scan:
        scan(a.scan)
    elif a.runners:
        cluster_runners(a.days)
    else:
        ap.print_help()
