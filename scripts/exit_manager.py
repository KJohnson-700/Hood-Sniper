#!/usr/bin/env python3
"""
Hood Sniper -- exit manager. Watches open positions and fires stop / take-profit.

This is the half that decides whether you keep anything. 80% of outcomes are
stops, and measured latency showed a 15% chance of a >13pp adverse move at 3s
and 26% at 12s -- so exits must fire from a running process, not a keypress.

HOW PRICE IS DETERMINED
-----------------------
Not modelled. Each poll `eth_call`s the real `sell()` for the full balance and
reads `quoteOut`. That is what you would actually receive -- fees, creator tax
and curve slippage already included -- so the trigger is based on realisable
value rather than a mid price that cannot be transacted.

TWO THINGS THAT WILL BITE IF IGNORED
------------------------------------
1. `sell()` uses `safeTransferFrom`, so the curve needs an **approve first**.
   Doing that at exit time adds a whole transaction at the worst possible
   moment. This tool approves at REGISTRATION time and refuses to consider a
   position armed until the allowance is in place.
2. `sell()` reverts once the curve is `graduated()` or `readyToGraduate()`.
   The exit path disappears mid-position. That is detected and escalated
   loudly; selling a graduated token needs a V4 route, which is NOT built.

    python3 exit_manager.py --add   0x<curve> --entry-usd 25   # register + approve
    python3 exit_manager.py --watch                            # monitor (dry run)
    python3 exit_manager.py --watch --arm                      # monitor and sell
"""
import argparse
import itertools
import json
import os
import select
import sys
import termios
import time
import tty
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
from ethsign import priv_to_addr, sign_1559  # noqa: E402
import v4sell as V4  # noqa: E402

T_V4_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"

POSITIONS = os.path.join(DATA, "positions.json")
JOURNAL = os.path.join(DATA, "exits.jsonl")
RPCS = ["https://rpc.mainnet.chain.robinhood.com",
        "https://robinhood-rpc.publicnode.com"]
_rr = itertools.cycle(RPCS)
CHAIN_ID = 4663
ETH_USD = 2450.0
MAX_UINT = (1 << 256) - 1

SEL_SELL = "d04c6983"
SEL_BALANCE = "70a08231"
SEL_APPROVE = "095ea7b3"
SEL_ALLOWANCE = "dd62ed3e"
SEL_TOKEN = "0xfc0c546a"
SEL_GRADUATED = "0xe7c2b772"
SEL_READY = "0xc68360a5"
SEL_SELLABLE = "0x808bcddc"      # sellableTokens()
SEL_LAUNCHSUPPLY = "0x3f7ed6b7"  # launchSupply()
SEL_DEPLOYER = "0xd5f39488"      # deployer()
T_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
T_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
BLOCK_TIME = 0.101

# --- state-based exit signals ------------------------------------------------
# Price stops are lagging: -30% tells you the dump already happened. These read
# the position's STATE instead, which can move first.
#
# VERIFIED READABLE on live curves before being wired in:
#   deployer()/balanceOf   yes -- real values (0% and 1.283% of launchSupply)
#   sellableTokens()       yes -- curve progress
#   curve BUY/SELL logs    yes -- trade counts and sell ratio
#   eth_getBalance(curve)  NO  -- returns 0 for every curve; the curve holds no
#                                 native quote, so a "reserve drop" trigger built
#                                 on it would fire on nothing. NOT USED.
# Realisable value is already covered by quote_out (a simulated sell), which is
# a better liquidity signal than any reserve read.
#
# UNVALIDATED. None of these is yet shown to predict an outcome, so they raise
# ALERTS and do not sell unless --state-exit is passed explicitly. The one time
# a pre-emptive exit idea in this project was actually measured it was a
# downgrade (0.930x vs 1.107x holding), so these stay opt-in until measured.
DEF_DEV_DUMP_PCT = 25.0    # deployer sheds this much of its opening balance
DEF_SELL_RATIO = 0.75      # share of recent trades that are sells
DEF_QUIET_MIN = 20.0       # no trade at all for this many minutes
# A deployer holding dust at open would make any tiny change read as a huge
# percentage ("shed 100%" on 3 wei). Below this the dev-dump alert stays off.
MIN_DEV_BASELINE = 10 ** 18

