#!/usr/bin/env python3
"""
Prove the four.meme SELL path on BSC without owning the token or spending anything.

Same reason base_selltest.py exists: the BSC buy was proven weeks before anything
could be shown to SELL, and "we'll find out at exit" is the worst possible time to
discover a bad encoder. A plain simulation from an empty address always reverts (no
balance, no approval), which proves nothing about the encoder. This uses eth_call
STATE OVERRIDES to fake exactly the two things a real position would have, then runs
the REAL sell calldata against a REAL live curve.

GROUND TRUTH -- decoded from live sell 0xcc454cd7…6878 (block ~120829900):

    SELL 0x3e11741f (token, amountTokens, minQuoteOut)

      user ──181,769.102168321 TOKEN──▶ TokenManager     <- PULL, needs approval
      TM   ──6.1451 USDT───────────────▶ user
      TM   ──0.0590 USDT───────────────▶ 0x55d571b7…     <- fee
      TM   ──0.0031 USDT───────────────▶ 0x2b6e6e4d…     <- fee
                                          total fee ~1.0%

The pull is the whole point: unlike the BNB-quoted buy (which pushes value), the sell
transfers the memecoin FROM the seller, so it reverts until the token is approved to
the TokenManager. That approval is a separate transaction and it must be staged BEFORE
the exit is needed, not at exit time -- staging it during a dump costs a block.

Storage slots are discovered by probing, never assumed. four.meme pre-graduation tokens
are EIP-1167 clones of one implementation, so the layout is identical across them and a
single discovery generalises -- but the probe still runs per token, because a graduated
token is a different 7646-byte contract with a different layout.

    python3 bsc_selltest.py                        # auto-pick live curves from recent blocks
    python3 bsc_selltest.py --token 0x…            # prove one specific token
    python3 bsc_selltest.py --token 0x… --amount 1000000000000000000000
"""
import argparse
import itertools
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ethsign import keccak256                                    # noqa: E402
from bsc_buy import (TOKEN_MANAGER, SEL_SELL, build_sell, quote_of, build_approve,  # noqa: E402
                     USDT, quantize_sell, SELL_GRANULARITY, quote_sell,
                     min_out_floor, quote_label, quote_tradeable)

WHO = "0x1111111111111111111111111111111111111111"
SEL_BAL, SEL_ALW, SEL_DEC, SEL_SYM = "0x70a08231", "0xdd62ed3e", "0x313ce567", "0x95d89b41"
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Not every BSC endpoint implements the third eth_call parameter. The ones that reject
# it fail LOUDLY here rather than silently ignoring the override and reporting a bogus
# revert, so the prover retries elsewhere instead of recording a false negative.
URLS = itertools.cycle(["https://bsc-dataseed.bnbchain.org",
                        "https://bsc-rpc.publicnode.com",
                        "https://binance.llamarpc.com",
                        "https://bsc-dataseed1.binance.org"])
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}


def post_batch(reqs, want=None, tries=6):
    """
    JSON-RPC batch POST. A response shorter than the request is a SILENT TRUNCATION,
    not an answer -- some BSC endpoints cap batch size and reply with one element.
    Reject those and retry elsewhere rather than passing a partial list upward.
    """
    want = want if want is not None else len(reqs)
    for _ in range(tries):
        try:
            req = urllib.request.Request(next(URLS), data=json.dumps(reqs).encode(), headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.load(r)
            if isinstance(out, list) and len(out) == want:
                return out
            BATCH_STATS["truncated"] += 1
        except Exception:  # noqa: BLE001
            time.sleep(0.35)
    BATCH_STATS["dropped"] += 1
    return []


BATCH_STATS = {"truncated": 0, "dropped": 0}


def rpc(method, params, tries=6):
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                next(URLS),
                data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params}).encode(),
                headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(0.35)
    return {}


def _slot(key_addr, slot):
    return keccak256(bytes.fromhex(key_addr[2:].rjust(64, "0") + hex(slot)[2:].rjust(64, "0")))


def bal_slot(slot):
    return "0x" + _slot(WHO, slot).hex()


def alw_slot(spender, slot):
    return "0x" + keccak256(bytes.fromhex(spender[2:].rjust(64, "0") + _slot(WHO, slot).hex())).hex()


def _call(to, data, ov=None):
    p = [{"from": WHO, "to": to, "data": data}, "latest"]
    if ov:
        p.append(ov)
    return rpc("eth_call", p)


