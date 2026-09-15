#!/usr/bin/env python3
"""
Base v4 position manager — laddered exits, live keys, and an exit route that EXISTS.

Why this file. `exit_manager.py` is hardcoded to Robinhood Chain (4663) and Pons curve
selectors, so before this the Base stack could BUY and could not SELL. That is a
self-inflicted honeypot and it is the reason no Base buy should ever have been armed
without it.

Two things it does that the RH manager did not:
  * LADDERED take-profit. The RH manager sells the WHOLE balance at one multiple
    (`do_sell(curve, balance)`). Here TP is a list of (multiple, percent) rungs --
    default 2x/50%, 3x/25%, 5x/25% -- so a runner is not fully clipped at the first rung.
  * LIVE KEYS while a position is open: [e] exit all now, [h] half out, [t] cycle
    trailing stop, [s] status, [q] quit. Previously an adjustment meant killing the
    process and re-running with different flags.

Prices come from the pool's own sqrtPriceX96 via StateView, oriented by which side the
token sits on -- the orientation bug that corrupted 13.5% of the RH paper trades came
from assuming that.
"""
import argparse, json, os, select, sys, termios, time, tty
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import base_watch as BW
from base_buy import (UNIVERSAL_ROUTER, PERMIT2, STATE_VIEW, USDC, NATIVE,
                      is_native, CHAIN_ID)
from v4sell import build_sell_calldata, build_erc20_approve, build_permit2_approve
import v4sell as V4

DATA = os.path.join(os.path.dirname(HERE), "data")
POSITIONS = os.path.join(DATA, "base_positions.json")
JOURNAL = os.path.join(DATA, "base_exits.jsonl")
SEL_SLOT0 = "0xc815641c"
SEL_BAL = "0x70a08231"

# (multiple, fraction of the ORIGINAL position to sell at that rung)
DEF_LADDER = [(2.0, 0.50), (3.0, 0.25), (5.0, 0.25)]
DEF_STOP = 0.65
DEF_TRAIL = None
DEF_TIME_MIN = 240


def rpc(m, p):      return BW.rpc(m, p)
def head_block():   return BW.head_block()


def load():
    if os.path.exists(POSITIONS):
        try: return json.load(open(POSITIONS))
        except Exception: pass
    return {}


def save(p): json.dump(p, open(POSITIONS, "w"), indent=1)


def journal(rec):
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(rec) + "\n")


PM_ADDR = "0x498581ff718922c3f8e6a244956af099b2652b2b"
T_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
T_MODLIQ = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
BASE_BLOCK_SEC = 2.0

# Ported from exit_manager.state_signals (Robinhood/Pons) to Base/v4. Price stops
# are lagging -- -35% means the dump already happened. These read the position's
# STATE, which moves first. All three are FACTS; none is validated to predict, so
# they raise alerts and only act when --state-exit is passed.
DEF_DEV_DUMP_PCT = 25.0
DEF_SELL_RATIO = 0.75
DEF_QUIET_MIN = 20.0
MIN_DEV_BASELINE = 10 ** 18      # dust baseline would read "shed 100%" on 3 wei


