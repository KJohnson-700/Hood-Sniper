#!/bin/bash
# Snapshot PRIME's calls every 2 minutes so the out-of-sample record accumulates.
# Same shape as the other supervisors, and for the same reason: macOS cron needs
# Full Disk Access and fails SILENTLY without it, while a loop started from an
# interactive shell inherits the grant.
#   restart: nohup bash scripts/supervise_prime_audit.sh >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/prime_audit.log"
while true; do
  BEFORE=$(wc -l < data/prime_audit.jsonl 2>/dev/null | tr -d ' ')
  /usr/bin/python3 -u scripts/prime_audit.py --snapshot >> "$LOG" 2>&1
  AFTER=$(wc -l < data/prime_audit.jsonl 2>/dev/null | tr -d ' ')
  echo "[$(date -u '+%FT%TZ')] +$(( ${AFTER:-0} - ${BEFORE:-0} )) (total ${AFTER:-0})" >> "$LOG"
  sleep 120
done
