#!/usr/bin/env python3
"""
Cached bonding-curve price tapes -- the missing substrate for every exit question.

WHY THIS EXISTS
    Nothing about a curve's price history was cached. Every exit idea needed a
    fresh RPC sweep, so sweeping a trailing-stop parameter over N settings meant
    N full chain scans and nobody ever ran one. Fetch once, replay offline.

PRICING IS EXACT, AND DECIMAL-FREE
    The curve's own Buy/Sell events carry both legs of every fill:
        Buy (topic T_CURVE_BUY):  [quote_in_gross, tokens_out, fee, fee]
        Sell(topic T_CURVE_SELL): [tokens_in, quote_out, fee, fee]
    so price = quote/token straight off the log. No archive node, no eth_call at
    a historical block, no modelled curve formula.

    Critically, a RETURN is a ratio of two such prices on the SAME curve, so the
    quote's decimals cancel and never need to be known. That matters here more
    than usual -- see the quote-asset note below.

    This replaces data/grad_forward.jsonl's `mcap` field, which is a first-sight
    snapshot: 75.1% of heating/near_grad pairs carry byte-identical mcap even
    though ETH raised moved (measured 2026-09-13), so any ratio built from it
    reads 1.00x at every percentile.

THE QUOTE ASSET IS NOT ALWAYS ETH
    Measured on 2,223 graduations: 62.9% native ETH, 13.7% USDG (6 decimals),
    and the rest tokenized equities -- GOOGL, NVDA, SPY, SPCX (18 decimals).
    Graduation targets differ per quote (4.2 ETH / 8,090 USDG / 41.6 NVDA), so
    there is no single "graduates at 4 ETH" rule.

    Consequence for anyone using this data: a return measured here is denominated
    in that curve's QUOTE. For a NVDA-quoted curve a 2.0x is 2.0x in NVDA terms,
    and the USD result also depends on what NVDA did. Filter on quote before
    comparing across curves or converting to USD.

    The curve does not self-describe: trackedQuote() and graduationThreshold()
    both return None on live curves (verified on 5 curves spanning 5 quotes), so
    the quote is carried in from the graduation pool where known and left null
    otherwise. Null quote does NOT invalidate the tape -- ratios still hold.

FAIL-CLOSED
    A curve whose scan raises is written with ok=false and NO trades, never as an
    empty-but-successful tape. Silent holes read as "no trading" and that is the
    failure this codebase keeps re-learning.
"""
import json
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launch_monitor as L  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAPES = os.path.join(HERE, "data", "curve_tapes.jsonl")

BUY, SELL = 0, 1


def decode_tape(logs):
    """Normalise raw curve logs into a compact, sorted price tape."""
    out, completed = [], None
    for lg in logs:
        t0 = lg["topics"][0]
        d = lg["data"][2:]
        blk = int(lg["blockNumber"], 16)
        li = int(lg.get("logIndex", "0x0"), 16)
        if t0 == L.T_CURVE_BUY and len(d) >= 256 and len(lg["topics"]) > 1:
            w = [int(d[i:i + 64], 16) for i in range(0, 256, 64)]
            q, tok, fee = w[0], w[1], w[2]
            if q and tok:
                out.append([blk, li, BUY, q, tok, fee, "0x" + lg["topics"][1][-40:]])
        elif t0 == L.T_CURVE_SELL and len(d) >= 256 and len(lg["topics"]) > 1:
            w = [int(d[i:i + 64], 16) for i in range(0, 256, 64)]
            tok, q, fee = w[0], w[1], w[2]
            if q and tok:
                out.append([blk, li, SELL, q, tok, fee, "0x" + lg["topics"][1][-40:]])
        elif t0 == L.T_CURVE_COMPLETED and len(d) >= 192:
            w = [int(d[i:i + 64], 16) for i in range(0, 192, 64)]
            completed = {"block": blk, "raise": w[1], "lp_tokens": w[2]}
    out.sort(key=lambda r: (r[0], r[1]))
    return out, completed


def build_one(curve, anchor_block, head):
    """
    One curve's tape. Returns a record dict; ok=False means the scan failed and
    the absence of trades must NOT be read as an absence of trading.
    """
    rec = {"curve": curve, "ok": False, "ts": time.time()}
    try:
        logs = L.curve_trade_logs(curve, anchor_block, head, pre_grad=False)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"[:200]
        return rec
    trades, completed = decode_tape(logs)
    rec["ok"] = True
    rec["n_logs"] = len(logs)
    rec["n_trades"] = len(trades)
    rec["completed"] = completed
    if trades:
        rec["first_block"] = trades[0][0]
        rec["last_block"] = trades[-1][0]
        # [block, kind, quote, token, fee] -- logIndex and wallet dropped to keep
        # the file small; re-fetch if wallet-level questions come up later.
        rec["trades"] = [[t[0], t[2], t[3], t[4], t[5]] for t in trades]
    else:
        rec["trades"] = []
    return rec


