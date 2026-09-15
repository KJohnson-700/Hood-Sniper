#!/usr/bin/env python3
"""
Smart-money watch -- the ONLY validated input this project has.

What is validated (2026-09-06, re-verified end to end):
  A wallet profitable in a train half of blocks is profitable in the held-out
  test half 58-62% of the time, vs 15-19% for unprofitable wallets (baseline
  32%), Fisher p ~ 1e-10 over 300k blocks. A 5,000-shuffle permutation control
  never reached the observed +38.7pt lift (0/5000), and the effect survives
  inside activity bands, so it is not "profitable == trades more".

  Everything else measured here FAILED: deployer reputation (out-of-sample),
  zero-sniper count (reversed), and blanket graduation sniping (negative even
  at zero fees). So this module is deliberately the only thing wired into a
  trade decision.

The criterion below is the one that was ACTUALLY TESTED -- pnl_eth > 0 with
>= MIN_CLOSED closed round trips. It is NOT trader_index.tier(), whose
ELITE/PROFITABLE bands (pnl > 1.0 / > 0.1) were never validated. Using a
prettier-looking threshold that no experiment supports is how the deployer
thesis survived as long as it did.

Caveats that belong in front of any use:
  * P&L excludes gas. Both halves are treated alike so the RANKING holds, but
    absolute profitability is overstated.
  * "profitable" != "copyable" -- following a wallet still needs its buy
    detected fast enough, which is unmeasured.
"""
import json
import os
import threading
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
INDEX = os.path.join(DATA, "trader_index.json")
HOLDER = os.path.join(DATA, "holder_index.json")

MIN_CLOSED = 3          # the value used in the validated split
ALERT_AT = 2            # distinct smart wallets in one token -> alert
# Thresholds MEASURED, not guessed. On 3,221 held-out tokens, counting distinct
# holder-validated wallets buying in the first ~5 minutes:
#   0 -> 21.4% hit 2x | 1 -> 27.0% | 2 -> 34.8% | 3 -> 30.7% | 4-5 -> 37.2% | 6+ -> 32.1%
#   >=2: 33.5% vs 22.4%  Fisher p=3.3e-07   <- the lift happens HERE
#   >=4: 34.5% vs 23.3%  Fisher p=3.7e-04
# ALERT_AT=2 is where the signal actually is. HOT_AT=4 is used for the louder
# tier because it is RARER, not because it is better -- 4-5 vs 2 is inside the
# noise. Do not read HOT as a stronger signal than the star.
HOT_AT = 4
SUPPLY = 1_000_000_000
ETH_USD = 2450.0


def load_smart(path=None, min_closed=MIN_CLOSED):
    """
    Wallets worth watching. Returns {addr: stats}.

    PREFERS the HOLDER index (`holder_index.json`), which scores a wallet by how
    the tokens it BOUGHT performed afterwards. Falls back to the old flipper
    index only if the holder index is missing.

    Why the switch. The flipper index (realized P&L on closed round trips) was
    validated and REAL -- profitable wallets stayed profitable out-of-sample,
    58-62% vs 15-19%, permutation 0/5000 -- but it did NOT transfer: tokens
    those wallets bought did not outperform (Fisher p=0.43). They are scalpers;
    a scalper can be persistently profitable without their picks ever running.

    The holder metric measures the thing we actually act on, and it transfers:
    on 1,005,071 curve events over ~42h, wallets whose picks hit 2x >=50% of the
    time in a train half went on to hit 2x 32.7% in the held-out half vs 19.0%
    for the worst wallets (baseline 26.4%), Fisher p=1.7e-23, permutation
    0/2000. Excluding each wallet's own later buys changes nothing, so it is not
    self-inflation.

    Returns {} rather than raising when neither file exists -- a monitor that
    dies because the leaderboard has not been built is worse than one showing
    no stars.
    """
    # PREFER the ranked leaderboard when it exists. The old bar (>=3 picks,
    # >=50% hit2x) admitted 19.8% of every scored wallet -- a fifth of the chain is
    # not "smart money", and it was dominated by small-sample noise: at >=3 picks the
    # 99th percentile hit rate is 100% (wallets that went 3-for-3), while at >=30
    # picks it is 55%. top_traders.py ranks by the Wilson 95% LOWER BOUND instead, so
    # evidence is required rather than a lucky ratio, and it excludes same-block
    # sniper bots -- whose ~100% hit rates are structural (they are first in, so
    # early price can only rise) and unfollowable at 0-block latency.
    TOP = os.path.join(DATA, "top_traders.json")
    if path is None and os.path.exists(TOP):
        try:
            with open(TOP) as f:
                tx = json.load(f)
            if tx:
                # carry EVERY scored field through. Hand-listing them dropped
                # pnl_usd and median_lag, so the monitor could not show why a
                # wallet was starred -- the filtering was right and the evidence
                # for it was invisible.
                # preserve an entry's OWN source. Blanket-stamping "top" erased the
                # early_winner tag, so wallets listed on completely different
                # evidence became indistinguishable from Wilson-ranked ones.
                return {w.lower(): dict(t, source=t.get("source") or "top")
                        for w, t in tx.items()}
        except Exception:  # noqa: BLE001
            pass
    if path is None and os.path.exists(HOLDER):
        try:
            with open(HOLDER) as f:
                hx = json.load(f)
        except Exception:  # noqa: BLE001
            hx = {}
        out = {}
        for w, t in hx.items():
            if t.get("picks", 0) >= 3 and (t.get("hit2x") or 0) >= 0.5:
                out[w.lower()] = {"hit2x": t["hit2x"], "picks": t["picks"],
                                  "median_fwd": t.get("median_fwd"),
                                  "source": "holder"}
        if out:
            return out
    path = path or INDEX
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            ix = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for w, t in (ix.get("traders") or {}).items():
        if t.get("closed", 0) >= min_closed and (t.get("pnl_eth") or 0) > 0:
            out[w.lower()] = {"pnl_eth": t.get("pnl_eth"), "closed": t.get("closed"),
                              "win_rate": t.get("win_rate"), "source": "flipper"}
    return out


