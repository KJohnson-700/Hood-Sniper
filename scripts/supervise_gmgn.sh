#!/bin/bash
# Snapshot GMGN's trenches feed across all three chains every 5 minutes.
#
# This is a SECOND, INDEPENDENT net. Our own collectors stay the primary record --
# GMGN is a third party with its own indexing lag, and this project has been burned
# three times by treating a vendor's healthy-looking response as truth. The value is
# that it sees what our chain listeners miss, and that disagreement between the two
# is itself information.
#
# Reports the OUTCOME (did the feed grow) rather than the exit code, because a
# process that runs and writes nothing is still a failure.
#   restart: nohup bash scripts/supervise_gmgn.sh >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/gmgn_source.log"
while true; do
  BEFORE=$(wc -l < data/gmgn_feed.jsonl 2>/dev/null | tr -d ' '); BEFORE=${BEFORE:-0}
  echo "[$(date -u +%FT%TZ)] snapshot start (feed=$BEFORE)" >> "$LOG"
  python3 scripts/gmgn_source.py --snapshot >> "$LOG" 2>&1
  RC=$?
  AFTER=$(wc -l < data/gmgn_feed.jsonl 2>/dev/null | tr -d ' '); AFTER=${AFTER:-0}
  if [ "$AFTER" -gt "$BEFORE" ]; then
    echo "[$(date -u +%FT%TZ)] OK rc=$RC +$((AFTER-BEFORE)) rows" >> "$LOG"
  else
    echo "[$(date -u +%FT%TZ)] NO NEW ROWS rc=$RC — check the API key / CLI" >> "$LOG"
  fi
  sleep 300
done
