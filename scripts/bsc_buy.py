#!/usr/bin/env python3
"""
BSC four.meme execution — buy/sell calldata for the TokenManager bonding curve.

BSC is EVM, so `ethsign` and the whole signing path already work; the only thing
missing was the venue's function selectors. Those are NOT documented anywhere, so
they were recovered from live transactions the same way the Pons and v4 routes were:
sample real buys/sells, group by (to, selector), and read the parameter layout off
transactions whose `value` confirms which argument is the amount.

Recovered from live four.meme traffic (2026-09-08):

    BUY  0x7f79f6df  (token, recipient, amountIn, minTokensOut)  PAYABLE — native BNB
    BUY  0x87f27655  (token, amount, 0)                          — ERC-20 quoted
    SELL 0x3e11741f  (token, amount, ...)                        — main sell path

VERIFIED by simulation with state overrides (BSC RPCs support them): faking a USDT
balance + allowance for a test address made 0x87f27655 execute against live curves —
2/4 sampled tokens OK, the other 2 reverting "More BNB" because they are BNB-quoted.
Both failure messages are informative and worth keeping:
    "BEP20: transfer amount exceeds allowance"  -> ERC-20 quoted, approval missing
    "More BNB"                                  -> BNB quoted, send value instead
USDT storage slots on BSC: balance 1, allowance 2.

Confirmation that arg[2] is the amount: a sampled tx carried `value` 0.00797 BNB and
arg[2] = 7,966,956,993,286,096 wei. Slightly under `value` because the venue takes its
fee off the top, which is also why minTokensOut must be derived from a simulation
rather than from `value`.
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)

CHAIN_ID = 56
USDT = "0x55d398326f99059ff775485246999027b3197955"   # 18 decimals on BSC, not 6
SEL_TOKEN_INFOS = "0xe684626b"      # _tokenInfos(address) -> word[1] is the QUOTE token
TOKEN_MANAGER = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
SEL_BUY_NATIVE = "7f79f6df"
SEL_BUY_ERC20 = "87f27655"
SEL_SELL = "3e11741f"
MAX_TRADE_USD = 25.0
BNB_USD = 620.0


def w(n):
    return hex(n & ((1 << 256) - 1))[2:].rjust(64, "0")


def a(addr):
    return "0" * 24 + addr[2:].lower()


def quote_of(rpc, token):
    """
    The curve's QUOTE currency for this token. Returns None if unreadable.

    four.meme launches are NOT all BNB-quoted -- many are USDT-quoted, and a
    USDT-quoted buy pulls via transferFrom, so it fails with
    "BEP20: transfer amount exceeds allowance" until the approval is staged.
    A BNB-quoted token buys with `value` and reverts "More BNB" if you send none.
    Read the quote first rather than assuming, exactly as on Base.
    """
    r = rpc("eth_call", [{"to": TOKEN_MANAGER,
                          "data": SEL_TOKEN_INFOS + "0" * 24 + token[2:]}, "latest"])
    res = r.get("result")
    if not res or len(res) < 194:
        return None
    return "0x" + res[2:][64:128][-40:]


def build_buy_erc20(token, amount_in):
    """ERC-20 (usually USDT) quoted buy. Needs quote -> TokenManager approval first."""
    return "0x" + SEL_BUY_ERC20 + a(token) + w(amount_in) + w(0)


def build_approve(spender, amount=(1 << 256) - 1):
    return "0x095ea7b3" + a(spender) + w(amount)


def build_buy(token, recipient, amount_in_wei, min_tokens_out):
    """Native-BNB buy. amount_in_wei must also be sent as tx.value."""
    return ("0x" + SEL_BUY_NATIVE + a(token) + a(recipient)
            + w(amount_in_wei) + w(min_tokens_out))


# four.meme rejects any sell whose token amount is not a whole multiple of 1e9 wei
# with `revert "GW"`. This is undocumented and it is an exit-time landmine: a buy
# fills an arbitrary number of tokens, so a "sell 100% of my balance" call reverts
# almost every time -- and it reverts at exactly the moment you need the exit.
# Recovered by bisection against a live curve (2026-09-08): 28459073398718457600000
# reverts, 28459073398718000000000 fills.
SELL_GRANULARITY = 10 ** 9


def quantize_sell(amount_tokens, granularity=SELL_GRANULARITY):
    """Round a sell size DOWN to the venue's granularity. Down, never up: rounding up
    would ask for more tokens than the wallet holds and revert on the transfer."""
    return (amount_tokens // granularity) * granularity


def build_sell(token, amount_tokens, min_out=0, granularity=SELL_GRANULARITY):
    amt = quantize_sell(amount_tokens, granularity)
    if amt == 0:
        raise ValueError(f"sell size {amount_tokens} is below one granularity unit "
                         f"({granularity}) -- nothing sellable")
    return "0x" + SEL_SELL + a(token) + w(amt) + w(min_out)


def quote_buy(post, token, recipient, amount_in_wei):
    """
    Tokens a buy of `amount_in_wei` will actually return. None if it cannot fill.

    WHY THIS HAD TO EXIST BEFORE ANY BUY WAS ARMED. `simulate_buy` below answers
    only "does it revert", discarding the fill — so min_tokens_out had nothing to be
    derived from and every buy would have gone out with min_out=0. A zero floor
    accepts ANY fill, including a sandwich that returns dust. That exact bug was
    caught twice already in this project (Base `try_buy`, and the RH executor), and
    it would have shipped a third time here.

    four.meme publishes no quoter, so the buy's OWN minTokensOut argument is the
    oracle: the contract reverts when the computed output is below it, so the
    largest value that still simulates IS the fill. Same technique as quote_sell,
    two batched rounds.
    """
    def cd(min_out):
        return ("0x" + SEL_BUY_NATIVE + a(token) + a(recipient)
                + w(amount_in_wei) + w(min_out))

    def _batch(datas):
        reqs = []
        for i, d in enumerate(datas):
            reqs.append({"jsonrpc": "2.0", "id": i, "method": "eth_call",
                         "params": [{"from": recipient, "to": TOKEN_MANAGER,
                                     "value": hex(amount_in_wei), "data": d}, "latest"]})
        out = []
        for i in range(0, len(reqs), BATCH_MAX):
            chunk = reqs[i:i + BATCH_MAX]
            for j, r in enumerate(chunk):
                r["id"] = j
            res = post(chunk, want=len(chunk))
            got = [None] * len(chunk)
            for r in res or []:
                if isinstance(r, dict) and isinstance(r.get("id"), int) and 0 <= r["id"] < len(got):
                    got[r["id"]] = "result" in r
            out.extend(got)
        return out

    powers = [1 << k for k in range(0, 96)]
    hi_i = _last_true(_batch([cd(p) for p in powers]))
    if hi_i is None:
        return None
    lo = powers[hi_i]
    hi = powers[hi_i + 1] if hi_i + 1 < len(powers) else lo * 2
    step = max((hi - lo) // 50, 1)
    cands = [lo + step * j for j in range(1, 51)]
    j = _last_true(_batch([cd(c) for c in cands]))
    return cands[j] if j is not None else lo


def buy_min_out(expected_tokens, slippage_bps=300):
    """Floor for a buy. Never send 0 — that is a blank cheque to a sandwicher."""
    return (expected_tokens * (10_000 - slippage_bps)) // 10_000


def simulate_buy(rpc, token, recipient, amount_in_wei, min_out=0):
    """
    eth_call the buy before sending. Returns (ok, err).

    Never send an unsimulated buy: an unverified selector fails at the worst possible
    moment, and on a launch the difference between reverting and filling badly is the
    whole trade.
    """
    data = build_buy(token, recipient, amount_in_wei, min_out)
    r = rpc("eth_call", [{"from": recipient, "to": TOKEN_MANAGER,
                          "value": hex(amount_in_wei), "data": data}, "latest"])
    if "result" in r:
        return True, None
    return False, (r.get("error") or {}).get("message", "?")[:120]


if __name__ == "__main__":
    cd = build_buy("0x29883de21aeee602158e379593e187ae5bc34444",
                   "0x060eeafc9c3dac015287ee6bb210214bc4e8ac7d",
                   7966956993286096, 661874241403936890000000)
    print(f"reconstructed buy calldata: {len(cd)//2-1} bytes")
    print(f"  {cd}")


# --- synthetic quoter -------------------------------------------------------
# four.meme ships no quoter contract, and an exit sent with min_out=0 accepts ANY
# fill -- the same mistake that was already caught and fixed once on Base. So the
# floor has to come from somewhere.
#
# The sell's own `minQuoteOut` argument is the oracle: the contract reverts when the
# computed proceeds fall below it, so the LARGEST min_out that still simulates is
# exactly the fill. That works on any RPC, needs no tracer, and cannot drift from the
# venue's real pricing the way a reimplemented bonding curve would.
#
# Cost is kept to two round trips by batching: one batch brackets the answer between
# consecutive powers of two, a second splits that bracket 64 ways.

# Public BSC endpoints cap JSON-RPC batch size and DO NOT say so: bsc-dataseed answers
# a 128-request batch with a one-element list. Accepting that as the answer marks 127
# unanswered calls as "reverted" -- which is how the first version of this quoter
# concluded a working sell returned nothing. Same failure shape as the get_logs
# truncation bug. So: chunk to a size every endpoint honours, and treat any short
# response as NO ANSWER rather than as a result.
BATCH_MAX = 25


def _batch_call(post, to, datas, ov=None):
    """
    Send eth_calls in batches. Returns [tri-state] in input order:
        True  = executed        False = reverted        None = never answered
    None is not False. Collapsing the two is what produced the false negative.
    """
    out = []
    for i in range(0, len(datas), BATCH_MAX):
        chunk = datas[i:i + BATCH_MAX]
        reqs = []
        for j, d in enumerate(chunk):
            p = [{"from": SIM_FROM, "to": to, "data": d}, "latest"]
            if ov:
                p.append(ov)
            reqs.append({"jsonrpc": "2.0", "id": j, "method": "eth_call", "params": p})
        res = post(reqs, want=len(reqs))
        got = [None] * len(chunk)
        for r in res or []:
            if isinstance(r, dict) and isinstance(r.get("id"), int) and 0 <= r["id"] < len(got):
                got[r["id"]] = "result" in r
        out.extend(got)
    return out


SIM_FROM = "0x1111111111111111111111111111111111111111"


def quote_sell(post, token, amount_tokens, ov=None, granularity=SELL_GRANULARITY):
    """
    Proceeds in quote-token wei for selling `amount_tokens`, by bisecting min_out.

    `post` takes a list of JSON-RPC request dicts and returns the list of responses.
    `ov` is an optional eth_call state override -- pass one to quote a position you do
    not hold yet (that is how the exit path was proven before any token was owned).

    Returns None when nothing brackets, which callers must treat as "do not send",
    never as zero.
    """
    amt = quantize_sell(amount_tokens, granularity)
    if amt == 0:
        return None

    def cd(min_out):
        return "0x" + SEL_SELL + a(token) + w(amt) + w(min_out)

    # round 1 -- bracket between consecutive powers of two. 96 covers any plausible
    # proceeds (2**96 wei is ~7.9e10 tokens at 18 decimals).
    powers = [1 << k for k in range(0, 96)]
    ok = _batch_call(post, TOKEN_MANAGER, [cd(p) for p in powers], ov)
    hi_i = _last_true(ok)
    if hi_i is None:
        return None                      # unanswered, or even min_out=1 reverts
    lo = powers[hi_i]
    hi = powers[hi_i + 1] if hi_i + 1 < len(powers) else lo * 2

    # round 2 -- split the bracket 50 ways; ~2% precision, which the min_out floor
    # discounts past anyway
    step = max((hi - lo) // 50, 1)
    cands = [lo + step * j for j in range(1, 51)]
    ok = _batch_call(post, TOKEN_MANAGER, [cd(c) for c in cands], ov)
    j = _last_true(ok)
    return cands[j] if j is not None else lo


def _last_true(flags):
    """
    Index of the last True before the first False. An unanswered slot (None) ENDS the
    scan without being counted -- guessing either way would silently mis-quote.
    """
    last = None
    for i, f in enumerate(flags):
        if f is True:
            last = i
        elif f is False:
            break
        else:
            break
    return last


def min_out_floor(proceeds, slippage_bps=300):
    """Exit floor. Never send min_out=0 -- that accepts any fill, including a robbery."""
    return (proceeds * (10_000 - slippage_bps)) // 10_000


# --- quote-currency gate ----------------------------------------------------
# four.meme curves are NOT mostly BNB or USDT quoted. Measured over 300 CONSECUTIVE
# launches (2026-09-08, 635 launches in ~45 min of blocks):
#
#     QQQB  18.7%   tokenized QQQ        FLNCB   5.7%
#     BNB   16.7%   native               + six more exotic quotes at 6-9% each
#     SPCXB 16.3%   tokenized SpaceX     USDT    0.7%
#
# so only ~17% of launches are reachable with BNB or USDT, spread against TEN
# different quote assets. (An earlier read said ~22% BNB; that sampled the oldest
# slice of a 20k-block window rather than consecutive launches. This figure is the
# consecutive one.) A curve
# quoted in QQQB can only be BOUGHT with QQQB and pays its EXIT in QQQB, which then
# needs a second hop through a thin market before it is money. Trading those means
# carrying inventory in six exotic assets and wearing a second leg of slippage on
# every exit.
#
# So the venue is gated to quotes the wallet natively holds. This is a deliberate
# coverage cut, not an oversight: ~22% of ~12.6k launches/day is still ~2,700
# candidates a day, and every one of them exits straight to money.
NATIVE_BNB = "0x0000000000000000000000000000000000000000"
TRADEABLE_QUOTES = {NATIVE_BNB: "BNB", USDT: "USDT"}


def quote_label(q):
    if not q:
        return "unreadable"
    return TRADEABLE_QUOTES.get(q.lower(), q[:10] + "…")


def quote_tradeable(q):
    """(ok, reason). Unreadable is NOT tradeable -- it usually means graduated."""
    if not q:
        return False, "quote unreadable — token is graduated or not a four.meme curve"
    if q.lower() in TRADEABLE_QUOTES:
        return True, TRADEABLE_QUOTES[q.lower()]
    return False, (f"quoted in {q[:10]}… — buying needs an inventory of that asset and "
                   f"the exit pays out in it, not in money")
