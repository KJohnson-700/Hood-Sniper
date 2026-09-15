#!/usr/bin/env python3
"""
Offline replay of entry/exit rules over cached curve tapes.

THE QUESTION THIS EXISTS TO ANSWER
    Buying at graduation is dead -- proven at zero fees (pf 0.96, 93.6% stop out
    at 0.70). The signal that IS validated predicts graduation (13.2% vs a 3.3%
    base, n=1,226 out-of-sample). So the only live question is whether the
    PRE-graduation leg pays: buy on the curve, sell into graduation.

PROGRESS IS MEASURED IN TOKENS SOLD, NOT QUOTE RAISED
    Quote raised cannot be compared across curves: the quote asset is native ETH
    on only 62.9% of them and the graduation target differs per quote (4.2 ETH /
    8,090 USDG / 41.6 NVDA). Token supply, by contrast, is identical on every
    curve -- 714,285,714 sellable, with 285,714,285 reserved for the LP -- which
    was verified constant across every graduated curve sampled.

    So "25% of the way to graduation" means 25% of sellable tokens sold. That is
    uniform, needs no decimals, and needs no quote lookup -- which matters because
    the curve does not self-describe (trackedQuote() returns None).

SURVIVORSHIP
    Run this over the `crossings` tape set, not the `grads` set. Sourcing from
    graduations answers "what did the winners return"; only 19.1% of heating
    curves ever reach near-graduation, so the 80.9% that stall ARE the result.
    Each curve's outcome is read from its own tape -- a CurveCompleted event is
    present or it is not -- never assumed from the source list.

PRICES ARE EFFECTIVE FILLS, NOT SPOT
    Each trade's price is quote/token for that fill, so it already carries that
    trader's size impact and fee. Entry uses a BUY price (what a buyer paid,
    fee included) and exit uses a SELL price (what a seller received, fee
    deducted), so a round trip carries the real spread rather than a modelled
    one. The size is whoever happened to trade, not ours -- stated as a known
    limitation, not silently assumed away.
"""
import json
import os
import sys
import argparse
import statistics as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAPES = os.path.join(HERE, "data", "curve_tapes.jsonl")

SELLABLE = 714_285_714 * 10 ** 18     # constant across every curve checked
BUY, SELL = 0, 1


def load(path=TAPES, need_trades=5, src="crossings"):
    """
    src="crossings" is the DEFAULT and the only unbiased set. The grads-sourced
    tapes are 100% graduated by construction (verified: 17/17) and including them
    drove a 4.0x mean return that collapsed once they were removed.
    """
    out, bad = {}, 0
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            out[r["curve"]] = r          # later line wins (repairs)
    recs = []
    for r in out.values():
        # provenance: older crossings rows predate the src tag but are the only
        # ones written with a null quote, so that identifies them unambiguously
        rsrc = r.get("src") or ("crossings" if r.get("quote") is None else "grads")
        if src and rsrc != src:
            continue
        if not r.get("ok"):
            bad += 1
            continue
        if len(r.get("trades") or []) < need_trades:
            continue
        recs.append(r)
    return recs, bad, len(out)


def walk(rec):
    """Yield (idx, block, kind, price, progress) with progress = tokens sold / SELLABLE."""
    cum = 0
    for i, (blk, kind, q, tok, fee) in enumerate(rec["trades"]):
        cum += tok if kind == BUY else -tok
        yield i, blk, kind, q / tok, max(0.0, cum / SELLABLE)


def leg(rec, enter_at, exit_at=None):
    """
    Enter on the first BUY at/after `enter_at` progress. Exit on the last SELL
    at/before `exit_at` progress (default: graduation, i.e. the end of the tape).

    Returns None when the curve never reaches the entry point, or when no sell
    exists to price the exit -- never a fabricated fill.
    """
    ent = None
    best_exit = None
    for i, blk, kind, px, prog in walk(rec):
        if ent is None:
            if prog >= enter_at and kind == BUY:
                ent = {"i": i, "block": blk, "px": px, "prog": prog}
            continue
        if exit_at is not None and prog > exit_at:
            break
        if kind == SELL:
            best_exit = {"i": i, "block": blk, "px": px, "prog": prog}
    if not ent or not best_exit or best_exit["i"] <= ent["i"]:
        return None
    return {"entry_px": ent["px"], "exit_px": best_exit["px"],
            "ret": best_exit["px"] / ent["px"],
            "entry_prog": ent["prog"], "exit_prog": best_exit["prog"],
            "blocks": best_exit["block"] - ent["block"]}


def graduated(rec):
    return bool(rec.get("completed"))


def peak_after(rec, enter_at):
    """Max price reached at/after the entry point, and the final price."""
    ent = None
    pk = None
    last = None
    for i, blk, kind, px, prog in walk(rec):
        if ent is None:
            if prog >= enter_at and kind == BUY:
                ent = px
            continue
        pk = px if pk is None else max(pk, px)
        last = px
    if ent is None or pk is None:
        return None
    return {"entry": ent, "peak_mult": pk / ent, "last_mult": last / ent}


def report(recs, enter_at):
    rows, grads, stalls = [], 0, 0
    for r in recs:
        res = leg(r, enter_at)
        if res is None:
            continue
        res["grad"] = graduated(r)
        grads += res["grad"]
        stalls += (not res["grad"])
        rows.append(res)
    if not rows:
        print(f"  entry@{enter_at:.0%}: no curves reached this point")
        return None
    rets = [x["ret"] for x in rows]
    g = [x["ret"] for x in rows if x["grad"]]
    s = [x["ret"] for x in rows if not x["grad"]]
    print(f"\n  ENTRY at {enter_at:.0%} of supply sold   n={len(rows)}   "
          f"graduated={grads} ({100*grads/len(rows):.1f}%)  stalled={stalls}")
    print(f"    all      median {st.median(rets):.3f}x   mean {st.mean(rets):.3f}x   "
          f">1x: {100*sum(1 for x in rets if x>1)/len(rets):.1f}%")
    if g:
        print(f"    graduated median {st.median(g):.3f}x   mean {st.mean(g):.3f}x   n={len(g)}")
    if s:
        print(f"    stalled   median {st.median(s):.3f}x   mean {st.mean(s):.3f}x   n={len(s)}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enter", type=float, nargs="*",
                    default=[0.05, 0.10, 0.25, 0.50, 0.75])
    ap.add_argument("--src", default="crossings", choices=("crossings", "grads", ""),
                    help="crossings = unbiased; grads = winners only (biased)")
    ap.add_argument("--tax-bps", type=float, default=200.0,
                    help="round-trip curve tax; median measured is 200 bps")
    a = ap.parse_args()

    recs, bad, tot = load(src=a.src)
    print(f"  tapes: {tot:,} cached, {len(recs):,} usable, {bad:,} failed scans "
          f"(failed are excluded, NOT counted as no-trade)")
    ng = sum(1 for r in recs if graduated(r))
    print(f"  graduated: {ng:,}/{len(recs):,} = {100*ng/len(recs):.1f}%   "
          f"stalled: {len(recs)-ng:,}")

    for e in a.enter:
        rows = report(recs, e)
        if rows:
            tax = a.tax_bps / 10000.0
            net = [x["ret"] * (1 - tax) for x in rows]
            w = sum(x - 1 for x in net if x > 1)
            l = -sum(x - 1 for x in net if x < 1)
            print(f"    net of {a.tax_bps:.0f}bps tax: mean {st.mean(net):.3f}x   "
                  f"pf {w/l if l else float('inf'):.2f}")


if __name__ == "__main__":
    main()
