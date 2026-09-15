---
title: Hood Sniper — Phase 0 Backtest Report (Revision 2)
date: 2026-09-01
status: COMPLETE — recommendation: ABANDON the deployer-reputation filter
supersedes: Revision 1 (stale-window result, retracted)
project: Hood Sniper
---

# Hood Sniper — Phase 0 Report (Rev 2)

**Question:** Does `win_score >= 2.0` over `>= 3` prior launches predict that a new
Robinhood Chain launch reaches **$3M+ market cap**?

**Answer: No — and not because the market is dead. The market is hot. The *filter* has
nothing to condition on.**

> **Recommendation: ABANDON the deployer-reputation filter as specified.** The venue is
> worth continued study; this specific edge is not supported by current data.

---

## 0. Retraction of Revision 1

Rev 1 sampled blocks 22,000,000–24,000,000 — which is **2026-07-28 to 07-31, a month
stale** — and reported "zero $3M+ outcomes, filter never fires." Slim challenged that as
unrepresentative of the current regime. **He was right.** Rev 1's null result was an
artifact of a dead window, and the report failed to flag that its window might not
represent current conditions. That conclusion is withdrawn and replaced below.

---

## 1. Slim was right: the opportunity is real

Current regime, **2026-08-13 → 08-25 (13 days)**, tokens with **real liquidity**
(`reserve_in_usd >= $5k` AND `vol_h24 >= $1k`, deduped to distinct tokens):

| band | distinct tokens |
|---|---|
| ≥ $1M | **30** |
| ≥ $3M | **19** |
| ≥ $10M | **10** |
| ≥ $50M | **6** |

Real winners with real depth — GOOSE ($70M FDV, **$7.1M** liquidity), BONER ($53M, $3.4M),
NET ($76M, $1.2M), MICRODUCK, GPRO, QUOTRON. These are not the phantom-FDV artifacts that
polluted the July sample. **Coins are running to $3–10M+, exactly as described.**

---

## 2. But the base rate is brutal

| | |
|---|---|
| On-chain launches enumerated (v2 `PairCreated`), Aug 13–25 | **45,138** |
| Distinct tokens reaching ≥ $3M with real liquidity | **19** |
| **Base rate ≥ $3M** | **0.042%** (~1 in 2,376) |
| **Base rate ≥ $50M** | **0.013%** |

≈ 3,470 launches/day producing ≈ **1.5** tokens/day above $3M. *(The numerator counts
GT-indexed tokens across all DEX versions while the denominator counts v2 only, so the
true base rate is **≤** these figures.)*

An unfiltered sniper is heavily negative EV here. Everything depends on the filter lifting
0.042% to something tradeable.

---

## 3. The filter cannot lift it — winners do not repeat