def discover(token, log=print):
    """
    Probe for balance + allowance slots. Assuming them silently produces false passes:
    a wrong slot means the override does nothing, the sell reverts for lack of balance,
    and the encoder gets blamed for a bug it does not have.
    """
    MAGIC = 12345 * 10 ** 18
    bal = alw = None
    for s in range(12):
        ov = {token: {"stateDiff": {bal_slot(s): "0x" + hex(MAGIC)[2:].rjust(64, "0")}}}
        r = _call(token, SEL_BAL + "0" * 24 + WHO[2:], ov)
        if r.get("result") and int(r["result"], 16) == MAGIC:
            bal = s
            break
        time.sleep(0.1)
    for s in range(12):
        ov = {token: {"stateDiff": {alw_slot(TOKEN_MANAGER, s): "0x" + "f" * 64}}}
        r = _call(token, SEL_ALW + "0" * 24 + WHO[2:] + "0" * 24 + TOKEN_MANAGER[2:], ov)
        if r.get("result") and int(r["result"], 16) > 10 ** 30:
            alw = s
            break
        time.sleep(0.1)
    log(f"  slots — balance {bal} · allowance {alw}")
    return bal, alw


def discover_granularity(token, bal, alw, log=print):
    """
    Find the smallest 10**k the venue accepts as a sell increment. Returns
    (granularity, max_round_size) -- both None if nothing sells at all.

    BUG THIS FIXES: the first version probed one fixed ugly amount (~28k tokens).
    On a near-empty curve every rounding of it underflows the reserve, and the probe
    reported "no granularity" for a token that sells fine at small size. Size failure
    and granularity failure are different things and must not collapse into one
    verdict -- that is a false negative on a working exit route.

    So: first scale DOWN to a round size the curve can actually absorb, then find the
    granularity by adding 1, 10, 100 ... to that size. Because the base is a power of
    ten, base+10**k is a multiple of 10**k and of nothing larger, which isolates the
    increment regardless of how deep the curve is.
    """
    base = None
    for e in range(27, 8, -1):
        if _sell_ok(token, bal, alw, 10 ** e):
            base = 10 ** e
            break
        time.sleep(0.06)
    if base is None:
        log("  granularity — nothing sells at any size; no exit route here")
        return None, None
    for k in range(0, 19):
        if _sell_ok(token, bal, alw, base + 10 ** k):
            g = 10 ** k
            note = ("  (matches compiled SELL_GRANULARITY)" if g == SELL_GRANULARITY
                    else f"  *** DIFFERS from compiled {SELL_GRANULARITY} ***")
            log(f"  granularity — multiples of 1e{k}{note}"
                f"   ·  curve absorbs up to {base/1e18:,.0f} tokens/sell")
            return g, base
        time.sleep(0.06)
    log("  granularity — no increment accepted — INCONCLUSIVE")
    return None, base


def _sell_ok(token, bal, alw, amount):
    ov = {token: {"stateDiff": {bal_slot(bal): "0x" + hex(10 ** 30)[2:].rjust(64, "0"),
                                alw_slot(TOKEN_MANAGER, alw): "0x" + "f" * 64}},
          WHO: {"balance": "0xde0b6b3a7640000"}}
    cd = "0x" + SEL_SELL + "0" * 24 + token[2:] + hex(amount)[2:].rjust(64, "0") + "0" * 64
    return "result" in _call(TOKEN_MANAGER, cd, ov)


