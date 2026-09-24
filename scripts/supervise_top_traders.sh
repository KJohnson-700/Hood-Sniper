#!/bin/bash
# Rebuild the smart-money list every 30 minutes.
#
# WHY THIS EXISTS. The list was built ONCE, on 2026-09-11, and nothing ever rebuilt
# it. Twelve days later its 243 wallets had a MEDIAN 316.7 hours since their last
# trade, only 1 of 243 had traded in the previous 15 minutes, and the star column
# was empty on every row of the board. A freshly built list shared just 9 of those
# 243 wallets.
#
# The selection method was never the problem -- out-of-sample, on 10,078 picks made
# after the list was built, those wallets still hit 2x 41.7% of the time against a
# 22.8% base. Memecoin traders simply rotate wallets, so the list decays fast and
# does so SILENTLY: a stale list and a working one look identical on screen.
#
# Same shape as the other supervisors, and for the same reason: macOS cron needs
# Full Disk Access and fails silently without it, while a loop started from an
# interactive shell inherits the grant.
#   restart: nohup bash scripts/supervise_top_traders.sh >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/top_traders.log"
while true; do
  BEFORE=$(python3 -c "import json;print(len(json.load(open('data/top_traders.json'))))" 2>/dev/null || echo 0)
  echo "[$(date -u +%FT%TZ)] rebuild start (current=$BEFORE)" >> "$LOG"
  python3 scripts/top_traders.py --export >> "$LOG" 2>&1
  RC=$?
  AFTER=$(python3 -c "import json;print(len(json.load(open('data/top_traders.json'))))" 2>/dev/null || echo 0)
  # report the OUTCOME, not the exit code. A zero return that leaves the list
  # unchanged is exactly how this went stale for twelve days without a warning.
  if [ "$AFTER" -gt 0 ]; then
    echo "[$(date -u +%FT%TZ)] OK rc=$RC wallets=$AFTER (was $BEFORE)" >> "$LOG"
  else
    echo "[$(date -u +%FT%TZ)] EMPTY LIST rc=$RC — smart money is now blind" >> "$LOG"
  fi
  sleep 1800
done
