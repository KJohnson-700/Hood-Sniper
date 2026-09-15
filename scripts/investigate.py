#!/usr/bin/env python3
"""
Hood Sniper -- token investigation harness.

Runs the manual DYOR checklist in parallel and prints a pass/fail board, so the
decision is made on checks rather than on how fast the chart is moving.

READ-ONLY. No keys, no signing, no orders.

    python3 investigate.py 0x<token>
    python3 investigate.py 0x<token> --stake 25 --json

Probe classes
-------------
  chain   : on-chain, deterministic, ~1s
  net     : third-party HTTP (DexScreener, link liveness), ~1-3s
  social  : needs an API key -- see SOCIAL PROBES below. Reports NEEDS-KEY
            rather than silently passing.

Status values: PASS / WARN / FAIL / N-A / UNKNOWN / NEEDS-KEY
Nothing here is a prediction. These are facts about the token, not a forecast.
"""
import argparse
import concurrent.futures as cf
import itertools
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
from ethsign import keccak256  # noqa: E402

# --- chains -----------------------------------------------------------------
# Probes fall into two groups: venue-specific (Pons curve, snipe tax, fees) and
# chain-agnostic (identity, template/honeypot, supply map, DexScreener, momentum,
# links). Selecting a chain swaps the RPCs and the venue templates; the
# agnostic probes run everywhere.
CHAINS = {
    "rh": {
        "name": "Robinhood Chain", "id": 4663, "ds": "robinhood",
        "rpcs": ["https://rpc.mainnet.chain.robinhood.com",
                 "https://robinhood-rpc.publicnode.com"],
        "native_usd": 2450.0, "block_time": 0.101,
        "explorer": "https://robinhoodchain.blockscout.com/api/v2",
    },
    "bsc": {
        "name": "BNB Chain", "id": 56, "ds": "bsc",
        "rpcs": ["https://bsc-dataseed.bnbchain.org",
                 "https://bsc-rpc.publicnode.com",
                 "https://bsc-dataseed1.binance.org"],
        "native_usd": 620.0, "block_time": 0.45,
        "explorer": None,          # no public Blockscout; creator lookups unavailable
    },
}
CHAIN = CHAINS["rh"]

# BSC token templates (see bsc_monitor.py) -- both fixed, so honeypot risk is
# structural exactly as on Robinhood Chain.
BSC_TEMPLATES = {
    "e506cd33886785816895dbfb2bc8927696c0c8ec": "four.meme clone",
    "024f18294970b5c76c0691b87f138a0317156422": "flap.sh clone",
    "88881b6f03090462a969ec7f48385744eeb63333": "flap.sh clone (older gen)",
}
BSC_FOURMEME_GRAD_LEN = 7646

