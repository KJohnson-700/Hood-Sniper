#!/usr/bin/env python3
"""
Hood Sniper -- Solana (pump.fun) launch monitor.

READ-ONLY. No keys, no signing, no orders. This is stage one of the Solana work and
it deliberately stops short of execution; see the gate at the bottom of this
docstring.

Separate from launch_monitor.py and bsc_monitor.py because Solana is not EVM: no
eth_call, no selectors, no secp256k1 signing, no state overrides. Nothing in the
existing execution stack transfers. What DOES transfer is the method -- decode the
venue off live traffic and verify every field against a real transaction before
building anything on it.

VENUE: pump.fun  program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
    Anchor events arrive as base64 in `Program data:` log lines, keyed by an 8-byte
    discriminator. Both layouts below were recovered from live mainnet transactions
    and verified field-by-field (2026-09-09):

    TradeEvent  disc bddb7fd34ee661ee
        mint:pubkey(32) sol:u64 tokens:u64 is_buy:bool user:pubkey(32)
        timestamp:i64 virtual_sol:u64 virtual_tokens:u64
        -- verified: 0.049383 SOL -> 816,111 tokens, reserves 44.159 SOL /
           728,961,566 tokens, implied 6.058e-08 SOL/token

    CreateEvent
        name:string symbol:string uri:string mint:pubkey curve:pubkey dev:pubkey
        (Anchor strings are u32 length-prefixed.)

PRICE COMES FROM THE VIRTUAL RESERVES, which the venue itself publishes on every
trade. That is a transactable price, not a mid quoted by an indexer -- the same
reason the RH and BSC monitors price off the curve rather than DexScreener.

THE EXECUTION GATE (unchanged project rule): no buy is armed on a chain whose exit
path is not proven. On Solana that proof is harder than on EVM -- there is no
eth_call state override, so the sell has to be shown via simulateTransaction, and
signing needs ed25519 (a new dependency and a different key format). Until that is
done this file stays read-only, and that is the point of it.

    python3 sol_monitor.py                 # live feed
    python3 sol_monitor.py --backfill 300  # replay recent signatures
    python3 sol_monitor.py --watch BONK    # alert on a ticker
"""
import argparse
import base64
import itertools
import json
import os
import struct
import sys
import threading
import time
import urllib.request
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
os.makedirs(DATA, exist_ok=True)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# --- StonkFun -----------------------------------------------------------------
# StonkFun runs no program of its own: it is a CONFIG LAYER on Raydium LaunchLab.
# Verified on chain 2026-09-10 -- LaunchLab is an executable BPF program and both
# config accounts are OWNED BY it.
LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
STONK_CFG_REWARD = "6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"   # Token-2022 fees
STONK_CFG_STD = "4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7"      # no transfer fee
STONK_CFGS = (STONK_CFG_STD, STONK_CFG_REWARD)
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"        # graduated pools

# THE QUOTE TRAP, AGAIN. StonkFun pairs each launch with a quote of the creator's
# choosing: SOL, USDC, tokenized stocks (SPYx/NVDAx/QQQx), pre-IPO tokens, or
# ANOTHER STONKFUN COIN. Sampled live, three of the five quote mints seen were
# plain memecoins -- 1B supply, mint authority revoked.
#
# This is structurally the same problem that gated BSC, where only ~17% of
# four.meme launches were BNB- or USDT-quoted and the rest paid their exit in an
# asset you would then have to sell again. A coin quoted in another memecoin is
# worse: its exit price depends on a second bonding curve you do not control.
# Venues are a config entry, the same shape as launch_monitor's VENUES and
# bsc_monitor's. StonkFun is NOT a separate monitor: it trades the same chain, the
# same wallet and the same quote assets as pump.fun, so it belongs in the same feed
# with a venue tag -- running it apart would mean two screens for one wallet.
VENUES = OrderedDict([
    ("pumpfun", {"label": "pump.fun", "mentions": PUMP_PROGRAM, "enabled": True,
                 "note": "own program; Anchor events in Program data logs"}),
    ("stonkfun", {"label": "StonkFun", "mentions": None, "enabled": True,
                  "note": "no program of its own — Raydium LaunchLab filtered to "
                          "two config accounts, so it is watched by mention"}),
])

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TRADEABLE_QUOTES = {WSOL: "SOL", USDC: "USDC"}


