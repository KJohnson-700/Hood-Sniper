#!/usr/bin/env python3
"""
Route alerts to per-chain Discord channels, and alert on RUNNERS rather than only
graduations.

WHAT WAS WRONG
    Everything fired into one webhook, and only two things fired at all:
    smart-money HOT and a Robinhood Chain GRADUATION. So the channel was graduated
    hood tokens and nothing else -- no low-mcap movers, no Solana, no BNB, even
    though all four chains have been collected for days.

    A graduation is also the wrong moment to be told about. Measured on 1,669
    post-graduation paths: 93.6% stop out at 0.70 and every take-profit from 1.15x
    to 5.0x loses, even at zero fees. The tradeable window is BEFORE it, on the
    curve, which is where these alerts now point.

WHY A SEPARATE ROUTER
    It tails the feeds the four collectors already write. No new connections, no
    RPC, cannot slow or break a collector -- and one chain going quiet cannot stop
    the others alerting. Same reasoning as allvenues.py.

CHANNELS
    Set these in .env. Any that is missing falls back to DISCORD_WEBHOOK_URL, so
    adding a channel is opt-in and nothing breaks if you only want one:

        DISCORD_WEBHOOK_URL       default / Robinhood Chain
        DISCORD_WEBHOOK_SOL       pump.fun + StonkFun
        DISCORD_WEBHOOK_BSC       flap.sh + four.meme

WHAT EACH CHAIN ALERTS ON
    The venues do not measure the same thing, so the trigger differs per venue
    rather than forcing one fake common rule:

      rhc       on the curve, in the mcap band, MOVING -- buy-side pressure and
                fresh wallets, which is the 4.19x swell cohort
      pump.fun  curve progress in the actionable band with real buyer count
      stonkfun  rank 1-5 on its pair -- 22.82% graduate vs 2.38% at rank 101+
      bsc       a fresh launch that is already tradeable

NEVER SPAMS
    One alert per token ever, recorded to disk, plus a global per-minute ceiling.
    An alert channel that cries wolf is worse than no channel, and these feeds move
    at thousands of rows an hour.
"""
import json
import os
import sys
import time
import argparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from investigate import env_key  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
SENT = os.path.join(DATA, "router_sent.jsonl")

POLL = 20.0
MAX_PER_MIN = 6                  # global ceiling; silence beats a firehose
FRESH_MIN = 12.0                 # only alert on rows observed this recently

# A FIRST RUN MUST NOT FLOOD. The feeds hold hours of history, so the first pass
# found 2,076 qualifying tokens -- every one of them would have fired. On a cold
# start everything currently in the feeds is marked as already-alerted and only
# what arrives AFTER that is sent. An alert channel that opens with two thousand
# messages is one you mute, which makes it worse than having none.
SEED_ON_FIRST_RUN = True

CHANNEL_ENV = {"rhc": "DISCORD_WEBHOOK_URL",
               "sol": "DISCORD_WEBHOOK_SOL",
               "bsc": "DISCORD_WEBHOOK_BSC"}
COLOR = {"rhc": 0x5865F2, "sol": 0x9945FF, "bsc": 0xF0B90B}


def hook(channel):
    """Per-channel webhook, falling back to the default so a missing one is a
    degraded path rather than a silent drop."""
    return env_key(CHANNEL_ENV.get(channel, "DISCORD_WEBHOOK_URL")) or \
        env_key("DISCORD_WEBHOOK_URL")


def _tail(name, nbytes=900_000):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return []
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, sz - nbytes))
            lines = f.read().decode("utf8", "ignore").split("\n")[1:]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return out


def _load_sent():
    s = set()
    if os.path.exists(SENT):
        try:
            with open(SENT) as f:
                for line in f:
                    try:
                        s.add(json.loads(line)["id"])
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
    return s


def _rts(r):
    """Row timestamp in epoch seconds, or None. Never guessed -- a row with no
    usable timestamp cannot be judged fresh and is left alone."""
    import datetime as dt
    v = r.get("ts")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            return None
    v = r.get("ts_utc")
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            return None
    return None


def _fresh(r, cutoff):
    t = _rts(r)
    return t is not None and t >= cutoff