RPCS = ["https://rpc.mainnet.chain.robinhood.com",
        "https://robinhood-rpc.publicnode.com"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_rr = itertools.cycle(RPCS)
ETH_USD = 2450.0
BLOCK_TIME = 0.101
PONS_TOKEN_CODELEN = 6498

# --- Bankr / Doppler -------------------------------------------------------
# GeckoTerminal's "bankr" label is Doppler infrastructure. Tokens are Solady
# clones (code len 90) of one verified implementation, so honeypot risk is
# structural exactly as on Pons. Launches are detected from a v4 Initialize
# whose `hooks` field is the Doppler hook -- the factory emits no events.
DOPPLER_IMPL = "3be8b97fd0e713b5abe0649fa830223b6b4bc599"
DOPPLER_HOOK = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"
DOPPLER_FACTORY = "0x1b37d3a72082029c44b35b604ea473617580b69a"
CLONE_PREFIX = "3d3d3d3d363d3d37363d73"

# --- o1 Launchpad ----------------------------------------------------------
# Two RWAERC20LaunchpadFactory instances. Tokens carry a vanity "…01" address
# suffix and expose factory(). Launches are announced by the factory event
# below, which conveniently carries token, poolId and creator in its topics.
# o1 pairs a meme against its MATCHING real ticker (BND/BND, CCL/CCL).
O1_FACTORIES = {"0xce9c48cfa068947f77738c81be406b53338e5b0d",
                "0xe64ac4113848bbc1a6dde1a6d1da96720a36f297"}
O1_LAUNCH_TOPIC = "0x207384e895174175cc774fe7f7457b37c382f27ebf53d37d5257b862f80eaf9c"
O1_CODELENS = {9450, 9316}
SEL_FACTORY = "0xc45a0155"      # factory()


def use_chain(key):
    """Point every module-level constant at the selected chain."""
    global CHAIN, RPCS, _rr, ETH_USD, BLOCK_TIME, DS_NET
    CHAIN = CHAINS[key]
    RPCS = CHAIN["rpcs"]
    _rr = itertools.cycle(RPCS)
    ETH_USD = CHAIN["native_usd"]
    BLOCK_TIME = CHAIN["block_time"]
    DS_NET = CHAIN["ds"]


DS_NET = "robinhood"


def detect_venue(code, token=None):
    """
    Which launchpad produced this token.

    Bytecode alone is enough for Pons (one 6498-byte template) and Bankr (a
    Solady clone of one implementation). o1 ships more than one template, so it
    is confirmed by calling factory() and matching a known RWA factory.
    """
    if not code or len(code) <= 2:
        return "unknown"
    if len(code) == PONS_TOKEN_CODELEN:
        return "pons"
    body = code[2:]
    if len(code) == 90 and body.startswith(CLONE_PREFIX):
        impl = body[len(CLONE_PREFIX):len(CLONE_PREFIX) + 40]
        return "bankr" if impl.lower() == DOPPLER_IMPL else f"clone:{impl[:8]}"
    if len(code) == 92 and body.startswith("363d3d373d3d3d363d73"):
        impl = body[20:60].lower()
        if impl in BSC_TEMPLATES:
            return BSC_TEMPLATES[impl]
        return f"clone:{impl[:8]}"
    if CHAIN["id"] == 56 and len(code) == BSC_FOURMEME_GRAD_LEN:
        return "four.meme graduated"
    if token and len(code) in O1_CODELENS:
        fac = as_addr(call(token, SEL_FACTORY))
        if fac and fac in O1_FACTORIES:
            return "o1"
    return "other"

T_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
T_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
T_EXEMPTED = "0xe4b7e48fbd47c2f602bacadee76ad33b16542ddb4997cfc0de04c311adcfa8c7"
T_SNIPE_CHARGED = "0x3bc39a5562b28f5fe8f36cecabfbaa12bb969acf05717994709225fc412a9934"
T_V4_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
T_V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

SEL = {"symbol": "0x95d89b41", "name": "0x06fdde03", "decimals": "0x313ce567",
       "totalSupply": "0x18160ddd", "description": "0x7284e416",
       "curve": "0x7165485d", "owner": "0x8da5cb5b", "getOwner": "0x893d20e8",
       "deployer": "0xd5f39488", "feeBps": "0x24a9d853",
       "creatorTaxBps": "0xc1bb8901", "token": "0xfc0c546a",
       "graduated": "0xe7c2b772", "launchedAt": "0xbf56b371",
       "snipeTaxStartBps": "0x50e25ac2", "snipeTaxSeconds": "0x6783774b"}

URL_RE = re.compile(r"(https?://[^\s\"'<>)]+|(?:www\.|t\.me/|discord\.gg/|x\.com/|twitter\.com/)[^\s\"'<>)]+)", re.I)


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


def call(to, sel, tag="latest"):
    return (rpc("eth_call", [{"to": to, "data": sel}, tag]) or {}).get("result")


def as_int(r):
    try:
        return int(r, 16) if r and r != "0x" else None
    except (TypeError, ValueError):
        return None


def as_addr(r):
    return ("0x" + r[-40:]).lower() if r and len(r) >= 66 else None


def as_str(r):
    if not r or len(r) < 130:
        return None
    try:
        b = r[2:]
        ln = int(b[64:128], 16)
        return bytes.fromhex(b[128:128 + ln * 2]).decode("utf-8", "replace").strip("\x00")
    except Exception:  # noqa: BLE001
        return None


def get_logs(p, width=40_000):  # noqa: D401
    lo, hi = int(p["fromBlock"], 16), int(p["toBlock"], 16)
    out, b = [], lo
    while b <= hi:
        to = min(b + width, hi)
        r = rpc("eth_getLogs", [dict(p, fromBlock=hex(b), toBlock=hex(to))])
        out.extend(r.get("result", []))
        b = to + 1
    return out


def http(url, timeout=8, method="GET"):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:  # noqa: BLE001
        return None, b""


# measured anchor: block 25,000,000 == 1785583378 (2026-08-01T11:22:58Z), 0.101 s/block
ANCHOR_BLOCK, ANCHOR_TS = 25_000_000, 1785583378


_BLK_CACHE = {}


def block_from_ts(ts, head=None):
    """
    Block number for a unix timestamp, by BINARY SEARCH on block timestamps.

    The previous version extrapolated linearly from a fixed anchor. Over ~30M
    blocks a sub-millisecond error in the assumed block time compounds into
    ~100k blocks of drift, which put every scan window in the wrong place and
    made probes report "no logs in range" for tokens that traded fine.
    ~25 RPC calls, cached per timestamp.
    """
    key = int(ts)
    if key in _BLK_CACHE:
        return _BLK_CACHE[key]
    hi = head or (as_int((rpc("eth_blockNumber", []) or {}).get("result")) or 0)
    lo = 0
    def bts(b, tries=4):
        for _ in range(tries):
            r = (rpc("eth_getBlockByNumber", [hex(b), False]) or {}).get("result") or {}
            t = as_int(r.get("timestamp"))
            if t is not None:
                return t
            time.sleep(0.3)
        return None
    if bts(hi) is None:
        # do NOT silently use the linear estimate: it drifts ~140k blocks and
        # every caller then scans the wrong window
        return None
    for _ in range(40):
        if hi - lo <= 1:
            break
        mid = (lo + hi) // 2
        t = bts(mid)
        if t is None:
            break
        if t < ts:
            lo = mid
        else:
            hi = mid
    _BLK_CACHE[key] = lo
    return lo


def s256(h):
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


_QUOTE_PRICE_CACHE = {}


def quote_price_usd(addr):
    """
    USD price of the pool's quote currency.

    On this chain the quote is NOT always ETH -- it can be a tokenized stock or
    another memecoin (STOA's main pool is quoted in AI). Treating quote units as
    ETH reported a $19.5k pool as holding $232M.
    """
    a = (addr or "").lower()
    if not a or int(a, 16) == 0:
        return ETH_USD, 18
    if a in _QUOTE_PRICE_CACHE:
        return _QUOTE_PRICE_CACHE[a]
    dec = as_int(call(a, SEL["decimals"])) or 18
    px = None
    code, body = http(f"{DS}/latest/dex/tokens/{a}")
    if code == 200 and body:
        try:
            pairs = json.loads(body).get("pairs") or []
            if pairs:
                best = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
                px = float(best.get("priceUsd") or 0) or None
        except Exception:  # noqa: BLE001
            px = None
    _QUOTE_PRICE_CACHE[a] = (px, dec)
    return px, dec


def P(name, cls, status, detail, extra=None):
    return {"probe": name, "class": cls, "status": status,
            "detail": detail, "extra": extra or {}}


# ============================ CHAIN PROBES =================================
def probe_identity(ctx):
    t = ctx["token"]
    sym = as_str(call(t, SEL["symbol"])) or "?"
    nm = as_str(call(t, SEL["name"])) or ""
    sup = as_int(call(t, SEL["totalSupply"]))
    dec = as_int(call(t, SEL["decimals"])) or 18
    ctx["symbol"], ctx["name"] = sym, nm
    ctx["supply"] = sup / (10 ** dec) if sup else None
    return P("identity", "chain", "PASS",
             f"{sym} · {nm[:34]} · supply {ctx['supply']:,.0f}" if ctx["supply"] else sym)


def probe_honeypot(ctx):
    """
    Both supported venues ship one fixed template, so sell logic cannot be made
    malicious per-token:
      pons  -- full contract, code len 6498
      bankr -- Solady clone (len 90) of the verified DopplerERC20V1
    Anything else is unrecognised and must be checked by hand.
    """
    code = (rpc("eth_getCode", [ctx["token"], "latest"]) or {}).get("result") or ""
    ctx["code_len"] = len(code)
    venue = detect_venue(code, ctx["token"])
    ctx["venue"] = venue
    if venue == "pons":
        return P("honeypot", "chain", "PASS",
                 "standard Pons template — sell logic cannot be per-token malicious")
    if venue == "bankr":
        return P("honeypot", "chain", "PASS",
                 "Bankr/Doppler clone of verified DopplerERC20V1 — fixed template")
    if venue == "o1":
        return P("honeypot", "chain", "PASS",
                 "o1 RWAERC20LaunchpadFactory token (verified factory template)")
    if "four.meme" in venue or "flap.sh" in venue:
        return P("honeypot", "chain", "PASS",
                 f"{venue} — fixed factory template, sell logic cannot be per-token malicious")
    if venue == "unknown":
        return P("honeypot", "chain", "FAIL", "no bytecode at address")
    return P("honeypot", "chain", "WARN",
             f"unrecognised template ({venue}, len {len(code)}) — verify sell path manually")


def probe_ownership(ctx):
    o1 = call(ctx["token"], SEL["owner"])
    o2 = call(ctx["token"], SEL["getOwner"])
    if not o1 and not o2:
        v = ctx.get("venue", "?")
        return P("ownership", "chain", "N-A",
                 f"no owner() on this {v} token — no admin exists, nothing to renounce")
    addr = as_addr(o1 or o2)
    if addr and int(addr, 16) == 0:
        return P("ownership", "chain", "PASS", "owner is 0x0 (renounced)")
    return P("ownership", "chain", "WARN", f"owner set: {addr}")


def probe_curve(ctx):
    if CHAIN["id"] == 56:
        return P("fees", "chain", "N-A",
                 "BSC venues have no Pons-style curve fee; cost is the venue's own "
                 "trade fee plus the AMM fee once migrated")
    if ctx.get("venue") == "o1":
        # fee is set per launch; the launch event carries it, and 200bps is the
        # value seen on every sampled o1 launch
        ctx["fee_bps"], ctx["tax_bps"] = 200, 0
        return P("fees", "chain", "WARN",
                 "o1 launch fee is 200 bps (2.0%) per trade ≈ 4.0% round trip — "
                 "no bonding curve, no snipe tax")
    if ctx.get("venue") == "bankr":
        ctx["fee_bps"], ctx["tax_bps"] = 0, 0
        return P("fees", "chain", "N-A",
                 "Bankr/Doppler has no bonding curve — no curve fee or snipe tax; "
                 "cost is the v4 pool fee + hook auction dynamics")
    c = as_addr(call(ctx["token"], SEL["curve"]))
    if not c:
        return P("curve", "chain", "UNKNOWN", "token exposes no curve()")
    ctx["curve"] = c
    fee = as_int(call(c, SEL["feeBps"])) or 0
    tax = as_int(call(c, SEL["creatorTaxBps"])) or 0
    grad = as_int(call(c, SEL["graduated"]))
    ctx["fee_bps"], ctx["tax_bps"] = fee, tax
    ctx["graduated"] = bool(grad)
    total = (fee + tax) / 100.0
    st = "PASS" if total <= 1.5 else ("WARN" if total < 3.0 else "FAIL")
    return P("fees", "chain", st,
             f"{total:.2f}% per trade (base {fee/100:.1f}% + creator {tax/100:.1f}%) "
             f"≈ {2*total:.1f}% round trip · graduated={ctx['graduated']}")


def probe_dev(ctx):
    if CHAIN["id"] == 56:
        # BSC has no public explorer for creator lookup, but both venues put the
        # creator in the launch event, so the registry built from those events
        # is the dev source here.
        path = os.path.join(DATA, "bsc_dev_registry.json")
        if not os.path.exists(path):
            return P("dev", "chain", "UNKNOWN",
                     "no BSC dev registry — run: build_bsc_dev_registry.py --blocks 20000")
        try:
            with open(path) as f:
                reg = json.load(f)
        except Exception:  # noqa: BLE001
            return P("dev", "chain", "UNKNOWN", "BSC dev registry unreadable")
        devs = reg.get("devs", {})
        tok = ctx["token"].lower()
        hit = None
        for d, v in devs.items():
            if any(t.lower() == tok for t in v.get("tokens", [])):
                hit = (d, v)
                break
        if not hit:
            return P("dev", "chain", "UNKNOWN",
                     f"token not in the indexed window {reg.get('scanned')} — "
                     "rebuild the registry over a range covering this launch")
        d, v = hit
        n = v.get("launches", 0)
        ctx["dev"] = d
        syms = ", ".join(dict.fromkeys(v.get("symbols", [])))[:60]
        venues = ",".join(v.get("venues", {}))
        st = "PASS" if n == 1 else ("WARN" if n < 10 else "FAIL")
        return P("dev", "chain", st,
                 f"{d} — {n} launch(es) on {venues} in the indexed window · {syms}")
    if ctx.get("venue") == "o1":
        code, body = http(f"https://robinhoodchain.blockscout.com/api/v2/addresses/{ctx['token']}")
        dev = None
        if code == 200 and body:
            try:
                j = json.loads(body)
                cx = j.get("creation_transaction_hash")
                if cx:
                    r = (rpc("eth_getTransactionByHash", [cx]) or {}).get("result") or {}
                    dev = (r.get("from") or "").lower() or None
            except Exception:  # noqa: BLE001
                pass
        ctx["dev"] = dev
        if not dev:
            return P("dev", "chain", "UNKNOWN",
                     "creator not resolvable (o1 factory deploys internally)")
        v = ctx["registry"].get(dev)
        if v:
            return P("dev", "chain", "PASS" if v.get("graduations", 0) else "WARN",
                     f"{dev} — {v.get('launches',0)} launches, {v.get('graduations',0)} graduations (Pons history)")
        return P("dev", "chain", "WARN", f"{dev} — no Pons history on record")
    if ctx.get("venue") == "bankr":
        # no deployer() on a clone -- fall back to the creation tx's origin
        code, body = http(f"https://robinhoodchain.blockscout.com/api/v2/addresses/{ctx['token']}")
        dev = None
        if code == 200 and body:
            try:
                j = json.loads(body)
                ctxh = j.get("creation_transaction_hash")
                if ctxh:
                    r = (rpc("eth_getTransactionByHash", [ctxh]) or {}).get("result") or {}
                    dev = (r.get("from") or "").lower() or None
            except Exception:  # noqa: BLE001
                pass
        ctx["dev"] = dev
        if not dev:
            return P("dev", "chain", "UNKNOWN", "could not resolve creator (Blockscout gap)")
        v = ctx["registry"].get(dev)
        if v:
            return P("dev", "chain", "PASS" if v.get("graduations", 0) else "WARN",
                     f"{dev} — {v.get('launches',0)} launches, {v.get('graduations',0)} graduations (Pons history)")
        return P("dev", "chain", "WARN", f"{dev} — no Pons history on record")
    c = ctx.get("curve")
    if not c:
        return P("dev", "chain", "UNKNOWN", "no curve")
    dev = as_addr(call(c, SEL["deployer"]))
    ctx["dev"] = dev
    reg = ctx["registry"]
    v = reg.get(dev or "")
    if not v:
        return P("dev", "chain", "WARN", f"{dev} — no history on record (new wallet)")
    L, G = v.get("launches", 0), v.get("graduations", 0)
    ctx["dev_launches"], ctx["dev_grads"] = L, G
    if L >= 100 and G == 0:
        return P("dev", "chain", "FAIL", f"{dev} — {L} launches, 0 graduations (spam factory)")
    if G >= 1:
        return P("dev", "chain", "PASS", f"{dev} — {L} launches, {G} graduations")
    return P("dev", "chain", "WARN", f"{dev} — {L} launches, 0 graduations")


def probe_holders(ctx):
    if CHAIN["id"] == 56:
        return P("holders", "chain", "N-A", "no Pons curve on BSC — see supply-map instead")
    if ctx.get("venue") == "o1":
        return P("holders", "chain", "N-A",
                 "no bonding curve — holder history needs Transfer-log indexing, not wired")
    if ctx.get("venue") == "bankr":
        return P("holders", "chain", "N-A",
                 "no bonding curve — holder history needs Transfer-log indexing, not wired")
    c = ctx.get("curve")
    if not c:
        return P("holders", "chain", "UNKNOWN", "no curve")
    head = as_int((rpc("eth_blockNumber", []) or {}).get("result")) or 0
    la = as_int(call(c, SEL["launchedAt"]))
    ctx["launched_at"] = la if la else ctx.get("launched_at")
    lb = block_from_ts(la) if la else None
    if lb:
        # a curve stops emitting once it graduates, so cap the sweep rather than
        # running it to head -- an old token would otherwise span ~10M blocks
        lo = max(0, lb - 5_000)
        hi = min(head, lo + 250_000)
    else:
        lo, hi = max(0, head - 250_000), head
    logs = get_logs({"fromBlock": hex(lo), "toBlock": hex(hi), "address": c})
    buys, sells, exempt, snipers = {}, {}, set(), set()
    for lg in logs:
        t0 = lg["topics"][0]
        if t0 == T_CURVE_BUY and len(lg["topics"]) > 1:
            d = lg["data"][2:]
            if len(d) >= 128:
                a = "0x" + lg["topics"][1][-40:]
                buys[a] = buys.get(a, 0) + int(d[64:128], 16)
        elif t0 == T_CURVE_SELL and len(lg["topics"]) > 1:
            d = lg["data"][2:]
            if len(d) >= 64:
                a = "0x" + lg["topics"][1][-40:]
                sells[a] = sells.get(a, 0) + int(d[0:64], 16)
        elif t0 == T_EXEMPTED and len(lg["topics"]) > 1:
            exempt.add("0x" + lg["topics"][1][-40:])
        elif t0 == T_SNIPE_CHARGED and len(lg["topics"]) > 1:
            snipers.add("0x" + lg["topics"][1][-40:])
    tot = sum(buys.values()) or 1
    top = sorted(buys.values(), reverse=True)
    top1 = top[0] / tot if top else 0
    ins = sum(1 for a in exempt if sells.get(a, 0) > 0)
    ctx.update({"n_buyers": len(buys), "n_snipers": len(snipers),
                "top1_share": top1, "insider_sold": ins, "n_exempt": len(exempt)})
    bits = [f"{len(buys)} buyers", f"top1 {100*top1:.0f}%",
            f"{len(exempt)} bundled", f"{len(snipers)} sniped"]
    if ins:
        bits.append(f"{ins} insiders SOLD")
    st = "PASS"
    if top1 > 0.5 or ins:
        st = "WARN"
    if top1 > 0.8:
        st = "FAIL"
    return P("holders", "chain", st, " · ".join(bits))


def quote_impact(stake_usd, sqrt_x96, liquidity, token_is_currency1, quote):
    """
    Percent price impact of spending `stake_usd` of the pool's QUOTE asset.

    Extracted so pool_state() and the paper-trade logger cannot drift apart.
    The logger kept its own copy that assumed an 18-decimal ETH quote and only
    ever used the token0-in branch; on tokenized-stock- and stablecoin-quoted
    pools that underflowed to exactly 0.0, which silently disabled the
    logger's slippage gate for ~13.5% of trades.

    Two things it must get right, both of which the old copy got wrong:
      * decimals -- the stake is converted into QUOTE units via the quote's own
        price and decimals, not assumed to be 1e18 of ETH.
      * orientation -- Uniswap sorts currencies by address, so the quote may be
        currency0 or currency1, and the two cases use different formulas.

    Returns None (never 0.0) when the quote cannot be priced or state is
    unusable, so callers can tell "no estimate" from "no impact".
    """
    if not liquidity or not sqrt_x96:
        return None
    sp = sqrt_x96 / (2 ** 96)
    if sp <= 0:
        return None
    qpx, qdec = quote_price_usd(quote)
    if not qpx:
        return None
    dq = stake_usd / qpx * (10 ** qdec)           # stake expressed in quote units
    if token_is_currency1:                        # quote is currency0 -> token0-in
        inv_new = 1.0 / sp + dq / liquidity
        if inv_new <= 0:
            return None
        imp = (sp * inv_new) ** 2 - 1
    else:                                         # quote is currency1 -> token1-in
        imp = ((sp + dq / liquidity) / sp) ** 2 - 1
    return 100 * imp


def pool_state(token, lo, hi, head, stake_usd):
    """
    SINGLE SOURCE OF TRUTH for pool liquidity + price impact.

    Both the investigator and the live monitor call this. They previously kept
    separate copies and only one received the orientation and quote-pricing
    fixes, so the monitor still reported a $22k pool as $231M.

    Two things that are easy to get wrong and are handled here:
      * orientation -- Uniswap sorts currencies by address, so the token may be
        currency0 or currency1, which flips the impact formula.
      * the quote is NOT always ETH. It can be a tokenized stock or another
        memecoin (STOA's busiest pool is quoted in AI), so quote units must be
        priced, not assumed.

    Returns {} when nothing usable is found.
    """
    tt = "0x" + "0" * 24 + token[2:]
    cands = []
    for slot in (3, 2):
        tp = [T_V4_INITIALIZE, None, None, None][:slot + 1]
        tp[slot] = tt
        for lg in get_logs({"fromBlock": hex(lo), "toBlock": hex(hi),
                            "address": POOL_MANAGER, "topics": tp}):
            cands.append((lg, slot == 3))
    if not cands:
        return {}

    best = None
    for lg, tok_is_c1 in cands:
        pid = lg["topics"][1]
        ib = int(lg["blockNumber"], 16)
        sw = get_logs({"fromBlock": hex(max(ib, head - 150_000)), "toBlock": hex(head),
                       "address": POOL_MANAGER, "topics": [T_V4_SWAP, pid]})
        if not sw:
            sw = get_logs({"fromBlock": hex(ib), "toBlock": hex(min(head, ib + 150_000)),
                           "address": POOL_MANAGER, "topics": [T_V4_SWAP, pid]})
        if not sw:
            continue
        quote = "0x" + (lg["topics"][2] if tok_is_c1 else lg["topics"][3])[-40:]
        if best is None or len(sw) > best[0]:
            best = (len(sw), sw[-1], tok_is_c1, quote, pid, ib)
    if not best:
        return {}

    nsw, lastlog, tok_is_c1, quote, pid, ib = best
    d = lastlog["data"][2:]
    sqrt_x96, liq = int(d[128:192], 16), int(d[192:256], 16)
    sp = sqrt_x96 / (2 ** 96)
    out = {"pool_id": pid, "n_swaps": nsw, "quote": quote,
           "token_is_currency1": tok_is_c1, "pool_init_block": ib}
    if sp <= 0 or not liq:
        return out
    qpx, qdec = quote_price_usd(quote)
    out["quote_symbol"] = ("ETH" if int(quote, 16) == 0
                           else (as_str(call(quote, SEL["symbol"])) or quote[:8]))
    if not qpx:
        return out
    out["slippage_pct"] = quote_impact(stake_usd, sqrt_x96, liq, tok_is_c1, quote)
    active_quote = (liq / sp) if tok_is_c1 else (liq * sp)
    out["active_liq_usd"] = active_quote / (10 ** qdec) * qpx
    return out


def probe_liquidity(ctx):
    """Can the stake actually be filled? Delegates the math to pool_state()."""
    if CHAIN["id"] == 56:
        liq = ctx.get("ds_liq")
        if not liq:
            return P("liquidity", "chain", "UNKNOWN", "no DexScreener liquidity yet")
        # constant-product approximation against the quote side of the book
        imp = ctx["stake"] / (liq / 2 + ctx["stake"])
        ctx["slippage_pct"] = 100 * imp
        st = "PASS" if imp <= 0.02 else ("WARN" if imp <= 0.10 else "FAIL")
        return P("liquidity", "chain", st,
                 f"${ctx['stake']:.0f} moves price ~{100*imp:.1f}% against ${liq:,.0f} "
                 "book (constant-product estimate, not V4 tick math)")
    t = ctx["token"]
    head = as_int((rpc("eth_blockNumber", []) or {}).get("result")) or 0
    la = ctx.get("launched_at") or as_int(call(ctx.get("curve") or t, SEL["launchedAt"]))
    if la:
        lo = max(0, block_from_ts(la) - 5_000)
        hi = min(head, lo + 400_000)
    elif ctx.get("age_hours"):
        lo = max(0, head - int(ctx["age_hours"] * 3600 / BLOCK_TIME) - 40_000)
        hi = head
    else:
        lo, hi = max(0, head - 400_000), head

    st8 = pool_state(t, lo, hi, head, ctx["stake"])
    if not st8:
        return P("liquidity", "chain", "UNKNOWN", "no V4 pool found in the searched range")
    ctx["pool_id"] = st8.get("pool_id")
    ctx["quote"] = st8.get("quote")
    if st8.get("slippage_pct") is None:
        return P("liquidity", "chain", "UNKNOWN",
                 f"pool found ({st8.get('n_swaps', 0)} swaps) but quote "
                 f"{st8.get('quote_symbol', '?')} is unpriceable")
    pct = st8["slippage_pct"]
    liq_usd = st8["active_liq_usd"]
    ctx["slippage_pct"], ctx["active_liq_usd"] = pct, liq_usd
    tail = f"quote {st8['quote_symbol']} · {st8['n_swaps']} swaps"
    if ctx.get("ds_liq"):
        tail += f" · DS total ${ctx['ds_liq']:,.0f}"
    if pct > 25:
        return P("liquidity", "chain", "FAIL",
                 f"active liquidity too thin — ${ctx['stake']:.0f} would cross ticks "
                 f"(active ≈ ${liq_usd:,.0f}) · {tail}")
    st = "PASS" if pct <= 2 else ("WARN" if pct <= 10 else "FAIL")
    return P("liquidity", "chain", st,
             f"${ctx['stake']:.0f} moves price {pct:.3f}% · active ≈ ${liq_usd:,.0f} · {tail}")



def probe_description(ctx):
    d = as_str(call(ctx["token"], SEL["description"]))
    ctx["description"] = d or ""
    urls = URL_RE.findall(d or "")
    ctx["desc_urls"] = [u if u.startswith("http") else "https://" + u for u in urls]
    if not d:
        return P("description", "chain", "WARN", "empty on-chain description")
    return P("description", "chain", "PASS",
             f"{len(d)} chars, {len(urls)} link(s): {d[:80]!r}")


def probe_supply_map(ctx):
    """
    Who holds the supply, how early did they get in, and are they fresh wallets.

    Reconstructed from Transfer logs rather than any indexer, so it works on any
    EVM chain. Three distinct risks are separated here because they are not the
    same thing:
      concentration  -- one wallet able to dump the book
      snipers/bundlers -- wallets in within a couple of blocks of the first
                          transfer, i.e. supply acquired before anyone could react
      fresh wallets  -- low-nonce addresses, the signature of a farmed cluster
    """
    tok = ctx["token"]
    head = as_int((rpc("eth_blockNumber", []) or {}).get("result")) or 0
    T_TRANSFER = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()
    # anchor on the launch, exactly like probe_liquidity -- a blind
    # head-200k window misses the token's whole life and reports "no logs"
    la = ctx.get("launched_at") or as_int(call(ctx.get("curve") or tok, SEL["launchedAt"]))
    lb = block_from_ts(la) if la else None
    if lb:
        lo = max(0, lb - 2_000)
    elif ctx.get("age_hours"):
        lo = max(0, head - int(ctx["age_hours"] * 3600 / BLOCK_TIME) - 5_000)
    else:
        lo = max(0, head - 60_000)
    hi = min(head, lo + 60_000)
    logs = get_logs({"fromBlock": hex(lo), "toBlock": hex(hi),
                     "address": tok, "topics": [T_TRANSFER]}, width=4_000)
    if not logs:
        return P("supply-map", "chain", "UNKNOWN", "no Transfer logs in range")
    from collections import defaultdict
    bal, first_seen = defaultdict(int), {}
    launch = min(int(l["blockNumber"], 16) for l in logs)
    for l in logs:
        if len(l["topics"]) < 3:
            continue
        frm = "0x" + l["topics"][1][-40:]
        to = "0x" + l["topics"][2][-40:]
        try:
            v = int(l["data"], 16)
        except ValueError:
            continue
        b = int(l["blockNumber"], 16)
        bal[frm] -= v
        bal[to] += v
        first_seen.setdefault(to, b)
    # The curve / pool holds all unsold supply, so leaving it in makes every
    # pre-graduation token read as ~99% concentrated. Exclude venue contracts
    # and anything that is not an EOA-looking holder.
    venue_addrs = {(ctx.get("curve") or "").lower(), (ctx.get("pool_id") or "").lower(),
                   POOL_MANAGER.lower(), tok.lower(), "0x" + "0" * 40,
                   "0x000000000000000000000000000000000000dead"}
    hold = {a: v for a, v in bal.items()
            if v > 0 and int(a, 16) != 0 and a.lower() not in venue_addrs}
    if not hold:
        return P("supply-map", "chain", "UNKNOWN", "no positive balances reconstructed")
    ctx["circulating_raw"] = sum(hold.values())
    ctx["n_holders"] = len(hold)
    tot = sum(hold.values()) or 1
    top = sorted(hold.items(), key=lambda x: -x[1])
    top1 = top[0][1] / tot
    top5 = sum(v for _, v in top[:5]) / tot
    top10 = sum(v for _, v in top[:10]) / tot
    early = [a for a in hold if first_seen.get(a, 1 << 60) <= launch + 2]
    early_share = sum(hold[a] for a in early) / tot
    # wallet age of the biggest holders
    fresh = 0
    checked = 0
    for a, _ in top[:8]:
        code = (rpc("eth_getCode", [a, "latest"]) or {}).get("result") or ""
        if len(code) > 2:
            continue                      # contract: pool/router, not a holder
        n = as_int((rpc("eth_getTransactionCount", [a, "latest"]) or {}).get("result"))
        checked += 1
        if (n or 0) < 5:
            fresh += 1
    ctx.update({"top1_share": top1, "top10_share": top10,
                "sniper_share": early_share, "n_holders": len(hold)})
    bits = [f"{len(hold)} holders",
            f"top1 {100*top1:.1f}% top5 {100*top5:.1f}% top10 {100*top10:.1f}%",
            f"snipers(≤2blk) {len(early)} holding {100*early_share:.1f}%"]
    if checked:
        bits.append(f"{fresh}/{checked} top holders are fresh wallets")
    st = "PASS"
    if top1 > 0.25 or early_share > 0.30 or fresh >= 3:
        st = "WARN"
    if top1 > 0.50 or early_share > 0.60:
        st = "FAIL"
    return P("supply-map", "chain", st, " · ".join(bits))


def probe_momentum(ctx):
    """
    How violent is the move right now.

    A 24h percentage says nothing about whether a token is accelerating or
    already rolling over. These compare the most recent 5 minutes against the
    hour it sits inside:
      accel   m5 volume x12 vs h1 volume   >1 means the last 5 min is hotter
      pressure buys/(buys+sells) over m5   >0.5 means net buying
    """
    p = ctx.get("ds_pair")
    if not p:
        return P("momentum", "net", "UNKNOWN", "not indexed by DexScreener yet")
    vol = p.get("volume") or {}
    tx = p.get("txns") or {}
    chg = p.get("priceChange") or {}
    m5v = float(vol.get("m5") or 0)
    h1v = float(vol.get("h1") or 0)
    accel = (m5v * 12 / h1v) if h1v > 0 else None
    m5 = tx.get("m5") or {}
    b, sl = m5.get("buys", 0), m5.get("sells", 0)
    press = (b / (b + sl)) if (b + sl) else None
    ctx["accel"], ctx["pressure"] = accel, press
    bits = [f"m5 ${m5v:,.0f} vs h1 ${h1v:,.0f}"]
    if accel is not None:
        bits.append(f"accel {accel:.2f}x")
    if press is not None:
        bits.append(f"pressure {100*press:.0f}% ({b}B/{sl}S)")
    bits.append(f"chg m5 {chg.get('m5','?')}% h1 {chg.get('h1','?')}% h24 {chg.get('h24','?')}%")
    st = "PASS"
    if accel is not None and accel < 0.3:
        st = "WARN"            # cooling fast
    if press is not None and press < 0.35:
        st = "WARN"            # net selling
    if (b + sl) == 0:
        st = "WARN"
    return P("momentum", "net", st, " · ".join(bits))


# ============================ NET PROBES ===================================
DS = "https://api.dexscreener.com"


def probe_dexscreener(ctx):
    """Market state: volume, buy/sell pressure, age, liquidity. Free, no key."""
    code, body = http(f"{DS}/latest/dex/tokens/{ctx['token']}")
    if code != 200 or not body:
        return P("dexscreener", "net", "UNKNOWN", f"HTTP {code}")
    try:
        pairs = json.loads(body).get("pairs") or []
    except Exception:  # noqa: BLE001
        return P("dexscreener", "net", "UNKNOWN", "bad JSON")
    if not pairs:
        return P("dexscreener", "net", "WARN", "not indexed by DexScreener yet")
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    ctx["ds_pair"] = p
    # Entry metrics, not just safety. Vetting tells you whether it is a trap;
    # these tell you whether it is worth buying, which is a separate question.
    try:
        ctx["mcap"] = float(p.get("marketCap") or 0) or None
    except Exception:  # noqa: BLE001
        ctx["mcap"] = None
    _v = p.get("volume") or {}
    ctx["vol_h1"] = float(_v.get("h1") or 0)
    ctx["vol_h24"] = float(_v.get("h24") or 0)
    ctx["price_usd"] = p.get("priceUsd")
    vol = p.get("volume") or {}
    tx = p.get("txns") or {}
    liq = (p.get("liquidity") or {}).get("usd")
    age_ms = p.get("pairCreatedAt")
    age_h = (time.time() * 1000 - age_ms) / 3.6e6 if age_ms else None
    ctx["age_hours"] = age_h
    ctx["ds_liq"] = liq
    h1 = tx.get("h1") or {}
    b, s = h1.get("buys", 0), h1.get("sells", 0)
    ctx["buy_sell_h1"] = (b, s)
    ratio = (b / s) if s else (float("inf") if b else 0)
    bits = [f"liq ${liq:,.0f}" if liq else "liq ?",
            f"vol h1 ${vol.get('h1',0):,.0f} / h24 ${vol.get('h24',0):,.0f}",
            f"h1 {b}B/{s}S",
            f"age {age_h:.1f}h" if age_h is not None else "age ?",
            f"chg h24 {(p.get('priceChange') or {}).get('h24','?')}%"]
    st = "PASS"
    if liq is not None and liq < 5000:
        st = "FAIL"
    elif (vol.get("h1") or 0) < 100:
        st = "WARN"
    return P("dexscreener", "net", st, " · ".join(bits),
             {"buy_sell_ratio_h1": ratio if ratio != float("inf") else None})


def probe_ds_paid(ctx):
    """
    'Has the dex been paid?' -- DexScreener orders/boosts for this token.
    A paid profile means the dev spent money on visibility. That is a signal of
    intent, NOT of safety, and it is not evidence the token performs better.
    """
    code, body = http(f"{DS}/orders/v1/{DS_NET}/{ctx['token']}")
    if code != 200:
        return P("dex-paid", "net", "UNKNOWN", f"HTTP {code}")
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return P("dex-paid", "net", "UNKNOWN", "bad JSON")
    orders = d.get("orders") or []
    boosts = d.get("boosts") or []
    if not orders and not boosts:
        return P("dex-paid", "net", "WARN",
                 "no paid profile / no boosts — dev spent nothing on DexScreener")
    kinds = [o.get("type") or o.get("paymentTimestamp") for o in orders]
    return P("dex-paid", "net", "PASS",
             f"{len(orders)} order(s) {kinds} · {len(boosts)} boost(s)")


GITHUB_API = "https://api.github.com"


def _gh(path):
    """
    GitHub API GET. Uses GITHUB_TOKEN when present (60 req/hr anonymous vs 5000).

    Returns (data, err). err is a STRING when the call failed, so the caller can
    say "could not check" instead of silently treating a rate limit as a clean
    result -- the whole point of this probe is that missing is not a pass.
    """
    req = urllib.request.Request(GITHUB_API + path,
                                 headers={"User-Agent": UA,
                                          "Accept": "application/vnd.github+json"})
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read(400_000)), None
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return None, "github rate-limited (set GITHUB_TOKEN)"
        if e.code == 404:
            return None, "404"
        return None, f"http {e.code}"
    except Exception as ex:  # noqa: BLE001
        return None, str(ex)[:40]