# Graduation is not a surprise: readyToGraduate() is just sellableTokens()==0,
# and sellableTokens() is a public view, so the distance to the door is
# continuously readable and we CAN leave before it shuts.
#
# But measured against the alternative it is a DOWNGRADE, not an optimisation.
# From the graduation point, holding through returns 1.107x even at ~30s manual
# latency (1.212x automated), while exiting at the door is a flat 0.930x after
# fees. Leaving early costs roughly 19%.
#
# So it is OFF by default (100.0 = never fires) and exists only as a safety net
# for anyone who cannot tolerate the STRANDED case at all. The real fix is a V4
# sell route, not this.
DEF_GRAD_EXIT_PCT = 100.0

# defaults mirror the backtested rule; both are settable per position
DEF_STOP = 0.70          # exit at -30%
DEF_TP = 5.0             # exit at 5x
DEF_TIME_STOP_MIN = 960  # 16h

# Laddered take-profit, ported from base_exit.py. This manager previously sold the
# WHOLE balance at a single multiple, which fully clips a runner at the first rung.
# Fractions are of the ORIGINAL position, and must total <= 100%; a ladder under
# 100% deliberately leaves a moon bag riding on stop/trail/time only.
DEF_LADDER = [(2.0, 0.50), (3.0, 0.25), (5.0, 0.25)]


def parse_ladder(spec):
    out = []
    for part in spec.split(","):
        m, pct = part.split(":")
        out.append((float(m), float(pct) / 100.0))
    tot = sum(f for _, f in out)
    if tot > 1.0001:
        raise SystemExit(f"ladder sells {tot:.0%} of the position — must total <= 100%")
    return out


def rpc(method, params, tries=3, timeout=20):
    for a in range(tries):
        try:
            body = json.dumps({"jsonrpc": "2.0", "method": method,
                               "params": params, "id": 1}).encode()
            req = urllib.request.Request(next(_rr), data=body,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(0.3 * (a + 1))
    return {}


def call(to, data, frm=None):
    p = {"to": to, "data": data}
    if frm:
        p["from"] = frm
    return (rpc("eth_call", [p, "latest"]) or {}).get("result")


def as_int(r):
    try:
        return int(r, 16) if r and r != "0x" else None
    except (TypeError, ValueError):
        return None


def u(n):
    return hex(n)[2:].rjust(64, "0")


def ad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def load():
    if os.path.exists(POSITIONS):
        with open(POSITIONS) as f:
            return json.load(f)
    return {}


def save(p):
    tmp = POSITIONS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2)
    os.replace(tmp, POSITIONS)


def journal(rec):
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(rec) + "\n")


def find_pool_key(token, head=None):
    """
    PoolKey for a graduated token, read straight off its V4 Initialize event.
    Verified: the extracted (fee, tickSpacing, hooks) reproduce the values used
    by a real on-chain swap for the same pool.
    """
    head = head or (as_int((rpc("eth_blockNumber", []) or {}).get("result")) or 0)
    tt = "0x" + "0" * 24 + token[2:]
    for slot in (3, 2):
        tp = [T_V4_INITIALIZE, None, None, None][:slot + 1]
        tp[slot] = tt
        lg = (rpc("eth_getLogs", [{"fromBlock": hex(max(0, head - 2_000_000)),
                                   "toBlock": hex(head),
                                   "address": V4.POOL_MANAGER, "topics": tp}])
              or {}).get("result") or []
        if lg:
            l = sorted(lg, key=lambda x: int(x["blockNumber"], 16))[0]
            d = l["data"][2:]
            return {"pool_id": l["topics"][1],
                    "currency0": "0x" + l["topics"][2][-40:],
                    "currency1": "0x" + l["topics"][3][-40:],
                    "fee": int(d[0:64], 16),
                    "tick_spacing": int(d[64:128], 16),
                    "hooks": "0x" + d[128:192][-40:]}
    return None


