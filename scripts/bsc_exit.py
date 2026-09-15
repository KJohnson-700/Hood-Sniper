#!/usr/bin/env python3
"""
BSC four.meme position manager — laddered exits with a route that is PROVEN to work.

Why this file exists. `exit_manager.py` is hardcoded to Robinhood Chain (4663) and
Pons selectors; `base_exit.py` is hardcoded to Base v4. Before this, the BSC stack
could BUY and could not SELL. Arming a buy in that state builds your own honeypot,
which is the standing rule in this project: never arm a buy on a chain whose exit
path does not exist.

The route is proven by `bsc_selltest.py` against live curves using state overrides,
not assumed. Three things it turned up that this manager is built around:

1. GRANULARITY. four.meme reverts `GW` on any sell whose amount is not a whole
   multiple of 1e9 wei. A buy fills an arbitrary token count, so "sell 100% of my
   balance" reverts ALMOST EVERY TIME -- at exit, when it matters most. Every size
   here goes through `quantize_sell`.

2. THE SELL PULLS. The seller's tokens are moved by the TokenManager, so the token
   must be approved to it. Approving at exit time costs a whole transaction at the
   worst possible moment, so this approves at REGISTRATION time and refuses to call
   a position armed until the allowance is on chain -- the same lesson already
   learned on Robinhood Chain.

3. THERE IS NO QUOTER, so price comes from the venue itself. Each poll bisects the
   sell's own `minQuoteOut` argument to find the largest floor the sell still
   clears; that IS the realisable fill, with fees, creator tax and curve slippage
   already inside it. A modelled mid price is not transactable and would drift.

Exits are laddered (2x/50%, 3x/25%, 5x/25%) rather than all-out at one multiple, so
a runner is not fully clipped at the first rung.

    python3 bsc_exit.py --add 0x<token> --entry-usd 25   # register (+ approve)
    python3 bsc_exit.py --watch                          # monitor, DRY RUN
    python3 bsc_exit.py --watch --arm                    # monitor and actually sell
"""
import argparse
import json
import os
import select
import sys
import termios
import time
import tty
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
os.makedirs(DATA, exist_ok=True)
sys.path.insert(0, HERE)
from bsc_buy import (TOKEN_MANAGER, CHAIN_ID, USDT, build_sell, build_approve,   # noqa: E402
                     quote_of, quote_sell, quantize_sell, min_out_floor,
                     SELL_GRANULARITY, MAX_TRADE_USD, quote_tradeable, quote_label)
from bsc_selltest import (rpc, post_batch, BATCH_STATS, discover,               # noqa: E402
                          bal_slot, alw_slot, WHO)

POSITIONS = os.path.join(DATA, "bsc_positions.json")
JOURNAL = os.path.join(DATA, "bsc_exits.jsonl")
SEL_BAL = "0x70a08231"
SEL_ALW = "0xdd62ed3e"
MAX_UINT = (1 << 256) - 1
BNB_USD = 620.0
BSC_BLOCK_SEC = 0.45

# (multiple, fraction of the ORIGINAL position sold at that rung)
DEF_LADDER = [(2.0, 0.50), (3.0, 0.25), (5.0, 0.25)]
DEF_STOP = 0.65
DEF_TRAIL = None
DEF_TIME_MIN = 240
DEF_SLIP_BPS = 300
POLL_SEC = 6.0


def _now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def load():
    if os.path.exists(POSITIONS):
        try:
            return json.load(open(POSITIONS))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save(p):
    json.dump(p, open(POSITIONS, "w"), indent=1)


def journal(rec):
    rec.setdefault("ts", time.time())
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(rec) + "\n")


def token_balance(token, owner):
    """None means UNREADABLE, never zero. Zero would read as 'position closed'."""
    r = rpc("eth_call", [{"to": token, "data": SEL_BAL + "0" * 24 + owner[2:]}, "latest"])
    res = r.get("result")
    return int(res, 16) if res and res != "0x" else None


def allowance(token, owner):
    r = rpc("eth_call", [{"to": token,
                          "data": SEL_ALW + "0" * 24 + owner[2:] + "0" * 24 + TOKEN_MANAGER[2:]},
                         "latest"])
    res = r.get("result")
    return int(res, 16) if res and res != "0x" else None


