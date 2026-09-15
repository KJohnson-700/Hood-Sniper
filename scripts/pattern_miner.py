#!/usr/bin/env python3
"""
Pattern miner — test every logged feature against outcomes, not just the ones asked about.

WHY THIS EXISTS. The feed carries ~30 populated features across 114k rows, and until
now exactly four had ever been tested: dev launch count, mcap band, entry velocity
and creator tax. Each was analysed reactively because Slim asked a question. Nothing
was systematically checking the rest, so a real signal sitting in `n_snipers` or
`exempt_sold` would never have surfaced.

OUTCOME: did the curve go on to reach NEAR-GRADUATION (2.0 ETH raised, ~$20k mcap).
That is recorded forward in grad_forward.jsonl at the moment of crossing, so it
cannot be selected after the fact.

STATISTICS, and the honest caveats:
  * Every bucket gets a two-sided Fisher-style test against the base rate, plus a
    Wilson lower bound on the lift so a 3-of-4 bucket cannot look like an edge.
  * ~30 features x several buckets is a lot of comparisons. At p<0.05 you expect
    false positives BY CONSTRUCTION, so the report applies a Bonferroni-corrected
    threshold and says how many tests were run. A result that only clears the
    uncorrected bar is reported as SUGGESTIVE, never as a finding.
  * Correlation here is not a trade. Anything this surfaces is a candidate for a
    forward test, which is what killed the deployer filter and the dip strategy
    after they looked good in-sample.

    python3 pattern_miner.py              # full sweep
    python3 pattern_miner.py --min-n 200  # stricter
"""
import argparse
import json
import math
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
FEED = os.path.join(DATA, "monitor_feed.jsonl")
CROSS = os.path.join(DATA, "grad_forward.jsonl")
Z = 1.96


def wilson_lower(hits, n, z=Z):
    if n <= 0:
        return 0.0
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (c - m) / d)


def two_prop_z(h1, n1, h2, n2):
    """z for "this bucket differs from the rest". Cheap, and enough at this n."""
    if n1 < 5 or n2 < 5:
        return 0.0
    p1, p2 = h1 / n1, h2 / n2
    p = (h1 + h2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return 0.0 if se == 0 else (p1 - p2) / se


def load():
    rows = []
    for line in open(FEED):
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    # richest row per curve
    by = {}
    for r in rows:
        c = r.get("curve")
        if not c:
            continue
        prev = by.get(c) or {}
        by[c] = {**prev, **{k: v for k, v in r.items() if v is not None}}
    win = set()
    for line in open(CROSS):
        try:
            x = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if x.get("level") == "near_grad" and x.get("curve"):
            win.add(x["curve"])
    return by, win


def buckets_for(name, vals):
    """Sensible buckets per feature — quantiles for numbers, values for flags."""
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(nums) < 50:
        return None
    nums.sort()
    qs = [nums[int(len(nums) * f)] for f in (0.25, 0.5, 0.75, 0.9)]
    qs = sorted(set(qs))
    if len(qs) < 2:
        return None
    edges = [-math.inf] + qs + [math.inf]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def mine(min_n=100, log=print):
    by, win = load()
    tot = len(by)
    base_h = len([c for c in by if c in win])
    base = base_h / tot if tot else 0
    log(f"curves analysed: {tot:,}   reached near-graduation: {base_h:,} "
        f"({base:.2%} base rate)\n")

    FEATURES = ["tax_bps", "fee_bps", "total_fee_bps", "n_buyers", "n_sellers",
                "n_snipers", "n_exempt", "exempt_sold", "top1_share",
                "curve_volume_usd", "active_liq_usd", "dev_launches",
                "dev_prior_grads", "code_len", "tries", "curve_sellable"]
    results, ntests = [], 0
    for f in FEATURES:
        vals = [r.get(f) for r in by.values() if r.get(f) is not None]
        bks = buckets_for(f, vals)
        if not bks:
            continue
        for lo, hi in bks:
            sel = [c for c, r in by.items()
                   if isinstance(r.get(f), (int, float))
                   and not isinstance(r.get(f), bool) and lo <= r[f] < hi]
            if len(sel) < min_n:
                continue
            h = len([c for c in sel if c in win])
            rest_n = tot - len(sel)
            rest_h = base_h - h
            z = two_prop_z(h, len(sel), rest_h, rest_n)
            ntests += 1
            results.append({"feat": f, "lo": lo, "hi": hi, "n": len(sel),
                            "hits": h, "rate": h / len(sel), "z": z,
                            "floor": wilson_lower(h, len(sel)),
                            "lift": (h / len(sel)) / base if base else 0})
    # Bonferroni: 30+ tests at 0.05 yields false positives by construction
    zc = 1.96
    zb = abs(_z_for_bonferroni(ntests))
    strong = [r for r in results if abs(r["z"]) >= zb]
    sugg = [r for r in results if zc <= abs(r["z"]) < zb]
    strong.sort(key=lambda r: -abs(r["z"]))
    sugg.sort(key=lambda r: -abs(r["z"]))

    def show(rs, title):
        if not rs:
            log(f"  {title}: none\n")
            return
        log(f"  {title}")
        log(f"    {'feature':<20}{'range':<22}{'n':>7}{'rate':>8}{'lift':>7}{'z':>8}")
        for r in rs[:12]:
            lo = "-inf" if r["lo"] == -math.inf else f"{r['lo']:g}"
            hi = "inf" if r["hi"] == math.inf else f"{r['hi']:g}"
            log(f"    {r['feat']:<20}{f'{lo} .. {hi}':<22}{r['n']:>7,}"
                f"{r['rate']:>7.1%}{r['lift']:>6.1f}x{r['z']:>8.1f}")
        log("")

    log(f"tests run: {ntests}   Bonferroni z threshold: {zb:.2f} "
        f"(uncorrected would be 1.96)\n")
    show(strong, "SURVIVES correction — worth a forward test")
    show(sugg, "SUGGESTIVE only — expected by chance at this many tests")
    log("  Nothing here is a trade. These are in-sample correlations on one window;")
    log("  the deployer filter and the dip strategy both looked like this before")
    log("  failing out of sample. Treat each as a hypothesis to test forward.")
    return strong


def _z_for_bonferroni(ntests):
    """z for alpha=0.05/ntests, two-sided. Rational approximation, no scipy."""
    if ntests <= 1:
        return 1.96
    p = 0.05 / ntests / 2
    # Acklam-style inverse normal, adequate for a reporting threshold
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=100)
    a = ap.parse_args()
    mine(a.min_n)
