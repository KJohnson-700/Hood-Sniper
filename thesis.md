# Hood Sniper — Thesis (Rough Draft)

**Status:** v0.6 ROUGH DRAFT — 2026-08-31 (corrected exit tiers to Slim's stated $1M+ reality)
**Edge type:** Manual/auto entry at launch on tokens from proven-bonding deployer wallets
**Direction:** Long (entry at launch)
**Venue:** Robinhood Chain (chain ID 4663)

---

## The Setup

Robinhood Chain (Arbitrum Orbit L2, chain ID 4663) launched July 1, 2026 and quickly became one of the most active memecoin venues. Tokens launch via bonding-curve mechanics similar to pump.fun — they start with no liquidity, build trading volume on a curve, and "graduate" / "migrate" to a DEX (Uniswap-style) once they hit a market cap threshold.

Most launches rug within hours. Most that don't rug still die at low mcap. But a small fraction bond all the way up — surviving 7-30 days, hitting $3-10M mcap, and a smaller fraction hitting $50M+ mcap and fully migrating.

The smart money tracks which **deployer wallets** are producing these bonding tokens.

---

## The Edge — Deployer Reputation by Bonding Tier

The filter is the **deployer wallet**, not the token. A wallet that has shipped multiple tokens that successfully bonded (survived 7-30 days, hit meaningful mcap, possibly migrated) is much more likely to ship another bonding token than a fresh anonymous wallet.

**Bonding tiers (the actual scoring axes):**

| Tier | Criteria | Points |
|---|---|---|
| **Survived** | Token still trading 7-30 days post-launch | +1 each |
| **Bonded-mid** | Token hit $3-10M market cap | +2 each |
| **Bonded-high** | Token hit $50M+ market cap | +5 each |
| **Migrated** | Token fully graduated to DEX (bonding curve complete) | +3 each |
| **Rugged** | Token died in <7 days | -3 each |
| **Copy-cat** | Token name piggybacks on existing ticker | -1 each |

**Score formula (rolling window, last 10 launches):**
```
win_score = (
    survived_count          * 1
  + bonded_mid_count        * 2
  + bonded_high_count       * 5
  + migrated_count          * 3
  - rugged_count            * 3
  - copy_cat_count          * 1
) / total_launches
```

**Top-dev thresholds:**
- `win_score >= 2.0` across `>= 3 launches` = proven shipper
- A dev who has 2 tokens at $50M+ and 1 at $3-10M = `win_score = (0+0+10+3)/(3) - 0 = 13/3 ≈ 4.3` → top-tier
- A dev who has 1 token at $3M, 2 that rugged = `win_score = (0+2+0+0-3)/3 = -1/3 ≈ -0.33` → skip

**Why bonding metrics beat "did it pump" metrics:**
- "Hit $50M" is a lagging indicator (you find out weeks later)
- Bonding tier progress is observable in real-time from the bonding curve contract itself
- "Did this dev ship tokens that survived" is a **predictive** signal for their next launch
- "Did this dev ship tokens that hit $50M" = elite tier, the wallets to actually watch

---

## Trade Construction (Manual + Automated)

### Entry
- **Trigger:** New pair detected on Robinhood Chain from a deployer with `win_score >= 2.0` and `>= 3 past launches`.
- **Direction:** Long.
- **Timing:** as fast as possible — within 30-90s of liquidity being added (early bonding curve entry).
- **Entry mcap target:** sub $100K (early curve position), sometimes sub $50K for the deepest entries.
- **Order type:** limit at curve price + slippage; market fallback if bonding is moving fast.
- **Size:** tiny — $20-50 per entry (4-5% of bankroll) because most positions will die.

### Exit Targets (Slim's actual tiers)

You make money at multiple levels — not just moonshots:

- **Tier 1 (regular win, happens most often):** exit at **$1M-$1.5M market cap** = +20-30x
- **Tier 2 (good win, the sweet spot):** exit at **$3M-$10M market cap** = +60-200x
- **Tier 3 (moonshot, ride it):** exit at **$50M+ market cap** = +1,000x+ (CASHCAT-tier, rare)

**TP ladder (locks profit at every tier):**
- **TP1:** 50% out at **$1M-$1.5M mcap** — the realistic case, lock it in.
- **TP2:** 30% out at **$3M-$5M mcap** — let the winners ride to the sweet spot.
- **TP3:** 20% out at **$10M+ mcap OR pre-migration peak** — moonshot tier, trail until momentum breaks.

**Hit rate reality by tier (with deployer-rep filter):**
- $1M+ mcap: ~20-35% of top-dev launches (this is the realistic edge)
- $3M+ mcap: ~10-20% of top-dev launches (your stated target)
- $50M+ mcap: ~1-3% of launches (lottery tier, ride when it happens)

### Stop Loss
- **Tight:** -25% from entry (most bonding tokens retrace hard).
- **Time stop:** -7 days if no bonding progress (token is dead).
- **Exit everything:** if dev wallet dumps their allocation OR holder concentration spikes (top 10 >80%).

### Position Sizing Math
- $500 bankroll, $25 average entry = 20 concurrent attempts possible.
- Even a 15-20% "bond to $3M+" hit rate = 3-4 winners out of 20.
- One winner at $25 → $3M = +120x = $3,000 return on a $25 bet.
- 16 losers at -25% = -$100 total loss.
- **Net: massive positive EV even at low hit rates because winners are 50-120x.**

---

## The Social Signal Layer (DYOR Pack)

For every top-dev launch detected, alert includes clickable links so Slim can DYOR in 30 seconds:

### Sources
1. **X API v2** (Free tier 10K posts/month, Basic $200/mo for 500K) — mention velocity + sentiment.
2. **YouTube Data API v3** (Free, 100 search.list calls/day cap) — videos in last 24h.
3. **Fomo app trending** — X proxy for trending tab awareness.
4. **Telegram alpha channels** — MTProto scraping via telethon.

### DYOR Alert Format
```
🎯 HOOD SNIPER — TOP DEV LAUNCH

Token: $CASHCAT (0xabc...123)
Deployer: 0xdef...456
Win Score: 4.3 (top-tier)

📊 DEPLOYER TRACK RECORD:
• 3 past launches:
  - $CASHCAT2: hit $52M mcap ✓ MIGRATED
  - $MOONDOG: hit $8M mcap, survived 30d
  - $CATGIRL: rugged in 4d ✗
• LP locked, mint renounced
• Last launch 12d ago

📱 SOCIAL SIGNALS:
• X mentions: 247 (z=+3.4) ▲
• YouTube: 2 videos last 24h
• Fomo trending: YES
• Sentiment: 12% negative ✓

🔗 DYOR LINKS:
X top tweets:
  • https://x.com/alpha/status/123 — "@defi_kid: $CASHCAT bonding fast..." (2.3K ❤️)
YouTube:
  • https://youtube.com/watch?v=xyz — "New Robinhood Chain gem" (12K views, 6h ago)
DexScreener: https://dexscreener.com/robinhood/0xabc...123
Blockscout: https://robinhoodchain.blockscout.com/token/0xabc...123
Bonding curve: https://robinhoodchain.blockscout.com/address/0xabc...123

⚠️ Top dev ✓ | Social aligned ✓ | Sub-$100K entry window ✓
Not financial advice. Tight stops. Most of these will die.
```

---

## Counter-Thesis / Why This Could Fail

1. **Survivorship bias in scoring.** A "proven" dev might just be lucky. Past bonding wins don't guarantee future ones.

2. **Sybil / cluster deployers.** One bad actor with 5 wallets, each with "2 winners / 1 rug" looks like 5 separate decent deployers. Cluster detection (common funder, identical bytecode, coordinated timing) is mandatory.

3. **Liquidity exit risk.** A $25 entry at $50K mcap, exit at $3M = 60x of mcap in sells. If the token has thin liquidity, your exit moves the price down hard. Need to scale out (50% / 30% / 20%) not all-at-once.

4. **Top-dev rugs.** Proven devs CAN still rug. They have reputation, but they also have the most to gain from a successful rug (largest community to exploit). Per-launch honeypot check still required.

5. **Front-running.** Other bots will beat you to the slot on top-dev launches. Expect ~30-50% of "snipe candidates" to be unfilled or filled much worse.

6. **Memecoin season ends.** If BTC dominance rises and memecoin volume dies, bonding curves stall. Whole thesis collapses.

7. **L2 risk.** Robinhood Chain is below L2Beat Stage 0.

---

---

## Rug Rate Reality (Why The Deployer Filter Matters)

**Base rate on Robinhood Chain (and pump.fun-style chains generally):**
- ~98.5% - 99.7% of tokens never graduate the bonding curve to a DEX.
- ~99% of launches on pump.fun show pump-and-dump or rug characteristics (Solidus Labs).
- Robinhood Chain hit ~18,600 launches/day at peak. First honeypot documented on Day 2. Documented "vanishing tokens" scam wave in mid-July 2026.
- Without filtering: ~99% of entries will go to zero.

**With the deployer-rep filter:**
- Filter excludes fresh anonymous wallets (the source of most rugs).
- Filter excludes serial ruggers (cluster detection collapses sybils).
- Filter excludes copy-cat tickers (piggybacking on existing tokens).
- Remaining candidates: top-decile deployers who've already shipped bonding successes.

**Realistic hit rate after all filters (by tier):**
- $1M+ mcap: ~20-35% of top-dev launches (most common edge)
- $3M+ mcap: ~10-20% of top-dev launches (Slim's stated target)
- $50M+ mcap: ~1-3% of launches (moonshot tier)

**Why the math still works:** even at the pessimistic 20% / 10% / 1% hit rates, winners are 30-1000x. 100 trades → ~20 winners hitting $1M+ (+30x each) + ~8 incremental winners hitting $3M+ (+80x each) + ~1 winner hitting $50M+ (+1000x) = **+$60,000+**. 80 losers × $30 × -25% = **-$600**. Net positive EV is enormous IF the filter actually works.

**But: filter must be validated.** Phase 0 backtest is mandatory. If `win_score >= 2.0` doesn't predict bonding-tier outcomes, the math collapses.

---

## Validation Plan (must run before live)

1. **Deployer-rep backtest:** Pull 60-90 days of RH Chain launches. For each, compute the deployer's `win_score` AS OF that launch (using only earlier history). Measure correlation between score and whether the new launch hit $3M+ / $50M+ mcap.
2. **Bonding tier accuracy:** Does `bonded_high_count` predict future `bonded_high_count` for the same dev? (i.e. does past elite shipping predict future elite shipping?)
3. **Precision @ score thresholds:** Of all launches that hit $3M+ in the dataset, what % came from `win_score >= 2.0` deployers? Want this high (>50%).
4. **Time-to-fill:** Can we get a fill within 30-90s of `PairCreated` on RH Chain? Measure RPC + mempool lag.
5. **Paper trade:** Run signal + filter for 7-14 days, log every signal + hypothetical P&L.

**If precision <30% or avg PnL per trade is negative on the backtest, do not go live.**

---

## Companion Files

- `CLAUDE.md` — AI-assistant context, build phases, component details.
- `requirements.txt` — Python deps.
- `.env.example` — API key template (no secrets).
- `README.md` — project hub (TBD).
