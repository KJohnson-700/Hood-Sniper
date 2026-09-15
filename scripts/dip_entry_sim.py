#!/usr/bin/env python3
"""
Simulate "wait for the first dip from ATH" against "buy at migration".

Forward-only by construction: `ath` is the high seen so far in the walk, never
the window's high. Both arms pay the same fees and are compared ONLY on tokens
where the dip actually triggered, so the two arms trade the same set.
"""
import json
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
PATHS = os.path.join(DATA, "dip_paths.jsonl")
FEES = 0.07                      # round trip, same for both arms


def run(path, start_idx, tp, stop):
    """Exit from start_idx: first TP hit, else first stop hit, else last price."""
    e = path[start_idx][1]
    if e <= 0:
        return None, None
    peak = e
    for _, px in path[start_idx + 1:]:
        peak = max(peak, px)
        if px >= e * tp:
            return tp, peak / e
        if px <= e * stop:
            return stop, peak / e
    return path[-1][1] / e, peak / e


def dip_entry(path, d, require_runup):
    """
    First index where price has fallen d below the RUNNING ath.

    require_runup: the ath must first exceed the open by that factor, so an
    immediate dump off the open does not count as "a dip from the ATH".
    """
    ath = path[0][1]
    armed = require_runup <= 1.0
    for i, (_, px) in enumerate(path):
        if px > ath:
            ath = px
        if not armed and ath >= path[0][1] * require_runup:
            armed = True
        if armed and i > 0 and px <= ath * (1 - d):
            return i, ath
    return None, ath


def runup_idx(path, mult):
    """First index where price has risen `mult` above the open (forward-only)."""
    if mult <= 1.0:
        return 0
    open_px = path[0][1]
    for i, (_, px) in enumerate(path):
        if px >= open_px * mult:
            return i
    return None


def main():
    rows = [json.loads(l) for l in open(PATHS)]
    rows = [r for r in rows if r.get("n", 0) >= 20]
    print(f"paths: {len(rows)}  (median {st.median([r['n'] for r in rows]):.0f} swaps)\n")
    TP, STOP = 2.0, 0.65
    print(f"exit for BOTH arms: TP {TP}x / stop {STOP}x / else window close, fees {FEES:.0%}\n")
    print(f"{'dip':>6} {'runup':>6} {'fills':>6} {'dip ret':>9} {'buy-now ret':>12} "
          f"{'momentum':>10} {'dip win':>8} {'now win':>8}  verdict")
    print("-" * 90)
    for require_runup in (1.0, 1.10, 1.25):
        for d in (0.10, 0.15, 0.20, 0.30, 0.40):
            dr, nr, mr = [], [], []
            for r in rows:
                p = r["path"]
                i, _ath = dip_entry(p, d, require_runup)
                if i is None:
                    continue                       # no dip -> this arm does not trade
                m_dip, _ = run(p, i, TP, STOP)
                m_now, _ = run(p, 0, TP, STOP)
                if m_dip is None or m_now is None:
                    continue
                dr.append(m_dip * (1 - FEES))
                nr.append(m_now * (1 - FEES))      # MATCHED: same tokens only
                # third arm: buy the CONFIRMATION, not the pullback -- enter the
                # moment the run-up threshold is crossed, forward-only
                j = runup_idx(p, require_runup)
                if j is not None:
                    m_mom, _ = run(p, j, TP, STOP)
                    if m_mom is not None:
                        mr.append(m_mom * (1 - FEES))
            if len(dr) < 15:
                continue
            rd = sum(x - 1 for x in dr) / len(dr)
            rn = sum(x - 1 for x in nr) / len(nr)
            wd = sum(1 for x in dr if x > 1) / len(dr)
            wn = sum(1 for x in nr if x > 1) / len(nr)
            rm = (sum(x - 1 for x in mr) / len(mr)) if mr else None
            best = max([("dip", rd), ("buy-now", rn)] +
                       ([("momentum", rm)] if rm is not None else []),
                       key=lambda kv: kv[1])[0]
            ms = f"{rm:+10.2%}" if rm is not None else f"{'-':>10}"
            print(f"{d:6.0%} {require_runup:6.2f} {len(dr):6d} {rd:+9.2%} {rn:+12.2%} "
                  f"{ms} {wd:8.1%} {wn:8.1%}  best: {best}")
    # selection check: how often does a dip even trigger?
    print()
    for require_runup in (1.0, 1.10, 1.25):
        for d in (0.15, 0.30):
            hit = sum(1 for r in rows if dip_entry(r["path"], d, require_runup)[0] is not None)
            print(f"  dip {d:.0%} runup {require_runup:.2f}: triggers on "
                  f"{hit}/{len(rows)} = {hit/len(rows):.1%} of graduations")


if __name__ == "__main__":
    main()
