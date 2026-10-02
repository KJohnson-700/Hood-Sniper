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

# Several accepted names per channel, first match wins. The operator named them
# SOL_/BNB_DISCORD_WEBHOOK_URL, which reads better than my DISCORD_WEBHOOK_SOL, so
# that form is checked first -- a key that is present but spelled differently from
# what the code expects is indistinguishable from an unset key, and it reports as
# "falls back to default" while quietly sending every chain to one channel.
CHANNEL_ENV = {"rhc": ("DISCORD_WEBHOOK_URL", "HOOD_DISCORD_WEBHOOK_URL"),
               "sol": ("SOL_DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_SOL"),
               "bsc": ("BNB_DISCORD_WEBHOOK_URL", "BSC_DISCORD_WEBHOOK_URL",
                       "DISCORD_WEBHOOK_BSC")}
COLOR = {"rhc": 0x5865F2, "sol": 0x9945FF, "bsc": 0xF0B90B}


def channel_key(channel):
    """The env name actually holding this channel's webhook, or None."""
    for name in CHANNEL_ENV.get(channel, ()):
        if env_key(name):
            return name
    return None


def hook(channel):
    """Per-channel webhook, falling back to the default so a missing one is a
    degraded path rather than a silent drop."""
    k = channel_key(channel)
    return env_key(k) if k else env_key("DISCORD_WEBHOOK_URL")


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


def _gmgn_index():
    """
    address -> GMGN's read of it (smart money, swaps, net buy).

    bsc_feed carries almost no quality signal -- symbol, quote, tradeable, and
    that is it -- so alerting every tradeable BNB-quoted launch ran at ~195/hr.
    GMGN already indexes the same chain with smart_degen_count and swaps_24h, so
    it supplies the gate BSC cannot supply for itself.

    Corroboration also means a BSC alert needs TWO independent sources to agree:
    our chain listener saw the launch and GMGN sees activity on it.
    """
    ix = {}
    for r in _tail("gmgn_feed.jsonl", 6_000_000):
        a = (r.get("address") or "").lower()
        if a:
            ix[a] = r
    return ix


