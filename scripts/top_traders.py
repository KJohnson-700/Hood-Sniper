#!/usr/bin/env python3
"""
Robinhood Chain top traders -- a real leaderboard, ranked by evidence.

WHAT WAS WRONG WITH THE OLD BAR. `picks>=3 and hit2x>=50%` admitted 19.8% of all
scored wallets -- a fifth of the chain is not a "top traders" list. It was dominated
by small-sample noise: at picks>=3 the 99th percentile is 100% (wallets that went
3-for-3), while at picks>=30 the 99th percentile is only 55%. So a coin-flip wallet
with three lucky picks outranked someone genuinely elite over a hundred.

THE FIX IS THE WILSON LOWER BOUND, not a raw rate. It answers "given this many
picks, what is the WORST the true hit rate plausibly is?" -- so evidence is required,
not just a good-looking ratio:

    3-for-3   (100% raw)  ->  Wilson  43.8%
    45-of-100 (45% raw)   ->  Wilson  35.6%
    18-of-20  (90% raw)   ->  Wilson  69.9%

That ordering is the whole point: 3-for-3 is not better than 45-of-100, and the raw
rate says it is. This is the same discipline that killed the deployer filter and the
dip strategy -- rank by what the data can actually support.

BASELINE: 26.4% of ALL curve buys hit 2x. A wallet is only interesting if its lower
bound clears that, because otherwise it is indistinguishable from buying at random.

    python3 top_traders.py                  # top 40
    python3 top_traders.py --n 100 --min-picks 20
    python3 top_traders.py --export         # write top_traders.json for the monitor
"""
import argparse
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
HOLDER = os.path.join(DATA, "holder_index.json")
OUT = os.path.join(DATA, "top_traders.json")

BASELINE = 0.264
Z = 1.96          # 95% confidence
EVENTS = os.path.join(DATA, "holder_events.jsonl")
LAGS = os.path.join(DATA, "wallet_lag.json")
BLOCK_SEC = 0.101

# A wallet that buys in the SAME BLOCK as the first buyer is a sniper bot, and its
# hit rate is structural rather than skilled: it is first in, so the only direction
# from its entry is up -- for the first few blocks it IS the pump. Measured on the
# raw leaderboard, the top three were first-buyer 41%, 89% and 90% of the time at
# 0-1 blocks of lag, all showing ~100% hit2x over 65-149 picks.
#
# Following one is worse than useless. You cannot beat 0-block latency, so you would
# arrive after it and become its exit liquidity. They are excluded by default, and
# the exclusion is a FILTER not a penalty: no amount of hit rate makes a bot
# followable.
MIN_LAG_BLOCKS = 10          # ~1s after the first buyer
MAX_FIRST_SHARE = 0.25       # first-in on at most a quarter of its buys

# THREE HOLES THAT LET INFRASTRUCTURE THROUGH, found by Slim on token MATILDA where
# 12 wallets were starred and none were traders:
#
# 1. NO MINIMUM EFFECT SIZE. The bar was "Wilson lower bound > baseline", with no
#    margin. A wallet at 28% over 42,504 picks has a confidence interval so tight
#    that its lower bound clears 26.4% -- a 1.6-POINT edge. Statistically real,
#    practically worthless, and it starred a router. Significance is not edge.
# 2. CONTRACTS WERE SCORED AS TRADERS. Two of the twelve were contracts (100 and 23
#    bytes of code). A router that touches everything is not smart money.
# 3. NO UPPER BOUND ON PICKS. A wallet with tens of thousands of buys in a 42h
#    window is infrastructure. "Buys everything" cannot be a signal, by definition:
#    at that volume its hit rate IS the baseline.
MIN_FLOOR = 0.40             # lower bound must clear 40%, not merely beat 26.4%
MAX_PICKS = 400              # above this it is a router/bot, not a person

# 4. LOSING MONEY WAS NOT DISQUALIFYING. The holder index only asks whether tokens a
#    wallet BOUGHT later 2x'd -- it never looks at what the wallet actually made. A
#    wallet can buy things that double and still bleed: in late, out early, or the
#    2x arrived after it sold. Slim caught wallets starred as "smart" that were
#    -$2k over the week.
#
#    P&L is used ONLY as a disqualifier, never as a ranker. As a ranker it was
#    measured and FAILED: persistently profitable wallets did not go on to pick
#    tokens that outperformed (Fisher p=0.43) -- they were scalpers. But "picks 2x
#    AND does not lose money" is strictly stronger than "picks 2x" alone.
TRADER_INDEX = os.path.join(DATA, "trader_index.json")
MIN_PNL_USD = 0.0            # must not be underwater

