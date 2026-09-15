#!/usr/bin/env python3
"""
Build the BSC dev registry from launch events.

Both BSC venues put the creator in the launch event itself, so dev identity is
free here -- no explorer needed (there is no public Blockscout for BSC, which
is why `investigate --chain bsc` reported dev as N-A until this existed).

    python3 build_bsc_dev_registry.py --blocks 20000

Output: data/bsc_dev_registry.json
    {dev: {launches, tokens[], symbols[], first_block, last_block, venues{}}}
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
import bsc_monitor as B  # noqa: E402

OUT = os.path.join(DATA, "bsc_dev_registry.json")


def build(blocks, log=print):
    head = B.head_block()
    lo = head - blocks
    reg = defaultdict(lambda: {"launches": 0, "tokens": [], "symbols": [],
                               "first_block": None, "last_block": None,
                               "venues": defaultdict(int)})
    total = 0
    for key in ("four_meme", "flapsh"):
        v = B.VENUES[key]
        if not v["enabled"]:
            continue
        logs = B.get_logs(v["address"], v["topic"], lo, head)
        log(f"  {key}: {len(logs)} launch events")
        for l in logs:
            ev = B.decode_flapsh(l) if key == "flapsh" else B.decode_launch(l)
            if not ev or not ev.get("creator"):
                continue
            d = reg[ev["creator"].lower()]
            d["launches"] += 1
            d["tokens"].append(ev["token"])
            d["symbols"].append(ev["symbol"])
            d["venues"][key] += 1
            b = ev["block"]
            d["first_block"] = b if d["first_block"] is None else min(d["first_block"], b)
            d["last_block"] = b if d["last_block"] is None else max(d["last_block"], b)
            total += 1
    out = {k: {**v, "venues": dict(v["venues"])} for k, v in reg.items()}
    with open(OUT, "w") as f:
        json.dump({"scanned": [lo, head], "blocks": blocks, "devs": out}, f)
    return out, total, [lo, head]


def main():
    ap = argparse.ArgumentParser(description="Build the BSC dev registry")
    ap.add_argument("--blocks", type=int, default=20000,
                    help="how far back to scan (~0.45s/block)")
    a = ap.parse_args()
    print(f"scanning {a.blocks} blocks (~{a.blocks*0.45/3600:.1f}h) of BSC launches")
    devs, total, rng = build(a.blocks)
    multi = {k: v for k, v in devs.items() if v["launches"] > 1}
    heavy = {k: v for k, v in devs.items() if v["launches"] >= 5}
    print(f"\n  launches indexed : {total}")
    print(f"  distinct devs    : {len(devs)}")
    print(f"  devs with >1     : {len(multi)} ({100*len(multi)/max(len(devs),1):.1f}%)")
    print(f"  devs with >=5    : {len(heavy)}")
    top = sorted(devs.items(), key=lambda x: -x[1]["launches"])[:8]
    print("\n  most prolific devs:")
    for k, v in top:
        syms = ", ".join(dict.fromkeys(v["symbols"]))[:44]
        print(f"    {k} x{v['launches']:<4} {dict(v['venues'])}  {syms}")
    print(f"\n  wrote {OUT}")


if __name__ == "__main__":
    main()
