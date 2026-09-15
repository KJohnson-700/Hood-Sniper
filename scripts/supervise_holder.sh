#!/bin/bash
# Keep the holder index current. Same shape as supervise_snapshots.sh, and for the
# same reason: macOS cron needs Full Disk Access and fails SILENTLY without it, and a
# launchd agent's child has no TCC grant for ~/Documents (verified: exit 126). A loop
# started from an interactive session inherits that grant.
#   restart: nohup bash scripts/supervise_holder.sh >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/holder_update.log"
LOCK="$PROJ/logs/.holder.lock"
echo "[$(date -u '+%FT%TZ')] holder supervisor up (pid $$)" >> "$LOG"
while true; do
  if mkdir "$LOCK" 2>/dev/null; then
    BEFORE=$(wc -l < data/holder_events.jsonl 2>/dev/null | tr -d ' ')
    echo "[$(date -u '+%FT%TZ')] update start (events=${BEFORE:-0})" >> "$LOG"
    /usr/bin/python3 -u scripts/holder_index.py --update >> "$LOG" 2>&1
    RC=$?
    AFTER=$(wc -l < data/holder_events.jsonl 2>/dev/null | tr -d ' ')
    GREW=$(( ${AFTER:-0} - ${BEFORE:-0} ))
    # outcome, not exit code -- rc=0 with no new rows is a stall that reads as success
    if [ "$GREW" -gt 0 ]; then
      echo "[$(date -u '+%FT%TZ')] OK rc=$RC +$GREW events (total $AFTER)" >> "$LOG"
    else
      echo "[$(date -u '+%FT%TZ')] *** STALLED rc=$RC +0 events" >> "$LOG"
    fi
    rmdir "$LOCK" 2>/dev/null
  else
    echo "[$(date -u '+%FT%TZ')] SKIP: previous update still running" >> "$LOG"
  fi
  sleep 1800      # every 30 min; --update caps catch-up so a gap spreads over runs
done
