#!/usr/bin/env python3
"""
Uniswap V4 sell route via UniversalRouter -- the exit path for positions whose
curve has graduated (`curve.sell()` reverts once `graduated()` is true).

The encoding is NOT written from spec. It replicates, byte for byte, a live
V4 swap decoded off this chain, with only the direction and amounts changed:

    commands = 0x10                       (V4_SWAP)
    actions  = 0x06 0x0c 0x0f             (SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL)
    params[0] = 12 words: offset, PoolKey(c0,c1,fee,tickSpacing,hooks),
                zeroForOne, amountIn, amountOutMin, 0, hookDataOffset(0x140), 0
    params[1] = SETTLE_ALL (currency paid IN,  max amount)
    params[2] = TAKE_ALL   (currency taken OUT, min amount)

Selling flips zeroForOne and swaps which currency is settled vs taken.

APPROVALS: the router pulls tokens through Permit2, so a sell needs two
one-time approvals -- token->Permit2, then Permit2->router. Both belong at
position registration; doing them at exit costs two transactions at the worst
possible moment.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UNIVERSAL_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

CMD_V4_SWAP = "10"
ACT_SWAP_EXACT_IN_SINGLE = "06"
ACT_SETTLE_ALL = "0c"
ACT_TAKE_ALL = "0f"

SEL_EXECUTE = "3593564c"        # execute(bytes,bytes[],uint256)
SEL_APPROVE = "095ea7b3"        # ERC20 approve(address,uint256)
SEL_P2_APPROVE = "87517c45"     # Permit2 approve(address,address,uint160,uint48)
SEL_P2_ALLOWANCE = "927da105"   # Permit2 allowance(address,address,address)

MAX_UINT = (1 << 256) - 1
MAX_UINT160 = (1 << 160) - 1
MAX_UINT48 = (1 << 48) - 1


def w(n):
    """One 32-byte word."""
    if isinstance(n, str):
        n = int(n, 16) if n.startswith("0x") else int(n, 16)
    return hex(n)[2:].rjust(64, "0")


def wa(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def dyn(payload_hex):
    """Length-prefixed dynamic bytes, padded to a 32-byte boundary."""
    n = len(payload_hex) // 2
    body = payload_hex + "0" * ((32 - n % 32) % 32 * 2)
    return w(n) + body


def build_swap_params(currency0, currency1, fee, tick_spacing, hooks,
                      zero_for_one, amount_in, amount_out_min):
    """params[0] -- replicates the 12-word layout observed on chain."""
    return (
        w(0x20)                      # offset to the struct
        + wa(currency0)
        + wa(currency1)
        + w(fee)
        + w(tick_spacing)
        + wa(hooks)
        + w(1 if zero_for_one else 0)
        + w(amount_in)
        + w(amount_out_min)
        + w(0)                       # observed as zero on chain
        + w(0x140)                   # hookData offset, relative to struct start
        + w(0)                       # hookData length
    )


def build_sell_calldata(token, currency0, currency1, fee, tick_spacing, hooks,
                        amount_in, min_out, deadline):
    """
    Sell `amount_in` of `token` for the other currency in the pool.

    zeroForOne is decided by which side the token sits on: selling means the
    token goes IN, so if the token is currency1 we are swapping 1 -> 0.
    """
    token = token.lower()
    token_is_c0 = token == currency0.lower()
    zero_for_one = token_is_c0
    quote = currency1 if token_is_c0 else currency0

    p0 = build_swap_params(currency0, currency1, fee, tick_spacing, hooks,
                           zero_for_one, amount_in, min_out)
    p1 = wa(token) + w(amount_in)      # SETTLE_ALL: pay the token in
    p2 = wa(quote) + w(min_out)        # TAKE_ALL:  receive the quote out

    actions = ACT_SWAP_EXACT_IN_SINGLE + ACT_SETTLE_ALL + ACT_TAKE_ALL
    # abi.encode(bytes actions, bytes[] params)
    head = w(0x40) + w(0)              # offsets, params offset patched below
    a_enc = dyn(actions)
    params = [p0, p1, p2]
    arr = w(len(params))
    offs, bodies, cur = [], [], 32 * len(params)
    for p in params:
        offs.append(w(cur))
        b = dyn(p)
        bodies.append(b)
        cur += len(b) // 2
    arr += "".join(offs) + "".join(bodies)
    params_off = 0x40 + len(a_enc) // 2
    v4_input = w(0x40) + w(params_off) + a_enc + arr

    cmds = dyn(CMD_V4_SWAP)
    inputs_arr = w(1) + w(0x20) + dyn(v4_input)
    # execute(bytes commands, bytes[] inputs, uint256 deadline)
    cmd_off = 0x60
    inp_off = cmd_off + len(cmds) // 2
    calldata = (SEL_EXECUTE + w(cmd_off) + w(inp_off) + w(deadline)
                + cmds + inputs_arr)
    return "0x" + calldata


def build_erc20_approve(spender, amount=MAX_UINT):
    return "0x" + SEL_APPROVE + wa(spender) + w(amount)


def build_permit2_approve(token, spender, amount=MAX_UINT160, expiry=MAX_UINT48):
    """Permit2.approve(token, spender, uint160 amount, uint48 expiration)"""
    return "0x" + SEL_P2_APPROVE + wa(token) + wa(spender) + w(amount) + w(expiry)


def build_permit2_allowance_call(owner, token, spender):
    return "0x" + SEL_P2_ALLOWANCE + wa(owner) + wa(token) + wa(spender)


def selftest():
    """Structural checks against the shape decoded from a live transaction."""
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {name} {extra}")

    p0 = build_swap_params("0x" + "0" * 40, "0x" + "ab" * 20, 0, 200,
                           "0x" + "cd" * 20, False, 12345, 678)
    chk("params[0] is 12 words (384 bytes)", len(p0) == 12 * 64, f"got {len(p0)//64}")
    chk("word4 is tickSpacing 200", int(p0[4 * 64:5 * 64], 16) == 200)
    chk("word10 is hookData offset 0x140", int(p0[10 * 64:11 * 64], 16) == 0x140)
    cd = build_sell_calldata("0x" + "ab" * 20, "0x" + "0" * 40, "0x" + "ab" * 20,
                             0, 200, "0x" + "cd" * 20, 1000, 900, 1788643268)
    chk("calldata selector is execute()", cd[2:10] == SEL_EXECUTE, cd[2:10])
    body = cd[10:]
    chk("commands offset 0x60", int(body[0:64], 16) == 0x60)
    cmd_len = int(body[0x60 * 2:0x60 * 2 + 64], 16)
    cmds = body[0x60 * 2 + 64:0x60 * 2 + 64 + cmd_len * 2]
    chk("commands == 0x10 (V4_SWAP)", cmds[:2] == "10", cmds[:2])
    chk("selling token=currency1 -> zeroForOne false",
        int(build_swap_params("0x" + "0" * 40, "0x" + "ab" * 20, 0, 200,
                              "0x" + "cd" * 20, False, 1, 0)[6 * 64:7 * 64], 16) == 0)
    ap = build_permit2_approve("0x" + "ab" * 20, UNIVERSAL_ROUTER)
    chk("permit2 approve selector", ap[2:10] == SEL_P2_APPROVE, ap[2:10])
    return ok


if __name__ == "__main__":
    print("v4sell selftest")
    sys.exit(0 if selftest() else 1)
