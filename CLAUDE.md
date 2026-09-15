# Hood Sniper — CLAUDE.md

**Purpose:** AI-assistant context for building, running, and extending Hood Sniper.
**Status:** v0.6 ROUGH DRAFT — 2026-08-31 (corrected exit tiers to Slim's stated $1M+ reality)
**Type:** Launch sniper filtered by deployer bonding-tier reputation
**Direction:** Long (entry at launch, sub-$100K mcap)
**Venue:** Robinhood Chain (chain ID 4663)

---

## Project at a Glance

Hood Sniper detects **new token launches on Robinhood Chain** from deployer wallets that have shipped bonding successes before. Tokens launch via bonding-curve mechanics (pump.fun-style); most die in hours. A small fraction bond all the way up — surviving 7-30 days, hitting $3-10M mcap, and rarer still hitting $50M+ and fully migrating to a DEX.

Hood Sniper scores **deployer wallets** by their past bonding-tier achievements, then surfaces only the launches from proven shippers. Combined with social-signal confirmation (X, YouTube, Fomo, Telegram), every signal includes a full DYOR pack so Slim can verify in 30 seconds.

**One-liner:** *Watch every launch on Robinhood Chain. Snipe the ones from devs who've bonded tokens to $3M+ before.*

---

## Vault Protocol (READ FIRST)

Per the Second Brain vault protocol:
1. **API keys** (Robinhood Chain RPC, DexScreener, GeckoTerminal, X/Twitter, YouTube, Telegram, Fomo scraper) MUST go into this project's `.env` file, then be logged to `/Users/mainfolder/Documents/Hermes Second Brain/projects/api-keys/<service>.md` with date.
2. **Privacy reminder:** Before pasting any secret, switch to local model (`/model qwen`) so it stays off the cloud.

---

## Thesis (short version)

See `thesis.md` for full version. Core insight:

- Most launches rug within hours. Bonding metrics (survived 7-30d, hit $3-10M, hit $50M+, migrated) separate winners from noise.
- **Deployer wallet is the filter, not the token.** A wallet that has shipped multiple bonding tokens is much more likely to ship another.
- **Score formula:** `win_score = (survived*1 + bonded_mid*2 + bonded_high*5 + migrated*3 - rugged*3 - copy_cat*1) / total_launches`
- **Top-dev threshold:** `win_score >= 2.0` across `>= 3 launches`.
- **Entry:** sub-$100K mcap (sometimes sub-$50K), within 30-90s of launch.
- **Exit:** TP ladder at $3M / $10M / $50M+, trail the runners.
- **Stops:** -25% hard stop, -7d time stop.
- **Sizing:** tiny — $20-50 per entry. Winners are 50-120x, so even 15-20% hit rate = massive EV.
- **Social signal layer:** X / YouTube / Fomo / Telegram confirm at-launch hype + DYOR links.

---

## Tech Stack

- **Language:** Python 3.12+
- **Chain:** Robinhood Chain (chain ID 4663, Arbitrum Orbit L2)
- **Data sources:**
  - **Robinhood Chain RPC** — `PairCreated` events, deployer history, holder counts, bonding curve state.
  - **DexScreener** (`chainId=robinhood`) — pair metadata, social links, OHLCV.
  - **GeckoTerminal** (`/networks/robinhood/pools`) — historical pair creation data, OHLCV.
  - **X API v2** (`https://api.x.com/2/tweets/search/recent`) — Free tier 10K posts/month. Basic $200/mo for 500K.
  - **YouTube Data API v3** — Free, 10K units/day, `search.list` = **100 units/call** (~100 searches/day).
  - **Fomo app** — No public API. Approach: X proxy for "fomo app" mentions.
  - **Telegram** — MTProto via `telethon` for alpha channel scraping.
  - **Robinhood Chain Blockscout** (`robinhoodchain.blockscout.com`) — contract verification.
- **Execution:**
  - **web3.py** for event subscription + tx signing.
  - Private key in `.env` (NOT committed).
- **Storage:** SQLite for deployer scores, pair events, fills, journal.
- **Notifications:** Telegram (Slim DM, chat_id 8273896884) with full DYOR pack per signal.
- **Config:** `.env` file (`python-dotenv`).

---

## Repository Layout (Target)

```
hood-sniper/
├── .env.example                    # API key template (no secrets)
├── .gitignore
├── requirements.txt
├── README.md                       # project hub
├── CLAUDE.md                       # this file
├── thesis.md                       # strategy thesis
├── config.py                       # loads .env, exposes constants
├── sniper/
│   ├── __init__.py
│   ├── chain_listener.py           # subscribes to PairCreated on RH Chain factories
│   ├── deployer_indexer.py         # builds deployer history DB (backfill on first run)
│   ├── deployer_scorer.py          # computes win_score from bonding-tier history
│   ├── cluster_detector.py         # identifies sybil / cluster-funded deployers
│   ├── bonding_tracker.py          # tracks per-token bonding-tier status over time
│   ├── x_monitor.py                # X mention velocity + sentiment
│   ├── youtube_monitor.py          # YouTube search + cache (quota-aware)
│   ├── fomo_monitor.py             # Fomo trending detection via X proxy
│   ├── telegram_monitor.py         # Telegram alpha channel scraping
│   ├── social_scorer.py            # computes social_multiplier
│   ├── dyor_linker.py              # fetches + formats DYOR links for alerts
│   ├── token_filter.py             # honeypot / mint authority / holder checks
│   ├── entry_executor.py           # signs + broadcasts the buy tx
│   ├── exit_manager.py             # TP ladder + SL + time stop
│   ├── db.py                       # SQLite schema
│   ├── journal.py                  # signal + trade journal
│   └── notifier.py                 # Telegram alerts with DYOR pack
├── scripts/
│   ├── backfill_deployers.py       # build initial deployer history DB
│   ├── backtest_bonding_filter.py  # validate deployer-rep on historical launches
│   └── journal_review.py
└── tests/
    ├── test_deployer_scorer.py
    ├── test_cluster_detector.py
    ├── test_bonding_tracker.py
    ├── test_social_scorer.py
    └── test_exit_manager.py
```

---

## Component Details

### Component 1 — Chain Listener (`sniper/chain_listener.py`)

**Job:** Subscribe to `PairCreated` events on all DEX factories deployed to Robinhood Chain.

**Sources:** Uniswap V2/V3/V4 factories + Pons, Lighter, NOXA Fun, etc. + **bonding curve contracts** (for tokens that haven't migrated yet).

```python
from web3 import Web3

w3 = Web3(Web3.HTTPProvider(RPC_URL))
factory = w3.eth.contract(address=FACTORY_ADDR, abi=FACTORY_ABI)

event_filter = factory.events.PairCreated.create_filter(fromBlock='latest')
for event in event_filter.get_new_entries():
    token0, token1, pair, all_pools_length = event.args.values()
    deployer = event['from']  # EOA that sent the tx
```

**Output:** `pair_events (pair_address, token0, token1, factory, deployer, block_number, tx_hash, created_at)` table.

**Also listen for:** bonding curve buy/sell events to track live mcap progression.

### Component 2 — Deployer Indexer (`sniper/deployer_indexer.py`)

**Job:** Build and maintain a database of every deployer wallet's launch history with bonding-tier annotations.

**Backfill on first run:** iterate all `PairCreated` events on RH Chain since genesis. For each token:
1. Record the deployer.
2. Track max market cap reached (peak mcap during bonding / post-migration).
3. Determine highest bonding tier achieved: survived (still alive 7-30d), bonded-mid ($3-10M), bonded-high ($50M+), migrated (full graduation).
4. Flag as rugged if token died in <7d.
5. Flag as copy-cat if ticker matches an existing token.

**Output:** `deployers (address, first_seen_at, total_launches, survived_count, bonded_mid_count, bonded_high_count, migrated_count, rugged_count, copy_cat_count, win_score, last_updated)` table.

### Component 3 — Deployer Scorer (`sniper/deployer_scorer.py`)

**Job:** Compute `win_score` for each deployer.

```python
def win_score(deployer):
    launches = deployer.total_launches
    if launches < MIN_LAUNCHES:
        return 0.0  # not enough history
    return (
        deployer.survived_count       * 1
      + deployer.bonded_mid_count     * 2
      + deployer.bonded_high_count    * 5
      + deployer.migrated_count       * 3
      - deployer.rugged_count         * 3
      - deployer.copy_cat_count       * 1
    ) / launches
```

**Config:**
- `MIN_LAUNCHES = 3` (need history before scoring)
- `SCORE_THRESHOLD = 2.0` (above this = snipe candidate)
- `MAX_RUG_RATE = 0.5` (auto-reject if >50% of past launches rugged)

### Component 4 — Cluster Detector (`sniper/cluster_detector.py`)

**Job:** Identify wallets controlled by the same entity (sybil deployers).

**Signals:** common funder, identical bytecode, coordinated timing, same LP locker.

**Why it matters:** one bad actor with 5 wallets, each with "1 mid-bond + 1 rugged" looks like 5 separate decent deployers. Cluster detection collapses them into one entity.

### Component 5 — Bonding Tracker (`sniper/bonding_tracker.py`)

**Job:** Periodically poll each active launch's bonding curve state (mcap, holder count, time since launch) and update the deployer's bonding-tier annotations as tokens mature.

**Cadence:** every 5-15 min for active launches, every 6h for older ones.

**Tier transitions tracked:**
- Survived: T+7d alive (was potentially dead)
- Bonded-mid: mcap crossed $3M
- Bonded-high: mcap crossed $50M
- Migrated: bonding curve complete, liquidity on DEX
- Rugged: mcap dropped to <5% of peak OR liquidity removed

### Component 6 — X Monitor (`sniper/x_monitor.py`)

**Job:** Track X mentions of each snipe candidate ticker.

**Queries:**
```python
mentions_1h  = count(tweets matching "$TICKER OR #TICKER" since 1h ago)
mentions_24h = count(tweets matching same query last 24h)
zscore = (mentions_1h - mentions_24h/24) / std(rolling_24h_counts)
```

**Sentiment:** keyword count (+1 for moon/gem/alpha; −1 for rug/scam/dump).

**DYOR output:** top 5 highest-engagement tweets with `https://x.com/<author>/status/<tweet_id>` links.

**API tier:** start Free (10K/month). Upgrade to Basic ($200/mo) only if quota insufficient.

### Component 7 — YouTube Monitor (`sniper/youtube_monitor.py`)

**Job:** Find recent YouTube videos mentioning the ticker.

**Critical constraint:** `search.list` = **100 units/call**. Default quota = ~100 searches/day max.

**Strategy:** only search when X mentions spiking OR deployer score borderline OR manual `/yt <ticker>` trigger.

```python
youtube.search().list(
    q=f"{ticker} robinhood chain crypto",
    part="snippet",
    maxResults=5,
    order="date",
    type="video",
    publishedAfter=(now - 24h).isoformat() + "Z"
)
```

**Cache:** results per ticker for 6h.

**DYOR output:** top 3 videos with `https://youtube.com/watch?v=<video_id>` + channel + view count.

### Component 8 — Fomo Monitor (`sniper/fomo_monitor.py`)

**Job:** Detect if ticker is trending on Fomo app.

**No public API.** Recommended: monitor X for `"fomo app" OR "fomo.bot" $TICKER` mentions → proxy for trending tab awareness.

**Alt (fragile):** scrape public Fomo trending HTML page.

### Component 9 — Telegram Monitor (`sniper/telegram_monitor.py`)

**Job:** Watch alpha channels on Telegram via MTProto (`telethon`).

**Channels to watch:** RH Chain alpha groups, memecoin alpha groups, Fomo-related channels.

**Per-ticker capture:** mention count per channel (1h, 24h), unique author count, forward count.

**Caution:** Telegram ToS restricts scraping. Use session auth carefully. Prefer public channels.

### Component 10 — Social Scorer (`sniper/social_scorer.py`)

**Job:** Compute `social_multiplier` from all social sources.

```python
def social_multiplier(ticker):
    mult = 1.0
    if x_mention_velocity_zscore(ticker) > 2:
        mult += 0.5
    if youtube_video_count_24h(ticker) >= 1:
        mult += 0.3
    if fomo_app_trending(ticker):
        mult += 0.2
    if telegram_active_users_zscore(ticker) > 1.5:
        mult += 0.1
    if negative_sentiment_ratio(ticker) > 0.4:
        mult -= 0.3
    return max(mult, 0.1)  # floor to avoid zero
```

**Decision rule:** alert if `win_score(deployer) >= 2.0` AND social signals align (multiplier >= 1.3) AND token-side filters pass.

### Component 11 — DYOR Linker (`sniper/dyor_linker.py`)

**Job:** Aggregate all DYOR links for a single signal and format the Telegram alert.

**Per-signal output includes:**
- Token contract + DexScreener + Blockscout + bonding curve link.
- Deployer history summary (past launches + tiers).
- Top 5 X tweets (link + author + engagement).
- Top 3 YouTube videos (link + channel + views).
- Combined signal breakdown.

### Component 12 — Token Filter (`sniper/token_filter.py`)

**Job:** Per-token hard gates before alerting / committing capital.

**Required:** contract verified, mint renounced, LP locked (or curve-locked), top 10 holders <60%, honeypot check passes (`eth_call` fork).

**Reject:** honeypot, mint not renounced, blacklist/setFee in contract, top 10 >80%, deployer fails scorer.

### Component 13 — Entry Executor (`sniper/entry_executor.py`)

**Job:** Sign and broadcast the buy.

```python
from web3 import Web3

w3 = Web3(Web3.HTTPProvider(RPC_URL))
router = w3.eth.contract(address=ROUTER_ADDR, abi=ROUTER_ABI)

tx = router.functions.swapExactETHForTokens(
    min_out,           # slippage protection
    [WETH, token_addr],
    wallet.address,
    int(time.time()) + 60
).build_transaction({
    'from': wallet.address,
    'value': position_size_wei,
    'gas': 500_000,
    'gasPrice': w3.to_wei(gas_gwei, 'gwei'),
    'nonce': w3.eth.get_transaction_count(wallet.address),
})

signed = wallet.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
```

**Sizing:** $20-50 per entry (4-10% of $500-1K bankroll). Many small attempts, not concentrated bets.

**Slippage:** 5% on entry (memecoins volatile at T+0). Tighten to 2% if liquidity >$50K.

### Component 14 — Exit Manager (`sniper/exit_manager.py`)

**Job:** Close at bonding-tier targets / SL / time stop.

**TP ladder (Slim's actual exit tiers — money at every rung):**
- **TP1:** 50% out at **$1M-$1.5M mcap** (regular win, lock it in).
- **TP2:** 30% out at **$3M-$5M mcap** (the sweet spot).
- **TP3:** 20% out at **$10M+ mcap OR pre-migration peak** (moonshot, ride until momentum breaks).

**Hit rate reality by tier (with deployer-rep filter):**
- $1M+ mcap: ~20-35% (realistic edge)
- $3M+ mcap: ~10-20% (stated target)
- $50M+ mcap: ~1-3% (lottery)

**Stops:**
- Hard SL: -25% from entry.
- Time stop: -7 days if no bonding progress.
- Exit all: dev wallet dumps their allocation OR holder concentration >80%.

---

## Detection → Alert → Entry Flow

1. **Chain listener** detects new pair / bonding curve buy.
2. **Deployer scorer** computes `win_score` for the deployer.
3. **Token filter** runs honeypot / mint / LP / holder checks.
4. **Social sources** (X / YouTube / Fomo / Telegram) compute `social_multiplier`.
5. **DYOR linker** builds full Telegram alert with clickable links.
6. **Notifier** sends to Slim DM.
7. Manual: Slim clicks links, verifies in 30s, decides to buy.
8. Live mode (optional later): entry executor buys within 60s.

---

---

## Rug Rate Reality (Why The Deployer Filter Is Mandatory)

**Without any filter on Robinhood Chain / pump.fun-style chains:**
- ~98.5% - 99.7% of tokens never graduate the bonding curve to a DEX.
- ~99% of launches show pump-and-dump or rug characteristics (Solidus Labs data).
- Robinhood Chain peaked at ~18,600 launches/day. First honeypot ($RIALTO) on Day 2. Documented "vanishing tokens" scam wave in mid-July 2026.
- Median rug size: ~$2,800-$3,000 (per Solidus Labs Raydium study).

**With the deployer-rep filter:**
- Excludes fresh anonymous wallets (the source of most rugs).
- Excludes serial ruggers (cluster detection collapses sybils).
- Excludes copy-cat tickers.
- Remaining candidates: top-decile deployers who've shipped bonding successes.

**Realistic hit rate after all filters:**
- 15-25% for tokens hitting $3M+ mcap.
- 2-5% for tokens hitting $50M+ mcap.

**Why the math still works:** even at 15% hit rate with 50-120x winners, 100 trades yields massive positive EV IF the filter actually works. **If the filter doesn't work, the math collapses to ~break-even or negative.**

**Phase 0 is non-negotiable** — must backtest `win_score >= 2.0` against historical launches and confirm precision >30% before any capital deployment.

---

## Build Phases

### Phase 0 — Validate the deployer-rep filter (before any execution code)
1. Backfill all `PairCreated` events from RH Chain since launch.
2. For each token, determine its highest bonding tier achieved (survived / bonded-mid / bonded-high / migrated / rugged).
3. Build deployer history DB.
5. Run `scripts/backtest_bonding_filter.py`: replay 60-90 days of launches, see if `win_score >= 2.0` correlates with new launches hitting $3M+ / $50M+.
6. **Decision gate:** if precision <30%, stop. The filter doesn't work.

### Phase 1 — Listener + Indexer + Scorer + Bonding Tracker (Week 1)
1. `chain_listener.py` — subscribe to `PairCreated` + bonding curve events.
2. `deployer_indexer.py` — build DB with bonding-tier annotations.
3. `deployer_scorer.py` — score formula.
4. `cluster_detector.py` — sybil heuristics.
5. `bonding_tracker.py` — periodic mcap polling + tier updates.
6. Smoke test: live scoring of new launches, Telegram ping on top-dev candidates.

### Phase 2 — Social Signal Layer (Week 2)
1. `x_monitor.py`, `youtube_monitor.py`, `fomo_monitor.py`, `telegram_monitor.py`, `social_scorer.py`, `dyor_linker.py`.
2. Wire into alert pipeline.

### Phase 3 — Token Filter (Week 3)
1. `token_filter.py` — honeypot + mint + LP + holder checks.
2. Wire into alert pipeline: deployer score → token filter → social signal → alert.

### Phase 4 — Manual Trading (Week 4+)
1. Run alerts in Slack/Telegram for several weeks. Slim manually clicks DYOR links and enters via RH Chain wallet.
2. Log every alert + Slim's manual entry + outcome.
3. Validate: hit rate, avg P&L, max drawdown.

### Phase 5 — Optional Automation (later)
1. Only after manual phase validates the edge: wire `entry_executor.py` + `exit_manager.py`.
2. Start at $200 bankroll, max 1 concurrent position, paper-trade mode first.
3. After 30+ trades with hit rate ≥15-20% (note: lower than short strategies because winners are bigger) and PF ≥2.0 → size up.
4. Ramp: $200 → $500 → $1K.

---

## Critical Constraints (READ THESE)

1. **Run Phase 0 first.** Do NOT write execution code until the backtest validates the deployer-rep filter. If `win_score >= 2.0` doesn't predict bonding-tier outcomes, the whole thesis fails.

2. **Cluster detection is mandatory.** Without it, sybil deployers will poison the scoring.

3. **Liquidity exit risk.** Exiting a small-cap position at $3M means selling 60x of current mcap. Scale out via TP ladder, not all-at-once.

4. **Top-dev rugs are real.** Proven devs CAN still rug. Per-launch honeypot check still required.

5. **Honeypot check is critical.** Always simulate the sell via `eth_call` before buying.

6. **YouTube search quota is 100 units/call.** Only search on signal triggers, not on every launch.

7. **X API tier:** start Free (10K/month), upgrade to Basic ($200/mo) only if quota insufficient.

8. **Telegram scraping ToS risk.** Use carefully. Prefer public channels.

9. **Do NOT auto-size-up.** Phase ramps only with explicit manual approval.

10. **L2 risk carries.** Robinhood Chain is below L2Beat Stage 0.

11. **Disclosure on alerts.** Every Telegram alert must flag: "top-dev candidate, not financial advice, most of these will die, tight stops."

---

## Failure Modes to Handle

| Failure | Detection | Response |
|---|---|---|
| RH Chain RPC drops events | listener missed blocks | Backfill from last block + alert |
| Honeypot false negative | post-buy sell fails | SL triggered; log for honeypot DB update |
| Front-run badly | tx underpriced, never lands | Bump gas next iteration; consider private RPC |
| Deployer sybil detected post-entry | cluster updated | Manual review; consider early exit |
| Top-dev rugs their new launch | LP removed / mcap dumps | Exit all immediately |
| Liquidity rug mid-trade | pool reserves drop to 0 | Exit at market, accept loss |
| X API rate limit (429) | HTTP 429 | Exponential backoff + alert |
| YouTube quota exhausted | quotaExceeded error | Switch to manual trigger mode only |
| Telegram session banned | auth error | Rotate session, alert |
| Social signal false positive (coordinated pump) | sentiment ratio catch | Alert flagged |
| Telegram alert spam | same deployer fires 5x in 1h | Cooldown per deployer per hour |

---

## Open Questions for Slim

1. **Min deployer history:** `MIN_LAUNCHES = 3` reasonable?
2. **Score threshold:** `SCORE_THRESHOLD = 2.0` initial — calibrate from backtest?
3. **Cluster detection depth:** common funder only, or deeper (bytecode, timing)?
4. **Slippage tolerance:** 5% on entry OK, or tighter?
5. **Max position count:** unlimited small ($20-50) entries OK, or cap concurrent positions?
6. **TP ladder specifics:** 50% at $3-5M / 30% at $10M / 20% at $50M — adjust?
7. **Time stop:** -7d or -14d?
8. **Bankroll start:** $200 / $500 / $1K?
9. **Manual phase duration:** 2 weeks of alerts before considering automation?
10. **X API tier:** start Free, go Basic immediately, or wait?
11. **YouTube search trigger:** X-spike only, manual-only, or every top-dev launch?
12. **Telegram channel list:** which alpha channels to monitor?
13. **Fomo detection method:** X proxy, HTML scrape, or both?

---

## Companion Files

- `thesis.md` — full strategy thesis
- `requirements.txt` — Python deps (`web3`, `requests`, `python-dotenv`, `telethon`, `sqlite3`)
