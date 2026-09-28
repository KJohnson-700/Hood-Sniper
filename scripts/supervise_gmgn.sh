#!/bin/bash
# GMGN trenches, polled at a cadence matched to how fast each stage actually moves.
#
# MEASURED: one CLI call is ~0.6-0.9s, and 12 back-to-back calls drew no rate
# limiting, so throughput is not the constraint -- politeness is. The waste in a
# flat 5-minute poll was not volume, it was polling `completed` (an already-
# graduated reference list) exactly as often as `new_creation`, where an entry
# decision actually lives.
#
#   fast  new_creation + near_completion   every 60s   (6 calls)  = 360 calls/hr
#   slow  completed                        every 10m   (3 calls)  =  18 calls/hr
#
# ~378 calls/hr against the old 108 -- 4x fresher on everything actionable, and
# still under 10k/day. If GMGN ever does start refusing, BACKOFF below doubles the
# interval rather than hammering a limit.
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/gmgn_source.log"
FAST=60
SLOW=600
last_slow=0
backoff=1
while true; do
  BEFORE=$(wc -l < data/gmgn_feed.jsonl 2>/dev/null | tr -d ' '); BEFORE=${BEFORE:-0}
  now=$(date +%s)
  if [ $((now - last_slow)) -ge $SLOW ]; then
    STAGES="new_creation near_completion completed"; last_slow=$now
  else
    STAGES="new_creation near_completion"
  fi
  python3 scripts/gmgn_source.py --snapshot --stages $STAGES >> "$LOG" 2>&1
  AFTER=$(wc -l < data/gmgn_feed.jsonl 2>/dev/null | tr -d ' '); AFTER=${AFTER:-0}
  if [ "$AFTER" -gt "$BEFORE" ]; then
    echo "[$(date -u +%FT%TZ)] OK +$((AFTER-BEFORE)) rows [$STAGES]" >> "$LOG"
    backoff=1
  else
    # nothing written is a failure even at exit 0 -- back off instead of retrying
    # into whatever is refusing us
    backoff=$((backoff * 2)); [ $backoff -gt 16 ] && backoff=16
    echo "[$(date -u +%FT%TZ)] NO NEW ROWS — backing off to $((FAST*backoff))s" >> "$LOG"
  fi
  sleep $((FAST * backoff))
done
