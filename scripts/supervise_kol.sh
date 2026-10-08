#!/bin/bash
# Accumulate GMGN KOL trades so the caller roster and its scores can be built.
#
# WHY A SUPERVISOR AND NOT A ONE-SHOT. `track kol` returns the most recent ~100
# trades, which is a live stream, not a directory. A single call saw 9 handles;
# the roster only becomes a roster by sampling it repeatedly. Scoring also needs
# the SNAPSHOTS THAT COME AFTER each buy, so the outcome side is only measurable
# once the discovery feed has moved past the trade.
#
# INTERVAL IS DELIBERATELY SLOW. This key is shared with the live discovery feed,
# and a background study already starved it once today: an attribution run paced
# at 0.25s triggered a key-wide 429 that took >5 minutes to clear while
# gmgn_feed went from 6.5 to 9.5 minutes stale. 90s costs some trades to churn
# and keeps the bot fed, which is the right trade.
PROJ="/Users/mainfolder/Documents/Hood Sniper"
cd "$PROJ" || exit 1
while true; do
  python3 -u scripts/kol_tracker.py --collect --rounds 1 >> logs/kol.log 2>&1
  sleep 90
done