def graduated(token):
    """
    True when the curve is gone. four.meme's sell reverts once a token graduates to
    PancakeSwap -- the exit route DISAPPEARS mid-position. `_tokenInfos` stops
    returning a readable quote at that point, which is the cheapest tell. Selling a
    graduated token needs a Pancake route, which is NOT built; this escalates loudly
    instead of pretending it can still exit.
    """
    return quote_of(rpc, token) is None


def realisable(token, amount, quote_dec=18):
    """
    What selling `amount` would actually return, in quote-token wei.

    Not modelled: this bisects the venue's own minQuoteOut. None means COULD NOT
    QUOTE, which callers must treat as "do not act", never as zero -- a zero would
    read as a total loss and trigger a panic exit on an RPC hiccup.

    NOTE this is a size-DEPENDENT number. Do not use it to track price across time
    unless the size is held fixed; see `reference_price`.
    """
    amt = quantize_sell(amount)
    if amt <= 0:
        return None
    return quote_sell(post_batch, token, amt)


def price_override(token, slots, probe_amount):
    """
    State override that makes the probe quotable regardless of what is held.

    BUG THIS FIXES: a fixed-size probe only quotes while the wallet still HOLDS that
    much. After the first rung fills, the balance drops below the probe, every
    min_out reverts, and the price reads None -- so the position would freeze on
    HOLD for the rest of its life, exactly when the remaining rungs matter. Faking
    the probe balance for the read makes the price a property of the CURVE, not of
    the position, which is what a price is supposed to be.

    Read-only: this is an eth_call override, it changes nothing on chain.
    """
    if not slots:
        return None
    b, a = slots.get("bal"), slots.get("alw")
    if b is None or a is None:
        return None
    return {token: {"stateDiff": {bal_slot(b): "0x" + hex(probe_amount * 2)[2:].rjust(64, "0"),
                                  alw_slot(TOKEN_MANAGER, a): "0x" + "f" * 64}},
            WHO: {"balance": "0xde0b6b3a7640000"}}