def quote_label(mint):
    if not mint:
        return "unknown"
    return TRADEABLE_QUOTES.get(mint, mint[:8] + "…")


def quote_tradeable(mint):
    """(ok, reason). Unknown is NOT tradeable — absence of proof is not proof."""
    if not mint:
        return False, "quote asset could not be read"
    if mint in TRADEABLE_QUOTES:
        return True, TRADEABLE_QUOTES[mint]
    return False, (f"quoted in {mint[:8]}… — not SOL or USDC, so the exit pays out "
                   f"in an asset you must sell again")
DISC_TRADE = "bddb7fd34ee661ee"
RPCS = ["https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com"]
WSS = "wss://solana-rpc.publicnode.com"
_rr = itertools.cycle(RPCS)
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
JOURNAL = os.path.join(DATA, "sol_feed.jsonl")

# pump.fun curve constants, used only for PROGRESS. They are stated here as
# assumptions rather than measured facts -- unlike the Pons 4.0 ETH threshold,
# which was measured off real graduations. Verify before trusting the % figure.
INIT_VIRT_SOL = 30.0
GRAD_VIRT_SOL = 85.0
TOTAL_SUPPLY = 1_000_000_000
SOL_USD = 210.0

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def rpc(method, params, tries=3, timeout=25):
    err = "?"
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                next(_rr),
                data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params}).encode(),
                headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            err = str(e)[:60]
            time.sleep(0.4)
    return {"_err": err}


def _read_str(b, off):
    n = struct.unpack_from("<I", b, off)[0]
    off += 4
    return b[off:off + n].decode("utf8", "replace"), off + n


def decode_trade(b):
    """TradeEvent -> dict, or None if the buffer is the wrong shape."""
    if len(b) < 8 + 32 + 16 + 1 + 32 + 8 + 16:
        return None
    o = 8
    mint = b58encode(b[o:o + 32]); o += 32
    sol, tok = struct.unpack_from("<QQ", b, o); o += 16
    is_buy = bool(b[o]); o += 1
    user = b58encode(b[o:o + 32]); o += 32
    ts = struct.unpack_from("<q", b, o)[0]; o += 8
    vsol, vtok = struct.unpack_from("<QQ", b, o)
    if not vtok:
        return None
    return {"mint": mint, "sol": sol / 1e9, "tokens": tok / 1e6, "is_buy": is_buy,
            "user": user, "ts": ts, "virt_sol": vsol / 1e9, "virt_tokens": vtok / 1e6}


def decode_create(b):
    """CreateEvent -> dict. Returns None rather than guessing on a short buffer."""
    try:
        o = 8
        name, o = _read_str(b, o)
        sym, o = _read_str(b, o)
        uri, o = _read_str(b, o)
        mint = b58encode(b[o:o + 32]); o += 32
        curve = b58encode(b[o:o + 32]); o += 32
        dev = b58encode(b[o:o + 32])
        if not mint.endswith("pump") and len(mint) < 32:
            return None
        return {"name": name, "symbol": sym, "uri": uri,
                "mint": mint, "curve": curve, "dev": dev}
    except Exception:  # noqa: BLE001
        return None


def curve_metrics(virt_sol, virt_tokens):
    """price / mcap / progress from the reserves the venue itself published."""
    if not virt_tokens:
        return {}
    price_sol = virt_sol / virt_tokens
    prog = 100 * (virt_sol - INIT_VIRT_SOL) / (GRAD_VIRT_SOL - INIT_VIRT_SOL)
    return {"price_sol": price_sol,
            "mcap_usd": price_sol * TOTAL_SUPPLY * SOL_USD,
            "progress_pct": max(0.0, min(prog, 100.0)),
            "virt_sol": virt_sol}


