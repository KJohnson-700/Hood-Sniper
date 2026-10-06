# Why no flap.sh launch has reached Discord since 2026-10-02

**Date:** 2026-10-06
**Reported by:** Slim — "I DONT SEE THE FLAP LAUNCHES COMING IN THE DISCORD CHANNEL FOR BNB"
**Verdict:** real, total, and caused by two individually-correct decisions multiplying to zero.

## The failure

| | |
|---|---|
| flap.sh rows ever collected | **331,556** |
| flap.sh rows that passed the alert router's quote gate | **0** |
| flap.sh alerts ever sent | 68, all in a single batch at `2026-10-02T21:15:02Z` |

Those 68 were the cold-start seeding batch. The quote gate shipped in `4446d22`
("Actionable alert output, working links, and rates tuned per channel") and flap.sh
went to zero in the same moment. four.meme kept alerting through 2026-10-06, which is
why the channel looked alive rather than broken.

## The two decisions

1. `bsc_monitor.on_launch` resolved the quote **only for four.meme**, because
   `quote_of()` reads four.meme's own `TokenManager` registry, which has no entry for a
   flap.sh token. Everything else was hardcoded to `(True, "-")` — quote unresolved.
2. `alert_router` drops any bsc row whose quote did not resolve. Correct on its own:
   an unresolved quote means an unproven exit, and alerting every tradeable launch had
   produced a 2,220-row firehose.

Neither is wrong. Together they excluded 100% of the venue, permanently and silently.
This is the third instance of this shape on BSC alone — see the earlier
freshness-AND-GMGN-rating gate that returned 0 candidates from 370 genuine overlaps.

**Check:** any gate that drops rows must be countable per venue. "0 from this venue"
and "this venue is quiet" are different facts and must not render identically.

## What flap.sh actually is

Not a bonding curve. A launch creates a real AMM pair and **funds it in the same
transaction** — verified on block 126147733, token `0xbac1d9…7777`: two `PairCreated`
events, the funded one holding **6.14 WBNB against 1,107,036,368 tokens** (~$3,800).
6.14 BNB is the standard seed; it recurred on every BNB-quoted launch sampled.

So flap.sh launches have an exit from block zero — a **better** exit path than a
four.meme curve, not a worse one. They were the rows being thrown away.

## flap.sh is NOT uniformly BNB-quoted

Measured on 8–9 consecutive live launches rather than assumed: 6 paired against WBNB,
1 against USDT, and 2 against **QQQB ("Invesqo QQQ", a tokenized Nasdaq-100)**.
Hardcoding "flapsh means BNB" would have mislabelled ~25% of launches, including the
ones whose exit pays out in QQQB — the exact asset class `quote_tradeable()` exists to
refuse. `flapsh_quote()` therefore reads whichever side of the funded pair is not the
launch token, and runs it through the same allowlist four.meme uses.

Note flap.sh pairs against **real WBNB** while four.meme uses a zero-address sentinel
for native BNB, so `bsc_buy.TRADEABLE_QUOTES` could not be reused as-is.

## TRAP — the receipt-indexing race

The launch log arrives before the node will serve that transaction's receipt. The first
read returned null for **2 of 6** launches, and both resolved on retry — one with 6.14
BNB of real liquidity. Left alone this re-creates the same silent exclusion in miniature.
Fixed with a bounded 3-attempt retry (1.2s apart) that fires only when the receipt is
genuinely absent, never when a receipt simply has no pair. Post-fix: 3/3 then 100%.

## TRAP — I measured a filter using only the rows it had already passed

While checking the alert footer's claim that "only ~17% of four.meme launches are
BNB/USDT-quoted", the feed said **100.0% across 27,064 rows over 7 days**. I was about
to record that as a correction to the 2026-09-08 finding.

It is a selection artifact. `bsc_monitor`'s `--all-quotes` flag defaults to
`tradeable_only=True`, so launches with an untradeable quote `return` **before** the
feed write. The feed contains only the survivors of the exact filter being measured, so
the question "what share are BNB-quoted" returns 100% by construction.

An unbiased re-measure straight from chain logs gave 11/11 BNB — unfiltered, but n=11
cannot overturn n=300. **The ~17% figure stands; it is not corrected.** The collector
now runs with `--all-quotes` (persisted in `start_all.sh`) so every launch is recorded
and the router keeps filtering on `quote_why`. Alerting is unchanged; the coverage
question becomes answerable from accumulated data.

Within minutes of switching the flag the artifact was confirmed outright: four.meme
began recording **3 untradeable of 11** launches, and flap.sh **6 of 27** — rows that
simply did not exist in the feed an hour earlier. So the true four.meme tradeable share
is neither the 100% the filtered feed reported nor yet confirmed as 17%; early
unfiltered counts sit near 73%. Left to accumulate rather than called on n=11.

**Check:** before quoting a rate from a feed, ask what the writer dropped. A filtered
feed answers questions about its own filter with 100%.

## Second, separate finding: the BNB channel is nearly dead anyway

Simulated over the last 24h of real rows:

```
flapsh     8,672 -> tradeable 8,672 -> in GMGN 5,543 -> GMGN fresh 118 -> traded 27  = 1.1/hr
four_meme  1,905 -> tradeable 1,905 -> in GMGN 1,792 -> GMGN fresh  54 -> traded  2  = 0.1/hr
```

So four.meme has been clearing roughly **two alerts per 24h**. The binding constraint is
the 12-minute GMGN-freshness gate, which cuts 1,792 to 54 — it effectively requires the
token to be trending on GMGN at that moment. Fixing flap.sh adds ~1.1/hr, a usable rate
rather than a firehose. Whether to loosen `FRESH_MIN` for bsc is a separate call and is
NOT changed here.

## Third finding: the router's bsc window was shorter than its own metric

`_tail("bsc_feed.jsonl")` ran on the 900KB default, which at BSC's launch rate covered
**6.1 hours** (2,163 rows). But the gate judges a launch on GMGN's `swaps_24h`, so a
token can first become interesting up to 24h after launch — the row was dropping out of
the window before the metric that would alert on it could move. The entire 6–24h
maturity band was invisible. Widened to 6MB, covering ~33h.

The effect was immediate and independent of the flap.sh fix: bsc candidates went from
**1 to 4** on the same data the moment the window widened.

**Check:** a feed window must be at least as long as the lookback of the metric that
reads it.

## State at write time

Verified end to end except the last hop: quote resolution works live (100% of sampled
BNB launches), rows carry `liq_quote`, the quote gate now passes 55 flap.sh rows in the
last hour where it passed 0 of 331,556 before, and the router builds bsc candidates.

No flap.sh alert has landed in Discord **yet**, and that is expected rather than a
remaining fault: the newly-resolved rows are ~25 minutes old and the final gate requires
50 swaps or 2 smart wallets, which takes time to accumulate. The 331,556 pre-fix rows
carry `quote_why = "-"` permanently and will never backfill — correct, they are history.
Expect roughly 1.1 flap.sh alerts/hour once post-fix rows age in.

## Not changed, deliberately

- `FRESH_MIN` / the GMGN-traded thresholds — the rate is now sane; tuning needs its own measurement.
- No liquidity floor on `liq_quote` yet. It is recorded and shown (6.14 BNB is the
  standard seed, and one QQQB pair read 0.0000), but gating on an unmeasured threshold
  is how the `buyers>=10` filter happened.
- The `~17%` four.meme coverage figure — see the selection trap above.

Executor remains disarmed. Zero transactions broadcast on any chain.
