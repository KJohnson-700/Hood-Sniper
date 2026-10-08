#!/usr/bin/env python3
"""
Which coins ran, who was early in them, and what the winners had in common.

THE THREE QUESTIONS THIS ANSWERS, in the order they have to be answered:

  1. WHICH COINS RAN?  Ground truth, from our own gmgn_feed snapshot history --
     not from a vendor's "trending" list, which is a popularity read rather than
     an outcome. Everything downstream is worthless if this label is wrong.

  2. WHO WAS EARLY IN THEM?  `gmgn token traders` returns up to 100 traders per
     token with wallet tags (smart_degen / renowned / sniper / dev / bundler /
     rat_trader / fresh_wallet). That replaces chain pagination, which cost 45
     paginated RPC calls per token when done by hand -- 44,933 signatures for one
     pump.fun token.

  3. WHAT DID THE WINNERS SHARE?  Feature frequencies in runners vs controls.

THE TRAP THIS FILE IS BUILT AROUND
    Scoring wallets on runners ALONE makes every wallet look brilliant: if you
    only examine coins that went up, everyone who bought them "called it". The
    denominator is the whole result. So `label()` emits a CONTROL set matched on
    launch window and starting market cap, and a wallet's score is
    (times early in a runner) / (times early in anything). This project has
    already produced two false edges by selecting on outcome -- a 4.0x tape study
    seeded from graduations only, and a "winner-only journal" that passed 2 of 31
    arms. Not again.

MCAP SANITY. The raw feed contains impossible trajectories -- the worst was
$12 -> $12,217,920,000, a 1,016,465,890x -- so values outside a plausible band are
dropped rather than ranked. An unfiltered max() finds the corruption, not the winner.

COVERAGE LIMIT, stated because it bounds every conclusion here: GMGN snapshots the
top ~80 per chain per stage, so of 481,468 distinct Solana tokens seen in 9.9 days
only ~5,247 have the >=3 snapshots needed to measure a trajectory. Tokens that
appeared once cannot be labelled either way and are excluded from BOTH sets, which
means the control group is "tokens GMGN surfaced repeatedly that did not run",
never "all tokens". Rates here are relative to that universe.
"""
import json
import os
import sys
import time
import argparse
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import investigate as I  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
GMGN_FEED = os.path.join(DATA, "gmgn_feed.jsonl")
LABELS = os.path.join(DATA, "meta_labels.json")
ATTRIB = os.path.join(DATA, "meta_attrib.jsonl")

# --- sanity band for a bonding-curve launch -------------------------------------
# A pump.fun launch starts near $4-6k and a $100M cap is already an outlier; a
# $12B one is a decode error. Anything outside the band is dropped, not clamped --
# clamping would keep a corrupt row at the boundary and it would rank as a winner.
MCAP_FLOOR = 1_000.0
MCAP_CEIL = 500_000_000.0

RUN_MULT = 3.0            # peak / first-seen
RUN_PEAK = 150_000.0      # and an absolute floor, so 3x off $2k does not qualify
MIN_SNAPS = 3
MIN_SPAN = 1800.0         # 30 min, so a single burst is not a "trajectory"


def _traj(chain="sol", log=print):
    """token -> sorted [(ts, mcap)] from our own snapshot history."""
    t = collections.defaultdict(list)
    meta = {}
    bad = 0
    n = 0
    with open(GMGN_FEED) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("chain") != chain:
                continue
            a, ts, m = r.get("address"), r.get("ts"), r.get("mcap")
            if not a or not ts:
                continue
            n += 1
            if m is None:
                continue
            try:
                m = float(m)
            except Exception:  # noqa: BLE001
                continue
            if not (MCAP_FLOOR <= m <= MCAP_CEIL):
                bad += 1
                continue
            t[a].append((float(ts), m))
            meta.setdefault(a, {})
            for k in ("symbol", "name", "launchpad", "creator", "twitter"):
                if r.get(k) and not meta[a].get(k):
                    meta[a][k] = r[k]
    for v in t.values():
        v.sort()
    log(f"  {chain}: {n:,} rows, {len(t):,} tokens, {bad:,} mcaps outside "
        f"${MCAP_FLOOR:,.0f}-${MCAP_CEIL:,.0f} dropped")
    return t, meta


