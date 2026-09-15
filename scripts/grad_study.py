#!/usr/bin/env python3
"""
Forward test: does crossing a curve threshold actually predict graduation?

The monitor journals every FIRST crossing of 0.10 ETH (heating) and 2.0 ETH (near
grad) to data/grad_forward.jsonl as it happens. This reads those crossings back and
checks, on chain, which ones went on to graduate.

WHY IT IS A FORWARD TEST AND NOT A BACKTEST: crossings are recorded BEFORE the
outcome is known, so there is no way to select the sample after the fact. Every
signal this project has killed -- the deployer filter, the dip strategy, the
zero-sniper flag -- looked fine until it was tested out of sample. Population is
already measured (graduation fires at ~4.0 ETH; ~1.6% of launches get there). What
is NOT yet measured is the conditional probability, which is the only number that
justifies acting on the panel.

Give it hours before reading it. A crossing logged 5 minutes ago has not had time
to graduate OR to die, and counting it as a failure would understate the rate.

    python3 grad_study.py                 # report
    python3 grad_study.py --min-age-h 3   # only crossings old enough to have resolved
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
import launch_monitor as M  # noqa: E402

JOURNAL = os.path.join(DATA, "grad_forward.jsonl")


def load():
    if not os.path.exists(JOURNAL):
        return []
    out = []
    for line in open(JOURNAL):
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def graduated_set(since_block, log=print):
    """Curves that fired CurveCompleted since `since_block`."""
    head = M.head_block()
    lg = M.get_logs({"fromBlock": hex(max(0, since_block)), "toBlock": hex(head),
                     "topics": [M.T_CURVE_COMPLETED]})
    log(f"  graduation events scanned: {len(lg)}")
    return {e["address"].lower() for e in lg}, head


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-h", type=float, default=2.0,
                    help="ignore crossings younger than this (not yet resolved)")
    a = ap.parse_args()

    rows = load()
    if not rows:
        print("no crossings recorded yet — the monitor journals them as they happen.")
        print(f"expected at {JOURNAL}")
        return
    now = time.time()
    ripe = [r for r in rows if (now - r["ts"]) >= a.min_age_h * 3600]
    print(f"crossings recorded: {len(rows)}   old enough to judge "
          f"(>{a.min_age_h}h): {len(ripe)}")
    if not ripe:
        oldest = (now - min(r["ts"] for r in rows)) / 3600
        print(f"  oldest is {oldest:.1f}h — let it run longer before reading this.")
        return

    first_block = min((r.get("block") or 0) for r in ripe) or 0
    grad, head = graduated_set(first_block - 1000)

    print(f"\n{'level':<12}{'crossed':>9}{'graduated':>11}{'P(grad | crossed)':>20}")
    for lvl in ("heating", "near_grad"):
        sub = [r for r in ripe if r["level"] == lvl]
        if not sub:
            print(f"  {lvl:<10}{0:>9}{'—':>11}{'—':>20}")
            continue
        g = sum(1 for r in sub if (r.get("curve") or "").lower() in grad)
        print(f"  {lvl:<10}{len(sub):>9}{g:>11}{100*g/len(sub):>19.1f}%")

    base = 1.58   # measured: 102 of 6,469 launches graduated over 5h
    print(f"\n  base rate for ANY launch: {base}%  (measured, 102/6,469 over 5h)")
    print("  a level only earns its place if P(grad | crossed) clears that by a wide")
    print("  margin -- and with small n, a few percentage points is noise, not edge.")


if __name__ == "__main__":
    main()
