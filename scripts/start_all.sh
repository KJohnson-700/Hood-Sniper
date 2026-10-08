#!/bin/bash
# Start every collector the merged view depends on. Safe to re-run: anything already
# alive is left exactly as it is, so this is the one command to get the bot running
# and the one command to repair it after a reboot or a crash.
#
# WHY THIS EXISTS. Four chains are collected by four independent processes, on
# purpose -- launch_monitor is EVM down to its selectors and state-override probes,
# sol_monitor is not, and merging them would mean one chain's RPC outage taking
# down all four. The cost of that separation is that "run the bot" was four
# commands nobody could be expected to remember, and a single missing collector
# shows up on screen as a quiet market rather than as a missing feed.
#
#   bash scripts/start_all.sh          # start / repair everything
#   python3 scripts/allvenues.py       # the screen
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
mkdir -p logs

# up <pattern> -- is something matching this already running?
up() { pgrep -f "$1" >/dev/null 2>&1; }

# start <label> <pattern-to-test> <command...>
start() {
  local label="$1" pat="$2"; shift 2
  if up "$pat"; then
    echo "  [already up] $label"
  else
    nohup "$@" >/dev/null 2>&1 &
    sleep 1
    if up "$pat"; then echo "  [STARTED]    $label"
    else echo "  [FAILED]     $label — check logs/"; fi
  fi
}

echo "Hood Sniper — starting collectors"
echo

# --- Robinhood Chain: Pons + Bankr + o1 -------------------------------------
# Headless on purpose. The TUI needs a terminal, and the merged view reads the
# FEED, so the collector must run whether or not a screen is attached. Run the
# TUI yourself separately if you want the RHC panels.
start "RHC  (pons/bankr/o1)" "launch_monitor.py" \
      bash -c "cd '$PROJ' && exec python3 scripts/launch_monitor.py --no-tui >> logs/rhc.log 2>&1"

# --- BSC: flap.sh + four.meme ------------------------------------------------
# --all-quotes ON PURPOSE. Without it the collector DROPS every launch whose quote
# is not BNB/USDT before writing, so the feed contains only the survivors of the
# very filter you would want to measure -- asking it "what share of launches are
# BNB-quoted" then returns 100% by construction. It did: 27,064 rows over 7 days,
# all BNB/USDT, against a 2026-09-08 measurement of ~17% on 300 consecutive
# launches. Recording everything and letting the ALERT ROUTER filter on quote_why
# keeps alerting identical and makes the coverage question answerable.
start "BSC  (flapsh/four.meme)" "bsc_monitor.py" \
      bash -c "cd '$PROJ' && exec python3 -u scripts/bsc_monitor.py --verbose --all-quotes >> logs/bsc_collect.log 2>&1"

# --- Solana: pump.fun + StonkFun on-chain ------------------------------------
start "SOL  (pump.fun on-chain)" "supervise_sol_monitor.sh" \
      bash "$PROJ/scripts/supervise_sol_monitor.sh"

# --- Solana: StonkFun API (rank-on-pair signal) ------------------------------
start "SOL  (stonkfun watcher)" "supervise_stonkfun_watch.sh" \
      bash "$PROJ/scripts/supervise_stonkfun_watch.sh"
start "SOL  (stonkfun pairs)" "supervise_stonkfun_pairs.sh" \
      bash "$PROJ/scripts/supervise_stonkfun_pairs.sh"

# --- GMGN: cross-chain discovery (second, independent net) -------------------
start "GMGN discovery (3 chains)" "supervise_gmgn.sh" \
      bash "$PROJ/scripts/supervise_gmgn.sh"

# --- Discord alert router (per-chain channels) --------------------------------
start "alert router" "alert_router.py" \
      bash "$PROJ/scripts/supervise_alerts.sh"

# --- GMGN KOL trades: who the named callers are buying -----------------------
start "KOL tracker (callers)" "supervise_kol.sh" \
      bash "$PROJ/scripts/supervise_kol.sh"

# --- supporting indexes ------------------------------------------------------
start "smart-money rebuild" "supervise_top_traders.sh" \
      bash "$PROJ/scripts/supervise_top_traders.sh"
start "holder index" "supervise_holder.sh" \
      bash "$PROJ/scripts/supervise_holder.sh"

echo
echo "waiting 20s for feeds to move..."
sleep 20
echo
python3 - <<'PY'
import os, time
FEEDS = [("RHC  pons", "data/monitor_feed.jsonl"),
         ("BSC  flapsh", "data/bsc_feed.jsonl"),
         ("SOL  pump.fun", "data/sol_feed.jsonl"),
         ("SOL  stonkfun", "data/stonkfun_feed.jsonl")]
print("feed freshness:")
bad = 0
for lab, p in FEEDS:
    try:
        age = (time.time() - os.path.getmtime(p)) / 60
    except Exception:
        print(f"   {lab:16s} MISSING"); bad += 1; continue
    flag = "" if age < 5 else "   <-- STALE, collector not writing"
    if age >= 5:
        bad += 1
    print(f"   {lab:16s} {age:6.1f} min old{flag}")
print()
print("ALL FOUR FEEDS LIVE — run: python3 scripts/allvenues.py" if not bad
      else f"{bad} feed(s) not writing yet — give it a minute, then re-run this script")
PY
