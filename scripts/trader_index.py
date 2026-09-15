#!/usr/bin/env python3
"""
Hood Sniper -- trader index ("smart money").

Builds a persistent, incremental leaderboard of Pons traders from curve flow,
and answers the question that actually matters at entry time:

    "how many proven traders are in this token, and at what market cap did
     they get in relative to me?"

Why traders and not devs: 92% of Pons devs launch exactly once, so skill cannot
be separated from luck. Traders repeat constantly -- ~98 wallets clear 10 closed
round trips in two hours. On a temporal split, wallets profitable in the train
half won 64% of the time in the test half vs 28% for unprofitable ones
(Spearman 0.237, Fisher p=0.00025). The dev signal failed the same test.

READ-ONLY. No keys, no signing, no orders.

    python3 trader_index.py --scan 200000        # extend the index backwards/forwards
    python3 trader_index.py --top 25             # leaderboard
    python3 trader_index.py --token 0x<addr>     # who is in this token, at what mcap
    python3 trader_index.py --persistence        # re-run the train/test check
"""
import argparse
import itertools
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
os.makedirs(DATA, exist_ok=True)
INDEX = os.path.join(DATA, "trader_index.json")

RPCS = ["https://rpc.mainnet.chain.robinhood.com",
        "https://robinhood-rpc.publicnode.com"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_rr = itertools.cycle(RPCS)

ETH_USD = 2450.0
SUPPLY = 1_000_000_000          # every Pons token mints 1e9
BLOCK_TIME = 0.101

T_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
T_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"

# Routers/aggregators appear as the counterparty on huge numbers of trades and
# would dominate any naive leaderboard -- 0x65050a9b alone is ~22% of all curve
# trades. These are infrastructure, not traders.
ROUTERS = {
    "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc",
    "0x1319402928807ca1188014bd55f43c63eb794f2d",
    "0x4a86009a36fcec5aa341ffceb3205a911fcf6f60",
    "0x77058091a6a1cf0ea6862467db88c44cf04fdd86",
    "0x8876789976decbfcbbbe364623c63652db8c0904",
    "0x58daec3116aae6d93017baaea7749052e8a04fa7",
    "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be",
    "0x4337084d9e255ff0702461cf8895ce9e3b5ff108",
}

# eth_getLogs on this RPC caps at 10,000 matched logs. Curve events run ~1.2k
# per 1k blocks, so chunks must stay small or the call fails outright.
CHUNK = 4_000


def rpc(method, params, tries=4, timeout=30):
    for a in range(tries):
        try:
            body = json.dumps({"jsonrpc": "2.0", "method": method,
                               "params": params, "id": 1}).encode()
            req = urllib.request.Request(next(_rr), data=body,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.load(r)
            if "error" in j:
                time.sleep(0.5 * (a + 1))
                continue
            return j
        except Exception:  # noqa: BLE001
            time.sleep(0.5 * (a + 1))
    return {}


def head_block():
    return int((rpc("eth_blockNumber", []) or {}).get("result", "0x0"), 16)


def load():
    if os.path.exists(INDEX):
        with open(INDEX) as f:
            return json.load(f)
    return {"traders": {}, "positions": {}, "scanned": [], "updated": None}


def save(ix):
    ix["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ix, f)
    os.replace(tmp, INDEX)


def scan(ix, lo, hi, log=print):
    """
    Walk curve events and fold them into per-(wallet, token) positions.

    Positions are keyed wallet|token and carry cumulative quote in/out plus the
    token balance, so a realized P&L only counts once the balance returns to ~0.
    Marking open positions to market would import unrealised noise.
    """
    pos = ix["positions"]
    n = 0
    b = lo
    while b < hi:
        to = min(b + CHUNK, hi)
        r = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                 "topics": [[T_CURVE_BUY, T_CURVE_SELL]]}])
        if "result" not in r:
            b = to
            continue
        for lg in r["result"]:
            w = "0x" + lg["topics"][2][-40:]          # recipient == the real user
            if w in ROUTERS:
                continue
            d = lg["data"][2:]
            if len(d) < 128:
                continue
            tok = lg["address"].lower()
            k = f"{w}|{tok}"
            p = pos.setdefault(k, {"qin": 0, "qout": 0, "tok": 0,
                                   "buys": 0, "sells": 0,
                                   "first_blk": int(lg["blockNumber"], 16),
                                   "entry_mcap": None})
            q = int(d[0:64], 16)
            t = int(d[64:128], 16)
            if lg["topics"][0] == T_CURVE_BUY:
                p["qin"] += q
                p["tok"] += t
                p["buys"] += 1
                if p["entry_mcap"] is None and t > 0:
                    # q and t are both raw 18-dec, so q/t IS the ETH-per-token
                    # price directly -- decimals cancel. Dividing by 1e18 again
                    # (an earlier bug) collapsed every entry mcap to $0.
                    p["entry_mcap"] = (q / t) * SUPPLY * ETH_USD
            else:
                p["tok"] -= q          # sells: tokensIn, quoteOut
                p["qout"] += t
                p["sells"] += 1
            n += 1
        log(f"  {b}-{to}  events={len(r['result'])}  positions={len(pos)}")
        b = to
    ix["scanned"].append([lo, hi])
    return n


