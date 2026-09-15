---
title: Hood Sniper — Strategy B (Graduation Momentum)
date: 2026-09-02
status: SLIPPAGE VALIDATED (0.15% vs 18-20% breakeven) — needs a larger live sample before capital
supersedes: the launch-snipe thesis (negative EV, see PHASE_0_REPORT.md)
---

# Strategy B — Graduation Momentum

**One line:** Don't snipe the launch. Buy the **graduation**, take profit at 5x, stop at −30%.
Median hold is **8 minutes**.

The original thesis lost money because it entered on the bonding curve (paying up to a 99%
snipe tax, 0.888% graduation rate) and exited at $1M–$10M mcap targets that a median token
never reaches ($87.8k median peak). Strategy B trades the same venue at a different point in
the lifecycle, where the distribution actually pays.

---

## The pipeline

### 1. Dev wallet vetting — **VALIDATED, use it**

Pons emits the true dev identity, not a router address:

```
Launched(address indexed token, address indexed curve, address indexed recipient,
         address launcher, uint256 quoteSpent, uint256 tokensReceived)
topic0 0xdcacba5e347ae7abd91cb519eb877af8fa7774e347b85dd3ddcd24a2ba8cdf37
at     0xe33e9e479df8802cb0866d5d05258bec4cf62948   (PonsV2LaunchAndBuy)
```

| filter | graduation rate | lift | significance |
|---|---|---|---|
| base (all launches) | 0.888% | — | — |
| **dev has ≥1 prior graduation** | **1.859%** | **2.20x** | **Fisher p = 0.007 ✅** |
| dev has ≥3 prior *launches* | 0.542% | 0.61x | **worse than random** ❌ |

**Score on prior SUCCESS, never on launch count.** Launch count is a bot detector — the top
wallet has 1,207 launches at a 0.17% hit rate. Watchlist: `data/pons_dev_watchlist.json`
(143 devs with ≥1 graduation).

*Weaker second finding:* devs with a prior graduation also produced bigger post-graduation
runs (median 5.13x vs 2.07x; 55% vs 27% reached ≥5x) — but n=11, **p = 0.06, not
significant**. Treat as a tiebreaker, not a gate, until more history is indexed.

**Dev opening buy size does NOT predict anything** (2.52x / 2.01x / 2.15x across small/mid/large
buy terciles). Don't build on it.

### 2. Memeability / trend qualification — **WEAK, do not over-build**

Measured on 217 graduates:

| feature | n | median mult | ≥5x |
|---|---|---|---|
| classic meme word (inu/dog/cat/pepe/moon…) | 32 | 2.47x | 34% |
| paired vs tokenized stock | 33 | 2.20x | 30% |
| paired vs native ETH | 160 | 2.03x | 26% |
| utility/AI word | 11 | 1.87x | 9% |
| **all** | 217 | 2.08x | — |

Differences are inside the noise at these sample sizes. **On-chain metadata does not qualify
memeability.** If this layer matters it needs off-chain social velocity (X/YouTube/Telegram),
which is a separate data source and unvalidated here. Do not gate trades on ticker/name
heuristics.

### 3. Trade / no-trade and size

- **Trade every graduation.** Supply is only ~19/day; selectivity costs more in missed
  volume than it gains in precision.
- **Flat $25.** The distribution is lottery-shaped (13% win rate); varying size on weak
  signals adds variance without edge.
- Optional tilt: prefer devs on the watchlist (2.20x graduation lift already realised by the
  time you see a graduation, so its marginal value post-graduation is the unproven n=11 result).

### 4. Entry timing — **snipe tax is structurally avoided**

The snipe tax lives **only in the bonding curve** — `buy()` reverts `CurveGraduated` once
graduated. Post-graduation trading is a normal Uniswap V4 pool. **Strategy B never touches
the tax.**

For reference, if you ever do buy on the curve:
`tax = snipeTaxStartBps >> ((elapsed*14)/snipeTaxSeconds)`, live params 9900 bps / 3s →
**t+0 = 99%, t+1s = 6.18%, t+2s = 0.19%, t+3s = 0%.** Never buy in the launch second; wait 3s.
149 bots/day fail this and burn ~380 ETH/day (~$930k).

**Entry for Strategy B: as soon as possible after `CurveCompleted`.** Backtested entry is the
close of the first minute candle. Delay hurts:

