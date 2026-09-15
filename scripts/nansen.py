#!/usr/bin/env python3
"""
Nansen API — fills the P&L gap in the smart-money index.

WHY THIS EXISTS. top_traders.py disqualifies wallets with negative realized P&L,
but that check only reached 220 of 558 survivors: realized P&L needs a SELL, and
trader_index.py only sees Pons curve round trips. A wallet that bought on the curve
and sold somewhere else -- or holds -- was unmeasurable, so "not underwater" was a
guarantee that did not actually apply to most of the list.

Nansen indexes Robinhood Chain (from 30 Apr 2026, confirmed in their coverage table)
and returns realized P&L, ROI and win rate per address. That is the same question,
answered from a source that sees the whole chain rather than one venue.

AUTH: header is lowercase `apikey`. Tested: `apiKey`/`Bearer`/`X-API-KEY` all return
401, so the casing is not cosmetic.

CREDITS ARE FINITE and endpoint-priced -- address/labels returned "Insufficient
credits" while pnl-summary returned 200 on the same key. So every call here is
cached to disk and nothing is called from the live feed.

    python3 nansen.py --pnl 0x<wallet>
    python3 nansen.py --enrich-top     # add P&L to top_traders.json
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

BASE = "https://api.nansen.ai"
CACHE = os.path.join(DATA, "nansen_cache.json")
TOP = os.path.join(DATA, "top_traders.json")
CACHE_HOURS = 12


def _key():
    return env_key("NANSEN_API")


def _cache():
    try:
        return json.load(open(CACHE))
    except Exception:  # noqa: BLE001
        return {}


def _save(c):
    try:
        json.dump(c, open(CACHE, "w"))
    except Exception:  # noqa: BLE001
        pass


def post(path, body, log=print):
    """(status, json) — never raises. 402/403 means out of credits, not 'no data'."""
    k = _key()
    if not k:
        return None, {"error": "NANSEN_API not set"}
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"apikey": k, "Content-Type": "application/json",
                                          "User-Agent": "HoodSniper/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read()[:400])
        except Exception:  # noqa: BLE001
            return e.code, {"error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return None, {"error": str(e)[:100]}


def pnl(address, chain="robinhood", days=30, log=print):
    """
    Realized P&L for one address. Returns dict, or {"error": ...}.

    *** DO NOT USE THIS AS A DISQUALIFIER ON ROBINHOOD CHAIN. ***
    Measured 2026-09-10: every RH wallet tested comes back traded_token_count=1 with
    the single token being ETH, while the same wallets show 11-13 memecoin picks in
    our own index and Nansen itself reports 25-126 trades. So Nansen has indexed the
    CHAIN but not the Pons tokens: the figure it returns is net ETH movement, which
    is mostly gas, not trading P&L.

    Wiring it in as a gate would have cut 28% of the top-traders list (7 of the
    first 25 read "negative") on a number that measures gas costs. `token_coverage`
    is returned so a caller can see this rather than trust the headline figure.

    An error is NOT zero P&L. A wallet Nansen has no data for and a wallet that
    broke even are different facts, and collapsing them would let an unmeasured
    wallet pass a "not underwater" check it was never tested against.
    """
    ck = f"{chain}:{address.lower()}:{days}"
    c = _cache()
    hit = c.get(ck)
    if hit and time.time() - hit.get("_ts", 0) < CACHE_HOURS * 3600:
        return hit
    frm = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    to = time.strftime("%Y-%m-%d", time.gmtime())
    st, body = post("/api/v1/profiler/address/pnl-summary",
                    {"address": address, "chain": chain,
                     "date": {"from": frm, "to": to}}, log)
    if st != 200:
        return {"error": (body or {}).get("message") or (body or {}).get("error")
                or f"HTTP {st}"}
    toks = body.get("top5_tokens") or []
    out = {"address": address.lower(), "chain": chain,
           # the tell: 1 == ETH only, i.e. the venue's tokens are not indexed
           "token_coverage": body.get("traded_token_count"),
           "tokens_seen": [t.get("token_symbol") for t in toks][:5],
           "realized_pnl_usd": body.get("realized_pnl_usd"),
           "realized_pnl_pct": body.get("realized_pnl_percent"),
           "win_rate": body.get("win_rate"),
           "traded_times": body.get("traded_times"),
           "traded_tokens": body.get("traded_token_count"),
           "_ts": time.time()}
    c[ck] = out
    _save(c)
    return out


def enrich_top(limit=None, log=print):
    """Add Nansen P&L to top_traders.json, prioritising the ones we could not measure."""
    if not os.path.exists(TOP):
        log("no top_traders.json — run top_traders.py --export first")
        return
    d = json.load(open(TOP))
    # unmeasured first: those are the ones whose 'not underwater' claim was hollow
    order = sorted(d.items(), key=lambda kv: (kv[1].get("pnl_usd") is not None,
                                              -(kv[1].get("wilson") or 0)))
    if limit:
        order = order[:limit]
    log(f"enriching {len(order)} of {len(d)} wallets (unmeasured first)\n")
    added = failed = 0
    for w, t in order:
        r = pnl(w)
        if r.get("error"):
            failed += 1
            if failed <= 3:
                log(f"  {w[:14]}… {r['error'][:60]}")
            if "credit" in str(r.get("error", "")).lower():
                log("  OUT OF CREDITS — stopping rather than burning the rest")
                break
            continue
        if (r.get("token_coverage") or 0) <= 1:
            # ETH-only coverage: the number is gas, not trading. Recording it would
            # put a plausible-looking figure next to real ones.
            failed += 1
            if failed == 1:
                log(f"  {w[:14]}… coverage is ETH-only ({r.get('tokens_seen')}) — "
                    f"NOT recording, this is not trading P&L")
            continue
        t["nansen_pnl_usd"] = r.get("realized_pnl_usd")
        t["nansen_win_rate"] = r.get("win_rate")
        t["nansen_trades"] = r.get("traded_times")
        added += 1
        time.sleep(0.25)
    json.dump(d, open(TOP, "w"), indent=1)
    log(f"\n  enriched {added}, failed {failed} -> {TOP}")
    neg = [w for w, t in d.items()
           if t.get("nansen_pnl_usd") is not None and t["nansen_pnl_usd"] < 0]
    if neg:
        log(f"  {len(neg)} wallets Nansen shows as NEGATIVE that our index could not see")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pnl")
    ap.add_argument("--chain", default="robinhood")
    ap.add_argument("--enrich-top", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.pnl:
        print(json.dumps(pnl(a.pnl, a.chain), indent=1))
    elif a.enrich_top:
        enrich_top(a.limit)
    else:
        ap.print_help()


# --- on-demand investigation ------------------------------------------------
# CREDITS ARE THE CONSTRAINT, so nothing here runs automatically. autovet touches
# every row at ~21 launches/min; wiring Nansen into it would drain the plan in an
# afternoon for data that is only wanted on the handful of coins actually being
# considered. This is called from a keypress and cached hard.
#
# WHAT WORKS ON ROBINHOOD CHAIN, measured 2026-09-10:
#   related-wallets   200  <- carries "First Funder", the useful one
#   counterparties    200
#   labels            403  insufficient credits on this plan
#   first-funder      422  (its own endpoint; related-wallets already returns it)
#   pnl-summary       200  but ETH-ONLY -- see the warning on pnl()
DEEP_CACHE_HOURS = 72


def investigate_address(address, chain="robinhood", log=print):
    """
    Funding + counterparty picture for one address. Two credits, cached 72h.

    Answers the question our own chain data cannot: WHO FUNDED THIS WALLET, and who
    does it transact with. That is the hot-wallet-cluster trap from Phase 0 -- a set
    of "different" wallets all funded by one address is one actor, and no amount of
    per-wallet scoring reveals it.
    """
    ck = f"deep:{chain}:{address.lower()}"
    c = _cache()
    hit = c.get(ck)
    if hit and time.time() - hit.get("_ts", 0) < DEEP_CACHE_HOURS * 3600:
        hit["_cached"] = True
        return hit
    out = {"address": address.lower(), "chain": chain, "_ts": time.time(),
           "funder": None, "related": [], "counterparties": [], "errors": []}

    st, body = post("/api/v1/profiler/address/related-wallets",
                    {"address": address, "chain": chain}, log)
    if st == 200:
        for r in (body.get("data") or []):
            rel = {"address": r.get("address"), "relation": r.get("relation"),
                   "label": r.get("address_label") or None}
            out["related"].append(rel)
            if (r.get("relation") or "").lower().startswith("first funder"):
                out["funder"] = rel["address"]
    else:
        out["errors"].append(f"related-wallets HTTP {st}")

    frm = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
    to = time.strftime("%Y-%m-%d", time.gmtime())
    st, body = post("/api/v1/profiler/address/counterparties",
                    {"address": address, "chain": chain,
                     "date": {"from": frm, "to": to}}, log)
    if st == 200:
        for r in (body.get("data") or [])[:8]:
            out["counterparties"].append(
                {"address": r.get("counterparty_address"),
                 "label": r.get("counterparty_address_label"),
                 "n": r.get("interaction_count"),
                 "usd": r.get("total_volume_usd")})
    else:
        out["errors"].append(f"counterparties HTTP {st}")

    c[ck] = out
    _save(c)
    return out


def shared_funder(addresses, chain="robinhood", log=print):
    """
    Do these wallets share a funder? One credit each, cached.

    This is the actual cluster test: N wallets that look independent but trace to
    one funder are one actor, which turns "5 smart wallets bought" into "1 wallet
    bought 5 times".
    """
    funders = {}
    for a in addresses:
        d = investigate_address(a, chain, log)
        f = d.get("funder")
        if f:
            funders.setdefault(f.lower(), []).append(a.lower())
    return {f: ws for f, ws in funders.items() if len(ws) > 1}
