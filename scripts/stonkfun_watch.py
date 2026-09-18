#!/usr/bin/env python3
"""
Alert on the first tokens launched against a pair -- the strongest signal measured
on this project.

WHAT IT WATCHES AND WHY
    Measured on 57,137 StonkFun tokens across the 71 pairs with >=200 launches, so
    pair identity is held constant, graduation rate by a token's rank ON ITS PAIR:

        1st-5th      n=  355   22.82%   7.25x
        6th-25th     n=1,420   12.18%   3.87x
        26th-100th   n=5,325    6.61%   2.10x
        101st+       n=50,037   2.38%   0.76x
        overall      n=57,137    3.15%

    Monotonic, on a hard binary outcome. Nearly one in four of the first five
    tokens on a pair graduates, against 2.38% for the long tail -- 9.6x.

    It survives the two confounds that would manufacture it. Restricting to
    high-volume pairs made it STRONGER (7.0x -> 13.8x on the >=$50k peak measure),
    ruling out obscure pairs where every token is trivially "first". And it holds
    within every token-age band (6.00% vs 1.00% at 3-7 days, 7.79% vs 1.40% at
    7-14), so it is not just older tokens having had longer to run.

WHY THIS ONE IS DIFFERENT FROM EVERY OTHER SIGNAL HERE
    It is knowable AT LAUNCH. No tape scan, no buyer history, no waiting for a
    curve to mature. Three separate Robinhood-Chain signals -- the clip score, the
    bad-wallet share, the entry-timing edge -- were all blocked by the same problem:
    we look too early for them to have data. This one REWARDS looking early.

    It is also rare by construction: 355 of 57,137 rows. Expect a handful of alerts
    a day, not a stream. That is the point.

SCOPE
    Solana / Raydium LaunchLab via the public StonkFun API. READ ONLY -- the same
    API documents launching a token, which requires signing a fee transaction.
    Nothing here signs anything.

    This is measured on StonkFun only. Robinhood Chain has the same structure
    available (37% of graduations are non-ETH quoted) but the result has NOT been
    shown to transfer, and must not be assumed to.
"""
import json
import os
import sys
import time
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stonkfun as SF  # noqa: E402

DATA = SF.DATA
STATE = os.path.join(DATA, "stonkfun_watch_state.json")
SEEN = os.path.join(DATA, "stonkfun_alerts.jsonl")

ALERT_RANK = 5           # alert on a token that is this early on its pair
NEW_PAIR_HOURS = 48.0    # a pair first seen this recently is itself newsworthy
POLL = 45.0


def seed_counts(log=print):
    """Tokens already launched per pair, from the backfill. Without this every pair
    would look brand new on first run and the very first poll would alert on
    everything."""
    counts = defaultdict(int)
    first_seen = {}
    n = 0
    path = os.path.join(DATA, "stonkfun_tokens.jsonl")
    if not os.path.exists(path):
        log("  no backfill found -- run: python3 scripts/stonkfun.py --backfill")
        return counts, first_seen, set()
    mints = set()
    with open(path) as f:
        for line in f:
            try:
                t = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            m = t.get("mint")
            if not m or m in mints:
                continue
            mints.add(m)
            q = (t.get("quote") or {}).get("mint")
            if not q:
                continue
            counts[q] += 1
            c = t.get("createdAt")
            if c and (q not in first_seen or c < first_seen[q]):
                first_seen[q] = c
            n += 1
    log(f"  seeded {n:,} tokens across {len(counts):,} pairs")
    return counts, first_seen, mints


def load_state():
    if os.path.exists(STATE):
        try:
            d = json.load(open(STATE))
            return (defaultdict(int, d.get("counts") or {}),
                    d.get("first_seen") or {}, set(d.get("alerted") or []))
        except Exception:  # noqa: BLE001
            pass
    return None


def save_state(counts, first_seen, alerted):
    json.dump({"counts": dict(counts), "first_seen": first_seen,
               "alerted": sorted(alerted)[-5000:]}, open(STATE, "w"))