| entry delay | EV (TP5x, stop −30%) |
|---|---|
| **+0 min** | **1.230x** |
| +1 min | 1.171x |
| +5 min | 1.104x |
| +15 min | 1.045x |
| +30 min | 0.803x |

### 5. Exit

**TP 5x / stop −30% / time-exit at 16h.**

| outcome | share | median time |
|---|---|---|
| stopped −30% | 80% | **6 min** |
| hit 5x TP | 13% | **10 min** |
| time exit | 7% | ~16h |

Median hold **8 minutes**; 80% resolve inside an hour. Do not use an mcap-target ladder —
median peak is $87.8k and median drawdown from peak is 95.4%.

### 6. Alerting requirement

Entry at +0–1 min sets the budget: **detect `CurveCompleted` and fill inside 60 seconds.**

- Subscribe via WSS to `robinhood-rpc.publicnode.com`, filter topic0
  `0xf8d37a90738ae063b8b8058b66f5880cf3cf7ab0c5d4fa78219696591dfbfb67`
- The emitting contract address **is** the curve → `token()` gives the token
- Block time is 0.101s, so 60s ≈ 594 blocks of headroom — this is a comfortable budget,
  not an HFT problem. No co-location needed.

---

## Backtested performance

Look-ahead-free, path-dependent (entry = candle close at delay, forward-only prices, TP vs
stop first-touch), 214 graduated tokens, 2026-08-13 → 08-25, 7% round-trip fees:

| slippage | EV per trade |
|---|---|
| 0% | 1.230x |
| 5% | **1.169x** |
| 10% | 1.107x |
| 15% | 1.046x |
| ~18–20% | breakeven |

**Bankroll simulation** ($500 bank, $25 stake, 5% slippage, 20,000 Monte Carlo runs):

| trades | median bank | p10 | p90 | P(down) | P(wiped) |
|---|---|---|---|---|---|
| 20 | $577 | $409 | $784 | 30% | 0% |
| 60 | $742 | $443 | $1,082 | 15% | 0% |
| 120 | $993 | $571 | $1,459 | 7% | 0% |
| 240 | $1,496 | $886 | $2,157 | 2% | 1% |

At ~19 graduations/day, 240 trades ≈ 13 days. Median hold 8 min → **capital is not the
binding constraint**; signal supply is.

---

## Honest caveats — read before deploying

1. **EV rests on 27 winners out of 214.** Lose 80% of trades. A 20-trade sample has a **30%
   chance of being down**. This needs volume to express.
2. **Stop is checked on candle CLOSE** — the data has no lows, so real stop-outs are
   *understated* and true EV is somewhat lower than shown.
3. **Slippage is assumed, not measured.** Graduated pools seed with the curve's reserves
   (tiers 4.2–917 ETH). At $25 size slippage should be small, but it is unverified, and
   breakeven is ~18–20%.
4. **One regime, 12 days, 214 tokens.** Memecoin venues are regime-dependent — this is the
   exact mistake that invalidated the first version of this analysis.
5. **Competition.** If other bots trade the same public `CurveCompleted` signal, entry prices
   degrade toward the no-edge case.

**Next step: forward paper trade for 7–14 days, logging every `CurveCompleted`, the fill
price achievable, realised slippage, and P&L. Do not commit capital until measured slippage
is confirmed under ~10%.**

---

## Paper-trade validation (2026-09-03)

`scripts/paper_trade_logger.py` — **observational only**: no keys, no signing, no broadcast.
It reads real `Swap` fills on the graduated V4 pool rather than candles, and derives price
impact from the pool's own `liquidity` + `sqrtPriceX96`.

### The number that gated deployment

| metric | value |
|---|---|
| unique graduations processed | 49 |
| tradeable after gate | 41 |
| **measured slippage, $25 buy — median** | **0.151%** |
| p75 / p90 / max | 0.455% / 0.484% / 0.531% |
| **breakeven** | **18–20%** |
| **margin** | **~119x** |

Slippage was the one assumed input in the backtest and the thing most likely to kill the
strategy. Measured from real pool state it is **~0.15%**, two orders of magnitude below
breakeven. A $25 trade sits at the **35th percentile** of real trade sizes on these pools —
normal size for the venue, not a whale order.

### Independent corroboration of the backtest

| | backtest (GT candles) | paper logger (real swaps) |
|---|---|---|
| EV / trade | 1.169x @5% slip | **1.1176x** |
| TP rate | 13% | **9.8%** (95% CI 0.7–18.8%) |
| median hold | 8 min | **4.3 min** |