Resolved the deployer (`tx.origin` of the token's creation tx) for **129 winner tokens**:

| | |
|---|---|
| Winner tokens resolved | 129 |
| **Distinct deployer wallets** | **83** |
| Deployers with more than one winner | **2** |

And both "repeat" deployers are disqualified:

| wallet | wins | tokens | what it actually is |
|---|---|---|---|
| `0x5516b345…` | 17 | SPY, NVDA, SPCX, AAPL, MU, HIMS, GME, QQQ, GLD, GOOGL | **tokenized-equity issuer** — an RWA issuance desk, not a memecoin dev |
| `0xc8564726…` | 2 | EARN, GME | equity-ticker adjacent |

**Genuine memecoin deployers with repeat wins: zero.**

Every memecoin winner — GOOSE, BONER, NET, MICRODUCK, GPRO, QUOTRON — came from a wallet
that produced exactly one winner. **A reputation filter needs repeat performers to exist.
They do not.**

Worth noting: these are **not** fresh burner wallets (median **213** lifetime txs, min 8).
They have genuine on-chain history — just no prior *winning launch*. So the failure isn't
"we can't see their history"; it's that the history contains no predictive signal.

---

## 3b. Correction (Rev 3): memes are paired against tokenized stocks, not ETH

Slim flagged that on this chain memes launch **paired against tokenized stocks**, often via
the **Pons/Bankr** launchpads. Verified — **138 pools** pair a meme base against a stock
quote, and the matching is an explicit joke:

| pair | FDV | liquidity |
|---|---|---|
| AI *(Artificial Inu)* / **NVDA** | $184.5M | **$18.6M** |
| BONER / **HIMS** | $53.9M | $3.4M |
| SAYLORMOON / **MSTR** | $4.5M | $660k |
| CLIPPY / **MSFT**, ZUCC / **META**, SCHIFFY / **GLD**, DOGGIE / **TSLA**, AAPLCAT / **AAPL** | | |
| ORBIO / **NVDA** (on `pons-v2-dex`) | $3.3M | $142k |

**This means tokenized stocks are QUOTE ASSETS on this chain — the role WETH plays
elsewhere.** They are not launches, and Rev 2 wrongly counted 21 `BeaconProxy` stock tokens
as "wins."

**Re-running with them excluded — the result gets stronger, not weaker:**

| | Rev 2 | **Rev 3 (corrected)** |
|---|---|---|
| winner tokens | 129 | **106** (genuine meme/utility only) |
| distinct deployer wallets | 83 | **101** |
| wallets with >1 winner | 2 | **0** |

The Rev-2 "17-win repeat deployer" was **entirely quote-asset contamination**. Removing it
takes wallet-level repeats to **zero out of 101**. Only 2 weak funder-level candidates
survive (AI+CLANKER, VIRTUAL+WALLET).

### An error this surfaced in my own work

Rev 2's 0.042% base rate divided **all-DEX winners** by a **v2-only** denominator. Winners
are actually concentrated in v3/v4:

`uniswap-v4: 56 · uniswap-v3: 45 · up-v3: 21 · uniswap-v2: 9 · bankr: 9 · pons: 8`

v3 `PoolCreated` density is ~1,805 per 50k blocks vs ~250 for v2, and **v4 uses `Initialize`
events I never enumerated at all**. So the true denominator is far larger and the real base
rate is **materially lower** than 0.042%. Treat 0.042% as an upper bound.

### Further data corruption found

**31 pools report NEGATIVE `reserve_in_usd`** (AU/TSM −$3.19M, SIT/AI −$3.60M, CACHE/SNDK
−$2.05M). Any liquidity gate must reject `reserve <= 0`, not merely small values.


---

## 4. Cluster detection does not rescue it

The obvious defence is that devs rotate wallets, so entity-level clustering should recover
the signal. Tested via common funder across 102 winner wallets:

- 69 distinct funders; **9** funded more than one winner wallet
- **5 of those 9 are infrastructure hot wallets** — tx counts **1,244,085 / 1,028,561 /
  914,921 / 911,765 / 438,767**. These fund essentially every address on the chain.
- After removing them: **2** genuine candidates, each linking just 2 wallets —
  `VIRTUAL+WALLET` and `AI+CLANKER`, both platform/infra token pairs, not a memecoin dev
  shipping repeat winners.

**Trap worth keeping:** naive common-funder clustering reported a **16-wallet "cluster"**
that is really an exchange hot wallet. **Always gate clustering on funder tx_count.**
Unfiltered, this single false positive would have looked like a spectacular confirmation
of the thesis.

---

## 5. Data-quality guard (carried forward from Rev 1 — still essential)

In the July sample, *every* pool with `fdv_usd >= $1M` was a phantom: TEK at **$422
trillion** FDV on **$0.00027** of liquidity, zero volume. A naive `fdv >= $3M → bonded`
rule scores dead tokens as wins.

**Mandatory:** `reserve_in_usd >= 5000 AND vol_h24 >= 1000` before trusting any FDV.
All Rev 2 numbers apply this gate.

---

## 6. Recommendation — ABANDON this filter

The thesis has two independent load-bearing claims. The first survives; the second does not.

1. ✅ *"A small fraction of launches bond to $3M+"* — **confirmed**, 19 in 13 days.
2. ❌ *"Deployers who have bonded before are more likely to bond again"* — **not supported**.
   83 distinct winner wallets, zero genuine memecoin repeats, and clustering finds nothing
   after hot-wallet false positives are removed.

Without claim 2 there is no filter, and without a filter the 0.042% base rate makes the
strategy negative EV. **Do not build the listener/indexer/scorer as specified. No capital.**

**What would change this verdict:** evidence of repeat winners under an entity-resolution
method stronger than `tx.origin` or common-funder — bytecode/metadata similarity, launchpad
account identity, or social identity linking. If a genuine repeat-winner population exists,
it is hiding behind one of those, and that is the thing to test next.

**If the venue is still interesting,** the honest reframe is that the edge is *not* "who
deployed it." Candidates that fit the observed data better: early-liquidity/holder-growth
dynamics in the first minutes, or social velocity — signals that read the *token's* traction
rather than the *deployer's* past. Those are different projects and need their own Phase 0.

---

## Limitations

1. **Winner detection depends on GeckoTerminal indexing.** The full crawl is capped at 10
   pages/DEX, so 19 is a **lower bound** on $3M+ tokens. A larger winner set could surface
   repeats — though the current 83-wallet sample makes a strong repeat signal unlikely.
2. **Denominator is v2 `PairCreated` only.** v3/v4 and bonding-curve launches are excluded,
   so the true launch count is higher and the base rate correspondingly **lower**.
3. **Bonding-curve launchpads remain invisible** — Bags-style launches emit no `PairCreated`
   until graduation, so the sub-$100K entry window the strategy targets is not directly
   observed.
4. **Peak mcap is approximated by current FDV** (a lower bound); per-pool OHLCV was not
   affordable under GeckoTerminal's free-tier throttling.
5. **13-day window.** Regime-dependent by construction — this is the correction Rev 1
   needed, and it applies to Rev 2 too. Re-check before acting on these numbers later.

---

## Deliverables

| file | contents |
|---|---|
| `scripts/backtest_bonding_filter.py` | full pipeline: chain enumeration → deployer resolution → outcome resolution → tiering → point-in-time replay |
| `data/backtest_results.json` | Rev 1 + **`REVISION_2`** block with all current-regime findings |
| `data/chain_launches_aug13_25.json` | 45,138 enumerated launches (current window) |
| `data/gt_index_full.json` | GeckoTerminal winner index (1,991 pools) |
| `data/winner_deployers.json` | 129 winner tokens → deployer EOAs |
| `data/winner_funders.json` | 102 winner wallets → funders (cluster test) |

No execution code was written. Nothing committed or pushed. No capital at risk.

---

## 7. Rev 4 — venue enumeration and the SNIPE TAX

### Contracts discovered

| what | address / value |
|---|---|
| Uniswap **v4 PoolManager** | `0x8366a39cc670b4001a1121b8f6a443a643e40951` |
| v4 `Initialize` topic0 | `0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438` |
| **Pons launchpad** | `0xe33e9e479df8802cb0866d5d05258bec4cf62948` — `PonsV2LaunchAndBuy` (verified) |
| Pons token deployer | `0x3711cea4feade896c913c68f01eda97cb06d1a42` — `PonsV2LaunchDeployer` |
| Pons `Launched` topic0 | `0xdcacba5e347ae7abd91cb519eb877af8fa7774e347b85dd3ddcd24a2ba8cdf37` |

`Launched(address indexed token, address indexed curve, address indexed recipient, address launcher, uint256 quoteSpent, uint256 tokensReceived)` — gives the **true dev identity** (`launcher`) and their opening buy in one event.

*No keccak library was available, so Keccak-256 was implemented in pure Python and verified
against four known vectors before deriving any topic hash.*

**Bankr** pools use 64-byte v4-style pool IDs routed through the same v4 PoolManager with
its own hooks, so Bankr launches appear inside the v4 `Initialize` stream, not a separate
factory.

### Launch density (per 50k blocks)

`v3 PoolCreated: 1,805 · v4 Initialize: 975 · Pons Launched: ~430–583 · v2 PairCreated: 250`

**This confirms the Rev-3 denominator error.** v3 is ~7x denser than v2 and v4 ~4x denser,
so the Rev-2 base rate of 0.042% is several times too high. Treat it strictly as an upper
bound.

---

### The snipe tax — mechanic, and how to avoid it

From verified `PonsV2BondingCurve` source:

```solidity
function currentSnipeTaxBps(address recipient) public view returns (uint256) {
    if (snipeTaxExempt[recipient]) return 0;
    uint256 elapsed = block.timestamp - launchedAt;
    if (elapsed >= snipeTaxSeconds) return 0;
    return snipeTaxStartBps >> ((elapsed * 14) / snipeTaxSeconds);
}
```

Live params, **identical across all 4 curves sampled**: `snipeTaxStartBps = 9900` (99%),
`snipeTaxSeconds = 3`, `feeBps = 100`, `creatorTaxBps ∈ {0, 100, 250}`.

| elapsed | tax |
|---|---|
| **t+0s (launch second)** | **99.00%** |
| t+1s | 6.18% |
| t+2s | 0.19% |
| **t+3s and later** | **0.00%** |

**How to avoid it: do not buy in the launch second. Wait ≥3 seconds (~30 blocks at 0.101s)
and the tax is exactly zero.** Waiting even 2s cuts it to 0.19%. Trivial for a bot — the
only cost is ~3s of curve drift.

**The part you cannot avoid:** the creator passes `snipeTaxExemptions` — up to 31 wallets
plus the auto-appended recipient (**32 total**) — whose buys clear **untaxed in the launch
second**. There is no way to obtain exemption as an outside sniper.

### What snipers actually lose (measured, 80,000 blocks ≈ 2.24h)

| metric | value |
|---|---|
| `SnipeTaxCharged` events | 1,830 |
| distinct wallets taxed | 149 |
| median tax paid | 0.000071 ETH |
| **largest single hit** | **9.073 ETH (~$22k)** |
| total burned | 35.55 ETH |
| **extrapolated per day** | **~19,568 hits, ~380 ETH (~$930k) burned** |

`SnipeTaxExempted`: 3,583 events across 1,178 distinct addresses — devs bundle exemptions heavily.

**Honest assessment: the snipe tax is *not* the strategy killer.** It is avoidable by
waiting 3 seconds, and the dev's exempt head start is typically small (median opening buy
**0.03 ETH**). The costs that actually persist are `feeBps` 1% plus `creatorTaxBps` up to
2.5% — **up to 3.5% per trade, ~7% round trip** — which the thesis never accounted for.

