# What actually separated the runners — first pass

**Date:** 2026-10-07
**Ask:** track the traders who are consistently early in coins that run, build better
entry alerts, collect data on which coins do well and why, and find reliable accounts
to put in trackers.
**Universe:** Solana, 9.9 days of our own GMGN snapshot history, 1,435,643 rows.

## Ground truth first

Labels come from **our** snapshot history, not a vendor "trending" list — trending is a
popularity read, not an outcome. A token is a runner if peak/first-seen ≥ 3x **and**
peak ≥ $150k.

```
450,562 distinct Solana tokens seen
201,130 mcap values dropped as impossible (14% — worst was $12 -> $12,217,920,000)
  3,902 tokens with >=3 snapshots over >=30 min (enough to measure a trajectory)
     49 RUNNERS
    196 matched CONTROLS
```

**Coverage limit, which bounds every number here:** GMGN snapshots the top ~80 per
chain per stage, so only ~3,900 of 450,562 tokens can be labelled at all. The control
group is "tokens GMGN surfaced repeatedly that did not run" — never "all tokens".

## The confound I had to kill first

My own label is `peak / first_mcap`, so a token first *seen* smaller mechanically earns
a bigger multiple. With coarse bucket-matching, runners came in at a median $6,887
against $9,265 for controls — so "runners start smaller, with less liquidity, fewer
holders, earlier on the curve" fell straight out of the definition, and every
size-correlated feature inherited it.

Replaced with nearest-neighbour matching on log(first_mcap) within the same day, each
control used once:

```
runners   median $6,887   p25 $4,876   p75 $12,412
controls  median $6,928   p25 $4,862   p75 $12,508
```

Starting-size distributions now agree within 1%. What follows survives that.

## Result: almost nothing visible at discovery predicts a runner

Medians at **first sighting** — deliberately not final state, because a runner's last
snapshot shows the swaps and holders it earned *by running*, which would make every
feature look predictive and none be.

| feature | runners | controls | ratio |
|---|---|---|---|
| swaps_24h | 71 | 47 | **1.51x** |
| buys_24h | 47 | 31 | 1.52x |
| sells_24h | 23 | 15 | 1.53x |
| x_followers | 36 | 29 | 1.24x |
| bots | 11 | 9 | 1.22x |
| holders | 24 | 20 | 1.20x |
| volume_24h | 6,557 | 5,529 | 1.19x |
| net_buy_24h | 1,150 | 1,439 | 0.80x |
| progress | 0.336 | 0.397 | 0.84x |
| liq | 8,379 | 9,792 | 0.86x |
| top10_rate | 0.182 | 0.209 | 0.87x |
| **creator_launches** | **1** | **2** | **0.50x** |
| has twitter | 73.5% | 74.5% | 0.99x |

**1. Trade COUNT in the first ~30 seconds is the one real numeric signal — 1.51x.**
Confound checked: both groups were first seen a median **0.5 min** after creation
(0.92x), so this is not "runners were caught later". Normalising to swaps per minute of
age still gives **1.60x**. Note buys *and* sells both rise ~1.5x, so the signal is
**turnover, not buy pressure** — and `net_buy_24h` is actually *anti*-predictive at
0.80x. Buy-side pressure at discovery points the wrong way.

**2. Fresh creators, again — 1 prior launch vs 2, a 0.50x ratio.** This independently
replicates the Robinhood Chain finding (1 launch 11.6% vs 10+ launches 2.8%) on a
different chain and venue. Two chains, two datasets, same direction: serial deployers
are worse. The deployer-reputation thesis this project was named for is now contradicted
twice.

**3. GMGN's smart-money count is useless as an entry trigger.** Median `smart` and
`renowned` are **0 for runners and controls alike** at first sighting — they populate
later, after the move. Worth acting on: the BSC alert path currently gates on
`smart >= 2 or swaps >= 50`, and the smart half of that can never fire at discovery.

**4. Having a Twitter account carries no signal at all** — 73.5% vs 74.5%.

**5. The clearest single finding is a VENUE, not a feature:**

```
runners    Pump.fun 71%   meteora_virtual_curve 18%   pump_mayhem 6%
controls   Pump.fun 90%   meteora_virtual_curve  6%   bags 2%
```

meteora_virtual_curve is **3.0x over-represented** among runners; Pump.fun is
**0.79x under-represented**. A meta is often a venue before it is a theme. Caveat: 9
meteora runners, so this is a lead to size up, not a settled number.

## The caller side

`gmgn track kol` returns `twitter_username` **and** the wallet, which is exactly the
@solanaswaggy mapping — a named handle tied to a checkable on-chain address, with
timestamps that `token_top_traders` does not provide. Timestamps are the whole
difference between "was early" and "was in it".

`scripts/kol_tracker.py` now accumulates that stream on a supervisor. Scoring measures
each buy **forward from its own timestamp** (peak after the buy / cap at the buy), never
the token's all-time peak — which would credit a caller who bought the top of a coin
that had already run. Every buy counts, and the base rate is printed beside every score.

Current state: 89 trades, 11 handles, churn 4–8 per 90s so the 100-row window leaves no
gaps at that interval. @solanaswaggy has not appeared in the sample yet. **No scores
yet** — outcomes need snapshots after the buy, so this needs a day or two of
accumulation before any handle can be called reliable or not.

## TRAP — a background study starved the live bot

The first attribution run, paced at 0.25s, made ~90 calls and hit HTTP 429. The 429 was
**not** per-endpoint throttling: `market/trenches`, `user/kol`, `token/info`,
`token_top_holders`, `token_top_traders` and `user/wallet_stats` all began refusing at
once and stayed refused for over five minutes. And the retries kept grinding.

The real cost was not the lost research — **the live discovery feed shares that key**,
and `gmgn_feed` went from 6.5 to 9.5 minutes stale while this ran.

Fixed with a hard call budget (default 60), 6s pacing, and an abort after 3 consecutive
429s. Verified: it now stops after 3 instead of grinding through 245 tokens. Progress is
resumable, so aborting early costs only the current pass.

**Check:** a research script and the running bot must not share a quota without the
script having a budget and a breaker. Measure the bot's feed freshness during any bulk
pull.

## State

- `meta_lab.py --label --features` work and are reproducible.
- `meta_lab.py --attribute` is written and gated behind the breaker; it has pulled 1 of
  245 tokens and needs re-running as quota allows. **Wallet-level scores do not exist
  yet** — the "who is consistently early" list is not built, only the machinery for it.
- `kol_tracker.py` collecting; scoring awaits forward outcomes.

Executor remains disarmed. Zero transactions broadcast on any chain.