# RECENCY IS A HARD GATE, NOT A TIEBREAK.
# The method was validated out-of-sample -- on 10,078 picks made AFTER the list was
# built, the selected wallets hit 2x 41.7% of the time against a 22.8% base. The
# wallets were never the problem. STALENESS was: measured 2026-09-23, the live
# 243-wallet list had a median 316.7 HOURS since each wallet's last trade, only 24
# of 243 had traded in 24h and 1 in 15 minutes, so nothing on the board ever got
# starred. A fresh list would have shared just 9 of those 243.
#
# Memecoin traders rotate wallets constantly, so a one-time list decays fast and
# silently.
#
# 48h, not 24h. Wallets clearing every gate, by idle window: 24h -> 206, 48h -> 377,
# 72h -> 503, 7d -> 921 (before the P&L and sniper gates, which cut hardest). At 24h
# the final export was 35 wallets, and the HOT alert needs TWO of them in the same
# token -- with a list that small it would essentially never fire, trading one
# silent failure for another. 48h keeps the list usable while staying two weeks
# fresher than what it replaced. ~48h of chain at 0.101s blocks.
MAX_IDLE_BLOCKS = 1_728_000

# 5. REQUIRE P&L TO EXIST, not merely to be non-negative.
#    Realized P&L needs a SELL, so a buy-and-hold wallet has none -- and 338 of 558
#    survivors were unmeasured for exactly that reason, meaning the "not underwater"
#    guarantee did not actually apply to them. A wallet that has never closed a
#    position has not proven it can get out, which on this project is the whole
#    game: the exit is the half that decides whether you keep anything.
REQUIRE_PNL = True


def wilson_lower(hits, n, z=Z):
    """Lower bound of the Wilson score interval. 0.0 when there is no evidence."""
    if n <= 0:
        return 0.0
    p = hits / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / d)


