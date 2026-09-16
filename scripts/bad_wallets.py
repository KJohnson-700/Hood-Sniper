#!/usr/bin/env python3
"""
Wallets whose presence in the first buys predicts a token goes NOWHERE.

This is the inverse of smart_money.py, and it is the stronger half.

WHY BOTH DIRECTIONS ARE NOT THE SAME
    Validated walk-forward on 23,557 tokens the scoring never saw: wallets were
    scored only on tokens that started before block 60,686,915 and tested only on
    tokens that started after, so no token contributes to both sides.

    share of a token's first 20 buyers that are KNOWN-BAD:
        <15%    n=13,574   median peak 2.14x   >=2x 54.0%   >=5x 18.5%
        15-35%  n= 4,138   median peak 1.80x   >=2x 41.8%   >=5x 12.3%
        35-60%  n= 2,723   median peak 1.55x   >=2x 17.8%   >=5x  2.4%
        >60%    n= 3,122   median peak 1.26x   >=2x  5.7%   >=5x  1.0%

    Monotonic across every bucket -- a 9.5x spread on the 2x rate and 18.5x on 5x.

    The KNOWN-GOOD direction also predicts (36.6% -> 76.8%) but is NOT monotonic:
    it falls back to 55.1% in the top bucket. The negative signal is the clean one,
    which is why this index exists alongside a smart-money list we already had.

IT IS NOT AN ACTIVITY PROXY
    Checked, because that confound has bitten this codebase before. Within each
    activity band the split still holds:
        10-49 events     bad<35%: 18.9%   bad>=35%:  7.3%
        50-199           bad<35%: 78.4%   bad>=35%: 60.1%
        200-999          bad<35%: 97.9%   bad>=35%: 93.4%
    Discrimination is sharpest on quiet early tokens, which is where a filter is
    worth having.

WHAT "BAD" MEANS HERE
    Bottom-quartile median forward multiple across a wallet's picks -- wallets whose
    buys do not run. NOT "known ruggers": no rug labelling is involved, and none is
    needed. The forward multiple is computed strictly AFTER each buy (suffix max),
    so a buy is never credited or debited with a price that preceded it.

CAVEAT ON MAGNITUDE
    "Peak from first trade" is inflated on a bonding curve -- measured ~9x from first
    trade to graduation -- so a 2.14x median peak is not 2.14x of tradeable profit.
    Use the DISCRIMINATION between buckets, not the absolute level.
"""
import json
import os
import statistics as st
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS = os.path.join(HERE, "data", "holder_events.jsonl")
OUT = os.path.join(HERE, "data", "bad_wallets.json")

MIN_PICKS = 3
BAD_QUANTILE = 0.25          # bottom quartile of median forward multiple


def load_tokens(log=print):
    by = defaultdict(list)
    n = 0
    with open(EVENTS) as f:
        for line in f:
            try:
                blk, tok, w, px, buy = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if px <= 0:
                continue
            by[tok].append((blk, w, px, buy))
            n += 1
    log(f"  events {n:,} across {len(by):,} tokens")
    return by


def score_wallets(by, tokens=None, log=print):
    """wallet -> median forward multiple of its buys. Forward-only by construction."""
    picks = defaultdict(list)
    for tok in (tokens if tokens is not None else by):
        evs = by[tok]
        if len(evs) < 10:                 # untraded dust has no forward path
            continue
        evs.sort(key=lambda x: x[0])
        ne = len(evs)
        suf = [0.0] * (ne + 1)
        for i in range(ne - 1, -1, -1):
            suf[i] = max(suf[i + 1], evs[i][2])
        for i, (blk, w, px, buy) in enumerate(evs):
            if not buy:
                continue
            fwd = suf[i + 1]              # strictly after this buy
            if fwd > 0:
                picks[w].append(fwd / px)
    out = {w: st.median(v) for w, v in picks.items() if len(v) >= MIN_PICKS}
    log(f"  scored {len(out):,} wallets with >={MIN_PICKS} picks")
    return out


def build(log=print):
    by = load_tokens(log)
    score = score_wallets(by, log=log)
    vals = sorted(score.values())
    cut = vals[int(BAD_QUANTILE * len(vals))]
    bad = {w: round(v, 4) for w, v in score.items() if v <= cut}
    log(f"  bad cutoff (median_fwd <= {cut:.4f}): {len(bad):,} of {len(score):,}")
    # STORE EVERY SCORED WALLET, not only the bad ones. The validated statistic is
    # bad / SCORED, and a consumer holding only the bad set would divide by all
    # early buyers instead -- a different, systematically smaller ratio that does
    # not correspond to any measured bucket.
    json.dump({"cutoff": cut, "n_scored": len(score), "n_bad": len(bad),
               "scored": {w: round(v, 4) for w, v in score.items()}},
              open(OUT, "w"))
    log(f"  wrote {OUT}")
    return bad


def validate(log=print):
    """Re-run the walk-forward test that justified this index. Prints, changes nothing."""
    by = load_tokens(log)
    firsts = {t: min(e[0] for e in evs) for t, evs in by.items()}
    cut_blk = sorted(firsts.values())[int(.60 * len(firsts))]
    train = [t for t, b in firsts.items() if b < cut_blk]
    test = [t for t, b in firsts.items() if b >= cut_blk]
    log(f"  split at block {cut_blk:,}: train {len(train):,}  test {len(test):,}")
    score = score_wallets(by, train, log=log)
    vals = sorted(score.values())
    bad = {w for w, v in score.items() if v <= vals[len(vals) // 4]}
    rows = []
    for tok in test:
        evs = by[tok]
        if len(evs) < 10:
            continue
        evs.sort(key=lambda x: x[0])
        ne = len(evs)
        suf = [0.0] * (ne + 1)
        for i in range(ne - 1, -1, -1):
            suf[i] = max(suf[i + 1], evs[i][2])
        first = [w for blk, w, px, buy in evs[:20] if buy]
        known = [w for w in first if w in score]
        if len(first) < 5 or len(known) < 3 or evs[0][2] <= 0:
            continue
        rows.append((sum(1 for w in known if w in bad) / len(known),
                     suf[0] / evs[0][2]))
    log(f"\n  test tokens: {len(rows):,}")
    for lo, hi, lab in ((0, .15, "<15%"), (.15, .35, "15-35%"),
                        (.35, .60, "35-60%"), (.60, 1.01, ">60%")):
        b = [r for r in rows if lo <= r[0] < hi]
        if len(b) < 40:
            continue
        pk = [r[1] for r in b]
        log(f"   bad-share {lab:>7} n={len(b):5d}  median peak {st.median(pk):5.2f}x  "
            f">=2x {100*sum(1 for x in pk if x >= 2)/len(b):5.1f}%")


def load(path=OUT):
    """
    (scored, cutoff) for the monitor -- every scored wallet plus the bad threshold,
    so a caller can reproduce the validated bad/scored ratio.

    Returns ({}, 0.0) when absent. An empty index means NO INFORMATION and callers
    must not read it as "no bad wallets here".
    """
    if not os.path.exists(path):
        return {}, 0.0
    try:
        d = json.load(open(path))
        return d.get("scored", {}), d.get("cutoff", 0.0)
    except Exception:  # noqa: BLE001
        return {}, 0.0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--validate", action="store_true")
    a = ap.parse_args()
    if a.validate:
        validate()
    elif a.build:
        build()
    else:
        ap.print_help()
