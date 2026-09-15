#!/usr/bin/env python3
"""
Range order — a take-profit that rests ON CHAIN and needs no process running.

Every other exit in this stack is Python logic: if the process stops, the stop and the
TP ladder stop with it. A range order is different. You place single-sided liquidity in
a narrow tick band on the far side of the current price; when price crosses the band your
tokens convert to quote automatically, in the pool, with nothing watching. It also earns
fees while it rests.

The asymmetry, stated plainly: this works UPWARD only. It is a persistent TAKE-PROFIT.
It cannot be a stop-loss -- nothing on a hookless v4 pool sells for you on the way down,
so downside protection still requires a live process. Do not read this as "the position
is now safe unattended".

Direction is derived, not assumed:
  token is currency0 -> price(token) = 1.0001^tick, higher price = HIGHER tick -> band ABOVE
  token is currency1 -> price(token) = 1/1.0001^tick, higher price = LOWER tick  -> band BELOW
Both cases place the band where the position sits entirely in the TOKEN until it fills.
Getting this backwards mints a position that is instantly converted at a loss.
"""
import math, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from v4sell import w, wa, dyn
from base_buy import PERMIT2, NATIVE

POSITION_MANAGER = "0x7c5f5a4bbd8fd63184577525326123b519429bdc"   # verified poolManager()
SEL_MODIFY_LIQS = "dd46508f"      # modifyLiquidities(bytes,uint256)
ACT_MINT_POSITION = "02"
ACT_SETTLE_PAIR = "0d"
MIN_TICK, MAX_TICK = -887272, 887272


def price_to_tick(price):
    """P = 1.0001^tick, in POOL orientation (token1 per token0)."""
    if price <= 0:
        raise ValueError("price must be > 0")
    return int(math.floor(math.log(price) / math.log(1.0001)))


def band_for_target(target_token_price, token_is_c1, tick_spacing, width=1):
    """
    Tick band that fills as the TOKEN reaches `target_token_price` (quote per token).

    Returns (tick_lower, tick_upper). `width` is how many spacings wide the band is --
    1 is the tightest and behaves most like a limit; wider fills more gradually.
    """
    pool_price = (1.0 / target_token_price) if token_is_c1 else target_token_price
    t = price_to_tick(pool_price)
    t = (t // tick_spacing) * tick_spacing
    if token_is_c1:
        # token is currency1 -> it converts as price FALLS -> band sits BELOW spot
        lo, hi = t - tick_spacing * width, t
    else:
        # token is currency0 -> converts as price RISES -> band sits ABOVE spot
        lo, hi = t, t + tick_spacing * width
    return max(MIN_TICK, lo), min(MAX_TICK, hi)


def _tick(v):
    return w(v & ((1 << 256) - 1)) if v < 0 else w(v)


def build_mint_calldata(c0, c1, fee, tick_spacing, hooks, tick_lower, tick_upper,
                        liquidity, amount0_max, amount1_max, owner, deadline):
    """
    modifyLiquidities(abi.encode(bytes actions, bytes[] params), deadline)
    actions = MINT_POSITION, SETTLE_PAIR
    """
    pool_key = (wa(c0) + wa(c1) + w(fee) + _tick(tick_spacing) + wa(hooks))
    p0 = (pool_key + _tick(tick_lower) + _tick(tick_upper) + w(liquidity)
          + w(amount0_max) + w(amount1_max) + wa(owner)
          # hookData offset is the STATIC part: PoolKey(5) + 6 fields + the offset
          # word itself = 12 words = 0x180. 0x1a0 pointed past the end and reverted.
          + w(0x180) + w(0))                       # hookData offset + length
    p1 = wa(c0) + wa(c1)                            # SETTLE_PAIR
    actions = ACT_MINT_POSITION + ACT_SETTLE_PAIR
    a_enc = dyn(actions)
    params = [p0, p1]
    arr = w(len(params))
    offs, bodies, cur = [], [], 32 * len(params)
    for p in params:
        offs.append(w(cur)); b = dyn(p); bodies.append(b); cur += len(b) // 2
    arr += "".join(offs) + "".join(bodies)
    params_off = 0x40 + len(a_enc) // 2
    unlock = w(0x40) + w(params_off) + a_enc + arr
    return "0x" + SEL_MODIFY_LIQS + w(0x40) + w(deadline) + dyn(unlock)


def liquidity_for_token_amount(amount, tick_lower, tick_upper, token_is_c1):
    """
    Liquidity that a single-sided position of `amount` tokens represents.
    L = amount / (1/sqrt(Pa) - 1/sqrt(Pb))   for token0
    L = amount / (sqrt(Pb) - sqrt(Pa))       for token1
    """
    sa = 1.0001 ** (tick_lower / 2)
    sb = 1.0001 ** (tick_upper / 2)
    if sb <= sa:
        return 0
    denom = (sb - sa) if token_is_c1 else (1.0 / sa - 1.0 / sb)
    return int(amount / denom) if denom > 0 else 0


if __name__ == "__main__":
    # direction sanity: both orientations must place the band on the fill side
    for t_is_c1 in (True, False):
        lo, hi = band_for_target(0.00002, t_is_c1, 60, width=1)
        spot_pool = (1 / 0.00001) if t_is_c1 else 0.00001
        spot_tick = price_to_tick(spot_pool)
        side = "BELOW" if hi <= spot_tick else "ABOVE"
        ok = (side == "BELOW") if t_is_c1 else (side == "ABOVE")
        print(f"  token_is_c1={t_is_c1!s:5s} target 2x spot -> band [{lo}, {hi}] "
              f"sits {side} spot tick {spot_tick}  {'OK' if ok else 'WRONG SIDE'}")