def s256(h):
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def base_signals(pool_id, token, dev, token_is_c1, window_blocks=1800):
    """
    Swap-derived state for one v4 pool. None means COULD NOT READ, never 'fine'.

    Buy/sell is decided by sign, verified on chain: v4 Swap amounts are from the
    POOL's perspective and always carry opposite signs (12/12 sampled), so the
    pool RECEIVES the positive currency. For token_is_c1 a BUY is a0>0 / a1<0.
    Amounts are int128 sign-extended into a 256-bit word -- decoding them as
    unsigned reads a small negative as ~1e77.
    """
    sig = {"dev": dev, "dev_balance": None, "trades_recent": None,
           "sells_recent": None, "sell_ratio": None, "quiet_min": None}
    if dev:
        sig["dev_balance"] = token_balance(token, dev)
    head = rpc("eth_blockNumber", []).get("result")
    if not head:
        return sig
    head = int(head, 16)
    r = rpc("eth_getLogs", [{"fromBlock": hex(max(0, head - window_blocks)),
                             "toBlock": hex(head), "address": PM_ADDR,
                             "topics": [T_SWAP, pool_id]}])
    lgs = r.get("result")
    if lgs is None:
        return sig
    sells = 0
    for lg in lgs:
        d = lg["data"][2:]
        if len(d) < 128:
            continue
        a0, a1 = s256(d[0:64]), s256(d[64:128])
        tok_amt = a1 if token_is_c1 else a0
        if tok_amt > 0:            # pool RECEIVED the token -> someone sold
            sells += 1
    n = len(lgs)
    sig["trades_recent"], sig["sells_recent"] = n, sells
    sig["sell_ratio"] = (sells / n) if n else None
    if n:
        last = max(int(x["blockNumber"], 16) for x in lgs)
        sig["quiet_min"] = (head - last) * BASE_BLOCK_SEC / 60
    else:
        sig["quiet_min"] = window_blocks * BASE_BLOCK_SEC / 60
    return sig


def state_alerts(pos, sig):
    """Comparative: new state against the baseline stored at entry."""
    out = []
    base = pos.get("dev_balance_at_open")
    now = (sig or {}).get("dev_balance")
    if base and now is not None and base >= MIN_DEV_BASELINE:
        shed = 100.0 * (base - now) / base
        if shed >= pos.get("dev_dump_pct", DEF_DEV_DUMP_PCT):
            out.append(f"DEV DUMPING — shed {shed:.0f}% of its opening balance")
    sr = (sig or {}).get("sell_ratio")
    n = (sig or {}).get("trades_recent") or 0
    if sr is not None and n >= 10 and sr >= pos.get("sell_ratio", DEF_SELL_RATIO):
        out.append(f"SELL PRESSURE — {sr:.0%} of the last {n} swaps are sells")
    q = (sig or {}).get("quiet_min")
    if q is not None and q >= pos.get("quiet_min", DEF_QUIET_MIN):
        out.append(f"VOLUME DEAD — no swap for {q:.0f} min")
    return out


POSITION_MANAGER = "0x7c5f5a4bbd8fd63184577525326123b519429bdc"   # verified: poolManager() -> 0x498581ff…


def find_lp_provider(pool_id, lookback=120_000):
    """
    Whoever added the liquidity — the party that can pull it.

    BUG THIS FIXES: the ModifyLiquidity event's `sender` topic is the
    **PositionManager contract** (0x7c5f5a4b…, verified — its poolManager() returns
    the v4 PoolManager), NOT a person. Reading the topic returned a router that by
    construction holds zero tokens, so the DEV DUMPING baseline was always 0 and the
    alert was aimed at the wrong address entirely. The actual provider is the
    transaction's `from`.

    Even with the right address, note v4 liquidity sits in a POOL POSITION rather
    than the wallet, so a wallet-balance dump signal is weaker here than on a
    bonding curve. The liquidity guard (`liq_rug_frac`) is the real rug detector.
    """
    head = rpc("eth_blockNumber", []).get("result")
    if not head:
        return None
    head = int(head, 16)
    b = max(0, head - lookback)
    while b < head:
        to = min(b + 9000, head)
        r = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                 "address": PM_ADDR, "topics": [T_MODLIQ, pool_id]}])
        lgs = r.get("result")
        if lgs is None:          # RPC hiccup -- retry this chunk rather than skip it
            time.sleep(0.4)
            r = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                     "address": PM_ADDR, "topics": [T_MODLIQ, pool_id]}])
            lgs = r.get("result") or []
        if lgs:
            txh = lgs[0].get("transactionHash")
            tx = (rpc("eth_getTransactionByHash", [txh]) or {}).get("result") or {}
            frm = (tx.get("from") or "").lower()
            if frm and frm != POSITION_MANAGER:
                return frm
        b = to
    return None