---

### Pons deployer test — the filter is weaker here, not stronger

From **1,772** enumerated Pons launches:

| | |
|---|---|
| distinct launchers | **1,432** |
| launched exactly once | **1,320 (92%)** |
| launchers with ≥3 launches | **45 (3.1%)** |
| share of launches from ≥3-launch devs | **17.9%** |
| dev opening buy | median **0.03 ETH**, p90 0.15, max 68.0 |

**This corrects Rev 2.** That report claimed 32% of deployers had ≥3 launches covering 83%
of launches — inflated because it counted `tx.origin` on `PairCreated`, which sweeps in
router and bot wallets. Pons's `launcher` field is the **true dev identity**, and by that
measure **92% of devs launch exactly once**.

So on the venue Slim flagged as highest priority, the deployer-reputation filter has *less*
to work with, not more. This strengthens the ABANDON verdict rather than weakening it.

---

## 8. Rev 6 — post-graduation price distribution (closes the EV question)

Measured all **231 Pons graduations**: curve → `token()` → GT hourly OHLCV. 217 usable after
a supply cross-check. **GT coverage of graduates is 100%** (vs 1 of 17,684 for all Pons
launches) — graduation means a real DEX pool, which is why the earlier GT-based read failed.

> **Bug found and fixed:** the first pass took `top_pools[0]` OHLCV blindly and got the
> **paired stock's** price instead of the meme's — `COST / HOTDOG` returned Costco at $937
> rather than HOTDOG at $0.0000064, producing absurd $978B peak mcaps. Fixed with GT's
> `&token=<addr>` param; verified `last_close/price_usd` median = **1.0000**.

