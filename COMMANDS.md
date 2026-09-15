# Hood Sniper — command reference

All paths need quotes; the folder name has a space in it.

---

## 1. Watch launches (Robinhood Chain) — the main screen

```bash
"/Users/mainfolder/Documents/Hood Sniper/scripts/launch_monitor.py"
```

Read-only. `[b]` builds and *shows* a real transaction; `[y]` refuses to sign.
Add `--arm` only when you actually intend to trade.

**Useful flags**
| flag | default | meaning |
|---|---|---|
| `--watch HOOJA,PEPE` | – | alert on these tickers. Matches `$HOOJA` too |
| `--arm` | off | allow `[y]` to sign and send |
| `--stake 25` | 25 | USD per trade (hard cap 25) |
| `--hot-at 4` | 4 | validated wallets that trigger the flashing HOT banner |
| `--max-creator-tax-bps 400` | 400 | hard-block at/above 4% creator tax per side |
| `--max-gas-pct 8` | 8 | refuse if this trade's own gas exceeds 8% of stake |
| `--no-autovet` | off | disable background DYOR (faster, less useful) |

### Keys
| key | does |
|---|---|
| `↑ ↓` or `j k` | move selection |
| `i` or `⏎` | investigate selected token (full DYOR) |
| `.` | **jump to the flashing HOT token** and investigate it |
| `f` | cycle filter: all → TRADEABLE → **ACTIONABLE** |
| `b` | build a buy — shows size, minOut, all-in cost. Never sends |
| `y` | sign and send the armed buy (needs `--arm`) |
| `n` | cancel the armed buy |
| `g` | jump to newest row |
| `?` | show/hide the key list in-app |
| `q` then `y` | quit — **asks for confirmation** |

### Reading the table
`hp` honeypot · `tax` creator tax (**green 1–2% is the best-performing band**, flashing
red ≥4% is blocked) · `smart` ★ validated wallets in · `slip%` your fill impact ·
`age` dimmed past 15 min · header shows **N ACTIONABLE** = rows that pass every gate.

**Do not copy text off the screen** — every keystroke is captured and a stray one can
close it. Addresses are in `data/smart_alerts.jsonl` as plain text.

---

## 2. Manage an open position (Robinhood Chain)

```bash
"/Users/mainfolder/Documents/Hood Sniper/scripts/exit_manager.py" --add 0xCURVE --entry-usd 25
"/Users/mainfolder/Documents/Hood Sniper/scripts/exit_manager.py" --watch --arm
```

Laddered TP defaults to **2x→50%, 3x→25%, 5x→25%** (`--ladder 2:50,3:25,5:25`).
Stop `--stop 0.7` · trailing `--trail 30` · time stop `--time-stop-min 960`.

**Live keys while watching:** `[e]` exit all · `[h]` half out · `[t]` trailing on/off ·
`[s]` status · `[q]` quit.

---

## 3. Watch a specific Base token (LAPTOP) — data only

```bash
"/Users/mainfolder/Documents/Hood Sniper/scripts/base_watch.py" \
  --token 0xB095274743941e953c746F9C228DA9c18Bb6ec29 --min-liq-usd 15000 --max-impact-pct 3
```

Tells you **which pool is real** when liquidity lands. 43 pools exist; most are squats
at 5–99% fees. Already running under a watchdog.

---

## 4. Research / maintenance

```bash
# investigate any token by hand
"/Users/mainfolder/Documents/Hood Sniper/scripts/investigate.py" 0xTOKEN

# keep the smart-money index current (runs on a 30-min loop already)
"/Users/mainfolder/Documents/Hood Sniper/scripts/holder_index.py" --update

# smart-money leaderboard
"/Users/mainfolder/Documents/Hood Sniper/scripts/trader_index.py" --top 25
```

---

## Files worth knowing
| file | what |
|---|---|
| `data/smart_alerts.jsonl` | every ★ alert, written the instant it fires |
| `data/trades.jsonl` | buys sent |
| `data/exits.jsonl` | sells and exit decisions |
| `data/paper_trades.jsonl` | live observational log |
| `logs/laptop_watch.log` | Base LAPTOP watcher |

## Restarting
Code changes need a restart — Python loads everything at launch. Background loops
(snapshots, holder index, LAPTOP watcher) keep running on their own and do not need it.

---

## BNB Chain (four.meme) — buy proven, sell proven, exit built

BSC went through the same order every chain here does: **prove the sell before arming
a buy.** Nothing is armed; `--arm` is opt-in and refuses to run without a key.

