#!/usr/bin/env python3
"""
Which callers are actually worth following, measured against outcomes.

THE POINT
    Slim named @solanaswaggy as the kind of account worth tracking. The question
    "is this account reliable" is answerable, and the answer is NOT the account's
    follower count, its hit-rate screenshots, or how early it FEELS. It is:
    of every coin this wallet bought, what fraction went on to run -- against the
    base rate of coins that ran in the same window.

    `gmgn track kol` hands over exactly what is needed to do that honestly:

      twitter_username   the X handle, so a wallet maps to an account you can name
      maker              the wallet, so the claim is checkable on chain
      side / amount_usd  buy or sell and how much
      timestamp          WHEN -- which token_top_traders does not give, and which
                         is the whole difference between "was early" and "was in it"
      base_token         symbol + launchpad

SCORING RULES, and why each one is there
    1. EVERY buy counts, not just the ones that worked. A caller's score is
       hits/buys, and buys with no measurable outcome are excluded from BOTH
       halves rather than scored as misses.
    2. The outcome is measured FORWARD FROM THE BUY -- peak market cap after the
       timestamp, over the cap at the timestamp. Using the token's all-time peak
       would credit a caller who bought the top of a coin that had already run.
    3. The base rate is reported next to every score. A caller beating nothing is
       not a caller worth following, and most will not beat it.
    4. Outcomes come from OUR snapshot history, never from GMGN's own profit
       fields. A vendor's P&L number was already misread once on this project
       (a token-level net_buy figure taken for a wallet's earnings, off by three
       orders of magnitude).

QUOTA. This shares one API key with the live discovery feed. A study that starves
the running bot is a bug, so the same circuit breaker as meta_lab applies: hard
call budget, slow pacing, abort on consecutive 429s.
"""
import json
import os
import sys
import time
import argparse
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import investigate as I  # noqa: E402
import meta_lab as M  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
TRADES = os.path.join(DATA, "kol_trades.jsonl")

PACE = 6.0
TRIP = 3

# outcome thresholds, same shape as meta_lab's runner label
HIT_MULT = 3.0
HIT_PEAK = 150_000.0


def collect(chain="sol", rounds=1, limit=200, log=print):
    """Append new KOL trades, deduped by transaction hash."""
    seen = set()
    if os.path.exists(TRADES):
        with open(TRADES) as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["tx"])
                except Exception:  # noqa: BLE001
                    pass
    log(f"  {len(seen):,} KOL trades already recorded")
    streak = 0
    added = 0
    with open(TRADES, "a") as out:
        for i in range(rounds):
            if streak >= TRIP:
                log("  ABORTING: consecutive 429s — the live feed shares this key")
                break
            d, err = I._gmgn(["track", "kol", "--chain", chain,
                              "--limit", str(limit), "--raw"], timeout=90)
            if err:
                streak += 1
                log(f"  round {i+1}: ERR {str(err)[:70]}")
                time.sleep(PACE)
                continue
            streak = 0
            rows = d.get("list") or []
            new = 0
            for r in rows:
                tx = r.get("transaction_hash")
                if not tx or tx in seen:
                    continue
                seen.add(tx)
                mi = r.get("maker_info") or {}
                bt = r.get("base_token") or {}
                out.write(json.dumps({
                    "tx": tx, "chain": chain,
                    "handle": mi.get("twitter_username"),
                    "kol_name": mi.get("twitter_name") or mi.get("name"),
                    "tags": mi.get("tags") or [],
                    "wallet": r.get("maker"),
                    "token": r.get("base_address"),
                    "symbol": bt.get("symbol"),
                    "launchpad": bt.get("launchpad"),
                    "side": r.get("side"),
                    "usd": r.get("amount_usd"),
                    "price_usd": r.get("price_usd"),
                    "ts": r.get("timestamp"),
                }) + "\n")
                new += 1
            added += new
            out.flush()
            log(f"  round {i+1}: {len(rows)} rows, {new} new")
            if i + 1 < rounds:
                time.sleep(PACE)
    log(f"  added {added} trades -> {TRADES}")
    return added


def _load():
    rows = []
    if not os.path.exists(TRADES):
        return rows
    with open(TRADES) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    return rows