def permit2_ready(token, owner):
    """Both hops must exist: token -> Permit2 (ERC20), Permit2 -> router."""
    erc = as_int(call(token, "0x" + SEL_ALLOWANCE + ad(owner) + ad(V4.PERMIT2))) or 0
    r = call(V4.PERMIT2, V4.build_permit2_allowance_call(owner, token, V4.UNIVERSAL_ROUTER))
    p2 = 0
    if r and len(r) >= 66:
        p2 = int(r[2:66], 16)          # first word is the uint160 amount
    return erc, p2


def curve_token(curve):
    r = call(curve, SEL_TOKEN)
    return ("0x" + r[-40:]) if r and len(r) >= 66 else None


def get_logs(params):
    r = rpc("eth_getLogs", [params])
    return r.get("result") if "result" in r else None


def state_signals(curve, token, head=None):
    """
    Facts about the position's state. None means COULD NOT READ, never 'fine'.
    """
    sig = {"dev": None, "dev_balance": None, "trades_recent": None,
           "sells_recent": None, "sell_ratio": None, "quiet_min": None}
    d = call(curve, SEL_DEPLOYER)
    dev = ("0x" + d[-40:]) if d and len(d) >= 66 else None
    sig["dev"] = dev
    if dev:
        sig["dev_balance"] = as_int(call(token, "0x" + SEL_BALANCE + ad(dev)))
    if head is None:
        r = rpc("eth_blockNumber", [])
        head = int(r["result"], 16) if "result" in r else None
    if head:
        win = int(30 * 60 / BLOCK_TIME)          # ~30 min
        lg = get_logs({"fromBlock": hex(max(0, head - win)), "toBlock": hex(head),
                       "address": curve, "topics": [[T_CURVE_BUY, T_CURVE_SELL]]})
        if lg is not None:
            n = len(lg)
            sells = sum(1 for x in lg if x["topics"][0] == T_CURVE_SELL)
            sig["trades_recent"], sig["sells_recent"] = n, sells
            sig["sell_ratio"] = (sells / n) if n else None
            if n:
                last = max(int(x["blockNumber"], 16) for x in lg)
                sig["quiet_min"] = (head - last) * BLOCK_TIME / 60
            else:
                sig["quiet_min"] = 30.0          # nothing in the whole window
    return sig


def exit_state(curve, token, owner):
    """Everything needed to decide, in one place."""
    st = {"curve": curve, "token": token}
    st["graduated"] = bool(as_int(call(curve, SEL_GRADUATED)))
    st["ready"] = bool(as_int(call(curve, SEL_READY)))
    bal = as_int(call(token, "0x" + SEL_BALANCE + ad(owner))) or 0
    st["balance"] = bal
    allow = as_int(call(token, "0x" + SEL_ALLOWANCE + ad(owner) + ad(curve))) or 0
    st["allowance"] = allow
    st["approved"] = allow >= bal and bal > 0
    sellable = as_int(call(curve, SEL_SELLABLE))
    supply = as_int(call(curve, SEL_LAUNCHSUPPLY))
    st["sellable"] = sellable
    st["grad_pct"] = (100.0 * (1 - sellable / supply)
                      if sellable is not None and supply else None)
    st["v4_ready"] = None
    if bal > 0 and (st["graduated"] or st["ready"]):
        erc, p2 = permit2_ready(token, owner)
        st["permit2_erc20"], st["permit2_router"] = erc, p2
        st["v4_ready"] = erc >= bal and p2 >= bal
    st["signals"] = state_signals(curve, token)
    st["quote_out"] = None
    if bal > 0 and not st["graduated"] and not st["ready"]:
        # simulate the real sell -> realisable value, fees and tax included
        sim = call(curve, "0x" + SEL_SELL + u(bal) + u(0) + ad(owner), frm=owner)
        st["quote_out"] = as_int(sim)
    return st