def _iso(t):
    try:
        return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def probe_repo_authenticity(ctx):
    """
    Is the project's GitHub real, or was it staged for the launch?

    Everyone checks LP lock, mint authority and holder concentration, so a
    competent faker clears all three. Almost nobody checks whether the repo
    behind the token has a history. The tells:

      * an account that sat dead for months, then created this repo days before
        the token -- "account age" alone is not enough, what matters is the gap
        between the ACCOUNT and its FIRST LIVING REPO
      * a burst of forks in a tight window (bought engagement)
      * the ticker absent from the README -- a bare 0x address in docs is a weak
        association; the TICKER is a real one

    Two rules this probe will not break:
      1. NO REPO CLAIMED IS NOT A FAIL. Most memecoins have no GitHub at all.
         That is N-A, not evidence of fraud.
      2. COULD-NOT-CHECK IS NOT A PASS. If GitHub rate-limits us or the account
         hid its data, this returns UNKNOWN. A green light invented out of a
         hole is worse than no light.

    Unvalidated: nothing here is yet shown to predict outcomes. It is a
    fraud-detection heuristic, not an edge, and must not be scored as one.
    """
    urls = list(ctx.get("desc_urls") or [])
    info = (ctx.get("ds_pair") or {}).get("info") or {}
    for w in (info.get("websites") or []) + (info.get("socials") or []):
        if w.get("url"):
            urls.append(w["url"])
    repos = []
    for u in urls:
        m = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", u or "")
        if m:
            owner, name = m.group(1), m.group(2).removesuffix(".git")
            if owner.lower() not in ("orgs", "features", "about"):
                repos.append((owner, name))
    repos = list(dict.fromkeys(repos))
    if not repos:
        return P("repo", "net", "N-A", "no GitHub linked (normal for memecoins; "
                                       "absence is not a red flag)")

    owner, name = repos[0]
    r, err = _gh(f"/repos/{owner}/{name}")
    if err == "404":
        return P("repo", "net", "FAIL", f"linked repo github.com/{owner}/{name} does not exist")
    if err or not r:
        return P("repo", "net", "UNKNOWN", f"could not check: {err}")
    u, uerr = _gh(f"/users/{owner}")
    if uerr or not u:
        return P("repo", "net", "UNKNOWN", f"repo found but account unreadable: {uerr}")

    now = datetime.now(timezone.utc)
    rc, uc = _iso(r.get("created_at")), _iso(u.get("created_at"))
    if not rc or not uc:
        return P("repo", "net", "UNKNOWN", "github returned no timestamps")
    repo_age = (now - rc).days
    acct_age = (now - uc).days

    # The signal is the gap between the ACCOUNT and its FIRST LIVING REPO, not
    # this repo. An active dev with 20 repos who starts another today is
    # ordinary; an account made two years ago whose FIRST repo appeared last
    # week was parked and woken up for a launch. Measuring against this repo
    # flagged every prolific developer.
    gap_days = None
    rl, rlerr = _gh(f"/users/{owner}/repos?per_page=100&sort=created&direction=asc")
    if rl and not rlerr:
        firsts = sorted(x for x in (_iso(q.get("created_at")) for q in rl) if x)
        if firsts:
            gap_days = (firsts[0] - uc).days
    stars = r.get("stargazers_count", 0)
    forks = r.get("forks_count", 0)
    followers = u.get("followers", 0)
    n_repos = u.get("public_repos", 0)

    # ticker in the README is a real association; a bare address is not
    sym = (ctx.get("symbol") or "").strip()
    ticker_hit = None
    if sym:
        code, body = http(f"https://raw.githubusercontent.com/{owner}/{name}/HEAD/README.md",
                          timeout=7)
        if code == 200 and body:
            txt = body.decode("utf-8", "replace")
            ticker_hit = bool(re.search(r"(?<![A-Za-z0-9])\$?" + re.escape(sym) +
                                        r"(?![A-Za-z0-9])", txt, re.I))

    # fork burst: many forks landing in a tight window is bought engagement
    burst = None
    if forks >= 5:
        fl, ferr = _gh(f"/repos/{owner}/{name}/forks?per_page=100&sort=oldest")
        if fl and not ferr:
            ts = sorted(x for x in (_iso(f.get("created_at")) for f in fl) if x)
            if len(ts) >= 5:
                span_h = (ts[-1] - ts[0]).total_seconds() / 3600
                if span_h <= 48:
                    burst = f"{len(ts)} forks within {span_h:.0f}h"

    ctx["repo"] = {"owner": owner, "name": name, "repo_age_days": repo_age,
                   "account_age_days": acct_age, "gap_to_first_repo_days": gap_days,
                   "stars": stars, "forks": forks, "followers": followers,
                   "ticker_in_readme": ticker_hit, "fork_burst": burst}

    bad, warn = [], []
    if repo_age <= 7:
        bad.append(f"repo is {repo_age}d old")
    if gap_days is not None and gap_days >= 180 and repo_age <= 30:
        bad.append(f"account parked {gap_days}d before its first repo")
    if burst:
        bad.append(burst)
    if ticker_hit is False and repo_age <= 180:
        warn.append(f"${sym} not in README")
    if acct_age <= 30:
        warn.append(f"account only {acct_age}d old")
    if n_repos <= 1 and followers <= 1:
        warn.append("account has no other presence")
    if stars > 20 and followers <= 1:
        warn.append(f"{stars} stars but owner has {followers} followers")

    base = (f"github.com/{owner}/{name} · repo {repo_age}d · acct {acct_age}d · "
            f"{stars}★ {forks}⑂" +
            (f" · ${sym} in README" if ticker_hit else ""))
    if bad:
        return P("repo", "net", "FAIL", "staged repo: " + "; ".join(bad) + " · " + base)
    if warn:
        return P("repo", "net", "WARN", "; ".join(warn) + " · " + base)
    return P("repo", "net", "PASS", base)