### The distribution

| percentile | mcap at graduation | **peak mcap after** | current mcap |
|---|---|---|---|
| p25 | $38,706 | $53,031 | $2,740 |
| **median** | **$41,221** | **$87,801** | **$3,380** |
| p75 | $49,252 | $259,379 | $7,382 |
| p90 | $52,906 | $887,010 | $36,820 |
| p99 | $125,388 | $4,985,422 | $870,083 |
| max | $186,623 | **$106,595,832** | $63,584,883 |

**Multiple, graduation → peak:** median **2.08x**, p75 6.03x, p90 18.95x, p99 134x, max
2,583x (NOVAAI). Mean 19.72x is carried almost entirely by that one outlier.

### Tier clearance — *of tokens that already graduated*

| | share of graduates |
|---|---|
| peak ≥ $1M | **7.83%** |
| peak ≥ $3M | **1.38%** |
| peak ≥ $10M | 0.46% |
| peak ≥ $50M | 0.46% |

### Full funnel vs what the thesis assumed

| | thesis | **measured** | gap |
|---|---|---|---|
| launch → $1M+ (top-dev) | 20–35% | **0.146%** | **~200x** |
| launch → $3M+ (top-dev) | 10–20% | **0.0257%** | **~500x** |

*(0.888% base graduation × 7.83%, or 1.859% with the prior-graduation dev filter.)*

### EV

Entry at $20k mcap, the thesis TP ladder (50% @ $1M / 30% @ $3M / 20% @ $10M), and a
generous assumption that non-graduates recover 30% selling back down the curve:

| | EV multiple | per trade |
|---|---|---|
| unfiltered | 0.327x | **−67.3%** |
| **with the prior-graduation dev filter** | **0.356x** | **−64.4%** |

Breakeven needs entry at roughly **$1,000 mcap** — below the dev's own exempt t+0 buy, so
unreachable by an outside sniper. And 98% of even that EV comes from the 30% recovery
assumption; if losers go to zero, filtered EV is **0.062x (−94%)**.

### Operational finding

Even the winners demand a fast exit: **median time from graduation to peak is 0.0 hours,
71% peak within one hour**, and median drawdown from peak is **95.4%** (82% are down more
than 90%). A TP ladder waiting for $3M–$10M will, in the overwhelming majority of cases,
watch the position round-trip to zero.

---

## FINAL VERDICT — ABANDON

The 2.09x dev-reputation lift from Rev 5 is real but nowhere near sufficient. It doubles a
0.888% graduation rate to 1.859%; the funnel beyond that is 200–500x worse than the thesis
assumed, and no entry price reachable by an outside sniper turns it positive.

The venue mechanics are genuinely interesting and now fully mapped — bonding curves, the
snipe tax, the meme/stock pairing, the launchpad contracts. But **this strategy, as
specified, is decisively negative EV. Do not deploy capital.**