### Prove the exit route (no wallet, no capital)
```bash
"/Users/mainfolder/Documents/Hood Sniper/scripts/bsc_selltest.py"              # sample live curves
"/Users/mainfolder/Documents/Hood Sniper/scripts/bsc_selltest.py" --token 0x…  # one token
```
Fakes a balance and the TokenManager allowance with `eth_call` state overrides, then
runs the **real** sell encoder against a **real** curve. Result so far: **4/4 proven**,
across both BNB-quoted and USDT-quoted curves.

### Watch launches
```bash
"/Users/mainfolder/Documents/Hood Sniper/scripts/bsc_monitor.py" --verbose
"/Users/mainfolder/Documents/Hood Sniper/scripts/bsc_monitor.py" --all-quotes   # show untradeable ones too
```
Shows the **quote currency** per launch. By default it hides curves quoted in assets
the wallet does not hold — see the trap below.

### Manage a position
```bash
export HS_ADDRESS=0x…                       # read-only watching
"/Users/mainfolder/Documents/Hood Sniper/scripts/bsc_exit.py" --add 0x<token> --entry-usd 25
"/Users/mainfolder/Documents/Hood Sniper/scripts/bsc_exit.py" --watch            # DRY RUN
"/Users/mainfolder/Documents/Hood Sniper/scripts/bsc_exit.py" --watch --arm      # actually sells
```
Ladder 2x/50%, 3x/25%, 5x/25%; stop 0.65x; time stop 240min. Live keys while open:
`[e]` exit all · `[h]` half out · `[t]` cycle trailing stop · `[s]` status · `[q]` quit.

### Three BSC traps worth remembering

| trap | what happens | handled by |
|---|---|---|
| **1e9 granularity** | a sell whose size is not a multiple of 1e9 wei reverts `GW`. "Sell 100% of balance" reverts almost every time, at exit | `quantize_sell()` on every size |
| **the sell pulls** | TokenManager moves your tokens, so the token needs approving first. Approving at exit costs a block | approved at *registration*, not at exit |
| **exotic quote currencies** | only **~17%** of four.meme launches are BNB- or USDT-quoted (measured, 300 consecutive). The rest are quoted in tokenized equities (QQQB 18.7%, SPCXB 16.3%, GMEB, NVDAB…) — ten different assets, and the exit pays out in that asset, not money | `quote_tradeable()` gate — BNB and USDT only |

There is no quoter contract on four.meme, so price comes from the venue itself:
`quote_sell()` bisects the sell's own `minQuoteOut` argument in two batched round trips
(~3s). The largest floor the sell still clears **is** the realisable fill, fees and
creator tax included. Cross-checked against a live on-chain sell: quoter 3.42e-5 USDT
per token vs the real fill's 3.38e-5.

---

## Running it in tmux — so it can be restarted from anywhere

The monitor used to be trapped in whichever terminal launched it: every code change
meant switching to that window and relaunching by hand. Running it under tmux
decouples the process from the window.

```bash
"/Users/mainfolder/Documents/Hood Sniper/scripts/sniper.sh" start            # starts it AND opens it
"/Users/mainfolder/Documents/Hood Sniper/scripts/sniper.sh" start --watch HOOJA,PEPE
"/Users/mainfolder/Documents/Hood Sniper/scripts/sniper.sh" start -d         # background, no attach
```

`start` from a terminal **opens the bot right there** (`ctrl-b` then `d` to leave it
running). The first version started it detached and printed a line saying so, which
looked exactly like it had failed — nothing appeared, so the obvious next move was to
run `start` again, and "already running" then read like an error. It was telling the
truth. Now `start` shows you the thing it started, and `start` on an already-running
session attaches instead of complaining. From a script or a Claude session there is no
tty to attach to, so it stays in the background and says which command opens it.

| command | does |
|---|---|
| `sniper.sh start [flags]` | start it and open it here, remembering those flags (`-d` = background) |
| `sniper.sh restart` | relaunch in place, **reusing the saved flags** |
| `sniper.sh attach` | open it in this terminal (`ctrl-b` `d` to detach) |
| `sniper.sh peek` | print its current screen without attaching |
| `sniper.sh keys s` | press a key in it (`s` sort, `v` venue, `f` filter, `.` hot) |
| `sniper.sh status` / `stop` | is it up, with what flags / kill it |

Two things this buys:

* **Closing the terminal no longer kills the bot.** tmux keeps it alive; reattach later.
* **Restart is no longer yours to do.** Any shell — including a Claude session — can
  run `sniper.sh restart` after a code change.

`restart` deliberately replays the flags the session was STARTED with. A restart that
silently dropped `--watch` or `--arm` would leave you running a different bot than the
one you think is running.

**Why tmux and not launchd:** macOS denies `~/Documents` to launchd- and cron-spawned
children, silently — the trap already documented in `supervise_holder.sh`. tmux
started from an interactive shell inherits the grant.