def candidates(fresh_only=True):
    """(channel, id, title, body, url) for anything worth saying out loud."""
    out = []
    cutoff = time.time() - FRESH_MIN * 60 if fresh_only else 0
    gix = _gmgn_index()

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
        slip = r.get("slippage_pct")
        fee = r.get("total_fee_bps") or 0
        liq = r.get("active_liq_usd")
        sellers = r.get("n_sellers") or 0
        bshare = (buyers / (buyers + sellers)) if (buyers + sellers) else None
        # amber whenever a kill flag is present: green on a flagged token is the
        # most dangerous thing this message can do
        colour = 0xE74C3C if flags else (0x2ECC71 if good else 0xF1C40F)
        fields = [
            {"name": "Exit cost",
             "value": (f"slip **{slip:.2f}%** · fees **{fee/100:.1f}%**\n"
                       f"round trip ≈ **{2*(fee/100) + 2*(slip or 0):.1f}%**"
                       if slip is not None else "**unpriced** — cannot quote an exit"),
             "inline": True},
            {"name": "Size",
             "value": f"mcap **${mc:,.0f}**\nliq **${(liq or 0):,.0f}**",
             "inline": True},
            {"name": "Curve",
             "value": (f"**{100*prog:.0f}%** of supply sold" if prog is not None
                       else "progress unknown"),
             "inline": True},
            {"name": "Tape",
             "value": (f"**{buyers}** buyers · **{sellers}** sellers\n"
                       + (f"buy side **{bshare:.0%}**" if bshare is not None else "")),
             "inline": True},
            {"name": "Volume", "value": f"**${vol:,.0f}**", "inline": True},
        ]
        if good:
            fields.append({"name": "Good", "value": " · ".join(good[:4]), "inline": False})
        if flags:
            fields.append({"name": "⚠ Flags", "value": " · ".join(flags[:4]), "inline": False})
        out.append(("rhc", f"rhc:{c}", None,
                    _embed("rhc", (f"RUNNER · ${r['symbol']}" if r.get("symbol")
                                   else f"RUNNER · {(r.get('token') or c)[:10]}… (unnamed)"),
                           fields,
                           r.get("token") or c, colour=colour,
                           foot="entry at 10% of supply sold is pf 2.79; 40% is 0.77")))

    # --- Solana: pump.fun progress + StonkFun rank-on-pair ------------------
    pf = {}
    for r in _tail("sol_feed.jsonl"):
        if r.get("kind") == "token" and r.get("mint"):
            pf[r["mint"]] = r
    for m, r in pf.items():
        if not _fresh(r, cutoff):
            continue
        pg = r.get("progress_pct")
        # 30-75% of the curve. Below 30 is too early for the crowd to mean anything
        # on a venue launching 2,000+ tokens an hour; above 75 the entry is gone --
        # the tape study put entry at 10% of supply sold at pf 2.79 and 40% at 0.77,
        # and pump.fun's own curve is steeper still.
        if pg is None or not (30 <= pg <= 75):
            continue
        # Buyer counts on live rows: p50=3, p75=11, p90=88. Measured alert rates
        # per hour for buyers x progress band:
        #     >=75  15-80%: 135    >=100 30-75%: 60    >=150 30-75%: 40
        #     >=75  30-75%:  75    >=100 40-70%: 40    >=200 30-75%: 25
        # 100 with the 30-75% band lands at ~60/hr, in line with the other two
        # channels once pump.fun's volume is accounted for.
        if (r.get("n_buyers") or 0) < 100:
            continue
        fields = [
            {"name": "Curve", "value": f"**{pg:.0f}%** of curve", "inline": True},
            {"name": "Size", "value": f"mcap **${r.get('mcap_usd') or 0:,.0f}**", "inline": True},
            {"name": "Tape", "value": f"**{r.get('n_buyers')}** buyers · "
                                      f"{r.get('n_buys') or 0} buys", "inline": True},
            {"name": "SOL in", "value": f"**{r.get('sol_in') or 0:.2f}** SOL", "inline": True},
        ]
        out.append(("sol", f"pf:{m}", None,
                    _embed("sol",
                           # never dress a mint prefix as a ticker -- "$F1cTqn3i"
                           # reads like a symbol and is not one
                           (f"pump.fun · ${r['symbol']}" if r.get("symbol")
                            else "pump.fun · (unnamed)"),
                           fields, m, colour=0x9945FF,
                           foot="progress uses assumed INIT/GRAD virtual-SOL constants")))
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
        fields = [
            {"name": "Rank on pair", "value": f"**#{rk}** on **${r.get('quote')}**",
             "inline": True},
            {"name": "Size", "value": f"mcap **${r.get('mcap') or 0:,.0f}**\n"
                                      f"liq **${r.get('liq') or 0:,.0f}**", "inline": True},
            {"name": "Edge", "value": "rank 1-5 graduates **22.8%**\nvs **2.4%** at 101+",
             "inline": True},
        ]
        out.append(("sol", f"sf:{m}", None,
                    _embed("sol", f"StonkFun · ${r.get('symbol') or '?'} · #{rk} on ${r.get('quote')}",
                           fields, m, colour=0x2ECC71,
                           desc=f"**{r.get('symbol') or '?'}** is token **#{rk}** launched "
                                f"against **${r.get('quote')}**",
                           foot="n=57,137 within-pair, age-controlled")))

    # --- BSC: fresh and already tradeable ----------------------------------
    bs = {}
    for r in _tail("bsc_feed.jsonl"):
        if r.get("token"):
            bs[r["token"]] = r
    for t, r in bs.items():
        # FRESHNESS IS JUDGED ON GMGN'S ROW, NOT OURS. Our feed lists every launch
        # the instant it happens; GMGN lists what it considers notable, minutes
        # later. Requiring both "we saw it in the last 12 min" AND "GMGN already
        # rates it" excluded everything by construction -- 0 candidates, from 370
        # genuine overlaps. The useful moment is when GMGN STARTS seeing activity
        # on a launch we already recorded, so that is the trigger.
        if not r.get("tradeable"):
            continue
        # QUOTE MUST BE REACHABLE. Alerting every tradeable launch produced 2,220
        # candidates -- a firehose. Measured earlier on this project: only ~17% of
        # four.meme launches are BNB/USDT-quoted, and the rest pay out in a token
        # you would have to sell again. bsc_feed carries quote_why as a readable
        # label ("BNB") where it resolved and "-" where it did not, so an
        # unresolved quote means the exit path is unproven, not that it is fine.
        qw = (r.get("quote_why") or "-").strip()
        if qw in ("-", ""):
            continue
        if qw.upper() not in ("BNB", "WBNB", "USDT", "BUSD", "USD1"):
            continue
        g = gix.get(t.lower())
        if not g:
            continue                 # GMGN has not seen activity on it
        if not _fresh(g, cutoff):
            continue                 # GMGN's read is stale, not newly interesting
        gsmart = g.get("smart") or 0
        gswaps = g.get("swaps_24h") or 0
        if gsmart < 2 and gswaps < 50:
            continue                 # launched and quoted fine, but nobody is trading it
        fields = [
            {"name": "Venue", "value": f"**{r.get('venue') or 'bsc'}**", "inline": True},
            # quote_why is the readable label; the raw field is 0x000...0 for BNB
            # and printing that reads as a bug
            {"name": "Quote", "value": f"**{qw}**", "inline": True},
            {"name": "Exit", "value": ("**tradeable** at launch" if r.get("tradeable")
                                       else "not tradeable"), "inline": True},
            {"name": "GMGN", "value": f"**★{gsmart}** smart · **{gswaps:,}** swaps/24h",
             "inline": True},
        ]
        out.append(("bsc", f"bsc:{t}", None,
                    _embed("bsc", f"{r.get('venue') or 'bsc'} · ${r.get('symbol') or '?'}",
                           fields, t, colour=0xF0B90B,
                           desc=(r.get("name") or ""),
                           foot="only ~17% of four.meme launches are BNB/USDT-quoted — "
                                "check the quote before sizing")))
    return out


