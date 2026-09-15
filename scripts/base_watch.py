#!/usr/bin/env python3
"""
Base v4 launch watcher — find the pool that gets REAL liquidity, and refuse the squats.

Why this is not "just snipe the pool". $LAPTOP (0xB0952747…) had **34 pools already
initialized** before launch, every one with zero liquidity, at fees from 0.04% to **99%**.
That is pool squatting: pre-create every fee tier so whoever rushes in routes into a
skim. A 25% fee needs 1.78x round trip just to break even; the 99% pool takes
essentially everything. Whichever pool GMGN happens to report as "biggest" means nothing
while they are all empty.

The only thing that separates the real pool from 33 squats is **which one receives
liquidity**. So this watches every pool of the target token and reacts to the LP event,
not to a pool chosen in advance.

    python3 base_watch.py --token 0xB095... --max-fee-pct 3
"""
import argparse, json, os, sys, time, urllib.request
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base_buy import (build_buy_calldata, approvals_needed, is_native,
                      UNIVERSAL_ROUTER, PERMIT2, USDC)

CHAIN_ID = 8453
# Multiple endpoints, health-tracked. A single RPC is a single point of failure:
# on launch day a rate-limit or a 5xx means going blind at the only moment that
# matters. Measured latencies: drpc 74ms, mainnet.base.org 118ms, 1rpc 161ms,
# publicnode 226ms, meowrpc 223ms, tenderly 246ms. llamarpc/blockpi were dead.
RPCS = ["https://base.drpc.org",
        "https://mainnet.base.org",
        "https://1rpc.io/base",
        "https://base-rpc.publicnode.com",
        "https://base.meowrpc.com",
        "https://gateway.tenderly.co/public/base"]
_RPC_FAILS = {u: 0 for u in RPCS}
RPC_STATS = {"calls": 0, "retries": 0, "ms_total": 0.0}
POOL_MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
STATE_VIEW = "0xa3c0c9b65bad0b08107aa264b0f3db444b867a71"
T_INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
T_MODLIQ = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
T_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
SEL_SLOT0 = "0xc815641c"
ETH_USD = 2450.0
SEL_LIQ = "0xfa6793d5"
MAX_LOG_SPAN = 9000          # public Base RPC silently truncates wider ranges
DYNAMIC_FEE = 0x800000

KNOWN = {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
         "0x4200000000000000000000000000000000000006": "WETH",
         "0x" + "0" * 40: "ETH"}


