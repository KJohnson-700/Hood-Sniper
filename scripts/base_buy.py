#!/usr/bin/env python3
"""
Base v4 BUY route — mirror of v4sell.py, whose params[0] layout was verified
byte-for-byte against a live transaction.

A buy is the sell with the sides swapped: the QUOTE goes in, the TOKEN comes out.
zeroForOne follows from which side the quote sits on, and SETTLE_ALL/TAKE_ALL swap
roles accordingly. Getting that backwards silently builds a sell.

Native ETH pools (currency0 == 0x0) pay with tx.value; ERC-20 quotes (USDC on most
LAPTOP pools) need the two Permit2 hops approved FIRST -- doing them at launch time
costs two transactions at the worst possible moment.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from v4sell import (w, wa, dyn, build_swap_params, CMD_V4_SWAP, SEL_EXECUTE,
                    ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL,
                    build_erc20_approve, build_permit2_approve, MAX_UINT)

CHAIN_ID = 8453
POOL_MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
UNIVERSAL_ROUTER = "0x6ff5693b99212da76ad316178a184ab56d299b43"   # verified from live swaps
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
STATE_VIEW = "0xa3c0c9b65bad0b08107aa264b0f3db444b867a71"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
NATIVE = "0x" + "0" * 40


def build_buy_calldata(token, currency0, currency1, fee, tick_spacing, hooks,
                       amount_in, min_out, deadline):
    """Spend `amount_in` of the pool's quote to receive `token`."""
    token = token.lower()
    token_is_c0 = token == currency0.lower()
    quote = currency1 if token_is_c0 else currency0
    # the QUOTE goes in: if the quote is currency0 we are swapping 0 -> 1
    zero_for_one = not token_is_c0

    p0 = build_swap_params(currency0, currency1, fee, tick_spacing, hooks,
                           zero_for_one, amount_in, min_out)
    p1 = wa(quote) + w(amount_in)      # SETTLE_ALL: pay the quote in
    p2 = wa(token) + w(min_out)        # TAKE_ALL:  receive the token out

    actions = ACT_SWAP_EXACT_IN_SINGLE + ACT_SETTLE_ALL + ACT_TAKE_ALL
    a_enc = dyn(actions)
    params = [p0, p1, p2]
    arr = w(len(params))
    offs, bodies, cur = [], [], 32 * len(params)
    for p in params:
        offs.append(w(cur)); b = dyn(p); bodies.append(b); cur += len(b) // 2
    arr += "".join(offs) + "".join(bodies)
    params_off = 0x40 + len(a_enc) // 2
    v4_input = w(0x40) + w(params_off) + a_enc + arr
    cmds = dyn(CMD_V4_SWAP)
    inputs_arr = w(1) + w(0x20) + dyn(v4_input)
    cmd_off = 0x60
    inp_off = cmd_off + len(cmds) // 2
    return "0x" + SEL_EXECUTE + w(cmd_off) + w(inp_off) + w(deadline) + cmds + inputs_arr


def is_native(currency):
    return int(currency, 16) == 0


def approvals_needed(quote):
    """The two one-time hops an ERC-20 quote needs before any swap can land."""
    if is_native(quote):
        return []
    return [{"to": quote, "data": build_erc20_approve(PERMIT2),
             "why": "quote -> Permit2 (ERC-20 approve)"},
            {"to": PERMIT2, "data": build_permit2_approve(quote, UNIVERSAL_ROUTER),
             "why": "Permit2 -> UniversalRouter"}]


if __name__ == "__main__":
    cd = build_buy_calldata(
        token="0xb095274743941e953c746f9c228da9c18bb6ec29",
        currency0=USDC, currency1="0xb095274743941e953c746f9c228da9c18bb6ec29",
        fee=3000, tick_spacing=60, hooks=NATIVE,
        amount_in=25_000_000, min_out=1, deadline=99999999999)
    print(f"sample buy calldata: {len(cd)//2-1} bytes")
    print(f"  selector {cd[:10]}  (execute(bytes,bytes[],uint256))")
    for a in approvals_needed(USDC):
        print(f"  approval: {a['why']}  -> {a['to']}")
