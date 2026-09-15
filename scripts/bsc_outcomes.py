#!/usr/bin/env python3
"""
BSC outcome tracker -- turns indexed launches into measured results.

Why this exists: the dev-reputation question ("does a dev with a prior success
repeat?") showed real signal on Pons (2.20x, Fisher p=0.007) but was untestable
there because 92% of Pons devs launch exactly once. On BSC 47.5% repeat, so the
test is possible -- but only with OUTCOME data, which nothing was collecting.

Method
------
Snapshot every registry token through DexScreener (30 per call) and append the
result. Running this repeatedly builds a time series, so peak liquidity/volume
is captured rather than whatever happened to be true at one moment. A token that
never gets a funded pair is not "missing data" -- absence IS the failure signal,
the same logic that made the Robinhood Chain denominator honest.

    python3 bsc_outcomes.py --snapshot     # record current state of every launch
    python3 bsc_outcomes.py --report       # base rates + dev-conditional rates
"""
import argparse
import json
import os
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
REGISTRY = os.path.join(DATA, "bsc_dev_registry.json")
SNAPS = os.path.join(DATA, "bsc_outcomes.jsonl")
FM_TRADES = os.path.join(DATA, "bsc_fm_trades.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Tiers are measured on VOLUME, not liquidity.
#
# Liquidity is the wrong yardstick here: flap.sh seeds every graduating pool
# with ~$9.2k, so a "peak liq >= $10k" test mostly counts pools the venue
# funded automatically -- dozens sat at ~$9,2xx with $1-200 of volume.
# Volume is what cannot be faked by a seeding routine.
#
# four.meme is also invisible on liquidity: pre-migration its pairs report
# liq $0 on DexScreener, so its successes only show up as volume or as a
# migration off the launch venue.
TIER_TRADED = 1_000        # any real trading at all
TIER_REAL = 25_000         # sustained interest
TIER_BIG = 100_000
SEED_LIQ_HINT = 9_000      # flap.sh auto-seed, do not mistake for traction


# four.meme pre-migration pairs report liq/vol $0 on DexScreener, so its
# outcomes are invisible there -- 507 of 551 launches came back with no pair at
# all. Its TokenManager trade events carry the token in data word 0, so activity
# is measurable directly on chain. Trade COUNT is used rather than the amount
# fields, which are ambiguous without an ABI.
FOURMEME_TM = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
T_FM_BUY = "0x7db52723a3b2cdd6164364b3b766e65e540d7be48ffa89582956d8eaebe62942"
T_FM_SELL = "0x0a5575b3648bae2210cee56bf33254cc1ddfbc7bf637c0af2ac18b14fb1bae19"


# Per-run block budget. A four.meme trade scan costs ~1 RPC call per 120 blocks
# per topic, so an unbounded catch-up after the laptop sleeps for a day would be
# thousands of calls in one run. Capping it lets successive hourly runs walk the
# backlog forward instead of one run stalling for hours.
FM_MAX_SPAN = 40_000


def fourmeme_scan_incremental(log=print):
    """
    Accumulate four.meme trade counts forward from a persisted cursor.

    The first version of this rescanned a FIXED window [registry_lo, registry_hi+20k]
    every run. That was wrong twice over: it paid full price for the same blocks each
    time, and it gave tokens launched near registry_hi almost no forward window in
    which to trade -- which is exactly why 545/551 read as zero. Activity has to be
    measured AFTER the launch, so the window must keep growing.
    """
    import bsc_monitor as B
    st = {"cursor": 0, "counts": {}}
    if os.path.exists(FM_TRADES):
        with open(FM_TRADES) as f:
            st = json.load(f)
    if not st.get("cursor"):
        with open(REGISTRY) as f:
            rng = json.load(f).get("scanned")
        if not rng:
            return {}
        st["cursor"] = rng[0]

    head = B.head_block()
    lo = st["cursor"] + 1
    hi = min(head, lo + FM_MAX_SPAN)
    if hi < lo:
        log(f"  four.meme: cursor already at head ({head})")
        return {k.lower(): v for k, v in st["counts"].items()}

    counts = defaultdict(int, {k.lower(): v for k, v in st["counts"].items()})
    for topic in (T_FM_BUY, T_FM_SELL):
        for l in B.get_logs(FOURMEME_TM, topic, lo, hi):
            d = l["data"][2:]
            if len(d) >= 64:
                counts["0x" + d[0:64][-40:]] += 1

    st = {"cursor": hi, "counts": dict(counts)}
    tmp = FM_TRADES + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, FM_TRADES)          # atomic: an interrupted run cannot corrupt the cursor
    behind = head - hi
    log(f"  four.meme: scanned {lo}-{hi} ({hi-lo+1} blocks), "
        f"{len(counts)} tokens with trades, {behind} blocks behind head")
    return dict(counts)