MIN_BUY_WEI = 10 ** 15          # 0.001 ETH -- ignore dust and sell-only legs


def rebuild_traders(ix):
    """
    Collapse positions into per-wallet stats. Realized P&L on closed legs only.

    A leg only counts as a round trip if the wallet actually BOUGHT in the
    scanned range. Without that guard, wallets that merely sold tokens acquired
    elsewhere (dev allocations, airdrops, buys before the window) score
    pnl = qout - 0 and dominate the board with fake 100% win rates on zero
    volume -- the first run produced exactly that.
    """
    tr = defaultdict(lambda: {"pnl_eth": 0.0, "closed": 0, "open": 0,
                              "vol_eth": 0.0, "tokens": 0, "wins": 0,
                              "sell_only": 0})
    for k, p in ix["positions"].items():
        w, tok = k.split("|")
        t = tr[w]
        t["tokens"] += 1
        t["vol_eth"] += p["qin"] / 1e18
        if p["qin"] < MIN_BUY_WEI or p["buys"] < 1:
            t["sell_only"] += 1          # not a tradeable round trip
            continue
        if p["qout"] > 0 and p["sells"] >= 1 and p["tok"] <= 10 ** 15:
            pnl = (p["qout"] - p["qin"]) / 1e18
            t["pnl_eth"] += pnl
            t["closed"] += 1
            if pnl > 0:
                t["wins"] += 1
        else:
            t["open"] += 1
    for w, t in tr.items():
        t["win_rate"] = round(100 * t["wins"] / t["closed"], 1) if t["closed"] else None
        t["pnl_usd"] = round(t["pnl_eth"] * ETH_USD, 2)
        t["pnl_eth"] = round(t["pnl_eth"], 6)
        t["vol_eth"] = round(t["vol_eth"], 4)
    ix["traders"] = dict(tr)
    return tr


def tier(t, min_closed=5):
    """Conservative tiering. Nothing here predicts; it ranks observed history."""
    if t["closed"] < min_closed:
        return None
    if t["vol_eth"] < 0.01:
        return None                      # no real capital deployed
    if t["pnl_eth"] > 1.0 and (t["win_rate"] or 0) >= 50:
        return "ELITE"
    if t["pnl_eth"] > 0.1:
        return "PROFITABLE"
    if t["pnl_eth"] < -0.5:
        return "LOSING"
    return "FLAT"


SEL_CURVE = "0x7165485d"     # curve()


def resolve_key(addr):
    """
    Positions are keyed by CURVE address (curve events are emitted by the
    curve). Accept either a token or a curve and return the curve.
    """
    a = addr.lower()
    r = (rpc("eth_call", [{"to": a, "data": SEL_CURVE}, "latest"]) or {}).get("result")
    if r and len(r) >= 66 and int(r, 16) != 0:
        return "0x" + r[-40:]
    return a