def label(chain="sol", log=print):
    traj, meta = _traj(chain, log=log)
    runners, controls = [], []
    for a, v in traj.items():
        if len(v) < MIN_SNAPS or (v[-1][0] - v[0][0]) < MIN_SPAN:
            continue
        first = v[0][1]
        peak = max(x[1] for x in v)
        rec = {"address": a, "first_mcap": first, "peak_mcap": peak,
               "mult": peak / first, "snaps": len(v),
               "t0": v[0][0], "t_last": v[-1][0], **meta.get(a, {})}
        (runners if (rec["mult"] >= RUN_MULT and peak >= RUN_PEAK)
         else controls).append(rec)
    # MATCH THE CONTROLS ON STARTING SIZE -- NEAREST NEIGHBOUR, not buckets.
    #
    # The runner label is peak/first_mcap, so a token that happened to be first
    # SEEN at a lower market cap mechanically earns a higher multiple. Without
    # tight matching, "runners start smaller" comes out of the definition rather
    # than out of the market, and every feature correlated with size (liquidity,
    # holders, progress, followers) inherits that tautology and reads as
    # predictive. A coarse bucket (a third of a decade) was not enough: runners
    # still came in at a median $6,887 against $9,265 for controls.
    #
    # So each runner draws its controls from the non-runners CLOSEST to it in
    # log(first_mcap) on the same day, and each control is used at most once.
    import math

    def key(r):
        return (math.log10(max(r["first_mcap"], 1.0)), r["t0"] // 86400)

    used = set()
    mc = []
    by_day = collections.defaultdict(list)
    for c in controls:
        by_day[c["t0"] // 86400].append(c)
    for r in runners:
        lr, day = key(r)
        pool = [c for c in by_day.get(day, []) if c["address"] not in used]
        pool.sort(key=lambda c: abs(math.log10(max(c["first_mcap"], 1.0)) - lr))
        for c in pool[:4]:             # 4 nearest controls per runner
            used.add(c["address"])
            mc.append(c)
    out = {"chain": chain, "built": time.time(),
           "runners": sorted(runners, key=lambda r: -r["mult"]),
           "controls": mc, "n_unmatched_controls": len(controls)}
    json.dump(out, open(LABELS, "w"))
    log(f"  RUNNERS {len(runners)}   matched CONTROLS {len(mc)} "
        f"(from {len(controls):,} eligible non-runners)")
    return out


# --- quota circuit breaker ------------------------------------------------------
# WHY THIS EXISTS, written the day it was needed. A first attribution run paced at
# 0.25s made ~90 calls, hit HTTP 429, and KEPT GOING -- and the 429 was not
# per-endpoint throttling but a key-wide block: market/trenches, user/kol,
# token/info, token_top_holders, token_top_traders and user/wallet_stats all
# started refusing at once, and they stayed refused for more than five minutes.
#
# The real cost was not the failed research. The LIVE discovery feed shares this
# key, and gmgn_feed went from 6.5 to 9.5 minutes stale while the retries ground
# on. A background study must never be able to starve the running bot.
#
# So: a hard call budget, slow pacing, and an abort after 3 consecutive 429s
# instead of grinding through hundreds. Progress is already resumable -- every
# token written to ATTRIB is skipped on the next run -- so aborting early costs
# nothing but the current pass.
QUOTA_PACE = 6.0        # seconds between calls
QUOTA_TRIP = 3          # consecutive 429s that abort the run
_q = {"streak": 0, "calls": 0}


def _traders(addr, chain="sol", limit=100, log=print):
    """Top traders for one token, or None on error. A FAILED pull is never
    recorded as an empty trader list -- that would read as "nobody traded it",
    which is the silent-hole bug this project keeps re-living."""
    # RATE LIMITS ARE PER-ENDPOINT, not per-key. market/token_top_traders 429s
    # far below the documented leaky bucket (rate=20/capacity=20), and the gmgn
    # discovery supervisor is drawing on the same key at the same time. So back
    # off on 429 and treat exhaustion as NO ANSWER, never as "no traders" --
    # recording an empty list here would mean "nobody traded this runner", which
    # is the silent-hole bug in its purest form.
    for attempt in range(3):
        d, err = I._gmgn(["token", "traders", "--chain", chain, "--address", addr,
                          "--limit", str(limit), "--order-by", "profit", "--raw"],
                         timeout=90)
        if not err:
            _q["streak"] = 0
            break
        if "429" in str(err):
            time.sleep(4.0 * (attempt + 1))
            continue
        log(f"    ERROR {addr[:12]}… {str(err)[:60]}")
        return None
    if err:
        _q["streak"] += 1
        log(f"    429 x{_q['streak']} on {addr[:12]}…")
        return None
    rows = d.get("list") if isinstance(d, dict) else d
    return rows if isinstance(rows, list) else None


def attribute(chain="sol", max_calls=60, log=print):
    """
    Pull the trader list for every runner AND every matched control.

    Controls are not optional. A wallet that appears in 8 runners looks like a
    genius until you see it also appears in 400 tokens that went nowhere, at
    which point it is a bot that buys everything. Only the ratio means anything.
    """
    lab = json.load(open(LABELS))
    jobs = ([(r, 1) for r in lab["runners"]] + [(c, 0) for c in lab["controls"]])
    log(f"  pulling traders for {len(jobs)} tokens "
        f"({len(lab['runners'])} runners + {len(lab['controls'])} controls)")
    done = set()
    if os.path.exists(ATTRIB):
        with open(ATTRIB) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["token"])
                except Exception:  # noqa: BLE001
                    pass
    n_ok = n_err = 0
    with open(ATTRIB, "a") as out:
        for i, (tok, is_run) in enumerate(jobs, 1):
            a = tok["address"]
            if a in done:
                continue
            if _q["calls"] >= max_calls:
                log(f"  call budget {max_calls} reached — stopping cleanly, "
                    f"re-run to continue (progress is saved)")
                break
            if _q["streak"] >= QUOTA_TRIP:
                log(f"  ABORTING: {QUOTA_TRIP} consecutive 429s. The key is "
                    f"blocked key-wide and the live discovery feed shares it. "
                    f"Re-run later; {len(done) + n_ok} tokens are already saved.")
                break
            _q["calls"] += 1
            rows = _traders(a, chain=chain, log=log)
            if rows is None:
                n_err += 1
                continue
            n_ok += 1
            out.write(json.dumps({
                "token": a, "symbol": tok.get("symbol"),
                "launchpad": tok.get("launchpad"), "is_runner": is_run,
                "mult": tok.get("mult"), "peak_mcap": tok.get("peak_mcap"),
                "first_mcap": tok.get("first_mcap"), "t0": tok.get("t0"),
                "traders": [{"w": r.get("address"),
                             "profit": r.get("profit"),
                             "buy_usd": r.get("buy_volume_cur"),
                             "sell_usd": r.get("sell_volume_cur"),
                             "netflow": r.get("netflow_usd"),
                             "buys": r.get("buy_tx_count_cur"),
                             "tag": r.get("wallet_tag_v2")} for r in rows],
            }) + "\n")
            out.flush()
            if i % 20 == 0:
                log(f"    {i}/{len(jobs)}  ok={n_ok} err={n_err}")
            time.sleep(QUOTA_PACE)
    log(f"  done: {n_ok} pulled, {n_err} errors, {len(done)} already had")
    return n_ok


def score(min_appear=2, log=print):
    """
    Wallet hit rate WITH a denominator, plus the base rate it has to beat.
    """
    runs = {}
    wal = collections.defaultdict(lambda: {"hit": 0, "app": 0, "profit": 0.0,
                                           "tokens": [], "tags": set()})
    with open(ATTRIB) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            runs[r["token"]] = r["is_runner"]
            for t in r["traders"]:
                w = t.get("w")
                if not w:
                    continue
                d = wal[w]
                d["app"] += 1
                if r["is_runner"] and (t.get("profit") or 0) > 0:
                    d["hit"] += 1
                    d["tokens"].append((r.get("symbol"), r.get("mult"),
                                        t.get("profit")))
                d["profit"] += (t.get("profit") or 0)
                if t.get("tag"):
                    d["tags"].add(t["tag"])
    n_run = sum(1 for v in runs.values() if v)
    base = n_run / max(len(runs), 1)
    log(f"  universe: {len(runs)} tokens, {n_run} runners -> BASE RATE "
        f"{base*100:.1f}% (a wallet that buys blindly scores this)")
    rows = [(w, d) for w, d in wal.items() if d["app"] >= min_appear]
    rows.sort(key=lambda kv: (-kv[1]["hit"], -kv[1]["profit"]))
    log(f"  wallets seen in >={min_appear} labelled tokens: {len(rows)}\n")
    log(f"  {'wallet':>46} {'hit':>4} {'app':>4} {'prec':>6} {'lift':>6} "
        f"{'profit$':>12}")
    for w, d in rows[:30]:
        prec = d["hit"] / d["app"]
        log(f"  {w:>46} {d['hit']:>4} {d['app']:>4} {prec*100:>5.0f}% "
            f"{prec/base if base else 0:>5.1f}x {d['profit']:>12,.0f}")
    return rows, base


# --- what the winners had in common ---------------------------------------------
# NUMERIC features compared at the median; BOOLEAN ones as a rate.
NUM_FEATS = ["liq", "progress", "swaps_24h", "buys_24h", "sells_24h",
             "net_buy_24h", "volume_24h", "smart", "renowned", "bots",
             "bundler_rate", "top10_rate", "holders", "creator_launches",
             "x_followers", "first_mcap"]
BOOL_FEATS = ["twitter", "renounced"]


def _first_snapshot(chain="sol", wanted=None, log=print):
    """
    The EARLIEST snapshot per token, i.e. what was observable when the coin was
    first discovered.

    NOT the latest. Using the most recent snapshot would let the outcome leak
    into the features -- a runner's final row shows the swaps and holders it
    earned BY running, so every feature would look predictive and none would be.
    This project has already shipped one look-ahead of exactly this shape
    (funding stamped an hour early in the venue panels).
    """
    best = {}
    with open(GMGN_FEED) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("chain") != chain:
                continue
            a, ts = r.get("address"), r.get("ts")
            if not a or not ts or (wanted is not None and a not in wanted):
                continue
            if a not in best or ts < best[a]["ts"]:
                best[a] = r
    log(f"  first-snapshot rows for {len(best):,} labelled tokens")
    return best


def features(chain="sol", log=print):
    lab = json.load(open(LABELS))
    runs = {r["address"]: r for r in lab["runners"]}
    ctrl = {c["address"]: c for c in lab["controls"]}
    first = _first_snapshot(chain, wanted=set(runs) | set(ctrl), log=log)

    def med(vals):
        v = sorted(x for x in vals if x is not None)
        return v[len(v) // 2] if v else None

    def grab(ids, key):
        out = []
        for a in ids:
            r = first.get(a)
            if not r:
                continue
            v = r.get(key)
            if key == "first_mcap":
                v = (runs.get(a) or ctrl.get(a) or {}).get("first_mcap")
            if isinstance(v, bool):
                v = 1.0 if v else 0.0
            try:
                out.append(float(v))
            except Exception:  # noqa: BLE001
                continue
        return out

    log(f"\n  RUNNERS {len(runs)} vs CONTROLS {len(ctrl)} "
        f"— medians at FIRST sighting, not final state\n")
    log(f"  {'feature':>18} {'runners':>12} {'controls':>12} {'ratio':>8} {'n_r':>5} {'n_c':>5}")
    rows = []
    for k in NUM_FEATS:
        rv, cv = grab(runs, k), grab(ctrl, k)
        mr, mc = med(rv), med(cv)
        if mr is None or mc is None:
            continue
        ratio = (mr / mc) if mc else float("inf")
        rows.append((abs((ratio or 1) - 1), k, mr, mc, ratio, len(rv), len(cv)))
    rows.sort(reverse=True)
    for _, k, mr, mc, ratio, nr, nc in rows:
        log(f"  {k:>18} {mr:>12,.3f} {mc:>12,.3f} {ratio:>7.2f}x {nr:>5} {nc:>5}")

    log("")
    for k in BOOL_FEATS:
        pr = [1 if (first.get(a) or {}).get(k) else 0 for a in runs if a in first]
        pc = [1 if (first.get(a) or {}).get(k) else 0 for a in ctrl if a in first]
        if not pr or not pc:
            continue
        a_, b_ = sum(pr) / len(pr), sum(pc) / len(pc)
        log(f"  has {k:>14}: runners {a_*100:5.1f}%  controls {b_*100:5.1f}%  "
            f"{(a_/b_ if b_ else float('inf')):.2f}x")

    # launchpad mix -- a meta is often a VENUE before it is a theme
    log("")
    for lbl, ids in (("runners", runs), ("controls", ctrl)):
        c = collections.Counter(str((first.get(a) or {}).get("launchpad"))
                                for a in ids if a in first)
        tot = sum(c.values()) or 1
        log(f"  {lbl:>9} launchpads: " + ", ".join(
            f"{k}={v}({v/tot*100:.0f}%)" for k, v in c.most_common(5)))
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", action="store_true")
    ap.add_argument("--attribute", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--features", action="store_true")
    ap.add_argument("--max-calls", type=int, default=60,
                    help="hard call budget per run; the live discovery feed "
                         "shares this API key")
    ap.add_argument("--min-appear", type=int, default=2)
    ap.add_argument("--chain", default="sol")
    ap.add_argument("--show", type=int, default=25)
    a = ap.parse_args()
    if a.label:
        out = label(chain=a.chain)
        print(f"\n  {'symbol':>14} {'first':>10} {'peak':>13} {'mult':>8} "
              f"{'snaps':>6}  {'pad':>8}")
        for r in out["runners"][:a.show]:
            print(f"  {str(r.get('symbol'))[:14]:>14} {r['first_mcap']:>10,.0f} "
                  f"{r['peak_mcap']:>13,.0f} {r['mult']:>8.1f}x {r['snaps']:>6} "
                  f"  {str(r.get('launchpad'))[:8]:>8}")
    elif a.attribute:
        attribute(chain=a.chain, max_calls=a.max_calls)
    elif a.score:
        score(min_appear=a.min_appear)
    elif a.features:
        features(chain=a.chain)
    else:
        ap.print_help()