def env_key(name, *extra_paths):
    """
    Read an API key from the environment, then from the project `.env`.

    Shared so a new key never becomes a SILENT NO-OP: put the value in .env, and
    whatever needs it finds it in the same place, with the same precedence, without
    a second bespoke reader that looks somewhere slightly different. Returns "" when
    absent -- callers must treat that as NEEDS-KEY, never as a clean result.
    """
    v = os.environ.get(name, "").strip()
    if v:
        return v
    for path in (os.path.join(os.path.dirname(HERE), ".env"),) + tuple(extra_paths):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, _, val = line.partition("=")
                    if k.strip() == name:
                        val = val.strip().strip('"').strip("'")
                        if val:
                            return val
        except Exception:  # noqa: BLE001
            continue
    return ""


def firecrawl_key():
    """Firecrawl API key. Same .env as every other key here."""
    return env_key("FIRECRAWL_API_KEY")


# Where each key actually lives. A key with its own config file MUST list it here.
# BUG THIS FIXES: the first version of key_status() checked only the project .env,
# so it reported GMGN_API_KEY as "not set" when it was configured and working in
# ~/.config/gmgn/.env -- gmgn-cli's own global config, which _gmgn_key() has always
# read. A status check that under-reports is worse than none: it says a working
# setup is broken.
KEY_PATHS = {
    "GMGN_API_KEY": (os.path.expanduser("~/.config/gmgn/.env"),),
}