def _sym(token):
    r = _call(token, SEL_SYM)
    res = r.get("result") or ""
    if len(res) > 130:
        try:
            n = int(res[66:130], 16)
            return bytes.fromhex(res[130:130 + n * 2]).decode(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    return "?"


def prove(token, amount=None, log=print):
    """
    Returns (ok, detail). ok is True ONLY if the sell simulation executed AND the
    decoded proceeds are non-zero -- a call that succeeds while returning nothing is
    not an exit, it is a donation.
    """
    token = token.lower()
    sym = _sym(token)
    quote = quote_of(rpc, token)
    tradeable, qwhy = quote_tradeable(quote)
    log(f"  {sym:<12} {token}  quote={quote_label(quote)}"
        + ("" if tradeable else "   <-- NOT TRADEABLE: " + qwhy))
    if not quote:
        return False, "quote unreadable (token not on the curve, or graduated)"

    bal, alw = discover(token, log)
    if bal is None or alw is None:
        return False, "could not locate slots — INCONCLUSIVE (not a pass)"

    gran, max_size = discover_granularity(token, bal, alw, log)
    if gran is None:
        return False, "no accepted sell size — INCONCLUSIVE"

    # Size the position off the curve's own supply so the amount is plausible for the
    # pool rather than an arbitrary constant that would blow through it. This is
    # ALSO the realistic case for the granularity trap: pool//10_000 is exactly the
    # kind of ugly number a real balance is, and it reverts unless quantized.
    if amount is None:
        held = _call(token, SEL_BAL + "0" * 24 + TOKEN_MANAGER[2:])
        pool = int(held.get("result", "0x0"), 16) if held.get("result") else 0
        raw = max(pool // 10_000, 10 ** 18)
        # never probe deeper than the curve demonstrably absorbs, or the proof fails
        # for lack of depth and gets misread as a broken encoder
        raw = min(raw, max_size)
        amount = quantize_sell(raw, gran)
        if raw != amount:
            log(f"  quantized {raw} -> {amount}  (dust left behind: {raw - amount} wei)")

    ov = {token: {"stateDiff": {bal_slot(bal): "0x" + hex(amount)[2:].rjust(64, "0"),
                                alw_slot(TOKEN_MANAGER, alw): "0x" + "f" * 64}},
          WHO: {"balance": "0xde0b6b3a7640000"}}

    # 1) Does it revert without the approval? If it does NOT, the approval is not
    #    actually required and the staging step is dead weight -- worth knowing.
    ov_noalw = {token: {"stateDiff": {bal_slot(bal): "0x" + hex(amount)[2:].rjust(64, "0")}},
                WHO: {"balance": "0xde0b6b3a7640000"}}
    r0 = _call(TOKEN_MANAGER, build_sell(token, amount, 0), ov_noalw)
    needs_approval = "result" not in r0
    log(f"  approval required: {needs_approval}"
        + ("" if needs_approval else "  <-- unexpected, sell pulls without allowance"))

    # 2) The real proof, with balance + allowance faked.
    cd = build_sell(token, amount, 0)
    r = _call(TOKEN_MANAGER, cd, ov)
    if "result" not in r:
        msg = (r.get("error") or {}).get("message", "?")[:160]
        log(f"  REVERTED: {msg}")
        return False, msg

    # 3) A succeeding call is not yet an exit -- it has to return VALUE. No public BSC
    #    node exposes debug_traceCall, so read the proceeds off the venue itself by
    #    bisecting min_out: the largest floor the sell still clears is the fill.
    t0 = time.time()
    proceeds = quote_sell(post_batch, token, amount, ov, gran)
    dt = time.time() - t0
    if proceeds is None:
        log("  call succeeded but NO min_out brackets — returns nothing. Not an exit.")
        return False, "zero proceeds"
    qn = quote_label(quote)
    log(f"  quoted in {dt:.1f}s — min_out floor @3% = {min_out_floor(proceeds)/1e18:.6f} {qn}")
    log(f"  *** SELL PROVEN — {amount/1e18:,.4f} {sym} -> {proceeds/1e18:.6f} {qn} ***")
    return True, f"{proceeds/1e18:.6f} {qn}"


def _proceeds(token, quote, cd, ov):
    """Read the quote-token Transfer to WHO out of a traced call. None = couldn't check."""
    r = rpc("debug_traceCall",
            [{"from": WHO, "to": TOKEN_MANAGER, "data": cd},
             "latest", {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "stateOverrides": ov}],
            tries=3)
    if "result" not in r:
        return None
    total = 0
    stack = [r["result"]]
    while stack:
        n = stack.pop()
        for lg in n.get("logs") or []:
            tp = lg.get("topics") or []
            if (len(tp) == 3 and tp[0] == T_TRANSFER
                    and (lg.get("address") or "").lower() == quote.lower()
                    and tp[2][26:].lower() == WHO[2:].lower()):
                total += int(lg.get("data") or "0x0", 16)
        stack.extend(n.get("calls") or [])
    return total


def recent_curve_tokens(n=6, blocks=200, log=print):
    """Pick tokens with LIVE curve activity — a token nobody trades proves nothing."""
    head = int(rpc("eth_blockNumber", []).get("result", "0x0"), 16)
    seen, out = set(), []
    for i in range(blocks):
        b = rpc("eth_getBlockByNumber", [hex(head - i), True]).get("result")
        if not b:
            continue
        for t in b["transactions"]:
            if (t.get("to") or "").lower() != TOKEN_MANAGER:
                continue
            d = t.get("input", "0x")
            if d[2:10] not in ("87f27655", "7f79f6df", SEL_SELL[2:]):
                continue
            tok = "0x" + d[10:74][-40:]
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
                if len(out) >= n:
                    log(f"  sampled {len(out)} live curve tokens from {i+1} blocks")
                    return out
    log(f"  sampled {len(out)} live curve tokens from {blocks} blocks")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--token")
    ap.add_argument("--amount", type=int)
    ap.add_argument("--n", type=int, default=5)
    a = ap.parse_args()

    toks = [a.token] if a.token else recent_curve_tokens(a.n)
    if not toks:
        print("no live curve tokens sampled — cannot prove anything"); sys.exit(2)

    print(f"\nfour.meme SELL proof — TokenManager {TOKEN_MANAGER}\n")
    ok_n = 0
    for t in toks:
        ok, detail = prove(t, a.amount)
        ok_n += ok
        print()
    print(f"RESULT: {ok_n}/{len(toks)} sells proven")
    print("\nSTAGING NOTE: each token must be approved to the TokenManager before its")
    print("exit works. Approve calldata (token -> spender TokenManager):")
    print(f"  {build_approve(TOKEN_MANAGER)[:74]}…")
    sys.exit(0 if ok_n else 1)