def rpc(method, params, tries=3):
    """
    Health-ordered failover across endpoints, with latency accounting.

    Endpoints that error get demoted, so a rate-limited provider stops being tried
    first instead of costing a retry on every call. Returns {} only when EVERY
    endpoint failed -- callers treat {} as "unknown", never as "nothing there".
    """
    order = sorted(RPCS, key=lambda u: _RPC_FAILS.get(u, 0))
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    for attempt in range(tries):
        for u in order:
            t0 = time.time()
            try:
                req = urllib.request.Request(
                    u, data=body,
                    headers={"Content-Type": "application/json",
                             "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=12) as r:
                    out = json.load(r)
                RPC_STATS["calls"] += 1
                RPC_STATS["ms_total"] += (time.time() - t0) * 1000
                _RPC_FAILS[u] = max(0, _RPC_FAILS.get(u, 0) - 1)   # heal on success
                return out
            except Exception:  # noqa: BLE001
                _RPC_FAILS[u] = _RPC_FAILS.get(u, 0) + 1
                RPC_STATS["retries"] += 1
        time.sleep(0.3 * (attempt + 1))
    return {}


def rpc_health():
    c = RPC_STATS["calls"] or 1
    return (f"rpc {RPC_STATS['calls']} calls · {RPC_STATS['ms_total']/c:.0f}ms avg · "
            f"{RPC_STATS['retries']} failovers · "
            + " ".join(f"{u.split('//')[1].split('/')[0][:14]}:{n}"
                       for u, n in sorted(_RPC_FAILS.items(), key=lambda kv: kv[1])[:3]))


def head_block():
    return int(rpc("eth_blockNumber", []).get("result", "0x0"), 16)


def call_int(to, data):
    r = rpc("eth_call", [{"to": to, "data": data}, "latest"]).get("result")
    return int(r, 16) if r and r != "0x" else None


def pool_liquidity(pid):
    return call_int(STATE_VIEW, SEL_LIQ + pid[2:])


def find_pools(token, lo, hi, log=print):
    """Every v4 pool holding this token. Chunked -- a wide getLogs returns [] silently."""
    tt = "0x" + "0" * 24 + token[2:].lower()
    out = {}
    b = lo
    while b < hi:
        to = min(b + MAX_LOG_SPAN, hi)
        for slot in (2, 3):
            tp = [T_INIT, None, None, None][:slot + 1]
            tp[slot] = tt
            r = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                     "address": POOL_MANAGER, "topics": tp}])
            for lg in (r.get("result") or []):
                d = lg["data"][2:]
                tsp = int(d[64:128], 16)
                if tsp >= 2 ** 23:
                    tsp -= 2 ** 24
                c0 = "0x" + lg["topics"][2][-40:]
                c1 = "0x" + lg["topics"][3][-40:]
                out[lg["topics"][1]] = {
                    "pool_id": lg["topics"][1], "c0": c0, "c1": c1,
                    "fee": int(d[0:64], 16), "tick_spacing": tsp,
                    "hooks": "0x" + d[128:192][-40:],
                    "block": int(lg["blockNumber"], 16),
                    "quote": c0 if c1.lower() == token.lower() else c1,
                    "token_is_c1": c1.lower() == token.lower()}
        b = to
    return out


def fee_str(f):
    return "DYNAMIC(hook-set)" if f == DYNAMIC_FEE else f"{f/10000:.2f}%"


def pool_depth(pid, token_is_c1):
    """
    (usd_depth, price_impact_for_25, token_price_eth) — None when unreadable.

    A clean fee is NOT sufficient. LAPTOP's first funded pool had a 0.01% fee and
    $1,160 of liquidity behind a $24.5M implied FDV, with zero swaps: cheap to
    trade and completely untradeable. Depth and impact have to gate too.
    """
    r = rpc("eth_call", [{"to": STATE_VIEW, "data": SEL_SLOT0 + pid[2:]}, "latest"]).get("result")
    if not r or r == "0x":
        return None, None, None
    sq = int(r[2:66], 16)
    liq = pool_liquidity(pid) or 0
    if sq <= 0 or liq <= 0:
        return 0.0, None, None
    sp = sq / (2 ** 96)
    # quote is currency0 (native ETH) when the token is currency1
    quote_res = (liq / sp) if token_is_c1 else (liq * sp)
    usd = quote_res / 1e18 * ETH_USD
    dx = 25.0 / ETH_USD * 1e18            # a $25 buy, in quote wei
    try:
        if token_is_c1:                    # token0 (ETH) in
            inv = 1.0 / sp + dx / liq
            impact = (sp * inv) ** 2 - 1
        else:                              # token1 in
            impact = ((sp + dx / liq) / sp) ** 2 - 1
    except Exception:  # noqa: BLE001
        impact = None
    px = (1 / (sp ** 2)) if token_is_c1 else (sp ** 2)
    return usd, (100 * impact if impact is not None else None), px