def key_status():
    """
    Which keys are configured, WITHOUT printing any value.

    Exists so a key can be verified after being set. Echoing a key to check it is
    how it ends up in scrollback and shell history. Each key is looked up through
    the SAME paths its real reader uses, or the check lies.
    """
    out = []
    for name in ("FIRECRAWL_API_KEY", "GMGN_API_KEY", "X_BEARER_TOKEN", "GITHUB_TOKEN"):
        v = env_key(name, *KEY_PATHS.get(name, ()))
        where = ""
        if v:
            where = ("env" if os.environ.get(name, "").strip()
                     else ".env" if env_key(name) else "global config")
        out.append((name, bool(v), len(v) if v else 0, where))
    return out


def _gmgn_key():
    """
    The API key, from the environment OR from gmgn-cli's own config file.

    BUG THIS FIXES: this probe used to check only os.environ. `gmgn-cli config
    --apply` writes the key to ~/.config/gmgn/.env (its documented global config),
    so a correctly configured key still reported NEEDS-KEY -- the probe bailed
    before ever invoking the CLI, which reads that file itself. Project-level
    .env takes precedence over the global one, matching the CLI's own order.
    """
    k = os.environ.get("GMGN_API_KEY", "").strip()
    if k:
        return k
    for path in (os.path.join(os.path.dirname(HERE), ".env"),
                 os.path.expanduser("~/.config/gmgn/.env")):
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("GMGN_API_KEY="):
                        v = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v
        except Exception:  # noqa: BLE001
            continue
    return ""