def ds_batch(addrs, tries=3):
    url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(addrs)
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r).get("pairs") or []
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (a + 1))
    return []


def load_registry():
    if not os.path.exists(REGISTRY):
        return {}, {}
    with open(REGISTRY) as f:
        reg = json.load(f)
    devs = reg.get("devs", {})
    tok2dev = {}
    for d, v in devs.items():
        for t in v.get("tokens", []):
            tok2dev[t.lower()] = d
    return devs, tok2dev


def snapshot(log=print):
    devs, tok2dev = load_registry()
    toks = list(dict.fromkeys(tok2dev.keys()))
    if not toks:
        log("  registry empty — run build_bsc_dev_registry.py first")
        return 0
    log(f"  resolving {len(toks)} launches through DexScreener…")
    ts = datetime.now(timezone.utc).isoformat()
    best = {}
    for i in range(0, len(toks), 30):
        chunk = toks[i:i + 30]
        for p in ds_batch(chunk):
            bt = (p.get("baseToken") or {}).get("address", "").lower()
            if not bt:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            cur = best.get(bt)
            if cur and cur["liq"] >= liq:
                continue
            best[bt] = {"liq": liq,
                        "vol24": float((p.get("volume") or {}).get("h24") or 0),
                        "mcap": float(p.get("marketCap") or 0),
                        "dex": p.get("dexId"),
                        "price": p.get("priceUsd")}
        time.sleep(0.35)
        if (i // 30) % 5 == 0:
            log(f"    {min(i+30, len(toks))}/{len(toks)}")
    # close the four.meme blind spot with on-chain trade counts
    fm = {}
    try:
        fm = fourmeme_scan_incremental(log=log)
    except Exception as e:  # noqa: BLE001
        log(f"  four.meme trade scan skipped: {e}")
    n = 0
    with open(SNAPS, "a") as f:
        for t in toks:
            b = best.get(t) or {"liq": 0.0, "vol24": 0.0, "mcap": 0.0,
                                "dex": None, "price": None}
            f.write(json.dumps({"ts": ts, "token": t, "dev": tok2dev.get(t),
                                "fm_trades": fm.get(t, 0), **b}) + "\n")
            n += 1
    log(f"  snapshot written: {n} tokens, {sum(1 for t in toks if best.get(t,{}).get('liq',0)>0)} with a funded pair")
    return n


def report(log=print):
    if not os.path.exists(SNAPS):
        log("  no snapshots yet — run --snapshot")
        return
    peak = {}
    dev_of = {}
    TRADE_ACTIVE = 5      # four.meme: this many curve trades = someone showed up
    with open(SNAPS) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = r["token"]
            dev_of[t] = r.get("dev")
            p = peak.setdefault(t, {"liq": 0.0, "vol": 0.0, "mcap": 0.0, "dex": None})
            if r.get("liq", 0) > p["liq"]:
                p["liq"] = r["liq"]
                p["dex"] = r.get("dex")
            p["vol"] = max(p["vol"], r.get("vol24") or 0)
            p["mcap"] = max(p["mcap"], r.get("mcap") or 0)
            p["trades"] = max(p.get("trades", 0), r.get("fm_trades") or 0)
    n = len(peak)
    if not n:
        log("  no rows")
        return
    # a launch counts as active if it drew DEX volume OR on-chain curve trades
    active = [t for t, p in peak.items()
              if p["vol"] >= TIER_TRADED or p.get("trades", 0) >= TRADE_ACTIVE]
    traded = [t for t, p in peak.items() if p["vol"] >= TIER_TRADED]
    real = [t for t, p in peak.items() if p["vol"] >= TIER_REAL]
    big = [t for t, p in peak.items() if p["vol"] >= TIER_BIG]
    seeded = [t for t, p in peak.items() if p["liq"] >= SEED_LIQ_HINT]
    log(f"\n  BSC OUTCOMES — {n} launches tracked")
    log(f"    got a funded pool (liq >= ${SEED_LIQ_HINT:,}): {len(seeded):>4} "
        f"({100*len(seeded)/n:5.2f}%)   <- mostly the venue's auto-seed")
    log(f"    ANY activity (DEX vol or >={TRADE_ACTIVE} curve trades): "
        f"{len(active):>4} ({100*len(active)/n:5.2f}%)")
    log(f"    peak vol24 >= ${TIER_TRADED:,}: {len(traded):>4} ({100*len(traded)/n:5.2f}%)")
    log(f"    peak vol24 >= ${TIER_REAL:,}: {len(real):>4} ({100*len(real)/n:5.2f}%)")
    log(f"    peak vol24 >= ${TIER_BIG:,}: {len(big):>4} ({100*len(big)/n:5.2f}%)")
    byv = defaultdict(lambda: [0, 0])
    for t, p in peak.items():
        d = (p.get("dex") or "none")
        byv[d][0] += 1
        if p["vol"] >= TIER_TRADED:
            byv[d][1] += 1
    log("    by venue (tokens / with real volume):")
    for d, (c, h) in sorted(byv.items(), key=lambda x: -x[1][0]):
        log(f"      {d:12} {c:>4} / {h}")
    migrated = [t for t, p in peak.items() if p["dex"] and p["dex"] not in ("fourmeme", "flapsh")]
    log(f"    migrated off the launch venue: {len(migrated)} ({100*len(migrated)/n:.2f}%)")

    # dev-conditional: does a dev with a prior success repeat?
    by_dev = defaultdict(list)
    for t, d in dev_of.items():
        if d:
            by_dev[d].append(t)
    succ = set(real)   # "success" = sustained VOLUME, not a seeded pool
    multi = {d: ts for d, ts in by_dev.items() if len(ts) > 1}
    log(f"\n  devs: {len(by_dev)} total, {len(multi)} with more than one launch")
    hit_devs = [d for d, ts in by_dev.items() if any(t in succ for t in ts)]
    log(f"    devs with >=1 success (peak vol24 >= ${TIER_REAL:,}): {len(hit_devs)}")
    if multi:
        a = [t for d, ts in multi.items() if d in hit_devs for t in ts]
        b = [t for d, ts in multi.items() if d not in hit_devs for t in ts]
        ra = 100 * sum(1 for t in a if t in succ) / len(a) if a else 0
        rb = 100 * sum(1 for t in b if t in succ) / len(b) if b else 0
        log(f"    launches by a dev that ever succeeded : {len(a):>4}  success rate {ra:5.2f}%")
        log(f"    launches by a dev that never did      : {len(b):>4}  success rate {rb:5.2f}%")
        log("    NOTE this is in-sample and circular (a dev is 'successful' partly "
            "because of these same launches).")
        log("    A real test needs the dev's PRIOR success only, which requires "
            "snapshots spanning more time than one run.")
    log(f"\n  snapshots on file: {sum(1 for _ in open(SNAPS))} rows")
    log("  run --snapshot again later to capture peaks and enable a point-in-time test")


def main():
    ap = argparse.ArgumentParser(description="BSC launch outcome tracker")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.snapshot:
        snapshot()
    if a.report or not a.snapshot:
        report()


if __name__ == "__main__":
    main()
