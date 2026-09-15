#!/bin/bash
# Supervised hourly loop for the BSC outcome snapshot.
#
# Why a loop and not a scheduler:
#   - /usr/sbin/cron on macOS needs Full Disk Access; without it entries fail
#     SILENTLY (a previous project's hourly job logged nothing for 21 hours).
#   - launchd fails LOUDLY but still fails: a LaunchAgent's child process has no
#     TCC grant for ~/Documents, so /bin/bash returns "Operation not permitted".
#     Verified here, not assumed.
#
# This loop is started from an interactive session, so it INHERITS that session's
# Documents access. Cost: it does not survive logout/reboot. Restart with:
#   nohup bash "scripts/supervise_snapshots.sh" >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] supervisor up (pid $$)" >> logs/snapshot.log
echo $$ > logs/.supervisor.pid

while true; do
  bash scripts/snapshot_hourly.sh
  # sleep until the next :07 -- off the :00 mark so we are not hammering
  # DexScreener at the same instant as every other hourly job on earth
  NOW_M=$(date +%M); NOW_S=$(date +%S)
  MINS=$(( (67 - 10#$NOW_M) % 60 )); [ "$MINS" -eq 0 ] && MINS=60
  SECS=$(( MINS * 60 - 10#$NOW_S ))
  [ "$SECS" -lt 60 ] && SECS=$(( SECS + 3600 ))
  sleep "$SECS"
done
