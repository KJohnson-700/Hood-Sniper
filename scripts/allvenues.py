#!/usr/bin/env python3
"""
One screen for every venue: Pons (Robinhood Chain), flap.sh + four.meme (BSC),
StonkFun (Solana).

WHY A SEPARATE VIEWER AND NOT ONE MERGED MONITOR
    The three collectors cannot be merged and should not be. launch_monitor.py is
    EVM-specific down to its bones -- eth_call selectors, state-override honeypot
    probes, secp256k1 signing. Solana has none of those, which is exactly why
    sol_monitor.py exists separately. Forcing them into one process would mean one
    chain's RPC outage taking down all three, and this project has already lost
    151 minutes of launches to a single silent endpoint.

    So the collectors stay independent and this only READS their feeds. It holds no
    connections, makes no RPC calls, and cannot break anything upstream. Kill it or
    restart it freely.

WHAT EACH VENUE CONTRIBUTES, AND WHY THE COLUMNS DIFFER
    The venues do not measure the same things, so the "signal" column is
    venue-specific rather than a fake common score:

      pons      curve progress -- pf 2.79 entering at 10% of supply sold, 0.77 at
                40%, so how far up the curve a row already is decides the trade
      stonkfun  rank on its pair -- 22.82% of the first five tokens on a pair
                graduate vs 2.38% for rank 101+ (n=57,137, within-pair, 9.6x)
      flapsh    tradeability at launch -- Doppler-style launches are immediately
                tradeable, so there is no curve to be early on

    Inventing a single blended score across three venues with different mechanics
    would hide exactly the differences that make each one actionable.
"""
import json
import os
import sys
import time
import argparse
import datetime as dt

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")

FEEDS = [
    ("pons", "rhc", os.path.join(DATA, "monitor_feed.jsonl")),
    ("bsc", "bsc", os.path.join(DATA, "bsc_feed.jsonl")),
    ("stonkfun", "sol", os.path.join(DATA, "stonkfun_feed.jsonl")),
]
TAIL_BYTES = 400_000


def _ts(r):
    """Seconds since epoch from whatever shape a feed uses. None when unknown --
    never fabricate a timestamp, since ordering is the whole point of this view."""
    v = r.get("ts")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            return None
    return None


def _norm(src, chain, r):
    if src == "pons":
        return {"ts": _ts(r), "chain": chain, "venue": r.get("venue") or "pons",
                "symbol": r.get("symbol"), "id": r.get("curve"),
                "mcap": r.get("mcap"), "liq": r.get("active_liq_usd"),
                "signal": (f"{100*r['curve_progress']:.0f}% up curve"
                           if r.get("curve_progress") is not None else ""),
                "extra": f"{r.get('n_buyers')} buyers" if r.get("n_buyers") is not None else "",
                "flags": r.get("flags") or []}
    if src == "bsc":
        return {"ts": _ts(r), "chain": chain, "venue": r.get("venue") or "bsc",
                "symbol": r.get("symbol"), "id": r.get("token"),
                "mcap": None, "liq": None,
                "signal": ("tradeable" if r.get("tradeable") else "not tradeable"),
                "extra": (r.get("quote") or ""),
                "flags": (["WATCH-HIT"] if r.get("watch_hit") else [])}
    return {"ts": _ts(r), "chain": chain, "venue": "stonkfun",
            "symbol": r.get("symbol"), "id": r.get("mint"),
            "mcap": r.get("mcap"), "liq": r.get("liq"),
            "signal": (f"#{r['rank_on_pair']} on ${r.get('quote')}"
                       if r.get("rank_on_pair") else ""),
            "extra": (f"{100*r['progress']:.1f}%" if r.get("progress") is not None else ""),
            "flags": (["FIRST-5"] if (r.get("rank_on_pair") or 99) <= 5 else [])}


