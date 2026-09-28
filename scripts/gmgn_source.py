#!/usr/bin/env python3
"""
GMGN as a DISCOVERY source across every chain, not just a second opinion.

WHAT THIS CHANGES
    GMGN was wired only as probe_gmgn(ctx) -- an outside read on a token we had
    already found ourselves. Discovery was never connected, so every coin on the
    board came from our own collectors and inherited their blind spots.

    `market trenches` returns, in ONE call per chain, fields we either compute
    worse or could not compute at all:

      smart_degen_count / renowned_count   a MAINTAINED smart-money count. Ours
                                           went 12 days stale and rebuilt to 44
                                           wallets.
      bot_degen_count                      bot participation, and
      bundler_trader_amount_rate           BUNDLE DETECTION -- which this project
                                           tried to build from funding traces and
                                           abandoned at 12-21% wallet coverage.
      swaps_24h / buys_24h / sells_24h     the swell filter, hand-built from tape
      net_buy_24h                          buy-side pressure in dollars
      creator_created_count                deployer history
      top_10_holder_rate, is_honeypot,     safety fields
      owner_renounced, burn_status
      twitter_handle, x_user_follower      social, which we have none of

    It also covers all three chains we watch (robinhood / sol / bsc) with the same
    schema, and splits by pipeline stage: new_creation, near_completion, completed.

WHAT THIS IS NOT
    Not a replacement for our own collectors. GMGN is a third party with its own
    indexing lag and its own incentives, and this project has been burned three
    times by treating a vendor's healthy-looking response as truth. Our feeds stay
    the primary record; this is a second, independent net that catches what our
    chain listeners miss -- and disagreement between the two is itself information.

    Nothing here signs anything. The CLI can swap and create tokens; those
    subcommands are never invoked, and the signing key is stripped from the child
    environment by _gmgn().

VERSION PINNING
    investigate._gmgn runs a PINNED gmgn-cli, not @latest. It executes third-party
    code with the API key in its environment on every call, so an auto-updating
    dependency is an unattended supply-chain hole.
"""
import json
import os
import sys
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import investigate as I  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
FEED = os.path.join(DATA, "gmgn_feed.jsonl")

CHAINS = ("robinhood", "sol", "bsc")
STAGES = ("new_creation", "near_completion", "completed")


def _rows(payload):
    """Find the row list wherever the CLI nests it, without assuming a shape."""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload
    if isinstance(payload, dict):
        for v in payload.values():
            r = _rows(v)
            if r:
                return r
    return None


def pull(chain, stage, limit=80, log=print):
    d, err = I._gmgn(["market", "trenches", "--chain", chain,
                      "--type", stage, "--limit", str(limit),
                      "--sort-by", "smart_degen_count", "--raw"], timeout=90)
    if err:
        # a failed pull is reported, never written as an empty result -- silent
        # holes are the most repeated bug in this project
        log(f"  {chain:10s} {stage:16s} ERROR {err}")
        return []
    rows = _rows(d) or []
    log(f"  {chain:10s} {stage:16s} {len(rows):3d} rows")
    return rows


def snapshot(stages=None, log=print):
    """
    One pass. `stages` limits which categories are pulled, because they do not
    move at the same speed and polling them equally wastes the budget:

      new_creation / near_completion   change constantly -- this is where an entry
                                       decision lives, so freshness matters
      completed                        already graduated; it is a slow-moving
                                       reference list, not a trading signal

    Measured: one CLI call is ~0.6-0.9s and 12 back-to-back calls drew no rate
    limiting, so the constraint is politeness rather than throughput. Splitting the
    cadence buys 4x freshness on the actionable stages for well under double the
    calls.
    """
    ts = time.time()
    n = 0
    use = tuple(stages) if stages else STAGES
    with open(FEED, "a") as f:
        for chain in CHAINS:
            for stage in use:
                for r in pull(chain, stage, log=log):
                    f.write(json.dumps({
                        "ts": ts, "src": "gmgn", "chain": chain, "stage": stage,
                        "address": r.get("address"), "symbol": r.get("symbol"),
                        "name": r.get("name"),
                        "launchpad": r.get("launchpad_platform") or r.get("launchpad"),
                        "mcap": r.get("market_cap"), "liq": r.get("liquidity"),
                        "progress": r.get("progress"),
                        "swaps_24h": r.get("swaps_24h"),
                        "buys_24h": r.get("buys_24h"), "sells_24h": r.get("sells_24h"),
                        "net_buy_24h": r.get("net_buy_24h"),
                        "volume_24h": r.get("volume_24h"),
                        "smart": r.get("smart_degen_count"),
                        "renowned": r.get("renowned_count"),
                        "bots": r.get("bot_degen_count"),
                        "bundler_rate": r.get("bundler_trader_amount_rate"),
                        "top10_rate": r.get("top_10_holder_rate"),
                        "holders": r.get("holder_count"),
                        "honeypot": r.get("is_honeypot"),
                        "renounced": r.get("owner_renounced"),
                        "creator": r.get("creator"),
                        "creator_launches": r.get("creator_created_count"),
                        "buy_tax": r.get("total_buy_tax"),
                        "sell_tax": r.get("total_sell_tax"),
                        "twitter": r.get("twitter_handle"),
                        "x_followers": r.get("x_user_follower"),
                        "created_ts": r.get("created_timestamp"),
                    }) + "\n")
                    n += 1
    log(f"  wrote {n} rows -> {FEED}")
    return n


def top(limit=15, min_smart=1, log=print):
    """Read back the newest snapshot, best first. Pure file read, no network."""
    if not os.path.exists(FEED):
        return log("  no gmgn feed yet — run --snapshot")
    latest = {}
    with open(FEED) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("address"):
                latest[r["address"]] = r
    rows = [r for r in latest.values() if (r.get("smart") or 0) >= min_smart]
    rows.sort(key=lambda r: -((r.get("smart") or 0) * 100 + (r.get("swaps_24h") or 0) / 100))
    log(f"\n{'chain':>9} {'stage':>15} {'symbol':>12} {'mcap':>10} {'smart':>6} "
        f"{'swaps24':>8} {'netbuy':>10} {'bots':>5}")
    for r in rows[:limit]:
        log(f"{r['chain']:>9} {r['stage']:>15} {str(r.get('symbol'))[:12]:>12} "
            f"{(r.get('mcap') or 0):>10,.0f} {(r.get('smart') or 0):>6} "
            f"{(r.get('swaps_24h') or 0):>8,} {(r.get('net_buy_24h') or 0):>10,.0f} "
            f"{(r.get('bots') or 0):>5}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--stages", nargs="*", default=None,
                    help="limit to these stages (default all three)")
    ap.add_argument("--top", action="store_true")
    ap.add_argument("--min-smart", type=int, default=1)
    a = ap.parse_args()
    if a.snapshot:
        snapshot(stages=a.stages)
    elif a.top:
        top(min_smart=a.min_smart)
    else:
        ap.print_help()
