#!/usr/bin/env python3
"""
Hood Sniper -- Phase 0 backtest: does the deployer-reputation filter predict
bonding-tier outcomes?

Question under test
-------------------
    Does `win_score >= 2.0` over `>= 3` prior launches predict that a NEW
    launch from that deployer reaches >= $3M market cap?

Design notes (why it is built this way)
---------------------------------------
The naive approach -- "list pools from GeckoTerminal, score their deployers" --
is fatally survivorship-biased. GeckoTerminal only indexes pools that attracted
real trading; tokens that rugged in an hour never enter the index at all. They
are missing from the DENOMINATOR, not merely missing a data field, so a
GT-derived universe would show an absurdly low rug rate and inflate every
precision number.

So the launch universe is enumerated FROM THE CHAIN (`PairCreated` /
`PoolCreated` logs over a contiguous block window). That yields every launch,
including the ones that died. GeckoTerminal is then used only to resolve
OUTCOMES for those chain-derived pools. A pool absent from GT is not "missing
data" -- absence is itself the signal that it never traded meaningfully.

A contiguous (not sampled) block window is used because deployer HISTORY must
be complete: sampling disjoint windows would undercount each deployer's prior
launches and corrupt the score.

Stages are cached to data/ so the script is resumable and each stage can be
re-run independently.

Usage:
    python3 backtest_bonding_filter.py --stage all
    python3 backtest_bonding_filter.py --stage enumerate --start 22000000 --end 24000000
"""

import argparse
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
os.makedirs(DATA, exist_ok=True)

RPCS = [
    "https://rpc.mainnet.chain.robinhood.com",
    "https://robinhood-rpc.publicnode.com",
]
GT = "https://api.geckoterminal.com/api/v2"
GT_NET = "robinhood"
BLOCKSCOUT = "https://robinhoodchain.blockscout.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Robinhood Chain: ~0.101 s/block  ->  ~855,800 blocks/day (measured 2026-09-01)
BLOCKS_PER_DAY = 855_800
CHAIN_ID = 4663

# Uniswap-style factory events
TOPIC_PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
TOPIC_POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# Bags launchpad (bonding curve) -- see handoff step 7
BAGS_FACTORY = "0xe8cc4431adf8b5a847c113ef0c6af9043219cb37"

# Tier thresholds (USD market cap) -- from thesis.md
MCAP_1M = 1_000_000
MCAP_MID = 3_000_000      # bonded-mid floor
MCAP_HIGH = 50_000_000    # bonded-high floor
RUG_MCAP_CEIL = 50_000    # never got above this => rugged/dead
SURVIVE_DAYS = 7

# Scoring
MIN_LAUNCHES = 3
SCORE_THRESHOLD = 2.0
SWEEP = [1.0, 1.5, 2.0, 2.5, 3.0]

_rr = itertools.cycle(RPCS)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def rpc(method, params, tries=5, timeout=45):
    """Single JSON-RPC call with endpoint rotation + backoff."""
    last = None
    for attempt in range(tries):
        url = next(_rr)
        try:
            body = json.dumps({"jsonrpc": "2.0", "method": method,
                               "params": params, "id": 1}).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.load(r)
            if "error" in j:
                last = j["error"]
                msg = str(last.get("message", ""))
                if last.get("code") == 429 or "Too Many" in msg:
                    time.sleep(2 + 2 * attempt)
                    continue
            return j
        except urllib.error.HTTPError as e:
            last = f"HTTP{e.code}"
            time.sleep(1.5 + 1.5 * attempt)
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(1.5 + 1.5 * attempt)
    return {"error": {"message": f"failed:{last}"}}


def rpc_batch(calls, tries=4, timeout=60):
    """Batched JSON-RPC. `calls` = [(method, params), ...]. Returns list aligned to input."""
    payload = [{"jsonrpc": "2.0", "method": m, "params": p, "id": i}
               for i, (m, p) in enumerate(calls)]
    for attempt in range(tries):
        url = next(_rr)
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.load(r)
            if isinstance(out, dict):           # error envelope
                time.sleep(2 + 2 * attempt)
                continue
            by_id = {o.get("id"): o for o in out}
            return [by_id.get(i) for i in range(len(calls))]
        except Exception:  # noqa: BLE001
            time.sleep(2 + 2 * attempt)
    return [None] * len(calls)