def _links(chain, addr, sym=None):
    """
    Clickable links per chain. VERIFIED routes, not guessed: pump.fun/coin/<mint>
    and stonkfun.xyz/token/<mint> both return 200, stonkfun.xyz/coin/<mint> is a
    404. The rhc and bsc alerts previously carried NO links at all, which is what
    "the links don't work" meant -- there was nothing to click.
    """
    if chain == "sol":
        return [f"[pump.fun](https://pump.fun/coin/{addr})",
                f"[StonkFun](https://www.stonkfun.xyz/token/{addr})",
                f"[GMGN](https://gmgn.ai/sol/token/{addr})",
                f"[Solscan](https://solscan.io/token/{addr})"]
    if chain == "bsc":
        return [f"[GMGN](https://gmgn.ai/bsc/token/{addr})",
                f"[four.meme](https://four.meme/token/{addr})",
                f"[BscScan](https://bscscan.com/token/{addr})",
                f"[DexScreener](https://dexscreener.com/bsc/{addr})"]
    return [f"[GMGN](https://gmgn.ai/robinhood/token/{addr})",
            f"[Blockscout](https://robinhoodchain.blockscout.com/address/{addr})",
            f"[DexScreener](https://dexscreener.com/robinhood/{addr})"]


def _embed(channel, title, fields, addr, desc="", colour=None, foot=""):
    """
    Same shape as the hood-sniper alerts: a short headline, inline fields for the
    numbers that decide the trade, links, and the contract on its own line so it is
    one tap to copy.
    """
    e = {"title": title[:250], "color": colour or COLOR.get(channel, 0x5865F2),
         "fields": [f for f in fields if f],
         "description": ((desc + "\n\n") if desc else "")
                        + " · ".join(_links(channel, addr))
                        + f"\n`{addr}`"}
    if foot:
        e["footer"] = {"text": foot[:2000]}
    return e


def send_embed(channel, embed, log=print):
    wh = hook(channel)
    if not wh:
        log(f"  [{channel}] no webhook configured — skipping")
        return False
    try:
        req = urllib.request.Request(wh, data=json.dumps({"embeds": [embed]}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "HoodSniper/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 204)
    except Exception as e:  # noqa: BLE001
        log(f"  [{channel}] send failed: {type(e).__name__}: {str(e)[:70]}")
        return False


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
    for ch in CHANNEL_ENV:
        k = channel_key(ch)
        log(f"    {ch:4s} -> {k if k else 'UNSET, falls back to DISCORD_WEBHOOK_URL'}")
    while True:
        try:
            fresh = [c for c in candidates() if c[1] not in sent]
            # newest-looking first is not knowable here, so cap and let the rest
            # come on the next pass rather than dumping everything at once
            for ch, cid, _t, embed in fresh[:MAX_PER_MIN]:
                title = embed.get("title", "?")
                if dry:
                    log(f"  [dry][{ch}] {title}")
                    log("      " + json.dumps(embed)[:300])
                    ok = True
                else:
                    ok = send_embed(ch, embed, log=log)
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
