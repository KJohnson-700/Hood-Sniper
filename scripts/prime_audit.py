#!/usr/bin/env python3
"""
PRIME audit — does the default view actually surface tokens that run?

WHY THIS IS NOT THE SAME AS THE 21.74% ALREADY MEASURED. That number came from
replaying history: rows were selected and scored on the same data. Every signal
this project has killed looked fine that way -- the deployer filter, the dip
strategy, the zero-sniper flag. The only test that counts is recording what PRIME
shows BEFORE the outcome exists, then checking back.

    python3 prime_audit.py --snapshot   # record what PRIME shows right now
    python3 prime_audit.py --score      # check what those tokens did since

Run the snapshot repeatedly (cron, or just when the terminal is open). Score after
a few hours. A snapshot taken minutes ago proves nothing -- near-graduation takes
a median 2.2 minutes but the tail runs to 40.
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

SNAP = os.path.join(DATA, "prime_audit.jsonl")
FEED = os.path.join(DATA, "monitor_feed.jsonl")
CROSS = os.path.join(DATA, "grad_forward.jsonl")
MIN_AGE_H = 2.0


def _rows():
    out = {}
    for line in open(FEED):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        c = r.get("curve")
        if not c:
            continue
        prev = out.get(c) or {}
        for k, v in r.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                prev[k] = max(prev.get(k, 0), v)
            elif v is not None:
                prev[k] = v
        out[c] = prev
    return out


def _crossed():
    by = defaultdict(set)
    for line in open(CROSS):
        try:
            x = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if x.get("curve"):
            by[x["curve"]].add(x.get("level"))
    return by


def _clean(e):
    fl = e.get("flags") or []
    return not any(f == "SOLO-EXEMPT" or f.startswith("CONCENTRATED") for f in fl)


MAX_AGE_BLOCKS = 9_000            # ~15 min of chain


def snapshot(log=print):
    """
    Record every curve PRIME would show that we have NOT recorded before.

    Deliberately records at the moment of qualification, so the outcome is still
    unknown. Re-recording an already-seen curve would let a token that ran be
    counted again after the fact.
    """
    rows, cr = _rows(), _crossed()
    # ONLY FRESH CANDIDATES. The first run of this recorded 7,476 curves straight
    # out of accumulated history -- but their outcomes were already fixed, so most
    # were long-dead tokens that would be scored as failures. That biases the
    # result DOWN and is not out-of-sample at all: the point is to record a call
    # while the outcome is still unknown.
    head = M.head_block()
    seen = set()
    if os.path.exists(SNAP):
        for line in open(SNAP):
            try:
                seen.add(json.loads(line)["curve"])
            except Exception:  # noqa: BLE001
                pass
    n = 0
    with open(SNAP, "a") as f:
        for c, e in rows.items():
            if c in seen:
                continue
            if "heating" not in cr.get(c, set()):
                continue
            if not _clean(e):
                continue
            if "near_grad" in cr.get(c, set()):
                continue          # already resolved before we looked -- not a call
            blk = e.get("block") or 0
            if not blk or (head - blk) > MAX_AGE_BLOCKS:
                continue          # too old for the outcome to still be open
            f.write(json.dumps({"ts": time.time(), "curve": c,
                                "symbol": e.get("symbol"),
                                "mcap": e.get("mcap"),
                                "n_buyers": e.get("n_buyers")}) + "\n")
            n += 1
    log(f"  snapshotted {n} new PRIME candidates (total recorded "
        f"{len(seen)+n})")
    return n


def score(min_age_h=MIN_AGE_H, log=print):
    if not os.path.exists(SNAP):
        log("no snapshots yet — run --snapshot first")
        return
    snaps = []
    for line in open(SNAP):
        try:
            snaps.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    ripe = [s for s in snaps if now - s["ts"] >= min_age_h * 3600]
    log(f"  recorded: {len(snaps):,}   old enough to judge (>{min_age_h}h): {len(ripe):,}")
    if not ripe:
        if snaps:
            log(f"  oldest is {(now-min(s['ts'] for s in snaps))/3600:.1f}h — "
                f"let it run longer")
        return
    cr = _crossed()
    hits = [s for s in ripe if "near_grad" in cr.get(s["curve"], set())]
    rate = 100 * len(hits) / len(ripe)
    log(f"\n  OUT-OF-SAMPLE: {len(hits)}/{len(ripe)} reached near-graduation = {rate:.1f}%")
    log(f"  in-sample claim was 21.7%; base rate is 3.3%")
    if rate < 6.6:
        log("  -> PRIME is NOT holding its lift. The view is overfit to the window")
        log("     it was built on, and should be loosened or rebuilt.")
    elif rate < 15:
        log("  -> holding a real but WEAKER lift than claimed. Believe this number,")
        log("     not the in-sample one.")
    else:
        log("  -> holding up out of sample.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--min-age-h", type=float, default=MIN_AGE_H)
    a = ap.parse_args()
    if a.snapshot:
        snapshot()
    elif a.score:
        score(a.min_age_h)
    else:
        ap.print_help()
