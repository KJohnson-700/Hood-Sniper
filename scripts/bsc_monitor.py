#!/usr/bin/env python3
"""
Hood Sniper -- BNB Chain (BSC) launch monitor.

DISPLAY ONLY. No keys, no signing, no orders.

Separate from launch_monitor.py because BSC is a different chain: different
RPC, different contracts, and none of the Robinhood Chain addresses apply.
The venue layer mirrors the RH Chain design so a venue is a config entry.

    python3 bsc_monitor.py                    # live feed
    python3 bsc_monitor.py --watch BONKE,NIKE # alert on specific tickers
    python3 bsc_monitor.py --backfill 6000    # replay recent blocks

VENUE: four.meme
    TokenManager2 0x5c952063c7fc8610FFDB798152D69F0B9550762b emits one launch
    event carrying creator, token, supply, name and symbol together -- no
    follow-up call needed to know what launched or who launched it.
    Measured ~12,600 launches/day, BSC block time ~0.45s.

    Tokens come in two shapes, both fixed templates, so honeypot risk is
    structural rather than per-token:
      fresh      EIP-1167 clone (92 bytes) of 0xe506cd33886785816895dbfb2bc8927696c0c8ec
      graduated  full contract, 7646 bytes
"""
import argparse
import itertools
import json
import os
import sys
import threading
import time
import urllib.request
from collections import deque, OrderedDict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
os.makedirs(DATA, exist_ok=True)
sys.path.insert(0, HERE)
from ethsign import keccak256  # noqa: E402
from bsc_buy import quote_of, quote_label, quote_tradeable  # noqa: E402

RPCS = ["https://bsc-dataseed.bnbchain.org",
        "https://bsc-rpc.publicnode.com",
        "https://bsc-dataseed1.binance.org"]
WSS = "wss://bsc-rpc.publicnode.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_rr = itertools.cycle(RPCS)
CHAIN_ID = 56
BLOCK_TIME = 0.45
BNB_USD = 620.0
JOURNAL = os.path.join(DATA, "bsc_feed.jsonl")
TRADES = os.path.join(DATA, "bsc_holder_events.jsonl")

# four.meme trade events, verified against live logs 2026-09-10.
# data w0 = TOKEN, w1 = WALLET -- confirmed with eth_getCode (w0 has bytecode,
# w1 does not). The vanity suffix on four.meme tokens (…ffff / …4444) makes a
# reversed decode obvious, and reversing them would index token contracts as
# traders.
T_FM_BUY = "0x7db52723a3b2cdd6164364b3b766e65e540d7be48ffa89582956d8eaebe62942"
T_FM_SELL = "0x0a5575b3648bae2210cee56bf33254cc1ddfbc7bf637c0af2ac18b14fb1bae19"

# WHY THIS IS COLLECTED LIVE RATHER THAN SCANNED.
# A smart-money index needs wallet->token history. On Robinhood Chain that came
# from a 2.9M-event backfill. On BSC it cannot: the only public endpoint that
# serves eth_getLogs at all (publicnode -- both dataseed hosts reject every width
# with -32005) prunes to roughly the last 1,000 blocks, about 8 minutes of chain.
# Everything older returns null. So the history has to be ACCUMULATED going
# forward, which is why this records to disk on every trade.

# --- flap.sh -----------------------------------------------------------
# Launchpad orchestrator; it is also the owner() of each per-token curve clone,
# which is how it was confirmed. A launch emits ~9 events; this is the richest.
# NOTE the aggregator 0xa0ffb9c1… trades BOTH flapsh and four.meme tokens and
# is NOT a factory — an earlier pass mistook it for one.
FLAPSH_LAUNCHPAD = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
T_FLAPSH_LAUNCH = "0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603"
FLAPSH_IMPLS = {"024f18294970b5c76c0691b87f138a0317156422",   # current gen (…7777)
                "88881b6f03090462a969ec7f48385744eeb63333"}   # older gen ($BONKE, …8888)

FOURMEME_TM = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
T_FOURMEME_LAUNCH = "0x396d5e902b675b032348d3d2e9517ee8f0c4a926603fbc075d3d282ff00cad20"
FOURMEME_IMPL = "e506cd33886785816895dbfb2bc8927696c0c8ec"
FOURMEME_GRAD_CODELEN = 7646
CLONE_1167 = "363d3d373d3d3d363d73"