Two independent data paths agree. PnL over 41 paper trades: **+$120.56** on $25 stakes.

### NEW RULE — liquidity gate at entry (required)

A minority of graduated pools seed with ~1e18 liquidity against a ~3e22 norm — 4 orders of
magnitude thinner. There, $25 moves price by **orders of magnitude** (one measured at 1.26e14%).
One such fill destroys the average.

**Gate: skip any pool where the stake's estimated impact exceeds 2%.**
- costs **8 of 49 signals (~16%)**
- the split is binary in practice — pools are either under ~0.5% or catastrophically thin
- checkable in real time from the `liquidity` field of the first `Swap`
- gated pools also show very few swaps (2, 3, 24) — a useful secondary tell

Without the gate, pooled EV is **−2.2e10x**. With it, **+1.1176x**. This is the single most
important implementation detail in the spec.

### Operational timings confirmed

- graduation → V4 pool `Initialize`: median **88 blocks (~9s)**
- so the practical alert budget is ~9s of unavoidable lag, then your fill
- median hold **4.3 min**

### What is still NOT proven

1. **n=41.** The TP-rate CI is 0.7–18.8%. The edge is directionally confirmed, not tightly
   estimated. A 12-trade sub-window in this very run returned 0 TPs and −$92 — that is
   normal variance for a 10–13% win rate, and it is what live trading will feel like.
2. **No order was ever placed.** Slippage is computed from pool state at the real entry
   block, not from a fill you actually received. Competition for the same public signal
   could degrade it.
3. **One regime**, ~5 days of graduations.

**Next: run `--live` for 7–14 days to reach n≈200+, then re-check TP rate and slippage
before any capital.**

```bash
python3 scripts/paper_trade_logger.py --live --stake 25 --max-slippage-pct 2
python3 scripts/paper_trade_logger.py --summary
```


---

## Wallet-vetting layer — tested 2026-09-03 (n=148 graduated tokens)

All five proposed checks are computable from Pons curve events alone — no Transfer indexing:

| check | source event | verdict |
|---|---|---|
| holder distribution | `CurveBuy` / `CurveSell` net positions per address | ❌ no signal |
| liquidity locks | `graduate()` is `onlyFactory`, protocol-routed — uniform across launches | ❌ non-discriminating |
| **block-1 snipers** | **`SnipeTaxCharged(address indexed recipient, uint256)`** | ⚠️ **best candidate** |
| coordinated fresh wallets | `CurveBuy` buyers + first-tx times | ❌ no signal |
| insiders exiting | `CurveSell` by `SnipeTaxExempted` wallets | ❌ no signal |

Topics: `CurveBuy 0xec36bf57…`, `CurveSell 0x8113d738…`, `SnipeTaxExempted 0xe4b7e48f…`,
`SnipeTaxCharged 0x3bc39a55…`. Extractor: `scripts/extract_wallet_features.py`.

### Most of it is noise

Eight features tested against post-graduation multiple. **None survive Bonferroni
correction (p < 0.00625).** An n=60 interim read showed `n_exempt` at 1.46x and
`top1_share` at 0.76x — at n=148 those became 0.98x and **1.50x (direction reversed)**.
Do not trust univariate splits on small samples here.

**"Insiders quietly exiting" showed nothing**: exempt wallets selling pre-graduation gave
2.52x (none) vs 1.95x (some), p=0.138. Intuitive, but not in the data.

### The one live candidate: zero block-1 snipers

| | n | median mult | ≥5x |
|---|---|---|---|
| **no snipers** | 39 | **4.73x** | 46% |
| snipers present | 109 | 1.87x | 23% |

Fisher p = 0.0065 (just misses the 0.00556 threshold). It **survives stratification by
activity** — 4.01x ratio in the low-activity band, 1.84x in high — so it is not purely an
attention artifact, though no-sniper tokens do have ~half the buyers.

**Post-hoc selected. Needs out-of-sample confirmation before sizing on it.**

### Architecture finding: SIZE on the score, never GATE on it

| policy | trades | $/day @ 19 signals |
|---|---|---|
| flat $25, trade all | 145 | $135.45 |
| **gate to no-snipers only** | 39 | **$50.19** ❌ |
| tilt $50 no-snipe / $25 else | 145 | **$185.64** ✅ |
| tilt $60 / $20 | 145 | **$188.67** ✅ |

