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

---

## Follow-up: how much the dev commits per launch

Slim's read was that the +0.8139 looked like creator fees being collected. The token
balance deltas rule it out — the position zeroes at the exact quantity bought:

```
10-05 20:02  BUY   0  ->  4,734,567.32 POLLEN   -0.5029 SOL
10-06 05:43  SELL  4,734,567.32  ->  0          +0.8139 SOL
```

A fee claim cannot return the precise token count the wallet bought. What made it
read as a fee claim is that the sell routed through `proVF4pMXVaYqmy4Nj…`, a
third-party program, rather than pump.fun's own `6EF8rrec…`.

**Dev commitment per launch:**

| launch | create fee | dev buy | outcome |
|---|---|---|---|
| POLLEN | 0.0078 SOL | **0.5029 SOL** | sold for 0.8139 (+0.311) |
| CYBERTRUCK | 0.0078 SOL | **none from this wallet** | still open |

So half the seed on the first token, nothing on the second. n=1 — not a sizing pattern.

## Is there a hidden second wallet doing the buying?

Checked, because deploy-from-A / buy-from-B is the standard pattern and would mean the
real position size is invisible on the creator. Enumerated every signature on all three
mints back to the create tx and decoded the first 35 of each launch window.

Two wallets appear in both POLLEN's and CYBERTRUCK's windows. Neither is a dev alt:

- `Gdfyi9hHz7s1aDKbexkGeudLZ4pVjpLpsxECTEmV55Qr` — POLLEN 0.302 + CYBERTRUCK 0.502 SOL.
  Looked promising until the wallet itself was profiled: **1,000+ signatures in 0.9 days
  and 41.3 SOL**. That is a sniper bot buying everything, not an operator's buy wallet,
  and at that rate hitting 2 of 3 tokens is unremarkable.
- `55mX9tbe…` — 0.012 SOL in each, 443 sigs in 0.8 days. Same story, smaller.

**No evidence of hidden dev buying.** Visible commitment stands at 0.5029 SOL once.

## TRAP 4 — getSignaturesForAddress returns the NEWEST signatures

My first pass at this sorted a 60-signature response ascending and called the result
"early buyers". It was the most *recent* 60. The tell I initially missed: the dev's own
0.50 POLLEN buy was absent from a list that claimed to cover POLLEN's launch.

A second pass capped enumeration at 12,000 and still landed 16 minutes after POLLEN's
create. The real counts are **44,933** signatures for POLLEN and **28,009** for
CYBERTRUCK, needing 45 and 29 paginated calls to reach genesis.

Check: to study a launch window, paginate with `before` until a page returns fewer rows
than the limit, then assert the oldest blockTime equals the create tx's. Any cap on that
loop silently relocates the window forward in time.
