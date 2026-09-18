#!/bin/bash
# Snapshot StonkFun's launchable pair list every 10 minutes.
#
# WHY A LOOP AND NOT A BACKFILL: the pairs endpoint returns CURRENT state only, so
# the moment a new pair becomes launchable is not recoverable after the fact. Pair
# novelty is the operator's own thesis and it can only be observed forward. The
# token backfill gives a rough first-use date per pair; this gives the exact
# arrival, which is the sharper signal.
#
# Same shape as the other supervisors, and for the same reason: macOS cron needs
# Full Disk Access and fails SILENTLY without it, while a loop started from an
# interactive shell inherits the grant.
#   restart: nohup bash scripts/supervise_stonkfun_pairs.sh >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/stonkfun_pairs.log"
while true; do
  BEFORE=$(wc -l < data/stonkfun_pairs.jsonl 2>/dev/null | tr -d ' ')
  BEFORE=${BEFORE:-0}
  echo "[$(date -u +%FT%TZ)] snapshot start (snapshots=$BEFORE)" >> "$LOG"
  python3 scripts/stonkfun.py --pairs >> "$LOG" 2>&1
  RC=$?
  AFTER=$(wc -l < data/stonkfun_pairs.jsonl 2>/dev/null | tr -d ' ')
  AFTER=${AFTER:-0}
  # report the OUTCOME, not the exit code -- a zero return with no new snapshot is
  # still a failure, and that distinction has hidden a stalled job here before
  if [ "$AFTER" -gt "$BEFORE" ]; then
    echo "[$(date -u +%FT%TZ)] OK rc=$RC total=$AFTER" >> "$LOG"
  else
    echo "[$(date -u +%FT%TZ)] NO NEW SNAPSHOT rc=$RC — check the API" >> "$LOG"
  fi
  sleep 600
done