def pool_liquidity(pool_id):
    """Raw v4 liquidity. None means UNREADABLE, never 'zero'."""
    r = rpc("eth_call", [{"to": STATE_VIEW, "data": "0xfa6793d5" + pool_id[2:]}, "latest"])
    res = r.get("result")
    return int(res, 16) if res and res != "0x" else None


def pool_price(pool_id, token_is_c1):
    """Quote-per-token from sqrtPriceX96, oriented. None when unreadable."""
    r = rpc("eth_call", [{"to": STATE_VIEW, "data": SEL_SLOT0 + pool_id[2:]}, "latest"])
    res = r.get("result")
    if not res or res == "0x":
        return None
    sq = int(res[2:66], 16)
    if sq <= 0:
        return None
    p = (sq / (2 ** 96)) ** 2          # token1 per token0
    # price of the TOKEN in quote units
    return p if not token_is_c1 else (1 / p if p else None)


def token_balance(token, owner):
    r = rpc("eth_call", [{"to": token, "data": SEL_BAL + "0" * 24 + owner[2:]}, "latest"])
    res = r.get("result")
    return int(res, 16) if res and res != "0x" else None


def sell_calldata(pos, amount, min_out):
    return build_sell_calldata(pos["token"], pos["c0"], pos["c1"], pos["fee"],
                               pos["tick_spacing"], pos["hooks"],
                               amount, min_out, int(time.time()) + 300)