def roster(log=print):
    rows = _load()
    if not rows:
        return log("  no KOL trades yet — run --collect")
    by = collections.defaultdict(lambda: {"buys": 0, "sells": 0, "wallets": set(),
                                          "tokens": set(), "usd": 0.0, "tags": set()})
    for r in rows:
        h = r.get("handle") or "(unnamed)"
        d = by[h]
        d["buys" if r.get("side") == "buy" else "sells"] += 1
        if r.get("wallet"):
            d["wallets"].add(r["wallet"])
        if r.get("token"):
            d["tokens"].add(r["token"])
        d["usd"] += float(r.get("usd") or 0)
        for t in (r.get("tags") or []):
            d["tags"].add(t)
    log(f"  {len(rows):,} trades across {len(by)} handles\n")
    log(f"  {'handle':>22} {'buys':>5} {'sells':>6} {'tokens':>7} {'wallets':>8} {'usd':>12}")
    for h, d in sorted(by.items(), key=lambda kv: -kv[1]["buys"])[:40]:
        log(f"  {h[:22]:>22} {d['buys']:>5} {d['sells']:>6} {len(d['tokens']):>7} "
            f"{len(d['wallets']):>8} {d['usd']:>12,.0f}")
    return by


def score(chain="sol", min_buys=3, log=print):
    """
    Hit rate per handle, measured FORWARD from each buy, with the base rate.
    """
    rows = [r for r in _load() if r.get("side") == "buy" and r.get("token")]
    if not rows:
        return log("  no KOL buys recorded yet — run --collect")
    traj, _meta = M._traj(chain, log=log)

    def outcome(tok, ts):
        """(ran, mult) measured only from snapshots AFTER the buy."""
        v = traj.get(tok)
        if not v:
            return None
        try:
            ts = float(ts)
        except Exception:  # noqa: BLE001
            return None
        if ts > 1e12:
            ts /= 1000.0
        at = [m for (t, m) in v if t <= ts]
        after = [m for (t, m) in v if t > ts]
        if not at or not after:
            return None
        base = at[-1]
        peak = max(after)
        if base <= 0:
            return None
        return (peak / base >= HIT_MULT and peak >= HIT_PEAK), peak / base

    by = collections.defaultdict(lambda: {"n": 0, "hit": 0, "mults": []})
    tot_n = tot_hit = 0
    for r in rows:
        o = outcome(r["token"], r.get("ts"))
        if o is None:
            continue
        ran, mult = o
        d = by[r.get("handle") or "(unnamed)"]
        d["n"] += 1
        d["mults"].append(mult)
        tot_n += 1
        if ran:
            d["hit"] += 1
            tot_hit += 1
    if not tot_n:
        return log("  no KOL buys have measurable forward outcomes yet. Outcomes "
                   "need snapshots AFTER the buy, so let --collect run alongside "
                   "the discovery feed for a while.")
    base = tot_hit / tot_n
    log(f"\n  {tot_n} KOL buys with a measurable forward outcome")
    log(f"  BASE RATE {base*100:.1f}% — any handle below this is worse than random\n")
    log(f"  {'handle':>22} {'buys':>5} {'hits':>5} {'rate':>7} {'lift':>6} {'med mult':>9}")
    out = []
    for h, d in by.items():
        if d["n"] < min_buys:
            continue
        rate = d["hit"] / d["n"]
        md = sorted(d["mults"])[len(d["mults"]) // 2]
        out.append((rate, d["n"], h, d["hit"], md))
    for rate, n, h, hit, md in sorted(out, reverse=True)[:30]:
        log(f"  {h[:22]:>22} {n:>5} {hit:>5} {rate*100:>6.1f}% "
            f"{rate/base if base else 0:>5.1f}x {md:>8.2f}x")
    if not out:
        log(f"  (no handle has >={min_buys} measurable buys yet)")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--roster", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--chain", default="sol")
    ap.add_argument("--min-buys", type=int, default=3)
    a = ap.parse_args()
    if a.collect:
        collect(chain=a.chain, rounds=a.rounds)
    elif a.roster:
        roster()
    elif a.score:
        score(chain=a.chain, min_buys=a.min_buys)
    else:
        ap.print_help()