def notify(tok, rank, pair_age_h, dry=False, log=print):
    q = tok.get("quote") or {}
    m = tok.get("market") or {}
    title = f"#{rank} on ${q.get('symbol')} — ${tok.get('symbol')}"
    lines = [
        f"**{tok.get('name')}** is token **#{rank}** launched against "
        f"**${q.get('symbol')}** ({q.get('categoryLabel') or '—'}).",
        "",
        f"rank 1-5 on a pair graduates **22.8%** of the time vs **2.4%** "
        f"for the long tail (n=57,137, within-pair, 9.6x).",
        "",
        f"mcap ${m.get('marketCapUsd') or 0:,.0f} · liq ${m.get('liquidityUsd') or 0:,.0f} "
        f"· progress {100*(tok.get('graduationProgress') or 0):.2f}%",
        f"pair first used {pair_age_h:.0f}h ago" if pair_age_h is not None else "",
        f"`{tok.get('mint')}`",
    ]
    body = "\n".join(x for x in lines if x)
    if dry:
        log(f"  [dry] {title}\n{body}\n")
        return True
    try:
        import urllib.request
        import alerts
        url = alerts.webhook_url()
        if not url:
            log("  DISCORD_WEBHOOK_URL not set — printing instead")
            log(f"  {title}: {body[:160]}")
            return False
        payload = {"embeds": [{"title": title, "description": body,
                               "color": 0x2ECC71,
                               "url": f"https://www.stonkfun.xyz/token/{tok.get('mint')}"}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "HoodSniper/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 204)
    except Exception as e:  # noqa: BLE001
        log(f"  alert failed: {type(e).__name__}: {str(e)[:80]}")
        return False


def run(once=False, dry=False, log=print):
    st = load_state()
    if st is None:
        counts, first_seen, mints = seed_counts(log)
        alerted = set()
        save_state(counts, first_seen, alerted)
    else:
        counts, first_seen, alerted = st
        log(f"  resumed: {len(counts):,} pairs, {len(alerted):,} already alerted")
    while True:
        try:
            d = SF._get("tokens?sort=newest&page=1")
            toks = d["data"]["tokens"]
        except Exception as e:  # noqa: BLE001
            log(f"  fetch failed: {type(e).__name__}")
            if once:
                return
            time.sleep(POLL)
            continue
        # oldest first so ranks increment in launch order
        for t in sorted(toks, key=lambda x: x.get("createdAt") or ""):
            mint = t.get("mint")
            q = (t.get("quote") or {}).get("mint")
            if not mint or not q or mint in alerted:
                continue
            created = t.get("createdAt")
            known_first = first_seen.get(q)
            if not known_first or (created and created < known_first):
                first_seen[q] = created
            # rank is 1-based: a pair we have never seen makes this token #1
            counts[q] += 1
            rank = counts[q]
            alerted.add(mint)
            if rank <= ALERT_RANK:
                age_h = None
                if first_seen.get(q):
                    try:
                        import datetime as dt
                        f0 = dt.datetime.fromisoformat(first_seen[q].replace("Z", "+00:00"))
                        age_h = (dt.datetime.now(dt.timezone.utc) - f0).total_seconds() / 3600
                    except Exception:  # noqa: BLE001
                        pass
                ok = notify(t, rank, age_h, dry=dry, log=log)
                with open(SEEN, "a") as f:
                    f.write(json.dumps({"ts": time.time(), "mint": mint,
                                        "symbol": t.get("symbol"), "rank": rank,
                                        "quote": (t.get("quote") or {}).get("symbol"),
                                        "sent": bool(ok)}) + "\n")
                log(f"  ALERT #{rank} ${t.get('symbol')} on "
                    f"${(t.get('quote') or {}).get('symbol')} sent={ok}")
        save_state(counts, first_seen, alerted)
        if once:
            return
        time.sleep(POLL)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true", help="print alerts, send nothing")
    a = ap.parse_args()
    run(once=a.once, dry=a.dry)