def do_sell(pos, amount, addr, key, arm, log=print, why="", limit_price=None):
    """
    Simulate first, always. Never sends a swap that has not been eth_called.

    When a LIMIT fired, minOut is derived from the limit price so the chain
    refuses a fill worse than the limit instead of taking it.
    """
    if amount <= 0:
        log("  nothing to sell"); return None
    min_out = min_out_for_limit("sell", limit_price, amount) if limit_price else 0
    if min_out:
        log(f"  limit enforced on chain: minOut {min_out} (reverts below {limit_price:.3e})")
    cd = sell_calldata(pos, amount, min_out)
    sim = rpc("eth_call", [{"from": addr, "to": UNIVERSAL_ROUTER,
                            "value": "0x0", "data": cd}, "latest"])
    if "error" in sim:
        msg = (sim["error"] or {}).get("message", "?")[:110]
        log(f"  SELL SIMULATION REVERTED: {msg}")
        log("  -> not sending. Check the token->Permit2->Router approvals.")
        journal({"ts": time.time(), "token": pos["token"], "action": "SELL_BLOCKED",
                 "why": why, "error": msg})
        return None
    g = rpc("eth_estimateGas", [{"from": addr, "to": UNIVERSAL_ROUTER,
                                 "value": "0x0", "data": cd}])
    gas = int(g["result"], 16) if "result" in g else 400_000
    log(f"  sell simulated OK · {amount/1e18:,.0f} tokens · gas {gas:,}")
    if not arm:
        log("  DRY RUN — not armed."); return None
    if not key:
        log("  ARMED but no key set."); return None
    import ethsign
    nonce = rpc("eth_getTransactionCount", [addr, "pending"]).get("result")
    tip = rpc("eth_maxPriorityFeePerGas", []).get("result")
    blk = (rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("result") or {}
    base = int(blk.get("baseFeePerGas", "0x0"), 16) or 10 ** 6
    tx = {"chainId": CHAIN_ID, "nonce": int(nonce, 16) if nonce else 0,
          "maxPriorityFeePerGas": int(tip, 16) if tip else 10 ** 6,
          "maxFeePerGas": base * 2 + (int(tip, 16) if tip else 10 ** 6),
          "gas": int(gas * 1.4), "to": UNIVERSAL_ROUTER, "value": 0, "data": cd}
    r = rpc("eth_sendRawTransaction", [ethsign.sign_1559(tx, key)])
    if "result" in r:
        log(f"  SENT {r['result']}")
        journal({"ts": time.time(), "token": pos["token"], "action": "SELL",
                 "why": why, "amount": amount, "tx": r["result"]})
        return r["result"]
    log(f"  SEND FAILED: {(r.get('error') or {}).get('message','?')[:110]}")
    return None


def decide(pos, price, liq=None, sig=None):
    """
    (action, amount_fraction, reason). Ladder rungs fire once, in order.

    LIQUIDITY IS CHECKED BEFORE PRICE. If LP is pulled you cannot sell, and price
    alone will not tell you -- a drained pool can print an unchanged or even
    higher price right up until the swap reverts. Watching only price is how you
    hold a token you can no longer exit.
    """
    base_liq = pos.get("entry_liquidity")
    if liq is not None and base_liq:
        frac = liq / base_liq
        if frac < pos.get("liq_rug_frac", 0.35):
            return "SELL_ALL", 1.0, (f"LIQUIDITY PULLED — {frac:.0%} of entry depth "
                                     f"remains ({liq:,} vs {base_liq:,}) — exiting now")
        if frac < pos.get("liq_warn_frac", 0.70):
            pos["_liq_warn"] = f"depth down to {frac:.0%} of entry"
    alerts = state_alerts(pos, sig)
    if alerts:
        pos["_alerts"] = alerts
        if pos.get("state_exit"):
            return "SELL_ALL", 1.0, "state trigger — " + " · ".join(alerts)
    entry = pos["entry_price"]
    if not entry or not price:
        return "HOLD", 0.0, "no price"
    mult = price / entry
    pos["peak_mult"] = max(pos.get("peak_mult", 1.0), mult)
    age = (time.time() - pos["opened_ts"]) / 60
    if mult <= pos.get("stop", DEF_STOP):
        return "SELL_ALL", 1.0, f"stop {mult:.3f}x <= {pos.get('stop', DEF_STOP)}"
    tr = pos.get("trail_pct")
    if tr and pos["peak_mult"] > 1.2 and mult <= pos["peak_mult"] * (1 - tr / 100):
        return "SELL_ALL", 1.0, (f"trailing {mult:.3f}x is {tr:.0f}% off "
                                 f"{pos['peak_mult']:.3f}x peak")
    # absolute-price limit orders fire before the multiple-based ladder
    # every uncrossed limit at or below the current price fires together -- one
    # per poll lets a fast move blow through several levels unfilled
    hit_l = [(i, lp, f) for i, (lp, f) in enumerate(pos.get("limit_sells", []))
             if i not in pos.get("limits_done", []) and price >= lp]
    if hit_l:
        frac = sum(f for _, _, f in hit_l)
        pos["_limit_firing"] = [i for i, _, _ in hit_l]
        # enforce the LOWEST crossed limit -- the one we actually promised to beat
        pos["_limit_price"] = min(lp for _, lp, _ in hit_l)
        lv = ", ".join(f"{lp:.3e}" for _, lp, _ in hit_l)
        return "SELL_PART", frac, f"LIMIT SELL {frac:.0%} — crossed {lv} at {price:.3e}"

    # Fire EVERY uncrossed rung at or below the current price in one go. Firing
    # them one poll at a time means a token that gaps 1x -> 5x needs three polls
    # to exit, and it can round-trip in between.
    hit = [(i, m, f) for i, (m, f) in enumerate(pos.get("ladder", DEF_LADDER))
           if i not in pos.get("rungs_done", []) and mult >= m]
    if hit:
        frac = sum(f for _, _, f in hit)
        rungs = ", ".join(f"{m}x" for _, m, _ in hit)
        pos["_firing"] = [i for i, _, _ in hit]
        return "SELL_PART", frac, f"TP {rungs} crossed at {mult:.3f}x — {frac:.0%} out"
    if age >= pos.get("time_stop_min", DEF_TIME_MIN):
        return "SELL_ALL", 1.0, f"time stop {age:.0f}min"
    return "HOLD", 0.0, f"{mult:.3f}x (peak {pos['peak_mult']:.3f}x)"


def min_out_for_limit(side, limit_price, amount_in, token_dec=18, quote_dec=18):
    """
    Turn a limit PRICE into an on-chain minOut, so the swap reverts rather than
    filling worse than the limit.

    This is what separates a limit order from a trigger. A trigger fires at your
    price and FILLS at market -- on a fast move you get filled well through your
    level, which is the opposite of the thing you asked for. Encoding the limit
    into minOut makes the chain enforce it: bad fill -> revert, not a bad fill.

    buy : spending `amount_in` quote, you must receive >= amount_in / limit
    sell: selling `amount_in` tokens, you must receive >= amount_in * limit
    """
    if not limit_price or limit_price <= 0 or amount_in <= 0:
        return 0
    # Exact rational math, then floor. Plain float arithmetic put minOut ONE WEI
    # above the true limit (25e18/0.002 -> …001048576), which would revert a fill
    # that actually met the limit. Flooring also errs on the safe side: we never
    # demand more than the stated limit.
    from fractions import Fraction
    lp = Fraction(limit_price).limit_denominator(10 ** 12)
    if side == "buy":
        v = Fraction(amount_in) / lp * Fraction(10 ** token_dec, 10 ** quote_dec)
    else:
        v = Fraction(amount_in) * lp * Fraction(10 ** quote_dec, 10 ** token_dec)
    return int(v)          # floor


def parse_orders(spec, side):
    """'0.0000012:50,0.0000020:50' -> [(price, fraction)] for limit sells/buys."""
    if not spec:
        return []
    out = []
    for part in spec.split(","):
        px, pct = part.split(":")
        out.append((float(px), float(pct) / 100.0))
    return out


def parse_ladder(spec):
    """'2:50,3:25,5:25' -> [(2.0,0.5),(3.0,0.25),(5.0,0.25)]"""
    out = []
    for part in spec.split(","):
        m, pct = part.split(":")
        out.append((float(m), float(pct) / 100.0))
    tot = sum(f for _, f in out)
    if tot > 1.0001:
        raise SystemExit(f"ladder sells {tot:.0%} of the position — must total <= 100%")
    return out


def cmd_add(a, addr):
    """Register a position. base_watch calls this shape after a fill."""
    pools = BW.find_pools(a.token.lower(), head_block() - a.lookback, head_block())
    p = pools.get(a.pool_id)
    if not p:
        raise SystemExit(f"pool {a.pool_id} not found for that token")
    price = pool_price(a.pool_id, p["token_is_c1"])
    bal = token_balance(a.token, addr) if addr else None
    pos = load()
    pos[a.token.lower()] = {
        "token": a.token.lower(), "pool_id": a.pool_id,
        "c0": p["c0"], "c1": p["c1"], "fee": p["fee"],
        "tick_spacing": p["tick_spacing"], "hooks": p["hooks"],
        "token_is_c1": p["token_is_c1"], "quote": p["quote"],
        "entry_price": a.entry_price or price, "opened_ts": time.time(),
        "entry_liquidity": pool_liquidity(a.pool_id),
        "dev": find_lp_provider(a.pool_id),
        "dev_balance_at_open": None, "state_exit": a.state_exit,
        "dev_dump_pct": a.dev_dump_pct, "sell_ratio": a.sell_ratio_alert,
        "quiet_min": a.quiet_min,
        "liq_rug_frac": a.liq_rug_frac, "liq_warn_frac": 0.70,
        "size_tokens": bal or 0, "peak_mult": 1.0, "rungs_done": [],
        "ladder": parse_ladder(a.ladder), "stop": a.stop,
        "limit_sells": parse_orders(a.limit_sell, "sell"), "limits_done": [],
        "trail_pct": a.trail, "time_stop_min": a.time_stop_min, "owner": addr}
    _d = pos[a.token.lower()]["dev"]
    if _d:
        pos[a.token.lower()]["dev_balance_at_open"] = token_balance(a.token, _d)
    save(pos)
    print(f"registered {a.token}\n  pool {a.pool_id[:20]}… fee {p['fee']/10000:.2f}% "
          f"quote {p['quote'][:12]}\n  entry {pos[a.token.lower()]['entry_price']}\n"
          f"  balance {(bal or 0)/1e18:,.0f} tokens")
    print(f"  ladder: " + " · ".join(f"{m}x→{f:.0%}" for m, f in pos[a.token.lower()]["ladder"]))
    _p = pos[a.token.lower()]
    if _p["dev"]:
        print(f"  LP provider {_p['dev']} holds {(_p['dev_balance_at_open'] or 0)/1e18:,.0f} "
              f"(baseline for DEV DUMPING)")
    else:
        print("  LP provider UNREADABLE — dev-dump alert stays OFF rather than assume clear")
    print(f"  state exits: " + ("ARMED" if _p["state_exit"] else "alert-only (--state-exit to act)"))
    if not is_native(p["quote"]):
        print("  NOTE selling needs token→Permit2→Router approved; run --approve")


def cmd_approve(a, addr, key):
    """The two one-time hops the SELL needs. Stage before launch, not at T-0."""
    tok = a.token.lower()
    for to, data, why in ((tok, build_erc20_approve(PERMIT2), "token → Permit2"),
                          (PERMIT2, build_permit2_approve(tok, UNIVERSAL_ROUTER),
                           "Permit2 → UniversalRouter")):
        g = rpc("eth_estimateGas", [{"from": addr, "to": to, "data": data}])
        if "error" in g:
            print(f"  {why}: would revert — {(g['error'] or {}).get('message','?')[:70]}"); continue
        gas = int(g["result"], 16)
        if not a.arm:
            print(f"  {why}: ready (gas {gas:,}) — dry run, add --arm to send"); continue
        import ethsign
        nonce = int(rpc("eth_getTransactionCount", [addr, "pending"]).get("result", "0x0"), 16)
        blk = (rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("result") or {}
        base = int(blk.get("baseFeePerGas", "0x0"), 16) or 10 ** 6
        tx = {"chainId": CHAIN_ID, "nonce": nonce, "maxPriorityFeePerGas": 10 ** 6,
              "maxFeePerGas": base * 2 + 10 ** 6, "gas": int(gas * 1.5),
              "to": to, "value": 0, "data": data}
        r = rpc("eth_sendRawTransaction", [ethsign.sign_1559(tx, key)])
        print(f"  {why}: {'sent ' + r['result'] if 'result' in r else (r.get('error') or {}).get('message','?')[:70]}")
        time.sleep(2)


HELP = "[e] exit all  [h] half out  [t] trailing on/off  [s] status  [q] quit"


def cmd_watch(a, addr, key):
    pos = load()
    if not pos:
        raise SystemExit("no positions — register one with --add")
    print(f"watching {len(pos)} position(s) · {'ARMED' if a.arm else 'DRY RUN'}")
    print(f"  {HELP}\n")
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd); tty.setcbreak(fd); keys = True
    except Exception:
        keys = False; old = None
    try:
        while True:
            for tok, p in list(pos.items()):
                t_poll = time.time()
                price = pool_price(p["pool_id"], p["token_is_c1"])
                liq = pool_liquidity(p["pool_id"])
                sig = base_signals(p["pool_id"], tok, p.get("dev"), p["token_is_c1"])
                bal = token_balance(tok, p["owner"] or addr)
                if bal is not None and bal == 0:
                    print(f"  {tok[:10]}… balance 0 — closing out"); pos.pop(tok); save(pos); continue
                act, frac, why = decide(p, price, liq, sig)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                lat = (time.time() - t_poll) * 1000
                warn = p.pop("_liq_warn", None)
                alerts = p.pop("_alerts", [])
                sr = sig.get("sell_ratio")
                tape = (f" · {sig.get('trades_recent',0)} swaps "
                        f"{sr:.0%} sells" if sr is not None else "")
                print(f"  [{ts}] {tok[:10]}… {act:9s} {why}{tape}"
                      + (f"  ⚠ {warn}" if warn else "")
                      + f"   [{lat:.0f}ms]", flush=True)
                for al in alerts:
                    print(f"           ⚠ {al}"
                          + ("" if p.get("state_exit") else "  (alert only)"), flush=True)
                if act.startswith("SELL") and bal:
                    amt = bal if act == "SELL_ALL" else int(p["size_tokens"] * frac)
                    amt = min(amt, bal)
                    lp = p.pop("_limit_price", None)
                    if do_sell(p, amt, addr, key, a.arm, why=why, limit_price=lp) \
                            and act == "SELL_PART":
                        p["rungs_done"].extend(p.pop("_firing", []))
                        p.setdefault("limits_done", []).extend(p.pop("_limit_firing", []))
                save(pos)
            # live keys
            if keys:
                t0 = time.time()
                while time.time() - t0 < a.poll:
                    if select.select([sys.stdin], [], [], 0.2)[0]:
                        ch = sys.stdin.read(1)
                        if ch == "q":
                            print("  stopped."); return
                        if ch == "s":
                            for tok, p in pos.items():
                                pr = pool_price(p["pool_id"], p["token_is_c1"])
                                m = (pr / p["entry_price"]) if pr and p["entry_price"] else 0
                                print(f"    {tok[:10]}… {m:.3f}x peak {p['peak_mult']:.3f}x "
                                      f"rungs {p['rungs_done']} stop {p['stop']} trail {p['trail_pct']}")
                        if ch in ("e", "h"):
                            for tok, p in list(pos.items()):
                                bal = token_balance(tok, p["owner"] or addr) or 0
                                amt = bal if ch == "e" else bal // 2
                                print(f"    manual {'EXIT ALL' if ch=='e' else 'HALF OUT'} {tok[:10]}…")
                                do_sell(p, amt, addr, key, a.arm, why="manual key")
                        if ch == "t":
                            for p in pos.values():
                                p["trail_pct"] = None if p.get("trail_pct") else 30.0
                                print(f"    trailing -> {p['trail_pct']}")
                            save(pos)
            else:
                time.sleep(a.poll)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if old:
            try: termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception: pass


def main():
    ap = argparse.ArgumentParser(description="Base v4 position manager (dry run unless --arm)")
    ap.add_argument("--add"); ap.add_argument("--token"); ap.add_argument("--pool-id")
    ap.add_argument("--entry-price", type=float)
    ap.add_argument("--limit-sell", default="",
                    help="absolute-price limit sells, 'price:pct,...' — minOut is "
                         "derived from the price so a worse fill REVERTS")
    ap.add_argument("--ladder", default="2:50,3:25,5:25",
                    help="TP rungs as mult:pct — default 2x/50%%, 3x/25%%, 5x/25%%")
    ap.add_argument("--state-exit", action="store_true",
                    help="SELL on dev-dumping / sell-pressure / dead-volume. OFF by "
                         "default: measured facts, but none validated to predict.")
    ap.add_argument("--dev-dump-pct", type=float, default=DEF_DEV_DUMP_PCT)
    ap.add_argument("--sell-ratio-alert", type=float, default=DEF_SELL_RATIO)
    ap.add_argument("--quiet-min", type=float, default=DEF_QUIET_MIN)
    ap.add_argument("--liq-rug-frac", type=float, default=0.35,
                    help="exit immediately if pool depth falls below this fraction of "
                         "entry depth (default 0.35). LP pull = cannot sell.")
    ap.add_argument("--stop", type=float, default=DEF_STOP)
    ap.add_argument("--trail", type=float, default=DEF_TRAIL)
    ap.add_argument("--time-stop-min", type=float, default=DEF_TIME_MIN)
    ap.add_argument("--lookback", type=int, default=60_000)
    ap.add_argument("--watch", action="store_true"); ap.add_argument("--approve", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--arm", action="store_true")
    a = ap.parse_args()
    addr, key = BW.sender()
    if a.list:
        for t, p in load().items():
            print(f"{t}  entry {p['entry_price']}  peak {p['peak_mult']:.3f}x  rungs {p['rungs_done']}")
        return
    if a.approve:  cmd_approve(a, addr, key); return
    if a.pool_id and a.token: cmd_add(a, addr); return
    if a.watch:    cmd_watch(a, addr, key); return
    ap.print_help()


if __name__ == "__main__":
    main()