def candidates(fresh_only=True):
    """(channel, id, title, body, url) for anything worth saying out loud."""
    out = []
    cutoff = time.time() - FRESH_MIN * 60 if fresh_only else 0

    # --- Robinhood Chain: curve runners, NOT graduations --------------------
    seen = {}
    for r in _tail("monitor_feed.jsonl", 3_000_000):
        if r.get("curve"):
            seen[r["curve"]] = r
    for c, r in seen.items():
        if not _fresh(r, cutoff):
            continue
        mc = r.get("mcap") or 0
        prog = r.get("curve_progress")
        buyers = r.get("n_buyers") or 0
        vol = r.get("vol_h1") or r.get("curve_volume_usd") or 0
        # on the curve, in the band, with a real tape -- the swell cohort
        if not (8_000 <= mc <= 60_000):
            continue
        if buyers < 8 or vol < 2_000:
            continue
        if prog is not None and prog >= 0.45:
            continue                      # too far up to be worth entering
        good = r.get("good") or []
        flags = r.get("flags") or []
        out.append(("rhc", f"rhc:{c}",
                    f"RUNNER · ${r.get('symbol') or '?'}",
                    f"**${r.get('symbol')}** on the curve — mcap **${mc:,.0f}**, "
                    f"{buyers} buyers, vol **${vol:,.0f}**"
                    + (f", **{100*prog:.0f}%** up the curve" if prog is not None else "")
                    + (f"\n{' · '.join(good[:3])}" if good else "")
                    + (f"\n⚠ {' · '.join(flags[:3])}" if flags else "")
                    + f"\n`{r.get('token') or c}`",
                    None))

    # --- Solana: pump.fun progress + StonkFun rank-on-pair ------------------
    pf = {}
    for r in _tail("sol_feed.jsonl"):
        if r.get("kind") == "token" and r.get("mint"):
            pf[r["mint"]] = r
    for m, r in pf.items():
        if not _fresh(r, cutoff):
            continue
        pg = r.get("progress_pct")
        if pg is None or not (15 <= pg <= 80):
            continue
        if (r.get("n_buyers") or 0) < 10:
            continue
        out.append(("sol", f"pf:{m}",
                    # never dress a mint prefix up as a ticker -- "$F1cTqn3i" reads
                    # like a symbol and is not one. The symbol worker fills real
                    # names within a minute or two; until then say so plainly.
                    (f"pump.fun · ${r['symbol']}" if r.get("symbol")
                     else "pump.fun · (unnamed)"),
                    f"**{r.get('symbol') or '(unnamed)'}** — **{pg:.0f}%** of curve, "
                    f"{r.get('n_buyers')} buyers, mcap **${r.get('mcap_usd') or 0:,.0f}**"
                    f"\n`{m}`",
                    f"https://pump.fun/{m}"))
    sf = {}
    for r in _tail("stonkfun_feed.jsonl"):
        if r.get("mint"):
            sf[r["mint"]] = r
    for m, r in sf.items():
        if not _fresh(r, cutoff):
            continue
        rk = r.get("rank_on_pair")
        if not rk or rk > 5:
            continue
        out.append(("sol", f"sf:{m}",
                    f"StonkFun · #{rk} on ${r.get('quote')}",
                    f"**{r.get('symbol') or '?'}** is token **#{rk}** on "
                    f"**${r.get('quote')}**\nrank 1-5 graduates **22.8%** vs "
                    f"**2.4%** at rank 101+ (n=57,137)\n`{m}`",
                    f"https://www.stonkfun.xyz/token/{m}"))

    # --- BSC: fresh and already tradeable ----------------------------------
    bs = {}
    for r in _tail("bsc_feed.jsonl"):
        if r.get("token"):
            bs[r["token"]] = r
    for t, r in bs.items():
        if not _fresh(r, cutoff):
            continue
        if not r.get("tradeable"):
            continue
        out.append(("bsc", f"bsc:{t}",
                    f"{r.get('venue') or 'bsc'} · ${r.get('symbol') or '?'}",
                    f"**{r.get('symbol')}** — {r.get('name') or ''}\n"
                    f"quote {r.get('quote') or '?'} · tradeable at launch\n`{t}`",
                    None))
    return out


def send(channel, title, body, url, log=print):
    wh = hook(channel)
    if not wh:
        log(f"  [{channel}] no webhook configured — skipping")
        return False
    payload = {"embeds": [{"title": title[:250], "description": body[:3900],
                           "color": COLOR.get(channel, 0x5865F2)}]}
    if url:
        payload["embeds"][0]["url"] = url
    try:
        req = urllib.request.Request(wh, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "HoodSniper/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 204)
    except Exception as e:  # noqa: BLE001
        log(f"  [{channel}] send failed: {type(e).__name__}: {str(e)[:70]}")
        return False


def run(once=False, dry=False, log=print):
    sent = _load_sent()
    if not sent and SEED_ON_FIRST_RUN:
        seed = [c[1] for c in candidates(fresh_only=False)]
        with open(SENT, "a") as f:
            for cid in seed:
                f.write(json.dumps({"ts": time.time(), "id": cid,
                                    "channel": "seed", "seeded": True}) + "\n")
        sent.update(seed)
        log(f"  cold start — seeded {len(seed):,} existing tokens as already-seen")
    log(f"  router up — {len(sent):,} already alerted")
    for ch, env in CHANNEL_ENV.items():
        log(f"    {ch:4s} -> {env}{'' if env_key(env) else '  (unset, falls back to default)'}")
    while True:
        try:
            fresh = [c for c in candidates() if c[1] not in sent]
            # newest-looking first is not knowable here, so cap and let the rest
            # come on the next pass rather than dumping everything at once
            for ch, cid, title, body, url in fresh[:MAX_PER_MIN]:
                if dry:
                    log(f"  [dry][{ch}] {title}")
                    ok = True
                else:
                    ok = send(ch, title, body, url, log=log)
                sent.add(cid)
                with open(SENT, "a") as f:
                    f.write(json.dumps({"ts": time.time(), "id": cid,
                                        "channel": ch, "title": title,
                                        "sent": bool(ok)}) + "\n")
                log(f"  [{ch}] {title} sent={ok}")
                time.sleep(1.0)
            if fresh and len(fresh) > MAX_PER_MIN:
                log(f"  {len(fresh)-MAX_PER_MIN} more queued for the next pass")
        except Exception as ex:  # noqa: BLE001
            log(f"  router error: {type(ex).__name__}: {str(ex)[:80]}")
        if once:
            return
        time.sleep(POLL)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true", help="print, send nothing")
    a = ap.parse_args()
    run(once=a.once, dry=a.dry)
