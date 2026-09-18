#!/bin/bash
# Watch StonkFun for tokens that are among the FIRST FIVE launched against a pair.
#
# Measured: rank 1-5 on a pair graduates 22.82% of the time vs 2.38% for rank 101+
# (n=57,137, within-pair, 9.6x). Rare by construction -- 355 of 57,137 rows -- so a
# handful of alerts a day is the expected rate, not a stream. Silence is normal.
#
# Same shape as the other supervisors, and for the same reason: macOS cron needs
# Full Disk Access and fails SILENTLY without it, while a loop started from an
# interactive shell inherits the grant.
#   restart: nohup bash scripts/supervise_stonkfun_watch.sh >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/stonkfun_watch.log"
while true; do
  echo "[$(date -u +%FT%TZ)] watcher start" >> "$LOG"
  python3 scripts/stonkfun_watch.py >> "$LOG" 2>&1
  # the watcher loops internally; reaching here means it died
  echo "[$(date -u +%FT%TZ)] watcher EXITED rc=$? — restarting in 30s" >> "$LOG"
  sleep 30
done
