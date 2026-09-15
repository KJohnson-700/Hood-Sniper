#!/bin/bash
# Watchdog for the LAPTOP launch watcher.
#
# The watcher is a single process holding the only thing that matters on launch day.
# If it dies -- RPC storm, unhandled exception, OOM -- nothing notices and the launch
# is missed silently. This restarts it and records WHY it stopped, so a crash loop is
# visible rather than looking like a healthy run.
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/laptop_watch.log"
SUP="$PROJ/logs/laptop_supervisor.log"
TOKEN="0xB095274743941e953c746F9C228DA9c18Bb6ec29"
echo "[$(date -u '+%FT%TZ')] supervisor up (pid $$)" >> "$SUP"
FAILS=0
while true; do
  if pgrep -f "base_watch.py --token $TOKEN" >/dev/null; then
    sleep 20; FAILS=0; continue
  fi
  BEFORE=$(wc -l < "$LOG" 2>/dev/null | tr -d ' ')
  echo "[$(date -u '+%FT%TZ')] watcher not running — starting (restart #$FAILS)" >> "$SUP"
  nohup python3 -u scripts/base_watch.py --token "$TOKEN" \
      --min-liq-usd 15000 --max-impact-pct 3 --poll 2 >> "$LOG" 2>&1 &
  sleep 25
  if pgrep -f "base_watch.py --token $TOKEN" >/dev/null; then
    echo "[$(date -u '+%FT%TZ')] OK running (log ${BEFORE:-0} lines)" >> "$SUP"
  else
    FAILS=$((FAILS+1))
    echo "[$(date -u '+%FT%TZ')] *** FAILED TO STAY UP (#$FAILS) — last log lines:" >> "$SUP"
    tail -5 "$LOG" >> "$SUP"
    # back off so a hard failure does not spin
    sleep $((FAILS<5 ? FAILS*10 : 60))
  fi
done