Gating *improves per-trade EV* (1.285x → 1.393x) but costs **73% of signals**, and net
profit falls 63%. **Signal supply (~19/day) is the binding constraint, not edge quality.**
Any vetting layer must modulate size, not admission.

### Where the AI actually belongs

The trade is ~4 minutes with a ~9s decision window (graduation → pool init is median 88
blocks). An LLM call in that path adds latency and nondeterminism for no gain — these five
checks are deterministic arithmetic over event logs.

**Correct split:**
- **hot path (deterministic, <1s):** compute features from curve logs, apply the liquidity
  gate, set size from a fixed weight vector.
- **offline (where AI earns its keep):** fit and periodically refit those weights on
  accumulating paper-trade outcomes. With n=148 and nothing surviving correction, there is
  **not yet enough labelled data to fit anything** — that is the real blocker, and the live
  logger is what fixes it.

---

## Corrections + dev-vetting result (2026-09-03, later)

### 1. Launch counts were ~9x undercounted — base rate corrected

`Launched` events were only being read from **one** Pons launchpad
(`0xe33e9e47`). Pons runs several routers (`0xa5aab3f0`, `0x4783c67b`,
`0xe47e41f4`, `0x7ed598bc`, …), so that enumeration saw ~1,474 launches/day.

Every curve emits `Initialized(address token)` (`0x908408e3…`) regardless of
router. Measured density: **~12,837 curves/day** (5 of 6 sampled emitters answer
`deployer()`, so ~83% are Pons curves → **~10,700/day**).

**Consequence:** the graduation base rate is **~0.18%, not 0.888%** — roughly 5x
rarer than reported. Graduations themselves were always complete (enumerated by
topic across all curves), so **Strategy B's economics are unaffected** — it trades
graduations, not launches. What changes is the Strategy-A framing and any
"lift over base rate" figure computed against launches.

### 2. Dev attribution fixed — registry grew 63%

`deployer()` on the curve (selector `0xd5f39488`) is the reliable dev identity;
`Launched`-based attribution silently drops every curve from a non-primary router.

| | before | after |
|---|---|---|
| devs with ≥1 graduation | 143 | **233** |
| devs with ≥2 graduations | 10 | **14** |
| graduations attributed | 157 | **229 / 231** |

### 3. The dev signal got stronger under correct attribution

Post-graduation run size, point-in-time prior graduations only:

| | n | median mult | **≥5x rate** |
|---|---|---|---|
| dev has 0 prior graduations | 137 | 2.06x | 26.3% |
| **dev has ≥1 prior graduation** | 11 | **6.03x** | **63.6%** |

Fisher **p = 0.0144** (was 0.0605 under the broken attribution). This is now the
strongest vetting result — a **2.4x lift** in the rate of hitting the 5x target.

### 4. Sizing policy — tilt, confirmed again

| policy | $/day @ 19 signals |
|---|---|
| flat $25 | $135 |
| tilt x2 zero-snipers | $186 |
| tilt x2 dev prior-grad | $179 |
| **logger default (x2 snipe, x1.5 dev)** | **$210** |
| tilt x3 dev prior-grad | $222 |
| **GATE on dev prior-grad** | **$43** ❌ |

Gating again destroys value (15 trades vs 145). Monte Carlo ($500 bank, 240
trades): tilting raises the median outcome with ruin risk moving 0.0% → 0.1%.

**But the tilt magnitudes are fitted in-sample on n=15 prior-grad observations,
and the Monte Carlo resamples those same rows — it propagates the in-sample edge
rather than testing it.** The *direction* is supported (p=0.0144); the *magnitude*
is not reliably estimated. The logger stays at the conservative x1.5 dev tilt
until live data confirms it out-of-sample.

---

## Live out-of-sample results (2026-09-04, n=267)

The logger ran ~15h unattended. **375 graduations seen, 267 scored, 108 gated.**

### Core strategy: HELD out of sample

| | backtest | earlier paper (n=41) | **live (n=267)** |
|---|---|---|---|
| EV / trade | 1.285x | 1.118x | **1.128x** |
| TP rate | 13% | 9.8% | **10.9%** |
| median slippage | assumed 5% | 0.151% | **0.329%** |
| median hold | 8 min | 4.3 min | **1.3 min** |

PnL flat $25: **+$852** on $6,675 deployed (**+12.8%**) after charging entry *and* exit
slippage. Exit slippage (selling ~5x notional back into the pool) costs only ~$51.

### Vetting layer: FAILED out of sample — sizing reverted to flat