def state_alerts(pos, st):
    """
    Measured state changes worth knowing about. Returns a list of strings.

    Every check is comparative -- new state against the baseline stored when the
    position was opened -- so it reports what CHANGED, not what merely is. A
    signal that cannot be read is skipped, never counted as clear.
    """
    sig = st.get("signals") or {}
    out = []
    base = pos.get("dev_balance_at_open")
    now = sig.get("dev_balance")
    if base and now is not None and base >= MIN_DEV_BASELINE:
        shed = 100.0 * (base - now) / base
        if shed >= pos.get("dev_dump_pct", DEF_DEV_DUMP_PCT):
            out.append(f"DEV DUMPING — deployer shed {shed:.0f}% of its opening "
                       f"balance ({base/1e18:,.0f} -> {now/1e18:,.0f})")
    sr = sig.get("sell_ratio")
    n = sig.get("trades_recent") or 0
    if sr is not None and n >= 10 and sr >= pos.get("sell_ratio", DEF_SELL_RATIO):
        out.append(f"SELL PRESSURE — {sr:.0%} of the last {n} trades are sells")
    q = sig.get("quiet_min")
    if q is not None and q >= pos.get("quiet_min", DEF_QUIET_MIN):
        out.append(f"VOLUME DEAD — no trade for {q:.0f} min")
    return out


def decide(pos, st):
    """Return (action, reason). Pure function of state -- easy to reason about."""
    if st["balance"] == 0:
        return "CLOSED", "zero balance"
    if st["graduated"] or st["ready"]:
        # curve.sell() is gone, but the V4 pool route works -- provided both
        # Permit2 hops are approved (they are one-time, done at registration)
        if st.get("v4_ready"):
            return "V4_SELL", "curve graduated — exiting through the V4 pool"
        return "BLOCKED", ("curve graduated and Permit2 not approved "
                           f"(erc20={st.get('permit2_erc20')}, "
                           f"router={st.get('permit2_router')}) — run --add again to approve")
    if not st["approved"]:
        return "BLOCKED", f"allowance {st['allowance']} < balance {st['balance']} — approve first"
    if st["quote_out"] is None:
        return "HOLD", "could not simulate sell"
    # leave before the curve exit disappears -- a winner that graduates while
    # held cannot be sold through the curve at all
    gp = st.get("grad_pct")
    thr = pos.get("grad_exit_pct", DEF_GRAD_EXIT_PCT)
    if gp is not None and thr < 100.0 and gp >= thr:
        return "SELL", (f"graduation imminent — curve {gp:.2f}% full (≥{thr}%); "
                        "selling while the curve exit still exists")
    # state signals: alert-only unless explicitly armed, because none of them
    # has been validated to predict an outcome yet
    alerts = state_alerts(pos, st)
    st["alerts"] = alerts
    if alerts and pos.get("state_exit"):
        return "SELL", "state trigger — " + " · ".join(alerts)

    entry_wei = int(pos["entry_usd"] / ETH_USD * 1e18)
    mult = st["quote_out"] / entry_wei if entry_wei else 0
    age_min = (time.time() - pos["opened_ts"]) / 60
    # trailing stop: ratchets on the best realisable value seen, never loosens
    peak = max(pos.get("peak_mult", 0.0), mult)
    if peak > pos.get("peak_mult", 0.0):
        pos["peak_mult"] = peak
        st["_peak_updated"] = True
    trail = pos.get("trail_pct")
    if trail and peak > 1.0 and mult <= peak * (1 - trail / 100.0):
        return "SELL", (f"trailing stop — {mult:.3f}x is {trail:.0f}% off the "
                        f"{peak:.3f}x peak")
    if mult <= pos.get("stop", DEF_STOP):
        return "SELL", f"stop hit — realisable {mult:.3f}x ≤ {pos.get('stop', DEF_STOP)}"
    # Fire EVERY uncrossed rung at or below the current price in one go. One rung
    # per poll lets a token that gaps 1x -> 5x need three polls to exit, and it can
    # round-trip in between.
    ladder = pos.get("ladder")
    if ladder:
        hit = [(i, m, f) for i, (m, f) in enumerate(ladder)
               if i not in pos.get("rungs_done", []) and mult >= m]
        if hit:
            frac = sum(f for _, _, f in hit)
            pos["_firing"] = [i for i, _, _ in hit]
            pos["_frac"] = frac          # decide() keeps its (action, reason) contract
            rungs = ", ".join(f"{m}x" for _, m, _ in hit)
            return "SELL_PART", f"TP {rungs} crossed at {mult:.3f}x — {frac:.0%} out"
    if not ladder and mult >= pos.get("tp", DEF_TP):
        return "SELL", f"take profit — realisable {mult:.3f}x ≥ {pos.get('tp', DEF_TP)}"
    if age_min >= pos.get("time_stop_min", DEF_TIME_STOP_MIN):
        return "SELL", f"time stop — {age_min:.0f} min ≥ {pos.get('time_stop_min')}"
    return "HOLD", f"{mult:.3f}x"