def collect(limit=40):
    out = []
    for src, chain, path in FEEDS:
        if not os.path.exists(path):
            continue
        try:
            sz = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, sz - TAIL_BYTES))
                lines = f.read().decode("utf8", "ignore").split("\n")[1:]
        except Exception:  # noqa: BLE001
            continue
        seen = {}
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            n = _norm(src, chain, r)
            if not n["symbol"] and not n["id"]:
                continue
            seen[n["id"] or n["symbol"]] = n      # latest row per token wins
        out.extend(seen.values())
    out = [r for r in out if r["ts"]]
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out[:limit]


def rates(window_min=60.0):
    """
    Launches per hour per chain, counted from each feed over a trailing window.

    THIS IS THE POINT OF THE MERGED VIEW. "Which chain has momentum" is a question
    about RATE, and eyeballing three interleaved row lists cannot answer it -- the
    busiest chain simply fills the screen. Counting is the only honest way to see a
    chain speeding up or going quiet.

    Counted per DISTINCT token, not per row: every feed re-writes a row when it
    refreshes, so raw line counts measure our own polling cadence rather than the
    market.
    """
    now = time.time()
    cut = now - window_min * 60
    out = {}
    for src, chain, path in FEEDS:
        if not os.path.exists(path):
            out[src] = None
            continue
        try:
            sz = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, sz - 12_000_000))
                lines = f.read().decode("utf8", "ignore").split("\n")[1:]
        except Exception:  # noqa: BLE001
            out[src] = None
            continue
        ids, oldest = set(), None
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            n = _norm(src, chain, r)
            if not n["ts"] or n["ts"] < cut:
                continue
            oldest = n["ts"] if oldest is None else min(oldest, n["ts"])
            ids.add(n["id"] or n["symbol"])
        span_h = max(0.05, (now - (oldest or cut)) / 3600.0)
        out[src] = (len(ids), len(ids) / span_h, span_h)
    return out


def health():
    """Per-feed freshness. A stale feed is the thing you most need to see here --
    a silent collector looks identical to a quiet market on a merged screen."""
    h = []
    for src, _c, path in FEEDS:
        if not os.path.exists(path):
            h.append((src, None))
            continue
        h.append((src, (time.time() - os.path.getmtime(path)) / 60.0))
    return h


def fmt_usd(v):
    if not v:
        return "-"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1000:
        return f"${v/1000:.0f}k"
    return f"${v:.0f}"


def render_plain(rows, limit):
    print(f"\n{'time':>8} {'chain':>5} {'venue':>9} {'symbol':>12} {'mcap':>8} "
          f"{'liq':>8} {'signal':>20} {'flags'}")
    for r in rows[:limit]:
        t = dt.datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
        fl = ",".join(r["flags"])[:34]
        print(f"{t:>8} {r['chain']:>5} {r['venue']:>9} {str(r['symbol'])[:12]:>12} "
              f"{fmt_usd(r['mcap']):>8} {fmt_usd(r['liq']):>8} {r['signal'][:20]:>20} {fl}")
    print()
    rr = rates()
    print(f"   {'chain':>10} {'new tokens/hr':>15} {'seen':>7} {'over':>7}")
    for src, chain, _p in FEEDS:
        v = rr.get(src)
        if not v:
            print(f"   {src:>10} {'no data':>15}")
            continue
        n, per_h, span = v
        print(f"   {src:>10} {per_h:15,.0f} {n:7,d} {span:6.1f}h")
    print()
    for src, mins in health():
        state = "no feed" if mins is None else (
            f"{mins:.1f}m ago" + ("   <-- STALE" if mins > 10 else ""))
        print(f"   {src:>9}: {state}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=30)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=float, default=5.0)
    a = ap.parse_args()
    while True:
        rows = collect(limit=a.rows)
        if not a.once:
            os.system("clear")
        render_plain(rows, a.rows)
        if a.once:
            return
        time.sleep(a.every)


if __name__ == "__main__":
    main()
