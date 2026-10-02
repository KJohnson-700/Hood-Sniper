#!/bin/bash
# Route alerts to per-chain Discord channels.
#
# Alerts on RUNNERS (on the curve, moving) rather than graduations. Measured on
# 1,669 post-graduation paths: 93.6% stop out at 0.70 and every take-profit from
# 1.15x to 5.0x loses even at zero fees -- a graduation is the wrong moment to be
# told about, the curve is where the trade is.
#   restart: nohup bash scripts/supervise_alerts.sh >/dev/null 2>&1 &
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
LOG="$PROJ/logs/alert_router.log"
while true; do
  echo "[$(date -u +%FT%TZ)] router start" >> "$LOG"
  python3 scripts/alert_router.py >> "$LOG" 2>&1
  echo "[$(date -u +%FT%TZ)] router EXITED rc=$? — restarting in 15s" >> "$LOG"
  sleep 15
done