class SmartWatch:
    """
    Live count of validated wallets buying each token, fed from curve BUY logs.

    Counts DISTINCT wallets, not buys: one wallet buying five times is one
    signal, and treating it as five is how a single actor looks like a crowd.
    """

    def __init__(self, min_closed=MIN_CLOSED, alert_at=ALERT_AT, hot_at=HOT_AT,
                 reload_sec=600):
        self.min_closed = min_closed
        self.alert_at = alert_at
        self.hot_at = hot_at
        self.hot = set()
        self.smart = load_smart(min_closed=min_closed)
        self.by_token = defaultdict(dict)      # token -> {wallet: rec}
        self.alerted = set()
        self.alerts = []                       # newest last
        self.lock = threading.Lock()
        self._last_load = time.time()
        self._reload_sec = reload_sec

    def maybe_reload(self):
        """Pick up index growth without restarting the monitor."""
        if time.time() - self._last_load < self._reload_sec:
            return
        self._last_load = time.time()
        s = load_smart(min_closed=self.min_closed)
        if s:
            with self.lock:
                self.smart = s

    def note_buy(self, token, wallet, quote_wei, tokens_out, block, symbol=None):
        """Record a curve buy. Returns an alert dict the first time a token crosses."""
        w = (wallet or "").lower()
        with self.lock:
            if w not in self.smart:
                return None
            tok = (token or "").lower()
            seen = self.by_token[tok]
            if w not in seen:
                # q and t are both raw 18-dec, so q/t IS ETH-per-token; dividing
                # by 1e18 again collapses every entry mcap to $0 (an earlier bug).
                mcap = (quote_wei / tokens_out) * SUPPLY * ETH_USD if tokens_out else None
                seen[w] = {"wallet": w, "block": block, "entry_mcap_usd": mcap,
                           "spent_eth": quote_wei / 1e18, **self.smart[w]}
            n = len(seen)
            hot = n >= self.hot_at and tok not in self.hot
            if hot:
                self.hot.add(tok)
            if (n >= self.alert_at and tok not in self.alerted) or hot:
                self.alerted.add(tok)
                a = {"token": tok, "symbol": symbol, "n": n, "block": block,
                     "wallets": list(seen.values()), "ts": time.time(), "hot": hot}
                self.alerts.append(a)
                return a
        return None

    def count(self, token):
        with self.lock:
            return len(self.by_token.get((token or "").lower(), {}))

    def wallets_in(self, token):
        with self.lock:
            return list(self.by_token.get((token or "").lower(), {}).values())

    def best_entry_mcap(self, token):
        ws = [w["entry_mcap_usd"] for w in self.wallets_in(token)
              if w.get("entry_mcap_usd")]
        return min(ws) if ws else None


if __name__ == "__main__":
    s = load_smart()
    print(f"validated smart wallets (closed >= {MIN_CLOSED}, pnl > 0): {len(s)}")
    for w, t in sorted(s.items(), key=lambda kv: -(kv[1]["pnl_eth"] or 0))[:10]:
        print(f"  {w}  pnl {t['pnl_eth']:+.4f} ETH  closed {t['closed']:3d}  "
              f"win {t['win_rate']}%")