VENUES = OrderedDict([
    ("four_meme", {"label": "four.meme", "address": FOURMEME_TM,
                   "topic": T_FOURMEME_LAUNCH, "enabled": True,
                   "note": "~12.6k launches/day; event carries creator+token+name+symbol"}),
    ("flapsh", {"label": "flap.sh", "address": FLAPSH_LAUNCHPAD,
                "topic": T_FLAPSH_LAUNCH, "enabled": True,
                "note": "~6,700 launches/day; event carries ts, dev, token, name, "
                        "symbol AND an IPFS metadata CID. $BONKE launched here."}),
    ("tiktokfun", {"label": "TikTokFun", "address": None, "topic": None,
                   "enabled": False, "note": "~$121k/24h on GT; unmapped"}),
])

SEL_SYMBOL = "0x95d89b41"
SEL_NAME = "0x06fdde03"


def rpc(method, params, tries=3, timeout=20):
    for a in range(tries):
        try:
            body = json.dumps({"jsonrpc": "2.0", "method": method,
                               "params": params, "id": 1}).encode()
            req = urllib.request.Request(next(_rr), data=body,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(0.3 * (a + 1))
    return {}


def get_logs(address, topic, lo, hi, chunk=120):
    """
    Chunked getLogs. BSC public RPCs cap the result set (~1000 logs) and answer
    with {"error": "limit exceeded"} rather than truncating, which an
    r.get("result") or [] would silently turn into "no launches".
    four.meme alone emits ~90 launches per 1000 blocks, so chunks stay small.
    """
    out, b, errs = [], lo, 0
    while b <= hi:
        to = min(b + chunk, hi)
        r = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                 "address": address, "topics": [topic]}])
        if "result" in r:
            out.extend(r["result"])
        else:
            errs += 1
            if chunk > 50:                     # back off and retry this range
                out.extend(get_logs(address, topic, b, to, chunk // 3))
        b = to + 1
    return out


def head_block():
    return int((rpc("eth_blockNumber", []) or {}).get("result", "0x0"), 16)


def as_str(r):
    if not r or len(r) < 130:
        return None
    try:
        b = r[2:]
        ln = int(b[64:128], 16)
        return bytes.fromhex(b[128:128 + ln * 2]).decode("utf-8", "replace").strip("\x00")
    except Exception:  # noqa: BLE001
        return None


def _read_str(d, len_word):
    """Length word followed by inline utf-8, both 32-byte aligned."""
    ln = int(d[len_word * 64:(len_word + 1) * 64], 16)
    off = (len_word + 1) * 32
    return bytes.fromhex(d[off * 2:off * 2 + ln * 2]).decode("utf-8", "replace")


def decode_flapsh(log):
    """
    flap.sh launch event.
      w0 timestamp · w1 creator · w2 id · w3 token
      w4-w6 string offsets · w7/w8 name · w9/w10 symbol · w11+ IPFS CID
    """
    d = log["data"][2:]
    if len(d) < 12 * 64:
        return None
    try:
        ev = {"creator": "0x" + d[64:128][-40:],
              "token": "0x" + d[3 * 64:4 * 64][-40:],
              "supply": None,
              "name": _read_str(d, 7),
              "symbol": _read_str(d, 9),
              "block": int(log["blockNumber"], 16),
              "venue": "flapsh"}
        try:
            ev["ipfs"] = _read_str(d, 11)
        except Exception:  # noqa: BLE001
            ev["ipfs"] = None
        return ev
    except Exception:  # noqa: BLE001
        return None


def decode_launch(log):
    """
    four.meme launch event. All fields are in data (no indexed params), so the
    token cannot be filtered on at the node -- decode and match locally.
    """
    d = log["data"][2:]
    if len(d) < 12 * 64:
        return None

    def txt(word_idx):
        ln = int(d[word_idx * 64:(word_idx + 1) * 64], 16)
        off = (word_idx + 1) * 32
        return bytes.fromhex(d[off * 2:off * 2 + ln * 2]).decode("utf-8", "replace")

    try:
        return {"creator": "0x" + d[0:64][-40:],
                "token": "0x" + d[64:128][-40:],
                "supply": int(d[5 * 64:6 * 64], 16) / 1e18,
                "name": txt(8), "symbol": txt(10),
                "block": int(log["blockNumber"], 16),
                "venue": "four_meme"}
    except Exception:  # noqa: BLE001
        return None


def classify(token):
    """Template check -- the honeypot question, answered structurally."""
    code = (rpc("eth_getCode", [token, "latest"]) or {}).get("result") or ""
    n = len(code)
    if n == 92 and code[2:].startswith(CLONE_1167):
        impl = code[2:][20:60].lower()
        if impl == FOURMEME_IMPL:
            return "four.meme clone", n
        if impl in FLAPSH_IMPLS:
            return "flap.sh clone", n
        return f"clone:{impl[:8]}", n
    if n == FOURMEME_GRAD_CODELEN:
        return "four.meme graduated", n
    if n <= 2:
        return "NO CODE", n
    return "unrecognised", n


def dexscreener(token):
    try:
        req = urllib.request.Request(
            f"https://api.dexscreener.com/latest/dex/tokens/{token}",
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            pairs = json.load(r).get("pairs") or []
    except Exception:  # noqa: BLE001
        return {}
    if not pairs:
        return {}
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    return {"dex": p.get("dexId"), "liq": (p.get("liquidity") or {}).get("usd"),
            "vol_h1": (p.get("volume") or {}).get("h1"),
            "vol_h24": (p.get("volume") or {}).get("h24"),
            "price_usd": p.get("priceUsd"),
            "buys_h1": ((p.get("txns") or {}).get("h1") or {}).get("buys"),
            "sells_h1": ((p.get("txns") or {}).get("h1") or {}).get("sells")}


class BscMonitor:
    def __init__(self, args):
        self.args = args
        self.watch = {w.strip().upper() for w in (args.watch or "").split(",") if w.strip()}
        self.recent = deque(maxlen=300)
        self.hits = []
        self.devs = {}
        self.skipped_quote = 0
        self.n = 0
        self.started = time.time()
        self.lock = threading.Lock()

    def record_trade(self, lg):
        """Append one four.meme trade. Cheap, append-only, survives restarts."""
        tp = (lg.get("topics") or [None])[0]
        if tp not in (T_FM_BUY, T_FM_SELL):
            return
        d = lg.get("data", "")[2:]
        if len(d) < 128:
            return
        token = "0x" + d[24:64]
        wallet = "0x" + d[88:128]
        try:
            with open(TRADES, "a") as f:
                f.write(json.dumps([int(lg["blockNumber"], 16), token.lower(),
                                    wallet.lower(), 1 if tp == T_FM_BUY else 0]) + "\n")
            self.stats["trades"] = self.stats.get("trades", 0) + 1
        except Exception:  # noqa: BLE001
            pass

    def on_launch(self, ev):
        with self.lock:
            self.n += 1
            self.recent.append(ev)
            self.devs[ev["creator"]] = self.devs.get(ev["creator"], 0) + 1
        # The quote currency decides whether this launch is even reachable. Most
        # four.meme curves are quoted in tokenized equities (QQQB, NVDAB, GMEB…),
        # which can only be bought with that asset and pay their exit back in it.
        # Reading it here means an untradeable launch never reaches the alert path.
        ev["quote"] = quote_of(rpc, ev["token"]) if ev.get("venue") == "four_meme" else None
        ev["tradeable"], ev["quote_why"] = (
            quote_tradeable(ev["quote"]) if ev.get("venue") == "four_meme" else (True, "-"))
        if self.args.tradeable_only and not ev["tradeable"]:
            self.skipped_quote += 1
            return
        hit = ev["symbol"].upper() in self.watch or any(
            w in ev["name"].upper() for w in self.watch)
        rec = dict(ev, ts=datetime.now(timezone.utc).isoformat(), watch_hit=hit)
        if hit:
            tmpl, n = classify(ev["token"])
            ds = dexscreener(ev["token"])
            rec.update({"template": tmpl, "code_len": n, **{f"ds_{k}": v for k, v in ds.items()}})
            with self.lock:
                self.hits.append(rec)
            print(f"\n  *** WATCH HIT  ${ev['symbol']}  ***")
            print(f"      CA       {ev['token']}")
            print(f"      venue    {ev.get('venue','?')}")
            if ev.get("venue") == "four_meme":
                mark = "OK" if ev["tradeable"] else "NOT TRADEABLE"
                print(f"      quote    {quote_label(ev.get('quote'))}  [{mark}]")
                if not ev["tradeable"]:
                    print(f"               {ev['quote_why']}")
            print(f"      dev      {ev['creator']}  ({self.devs.get(ev['creator'],1)} launches seen)")
            if ev.get("ipfs"):
                print(f"      metadata ipfs://{ev['ipfs']}")
            print(f"      template {tmpl} (len {n})")
            if ds:
                print(f"      market   dex={ds.get('dex')} liq=${ds.get('liq') or 0:,.0f} "
                      f"vol1h=${ds.get('vol_h1') or 0:,.0f} {ds.get('buys_h1')}B/{ds.get('sells_h1')}S")
            print("      NOTE duplicate tickers are common — verify against the "
                  "project's own announced CA before trading\n", flush=True)
        elif self.args.verbose:
            print(f"  {datetime.now(timezone.utc).strftime('%H:%M:%S')} "
                  f"${ev['symbol'][:14]:14} {ev['token']} dev={ev['creator'][:10]} "
                  f"q={(quote_label(ev.get('quote')) if ev.get('venue') == 'four_meme' else 'n/a'):<10}"
                  f"{'' if ev.get('tradeable') else ' [no exit to money]'}", flush=True)
        with open(JOURNAL, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def ws_loop(self):
        import websocket
        while True:
            try:
                ws = websocket.create_connection(WSS, timeout=25)
                subs = [v for v in VENUES.values() if v["enabled"] and v["address"]]
                # also stream four.meme trades so the wallet index accumulates
                ws.send(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "eth_subscribe",
                                    "params": ["logs", {"address": FOURMEME_TM,
                                               "topics": [[T_FM_BUY, T_FM_SELL]]}]}))
                for v in subs:
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                        "params": ["logs", {"address": v["address"],
                                                            "topics": [v["topic"]]}]}))
                    ws.recv()
                ws.settimeout(120)
                while True:
                    m = json.loads(ws.recv())
                    p = m.get("params", {}).get("result")
                    if not p:
                        continue
                    a = (p.get("address") or "").lower()
                    tp = (p.get("topics") or [None])[0]
                    if tp in (T_FM_BUY, T_FM_SELL):
                        self.record_trade(p)      # feeds the wallet index
                        continue
                    ev = (decode_flapsh(p) if a == FLAPSH_LAUNCHPAD
                          else decode_launch(p))
                    if ev:
                        self.on_launch(ev)
            except Exception:  # noqa: BLE001
                time.sleep(3)

    def status(self):
        while True:
            time.sleep(self.args.status)
            up = time.time() - self.started
            with self.lock:
                n, hits, devs = self.n, len(self.hits), len(self.devs)
            print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"{n} launches ({n/max(up/60,0.01):.0f}/min) · {devs} distinct devs · "
                  f"{hits} watch hits · watching {sorted(self.watch) or '—'}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="BSC launch monitor (display only)")
    ap.add_argument("--watch", default="", help="comma-separated tickers to alert on")
    ap.add_argument("--all-quotes", dest="tradeable_only",
                    action="store_false", default=True,
                    help="also show curves quoted in assets the wallet does not hold "
                         "(QQQB/NVDAB/…); they have no exit to money")
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--verbose", action="store_true", help="print every launch")
    ap.add_argument("--status", type=float, default=30.0)
    a = ap.parse_args()
    mon = BscMonitor(a)
    print(f"BSC monitor · chain {CHAIN_ID} · head {head_block()}")
    print(f"venues: {[k for k, v in VENUES.items() if v['enabled']]}  "
          f"(unmapped: {[k for k, v in VENUES.items() if not v['enabled']]})")
    if mon.watch:
        print(f"WATCHING {sorted(mon.watch)} — every match is shown, none are auto-picked")
    if a.backfill:
        head = head_block()
        v = VENUES["four_meme"]
        logs = get_logs(v["address"], v["topic"], head - a.backfill, head)
        print(f"backfill: {len(logs)} launches in {a.backfill} blocks")
        for l in logs:
            ev = decode_launch(l)
            if ev:
                mon.on_launch(ev)
        v2 = VENUES["flapsh"]
        logs2 = get_logs(v2["address"], v2["topic"], head - a.backfill, head)
        print(f"backfill flapsh: {len(logs2)} launches")
        for l in logs2:
            ev = decode_flapsh(l)
            if ev:
                mon.on_launch(ev)
        return
    threading.Thread(target=mon.ws_loop, daemon=True).start()
    threading.Thread(target=mon.status, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
