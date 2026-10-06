# The dev makes $36 a launch — and two numbers I reported were wrong

**Date:** 2026-10-06
**Wallet:** `Fb3fTYm3bdVMmYPG7fGeFjBAcx8GirHjaXZDUEYFijzn`
**Why this note is tracked:** the watchlist that carries these caveats lives in
`data/`, which is gitignored, so the correction would otherwise exist only on one
machine — while commit `7dce5dd` keeps the wrong figures in the repo history.

## The question

Slim asked whether this dev launched with 1 SOL and kept launching until it had
built up the funder's balance. It did not. Answering it properly required decoding
every transaction rather than reading a vendor summary.

## Full trace, all 7 signatures

```
10-05 19:58  +1.0000  funded by 2vVYEz9k
10-05 20:01  -0.0078  CREATE  POLLEN      BAgGYintgfRHs65evX8E6ELLnwxCqi5QCJeDxHtL1WB
10-05 20:02  -0.5029  BUY own POLLEN
10-06 05:43  +0.8139  SELL POLLEN
10-06 05:51  -0.0078  CREATE  CYBERTRUCK  CLHMgCrEz7GE5HJPsrTEgvE5MYYtQQ1Tkm11Yan9wKwr
```

Balance today 1.2955 SOL. Lifetime inflow 1.81 SOL.
**Dev profit: +0.295 SOL, about $36 at $121/SOL.**

No SOL has ever flowed dev → funder, so the funder's balance is not launch profit.

## TRAP 1 — a vendor's token-level metric read as the operator's P&L

GMGN showed `net_buy_24h = +$15,542` and I reported it alongside the dev without
separating the two. That field is **buy pressure on the token from every trader**,
not the creator's take. The gap is three orders of magnitude: POLLEN reached
$100k mcap while its creator cleared $36.

This is the usable finding, not a footnote. The money in this pattern is in being
**early on the dev's token**, never in copying the dev as a trader. The watchlist
entry is a launch trigger and nothing more.

Check: before quoting any vendor dollar figure against a wallet, ask whose balance
it would change. If the answer is "the token's traders", it is not that wallet's P&L.

## TRAP 2 — pruned RPC history read as complete history

I first reported the funder as **35.51 SOL across 6 signatures, having funded
exactly one dev**. Decoded properly it is **79.06 SOL across 152 signatures**, with
first activity about a year before this dev, plus 30.5 SOL sent to `Ch4MgL6P`,
0.1 SOL to `DxM1hfY8`, and 6.0 SOL swapped through Jupiter.

The cause: a shallow `getSignaturesForAddress` limit, and `getTransaction` returning
null for nearly all older signatures because the endpoint had pruned them. Both
failures look identical to a short, simple history. This is the same class of bug
this project keeps re-living — a partial pull that cannot be distinguished from a
complete one.

Check: count signatures returned vs the limit requested (equal means truncated), and
count null `getTransaction` results separately from decoded ones. A trace that
cannot say how much it could not see is not evidence of a short history.

The "~35 more launches of advance warning" estimate is withdrawn — the child count
is not bounded by anything measured.

## TRAP 3 — I assumed wallet rotation without checking

I recorded that fresh devs are the best bucket in our RHC data (1 launch 11.6% vs
10+ at 2.8%), inferred this operator probably rotates wallets, and warned the dev
watch would therefore catch nothing. **Wrong.** It created both tokens from this
same address, ten hours apart. The dev watch is the right instrument here.

The RHC base rate was real; applying it to one wallet as a prediction was not.

## Standing

The funder trigger (`poll_funders`, commit `7dce5dd`) is unaffected and still worth
having — it just cannot be justified by the 35-SOL arithmetic I used to justify it.
Executor remains disarmed. Zero transactions broadcast on any chain.