def load_cache(path=TAPES):
    have = {}
    if not os.path.exists(path):
        return have
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            # later line wins, so a re-run can repair a failed curve in place
            have[r["curve"]] = r
    return have


def sources_crossings(limit=None):
    """
    Curves that crossed the heating threshold, INCLUDING the ones that never
    graduated.

    THIS IS THE UNBIASED SOURCE AND THE DEFAULT FOR ANY ENTRY STUDY.
    sources_grads() below lists graduations only, so a leg measured against it
    answers "what did the winners return" -- survivorship bias, not the return
    of a strategy that must also hold the 80.9% that stall. Only 19.1% of
    heating curves ever reach near-graduation, so the failures ARE the result.

    Whether each curve graduated is then read from its own tape (a CurveCompleted
    event is present or it is not), never assumed from the source list.
    """
    p = os.path.join(HERE, "data", "grad_forward.jsonl")
    seen, out = set(), []
    rows = []
    with open(p) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    # earliest crossing per curve -- that is the closest anchor to creation
    first = {}
    for r in rows:
        c = r.get("curve")
        if not c or not r.get("block"):
            continue
        if c not in first or r["block"] < first[c]["block"]:
            first[c] = r
    for c, r in sorted(first.items(), key=lambda kv: -kv[1]["block"]):
        if c in seen:
            continue
        seen.add(c)
        out.append({"curve": c, "grad_block": r["block"], "quote": None,
                    "token": r.get("token"), "symbol": r.get("symbol"),
                    "src": "crossing"})
        if limit and len(out) >= limit:
            break
    return out


def sources(limit=None, only_ok_quote=None):
    """Graduated curves only -- SURVIVORSHIP-BIASED, see sources_crossings()."""
    p = os.path.join(HERE, "data", "paper_trades.jsonl")
    seen, out = set(), []
    rows = []
    with open(p) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    for r in reversed(rows):
        c = r.get("curve")
        if not c or c in seen or not r.get("grad_block"):
            continue
        # quote is the post-fix schema marker; pre-fix rows predate the quote fix
        if r.get("quote") is None:
            continue
        if only_ok_quote and r["quote"].lower() != only_ok_quote.lower():
            continue
        seen.add(c)
        out.append({"curve": c, "grad_block": r["grad_block"], "quote": r["quote"],
                    "token": r.get("token"), "symbol": r.get("symbol")})
        if limit and len(out) >= limit:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--quote", default=None,
                    help="only curves with this quote asset (0x0 = native ETH)")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--source", choices=("crossings", "grads"), default="crossings",
                    help="crossings = unbiased (includes stalls); grads = winners only")
    a = ap.parse_args()

    if a.stats:
        have = load_cache()
        ok = [r for r in have.values() if r.get("ok")]
        bad = [r for r in have.values() if not r.get("ok")]
        tot = sum(r.get("n_trades", 0) for r in ok)
        print(f"  tapes cached : {len(have):,}   ok={len(ok):,}  failed={len(bad):,}")
        print(f"  trades       : {tot:,}")
        if ok:
            ns = sorted(r["n_trades"] for r in ok)
            print(f"  trades/curve : med={ns[len(ns) // 2]}  p90={ns[int(.9 * len(ns))]}  max={max(ns)}")
            gc = sum(1 for r in ok if r.get("completed"))
            print(f"  with CurveCompleted: {gc:,}/{len(ok):,}")
        if bad:
            print(f"  NOTE {len(bad)} failed scans are recorded ok=false, not as empty tapes")
        return

    if not a.build:
        ap.print_help()
        return

    have = load_cache()
    src = (sources_crossings(limit=a.limit) if a.source == "crossings"
           else sources(limit=a.limit, only_ok_quote=a.quote))
    todo = [s for s in src if not have.get(s["curve"], {}).get("ok")]
    print(f"  cached ok: {sum(1 for r in have.values() if r.get('ok')):,}   to fetch: {len(todo):,}")
    if not todo:
        return
    head = L.head_block()
    t0 = time.time()
    done = fails = 0
    with open(TAPES, "a") as f:
        for i, s in enumerate(todo, 1):
            rec = build_one(s["curve"], s["grad_block"], head)
            rec["src"] = s.get("src", "grads")   # provenance: crossings = unbiased
            rec["quote"] = s["quote"]
            rec["symbol"] = s.get("symbol")
            rec["grad_block"] = s["grad_block"]
            f.write(json.dumps(rec) + "\n")
            f.flush()
            done += rec["ok"]
            fails += (not rec["ok"])
            if i % 10 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"    {i}/{len(todo)}  ok={done} fail={fails}  "
                      f"{el / i:.2f}s/curve  eta {(len(todo) - i) * el / i / 60:.1f}m",
                      flush=True)
    print(f"  done: {done} taped, {fails} failed")


if __name__ == "__main__":
    main()