def gt_get(path, tries=5, pause=2.3):
    """GeckoTerminal GET. Free tier ~30 req/min, so every call is paced."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                GT + path,
                headers={"Accept": "application/json;version=20230302",
                         "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.load(r)
            time.sleep(pause)
            return j
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 + 8 * attempt)
                continue
            if e.code == 404:
                return None          # meaningful: pool not indexed
            time.sleep(4)
        except Exception:  # noqa: BLE001
            time.sleep(4)
    return None


def cache(name, producer, force=False):
    """Run `producer` unless data/<name> already exists."""
    path = os.path.join(DATA, name)
    if os.path.exists(path) and not force:
        with open(path) as f:
            return json.load(f)
    val = producer()
    with open(path, "w") as f:
        json.dump(val, f)
    return val


# --------------------------------------------------------------------------
# Stage 1 -- enumerate launches from chain logs (unbiased denominator)
# --------------------------------------------------------------------------

def enumerate_launches(start, end, width=50_000, log=print):
    """
    Sweep `PairCreated` (v2) and `PoolCreated` (v3) logs across [start, end).

    Windows that time out are retried at halved width; ranges that still fail
    are recorded in `_gaps` so the report can state exactly how much of the
    block range went unobserved instead of silently under-counting launches.
    """
    events, gaps = {}, []

    def sweep(topic, kind, lo, hi, w, depth=0):
        b = lo
        while b < hi:
            to = min(b + w, hi)
            r = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                     "topics": [topic]}])
            if "result" not in r:
                if depth < 3 and w > 3_000:
                    sweep(topic, kind, b, to, max(w // 4, 3_000), depth + 1)
                else:
                    gaps.append({"kind": kind, "from": b, "to": to,
                                 "err": str(r.get("error", {}).get("message"))})
                b = to
                continue
            for lg in r["result"]:
                data = lg["data"][2:]
                # v2 PairCreated(token0,token1,pair,uint): 2 indexed ->
                #     data = [pair(32), allPairsLength(32)]; pool = word 0
                # v3 PoolCreated(token0,token1,fee,tickSpacing,pool): 3 indexed
                #     -> data = [tickSpacing(32), pool(32)]; pool = word 1
                if kind == "v2":
                    pool = "0x" + data[24:64] if len(data) >= 64 else None
                else:
                    pool = "0x" + data[64 + 24:128] if len(data) >= 128 else None
                if not pool:
                    continue
                events[pool.lower()] = {
                    "pool": pool.lower(),
                    "factory": lg["address"].lower(),
                    "kind": kind,
                    "token0": "0x" + lg["topics"][1][-40:],
                    "token1": "0x" + lg["topics"][2][-40:],
                    "block": int(lg["blockNumber"], 16),
                    "tx": lg["transactionHash"],
                }
            log(f"{kind} {b}-{to} logs={len(r['result'])} total={len(events)}")
            b = to

    for topic, kind in ((TOPIC_PAIR_CREATED, "v2"), (TOPIC_POOL_CREATED, "v3")):
        sweep(topic, kind, start, end, width)
    return {"events": events, "gaps": gaps,
            "range": [start, end], "width": width}


# --------------------------------------------------------------------------
# Stage 2 -- deployer (tx.origin) per launch, via batched RPC
# --------------------------------------------------------------------------

def resolve_deployers(events, batch=100, log=print):
    """
    The deployer is tx.origin of the pool-creation transaction, i.e. the `from`
    of the tx that emitted PairCreated -- NOT the factory contract, and not
    Blockscout's `creator_address_hash` (which for factory-deployed pools is
    the factory itself).
    """
    txs = sorted({e["tx"] for e in events.values()})
    out, done = {}, 0
    for i in range(0, len(txs), batch):
        chunk = txs[i:i + batch]
        res = rpc_batch([("eth_getTransactionByHash", [h]) for h in chunk])
        for h, r in zip(chunk, res):
            if r and r.get("result"):
                out[h] = {"from": r["result"]["from"].lower(),
                          "to": (r["result"].get("to") or "").lower()}
        done += len(chunk)
        log(f"deployers {done}/{len(txs)} resolved={len(out)}")
    return out


def block_times(events, log=print):
    """Timestamp for each distinct creation block (batched)."""
    blocks = sorted({e["block"] for e in events.values()})
    out = {}
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        res = rpc_batch([("eth_getBlockByNumber", [hex(b), False]) for b in chunk])
        for b, r in zip(chunk, res):
            if r and r.get("result"):
                out[str(b)] = int(r["result"]["timestamp"], 16)
        log(f"blocktimes {min(i + 100, len(blocks))}/{len(blocks)}")
    return out


# --------------------------------------------------------------------------
# Stage 3 -- outcomes from GeckoTerminal
# --------------------------------------------------------------------------

def gt_index_snapshot(log=print):
    """
    Crawl every GT-indexed robinhood pool reachable through the paginated
    endpoints (network-wide, new_pools, and per-DEX). GT caps each endpoint at
    10 pages x 20 pools, so per-DEX crawling is what actually widens coverage.
    """
    pools = {}

    def absorb(d, src):
        if not d or not d.get("data"):
            return 0
        inc = {it["id"]: it["attributes"] for it in d.get("included", [])}
        n = 0
        for p in d["data"]:
            a = p["attributes"]
            addr = (a.get("address") or "").lower()
            if not addr:
                continue
            rel = p.get("relationships", {})
            bt = (rel.get("base_token") or {}).get("data") or {}
            dx = (rel.get("dex") or {}).get("data") or {}
            rec = pools.setdefault(addr, {})
            rec.update({k: a.get(k) for k in
                        ("address", "name", "pool_created_at", "fdv_usd",
                         "market_cap_usd", "reserve_in_usd",
                         "base_token_price_usd")})
            rec["dex"] = dx.get("id")
            rec["src"] = rec.get("src") or src
            rec["base_token"] = (bt.get("id") or "").replace("robinhood_", "")
            if bt.get("id") in inc:
                rec["base_symbol"] = inc[bt["id"]].get("symbol")
            rec["vol_h24"] = (a.get("volume_usd") or {}).get("h24")
            n += 1
        return n

    for sort in ("h24_volume_usd_desc", "h24_tx_count_desc"):
        for page in range(1, 11):
            if not absorb(gt_get(f"/networks/{GT_NET}/pools?sort={sort}&page={page}"
                                 "&include=base_token,dex"), "net"):
                break
    for page in range(1, 11):
        if not absorb(gt_get(f"/networks/{GT_NET}/new_pools?page={page}"
                             "&include=base_token,dex"), "new"):
            break
    dl = gt_get(f"/networks/{GT_NET}/dexes")
    for dx in [x["id"] for x in (dl or {}).get("data", [])]:
        for page in range(1, 11):
            if not absorb(gt_get(f"/networks/{GT_NET}/dexes/{dx}/pools?page={page}"
                                 "&sort=h24_volume_usd_desc&include=base_token,dex"),
                          f"dex:{dx}"):
                break
        log(f"gt dex {dx} total={len(pools)}")
    return pools


def peak_mcap_from_ohlcv(pool_addr, fdv_now, price_now):
    """
    Peak market cap = max daily high price x implied supply.

    Implied supply is derived from GT's current FDV / current price, because
    GT does not expose supply directly and `market_cap_usd` is frequently null
    for memecoins. FDV is the right basis here: these tokens launch with
    effectively full supply outstanding.

    Returns (peak_mcap, n_candles, first_ts, last_ts) or None when OHLCV is
    unavailable (delisted / never indexed).
    """
    d = gt_get(f"/networks/{GT_NET}/pools/{pool_addr}/ohlcv/day?aggregate=1&limit=1000")
    if not d:
        return None
    lst = (d.get("data", {}).get("attributes", {}) or {}).get("ohlcv_list") or []
    if not lst:
        return None
    highs = [c[2] for c in lst if c and c[2] is not None]
    if not highs or not price_now or float(price_now) <= 0:
        return None
    supply = float(fdv_now) / float(price_now) if fdv_now else None
    if not supply:
        return None
    ts = [c[0] for c in lst]
    return (max(highs) * supply, len(lst), min(ts), max(ts))


# --------------------------------------------------------------------------
# Stage 4 -- tier assignment
# --------------------------------------------------------------------------

def assign_tier(launch, now_ts):
    """
    Map a launch to its highest achieved bonding tier.

    `indexed=False` means the pool exists on-chain but GeckoTerminal never
    indexed it -> it never sustained real trading. That is treated as
    rugged/dead, which is the whole point of enumerating from chain logs.
    """
    age_days = (now_ts - launch["created_ts"]) / 86400.0
    peak = launch.get("peak_mcap")

    if not launch.get("indexed"):
        return "rugged" if age_days >= SURVIVE_DAYS else "too_young"
    if peak is None:
        return "unknown_peak"
    if peak >= MCAP_HIGH:
        return "bonded_high"
    if peak >= MCAP_MID:
        return "bonded_mid"
    if peak < RUG_MCAP_CEIL and age_days >= SURVIVE_DAYS:
        return "rugged"
    if age_days >= SURVIVE_DAYS and launch.get("alive"):
        return "survived"
    if age_days < SURVIVE_DAYS:
        return "too_young"
    return "rugged"


def win_score_from_counts(c):
    n = c.get("total_launches", 0)
    if n <= 0:
        return 0.0
    return (c.get("survived", 0) * 1
            + c.get("bonded_mid", 0) * 2
            + c.get("bonded_high", 0) * 5
            + c.get("migrated", 0) * 3
            - c.get("rugged", 0) * 3
            - c.get("copy_cat", 0) * 1) / n


# --------------------------------------------------------------------------
# Stage 5 -- point-in-time replay
# --------------------------------------------------------------------------

def replay(launches, thresholds=SWEEP, min_launches=MIN_LAUNCHES):
    """
    For every launch, score its deployer using ONLY that deployer's strictly
    earlier launches, then test the prediction "this one reaches >= $3M".

    Strict point-in-time: the scored launch itself and everything after it are
    excluded from the score, so there is no look-ahead.
    """
    by_dep = defaultdict(list)
    for lc in launches:
        if lc.get("deployer"):
            by_dep[lc["deployer"]].append(lc)
    for v in by_dep.values():
        v.sort(key=lambda x: (x["created_ts"], x["block"]))

    rows = []
    for dep, seq in by_dep.items():
        counts = defaultdict(int)
        for lc in seq:
            prior = counts["total_launches"]
            score = win_score_from_counts(counts) if prior >= min_launches else None
            hit_1m = (lc.get("peak_mcap") or 0) >= MCAP_1M
            hit_3m = (lc.get("peak_mcap") or 0) >= MCAP_MID
            hit_50m = (lc.get("peak_mcap") or 0) >= MCAP_HIGH
            rows.append({
                "pool": lc["pool"], "deployer": dep,
                "created_ts": lc["created_ts"], "tier": lc["tier"],
                "peak_mcap": lc.get("peak_mcap"),
                "prior_launches": prior, "score_asof": score,
                "hit_1m": hit_1m, "hit_3m": hit_3m, "hit_50m": hit_50m,
                "scoreable": score is not None,
            })
            # fold this launch into the deployer's running history
            t = lc["tier"]
            if t in ("survived", "bonded_mid", "bonded_high", "migrated", "rugged", "copy_cat"):
                counts[t] += 1
                counts["total_launches"] += 1
            elif t in ("unknown_peak", "too_young"):
                counts["total_launches"] += 1

    scoreable = [r for r in rows if r["scoreable"]]
    out = {"n_rows": len(rows), "n_scoreable": len(scoreable), "thresholds": {}}
    for th in thresholds:
        pred = [r for r in scoreable if r["score_asof"] >= th]
        tp = sum(1 for r in pred if r["hit_3m"])
        all_hits = [r for r in scoreable if r["hit_3m"]]
        out["thresholds"][str(th)] = {
            "n_predicted": len(pred),
            "n_true_positive": tp,
            "precision": (tp / len(pred)) if pred else None,
            "recall": (tp / len(all_hits)) if all_hits else None,
            "n_actual_hits": len(all_hits),
            "hit_1m_rate": (sum(1 for r in pred if r["hit_1m"]) / len(pred)) if pred else None,
            "hit_3m_rate": (tp / len(pred)) if pred else None,
            "hit_50m_rate": (sum(1 for r in pred if r["hit_50m"]) / len(pred)) if pred else None,
        }
    base = {
        "base_rate_1m": (sum(1 for r in scoreable if r["hit_1m"]) / len(scoreable)) if scoreable else None,
        "base_rate_3m": (sum(1 for r in scoreable if r["hit_3m"]) / len(scoreable)) if scoreable else None,
        "base_rate_50m": (sum(1 for r in scoreable if r["hit_50m"]) / len(scoreable)) if scoreable else None,
    }
    out["base_rates_scoreable_universe"] = base
    out["rows"] = rows
    return out


# --------------------------------------------------------------------------
# Bonus -- Bags launchpad probe (handoff step 7)
# --------------------------------------------------------------------------

def bags_probe(start, end, log=print):
    """
    Count events emitted by the Bags bonding-curve factory in the window.

    Bonding-curve launches do NOT emit Uniswap `PairCreated` until they
    graduate, so this measures how much launch volume the Uniswap-log universe
    structurally misses.
    """
    total, sample_topics, gaps = 0, defaultdict(int), 0
    b = start
    while b < end:
        to = min(b + 50_000, end)
        r = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                 "address": BAGS_FACTORY}])
        if "result" in r:
            total += len(r["result"])
            for lg in r["result"]:
                if lg.get("topics"):
                    sample_topics[lg["topics"][0]] += 1
        else:
            gaps += 1
        b = to
    return {"factory": BAGS_FACTORY, "events": total,
            "topic0_counts": dict(sample_topics), "failed_windows": gaps}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def build_dataset(start, end, log=print, max_ohlcv=None):
    """Run stages 1-4 and return the launch records ready for replay()."""
    enum = cache("chain_launches.json",
                 lambda: enumerate_launches(start, end, log=log))
    events = enum["events"]
    log(f"[1] launches enumerated: {len(events)} (gaps={len(enum['gaps'])})")

    deployers = cache("deployers.json", lambda: resolve_deployers(events, log=log))
    log(f"[2] deployers resolved for {len(deployers)} txs")

    btimes = cache("block_times.json", lambda: block_times(events, log=log))
    log(f"[3] block timestamps: {len(btimes)}")

    gt_idx = cache("gt_index.json", lambda: gt_index_snapshot(log=log))
    log(f"[4] GT-indexed robinhood pools: {len(gt_idx)}")

    now_ts = int(datetime.now(timezone.utc).timestamp())
    launches, indexed_pools = [], []
    for addr, ev in events.items():
        d = deployers.get(ev["tx"]) or {}
        ts = btimes.get(str(ev["block"]))
        if ts is None:
            continue
        gt = gt_idx.get(addr)
        rec = {
            "pool": addr, "block": ev["block"], "created_ts": ts,
            "deployer": d.get("from"), "factory": ev["factory"],
            "kind": ev["kind"], "indexed": gt is not None,
            "peak_mcap": None, "alive": False,
        }
        if gt:
            rec["symbol"] = gt.get("base_symbol")
            rec["fdv_now"] = gt.get("fdv_usd")
            rec["price_now"] = gt.get("base_token_price_usd")
            rec["reserve_usd"] = gt.get("reserve_in_usd")
            try:
                rec["alive"] = float(gt.get("reserve_in_usd") or 0) > 1000
            except (TypeError, ValueError):
                rec["alive"] = False
            indexed_pools.append(rec)
        launches.append(rec)

    # Peak mcap only for GT-indexed pools: 1 request each, and non-indexed
    # pools have no OHLCV to fetch by definition.
    peaks = cache("peaks.json", lambda: _fetch_peaks(indexed_pools, log, max_ohlcv))
    n_404 = 0
    for rec in launches:
        p = peaks.get(rec["pool"])
        if p and p.get("peak_mcap") is not None:
            rec["peak_mcap"] = p["peak_mcap"]
            rec["ohlcv_candles"] = p.get("candles")
        elif rec["indexed"]:
            n_404 += 1
    log(f"[5] peak mcap resolved; indexed-but-no-OHLCV: {n_404}")

    for rec in launches:
        rec["tier"] = assign_tier(rec, now_ts)
    return launches, enum, gt_idx, n_404


def _fetch_peaks(indexed_pools, log, max_ohlcv=None):
    out = {}
    pool_list = indexed_pools if max_ohlcv is None else indexed_pools[:max_ohlcv]
    for i, rec in enumerate(pool_list, 1):
        r = peak_mcap_from_ohlcv(rec["pool"], rec.get("fdv_now"), rec.get("price_now"))
        if r:
            out[rec["pool"]] = {"peak_mcap": r[0], "candles": r[1],
                                "first_ts": r[2], "last_ts": r[3]}
        else:
            out[rec["pool"]] = {"peak_mcap": None}
        if i % 20 == 0:
            log(f"ohlcv {i}/{len(pool_list)}")
    return out


def summarize(launches, replay_out, enum, gt_idx, n_404, start, end):
    tiers = defaultdict(int)
    for lc in launches:
        tiers[lc["tier"]] += 1
    deps = defaultdict(int)
    for lc in launches:
        if lc.get("deployer"):
            deps[lc["deployer"]] += 1
    multi = {k: v for k, v in deps.items() if v >= MIN_LAUNCHES}
    observed = (end - start) - sum(g["to"] - g["from"] for g in enum["gaps"])
    return {
        "meta": {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "chain_id": CHAIN_ID,
            "block_range": [start, end],
            "blocks_requested": end - start,
            "blocks_observed": observed,
            "coverage_pct": round(100.0 * observed / (end - start), 2),
            "window_days_approx": round((end - start) / BLOCKS_PER_DAY, 2),
            "enumeration_gaps": enum["gaps"],
        },
        "dataset": {
            "launches_onchain": len(launches),
            "launches_gt_indexed": sum(1 for l in launches if l["indexed"]),
            "gt_index_total_pools": len(gt_idx),
            "indexed_but_no_ohlcv": n_404,
            "distinct_deployers": len(deps),
            "deployers_ge_min_launches": len(multi),
            "min_launches": MIN_LAUNCHES,
        },
        "tier_counts": dict(tiers),
        "replay": {k: v for k, v in replay_out.items() if k != "rows"},
        "score_threshold_sweep": replay_out["thresholds"],
        "decision_gate": _gate(replay_out),
    }


def _gate(replay_out):
    t = replay_out["thresholds"].get(str(SCORE_THRESHOLD), {})
    p = t.get("precision")
    if t.get("n_predicted", 0) == 0 or p is None:
        return {"verdict": "INCONCLUSIVE",
                "reason": "no launches met the score/history requirement"}
    if p < 0.30:
        return {"verdict": "ABANDON", "precision": p}
    if p <= 0.50:
        return {"verdict": "ITERATE_FILTER", "precision": p}
    return {"verdict": "PROCEED_TO_PHASE_1", "precision": p}


def main():
    ap = argparse.ArgumentParser(description="Hood Sniper Phase 0 backtest")
    ap.add_argument("--stage", default="all",
                    choices=["all", "enumerate", "deployers", "gt", "replay", "bags"])
    ap.add_argument("--start", type=int, default=22_000_000)
    ap.add_argument("--end", type=int, default=24_000_000)
    ap.add_argument("--max-ohlcv", type=int, default=None,
                    help="cap OHLCV requests (GT free tier is ~30/min)")
    ap.add_argument("--out", default=os.path.join(DATA, "backtest_results.json"))
    args = ap.parse_args()

    def log(m):
        print(m, flush=True)

    if args.stage == "bags":
        print(json.dumps(bags_probe(args.start, args.end, log=log), indent=2))
        return
    if args.stage == "enumerate":
        cache("chain_launches.json",
              lambda: enumerate_launches(args.start, args.end, log=log))
        return
    if args.stage == "gt":
        cache("gt_index.json", lambda: gt_index_snapshot(log=log))
        return

    launches, enum, gt_idx, n_404 = build_dataset(
        args.start, args.end, log=log, max_ohlcv=args.max_ohlcv)
    rep = replay(launches)
    summary = summarize(launches, rep, enum, gt_idx, n_404, args.start, args.end)
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(DATA, "replay_rows.json"), "w") as f:
        json.dump(rep["rows"], f)
    print(json.dumps(summary, indent=2)[:4000])
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