| signal | in-sample | **out-of-sample (n=267)** |
|---|---|---|
| zero-snipers | ≥5x rate 46% vs 23%, p=0.0065 | TP **8.3%** vs **11.6%** — **REVERSED**, p=0.33 |
| dev prior-graduation | ≥5x 63.6% vs 26.3%, p=0.0144 | n=3 only, EV 0.648x |

Tilting on them **hurt**: +12.09% return on capital vs **+13.20% flat**. The logger is now
flat $25. Deliberately **not** flipping the tilt to favour snipers>0 (which shows +13.82%
live) — that is refitting noise in the opposite direction, the exact error this test caught.

**This is the vetting proof-of-concept result: the wallet signals do not replicate.**
Worth knowing after ~1 day rather than after building an AI layer on them.

### Graduation supply grew ~30x

Backtest window (Aug 13–25): ~19 graduations/day.
Now: **~600–700/day**. Signal supply is no longer the binding constraint.

### ⚠ THE REMAINING FRAGILITY — fill quality, not slippage

82% of trades are stops, and the model assumes a stop fills exactly at its −30% trigger.
It also assumes a TP fills exactly at 5x. Neither is verified — paper trading cannot
verify them.

| stop actually fills at | EV | | TP actually fills at | EV |
|---|---|---|---|---|
| 0.70x (modelled) | 1.128x ✅ | | 5.0x (modelled) | 1.128x ✅ |
| 0.60x | 1.051x ✅ | | 4.5x | 1.078x ✅ |
| **0.57x** | **breakeven** | | 4.0x | 1.028x ⚠ |
| 0.50x | 0.974x ❌ | | **3.8x** | **breakeven** |

**The edge dies if stops slip ~13pp past trigger, or if TPs fill ~25% below target.**
On memecoins dumping in ~1 minute, that is entirely plausible — this is a momentum
strategy exiting into the same momentum.

**Slippage is solved. Fill quality is not, and it is now the whole risk.** No amount of
further paper trading answers it; only a small number of real orders will.

---

## ⚠ FILL QUALITY RESOLVED (2026-09-05) — and it changes who can trade this

The open risk was "stops fill worse than the −30% trigger." It splits into two very
different things, and they have opposite answers. Measured on **30,416 real fills**
across 14 graduated pools.

### 1. Liquidity slippage — a non-issue

Actual sell fills at ~$5–120 size, versus the immediately preceding print:

| | |
|---|---|
| median | **+0.405%** (better than prior print) |
| p10 / p05 | −0.805% / −1.673% |
| **fills >13pp worse** | **0.02%** |

At this size the book absorbs you. This was the fear and it is not real.

### 2. Latency gap — this is the killer

How far price moves **while your transaction is pending**:

| delay | median | p10 | chance of >13pp adverse |
|---|---|---|---|
| 1.0s | −0.15% | −11.5% | **8.3%** |
| 3.0s | −0.25% | −17.6% | **15.1%** |
| 6.1s | −0.62% | −23.0% | **20.6%** |
| 12.1s | −1.03% | −31.8% | **25.8%** |

### EV against execution latency

Applying the observed gap distribution to all 735 paper trades:

| latency | EV/trade | |
|---|---|---|
| instant (backtest assumption) | 1.128x | — |
| 1.0s | **1.065x** | profit |
| 3.0s | **1.052x** | profit |
| 6.1s | **1.036x** | profit |
| **12.1s** | **0.991x** | **loss** |
| 30.3s | 0.943x | loss |

**Breakeven is ~8–10 seconds of total execution latency.**

### What this means

**Strategy B is not manually executable.** Seeing an alert, opening Fomo, finding the
token, entering size, confirming and signing is comfortably 20–60s — squarely in the
negative-EV zone. The entire edge lives in the first few seconds after graduation.

Three honest options:

1. **Automate it.** Requires execution code, a hot key and real capital risk — a
   different project with a different risk profile, and explicitly out of scope so far.
2. **Don't trade Strategy B manually.** Use the monitor/investigator for what they are
   genuinely good at: refusing bad entries. Avoiding a −30% stop is worth more than
   catching a marginal one.
3. **Trade the slow tail instead.** ~29% of graduated tokens peak more than an hour
   after graduation. Those are latency-insensitive and *are* manually tradeable. That
   subset has not been backtested as its own strategy — it is the obvious next test.

The tooling keeps its value in all three cases. The *momentum* strategy does not survive
manual execution, and no amount of better alerting fixes an 8-second budget.
