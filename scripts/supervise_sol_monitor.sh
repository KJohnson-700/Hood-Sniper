#!/bin/bash
# Keep the Solana monitor (pump.fun + StonkFun on-chain) alive.
#
# WHY: it is now the highest-volume venue we track -- ~3,300 new tokens/hr against
# 365 on Pons -- and it was the ONLY collector with no supervisor. If it died,
# pump.fun went dark and the merged view would simply show fewer Solana rows, which
# is indistinguishable from a quiet market. That exact confusion has cost this
# project a 151-minute launch blackout and a 12-day-stale trader list.
#
# Reports the OUTCOME (did the feed grow) rather than the exit code, for the same
# reason: a process that is up and writing nothing is still a failure.
#   restart: nohup bash scripts/supervise_sol_monitor.sh >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/sol_monitor.log"
while true; do
  BEFORE=$(wc -l < data/sol_feed.jsonl 2>/dev/null | tr -d ' '); BEFORE=${BEFORE:-0}
  echo "[$(date -u +%FT%TZ)] start (feed=$BEFORE lines)" >> "$LOG"
  python3 scripts/sol_monitor.py --verbose >> "$LOG" 2>&1
  RC=$?
  AFTER=$(wc -l < data/sol_feed.jsonl 2>/dev/null | tr -d ' '); AFTER=${AFTER:-0}
  if [ "$AFTER" -gt "$BEFORE" ]; then
    echo "[$(date -u +%FT%TZ)] EXITED rc=$RC after writing $((AFTER-BEFORE)) rows — restarting" >> "$LOG"
  else
    echo "[$(date -u +%FT%TZ)] EXITED rc=$RC HAVING WRITTEN NOTHING — check the WS" >> "$LOG"
  fi
  sleep 10
done