def send(tx, key):
    raw = sign_1559(tx, key)
    res = rpc("eth_sendRawTransaction", [raw])
    return res.get("result"), (res.get("error") or {}).get("message")


def base_tx(addr, to, data, gas_default):
    nonce = as_int((rpc("eth_getTransactionCount", [addr, "pending"]) or {}).get("result")) or 0
    tip = as_int((rpc("eth_maxPriorityFeePerGas", []) or {}).get("result")) or 10 ** 8
    blk = (rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("result") or {}
    base = as_int(blk.get("baseFeePerGas")) or 10 ** 8
    est = rpc("eth_estimateGas", [{"from": addr, "to": to, "data": data}])
    gas = as_int(est.get("result")) if "result" in est else None
    err = (est.get("error") or {}).get("message") if "error" in est else None
    return {"chainId": CHAIN_ID, "nonce": nonce, "maxPriorityFeePerGas": tip,
            "maxFeePerGas": int(base * 2 + tip), "gas": int((gas or gas_default) * 1.3),
            "to": to, "value": 0, "data": data}, err


def do_approve(token, curve, addr, key, arm):
    data = "0x" + SEL_APPROVE + ad(curve) + u(MAX_UINT)
    tx, err = base_tx(addr, token, data, 80_000)
    if err:
        return None, f"approve would revert: {err}"
    if not arm or not key:
        return None, "dry run — approve not sent"
    return send(tx, key)


def do_v4_sell(token, bal, addr, key, slippage_bps, arm):
    """Exit a graduated position through the V4 pool via UniversalRouter."""
    pk = find_pool_key(token)
    if not pk:
        return None, "no V4 pool found for this token"
    cd = V4.build_sell_calldata(token, pk["currency0"], pk["currency1"], pk["fee"],
                                pk["tick_spacing"], pk["hooks"], bal, 0,
                                int(time.time()) + 600)
    sim = rpc("eth_call", [{"from": addr, "to": V4.UNIVERSAL_ROUTER, "data": cd}, "latest"])
    if "error" in sim:
        return None, f"V4 sell would revert: {(sim['error'] or {}).get('message')}"
    tx, err = base_tx(addr, V4.UNIVERSAL_ROUTER, cd, 500_000)
    if err:
        return None, f"V4 sell estimate failed: {err}"
    if not arm or not key:
        return None, f"dry run — would V4-sell {bal/1e18:,.0f} tokens via UniversalRouter"
    return send(tx, key)


def do_sell(curve, bal, addr, key, slippage_bps, quote_out, arm):
    min_out = int((quote_out or 0) * (10_000 - slippage_bps) // 10_000)
    data = "0x" + SEL_SELL + u(bal) + u(min_out) + ad(addr)
    tx, err = base_tx(addr, curve, data, 300_000)
    if err:
        return None, f"sell would revert: {err}"
    if not arm or not key:
        return None, f"dry run — would sell {bal/1e18:,.0f} tokens, minOut {min_out/1e18:.6f} ETH"
    return send(tx, key)


def cmd_add(a, key, addr):
    curve = a.add.lower()
    tok = curve_token(curve)
    if not tok:
        print("  cannot resolve token() from that curve")
        return
    pos = load()
    st = exit_state(curve, tok, addr)
    pos[curve] = {"curve": curve, "token": tok, "entry_usd": a.entry_usd,
                  "opened_ts": time.time(),
                  "stop": a.stop if a.stop is not None else DEF_STOP,
                  "tp": a.tp if a.tp is not None else DEF_TP,
                  "time_stop_min": (a.time_stop_min if a.time_stop_min is not None
                                    else DEF_TIME_STOP_MIN),
                  "trail_pct": a.trail,
                  "ladder": parse_ladder(a.ladder) if a.ladder else None,
                  "rungs_done": [], "size_tokens": st.get("balance", 0),
                  "grad_exit_pct": a.grad_exit_pct, "owner": addr,
                  # baseline for the state alerts -- they report CHANGE, so
                  # without this recorded at open there is nothing to compare to
                  "dev_balance_at_open": (st.get("signals") or {}).get("dev_balance"),
                  "dev": (st.get("signals") or {}).get("dev"),
                  "state_exit": a.state_exit,
                  "dev_dump_pct": a.dev_dump_pct,
                  "sell_ratio": a.sell_ratio,
                  "quiet_min": a.quiet_min}
    save(pos)
    print(f"  registered {curve}\n  token {tok}  balance {st['balance']/1e18:,.0f}")
    sg = st.get("signals") or {}
    if sg.get("dev_balance") is not None:
        print(f"  deployer {sg.get('dev')} holds {sg['dev_balance']/1e18:,.0f} "
              f"(baseline for the DEV DUMPING alert)")
    else:
        print("  deployer balance UNREADABLE — the DEV DUMPING alert will stay off "
              "for this position rather than assume it is clear")
    print("  state exits: " + ("ARMED (will sell)" if a.state_exit
                               else "alert-only (pass --state-exit to act on them)"))
    if st["approved"]:
        print("  allowance already sufficient")
        return
    print("  approving the curve so the exit is a SINGLE transaction "
          "(doing this at exit time costs a whole tx at the worst moment)…")
    txh, err = do_approve(tok, curve, addr, key, a.arm)
    print(f"    curve      {'sent ' + txh if txh else err}")
    # the V4 route needs two more one-time hops, or a graduated winner is stuck
    erc, p2 = permit2_ready(tok, addr)
    if erc < st["balance"]:
        tx, e = base_tx(addr, tok, V4.build_erc20_approve(V4.PERMIT2), 80_000)
        if e:
            print(f"    permit2    approve would revert: {e}")
        elif a.arm and key:
            h, er = send(tx, key)
            print(f"    permit2    {'sent ' + h if h else er}")
        else:
            print("    permit2    dry run — token→Permit2 approve not sent")
    else:
        print("    permit2    token→Permit2 already approved")
    if p2 < st["balance"]:
        cd = V4.build_permit2_approve(tok, V4.UNIVERSAL_ROUTER)
        tx, e = base_tx(addr, V4.PERMIT2, cd, 100_000)
        if e:
            print(f"    router     approve would revert: {e}")
        elif a.arm and key:
            h, er = send(tx, key)
            print(f"    router     {'sent ' + h if h else er}")
        else:
            print("    router     dry run — Permit2→router approve not sent")
    else:
        print("    router     Permit2→router already approved")


def cmd_adjust(a, addr):
    """
    Change a LIVE position's rules while it is open.

    The watch loop re-reads positions.json every poll, so an adjustment takes
    effect on the next cycle (default 2s) without restarting anything. That is
    deliberate: you can widen a stop, take profit earlier, or arm a trailing
    exit mid-trade without killing the process that is protecting the position.
    """
    pos = load()
    curve = a.adjust.lower()
    if curve not in pos:
        # allow adjusting by token address too
        for c, p in pos.items():
            if (p.get("token") or "").lower() == curve:
                curve = c
                break
    if curve not in pos:
        print(f"  no open position for {a.adjust}")
        print(f"  open: {list(pos)}")
        return
    p = pos[curve]
    before = {k: p.get(k) for k in ("stop", "tp", "time_stop_min", "trail_pct", "grad_exit_pct")}
    for fld, val in (("stop", a.stop), ("tp", a.tp),
                     ("time_stop_min", a.time_stop_min),
                     ("grad_exit_pct", a.grad_exit_pct)):
        if val is not None:
            p[fld] = val
    if a.trail is not None:
        p["trail_pct"] = a.trail
    save(pos)
    print(f"  adjusted {curve}")
    for k in ("stop", "tp", "time_stop_min", "trail_pct", "grad_exit_pct"):
        if before.get(k) != p.get(k):
            print(f"    {k}: {before.get(k)} -> {p.get(k)}")
    print("  takes effect on the next watch cycle — no restart needed")


KEYS_HELP = "[e] exit all  [h] half out  [t] trailing on/off  [s] status  [q] quit"


def _poll_keys(pos, a, key, addr, budget):
    """
    Live keyboard control while positions are open.

    Before this, changing anything mid-trade meant killing the process and
    re-running with different flags -- at exactly the moment you least want to be
    restarting a manager. Falls back to a plain sleep when stdin is not a tty.
    """
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except Exception:  # noqa: BLE001
        time.sleep(budget)
        return
    try:
        t0 = time.time()
        while time.time() - t0 < budget:
            if not select.select([sys.stdin], [], [], 0.2)[0]:
                continue
            ch = sys.stdin.read(1)
            if ch == "q":
                raise KeyboardInterrupt
            if ch == "s":
                for c, p in pos.items():
                    print(f"    {c[:10]}… peak {p.get('peak_mult',0):.3f}x "
                          f"rungs {p.get('rungs_done',[])} stop {p.get('stop')} "
                          f"trail {p.get('trail_pct')}")
            elif ch in ("e", "h"):
                for c, p in list(pos.items()):
                    st = exit_state(c, p["token"], addr)
                    bal = st["balance"]
                    amt = bal if ch == "e" else bal // 2
                    print(f"    manual {'EXIT ALL' if ch == 'e' else 'HALF OUT'} {c[:10]}…")
                    txh, err = do_sell(c, amt, addr, key, a.slippage_bps,
                                       st.get("quote_out"), a.arm)
                    print(f"      → {txh or err}")
                    journal({"ts_utc": datetime.now(timezone.utc).isoformat(),
                             "curve": c, "action": "MANUAL", "reason": ch,
                             "amount": amt, "tx": txh, "error": err, "armed": a.arm})
            elif ch == "t":
                for p in pos.values():
                    p["trail_pct"] = None if p.get("trail_pct") else 30.0
                print(f"    trailing -> {next(iter(pos.values())).get('trail_pct')}")
                save(pos)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:  # noqa: BLE001
            pass


def cmd_watch(a, key, addr):
    print(f"  watching every {a.poll}s · {'ARMED' if a.arm else 'DRY RUN'} · sender {addr}")
    while True:
        pos = load()
        if not pos:
            print("  no open positions")
            return
        for curve, p in list(pos.items()):
            st = exit_state(curve, p["token"], p.get("owner", addr))
            action, why = decide(p, st)
            if st.pop("_peak_updated", False):
                pos[curve] = p
                save(pos)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            gp = st.get("grad_pct")
            gtag = f" grad {gp:.1f}%" if gp is not None else ""
            line = f"  [{ts}] {curve[:12]} {action:9}{gtag}  {why}"
            # surface state alerts even when the action is HOLD -- that is the
            # entire point: they are meant to fire BEFORE the price stop does
            for al in (st.get("alerts") or []):
                line += f"\n           ⚠ {al}"
                journal({"ts_utc": datetime.now(timezone.utc).isoformat(),
                         "curve": curve, "action": "ALERT", "reason": al,
                         "acted": bool(p.get("state_exit"))})
            if action == "V4_SELL":
                txh, err = do_v4_sell(p["token"], st["balance"], addr, key,
                                      a.slippage_bps, a.arm)
                line += f"  → {txh or err}"
                journal({"ts_utc": datetime.now(timezone.utc).isoformat(),
                         "curve": curve, "action": "V4_SELL", "reason": why,
                         "tx": txh, "error": err, "armed": a.arm,
                         "balance": st["balance"]})
                if txh:
                    pos.pop(curve, None)
                    save(pos)
            elif action == "SELL_PART":
                # fraction is of the ORIGINAL size, capped by what is actually held
                frac = p.pop("_frac", 1.0)
                amt = min(int(p.get("size_tokens", st["balance"]) * frac), st["balance"])
                txh, err = do_sell(curve, amt, addr, key,
                                   a.slippage_bps, st["quote_out"], a.arm)
                line += f"  → {txh or err}"
                journal({"ts_utc": datetime.now(timezone.utc).isoformat(),
                         "curve": curve, "action": "SELL_PART", "reason": why,
                         "amount": amt, "tx": txh, "error": err, "armed": a.arm})
                if txh:
                    p.setdefault("rungs_done", []).extend(p.pop("_firing", []))
                    pos[curve] = p
                    save(pos)
            elif action == "SELL":
                txh, err = do_sell(curve, st["balance"], addr, key,
                                   a.slippage_bps, st["quote_out"], a.arm)
                line += f"  → {txh or err}"
                journal({"ts_utc": datetime.now(timezone.utc).isoformat(),
                         "curve": curve, "action": "SELL", "reason": why,
                         "tx": txh, "error": err, "armed": a.arm,
                         "balance": st["balance"], "quote_out": st["quote_out"]})
                if txh:
                    pos.pop(curve, None)
                    save(pos)
            elif action in ("CLOSED",):
                pos.pop(curve, None)
                save(pos)
            elif action == "STRANDED":
                line = f"  [{ts}] {curve[:12]} \033[91mSTRANDED\033[0m {why}"
            print(line, flush=True)
        _poll_keys(pos, a, key, addr, a.poll)


def main():
    ap = argparse.ArgumentParser(description="Hood Sniper exit manager")
    ap.add_argument("--add", help="register a position by curve address")
    ap.add_argument("--entry-usd", type=float, default=25.0)
    ap.add_argument("--stop", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--time-stop-min", type=float, default=None)
    ap.add_argument("--grad-exit-pct", type=float, default=DEF_GRAD_EXIT_PCT,
                    help="sell when the curve is this %%%% full. OFF by default (100). "
                         "Setting e.g. 97 guarantees you are never STRANDED, but costs "
                         "~19%%%% versus holding through graduation — measured, not assumed.")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--slippage-bps", type=int, default=500)
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--adjust", help="modify a LIVE position (curve or token address)")
    ap.add_argument("--ladder", default="2:50,3:25,5:25",
                    help="laddered TP as mult:pct,... (default 2x/50%%, 3x/25%%, 5x/25%%). "
                         "Pass '' to fall back to a single full-clip --tp.")
    ap.add_argument("--state-exit", action="store_true",
                    help="SELL on a state trigger (dev dumping / sell pressure / "
                         "dead volume). Off by default: these are measured facts "
                         "but none is yet validated to predict an outcome.")
    ap.add_argument("--dev-dump-pct", type=float, default=DEF_DEV_DUMP_PCT,
                    help="alert when the deployer sheds this %% of its opening balance")
    ap.add_argument("--sell-ratio", type=float, default=DEF_SELL_RATIO,
                    help="alert when this share of the last 30min of trades are sells")
    ap.add_argument("--quiet-min", type=float, default=DEF_QUIET_MIN,
                    help="alert after this many minutes with no trade at all")
    ap.add_argument("--trail", type=float,
                    help="trailing stop %%: exit if price falls this %% from its peak")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    key = os.environ.get("HOOD_SNIPER_PRIVATE_KEY", "").strip()
    addr = priv_to_addr(key) if key else None
    if not addr:
        print("  no HOOD_SNIPER_PRIVATE_KEY — read-only mode (cannot approve or sell)")
        addr = "0x" + "0" * 40

    if a.list:
        for c, p in load().items():
            st = exit_state(c, p["token"], p.get("owner", addr))
            print(f"  {c} {p['token'][:12]} bal {st['balance']/1e18:,.0f} "
                  f"approved={st['approved']} {decide(p, st)}")
        return
    if a.adjust:
        cmd_adjust(a, addr)
        return
    if a.add:
        cmd_add(a, key, addr)
        return
    if a.watch:
        cmd_watch(a, key, addr)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