def reference_price(token, probe_amount, slots=None):
    """
    Per-token realisable price at a FIXED probe size, plus a depth check.

    BUG THIS FIXES: quoting the whole remaining balance each poll makes the quote
    include the impact of dumping all of it. After a rung fills, the smaller leftover
    quotes at a BETTER per-token rate purely because it is smaller -- so the ladder
    could climb its own rungs on a token whose price never moved. Holding the probe
    size constant removes that entirely.

    Returns (price_per_token, depth_ok). depth_ok is False when the curve can no
    longer absorb the probe but can still absorb a tenth of it -- that is the curve
    thinning out underneath the position, which price alone will not show.
    """
    probe = quantize_sell(probe_amount)
    if probe <= 0:
        return None, True
    ov = price_override(token, slots, probe)
    v = quote_sell(post_batch, token, probe, ov)
    if v:
        return v / probe, True
    small = quantize_sell(probe // 10)
    if small > 0:
        v2 = quote_sell(post_batch, token, small, price_override(token, slots, small))
        if v2:
            return v2 / small, False        # thinned: quotes small, not full size
    return None, True                        # could not quote at all -> do not act


# --- registration -----------------------------------------------------------

def add_position(token, entry_usd, addr, key, arm, ladder=None, stop=DEF_STOP,
                 trail=None, time_min=DEF_TIME_MIN, log=print):
    token = token.lower()
    pos = load()
    quote = quote_of(rpc, token)
    ok, why = quote_tradeable(quote)
    if not ok:
        log(f"REFUSING: {why}")
        log("There is no exit-to-money route for that here. Not registering.")
        return False
    bal = token_balance(token, addr)
    if bal is None:
        log("REFUSING: could not read the token balance. Unreadable is not zero.")
        return False
    if bal == 0:
        log("REFUSING: wallet holds none of this token — nothing to manage.")
        return False

    val = realisable(token, bal)
    if val is None:
        log("REFUSING: the sell does not quote — the exit route does not work for this")
        log("token right now. This is exactly the check that must run BEFORE entry.")
        return False

    alw = allowance(token, addr) or 0
    log(f"balance {bal/1e18:,.4f}  realisable {val/1e18:.6f} {quote_label(quote)}"
        f"  allowance {'OK' if alw >= bal else 'MISSING'}")

    if alw < bal:
        log("staging the approval NOW rather than at exit — an approval during a dump")
        log("costs a block, and a block is the whole trade.")
        cd = build_approve(TOKEN_MANAGER)
        sent = _send(token, cd, 0, addr, key, arm, log, why="APPROVE")
        if not sent:
            log("position registered but NOT ARMED until the approval lands.")

    b_slot, a_slot = discover(token, log=lambda *_: None)
    if b_slot is None or a_slot is None:
        log("WARNING: could not locate the token's storage slots. Price will be read")
        log("from the live balance only, so it stops quoting once rungs fill.")
    probe = quantize_sell(bal)
    pos[token] = {"token": token, "quote": quote, "opened_ts": time.time(),
                  "entry_usd": entry_usd, "entry_value": val, "entry_amount": bal,
                  # every later price reading uses THIS size, so the multiple is not
                  # contaminated by the shrinking position's smaller price impact
                  "probe_amount": probe, "entry_price": (val / probe) if probe else None,
                  "slots": {"bal": b_slot, "alw": a_slot},
                  "ladder": ladder or DEF_LADDER, "stop": stop, "trail_pct": trail,
                  "time_stop_min": time_min, "slip_bps": DEF_SLIP_BPS,
                  "rungs_done": [], "peak_mult": 1.0, "sold": 0,
                  "approved": (alw >= bal)}
    save(pos)
    journal({"token": token, "action": "OPEN", "entry_value": val, "amount": bal})
    log(f"registered. entry realisable value {val/1e18:.6f} — the ladder measures against THIS,")
    log("not against a mid price, so 2x means 2x of what you could actually get out.")
    return True


# --- decision ---------------------------------------------------------------

def decide(pos, price, depth_ok=True):
    """
    (action, fraction, reason). Rungs fire once, in order, and every crossed rung
    fires in the SAME poll -- one rung per poll lets a token that gaps 1x -> 5x need
    three polls to exit, and it can round-trip in between.

    `price` is per-token realisable value at a fixed probe size, so it is comparable
    across polls and across partial exits.

    DEPTH IS CHECKED BEFORE PRICE, for the same reason base_exit checks liquidity
    first: if the curve can no longer absorb the position you cannot get out, and
    price will not tell you -- a thinning curve can quote an unchanged price right up
    until the sell reverts.
    """
    entry = pos.get("entry_price")
    if not entry or price is None:
        return "HOLD", 0.0, "no quote — holding rather than guessing"
    if not depth_ok:
        return "SELL_ALL", 1.0, ("DEPTH COLLAPSE — the curve no longer absorbs the "
                                 "position at full size; exiting while it still absorbs a tenth")
    mult = price / entry
    pos["peak_mult"] = max(pos.get("peak_mult", 1.0), mult)
    age = (time.time() - pos["opened_ts"]) / 60

    if mult <= pos.get("stop", DEF_STOP):
        return "SELL_ALL", 1.0, f"stop {mult:.3f}x <= {pos.get('stop', DEF_STOP)}"
    tr = pos.get("trail_pct")
    if tr and pos["peak_mult"] > 1.2 and mult <= pos["peak_mult"] * (1 - tr / 100):
        return "SELL_ALL", 1.0, (f"trailing {mult:.3f}x is {tr:.0f}% off "
                                 f"{pos['peak_mult']:.3f}x peak")
    hit = [(i, m, f) for i, (m, f) in enumerate(pos.get("ladder", DEF_LADDER))
           if i not in pos.get("rungs_done", []) and mult >= m]
    if hit:
        frac = sum(f for _, _, f in hit)
        pos["_firing"] = [i for i, _, _ in hit]
        rungs = ", ".join(f"{m}x" for _, m, _ in hit)
        return "SELL_PART", frac, f"TP {rungs} crossed at {mult:.3f}x — {frac:.0%} out"
    if age >= pos.get("time_stop_min", DEF_TIME_MIN):
        return "SELL_ALL", 1.0, f"time stop {age:.0f}min"
    return "HOLD", 0.0, f"{mult:.3f}x (peak {pos['peak_mult']:.3f}x)"


# --- execution --------------------------------------------------------------

def do_sell(pos, amount, addr, key, arm, log=print, why=""):
    """
    Quantize, quote, floor, simulate, THEN send. Never any of those out of order.

    min_out is derived from a fresh quote rather than left at 0. A zero floor accepts
    any fill including a robbery, which is the exact bug already caught once on Base.
    """
    token = pos["token"]
    amt = quantize_sell(amount)
    if amt <= 0:
        log(f"  size {amount} is under one granularity unit ({SELL_GRANULARITY}) — nothing sellable")
        return None
    if amt != amount:
        log(f"  quantized {amount} -> {amt} (venue rejects non-multiples of 1e9 with 'GW')")

    proceeds = realisable(token, amt)
    if proceeds is None:
        log("  COULD NOT QUOTE — refusing to send a sell with an unknown floor")
        journal({"token": token, "action": "SELL_BLOCKED", "why": why, "error": "no quote"})
        return None
    min_out = min_out_floor(proceeds, pos.get("slip_bps", DEF_SLIP_BPS))
    cd = build_sell(token, amt, min_out)
    log(f"  {amt/1e18:,.4f} tokens -> {proceeds/1e18:.6f} quote · floor {min_out/1e18:.6f} "
        f"({pos.get('slip_bps', DEF_SLIP_BPS)}bps)")

    sim = rpc("eth_call", [{"from": addr, "to": TOKEN_MANAGER, "data": cd}, "latest"])
    if "error" in sim:
        msg = (sim["error"] or {}).get("message", "?")[:120]
        log(f"  SELL SIMULATION REVERTED: {msg}")
        if "GW" in msg:
            log("  -> granularity. Size is not a multiple of 1e9 wei.")
        elif "allowance" in msg.lower() or "transfer" in msg.lower():
            log("  -> the token is not approved to the TokenManager. Approve first.")
        journal({"token": token, "action": "SELL_BLOCKED", "why": why, "error": msg})
        return None
    return _send(TOKEN_MANAGER, cd, 0, addr, key, arm, log, why=why,
                 extra={"token": token, "amount": amt, "min_out": min_out})


def _send(to, cd, value, addr, key, arm, log, why="", extra=None):
    g = rpc("eth_estimateGas", [{"from": addr, "to": to, "value": hex(value), "data": cd}])
    gas = int(g["result"], 16) if "result" in g else 400_000
    log(f"  simulated OK · gas {gas:,}")
    if not arm:
        log("  DRY RUN — not armed.")
        return None
    if not key:
        log("  ARMED but no key set — nothing sent.")
        return None
    import ethsign
    nonce = rpc("eth_getTransactionCount", [addr, "pending"]).get("result")
    gp = rpc("eth_gasPrice", []).get("result")
    tip = int(gp, 16) if gp else 10 ** 9
    blk = (rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("result") or {}
    base = int(blk.get("baseFeePerGas", "0x0"), 16) or 10 ** 9
    tx = {"chainId": CHAIN_ID, "nonce": int(nonce, 16) if nonce else 0,
          "maxPriorityFeePerGas": tip,
          "maxFeePerGas": base * 2 + tip,
          "gas": int(gas * 1.4), "to": to, "value": value, "data": cd}
    r = rpc("eth_sendRawTransaction", [ethsign.sign_1559(tx, key)])
    if "result" in r:
        log(f"  SENT {r['result']}")
        journal(dict({"action": why or "SEND", "tx": r["result"]}, **(extra or {})))
        return r["result"]
    log(f"  SEND FAILED: {(r.get('error') or {}).get('message','?')[:120]}")
    return None


# --- watch loop -------------------------------------------------------------

HELP = "[e] exit all  [h] half out  [t] trail  [s] status  [q] quit"


def watch(addr, key, arm, log=print):
    pos = load()
    if not pos:
        log("no open BSC positions."); return
    log(f"watching {len(pos)} position(s) — {'ARMED' if arm else 'DRY RUN'}   {HELP}")
    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old = termios.tcgetattr(fd) if fd is not None else None
    if fd is not None:
        tty.setcbreak(fd)
    try:
        while True:
            for token in list(pos):
                p = pos[token]
                bal = token_balance(token, addr)
                if bal is None:
                    log(f"{_now()} {token[:10]}… balance unreadable — holding, not guessing")
                    continue
                if bal == 0:
                    log(f"{_now()} {token[:10]}… position closed")
                    journal({"token": token, "action": "CLOSED"})
                    del pos[token]; save(pos); continue
                if graduated(token):
                    log(f"{_now()} *** {token[:10]}… GRADUATED — the curve exit route is GONE.")
                    log("    Selling this needs a PancakeSwap route, which is NOT built.")
                    journal({"token": token, "action": "GRADUATED_NO_ROUTE"})
                    continue
                price, depth_ok = reference_price(token, p.get("probe_amount") or bal,
                                                  p.get("slots"))
                act, frac, why = decide(p, price, depth_ok)
                log(f"{_now()} {token[:10]}… {why}")
                if act == "HOLD":
                    continue
                amt = bal if act == "SELL_ALL" else int(p["entry_amount"] * frac)
                amt = min(amt, bal)
                if do_sell(p, amt, addr, key, arm, log, why=why) and arm:
                    p.setdefault("rungs_done", []).extend(p.pop("_firing", []))
                    save(pos)
            if fd is not None:
                r, _, _ = select.select([sys.stdin], [], [], POLL_SEC)
                if r:
                    c = sys.stdin.read(1).lower()
                    if c == "q":
                        break
                    if c == "s":
                        log(json.dumps(pos, indent=1)[:2000])
                    if c in ("e", "h"):
                        for token in list(pos):
                            b = token_balance(token, addr) or 0
                            do_sell(pos[token], b if c == "e" else b // 2, addr, key,
                                    arm, log, why="manual")
                    if c == "t":
                        for p in pos.values():
                            p["trail_pct"] = {None: 20, 20: 30, 30: 50, 50: None}.get(p.get("trail_pct"), 20)
                        save(pos)
                        log(f"trail -> {[p.get('trail_pct') for p in pos.values()]}")
            else:
                time.sleep(POLL_SEC)
    finally:
        if fd is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if BATCH_STATS["dropped"]:
            log(f"NOTE: {BATCH_STATS['dropped']} quote batches went unanswered "
                f"({BATCH_STATS['truncated']} truncated) — quotes were skipped, not zeroed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--add"); ap.add_argument("--entry-usd", type=float, default=MAX_TRADE_USD)
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--stop", type=float, default=DEF_STOP)
    ap.add_argument("--trail", type=float)
    ap.add_argument("--time-stop", type=float, default=DEF_TIME_MIN)
    a = ap.parse_args()

    # Canonical name is HOOD_SNIPER_PRIVATE_KEY -- what executor.py, exit_manager.py
    # and launch_monitor.py all read. HS_PRIVATE_KEY is accepted as an alias because
    # this file shipped with it, but the canonical name wins. A key set under the
    # wrong name reads as "no key" and the bot stays silently disarmed, which is the
    # worst possible way to discover a naming mismatch.
    key = (os.environ.get("HOOD_SNIPER_PRIVATE_KEY")
           or os.environ.get("HS_PRIVATE_KEY"))
    if not key:
        try:
            sys.path.insert(0, HERE)
            from investigate import env_key
            key = env_key("HOOD_SNIPER_PRIVATE_KEY") or env_key("HS_PRIVATE_KEY")
        except Exception:  # noqa: BLE001
            key = None
    addr = None
    if key:
        from ethsign import priv_to_addr
        addr = priv_to_addr(key)
    if a.arm and not key:
        print("--arm given but HOOD_SNIPER_PRIVATE_KEY is unset. Refusing to pretend.")
        sys.exit(2)
    if not addr:
        addr = os.environ.get("HS_ADDRESS")
    if not addr:
        print("no wallet: set HS_PRIVATE_KEY (to act) or HS_ADDRESS (to watch read-only)")
        sys.exit(2)

    if a.add:
        sys.exit(0 if add_position(a.add, a.entry_usd, addr, key, a.arm,
                                   stop=a.stop, trail=a.trail, time_min=a.time_stop) else 1)
    if a.watch:
        watch(addr, key, a.arm)
    else:
        ap.print_help()