def verdict(p, max_fee_pct, min_liq_usd=0.0, max_impact_pct=None, pid=None):
    """Refuse anything a swap cannot survive. Dynamic fee = unknowable = refuse."""
    f = p["fee"]
    if f == DYNAMIC_FEE:
        return "REFUSE", "dynamic fee set by a hook — unknowable before the swap"
    pct = f / 10000.0
    if pct > max_fee_pct:
        rt = (1 - pct / 100) ** 2
        return "REFUSE", (f"fee {pct:.2f}%/swap > {max_fee_pct}% — round trip keeps "
                          f"{rt:.1%}, needs {1/rt:.2f}x to break even")
    if int(p["hooks"], 16) != 0:
        return "CAUTION", f"fee {pct:.2f}% but hooked ({p['hooks'][:12]}…) — hook can alter the swap"
    if min_liq_usd or max_impact_pct:
        usd, imp, _px = pool_depth(pid or p["pool_id"], p["token_is_c1"])
        if usd is None:
            return "REFUSE", "depth unreadable — not assuming it is fine"
        if usd < min_liq_usd:
            return "REFUSE", (f"fee {pct:.2f}% but only ${usd:,.0f} of depth "
                              f"(need ${min_liq_usd:,.0f}) — nothing to sell back into")
        if max_impact_pct is not None and imp is not None and imp > max_impact_pct:
            return "REFUSE", (f"fee {pct:.2f}% but a $25 buy moves price {imp:.1f}% "
                              f"(max {max_impact_pct}%)")
        return "OK", (f"fee {pct:.2f}% · depth ${usd:,.0f} · $25 impact "
                      f"{imp:.2f}%" if imp is not None else f"fee {pct:.2f}% · depth ${usd:,.0f}")
    return "OK", f"fee {pct:.2f}%/swap · quote {KNOWN.get(p['quote'].lower(), p['quote'][:10])}"


MAX_TRADE_USD = 25.0          # same hard cap as the RH executor
ETH_USD = 2450.0


def sender():
    key = os.environ.get("BASE_PRIVATE_KEY", "").strip() or \
          os.environ.get("HOOD_SNIPER_PRIVATE_KEY", "").strip()
    if not key:
        return None, None
    try:
        import ethsign
        return ethsign.priv_to_addr(key), key
    except Exception:  # noqa: BLE001
        return None, None


def check_approvals(addr, quote, log=print):
    """Report whether the two Permit2 hops are already staged. Doing them at
    launch time costs two transactions at the worst possible moment."""
    if is_native(quote):
        log("  quote is native ETH — no approval needed")
        return True
    from ethsign import keccak256
    sel_allow = "0x" + keccak256(b"allowance(address,address)").hex()[:8]
    a = call_int(quote, "0xdd62ed3e" + "0" * 24 + addr[2:] + "0" * 24 + PERMIT2[2:])
    log(f"  {quote[:10]}… -> Permit2 allowance: {a if a is not None else 'unreadable'}")
    ok = bool(a and a > 10 ** 12)
    if not ok:
        log("  !! stage this BEFORE launch: approve(Permit2, max) on the quote token")
    return ok


V4_QUOTER = "0x0d5e0f971ed27fbff6c2837bf31316121532048d"
# QuoteExactSingleParams wraps PoolKey as a NESTED struct — the flattened
# signature has a different selector and simply reverts.
SEL_QUOTE_IN = "0xaa9d21cb"      # quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))


def quote_exact_in(p, amount_in, log=print):
    """
    Exact expected output from the v4 Quoter — the basis for the slippage floor.

    Derived from the pool's own math rather than guessed, and returns None when the
    quoter reverts (unfillable size, no liquidity) so the caller can refuse rather
    than fall back to an accept-anything minOut.
    """
    # buying the token: the QUOTE goes in. Quote is currency0 exactly when the
    # token is currency1, so zeroForOne mirrors token_is_c1.
    zero_for_one = bool(p["token_is_c1"])
    def _w(n): return hex(n & ((1 << 256) - 1))[2:].rjust(64, "0")
    def _a(a): return "0" * 24 + a[2:].lower()
    tsp = p["tick_spacing"] & ((1 << 256) - 1) if p["tick_spacing"] >= 0 else \
        (p["tick_spacing"] + (1 << 256))
    data = (SEL_QUOTE_IN + _w(0x20) + _a(p["c0"]) + _a(p["c1"]) + _w(p["fee"])
            + _w(tsp) + _a(p["hooks"]) + _w(1 if zero_for_one else 0)
            # hookData offset is relative to the struct: PoolKey(5) + zeroForOne
            # + exactAmount + the offset word itself = 8 words = 0x100
            + _w(amount_in) + _w(0x100) + _w(0))
    r = rpc("eth_call", [{"to": V4_QUOTER, "data": data}, "latest"])
    res = r.get("result")
    if not res or res == "0x" or len(res) < 66:
        return None
    try:
        return int(res[2:66], 16)
    except Exception:  # noqa: BLE001
        return None