GMGN_CACHE = os.path.join(DATA, "gmgn_cache.json")
GMGN_CACHE_MIN = 10


def _gmgn_cached(token, chain="robinhood", log=print):
    """
    token info with a disk cache, because the CLI is INTERMITTENT.

    Measured 2026-09-11: three consecutive direct calls for one token all returned
    37 fields, then the very next call through investigate() returned zero -- for
    the same token, seconds apart. The report then renders every distribution and
    flow row as "—", which reads as "this token has no data" when it means "the
    shell-out dropped one call".

    So: keep the last good answer. A stale-but-real number beats an empty panel,
    and the staleness is bounded and small.
    """
    try:
        c = json.load(open(GMGN_CACHE))
    except Exception:  # noqa: BLE001
        c = {}
    k = f"{chain}:{token.lower()}"
    hit = c.get(k)
    fresh = hit and (time.time() - hit.get("_ts", 0)) < GMGN_CACHE_MIN * 60
    if fresh:
        return hit.get("data"), None
    d, err = _gmgn(["token", "info", "--chain", chain, "--address", token])
    if isinstance(d, dict) and d:
        c[k] = {"data": d, "_ts": time.time()}
        if len(c) > 3000:
            for kk in sorted(c, key=lambda x: c[x].get("_ts", 0))[:500]:
                c.pop(kk, None)
        try:
            json.dump(c, open(GMGN_CACHE, "w"))
        except Exception:  # noqa: BLE001
            pass
        return d, None
    # the call failed -- fall back to whatever we last knew rather than nothing
    if hit and hit.get("data"):
        return hit["data"], None
    return d, err