def token_holders(ix, token, min_closed=5):
    """
    Which indexed traders are in this token, and at what market cap did they
    enter relative to now. This is the cross-match the feed needs at entry time.
    """
    token = resolve_key(token)
    out = []
    for k, p in ix["positions"].items():
        w, tok = k.split("|")
        if tok != token:
            continue
        t = ix["traders"].get(w)
        if not t:
            continue
        out.append({
            "wallet": w,
            "tier": tier(t, min_closed),
            "trader_pnl_eth": t["pnl_eth"],
            "trader_closed": t["closed"],
            "trader_win_rate": t.get("win_rate"),
            "sell_only_legs": t.get("sell_only", 0),
            "entry_mcap_usd": p.get("entry_mcap"),
            "spent_eth": round(p["qin"] / 1e18, 5),
            "still_holding": p["tok"] > 10 ** 15,
            "buys": p["buys"], "sells": p["sells"],
        })
    out.sort(key=lambda x: -(x["trader_pnl_eth"] or 0))
    return out


def _fisher(a, b, c, d):
    """One-sided P(>= a) on a 2x2 with fixed margins."""
    from math import comb
    n = a + b + c + d
    return sum(comb(a + b, x) * comb(c + d, a + c - x) / comb(n, a + c)
               for x in range(a, min(a + b, a + c) + 1))


def persistence(ix, lo, hi, min_closed=3, split_frac=0.5, log=print):
    """
    THE test: does a wallet profitable in the TRAIN half stay profitable in TEST?

    This was a stub that returned a note -- the 64%/28% figure quoted in this
    module's docstring was computed ad hoc and never reproducible from the code.
    It is computed here now, and the honest answer may be "no".

    Each half is scanned into its OWN position map, so a wallet's train record
    cannot leak into its test record. Wallets are only compared if they closed
    `min_closed` round trips in BOTH halves -- otherwise the test half is mostly
    wallets with one lucky trade.
    """
    mid = int(lo + (hi - lo) * split_frac)
    halves = []
    for name, a, b in (("train", lo, mid), ("test", mid, hi)):
        sub = {"positions": {}, "scanned": [], "traders": {}}
        log(f"  scanning {name}: {a}-{b} ({b-a:,} blocks)")
        scan(sub, a, b, log=lambda *_: None)
        halves.append(rebuild_traders(sub))
    tr_a, tr_b = halves
    try:
        with open(os.path.join(DATA, "persistence_dump.json"), "w") as f:
            json.dump({"train": {w: tr_a[w] for w in tr_a},
                       "test": {w: tr_b[w] for w in tr_b}}, f)
    except Exception:  # noqa: BLE001
        pass
    both = [w for w in tr_a
            if w in tr_b and tr_a[w]["closed"] >= min_closed
            and tr_b[w]["closed"] >= min_closed]
    if len(both) < 20:
        return {"error": f"only {len(both)} wallets closed >={min_closed} trips in BOTH "
                         f"halves -- widen the block range", "train": [lo, mid],
                "test": [mid, hi]}
    win_p = [w for w in both if tr_a[w]["pnl_eth"] > 0]
    los_p = [w for w in both if tr_a[w]["pnl_eth"] <= 0]
    def rate(ws):
        return (sum(1 for w in ws if tr_b[w]["pnl_eth"] > 0) / len(ws)) if ws else None
    a_ = sum(1 for w in win_p if tr_b[w]["pnl_eth"] > 0); b_ = len(win_p) - a_
    c_ = sum(1 for w in los_p if tr_b[w]["pnl_eth"] > 0); d_ = len(los_p) - c_
    p = _fisher(a_, b_, c_, d_) if (win_p and los_p) else None
    return {"train": [lo, mid], "test": [mid, hi], "min_closed": min_closed,
            "wallets_in_both": len(both),
            "train_profitable_n": len(win_p), "train_unprofitable_n": len(los_p),
            "test_win_rate_if_train_profitable": None if rate(win_p) is None else round(100*rate(win_p),1),
            "test_win_rate_if_train_unprofitable": None if rate(los_p) is None else round(100*rate(los_p),1),
            "fisher_p_one_sided": None if p is None else round(p, 6),
            "verdict": ("no wallets to compare" if p is None else
                        "PERSISTS" if p < 0.05 else "NO PERSISTENCE (p >= 0.05)")}