def try_buy(p, usd, addr, key, arm, log=print, limit_price=None, slippage_pct=5.0):
    """
    Build -> simulate -> (only if armed) send. Never sends unsimulated.

    With --limit-buy, minOut is derived from the limit price so the chain REFUSES
    a fill worse than the limit. A trigger that fires at your price but fills at
    market is not a limit order -- on a launch gap that is exactly how you end up
    buying the top.
    """
    quote = p["quote"]
    if is_native(quote):
        amt = int(usd / ETH_USD * 1e18)
    elif quote.lower() == USDC.lower():
        amt = int(usd * 1e6)
    else:
        log(f"  cannot size a buy in {quote[:10]}… — unknown quote decimals"); return
    if usd > MAX_TRADE_USD:
        log(f"  BLOCKED size ${usd} > cap ${MAX_TRADE_USD}"); return
    tok_addr = p["c1"] if p["token_is_c1"] else p["c0"]
    min_out = 0
    # ---- MEV protection on Base is the slippage floor, not a private relay ----
    # Base exposes a pending block (104-176 txs observed), so a tx is visible before
    # confirmation. But the sequencer orders FCFS by arrival with no gas auction, and
    # mainnet-sequencer.base.org is 403 -- there is no private submission to buy.
    # Flashbots Protect does not cover Base. So the defence is minOut: a sandwich that
    # pushes the fill below the floor makes the swap REVERT instead of filling badly.
    # minOut=0 accepts any fill whatsoever, which is the whole vulnerability.
    if not limit_price:
        exp = quote_exact_in(p, amt, log=log)
        if exp:
            min_out = int(exp * (1 - slippage_pct / 100.0))
            log(f"  slippage floor: expect {exp:,} → minOut {min_out:,} (-{slippage_pct}%)")
        else:
            log("  !! no quote available — minOut stays 0, meaning ANY fill is accepted. "
                "Pass --limit-buy to bound it.")
    if limit_price:
        import base_exit as BE
        qdec = 6 if quote.lower() == USDC.lower() else 18
        min_out = BE.min_out_for_limit("buy", limit_price, amt,
                                       token_dec=18, quote_dec=qdec)
        px_now = None
        try:
            px_now = BE.pool_price(p["pool_id"], p["token_is_c1"])
        except Exception:  # noqa: BLE001
            pass
        if px_now and px_now > limit_price:
            log(f"  LIMIT: price {px_now:.3e} is above your limit {limit_price:.3e} "
                f"— holding, not chasing")
            return
        log(f"  limit enforced on chain: minOut {min_out} "
            f"(reverts if worse than {limit_price:.3e})")
    cd = build_buy_calldata(tok_addr, p["c0"], p["c1"], p["fee"], p["tick_spacing"],
                            p["hooks"], amt, min_out, int(time.time()) + 300)
    val = hex(amt) if is_native(quote) else "0x0"
    frm = addr or "0x" + "0" * 39 + "1"
    sim = rpc("eth_call", [{"from": frm, "to": UNIVERSAL_ROUTER,
                            "value": val, "data": cd}, "latest"])
    if "error" in sim:
        log(f"  SIMULATION REVERTED: {(sim['error'] or {}).get('message','?')[:110]}")
        log("  -> not sending. The pool may be unfillable or approvals missing.")
        return
    g = rpc("eth_estimateGas", [{"from": frm, "to": UNIVERSAL_ROUTER,
                                 "value": val, "data": cd}])
    gas = int(g["result"], 16) if "result" in g else None
    log(f"  simulation OK · gas {gas:,}" if gas else "  simulation OK")
    if not arm:
        log("  DRY RUN — not armed. Re-run with --arm to sign."); return
    if not key:
        log("  ARMED but BASE_PRIVATE_KEY is not set — nothing to sign."); return
    log("  >>> sending <<<")
    import ethsign
    n = call_int_raw("eth_getTransactionCount", [addr, "pending"])
    tip = call_int_raw("eth_maxPriorityFeePerGas", []) or 10 ** 6
    blk = (rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("result") or {}
    base = int(blk.get("baseFeePerGas", "0x0"), 16) or 10 ** 6
    tx = {"chainId": CHAIN_ID, "nonce": n or 0, "maxPriorityFeePerGas": tip,
          "maxFeePerGas": base * 2 + tip, "gas": int((gas or 400000) * 1.4),
          "to": UNIVERSAL_ROUTER, "value": amt if is_native(quote) else 0, "data": cd}
    raw = ethsign.sign_1559(tx, key)
    r = rpc("eth_sendRawTransaction", [raw])
    if "result" not in r:
        log(f"  SEND FAILED: {(r.get('error') or {}).get('message','?')[:120]}")
        return
    log(f"  SENT {r['result']}")
    # register the position immediately -- an unmanaged fill has no stop, no TP
    # and no exit. A buy with nothing watching it is the worst state to be in.
    try:
        import base_exit as BE
        from base_buy import STATE_VIEW as _SV
        tokaddr = p["c1"] if p["token_is_c1"] else p["c0"]
        price = BE.pool_price(p["pool_id"], p["token_is_c1"])
        pos = BE.load()
        pos[tokaddr.lower()] = {
            "token": tokaddr.lower(), "pool_id": p["pool_id"],
            "c0": p["c0"], "c1": p["c1"], "fee": p["fee"],
            "tick_spacing": p["tick_spacing"], "hooks": p["hooks"],
            "token_is_c1": p["token_is_c1"], "quote": p["quote"],
            "entry_price": price, "opened_ts": time.time(),
            "size_tokens": 0, "peak_mult": 1.0, "rungs_done": [],
            "ladder": BE.DEF_LADDER, "stop": BE.DEF_STOP, "trail_pct": None,
            "time_stop_min": BE.DEF_TIME_MIN, "owner": addr, "buy_tx": r["result"]}
        BE.save(pos)
        log(f"  position registered · entry {price} · ladder "
            + " ".join(f"{m}x/{f:.0%}" for m, f in BE.DEF_LADDER))
        log("  >>> NOW RUN:  python3 scripts/base_exit.py --watch --arm  <<<")
    except Exception as ex:  # noqa: BLE001
        log(f"  !! POSITION NOT REGISTERED ({str(ex)[:50]}) — register it by hand NOW")


def call_int_raw(method, params):
    r = rpc(method, params).get("result")
    return int(r, 16) if r else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--max-fee-pct", type=float, default=3.0,
                    help="refuse pools charging more than this per swap (default 3)")
    ap.add_argument("--lookback", type=int, default=60_000)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--usd", type=float, default=25.0)
    ap.add_argument("--min-liq-usd", type=float, default=15000.0,
                    help="refuse a pool with less depth than this. A clean fee is not "
                         "enough: LAPTOP's first funded pool was 0.01%% fee with $1,160 "
                         "of depth behind a $24.5M implied FDV.")
    ap.add_argument("--max-impact-pct", type=float, default=3.0,
                    help="refuse if a $25 buy moves price more than this")
    ap.add_argument("--slippage-pct", type=float, default=5.0,
                    help="slippage floor when no --limit-buy is set. This IS the MEV "
                         "protection on Base: no private relay exists, so a sandwich "
                         "that breaches the floor reverts instead of filling.")
    ap.add_argument("--limit-buy", type=float,
                    help="max price to pay per token (quote units). minOut is derived "
                         "from it, so a worse fill REVERTS instead of chasing the launch.")
    ap.add_argument("--arm", action="store_true",
                    help="allow signing after a successful simulation")
    a = ap.parse_args()
    tok = a.token.lower()
    head = head_block()
    addr, key = sender()
    print(f"Base head {head:,} · watching {tok}")
    print(f"sender: {addr or 'NONE (set BASE_PRIVATE_KEY)'} · "
          + ("ARMED — will sign after simulation" if a.arm else "DISARMED — builds and simulates only"))
    pools = find_pools(tok, head - a.lookback, head)
    print(f"pools already initialized: {len(pools)}")
    live = 0
    for p in sorted(pools.values(), key=lambda x: x["fee"]):
        v, why = verdict(p, a.max_fee_pct, a.min_liq_usd, a.max_impact_pct)
        liq = pool_liquidity(p["pool_id"]) or 0
        if liq:
            live += 1
        mark = "***LIQUIDITY***" if liq else "empty"
        print(f"  [{v:7s}] {p['pool_id'][:18]}… {fee_str(p['fee']):>17} "
              f"{KNOWN.get(p['quote'].lower(), p['quote'][:10]):>10}  {mark}")
    if addr:
        quotes = {p["quote"].lower() for p in pools.values()
                  if verdict(p, a.max_fee_pct)[0] in ("OK", "CAUTION")}
        print("\n  approval status for pools that pass the fee gate:")
        for q in quotes:
            check_approvals(addr, q)
    ok = [p for p in pools.values()
          if verdict(p, a.max_fee_pct, a.min_liq_usd, a.max_impact_pct)[0] == "OK"]
    print(f"\n  {len(ok)}/{len(pools)} pools pass the fee gate; {live} hold liquidity")
    if not live:
        print("  -> NOTHING IS TRADEABLE YET. The real pool is whichever gets LP.")
    if a.once:
        return
    print(f"\nwatching for liquidity (poll {a.poll}s). Ctrl-C to stop.\n", flush=True)
    seen = set()
    cur = head
    while True:
        try:
            h = head_block()
            if h > cur:
                r = rpc("eth_getLogs", [{"fromBlock": hex(cur + 1), "toBlock": hex(h),
                                         "address": POOL_MANAGER, "topics": [T_MODLIQ]}])
                for lg in (r.get("result") or []):
                    pid = lg["topics"][1]
                    if pid not in pools or pid in seen:
                        continue
                    seen.add(pid)
                    p = pools[pid]
                    v, why = verdict(p, a.max_fee_pct, a.min_liq_usd, a.max_impact_pct)
                    liq = pool_liquidity(pid) or 0
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    bar = "=" * 66
                    print(f"\n{bar}\n[{ts}] LIQUIDITY ADDED  pool {pid}")
                    print(f"  {fee_str(p['fee']):>17}  quote "
                          f"{KNOWN.get(p['quote'].lower(), p['quote'])}  liquidity {liq:,}")
                    print(f"  VERDICT: {v} — {why}")
                    if v == "OK":
                        print("  >>> THIS IS THE REAL POOL. Swap here, not the squats. <<<")
                        try_buy(p, a.usd, addr, key, a.arm, limit_price=a.limit_buy,
                                slippage_pct=a.slippage_pct)
                    else:
                        print("  >>> DO NOT BUY THIS POOL. <<<")
                    print(bar, flush=True)
                # a brand-new pool can appear after launch starts
                if (h // 300) != (cur // 300):
                    pools.update(find_pools(tok, cur - 2000, h))
                cur = h
            time.sleep(a.poll)
        except KeyboardInterrupt:
            print("\nstopped."); return
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {str(e)[:60]}", flush=True); time.sleep(2)


if __name__ == "__main__":
    main()
