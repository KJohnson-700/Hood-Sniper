#!/usr/bin/env python3
"""
Build the dev-wallet registry that the vetting layer scores against.

Without this, every dev looks brand new on day one and "prior graduations" --
the only filter that reached significance (2.20x lift, Fisher p=0.007) -- is
identically zero for weeks. Seeding from history makes the signal live
immediately.

Sources (already collected in Phase 0):
  data/pons_launches.json      17,684 Launched events -> dev identity per token
  data/pons_graduations.json   graduations by curve

Output:
  data/dev_registry.json  { dev: {launches, graduations, curves, tokens,
                                  first_block, last_block, grad_blocks} }
"""
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")


def build():
    with open(os.path.join(DATA, "pons_launches.json")) as f:
        launches = json.load(f)["events"]
    with open(os.path.join(DATA, "pons_graduations.json")) as f:
        grads = json.load(f)["events"]

    grad_block = {c.lower(): v["block"] for c, v in grads.items()}
    reg = defaultdict(lambda: {"launches": 0, "graduations": 0, "curves": [],
                               "graduated_tokens": [], "grad_blocks": [],
                               "first_block": None, "last_block": None})

    for v in launches.values():
        dev = (v.get("launcher") or "").lower()
        if not dev or dev == "0x" + "0" * 40:
            continue
        curve = (v.get("curve") or "").lower()
        r = reg[dev]
        r["launches"] += 1
        r["curves"].append(curve)
        b = v.get("block")
        if b is not None:
            r["first_block"] = b if r["first_block"] is None else min(r["first_block"], b)
            r["last_block"] = b if r["last_block"] is None else max(r["last_block"], b)
        if curve in grad_block:
            r["graduations"] += 1
            r["graduated_tokens"].append(v.get("token"))
            r["grad_blocks"].append(grad_block[curve])

    out = {k: v for k, v in reg.items()}
    for v in out.values():
        v["grad_blocks"].sort()
    path = os.path.join(DATA, "dev_registry.json")
    with open(path, "w") as f:
        json.dump(out, f)

    n = len(out)
    withg = sum(1 for v in out.values() if v["graduations"] > 0)
    multi = sum(1 for v in out.values() if v["graduations"] > 1)
    print(f"devs indexed:            {n}")
    print(f"  with >=1 graduation:   {withg}  ({100*withg/n:.1f}%)")
    print(f"  with >=2 graduations:  {multi}")
    print(f"  total launches:        {sum(v['launches'] for v in out.values())}")
    print(f"  total graduations:     {sum(v['graduations'] for v in out.values())}")
    print(f"wrote {path}")
    top = sorted(out.items(), key=lambda x: -x[1]["graduations"])[:8]
    print("\ntop devs by graduations:")
    for k, v in top:
        rate = 100 * v["graduations"] / v["launches"]
        print(f"  {k} launches={v['launches']:<5} grads={v['graduations']:<3} rate={rate:5.1f}%")


if __name__ == "__main__":
    build()