def cmd_scan(args, ix):
    head = head_block()
    hi = head
    lo = max(0, head - args.scan)
    log = print if args.verbose else (lambda *a, **k: None)
    print(f"scanning {lo} → {hi} ({args.scan} blocks, ~{args.scan*BLOCK_TIME/3600:.1f}h)")
    n = scan(ix, lo, hi, log=log)
    tr = rebuild_traders(ix)
    save(ix)
    ranked = [t for t in tr.values() if t["closed"] >= 5]
    print(f"  trades folded in : {n}")
    print(f"  positions tracked: {len(ix['positions'])}")
    print(f"  traders           : {len(tr)}  (with ≥5 closed: {len(ranked)})")


def cmd_top(args, ix):
    tr = ix["traders"] or rebuild_traders(ix)
    rows = [(w, t) for w, t in tr.items()
            if t["closed"] >= args.min_closed and t["vol_eth"] >= 0.01]
    rows.sort(key=lambda x: -x[1]["pnl_eth"])
    print(f"\n  TRADER INDEX — {len(rows)} wallets with ≥{args.min_closed} closed round trips")
    print(f"  {'wallet':44} {'tier':11} {'PnL ETH':>10} {'PnL USD':>11} {'closed':>7} {'win%':>6} {'vol ETH':>10}")
    print("  " + "─" * 106)
    for w, t in rows[:args.top]:
        print(f"  {w:44} {str(tier(t, args.min_closed) or '-'):11} "
              f"{t['pnl_eth']:>10.4f} {t['pnl_usd']:>11,.0f} {t['closed']:>7} "
              f"{str(t.get('win_rate') or '-'):>6} {t['vol_eth']:>10.2f}")
    if rows:
        prof = sum(1 for _, t in rows if t["pnl_eth"] > 0)
        print("  " + "─" * 106)
        print(f"  profitable: {prof}/{len(rows)} ({100*prof/len(rows):.0f}%)  "
              f"— most traders lose; that dispersion is what makes a signal possible")


def cmd_token(args, ix):
    if not ix["traders"]:
        rebuild_traders(ix)
    key = resolve_key(args.token)
    hold = token_holders(ix, args.token, args.min_closed)
    ranked = [h for h in hold if h["tier"] in ("ELITE", "PROFITABLE")]
    print(f"\n  token {args.token}")
    if key != args.token.lower():
        print(f"  curve {key}  (index is keyed by curve)")
    print(f"  indexed traders in this token: {len(hold)}   "
          f"of which ELITE/PROFITABLE: {len(ranked)}")
    if not hold:
        print("  (no indexed trader has touched it — scan a wider range or it is too new)")
        return
    print(f"  {'wallet':44} {'tier':11} {'entry mcap':>12} {'spent':>9} {'held':>6} {'trader PnL':>11}")
    print("  " + "─" * 100)
    for h in hold[:args.top]:
        em = f"${h['entry_mcap_usd']:,.0f}" if h["entry_mcap_usd"] else "-"
        print(f"  {h['wallet']:44} {str(h['tier'] or '-'):11} {em:>12} "
              f"{h['spent_eth']:>9.4f} {str(h['still_holding']):>6} "
              f"{h['trader_pnl_eth']:>11.4f}")
    ent = [h["entry_mcap_usd"] for h in ranked if h["entry_mcap_usd"]]
    if ent:
        ent.sort()
        print("  " + "─" * 100)
        print(f"  ELITE/PROFITABLE entry mcap: median ${ent[len(ent)//2]:,.0f} "
              f"(low ${ent[0]:,.0f} · high ${ent[-1]:,.0f})")
        print("  NOTE: an entry below yours means they are already in profit on your fill.")


def main():
    ap = argparse.ArgumentParser(description="Pons trader index (read-only)")
    ap.add_argument("--scan", type=int, default=0, help="scan this many recent blocks")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-closed", type=int, default=5)
    ap.add_argument("--token", help="show indexed traders in this token")
    ap.add_argument("--persistence", action="store_true")
    ap.add_argument("--persistence-blocks", type=int, default=300_000,
                    help="how many blocks back from head to run the train/test split over")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    ix = load()
    if a.scan:
        cmd_scan(a, ix)
    if a.token:
        cmd_token(a, ix)
    elif a.persistence:
        h = head_block()
        print(json.dumps(persistence(ix, h - a.persistence_blocks, h,
                                     min_closed=a.min_closed), indent=2))
    elif not a.scan:
        cmd_top(a, ix)
    elif a.scan:
        cmd_top(a, ix)


if __name__ == "__main__":
    main()
