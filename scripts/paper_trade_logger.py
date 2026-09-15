#!/usr/bin/env python3
"""
Hood Sniper -- Strategy B paper-trade logger.

OBSERVATIONAL ONLY. This script never signs, never broadcasts, and holds no keys.
It watches the chain, records what a trade WOULD have done, and measures the one
number the backtest could not: real executable slippage.

What it does
------------
1. Watches Pons `CurveCompleted` (a graduation).
2. Resolves curve -> token -> the graduated Uniswap V4 pool (`Initialize`).
3. Reads every `Swap` on that pool -- these are real fills, not candles.
4. Simulates entry after a configurable latency, then TP / stop / time exit.
5. Computes exact within-tick price impact for the stake from the pool's own
   `liquidity` and `sqrtPriceX96`, so slippage is derived from pool state rather
   than assumed.

Why it matters: STRATEGY_B_SPEC.md puts breakeven at ~18-20% slippage. Everything
rests on real slippage landing well under that. This measures it.

Usage:
    python3 paper_trade_logger.py --backfill 400000        # replay recent history
    python3 paper_trade_logger.py --live                   # follow the chain
    python3 paper_trade_logger.py --backfill 400000 --stake 25 --tp 5 --stop 0.7
"""

import argparse
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from investigate import quote_impact          # noqa: E402  single source of impact math

DATA = os.path.join(os.path.dirname(HERE), "data")
os.makedirs(DATA, exist_ok=True)
JOURNAL = os.path.join(DATA, "paper_trades.jsonl")

RPCS = [
    "https://rpc.mainnet.chain.robinhood.com",
    "https://robinhood-rpc.publicnode.com",
]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# --- contracts / topics (all derived + verified in Phase 0) -----------------
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PONS_LAUNCHPAD = "0xe33e9e479df8802cb0866d5d05258bec4cf62948"

T_CURVE_COMPLETED = "0xf8d37a90738ae063b8b8058b66f5880cf3cf7ab0c5d4fa78219696591dfbfb67"
T_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
T_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
T_LAUNCHED = "0xdcacba5e347ae7abd91cb519eb877af8fa7774e347b85dd3ddcd24a2ba8cdf37"
T_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
T_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
T_EXEMPTED = "0xe4b7e48fbd47c2f602bacadee76ad33b16542ddb4997cfc0de04c311adcfa8c7"
T_SNIPE_CHARGED = "0x3bc39a5562b28f5fe8f36cecabfbaa12bb969acf05717994709225fc412a9934"

REGISTRY = os.path.join(DATA, "dev_registry.json")

SEL_TOKEN = "0xfc0c546a"       # token()
SEL_PAIRTOKEN = "0x3de35b79"   # pairToken()
SEL_DEPLOYER = "0xd5f39488"    # deployer()  -- the dev wallet, straight off the curve

BLOCK_TIME = 0.101
ETH_USD = 2450.0               # override with --eth-usd

_rr = itertools.cycle(RPCS)


# ---------------------------------------------------------------- transport
def rpc(method, params, tries=5, timeout=45):
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
                if j["error"].get("code") == 429:
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


