#!/usr/bin/env python3
"""
Resolve FOMO traders to on-chain wallets, then SCORE them against our own data.

WHY THIS EXISTS. The smart-money index is built from Pons curve buys on Robinhood
Chain -- it can only see wallets that have traded there. A curated list of known
traders is a way to find good wallets our 42h window has never seen. But a curated
list is also exactly how a plausible-but-worthless signal gets adopted, so nothing
here trusts the list: it supplies CANDIDATES, and our own holder index decides.

The distinction matters because it already burned this project once. The original
"smart money" index ranked wallets by realized P&L on closed round trips. That was
real and it replicated out-of-sample (58-62% vs 15-19%, permutation 0/5000) -- and
it did NOT transfer: tokens those wallets bought did not outperform (Fisher p=0.43).
They were scalpers. A "top P&L traders" leaderboard is that same metric. So a wallet
from FOMO earns a star here only by clearing the SAME bar as every other wallet:
>=3 picks and >=50% of them hitting 2x, measured on our chain data.

WHY THE LEADERBOARD AND NOT PER-HANDLE LOOKUPS. /v2/users/{handle} costs 10 credits
each -- 25 handles would be 250 of the free tier's 1,000. /v2/leaderboard/{window}
costs 1 credit and returns up to 150 traders WITH both wallets. Same data, 1/250th
the cost, and it finds traders you did not know to ask for.

Robinhood Chain is FOMO's most active chain (their own docs), so their EVM wallets
are directly comparable to our index rather than being a different-chain mismatch.

    export FOMO_API_KEY=...            # free key: https://fomoapi.io/docs
    python3 fomo_kols.py --leaderboard # pull 150 traders + wallets  (1 credit)
    python3 fomo_kols.py --score       # score them against holder_index.json (0 credits)
    python3 fomo_kols.py --handles a,b # resolve specific handles      (10 credits each)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
from investigate import env_key  # noqa: E402

BASE = "https://api.fomoapi.io"
OUT = os.path.join(DATA, "fomo_traders.json")
HOLDER = os.path.join(DATA, "holder_index.json")

# The same bar every other wallet clears. Not negotiable for an imported list --
# that is the entire point of importing it through a filter.
MIN_PICKS = 3
MIN_HIT2X = 0.50
BASELINE = 0.264          # measured: any random buy hits 2x 26.4% of the time


def key():
    return env_key("FOMO_API_KEY")


def get(path, log=print):
    k = key()
    if not k:
        log("FOMO_API_KEY not set — add it to .env (free key at https://fomoapi.io/docs)")
        return None
    req = urllib.request.Request(BASE + path,
                                 headers={"Authorization": f"Bearer {k}",
                                          "User-Agent": "HoodSniper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
            cost = r.headers.get("x-credits-cost")
            left = r.headers.get("x-credits-remaining")
            if cost or left:
                log(f"  [credits: cost {cost}, {left} remaining]")
            return body
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode(errors="replace")
        log(f"  HTTP {e.code}: {detail}")
        return None
    except Exception as e:  # noqa: BLE001
        log(f"  request failed: {str(e)[:100]}")
        return None


def pull_leaderboard(windows=("all", "30d", "7d"), limit=150, log=print):
    """One credit per window. Merged by handle, keeping the best rank seen."""
    seen = {}
    for w in windows:
        d = get(f"/v2/leaderboard/{w}?limit={limit}", log)
        if not d:
            continue
        rows = d.get("traders") or []
        log(f"  {w:<5} {len(rows)} traders")
        for t in rows:
            h = (t.get("handle") or "").lower()
            if not h:
                continue
            prev = seen.get(h)
            t["_window"] = w
            if prev is None or (t.get("rank") or 999) < (prev.get("rank") or 999):
                seen[h] = t
        time.sleep(0.3)
    if seen:
        json.dump(list(seen.values()), open(OUT, "w"), indent=1)
        log(f"\nsaved {len(seen)} traders -> {OUT}")
    return list(seen.values())


def resolve_handles(handles, log=print):
    """10 credits EACH. Use only for handles the leaderboard did not return."""
    out = []
    log(f"resolving {len(handles)} handles at 10 credits each "
        f"= {len(handles)*10} credits")
    for h in handles:
        d = get(f"/v2/users/{h.lstrip('@')}", log)
        if d and d.get("wallets"):
            out.append(d)
            w = d["wallets"]
            log(f"  @{d.get('handle'):<20} evm={str(w.get('evm'))[:14]}… "
                f"sol={str(w.get('solana'))[:10]}… pnl=${(d.get('pnlUsd') or 0):,.0f}")
        else:
            log(f"  @{h:<20} not resolved")
        time.sleep(0.4)
    return out


def score(log=print):
    """
    Score FOMO wallets against OUR holder index. Zero credits — pure local join.

    Reports three groups, because the interesting answer is usually the third:
      QUALIFY   clears our bar -> genuinely worth starring
      SEEN      we have data and they DO NOT clear it -> the list is reputation
      UNSEEN    never traded Pons on RH Chain -> we cannot judge them at all
    """
    if not os.path.exists(OUT):
        log("no trader file — run --leaderboard first")
        return
    if not os.path.exists(HOLDER):
        log("no holder_index.json — build it with holder_index.py first")
        return
    traders = json.load(open(OUT))
    hx = {w.lower(): t for w, t in json.load(open(HOLDER)).items()}
    log(f"FOMO traders: {len(traders)}   wallets in our index: {len(hx):,}\n")

    qualify, seen, unseen = [], [], []
    for t in traders:
        evm = ((t.get("wallets") or {}).get("evm") or "").lower()
        if not evm:
            continue
        st = hx.get(evm)
        if not st:
            unseen.append(t)
            continue
        picks = st.get("picks", 0)
        hit = st.get("hit2x") or 0.0
        rec = dict(t, _picks=picks, _hit2x=hit)
        (qualify if (picks >= MIN_PICKS and hit >= MIN_HIT2X) else seen).append(rec)

    log(f"{'group':<10}{'n':>5}   meaning")
    log(f"  {'QUALIFY':<8}{len(qualify):>5}   clears our bar (>={MIN_PICKS} picks, "
        f">={MIN_HIT2X:.0%} hit 2x)")
    log(f"  {'SEEN':<8}{len(seen):>5}   we have their data, they do NOT clear it")
    log(f"  {'UNSEEN':<8}{len(unseen):>5}   never traded Pons on RH Chain — unjudgeable")

    if qualify:
        log(f"\n  wallets worth importing:")
        for t in sorted(qualify, key=lambda x: -x["_hit2x"])[:20]:
            log(f"    @{str(t.get('handle'))[:18]:<18} {t['_picks']:>3} picks "
                f"hit2x {t['_hit2x']:>5.0%}  {(t.get('wallets') or {}).get('evm')}")
    if seen:
        hits = [t["_hit2x"] for t in seen]
        log(f"\n  the {len(seen)} we CAN judge but that fail our bar average "
            f"{sum(hits)/len(hits):.0%} hit-2x vs {BASELINE:.0%} baseline.")
        log("  If that is at or below baseline, the list is reputation, not edge —")
        log("  which is the finding, not a failure of the exercise.")
    return qualify


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaderboard", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--handles")
    ap.add_argument("--limit", type=int, default=150)
    a = ap.parse_args()
    if a.leaderboard:
        pull_leaderboard(limit=a.limit)
    if a.handles:
        got = resolve_handles([h.strip() for h in a.handles.split(",") if h.strip()])
        if got:
            prev = json.load(open(OUT)) if os.path.exists(OUT) else []
            by = {(t.get("handle") or "").lower(): t for t in prev}
            for t in got:
                by[(t.get("handle") or "").lower()] = t
            json.dump(list(by.values()), open(OUT, "w"), indent=1)
            print(f"merged -> {OUT} ({len(by)} traders)")
    if a.score:
        score()
    if not (a.leaderboard or a.score or a.handles):
        ap.print_help()