def wallet_lags(rebuild=False, log=print):
    """
    Per-wallet median blocks behind the FIRST buyer of each token, plus how often it
    was first. Cached -- the scan is 2.9M events.
    """
    if not rebuild and os.path.exists(LAGS):
        try:
            return json.load(open(LAGS))
        except Exception:  # noqa: BLE001
            pass
    if not os.path.exists(EVENTS):
        log("no holder_events.jsonl — cannot tell snipers from traders")
        return {}
    first, per = {}, {}
    log("  scanning events for entry timing (once, then cached)…")
    rows = []
    for line in open(EVENTS):
        try:
            blk, tok, w, px, buy = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        rows.append((blk, tok, w.lower()))
        if tok not in first or blk < first[tok]:
            first[tok] = blk
    for blk, tok, w in rows:
        f = first.get(tok)
        if f is None:
            continue
        d = per.setdefault(w, {"lags": [], "first": 0, "n": 0})
        d["lags"].append(blk - f)
        d["n"] += 1
        if blk == f:
            d["first"] += 1
    out = {}
    for w, d in per.items():
        ls = sorted(d["lags"])
        out[w] = {"median_lag": ls[len(ls) // 2], "first_share": d["first"] / d["n"],
                  "n": d["n"]}
    try:
        json.dump(out, open(LAGS, "w"))
    except Exception:  # noqa: BLE001
        pass
    log(f"  timing computed for {len(out):,} wallets")
    return out


def is_contract(addrs, log=print):
    """Which of these have code? Contracts cannot be 'traders' to follow."""
    import urllib.request, itertools
    RPCS = itertools.cycle(["https://rpc.mainnet.chain.robinhood.com",
                            "https://robinhood-rpc.publicnode.com"])
    out = set()
    for i in range(0, len(addrs), 25):
        chunk = addrs[i:i + 25]
        payload = [{"jsonrpc": "2.0", "id": j, "method": "eth_getCode",
                    "params": [a, "latest"]} for j, a in enumerate(chunk)]
        try:
            req = urllib.request.Request(
                next(RPCS), data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                res = json.load(r)
            by = {x.get("id"): x for x in res} if isinstance(res, list) else {}
            for j, a in enumerate(chunk):
                code = (by.get(j) or {}).get("result") or "0x"
                if len(code) > 2:
                    out.add(a)
        except Exception:  # noqa: BLE001
            continue          # unreadable is not "is an EOA" -- just skip the check
    return out


def pnl_map(log=print):
    """wallet -> realized pnl_usd, from the flipper index. {} if unavailable."""
    if not os.path.exists(TRADER_INDEX):
        log("  no trader_index.json — P&L disqualifier NOT applied")
        return {}
    try:
        d = json.load(open(TRADER_INDEX))
    except Exception:  # noqa: BLE001
        return {}
    tr = d.get("traders") or {}
    return {w.lower(): (t or {}).get("pnl_usd") for w, t in tr.items()}


def rank(min_picks=10, exclude_snipers=True, log=print):
    if not os.path.exists(HOLDER):
        log("no holder_index.json — build it with holder_index.py --build")
        return []
    hx = json.load(open(HOLDER))
    lags = wallet_lags(log=log) if exclude_snipers else {}
    pnl = pnl_map(log)
    out, snipers, losers, unmeasured = [], 0, 0, 0
    head_blk = max((t.get("last_block") or 0) for t in hx.values()) if hx else 0
    stale = 0
    for w, t in hx.items():
        n = t.get("picks") or 0
        if n < min_picks:
            continue
        lbk = t.get("last_block") or 0
        # A wallet with no last_block predates this field; keep it rather than
        # silently emptying the list on the first run after the upgrade.
        if head_blk and lbk and (head_blk - lbk) > MAX_IDLE_BLOCKS:
            stale += 1
            continue
        rate = t.get("hit2x") or 0.0
        hits = round(rate * n)
        lb = wilson_lower(hits, n)
        if n > MAX_PICKS:
            continue                       # router / bot, not a person
        if lb < MIN_FLOOR:
            continue                       # significant is not the same as useful
        # underwater is disqualifying, but UNKNOWN is not the same as underwater --
        # a wallet the flipper index has never seen is simply unmeasured
        pv = pnl.get(w.lower())
        if pv is None:
            if REQUIRE_PNL:
                unmeasured += 1
                continue
        elif pv < MIN_PNL_USD:
            losers += 1
            continue
        lg = lags.get(w.lower())
        if exclude_snipers and lg:
            if lg["median_lag"] < MIN_LAG_BLOCKS or lg["first_share"] > MAX_FIRST_SHARE:
                snipers += 1
                continue
        out.append({"wallet": w.lower(), "picks": n, "hit2x": rate,
                    "hit5x": t.get("hit5x") or 0.0,
                    "median_fwd": t.get("median_fwd"),
                    "pnl_usd": pv,
                    "median_lag": (lg or {}).get("median_lag"),
                    "first_share": (lg or {}).get("first_share"),
                    "wilson": lb, "edge": lb - BASELINE})
    log(f"  dropped {stale:,} wallets idle >{MAX_IDLE_BLOCKS:,} blocks "
        f"(~{MAX_IDLE_BLOCKS*0.101/3600:.0f}h)")
    # contracts last, because it costs RPC and the list is already small by here
    if out:
        contracts = is_contract([r["wallet"] for r in out], log)
        if contracts:
            log(f"  excluded {len(contracts)} CONTRACT addresses (routers, not traders)")
            out = [r for r in out if r["wallet"] not in contracts]
    out.sort(key=lambda r: -r["wilson"])
    if losers:
        log(f"  excluded {losers:,} wallets with NEGATIVE realized P&L")
    if unmeasured:
        log(f"  excluded {unmeasured:,} wallets with NO closed trade "
            f"(never proven they can exit)")
    if snipers:
        log(f"  excluded {snipers:,} sniper wallets (same-block entry — unfollowable)")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--min-picks", type=int, default=10)
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--include-snipers", action="store_true",
                    help="show same-block bots too (they are not followable)")
    a = ap.parse_args()

    rows = rank(a.min_picks, exclude_snipers=not a.include_snipers)
    hx = json.load(open(HOLDER)) if os.path.exists(HOLDER) else {}
    print(f"scored wallets: {len(hx):,}   with >={a.min_picks} picks AND a lower bound "
          f"above the {BASELINE:.1%} baseline: {len(rows):,} "
          f"({100*len(rows)/max(len(hx),1):.2f}%)\n")
    print(f"{'#':>3}  {'wallet':<44}{'picks':>6}{'hit2x':>7}{'floor':>7}{'5x':>6}"
          f"{'entry lag':>11}{'P&L':>10}")
    for i, r in enumerate(rows[:a.n], 1):
        ml = r.get("median_lag")
        lag = f"{ml*BLOCK_SEC:.1f}s" if ml is not None else "-"
        pv = r.get("pnl_usd")
        pst = f"${pv:+,.0f}" if pv is not None else "n/a"
        print(f"{i:>3}  {r['wallet']:<44}{r['picks']:>6}{r['hit2x']:>6.0%}"
              f"{r['wilson']:>7.0%}{r['hit5x']:>6.0%}{lag:>11}{pst:>10}")
    if rows:
        print(f"\n  'floor' is the Wilson 95% lower bound — the worst the true rate")
        print(f"  plausibly is. 'edge' is how far that clears the {BASELINE:.1%} baseline.")
        print(f"  Sorted by floor, so a big sample beats a lucky small one.")
    if a.export:
        json.dump({r["wallet"]: r for r in rows}, open(OUT, "w"), indent=1)
        print(f"\n  exported {len(rows)} -> {OUT}")