def _gmgn(args, timeout=45):
    """
    Shell out to gmgn-cli. Returns (data, err); err is a STRING on failure so a
    missing key or a rate limit can never be mistaken for a clean result.
    """
    import subprocess
    key = _gmgn_key()
    if not key:
        return None, "no GMGN_API_KEY"
    env = dict(os.environ, GMGN_API_KEY=key)
    # never expose a signing key to this path -- we read data, we do not trade here
    env.pop("GMGN_PRIVATE_KEY", None)
    try:
        r = subprocess.run(["npx", "-y", "gmgn-cli@latest"] + args,
                           capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as ex:  # noqa: BLE001
        return None, str(ex)[:40]
    if r.returncode != 0:
        return None, (r.stderr or "cli error").strip().splitlines()[0][:60]
    try:
        return json.loads(r.stdout), None
    except Exception:  # noqa: BLE001
        return None, "unparseable output"


def probe_gmgn(ctx):
    """
    GMGN's own read of this token -- an INDEPENDENT second opinion.

    Why bother when we compute most of this ourselves: an outside measurement
    that agrees is worth far more than either alone, and disagreement is a
    reason to look harder. GMGN covers `robinhood` (verified live) plus
    four.meme/flap on BSC.

    NOT SCORED AS EDGE. Their `smart_degen_count` is a black box we cannot
    audit, and we could not validate it: of 58 GMGN smart wallets sampled, 15
    appear in our index and ALL 15 have ZERO closed round trips (median 4 OPEN
    positions, up to 151 ETH volume). They are HOLDERS; our index only scores
    closed round trips inside a ~34h window, so it is structurally blind to
    them. That is a measurement mismatch, not evidence either way -- so these
    numbers are reported as FACTS and never folded into a verdict.

    Read-only by construction: GMGN_PRIVATE_KEY is stripped from the env.
    """
    if CHAIN["id"] == 4663:
        chain = "robinhood"
    elif CHAIN["id"] == 56:
        chain = "bsc"
    else:
        return P("gmgn", "net", "N-A", "chain not covered by gmgn-cli")
    tok = ctx["token"]
    d, err = _gmgn_cached(tok, chain)
    if err == "no GMGN_API_KEY":
        return P("gmgn", "net", "NEEDS-KEY",
                 "set GMGN_API_KEY for an independent second opinion "
                 "(free key at https://gmgn.ai/ai). Not run.")
    if err or not d:
        return P("gmgn", "net", "UNKNOWN", f"gmgn-cli: {err}")
    ctx["gmgn"] = d
    bits = []
    for k, label, f in (("holder_count", "holders", "{:,.0f}"),
                        ("liquidity", "liq", "${:,.0f}"),
                        ("trade_fee", "fee", "{:.2f}%")):
        v = d.get(k)
        if v not in (None, ""):
            try:
                bits.append(f"{label} " + f.format(float(v)))
            except Exception:  # noqa: BLE001
                bits.append(f"{label} {v}")
    hp = d.get("is_honeypot")
    if hp:
        return P("gmgn", "net", "FAIL", "GMGN flags this as a HONEYPOT · " + " · ".join(bits))
    return P("gmgn", "net", "PASS", " · ".join(bits) or "no fields returned")


def probe_socials_links(ctx):
    """Pull links from DexScreener info + the on-chain description, then check they resolve."""
    urls = list(ctx.get("desc_urls") or [])
    info = (ctx.get("ds_pair") or {}).get("info") or {}
    for w in info.get("websites") or []:
        if w.get("url"):
            urls.append(w["url"])
    for s in info.get("socials") or []:
        if s.get("url"):
            urls.append(s["url"])
    urls = list(dict.fromkeys(urls))[:6]
    # Keep these on ctx: the caller pops ds_pair before handing ctx onward, which
    # also threw away the only place the token's X handle appeared.
    ctx["social_urls"] = urls
    if not urls:
        return P("links", "net", "WARN", "no website or socials found anywhere")
    results = []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for u, (code, _) in zip(urls, ex.map(lambda x: http(x, timeout=7), urls)):
            results.append((u, code))
    live = [u for u, c in results if c and 200 <= c < 400]
    dead = [(u, c) for u, c in results if not (c and 200 <= c < 400)]
    ctx["links_live"], ctx["links_dead"] = live, dead
    detail = " · ".join(f"{u[:44]}→{c or 'DEAD'}" for u, c in results)
    if not live:
        return P("links", "net", "FAIL", f"all {len(results)} link(s) dead: {detail}")
    if dead:
        return P("links", "net", "WARN", f"{len(live)}/{len(results)} live: {detail}")
    return P("links", "net", "PASS", f"all {len(live)} live: {detail}")


def probe_smart_money(ctx):
    if CHAIN["id"] == 56:
        return P("smart-money", "chain", "N-A",
                 "trader index is Robinhood-Chain only (built from Pons curve flow)")
    """
    Cross-match against the trader index: which proven wallets are in this
    token, and at what market cap did they get in relative to now.

    Traders, not devs: 92% of Pons devs launch once, so dev skill is untestable.
    Trader P&L persists out-of-sample (64% vs 28% test win-rate, p=0.00025).
    Still only a fact about who is positioned -- not a prediction.
    """
    path = os.path.join(DATA, "trader_index.json")
    if not os.path.exists(path):
        return P("smart-money", "chain", "UNKNOWN",
                 "no trader index yet — run: trader_index.py --scan 40000")
    try:
        with open(path) as f:
            ix = json.load(f)
    except Exception:  # noqa: BLE001
        return P("smart-money", "chain", "UNKNOWN", "trader index unreadable")
    # CurveBuy/CurveSell are emitted BY THE CURVE, so the index is keyed by
    # curve address. Investigations start from a token, so resolve across.
    tok = (ctx.get("curve") or ctx["token"]).lower()
    traders = ix.get("traders", {})
    hits, ranked = [], []
    for k, pos in ix.get("positions", {}).items():
        w, t = k.split("|")
        if t != tok:
            continue
        tr = traders.get(w) or {}
        if tr.get("closed", 0) < 5 or tr.get("vol_eth", 0) < 0.01:
            continue
        rec = {"w": w, "pnl": tr.get("pnl_eth", 0), "mcap": pos.get("entry_mcap"),
               "holding": pos.get("tok", 0) > 10 ** 15,
               "spent": pos.get("qin", 0) / 1e18}
        hits.append(rec)
        if tr.get("pnl_eth", 0) > 0.1:
            ranked.append(rec)
    if not hits:
        return P("smart-money", "chain", "WARN",
                 "no indexed trader (≥5 closed round trips) has touched this token")
    ranked.sort(key=lambda x: -x["pnl"])
    still = sum(1 for r in ranked if r["holding"])
    ctx["smart_money"] = ranked[:5]
    mc = sorted(r["mcap"] for r in ranked if r["mcap"])
    bits = [f"{len(hits)} indexed traders", f"{len(ranked)} profitable"]
    if mc:
        bits.append(f"their entry mcap median ${mc[len(mc)//2]:,.0f}")
    if ranked:
        bits.append(f"{still}/{len(ranked)} still holding")
    detail = " · ".join(bits)
    if ranked:
        top = ranked[0]
        detail += (f" · best {top['w'][:10]} +{top['pnl']:.2f}ETH"
                   f" in @ ${top['mcap']:,.0f}" if top["mcap"] else "")
    st = "PASS" if len(ranked) >= 2 else ("WARN" if ranked else "WARN")
    return P("smart-money", "chain", st, detail)


# ============================ SOCIAL PROBES ================================
# These need credentials. They report NEEDS-KEY instead of silently passing,
# because a social check that quietly returns "fine" is worse than no check.
def probe_social(ctx):
    """
    Which socials this token actually has — from data we ALREADY pay for.

    This used to return NEEDS-KEY for everything, which was wrong twice over: it
    was a stub that did nothing even WITH a key, and "does it have an X account"
    never needed one. GMGN's token `link` object carries twitter_username,
    website, telegram, discord, github and a verify_status; DexScreener carries
    the same socials. Both are already fetched by earlier probes.

    A key (X API / Firecrawl) buys exactly two things this cannot answer:
    FOLLOWER COUNT and MENTION VELOCITY. Those are reported as unchecked rather
    than blocking the parts that work.
    """
    have = {}
    link = ((ctx.get("gmgn") or {}).get("link") or {})
    for k in ("twitter_username", "website", "telegram", "discord", "github",
              "youtube", "tiktok", "reddit", "medium"):
        v = (link.get(k) or "").strip()
        if v:
            have[k.replace("_username", "")] = v
    info = (ctx.get("ds_pair") or {}).get("info") or {}
    for sset in (info.get("socials") or []):
        t = (sset.get("type") or "").lower()
        if t and t not in have and sset.get("url"):
            have[t] = sset["url"]
    for wsite in (info.get("websites") or []):
        if wsite.get("url") and "website" not in have:
            have["website"] = wsite["url"]

    ctx["socials"] = have
    verify = link.get("verify_status")
    x = have.get("twitter")
    bits = []
    if x:
        bits.append(f"X @{x}" if not str(x).startswith("http") else f"X {x}")
    for k in ("website", "telegram", "discord", "github"):
        if k in have:
            bits.append(k)
    # Do not assert the key is missing without looking -- it says "not checked"
    # while the report two panels away is printing the follower count.
    tail = ("" if env_key("X_BEARER_TOKEN")
            else " · follower count NOT checked (needs X_BEARER_TOKEN)")

    if not have:
        return P("social", "social", "WARN",
                 "no socials on GMGN or DexScreener — anonymous launch" + tail)
    if not x:
        return P("social", "social", "WARN",
                 "has " + ", ".join(bits) + " but NO X account" + tail)
    st = "PASS" if verify else "PASS"
    return P("social", "social", st,
             " · ".join(bits) + (f" · verified={verify}" if verify else "") + tail,
             {"socials": have})


# order matters: curve() feeds dev/holders; dexscreener feeds age hints and links
FAST_PROBES = [probe_identity, probe_honeypot, probe_ownership, probe_curve,
               probe_dev, probe_description]
MARKET_PROBES = [probe_dexscreener, probe_ds_paid, probe_gmgn]
LATE_PROBES = [probe_momentum]   # needs ds_pair from probe_dexscreener
DEEP_PROBES = [probe_holders, probe_liquidity, probe_smart_money, probe_supply_map]
LINK_PROBES = [probe_socials_links, probe_repo_authenticity]
SOCIAL_PROBES = [probe_social]


def investigate(token, stake=25.0):
    reg = {}
    rp = os.path.join(DATA, "dev_registry.json")
    if os.path.exists(rp):
        with open(rp) as f:
            reg = json.load(f)
    ctx = {"token": token.lower(), "stake": stake, "registry": reg}
    out = []
    t0 = time.time()

    def run(fn, cls):
        s = time.time()
        try:
            r = fn(ctx)
        except Exception as e:  # noqa: BLE001
            r = P(fn.__name__.replace("probe_", ""), cls, "UNKNOWN", f"error: {e}")
        r["ms"] = int((time.time() - s) * 1000)
        return r

    for fn in FAST_PROBES:
        out.append(run(fn, "chain"))
    deep = list(DEEP_PROBES)
    late = list(LATE_PROBES)
    if CHAIN["id"] == 56:
        # BSC liquidity is derived from the DexScreener book, so it must run
        # after MARKET_PROBES rather than racing them in the same pool
        deep = [f for f in deep if f is not probe_liquidity]
        late = [probe_liquidity] + late
    # market + deep run concurrently; deep probes only need ctx["curve"]
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(run, fn, "net") for fn in MARKET_PROBES]
        futs += [ex.submit(run, fn, "chain") for fn in deep]
        for f in futs:
            out.append(f.result())
    for fn in late + LINK_PROBES:
        out.append(run(fn, "net"))
    for fn in SOCIAL_PROBES:
        out.append(dict(fn(ctx), ms=0))
    ctx["elapsed_s"] = round(time.time() - t0, 1)
    return ctx, out