def get_logs(params, max_width=40_000):
    """getLogs with automatic range splitting -- this RPC dies above ~50k blocks."""
    lo = int(params["fromBlock"], 16)
    hi = int(params["toBlock"], 16) if params["toBlock"] != "latest" else head_block()
    out = []
    b = lo
    while b <= hi:
        to = min(b + max_width, hi)
        p = dict(params, fromBlock=hex(b), toBlock=hex(to))
        r = rpc("eth_getLogs", [p])
        if "result" in r:
            out.extend(r["result"])
        elif max_width > 5000:
            sub = b
            while sub <= to:
                s2 = min(sub + max_width // 4, to)
                r2 = rpc("eth_getLogs", [dict(params, fromBlock=hex(sub), toBlock=hex(s2))])
                if "result" in r2:
                    out.extend(r2["result"])
                sub = s2 + 1
        b = to + 1
    return out


def head_block():
    r = rpc("eth_blockNumber", [])
    return int(r["result"], 16) if "result" in r else 0


def s256(h):
    """Two's-complement decode. v4 sign-extends int128 across the full 32-byte word."""
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


# ---------------------------------------------------------------- discovery
def curve_token(curve, block=None):
    """
    Always query at "latest": the public RPC prunes state, so an eth_call pinned
    to an old block returns empty for anything but the most recent graduations.
    `token()` is set once at initialize, so "latest" is the same value.
    """
    r = rpc("eth_call", [{"to": curve, "data": SEL_TOKEN}, "latest"])
    res = r.get("result")
    return "0x" + res[-40:] if res and len(res) >= 66 else None


def find_pool(token, grad_block, lookahead=5000):
    """
    The graduated V4 pool is initialized within a few hundred blocks of graduation.
    Currencies sort by address, and native ETH is 0x0, so the token is usually
    currency1 -- but check both so ERC-20-quoted launches also resolve.
    """
    tt = "0x" + "0" * 24 + token[2:]
    for slot in (3, 2):
        topics = [T_INITIALIZE, None, None, None]
        topics[slot] = tt
        logs = get_logs({"fromBlock": hex(max(0, grad_block - 300)),
                         "toBlock": hex(grad_block + lookahead),
                         "address": POOL_MANAGER,
                         "topics": topics[:slot + 1]})
        if logs:
            lg = sorted(logs, key=lambda x: int(x["blockNumber"], 16))[0]
            # the OTHER currency is the quote: topic2 = currency0, topic3 = currency1
            quote = "0x" + (lg["topics"][2] if slot == 3 else lg["topics"][3])[-40:]
            return {"pool_id": lg["topics"][1],
                    "init_block": int(lg["blockNumber"], 16),
                    "token_is_currency1": slot == 3,
                    "quote": quote}
    return None


def pool_swaps(pool_id, from_block, to_block, token_is_currency1=True):
    """
    Swaps for one pool, priced as QUOTE PER TOKEN.

    `price` and `side` both depend on orientation. This used to hardcode
    abs(a0)/abs(a1) and "buy if a0 > 0", which is only right when the token is
    currency1 -- always true for ETH-quoted pools (ETH is 0x0 and sorts first)
    but a coin flip for ERC-20-quoted ones. When the token sorted first the
    price series came out INVERTED, so a pump replayed as a dump straight into
    the stop.
    """
    logs = get_logs({"fromBlock": hex(from_block), "toBlock": hex(to_block),
                     "address": POOL_MANAGER, "topics": [T_SWAP, pool_id]})
    out = []
    for lg in logs:
        d = lg["data"][2:]
        if len(d) < 256:
            continue
        a0 = s256(d[0:64])
        a1 = s256(d[64:128])
        sqrt_p = int(d[128:192], 16)
        liq = int(d[192:256], 16)
        if a0 == 0 or a1 == 0:
            continue
        quote_amt, token_amt = (a0, a1) if token_is_currency1 else (a1, a0)
        out.append({"block": int(lg["blockNumber"], 16),
                    "amount0": a0, "amount1": a1,
                    "sqrt_price_x96": sqrt_p, "liquidity": liq,
                    "price": abs(quote_amt) / abs(token_amt),
                    "side": "buy" if quote_amt > 0 else "sell",
                    "tx": lg["transactionHash"]})
    out.sort(key=lambda x: x["block"])
    return out


# ------------------------------------------------------------ dev vetting
def load_registry():
    if not os.path.exists(REGISTRY):
        return {}, {}
    with open(REGISTRY) as f:
        reg = json.load(f)
    curve2dev = {}
    for dev, v in reg.items():
        for c in v.get("curves", []):
            curve2dev[c] = dev
    return reg, curve2dev


def save_registry(reg):
    with open(REGISTRY, "w") as f:
        json.dump(reg, f)


def resolve_dev(curve, grad_block=None, curve2dev=None):
    """
    Dev identity straight from the curve's own `deployer()` getter.

    The obvious route -- the `Launched` event -- is NOT reliable: Pons runs
    several launchpad/router contracts (0xe33e9e47, 0xa5aab3f0, 0x4783c67b,
    0xe47e41f4, 0x7ed598bc, ...), so filtering Launched on any single address
    silently misses launches. Curves created through the other routers resolve
    to None that way. `deployer()` is one eth_call and always works.
    """
    if curve2dev and curve in curve2dev:
        return curve2dev[curve]
    r = rpc("eth_call", [{"to": curve, "data": SEL_DEPLOYER}, "latest"])
    res = r.get("result")
    return ("0x" + res[-40:]).lower() if res and len(res) >= 66 else None


def dev_prior_stats(dev, reg, grad_block):
    """Point-in-time: only count graduations that happened BEFORE this one."""
    if not dev or dev not in reg:
        return {"dev_launches": 0, "dev_prior_graduations": 0, "dev_known": False}
    v = reg[dev]
    prior = sum(1 for b in v.get("grad_blocks", []) if b < grad_block)
    return {"dev_launches": v.get("launches", 0),
            "dev_prior_graduations": prior,
            "dev_known": True}


def curve_features(curve, grad_block, lookback=1_500_000):
    """
    The five vetting checks, all from curve events:
      holder distribution -> CurveBuy/CurveSell net positions
      block-1 snipers     -> SnipeTaxCharged
      dev bundle          -> SnipeTaxExempted
      insiders exiting    -> exempt wallets that sold pre-graduation
    """
    logs = get_logs({"fromBlock": hex(max(0, grad_block - lookback)),
                     "toBlock": hex(grad_block), "address": curve})
    buys, sells, exempt, sniped = {}, {}, set(), set()
    for lg in logs:
        t0 = lg["topics"][0]
        if t0 == T_CURVE_BUY and len(lg["topics"]) > 1:
            a = "0x" + lg["topics"][1][-40:]
            d = lg["data"][2:]
            if len(d) >= 128:
                buys[a] = buys.get(a, 0) + int(d[64:128], 16)
        elif t0 == T_CURVE_SELL and len(lg["topics"]) > 1:
            a = "0x" + lg["topics"][1][-40:]
            d = lg["data"][2:]
            if len(d) >= 64:
                sells[a] = sells.get(a, 0) + int(d[0:64], 16)
        elif t0 == T_EXEMPTED and len(lg["topics"]) > 1:
            exempt.add("0x" + lg["topics"][1][-40:])
        elif t0 == T_SNIPE_CHARGED and len(lg["topics"]) > 1:
            sniped.add("0x" + lg["topics"][1][-40:])
    tot = sum(buys.values()) or 1
    top = sorted(buys.values(), reverse=True)
    ex_sold = sum(1 for a in exempt if sells.get(a, 0) > 0)
    return {"n_buyers": len(buys), "n_sellers": len(sells),
            "n_exempt": len(exempt), "n_snipers": len(sniped),
            "top1_share": round(top[0] / tot, 4) if top else 0.0,
            "top5_share": round(sum(top[:5]) / tot, 4) if top else 0.0,
            "exempt_sold_frac": round(ex_sold / len(exempt), 4) if exempt else 0.0,
            "sell_to_buy_ratio": round(sum(sells.values()) / tot, 4)}


def vetting_size(feat, prior, base_stake):
    """
    FLAT until a vetting signal replicates out-of-sample.

    Both candidates FAILED their first out-of-sample test (n=267 live trades):
      zero-snipers  in-sample >=5x 46% vs 23% (p=0.0065)
                    OUT-OF-SAMPLE TP 8.3% vs 11.6% -- direction REVERSED, p=0.33
      dev prior-grad  n=3 live, EV 0.648x -- too few to judge, not encouraging

    Tilting on them HURT: +12.09% return on capital vs +13.20% flat.

    Do NOT flip the tilt to favour snipers>0 just because that fits the live
    data (it shows +13.82%) -- that is refitting noise in the other direction,
    the exact error this test just caught.
    """
    return base_stake


# ---------------------------------------------------------------- simulation
def simulate(swaps, entry_block, stake_usd, tp_mult, stop_mult, time_exit_blocks,
             token_is_currency1=True, quote=None):
    """Enter at the first real fill at/after entry_block, then TP/stop/time."""
    entries = [s for s in swaps if s["block"] >= entry_block]
    if not entries:
        return None
    e = entries[0]
    entry_px = e["price"]
    if entry_px <= 0:
        return None
    slip = quote_impact(stake_usd, e["sqrt_price_x96"], e["liquidity"],
                        token_is_currency1, quote)
    tp_px = entry_px * tp_mult
    stop_px = entry_px * stop_mult
    peak = entry_px
    for s in entries[1:]:
        peak = max(peak, s["price"])
        if s["price"] >= tp_px:
            return _res("TP", tp_mult, s["block"] - e["block"], e, peak, entry_px, slip)
        if s["price"] <= stop_px:
            return _res("STOP", stop_mult, s["block"] - e["block"], e, peak, entry_px, slip)
        if s["block"] - e["block"] > time_exit_blocks:
            return _res("TIME", s["price"] / entry_px, s["block"] - e["block"],
                        e, peak, entry_px, slip)
    last = entries[-1]
    return _res("OPEN", last["price"] / entry_px, last["block"] - e["block"],
                e, peak, entry_px, slip)


def _res(kind, mult, blocks, e, peak, entry_px, slip):
    return {"outcome": kind, "gross_mult": mult,
            "blocks_held": blocks, "minutes_held": round(blocks * BLOCK_TIME / 60, 2),
            "entry_block": e["block"], "entry_price": entry_px,
            "peak_price": peak, "peak_mult": peak / entry_px if entry_px else None,
            "entry_liquidity": e["liquidity"],
            "est_slippage_pct": round(slip, 4) if slip is not None else None}


# ---------------------------------------------------------------- driver
def process_graduation(lg, args, seen, reg=None, curve2dev=None):
    curve = lg["address"].lower()
    if curve in seen:
        return None
    seen.add(curve)
    gblk = int(lg["blockNumber"], 16)
    token = curve_token(curve, gblk)
    if not token:
        return {"curve": curve, "grad_block": gblk, "skip": "no_token"}
    pool = find_pool(token, gblk)
    if not pool:
        return {"curve": curve, "token": token, "grad_block": gblk, "skip": "no_pool"}

    latency_blocks = int(args.latency / BLOCK_TIME)
    window = int(args.window_min * 60 / BLOCK_TIME)
    swaps = pool_swaps(pool["pool_id"], pool["init_block"],
                       pool["init_block"] + window, pool["token_is_currency1"])
    if not swaps:
        return {"curve": curve, "token": token, "grad_block": gblk,
                "pool_id": pool["pool_id"], "skip": "no_swaps"}

    # --- dev vetting (the proof-of-concept dataset) ---
    vet, prior, dev = {}, {}, None
    stake = args.stake
    if args.vet and reg is not None:
        dev = resolve_dev(curve, gblk, curve2dev)
        prior = dev_prior_stats(dev, reg, gblk)
        try:
            vet = curve_features(curve, gblk)
        except Exception:  # noqa: BLE001
            vet = {}
        stake = vetting_size(vet, prior, args.stake)

    sim = simulate(swaps, pool["init_block"] + latency_blocks, stake,
                   args.tp, args.stop, int(args.time_exit_min * 60 / BLOCK_TIME),
                   pool["token_is_currency1"], pool.get("quote"))
    if not sim:
        return {"curve": curve, "token": token, "grad_block": gblk, "skip": "no_entry"}

    # --- entry gate: reject un-fillable pools before "trading" them ---
    est = sim.get("est_slippage_pct")
    if est is None or est > args.max_slippage_pct:
        rec = {"ts_utc": datetime.now(timezone.utc).isoformat(),
               "curve": curve, "token": token, "pool_id": pool["pool_id"],
               "grad_block": gblk, "skip": "slippage_gate",
               "quote": pool.get("quote"), "token_is_currency1": pool["token_is_currency1"],
               "est_slippage_pct": est,
               "entry_liquidity": sim.get("entry_liquidity"),
               "n_swaps": len(swaps)}
        with open(JOURNAL, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    fees = args.fee_bps / 10000.0
    slip = (sim["est_slippage_pct"] or 0) / 100.0
    net = sim["gross_mult"] * (1 - fees) * (1 - slip)
    rec = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "curve": curve, "token": token, "pool_id": pool["pool_id"],
        "grad_block": gblk, "init_block": pool["init_block"],
        "init_lag_blocks": pool["init_block"] - gblk,
        "quote": pool.get("quote"), "token_is_currency1": pool["token_is_currency1"],
        "n_swaps": len(swaps),
        "dev": dev, **prior, **vet,
        "stake_usd": stake, "base_stake_usd": args.stake,
        **sim,
        "net_mult": round(net, 4),
        "pnl_usd": round(stake * (net - 1), 2),
    }
    # keep the registry current so later graduations see this dev's record
    if args.vet and reg is not None and dev:
        e = reg.setdefault(dev, {"launches": 0, "graduations": 0, "curves": [],
                                 "graduated_tokens": [], "grad_blocks": []})
        if gblk not in e["grad_blocks"]:
            e["graduations"] += 1
            e["grad_blocks"].append(gblk)
            e["graduated_tokens"].append(token)
            save_registry(reg)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def summarize():
    if not os.path.exists(JOURNAL):
        print("no journal yet")
        return
    rows = []
    with open(JOURNAL) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "net_mult" in r:
                rows.append(r)
    if not rows:
        print("no completed paper trades")
        return
    from collections import Counter
    gated = 0
    with open(JOURNAL) as f:
        for line in f:
            try:
                if json.loads(line).get("skip") == "slippage_gate":
                    gated += 1
            except json.JSONDecodeError:
                pass
    n = len(rows)
    mix = Counter(r["outcome"] for r in rows)
    nets = [r["net_mult"] for r in rows]
    slips = [r["est_slippage_pct"] for r in rows if r.get("est_slippage_pct") is not None]
    holds = sorted(r["minutes_held"] for r in rows)
    lags = sorted(r["init_lag_blocks"] for r in rows)
    print(f"\n=== PAPER TRADE SUMMARY (n={n}, gated out {gated}) ===")
    print(f"  outcomes: {dict(mix)}")
    print(f"  EV per trade: {sum(nets)/n:.4f}x   total PnL ${sum(r['pnl_usd'] for r in rows):,.2f}")
    print(f"  median hold: {holds[n//2]:.1f} min")
    print(f"  pool init lag after graduation: median {lags[n//2]} blocks "
          f"(~{lags[n//2]*BLOCK_TIME:.1f}s)")
    if slips:
        slips.sort()
        print(f"\n  *** MEASURED SLIPPAGE (the number that gates deployment) ***")
        print(f"      median {slips[len(slips)//2]:.3f}%  "
              f"p75 {slips[int(len(slips)*.75)]:.3f}%  "
              f"p90 {slips[int(len(slips)*.9)]:.3f}%  max {slips[-1]:.3f}%")
        print(f"      breakeven is ~18-20% -> "
              f"{'PASS' if slips[int(len(slips)*.9)] < 10 else 'REVIEW'}")


def main():
    ap = argparse.ArgumentParser(description="Strategy B paper-trade logger (no execution)")
    ap.add_argument("--backfill", type=int, default=0, help="replay this many recent blocks")
    ap.add_argument("--live", action="store_true", help="poll forward from head")
    ap.add_argument("--poll", type=int, default=30, help="live poll seconds")
    ap.add_argument("--stake", type=float, default=25.0)
    ap.add_argument("--eth-usd", type=float, default=ETH_USD)
    ap.add_argument("--tp", type=float, default=5.0, help="take profit multiple")
    ap.add_argument("--stop", type=float, default=0.7, help="stop as fraction of entry")
    ap.add_argument("--latency", type=float, default=60.0,
                    help="seconds between graduation and your fill")
    ap.add_argument("--window-min", type=float, default=960.0, help="max tracking window")
    ap.add_argument("--time-exit-min", type=float, default=960.0)
    ap.add_argument("--fee-bps", type=float, default=700.0, help="round-trip fees")
    ap.add_argument("--max-slippage-pct", type=float, default=2.0,
                    help="entry gate: skip pools where the stake would move price more "
                         "than this. A handful of graduated pools seed with ~1e18 "
                         "liquidity vs a ~3e22 norm, where $25 moves price by orders "
                         "of magnitude. Gating costs ~11%% of signals and removes them.")
    ap.add_argument("--settle-min", type=float, default=60.0,
                    help="live mode: wait this long after a graduation before scoring it, "
                         "so a price path exists. Median hold is 4.3min and 80%% of "
                         "positions resolve inside an hour, so 60 is a sane default.")
    ap.add_argument("--vet", action="store_true", default=True,
                    help="record dev identity + curve vetting features (default on)")
    ap.add_argument("--no-vet", dest="vet", action="store_false")
    ap.add_argument("--summary", action="store_true", help="summarize journal and exit")
    args = ap.parse_args()

    if args.summary:
        summarize()
        return

    reg, curve2dev = load_registry()
    if args.vet:
        print(f"dev registry: {len(reg)} devs, "
              f"{sum(1 for v in reg.values() if v.get('graduations',0)>0)} with a graduation")
    seen = set()
    head = head_block()
    print(f"head block {head}")

    if args.backfill:
        lo = max(0, head - args.backfill)
        print(f"backfilling {lo} -> {head} ({args.backfill} blocks, "
              f"~{args.backfill*BLOCK_TIME/3600:.1f}h)")
        grads = get_logs({"fromBlock": hex(lo), "toBlock": hex(head),
                          "topics": [T_CURVE_COMPLETED]})
        print(f"graduations found: {len(grads)}")
        for i, lg in enumerate(grads, 1):
            r = process_graduation(lg, args, seen, reg, curve2dev)
            if r and "net_mult" in r:
                print(f"  [{i}/{len(grads)}] {r['token'][:10]} {r['outcome']:5} "
                      f"net={r['net_mult']:.3f}x slip={r['est_slippage_pct']}% "
                      f"held={r['minutes_held']}m")
            elif r:
                print(f"  [{i}/{len(grads)}] skip: {r.get('skip')}")
        summarize()
        return

    if args.live:
        cursor = head
        settle_blocks = int(args.settle_min * 60 / BLOCK_TIME)
        pending = []          # (ready_block, log)
        print(f"live from {cursor}; poll {args.poll}s; settling {args.settle_min:.0f}min "
              f"({settle_blocks} blocks) before scoring. Ctrl-C to stop.")
        while True:
            try:
                time.sleep(args.poll)
                h = head_block()
                if h <= cursor:
                    continue
                new_grads = get_logs({"fromBlock": hex(cursor + 1), "toBlock": hex(h),
                                      "topics": [T_CURVE_COMPLETED]})
                for lg in new_grads:
                    gb = int(lg["blockNumber"], 16)
                    pending.append((gb + settle_blocks, lg))
                    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                          f"graduation @{gb} queued ({len(pending)} pending)")
                ready = [x for x in pending if x[0] <= h]
                pending = [x for x in pending if x[0] > h]
                for _, lg in ready:
                    r = process_graduation(lg, args, seen, reg, curve2dev)
                    if r and "net_mult" in r:
                        print(f"  SCORED {r['token'][:10]} dev={str(r.get('dev'))[:10]} "
                              f"priorG={r.get('dev_prior_graduations')} "
                              f"snipers={r.get('n_snipers')} ${r.get('stake_usd')} "
                              f"{r['outcome']} net={r['net_mult']:.3f}x")
                    elif r:
                        print(f"  SKIP {r.get('skip')}")
                cursor = h
            except KeyboardInterrupt:
                print("\nstopping; running summary...")
                summarize()
                return
            except Exception as e:  # noqa: BLE001
                print(f"  loop error (continuing): {e}")
                time.sleep(5)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
