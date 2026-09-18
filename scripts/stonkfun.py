#!/usr/bin/env python3
"""
StonkFun (Solana / Raydium LaunchLab) puller.

WHY THIS VENUE IS WORTH THE TROUBLE
    Its public API hands over, with no key and no signup, the two things that cost
    days of work to derive on Robinhood Chain:

      graduationProgress  -- the metric the RHC tape study showed decides the trade
                             (pf 2.79 entering at 10% of supply sold, 0.77 at 40%)
      quote               -- the STOCK PAIR a token launched against (NVDAX, SPYX,
                             TSLAX, HOODX, RBLX ...), plus its category

    So two questions become answerable here that were not answerable there:
      1. does the early-entry edge replicate on a different venue and chain? If it
         does it is a bonding-curve property, not a Robinhood-Chain quirk.
      2. do tokens launched against a NEWLY INTRODUCED pair outperform? That is the
         operator's own thesis, and on RHC it could only be inferred from
         graduations -- which produced three wrong answers before the data ran out.

WHAT IS AND IS NOT DONE HERE
    READ ONLY. The same API documents launching a token, which requires signing a
    fee transaction. Nothing in this file signs anything, and nothing should.

PAGINATION, MEASURED
    page= works; limit=, offset= and cursor= are ignored and the page size is fixed
    at 25. 71,146 tokens across 2,846 pages at the time of writing.

FAIL-CLOSED
    A page that errors is recorded as a gap and retried, never skipped. Silently
    advancing past a failed page is how a partial pull becomes indistinguishable
    from a complete one -- the single most repeated bug in this project.
"""
import json
import os
import sys
import time
import argparse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
TOKENS = os.path.join(DATA, "stonkfun_tokens.jsonl")
PAIRS = os.path.join(DATA, "stonkfun_pairs.jsonl")
STATE = os.path.join(DATA, "stonkfun_state.json")

BASE = "https://www.stonkfun.xyz/api/public/v1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PAGE_SIZE = 25
PAUSE = 0.35             # polite; the API is free and unauthenticated


def _get(path, tries=4, timeout=25):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(f"{BASE}/{path}",
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"[:90]
            time.sleep(0.6 * (a + 1))
    raise RuntimeError(last or "unknown")


def fetch_pairs(log=print):
    """Snapshot the launchable pair list. Appended with a timestamp so the file is a
    HISTORY -- diffing consecutive snapshots is what identifies a brand-new pair,
    which a single current-state list can never tell you."""
    d = _get("pairs?launchable=true")
    pairs = d.get("data") if isinstance(d.get("data"), list) else (
        d.get("data", {}).get("pairs") or d.get("pairs") or [])
    ts = time.time()
    with open(PAIRS, "a") as f:
        f.write(json.dumps({"ts": ts, "n": len(pairs), "pairs": pairs}) + "\n")
    log(f"  pairs snapshot: {len(pairs)} launchable")
    return pairs


def _state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:  # noqa: BLE001
            pass
    return {"done_pages": [], "gaps": []}


def _save_state(st):
    json.dump(st, open(STATE, "w"))


def backfill(max_pages=None, log=print):
    st = _state()
    done = set(st.get("done_pages") or [])
    first = _get("tokens?sort=newest&page=1")
    pg = first["data"]["pagination"]
    total_pages = pg["totalPages"]
    log(f"  {pg['total']:,} tokens across {total_pages:,} pages "
        f"({len(done):,} pages already pulled)")
    todo = [p for p in range(1, total_pages + 1) if p not in done]
    if max_pages:
        todo = todo[:max_pages]
    log(f"  fetching {len(todo):,} pages")
    t0 = time.time()
    gaps = list(st.get("gaps") or [])
    n_rows = 0
    with open(TOKENS, "a") as f:
        for i, p in enumerate(todo, 1):
            try:
                d = _get(f"tokens?sort=newest&page={p}")
                toks = d["data"]["tokens"]
            except Exception as e:  # noqa: BLE001
                # RECORD THE GAP. Advancing past a failed page would make a partial
                # pull look complete, which is the bug this project keeps re-living.
                gaps.append({"page": p, "error": str(e)[:80], "ts": time.time()})
                continue
            stamp = time.time()
            for t in toks:
                t["_fetched"] = stamp
                t["_page"] = p
                f.write(json.dumps(t) + "\n")
            n_rows += len(toks)
            done.add(p)
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                f.flush()
                st["done_pages"] = sorted(done)
                st["gaps"] = gaps
                _save_state(st)
                log(f"    {i}/{len(todo)} pages  {n_rows:,} rows  "
                    f"{el/i:.2f}s/page  eta {(len(todo)-i)*el/i/60:.1f}m  "
                    f"gaps={len(gaps)}", flush=True)
            time.sleep(PAUSE)
    st["done_pages"] = sorted(done)
    st["gaps"] = gaps
    _save_state(st)
    log(f"  done: {n_rows:,} rows, {len(done):,} pages pulled, {len(gaps)} gaps")
    if gaps:
        log("  NOTE gaps are recorded, not skipped -- re-run to retry them")


def stats(log=print):
    if not os.path.exists(TOKENS):
        return log("  no token file yet")
    n = 0
    mints = set()
    quotes = {}
    with open(TOKENS) as f:
        for line in f:
            try:
                t = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            n += 1
            mints.add(t.get("mint"))
            q = (t.get("quote") or {}).get("symbol")
            quotes[q] = quotes.get(q, 0) + 1
    st = _state()
    log(f"  rows {n:,}   distinct mints {len(mints):,}")
    log(f"  pages pulled {len(st.get('done_pages') or []):,}   "
        f"gaps {len(st.get('gaps') or [])}")
    top = sorted(quotes.items(), key=lambda kv: -kv[1])[:12]
    log("  top quote pairs: " + ", ".join(f"{k}:{v:,}" for k, v in top))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--pairs", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--max-pages", type=int, default=None)
    a = ap.parse_args()
    if a.pairs:
        fetch_pairs()
    elif a.backfill:
        backfill(max_pages=a.max_pages)
    elif a.stats:
        stats()
    else:
        ap.print_help()
