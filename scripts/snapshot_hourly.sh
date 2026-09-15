#!/bin/bash
# Hourly BSC outcome snapshot.
#
# NOT run from cron: /usr/sbin/cron on macOS needs Full Disk Access and without it
# fails SILENTLY -- an hourly job in a previous project produced zero log lines in
# 21 hours while running perfectly by hand. This runs under launchd instead, and
# every run stamps the log so "is it alive?" is answerable by looking, not assuming.
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
LOG="$PROJ/logs/snapshot.log"
LOCK="$PROJ/logs/.snapshot.lock"

cd "$PROJ" || exit 1

# Single-flight: a slow run must not overlap the next hour and interleave
# writes into the append-only jsonl.
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] SKIP: previous run still holding lock" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

START=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
BEFORE=$(wc -l < data/bsc_outcomes.jsonl 2>/dev/null | tr -d ' ')
echo "[$START] snapshot start (jsonl=${BEFORE:-0} lines)" >> "$LOG"

/usr/bin/python3 scripts/bsc_outcomes.py --snapshot >> "$LOG" 2>&1
RC=$?

AFTER=$(wc -l < data/bsc_outcomes.jsonl 2>/dev/null | tr -d ' ')
GREW=$(( ${AFTER:-0} - ${BEFORE:-0} ))

# Report the OUTCOME, not the exit code. A zero return with zero new rows is a
# failure that reads as success -- that is how an 11-hour stall got reported healthy.
if [ "$GREW" -gt 0 ]; then
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] OK rc=$RC +$GREW rows (total $AFTER)" >> "$LOG"
else
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] *** STALLED rc=$RC +0 rows -- ran but recorded nothing" >> "$LOG"
fi