class SolMonitor:
    def __init__(self, args):
        self.args = args
        self.watch = {w.strip().upper() for w in (args.watch or "").split(",") if w.strip()}
        self.tokens = OrderedDict()          # mint -> row
        self.buyers = defaultdict(set)       # mint -> distinct buyer wallets
        self.lock = threading.RLock()
        self.stats = {"trades": 0, "creates": 0, "started": time.time()}
        self.running = True

    def on_event(self, b, sig=None):
        disc = b[:8].hex()
        if disc == DISC_TRADE:
            t = decode_trade(b)
            if t:
                self.on_trade(t)
            return
        c = decode_create(b)
        if c and c.get("symbol"):
            self.on_create(c, sig)

    def on_create(self, c, sig=None):
        with self.lock:
            self.stats["creates"] += 1
            self.tokens[c["mint"]] = dict(c, first_seen=time.time(), n_buys=0,
                                          sol_in=0.0, venue="pump.fun")
            while len(self.tokens) > 600:
                self.tokens.popitem(last=False)
        hit = c["symbol"].upper() in self.watch
        line = (f"  {datetime.now(timezone.utc).strftime('%H:%M:%S')} "
                f"NEW ${c['symbol'][:14]:14} {c['mint']}  dev={c['dev'][:8]}…")
        if hit:
            print(f"\n  *** WATCH HIT  ${c['symbol']}  ***\n      mint {c['mint']}\n"
                  f"      dev  {c['dev']}\n", flush=True)
        elif self.args.verbose:
            print(line, flush=True)
        self._journal(dict(c, kind="create", sig=sig))

    def on_trade(self, t):
        m = curve_metrics(t["virt_sol"], t["virt_tokens"])
        with self.lock:
            self.stats["trades"] += 1
            row = self.tokens.get(t["mint"])
            if row is None:
                row = {"mint": t["mint"], "symbol": None, "first_seen": time.time(),
                       "n_buys": 0, "sol_in": 0.0, "venue": "pump.fun"}
                self.tokens[t["mint"]] = row
            if t["is_buy"]:
                row["n_buys"] += 1
                row["sol_in"] += t["sol"]
                self.buyers[t["mint"]].add(t["user"])
            row.update(m)
            row["n_buyers"] = len(self.buyers[t["mint"]])

    def on_stonk_tx(self, v):
        """
        One StonkFun transaction from the live stream.

        LaunchLab publishes no Anchor event we can decode the way pump.fun's
        TradeEvent decodes, so side and token come from the INSTRUCTION NAME in the
        logs, which is a fact the program itself prints. Amounts are not read here:
        guessing at Raydium's instruction layout to get a number would be worse than
        having no number, and the quote/side/token is what the gate needs.
        """
        logs = v.get("logs") or []
        kind = None
        for l in logs:
            if "Instruction: " in l:
                k = l.split("Instruction: ", 1)[1].strip()
                if k in ("BuyExactIn", "BuyExactOut", "SellExactIn", "SellExactOut",
                         "Initialize", "InitializeV2"):
                    kind = k
                    break
        if not kind:
            return
        is_launch = kind.startswith("Initialize")
        is_buy = kind.startswith("Buy")
        with self.lock:
            self.stats["stonk"] = self.stats.get("stonk", 0) + 1
            if is_launch:
                self.stats["creates"] += 1
            else:
                self.stats["trades"] += 1
        if is_launch and self.args.verbose:
            print(f"  {datetime.now(timezone.utc).strftime('%H:%M:%S')} "
                  f"[stonkfun] NEW  {v.get('signature','')[:24]}…", flush=True)
        self._journal({"kind": "stonk_" + ("launch" if is_launch else
                                           ("buy" if is_buy else "sell")),
                       "instr": kind, "sig": v.get("signature"), "venue": "stonkfun"})

    def near_graduation(self, min_pct=25.0, n=10):
        with self.lock:
            rs = [r for r in self.tokens.values() if (r.get("progress_pct") or 0) >= min_pct]
        rs.sort(key=lambda r: -(r.get("progress_pct") or 0))
        return rs[:n]

    def _journal(self, rec):
        rec["ts_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(JOURNAL, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # ---- ingest ----
    def ws_loop(self):
        import websocket
        while self.running:
            try:
                ws = websocket.create_connection(WSS, timeout=30)
                subs = {}
                if VENUES["pumpfun"]["enabled"]:
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                        "method": "logsSubscribe",
                                        "params": [{"mentions": [PUMP_PROGRAM]},
                                                   {"commitment": "processed"}]}))
                    subs[1] = "pumpfun"
                if VENUES["stonkfun"]["enabled"]:
                    # one subscription per config: logsSubscribe takes exactly one
                    # mention, so the two StonkFun configs need one each
                    for i, cfg in enumerate(STONK_CFGS, start=2):
                        ws.send(json.dumps({"jsonrpc": "2.0", "id": i,
                                            "method": "logsSubscribe",
                                            "params": [{"mentions": [cfg]},
                                                       {"commitment": "processed"}]}))
                        subs[i] = "stonkfun"
                print(f"  subscribed: {sorted(set(subs.values()))}", flush=True)
                sub_ids = {}
                while self.running:
                    msg = json.loads(ws.recv())
                    if "result" in msg and isinstance(msg.get("result"), int):
                        sub_ids[msg["result"]] = subs.get(msg.get("id"), "?")
                        continue
                    params = msg.get("params") or {}
                    venue = sub_ids.get(params.get("subscription"), "pumpfun")
                    v = (params.get("result") or {}).get("value") or {}
                    if v.get("err"):
                        continue                      # failed tx proves nothing
                    if venue == "stonkfun":
                        self.on_stonk_tx(v)
                        continue
                    for l in v.get("logs") or []:
                        if l.startswith("Program data: "):
                            try:
                                self.on_event(base64.b64decode(l.split("Program data: ", 1)[1]),
                                              v.get("signature"))
                            except Exception:  # noqa: BLE001
                                pass
            except Exception as e:  # noqa: BLE001
                print(f"  ws reconnect ({str(e)[:50]})", flush=True)
                time.sleep(2)

    def stonk_backfill(self, limit=40, log=print):
        """
        Recent StonkFun launches with their quote asset.

        The quote is read from the transaction's token balances rather than by
        decoding the LaunchLab instruction: LaunchLab is Raydium's program and its
        layout is not something to guess at, whereas the mints that actually moved
        in the transaction are a fact. The quote is the non-SOL-fee mint that is NOT
        the newly created token.
        """
        seen = {}
        for cfg in STONK_CFGS:
            sigs = (rpc("getSignaturesForAddress",
                        [cfg, {"limit": limit}]) or {}).get("result") or []
            ok = [x for x in sigs if not x.get("err")]
            log(f"  {cfg[:10]}…  {len(sigs)} sigs, {len(ok)} ok")
            for sg in ok:
                tx = (rpc("getTransaction",
                          [sg["signature"], {"encoding": "jsonParsed",
                                             "maxSupportedTransactionVersion": 0}])
                      or {}).get("result")
                if not tx:
                    continue
                meta = tx.get("meta") or {}
                logs = meta.get("logMessages") or []
                kind = next((l.split("Instruction: ", 1)[1] for l in logs
                             if "Instruction: " in l
                             and l.split("Instruction: ", 1)[1] in
                             ("BuyExactIn", "SellExactIn", "BuyExactOut",
                              "SellExactOut", "Initialize", "InitializeV2")), None)
                if not kind:
                    time.sleep(0.1)
                    continue
                mints = [b.get("mint") for b in (meta.get("postTokenBalances") or [])
                         if b.get("mint")]
                # The QUOTE is whichever known-quote mint moved; the TOKEN is the
                # other one. Reading it from balances rather than decoding
                # LaunchLab's instruction layout, which is Raydium's and not
                # something to guess at.
                quote = next((m for m in mints if m in TRADEABLE_QUOTES), None)
                others = [m for m in mints if m != quote and m != WSOL]
                if quote is None and others:
                    # no SOL/USDC leg at all -- the pair is exotic on both sides
                    quote = others[-1]
                    others = others[:-1]
                tok = others[0] if others else None
                if tok:
                    seen[tok] = {"mint": tok, "quote": quote, "kind": kind,
                                 "sig": sg["signature"], "slot": sg["slot"],
                                 "venue": "stonkfun"}
                time.sleep(0.1)
        return list(seen.values())

    def backfill(self, limit):
        sigs = (rpc("getSignaturesForAddress",
                    [PUMP_PROGRAM, {"limit": limit}]) or {}).get("result") or []
        ok = [s for s in sigs if not s.get("err")]
        print(f"backfill: {len(sigs)} signatures, {len(ok)} succeeded", flush=True)
        for s in ok:
            tx = (rpc("getTransaction",
                      [s["signature"], {"encoding": "json",
                                        "maxSupportedTransactionVersion": 0}]) or {}).get("result")
            if not tx:
                continue
            for l in ((tx.get("meta") or {}).get("logMessages") or []):
                if l.startswith("Program data: "):
                    try:
                        self.on_event(base64.b64decode(l.split("Program data: ", 1)[1]),
                                      s["signature"])
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(0.08)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--backfill", type=int)
    ap.add_argument("--stonkfun", type=int, nargs="?", const=40,
                    help="sample recent StonkFun launches and their quote assets")
    a = ap.parse_args()
    mon = SolMonitor(a)
    slot = rpc("getSlot", []).get("result")
    print(f"Solana monitor · {' + '.join(v['label'] for v in VENUES.values() if v['enabled'])}"
          f" · slot {slot}")
    print("READ-ONLY — no keys, no signing. Execution is gated on proving the sell.")
    if a.watch:
        print(f"WATCHING {sorted(mon.watch)}")
    if a.stonkfun:
        rows = mon.stonk_backfill(a.stonkfun)
        ok = [r for r in rows if quote_tradeable(r["quote"])[0]]
        print(f"\nStonkFun tokens seen: {len(rows)}   TRADEABLE (SOL/USDC quote): {len(ok)}\n")
        print(f"{'token':<46}{'quote':<12}{'action':<14}reachable")
        for r in rows:
            good, why = quote_tradeable(r["quote"])
            print(f"  {r['mint']:<44}{quote_label(r['quote']):<12}"
                  f"{str(r.get('kind'))[:13]:<14}"
                  + ("yes" if good else "no"))
        if rows:
            print(f"\n  {100*len(ok)/len(rows):.0f}% of sampled StonkFun tokens are "
                  f"quoted in SOL or USDC and exit straight to money.")
            print(f"  The rest pay out in another token you would have to sell again —")
            print(f"  the same trap that gated BSC, where only ~17% were reachable.")
            print(f"\n  METHOD CAVEAT: when several mints move in one transaction this")
            print(f"  picks the SOL/USDC leg as the quote if there is one, so a high")
            print(f"  reachable-rate here is partly an artifact of that preference.")
            print(f"  Treat it as 'these tokens have a SOL/USDC route', not as a")
            print(f"  measured share of the venue.")
        return
    if a.backfill:
        mon.backfill(a.backfill)
        ng = mon.near_graduation(0.0)
        print(f"\ntokens seen: {len(mon.tokens)}  trades {mon.stats['trades']}  "
              f"creates {mon.stats['creates']}")
        print(f"\n{'symbol':<12}{'progress':>10}{'virt SOL':>11}{'mcap':>12}{'buyers':>8}")
        for r in ng[:12]:
            print(f"  {str(r.get('symbol') or r['mint'][:10])[:11]:<11}"
                  f"{(r.get('progress_pct') or 0):>9.1f}%{(r.get('virt_sol') or 0):>11.2f}"
                  f"{('$' + format(r['mcap_usd'], ',.0f')) if r.get('mcap_usd') else '-':>12}"
                  f"{r.get('n_buyers', 0):>8}")
        return
    threading.Thread(target=mon.ws_loop, daemon=True).start()
    try:
        while True:
            time.sleep(30)
            up = (time.time() - mon.stats["started"]) / 60
            ng = mon.near_graduation()
            print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"tokens {len(mon.tokens)} · trades {mon.stats['trades']} "
                  f"({mon.stats['trades']/max(up,0.01):.0f}/min) · creates {mon.stats['creates']}"
                  + (f" · stonkfun {mon.stats.get('stonk',0)}" if mon.stats.get("stonk") else "")
                  + (f" · {len(ng)} past 25% of curve" if ng else ""), flush=True)
    except KeyboardInterrupt:
        mon.running = False


if __name__ == "__main__":
    main()