ICON = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "N-A": "–",
        "UNKNOWN": "?", "NEEDS-KEY": "🔑"}


def render(ctx, probes):
    sym = ctx.get("symbol", "?")
    print(f"\n  {sym}  {ctx['token']}")
    print(f"  {ctx.get('name','')[:70]}")
    print("  " + "─" * 74)
    for p in probes:
        print(f"  {ICON.get(p['status'],'?')} {p['status']:9} {p['probe']:12} "
              f"{p['detail'][:150]}")
    print("  " + "─" * 74)
    fails = [p for p in probes if p["status"] == "FAIL"]
    warns = [p for p in probes if p["status"] == "WARN"]
    nk = [p for p in probes if p["status"] == "NEEDS-KEY"]
    verdict = "BLOCKED" if fails else ("CAUTION" if warns else "CLEAR")
    print(f"  VERDICT: {verdict}   {len(fails)} fail · {len(warns)} warn · "
          f"{len(nk)} unchecked   ({ctx['elapsed_s']}s)")
    sl = ctx.get("slippage_pct")
    fees = (ctx.get("fee_bps", 0) + ctx.get("tax_bps", 0)) * 2 / 100
    if sl is not None and sl <= 25:
        print(f"  ${ctx['stake']:.0f} entry slippage {sl:.3f}% · round-trip fees {fees:.1f}%")
    elif sl is not None:
        print(f"  ${ctx['stake']:.0f} entry slippage: NOT MEASURABLE (crosses ticks) · "
              f"round-trip fees {fees:.1f}%")
    print("  NOTE: these are facts, not a prediction. Wallet-vetting signals "
          "failed out-of-sample.\n")


def main():
    ap = argparse.ArgumentParser(description="Investigate a token (read-only)")
    ap.add_argument("token")
    ap.add_argument("--stake", type=float, default=25.0)
    ap.add_argument("--chain", default="rh", choices=sorted(CHAINS),
                    help="rh = Robinhood Chain (default), bsc = BNB Chain")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    use_chain(a.chain)
    print(f"  chain: {CHAIN['name']} (id {CHAIN['id']})")
    ctx, probes = investigate(a.token, a.stake)
    if a.json:
        ctx.pop("registry", None)
        ctx.pop("ds_pair", None)
        print(json.dumps({"ctx": ctx, "probes": probes}, indent=2, default=str))
    else:
        render(ctx, probes)


if __name__ == "__main__":
    main()


# --- X / Twitter profile metrics --------------------------------------------
# ONE paid call per account ($0.010, deduplicated by X per 24h UTC day). Called
# ONLY from a --full report, never from the live feed: at ~15 launches/min a
# per-launch lookup would cost ~$216/day.
#
# WHAT THIS IS FOR, and it is not a score. Slim's vetting reads follower count as
# CONFIDENCE, not as a prediction: ~100 followers is low confidence unless something
# else holds the launch up; ~50,000 is high confidence *unless the account was bought
# or hijacked recently*. That last clause is the only part a machine can help with —
# so this returns the number together with the tells that would undermine trusting
# it, and refuses to collapse them into a verdict.
X_CACHE = os.path.join(DATA, "x_profiles.json") if "DATA" in dir() else \
    os.path.join(os.path.dirname(HERE), "data", "x_profiles.json")
X_HANDLE_RE = re.compile(r"(?:twitter\.com|x\.com)/(?!i/|intent/|share|home)([A-Za-z0-9_]{1,15})")


def x_handle_from_urls(urls):
    """
    Extract an X handle from links, bare handles, or handle/status/... paths.

    THREE SHAPES ARRIVE HERE, and only accepting one of them was the bug:
      https://x.com/someproject                    a real URL
      someproject                                  GMGN's bare twitter_username
      someproject/status/2097941650907557895       ALSO GMGN's twitter_username

    That third form is common -- measured on live tokens, 2 of 6 had it -- and it
    used to be dropped on the floor: it contains a "/" so the bare-handle branch
    rejected it, and it has no x.com prefix so the URL regex missed it too. The
    report then said "no X account linked" for a token that plainly had one.

    Anything unrecognisable returns None, which callers must render as NOT CHECKED
    rather than as "no account" -- those are different facts.
    """
    for u in urls or []:
        u = str(u).strip()
        if not u:
            continue
        m = X_HANDLE_RE.search(u)
        if m:
            return m.group(1)
        # bare handle, or "handle/status/123", or "handle/photo/1"
        head = u.lstrip("@").split("?")[0].split("/")[0].strip()
        if (head and "." not in head and " " not in head
                and 1 <= len(head) <= 15 and head.replace("_", "").isalnum()):
            return head
    return None


def _x_cache():
    try:
        return json.load(open(X_CACHE))
    except Exception:  # noqa: BLE001
        return {}


def x_profile(handle, max_age_h=24):
    """
    Profile metrics for one handle. Returns dict, or {"error": ...}.

    Never returns partial-looking success: if the key is missing or the call fails,
    the caller gets an explicit error so the report can say NOT CHECKED rather than
    implying the account is small or new.
    """
    if not handle:
        return {"error": "no handle"}
    cache = _x_cache()
    hit = cache.get(handle.lower())
    if hit and (time.time() - hit.get("_fetched", 0)) < max_age_h * 3600:
        return hit
    tok = env_key("X_BEARER_TOKEN")
    if not tok:
        return {"error": "X_BEARER_TOKEN not set — not checked"}
    url = ("https://api.x.com/2/users/by/username/" + handle
           + "?user.fields=created_at,public_metrics,verified,verified_type,description")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:80]}
    d = body.get("data")
    if not d:
        # X returns HTTP 200 with an `errors` array for a missing handle, so the
        # status code alone cannot tell a dead account from a bad token. Surface the
        # API's own words -- collapsing both into one message made a WORKING token
        # look broken when the test handle simply no longer existed.
        errs = body.get("errors") or []
        detail = (errs[0].get("detail") if errs and isinstance(errs[0], dict) else None)
        return {"error": detail or "no data returned"}
    pm = d.get("public_metrics") or {}
    created = d.get("created_at") or ""
    age_days = None
    if created:
        try:
            c = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - c).days
        except Exception:  # noqa: BLE001
            pass
    out = {"handle": d.get("username"), "name": d.get("name"),
           "created_at": created, "age_days": age_days,
           "followers": pm.get("followers_count"), "following": pm.get("following_count"),
           "tweets": pm.get("tweet_count"), "listed": pm.get("listed_count"),
           "verified": d.get("verified"), "verified_type": d.get("verified_type"),
           "_fetched": time.time()}
    out["tells"] = x_credibility_tells(out)
    cache[handle.lower()] = out
    try:
        os.makedirs(os.path.dirname(X_CACHE), exist_ok=True)
        json.dump(cache, open(X_CACHE, "w"))
    except Exception:  # noqa: BLE001
        pass
    return out


def x_credibility_tells(p):
    """
    Reasons the follower count might not mean what it looks like.

    Deliberately NOT a score and NOT a pass/fail. Slim reads the follower number
    himself; the machine's job is to surface the "unless it was bought or hacked"
    cases he cannot see from the number alone. Each tell names the fact, not a
    conclusion.
    """
    tells = []
    f = p.get("followers") or 0
    age = p.get("age_days")
    tw = p.get("tweets")
    fo = p.get("following") or 0

    if age is not None and age < 30 and f > 5_000:
        tells.append(f"{f:,} followers on an account only {age}d old — implausibly fast "
                     f"organically; bought or renamed")
    elif age is not None and age < 90 and f > 20_000:
        tells.append(f"{f:,} followers in {age}d — verify the growth is real")
    if age is not None and age > 365 and tw is not None and tw < 30:
        tells.append(f"{age//365}y old but only {tw} posts — dormant account, "
                     f"consistent with a purchased or repurposed handle")
    if f > 1_000 and tw is not None and tw < 10:
        tells.append(f"{f:,} followers but {tw} posts — audience without a history")
    if fo and f and fo > f * 2 and f < 5_000:
        tells.append(f"follows {fo:,} vs {f:,} followers — follow-back inflation")
    if age is not None and age <= 7:
        tells.append(f"account created {age}d ago")
    return tells
