#!/usr/bin/env python3
"""
Hood Sniper -- live launch monitor.

DISPLAY ONLY. No keys, no signing, no orders. It watches the chain and ranks
what it sees so entries are made on metrics instead of FOMO.

Layout
------
  ticker pane : every new curve as it is created (~8/min) -- compact, scrolling
  detail pane : graduations and anything clearing filters, with full metrics

Venues are pluggable: add an entry to VENUES with its topics and a decoder and
it joins the same feed. Pons is implemented; the others are stubbed with the
addresses already discovered in Phase 0.

Usage
-----
    python3 launch_monitor.py                 # live TUI
    python3 launch_monitor.py --no-tui        # plain log lines
    python3 launch_monitor.py --min-liq 5000  # hide sub-threshold graduations
"""
import argparse
import itertools
import json
import os
import select
import subprocess
import sys
import termios
import threading
import tty
import time
import urllib.error
import urllib.request
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor

# STACK DUMP ON DEMAND: kill -USR1 <pid> writes every thread's stack to stderr.
# This codebase has lost hours twice to a loop that stopped producing while the
# process stayed alive, and both times the answer came from thread stacks rather
# than from reading code. Cheap to install, impossible to add once it is stuck.
import faulthandler as _fh
import signal as _sig
try:
    _fh.register(_sig.SIGUSR1, all_threads=True, chain=False)
except Exception:  # noqa: BLE001
    pass
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
os.makedirs(DATA, exist_ok=True)
sys.path.insert(0, HERE)
try:
    from investigate import investigate as run_investigation
    from investigate import pool_state as shared_pool_state
except Exception:  # noqa: BLE001
    run_investigation = None
    shared_pool_state = None

WSS = "wss://robinhood-rpc.publicnode.com"
RPCS = ["https://rpc.mainnet.chain.robinhood.com",
        "https://robinhood-rpc.publicnode.com"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_rr = itertools.cycle(RPCS)
_RPC_FAILS = {u: 0 for u in RPCS}

ETH_USD = 2450.0
BLOCK_TIME = 0.101

try:
    from smart_money import SmartWatch
except Exception:  # noqa: BLE001
    SmartWatch = None
try:
    import executor as EXEC
except Exception:  # noqa: BLE001
    EXEC = None

# ---- event topics (all derived + verified in Phase 0) ---------------------
T_INITIALIZED = "0x908408e307fc569b417f6cbec5d5a06f44a0a505ac0479b47d421a4b2fd6a1e6"
T_CURVE_COMPLETED = "0xf8d37a90738ae063b8b8058b66f5880cf3cf7ab0c5d4fa78219696591dfbfb67"
T_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
T_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
T_EXEMPTED = "0xe4b7e48fbd47c2f602bacadee76ad33b16542ddb4997cfc0de04c311adcfa8c7"
T_SNIPE_CHARGED = "0x3bc39a5562b28f5fe8f36cecabfbaa12bb969acf05717994709225fc412a9934"
T_V4_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
T_V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

# Bankr == Doppler infrastructure. Its factory emits no events, so a launch is
# detected from a v4 Initialize whose `hooks` field is the Doppler hook.
# Verified: 8/8 tokens behind such events are DopplerERC20V1 clones.
DOPPLER_HOOK = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"
DOPPLER_IMPL = "3be8b97fd0e713b5abe0649fa830223b6b4bc599"

# o1 Launchpad: the factory announces each launch with token, poolId and
# creator in the topics -- richer than Bankr, which needs a hook filter.
O1_FACTORY = "0xce9c48cfa068947f77738c81be406b53338e5b0d"
T_O1_LAUNCH = "0x207384e895174175cc774fe7f7457b37c382f27ebf53d37d5257b862f80eaf9c"

SEL_TOKEN = "0xfc0c546a"        # token()
SEL_DEPLOYER = "0xd5f39488"     # deployer()
SEL_FEEBPS = "0x24a9d853"       # feeBps()
SEL_CREATORTAX = "0xc1bb8901"   # creatorTaxBps()
SEL_SYMBOL = "0x95d89b41"       # symbol()
SEL_NAME = "0x06fdde03"         # name()
SEL_GRADTHRESH = "0xd8a0b1b6"   # graduationThreshold()  (best-effort)
SEL_TRACKEDQUOTE = "0x0d1f4b0f" # trackedQuote()         (best-effort)

# ---- venue registry -------------------------------------------------------
# To add a venue: give it the topic that signals a NEW launch, the topic that
# signals GRADUATION (or None), and a label. Enrichment hooks are per-venue.
VENUES = OrderedDict([
    ("pons", {
        "label": "Pons",
        "new_topic": T_INITIALIZED,
        "grad_topic": T_CURVE_COMPLETED,
        "enabled": True,
        "note": "fully mapped; token bytecode is one factory template",
    }),
    ("bankr", {
        "label": "Bankr",
        "new_topic": T_V4_INITIALIZE,   # filtered by the Doppler hook in on_log
        "grad_topic": None,             # Doppler has NO graduation: launch == pool
        "enabled": True,
        "note": "Doppler infra; launch goes straight to a v4 pool, so a Bankr "
                "launch is immediately tradeable and lands in the detail pane",
    }),
    ("o1", {
        "label": "o1 Launchpad",
        "new_topic": T_O1_LAUNCH,   # emitted by RWAERC20LaunchpadFactory
        "grad_topic": None,         # no graduation: launch == tradeable pool
        "enabled": True,
        "note": "RWA launchpad; pairs a meme against its MATCHING real ticker "
                "(BND/BND, CCL/CCL). ~271 launches/day.",
    }),
    ("clanker", {
        "label": "Clanker",
        "new_topic": None,
        "grad_topic": None,
        "enabled": False,
        "note": "needs mapping",
    }),
    ("uniswap_v4", {
        "label": "UniV4 direct",
        "new_topic": T_V4_INITIALIZE,
        "grad_topic": None,
        "enabled": False,           # very noisy; arbitrary bytecode -> honeypot risk
        "note": "raw pool inits; honeypot checks REQUIRED before enabling",
    }),
])


# ---------------------------------------------------------------- transport
# ---------------------------------------------------------- endpoint health
# AN ENDPOINT CAN BE HALF-ALIVE, AND ROUND-ROBIN CANNOT SEE IT.
# Measured 2026-09-16: robinhood-rpc.publicnode.com answered eth_blockNumber and
# served the websocket stream normally while returning HTTP 403 to EVERY
# eth_getLogs -- 12/12 in a burst test, against 12/12 OK on the primary.
#
# Blind round-robin sent half of every scan's chunks there, and _logs_chunk turns
# a persistent failure into exponential work: retry, sleep 0.25s, then split the
# range and recurse to depth 6, so ONE bad chunk becomes up to 64 requests. A scan
# that costs 1.5s standalone blew past the 20s enrich timeout, and the table filled
# with SCAN-TIMEOUT and buyers=0 while every endpoint looked "up".
#
# So health is tracked PER METHOD: an endpoint that is fine for eth_call and dead
# for eth_getLogs is the case that actually happened.
_RPC_COOLDOWN = 180.0          # seconds an endpoint sits out after repeated failure
_RPC_TRIP = 3                  # consecutive failures before benching it
_rpc_health = {}               # (url, method) -> [consecutive_fails, benched_until]
_health_lock = threading.Lock()


def _endpoint_for(method):
    """Next endpoint not benched for this method. Never returns nothing: if every
    endpoint is benched we use the least-recently-benched one, because refusing to
    make the call is worse than making it to a flaky host."""
    now = time.time()
    with _health_lock:
        for _ in range(len(RPCS)):
            u = next(_rr)
            st = _rpc_health.get((u, method))
            if not st or st[1] <= now:
                return u
        return min(RPCS, key=lambda u: _rpc_health.get((u, method), [0, 0])[1])


def _mark(url, method, ok):
    with _health_lock:
        st = _rpc_health.setdefault((url, method), [0, 0.0])
        if ok:
            st[0] = 0
            st[1] = 0.0
        else:
            st[0] += 1
            if st[0] >= _RPC_TRIP:
                st[1] = time.time() + _RPC_COOLDOWN


def rpc_health():
    """Snapshot for the status line -- benched endpoints must be visible, not silent."""
    now = time.time()
    with _health_lock:
        return {f"{u.split('//')[-1][:22]}:{m}": round(st[1] - now)
                for (u, m), st in _rpc_health.items() if st[1] > now}


def rpc(method, params, tries=3, timeout=20):
    for attempt in range(tries):
        url = _endpoint_for(method)
        try:
            body = json.dumps({"jsonrpc": "2.0", "method": method,
                               "params": params, "id": 1}).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.load(r)
            # a JSON-RPC error body is still a live host; only transport failures
            # and 4xx/5xx count against an endpoint's health
            _mark(url, method, True)
            return out
        except Exception:  # noqa: BLE001
            _mark(url, method, False)
            time.sleep(0.4 * (attempt + 1))
    return {}


BATCH_STATS = {"calls": 0, "partial": 0, "failed": 0}


def rpc_batch(calls, timeout=30, tries=4):
    """
    Batched JSON-RPC that does NOT accept a partial response as success.

    THE BUG THIS REPLACES (the seventh instance of this shape in this project):
    the old body did `by.get(i)` over whatever came back, so any id the server
    omitted silently became None. A partial batch was indistinguishable from a
    complete one. Downstream, curve_metrics reads

        supply = call_int((res[0] or {}).get("result"))

    so a dropped entry became `supply = None`, which became "no market cap" -- and
    the whole table rendered mcap/liq/slippage as blank while the monitor reported
    itself perfectly healthy. The RH endpoints are INTERMITTENT rather than dead
    (measured: the primary answered 0/6 selectors on one pass and 6/6 a minute
    later), so round-robin meant a large fraction of enrichments quietly lost data.

    Now: a response missing ids is retried on the NEXT endpoint. Only after every
    try is the caller handed Nones, and that outcome is counted so it can be
    surfaced rather than mistaken for an empty chain.
    """
    payload = [{"jsonrpc": "2.0", "method": m, "params": p, "id": i}
               for i, (m, p) in enumerate(calls)]
    want = len(calls)
    BATCH_STATS["calls"] += 1
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                next(_rr), data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.load(r)
            if isinstance(out, dict):        # single error object, not a batch
                time.sleep(0.4)
                continue
            by = {o.get("id"): o for o in out if isinstance(o, dict)}
            # every id must be present AND carry a result; an `error` entry is a
            # real answer for that call, so it does not force a retry of the batch
            missing = [i for i in range(want)
                       if i not in by or ("result" not in by[i] and "error" not in by[i])]
            if missing:
                BATCH_STATS["partial"] += 1
                time.sleep(0.4 * (attempt + 1))
                continue                     # next(_rr) rotates to another endpoint
            return [by[i] for i in range(want)]
        except Exception:  # noqa: BLE001
            time.sleep(0.4 * (attempt + 1))
    BATCH_STATS["failed"] += 1
    return [None] * want


def call_addr(res):
    return ("0x" + res[-40:]).lower() if res and len(res) >= 66 else None


def call_int(res):
    try:
        return int(res, 16) if res and res != "0x" else None
    except (TypeError, ValueError):
        return None


def call_str(res):
    """Decode an ABI-encoded string return (offset, length, bytes)."""
    if not res or len(res) < 130:
        return None
    try:
        body = res[2:]
        ln = int(body[64:128], 16)
        raw = bytes.fromhex(body[128:128 + ln * 2])
        return raw.decode("utf-8", "replace").strip("\x00") or None
    except Exception:  # noqa: BLE001
        return None


def s256(h):
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


# ---------------------------------------------------------------- enrichment
PONS_TOKEN_CODELEN = 6498   # every Pons token is this one factory template


def load_registry():
    path = os.path.join(DATA, "dev_registry.json")
    if not os.path.exists(path):
        return {}, {}
    with open(path) as f:
        reg = json.load(f)
    c2d = {}
    for dev, v in reg.items():
        for c in v.get("curves", []):
            c2d[c] = dev
    return reg, c2d


def head_block():
    r = rpc("eth_blockNumber", [])
    return int(r["result"], 16) if "result" in r else 0


LOG_STATS = {"chunks": 0, "retries": 0, "dropped": 0}
# Persistent per-enrichment trace. Sampling 2 minutes and theorising is how the
# last round went wrong; this records EVERY attempt -- which branch it took, what
# the record held, how long it took, queue depth -- so the failure is observed
# rather than guessed at. Off unless HS_TRACE=1.
ENRICH_TRACE = bool(os.environ.get("HS_TRACE"))


# One shared, bounded pool for every parallel RPC in this module. Bounded because
# these are free public endpoints: past a handful of concurrent requests they start
# rate-limiting, which routes straight into the retry-and-split ladder below and
# ends up SLOWER than doing it serially. A shared pool also means total concurrency
# stays capped no matter how many workers call in at once.
RPC_WORKERS = int(os.environ.get("HS_RPC_WORKERS", "6"))
_rpc_pool = ThreadPoolExecutor(max_workers=RPC_WORKERS, thread_name_prefix="hs-rpc")

# DELIBERATELY A SECOND POOL, not a bigger first one.
# Outer tasks (an overlapped trade scan) call get_logs, which submits chunk tasks.
# Nested submission into the SAME bounded pool is a classic deadlock: if every
# worker is occupied by an outer task blocked on f.result(), no worker is left to
# run the chunks those futures depend on. Today only one thread starts outer tasks
# so it would not trigger, but that is a property of the current thread layout
# rather than of this code, and it would fail the moment a second enrich thread
# existed. Separate pools make the nesting safe by construction.
_outer_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hs-outer")
_stats_lock = threading.Lock()


def _bump(k, n=1):
    with _stats_lock:
        LOG_STATS[k] = LOG_STATS.get(k, 0) + n


def _logs_chunk(params, b, to, _depth=0):
    """
    ONE chunk, with the full retry -> split -> drop ladder.

    Unchanged in behaviour from the serial version, and deliberately so: this is
    the code that stopped eth_getLogs from lying, and it has been the source of
    four separate silent-truncation bugs in this project. Splitting stays
    SEQUENTIAL inside a chunk -- it is the rare path, and recursively submitting
    into the same bounded pool is how you deadlock it.
    """
    q = dict(params, fromBlock=hex(b), toBlock=hex(to))
    r = rpc("eth_getLogs", [q])
    _bump("chunks")
    if "result" in r:
        return r["result"]
    _bump("retries")                       # one straight retry — most are transient
    time.sleep(0.25)
    r = rpc("eth_getLogs", [q])
    if "result" in r:
        return r["result"]
    if to > b and _depth < 6:
        # still failing: split. "too many results" needs a narrower range, and
        # halving costs one extra call instead of losing the range.
        mid = b + (to - b) // 2
        return (_logs_chunk(params, b, mid, _depth + 1)
                + _logs_chunk(params, mid + 1, to, _depth + 1))
    _bump("dropped")                       # genuinely unreadable — do not hide it
    return []


def get_logs(params, max_width=40_000, _depth=0):
    """
    Chunked eth_getLogs that does NOT lie when a chunk fails, run in parallel.

    THE BUG THIS REPLACES: the original loop did `if "result" in r: out.extend(...)`
    then advanced the cursor UNCONDITIONALLY. A rate-limit, timeout or "too many
    results" silently skipped that block range and the caller got a PARTIAL list
    indistinguishable from a complete one. Symptom: 0 launches in a 5-minute window
    but 1,354 in a 67-minute window -- counts that cannot both be true.

    Chunks are now fetched concurrently, but results are reassembled BY INDEX, not
    by completion order, so the returned logs stay in block order. A worker that
    raises is counted as dropped rather than quietly contributing an empty list --
    an exception must not become a silent hole, which is the exact failure this
    function exists to prevent.

    A single-chunk range skips the pool entirely; after the window fix that is the
    common case and the executor round trip is pure overhead there.
    """
    lo = int(params["fromBlock"], 16)
    hi = int(params["toBlock"], 16)
    if hi < lo:
        return []
    ranges, b = [], lo
    while b <= hi:
        to = min(b + max_width, hi)
        ranges.append((b, to))
        b = to + 1
    if len(ranges) == 1:
        return _logs_chunk(params, ranges[0][0], ranges[0][1], _depth)

    results = [None] * len(ranges)
    futs = {}
    for i, (b0, t0) in enumerate(ranges):
        futs[_rpc_pool.submit(_logs_chunk, params, b0, t0, _depth)] = i
    for f in futs:
        i = futs[f]
        try:
            results[i] = f.result()
        except Exception:  # noqa: BLE001
            results[i] = []
            _bump("dropped")
    out = []
    for r in results:
        out.extend(r)
    return out


CURVE_HISTORY_MAX = 400_000       # only for curves whose creation we did not see
CURVE_HISTORY_STEP = 40_000


def curve_trade_logs(curve, block, head, pre_grad):
    """
    Every trade on one curve, scanning as little chain as possible.

    THE BUG THIS REPLACES: the window was [block - 400_000, block] -- 11.2 hours of
    chain ENDING at the launch block. Two separate failures in one line:

      * For a launch, almost all of that is BEFORE the contract existed. Measured
        on a live curve: 38.6s and 16 sequential getLogs chunks to return 15 logs.
        The same curve over the last 4k blocks costs 0.7s.
      * It ended AT the launch block, so every trade that happened AFTER launch --
        the data actually wanted -- fell outside it. That is why n_sellers and
        n_snipers were 0.0% across 544 recorded rows, and n_buyers came back 0-2.
        It also made retries pointless: the same fixed range was rescanned each
        time, so a retry could not learn anything the first attempt missed.

    Now the window is anchored at CREATION and always runs forward to head:

      * a pre-graduation row was created BY the launch event, so `block` IS the
        creation block and nothing can exist before it -- one narrow scan.
      * otherwise creation is unknown, so walk backwards a step at a time and stop
        at the first empty step rather than always paying for the full 400k.

    Scanning to `head` also means a retry sees trading that has arrived since.

    Caveat worth knowing: the backwards walk stops at the first empty step, so a
    curve that went completely silent for a whole step and then traded earlier
    would be truncated there. Bonding-curve activity is contiguous from creation,
    so that is not expected -- but it is an assumption, not a guarantee.
    """
    if head <= block:
        head = block
    if pre_grad:
        return get_logs({"fromBlock": hex(block), "toBlock": hex(head),
                         "address": curve})
    out = get_logs({"fromBlock": hex(block), "toBlock": hex(head), "address": curve})
    lo = block
    while block - lo < CURVE_HISTORY_MAX and lo > 0:
        nxt = max(0, lo - CURVE_HISTORY_STEP)
        chunk = get_logs({"fromBlock": hex(nxt), "toBlock": hex(max(nxt, lo - 1)),
                          "address": curve})
        if not chunk:
            break
        out = chunk + out
        lo = nxt
    return out


def tape_metrics(o, logs):
    """
    Everything derived from a curve's trade tape, in ONE place.

    Extracted so the rolling refresh and the first enrich cannot drift apart. They
    previously could not share it at all, which is why every buyer-derived column
    was frozen at discovery time: n_buyers, top1_share, the flow sparkline, the
    clip score and the bad-buyer share were all computed once, seconds after
    launch, and never recomputed.

    Measured cost of that: on 25 sampled curves the board understated the buyer
    count on 21 of them, median 10 buyers missed and max 79 -- one curve read 0 on
    screen while the chain showed 79. Callers pass a freshly scanned `logs` and
    this rewrites the derived fields in place.
    """
    buys, sells, exempt, snipers, quote_in = {}, {}, set(), set(), 0
    clips = []           # every buy's quote size, for the same-size-clip score
    # First-buy BLOCK per wallet. The trade scan already reads every buy log, so
    # this is free -- and it is the only way the flow sparkline survives a
    # restart. buy_accel used to read ONLY live-observed buys, so after every
    # restart self.buyflow was empty and the flow column stayed blank until three
    # fresh wallets happened to buy while the monitor was watching. The history
    # was right there in these logs and was being discarded.
    firsts = {}
    for lg in logs:
        t0 = lg["topics"][0]
        if t0 == T_CURVE_BUY and len(lg["topics"]) > 1:
            a = "0x" + lg["topics"][1][-40:]
            b = int(lg["blockNumber"], 16)
            if a not in firsts or b < firsts[a]:
                firsts[a] = b
            d = lg["data"][2:]
            if len(d) >= 128:
                q = int(d[0:64], 16)
                quote_in += q
                clips.append(q)
                buys[a] = buys.get(a, 0) + int(d[64:128], 16)
        elif t0 == T_CURVE_SELL and len(lg["topics"]) > 1:
            a = "0x" + lg["topics"][1][-40:]
            d = lg["data"][2:]
            if len(d) >= 64:
                sells[a] = sells.get(a, 0) + int(d[0:64], 16)
        elif t0 == T_EXEMPTED and len(lg["topics"]) > 1:
            exempt.add("0x" + lg["topics"][1][-40:])
        elif t0 == T_SNIPE_CHARGED and len(lg["topics"]) > 1:
            snipers.add("0x" + lg["topics"][1][-40:])
    tot = sum(buys.values()) or 1
    top = sorted(buys.values(), reverse=True)
    o["n_buyers"] = len(buys)
    o["n_sellers"] = len(sells)
    o["n_exempt"] = len(exempt)
    o["n_snipers"] = len(snipers)
    o["top1_share"] = top[0] / tot if top else 0.0
    o["curve_volume_eth"] = quote_in / 1e18
    o["curve_volume_usd"] = quote_in / 1e18 * ETH_USD
    o["exempt_sold"] = sum(1 for a in exempt if sells.get(a, 0) > 0)
    o["buy_firsts"] = firsts

    # --- SAME-SIZE-CLIP SCORE ---------------------------------------------
    # Share of buys arriving in near-identical sized clips. Measured on 1,132
    # cached curve tapes against a 5.65% base graduation rate:
    #
    #     dup share   n     graduated   lift
    #       <5%      136      0.00%     0.00x   <- 136 curves, ZERO graduations
    #        5-20%   324      1.54%     0.21x
    #       20-40%   180      3.89%     0.54x
    #       40-60%   179     19.55%     2.70x
    #       >60%      64     26.56%     3.66x
    #
    # Monotonic. NOTE THE SIGN: the published playbooks treat
    # a high bundle share as a reject. On this venue it is the BEST cohort --
    # a coordinated buyer is what actually walks a curve to graduation.
    #
    # Controlled for trade count, because dup share could merely proxy activity.
    # It does not -- within 100-399 buys it is 1.61% vs 21.09%, and within 400+
    # it is 3.64% vs 31.08%. The two are independent signals.
    #
    # This predicts REACHING GRADUATION, not profit. The same coordinated buyer
    # dumping afterwards is consistent with 93.6% of graduated tokens falling to
    # 0.70, so it belongs on the entry decision only.
    # MINIMUM 15 BUYS, AND THE REASON MATTERS MORE THAN THE NUMBER.
    # Lift by minimum, measured on the tapes (FLAT-CLIPS cohort vs base):
    #     MIN= 8  n=566  0.45x       MIN=20  n=261  0.12x
    #     MIN=12  n=426  0.27x       MIN=30  n=255  0.16x
    #     MIN=15  n=334  0.14x   <- best discrimination at the lowest cost
    #
    # THIS FLAG CANNOT INFORM THE ENTRY DECISION. At the 9.3%-of-supply entry
    # point the median curve has had just 3 buys (p90 = 5), so the score is not
    # computable when the buy decision is actually made. It becomes readable
    # only as a curve matures, which makes it a hold/monitor signal rather than
    # an entry one. It was originally gated at 30, which never fired at all --
    # live rows top out around 27 buyers.
    n_clips = len(clips)
    if n_clips >= 15:
        # ROUND to 3 significant figures -- do NOT truncate the decimal string.
        # str(q)[:3] buckets 10000000000000000 and 10000000000047514 together,
        # which scored a set of 120 all-different buys as 100% duplicates. The
        # flag would have fired on exactly the curves it is meant to exclude.
        cc = {}
        for q in clips:
            e = len(str(q)) - 3
            k = round(q, -e) if e > 0 else q
            cc[k] = cc.get(k, 0) + 1
        dup = sum(v for v in cc.values() if v >= 3)
        share = dup / n_clips
        o["clip_dup_share"] = share
        o["n_clips"] = n_clips
        # CLIPPED at the same 15-buy minimum: 11.04% vs a 6.38% base (1.73x,
        # n=154). Weaker than the 2.7-3.7x seen on fully mature curves, because
        # only the first ~45 buys are visible this early -- report the number
        # that matches when the flag actually fires, not the flattering one.
        if share >= 0.40 and n_clips >= 15:
            o["good"].append(f"CLIPPED {100*share:.0f}%")     # 3.4x
        elif share < 0.05:
            o["flags"].append(f"FLAT-CLIPS {100*share:.0f}%")  # 0.06x
    # ----------------------------------------------------------------------

    # --- DIRTY-BUYERS -----------------------------------------------------
    # Share of the EARLIEST buyers that sit in the bad-wallet index. Uses
    # `firsts`, which already holds each wallet's first-buy block, so the
    # ordering is real rather than log order.
    # TIMING LIMITATION, MEASURED -- READ BEFORE TRUSTING THIS FLAG.
    # enrich_curve runs the moment a curve is discovered, when it has 0-1 buyers
    # (live rows: p50=0, p90=1). The validated statistic needs >=3 SCORED wallets
    # among the first 20 buyers, so at enrich time it almost never computes:
    # 0 of 2,995 live rows over a six-minute run.
    #
    # The buyers do arrive -- rescanning those same curves minutes later finds
    # 5-41 buyers with 4 of them in the index -- but the row is enriched once and
    # then left alone, so the flag is evaluated against an empty crowd and stays
    # silent. It fires on re-vet [R], which rescans, and on the minority of
    # curves already trading when first seen.
    #
    # The fix is to recompute as a curve matures rather than only at discovery;
    # until then treat a missing bad_buyer_share as "not yet measurable", never
    # as a clean crowd. Same root cause as the clip score: we look too early.
    scored, bad_cut = bad_wallets()
    if scored and firsts:
        early = [w for w, _b in sorted(firsts.items(), key=lambda kv: kv[1])[:20]]
        known = [w for w in early if w in scored]
        # DENOMINATOR IS SCORED WALLETS, NOT ALL EARLY BUYERS. The validated
        # buckets are bad/scored; dividing by every early buyer instead gives a
        # systematically smaller ratio that matches no measured bucket.
        #
        # Fewer than 3 scored wallets is NO INFORMATION, not a clean bill. An
        # unknown crowd is exactly the case this must stay silent on.
        if len(known) >= 3:
            share = sum(1 for w in known if scored[w] <= bad_cut) / len(known)
            o["bad_buyer_share"] = share
            o["n_known_buyers"] = len(known)
            if share >= 0.35:
                o["flags"].append(f"DIRTY-BUYERS {100*share:.0f}%")
            elif share < 0.15:
                o["good"].append("CLEAN-BUYERS")
    # ----------------------------------------------------------------------

    # kill flag 1 -- exactly one exempt address (0.2% vs 2.24% base)
    if o["n_exempt"] == 1:
        o["flags"].append("SOLO-EXEMPT")
    elif o["n_exempt"] >= 11:
        o["good"].append(f"{o['n_exempt']} exempt")     # 11.1%, 5.0x

    # kill flag 2 -- top-1 buyer holding 50-80%. Above 80% is the launch
    # artifact (first buyer holds everything) and is NOT flagged, which the
    # old blanket ">50%" rule got wrong: it fired hardest on brand-new curves
    # where one buyer trivially owns 100% of one trade.
    t1 = o["top1_share"]
    if 0.50 <= t1 < 0.80 and o["n_buyers"] >= 3:
        o["flags"].append(f"CONCENTRATED {100*t1:.0f}%")
    elif t1 < 0.25 and o["n_buyers"] >= 5:
        o["good"].append("SPREAD")                      # <25% is the best band
    if o["n_snipers"] > 0:
        o["flags"].append(f"SNIPED x{o['n_snipers']}")
    if o["exempt_sold"] > 0:
        o["flags"].append("INSIDER-SELL")

def enrich_curve(curve, block, reg, c2d, stake_usd, head=None, pre_grad=False):
    """Everything the screen needs about one curve, as cheaply as possible."""
    o = {"curve": curve, "block": block, "venue": "pons", "flags": [], "good": []}

    # The trade scan needs only (curve, block, head) -- all known right now -- while
    # the token reads below cannot start until the first batch returns the token
    # address. Profiled on live launches, the scan was 64% of enrich and the token
    # batch another 31%, and they were running one after the other for no reason.
    # Starting the scan here overlaps the two, so enrich costs about what the scan
    # alone costs.
    scan = _outer_pool.submit(curve_trade_logs, curve, block,
                              head or head_block(), pre_grad)

    res = rpc_batch([
        ("eth_call", [{"to": curve, "data": SEL_TOKEN}, "latest"]),
        ("eth_call", [{"to": curve, "data": SEL_DEPLOYER}, "latest"]),
        ("eth_call", [{"to": curve, "data": SEL_FEEBPS}, "latest"]),
        ("eth_call", [{"to": curve, "data": SEL_CREATORTAX}, "latest"]),
    ])
    g = lambda i: (res[i] or {}).get("result")  # noqa: E731
    token = call_addr(g(0))
    dev = call_addr(g(1))
    fee_bps = call_int(g(2))
    tax_bps = call_int(g(3))
    o.update({"token": token, "dev": dev, "fee_bps": fee_bps, "tax_bps": tax_bps})
    o["total_fee_bps"] = (fee_bps or 0) + (tax_bps or 0)

    if token:
        r2 = rpc_batch([
            ("eth_call", [{"to": token, "data": SEL_SYMBOL}, "latest"]),
            ("eth_call", [{"to": token, "data": SEL_NAME}, "latest"]),
            ("eth_getCode", [token, "latest"]),
        ])
        o["symbol"] = call_str((r2[0] or {}).get("result")) or "?"
        o["name"] = call_str((r2[1] or {}).get("result")) or ""
        code = (r2[2] or {}).get("result") or ""
        o["code_len"] = len(code)
        # Pons honeypot check is structural: one factory template. A different
        # code length means it is NOT the standard token -> treat as unknown.
        o["standard_token"] = (len(code) == PONS_TOKEN_CODELEN)
        if not o["standard_token"]:
            o["flags"].append("NONSTD-CODE")

    # dev track record (point in time)
    if dev:
        v = reg.get(dev)
        if v:
            prior = sum(1 for b in v.get("grad_blocks", []) if b < block)
            o["dev_launches"] = v.get("launches", 0)
            o["dev_prior_grads"] = prior
            if prior >= 1:
                o["good"].append(f"DEV {prior}G")
            # SERIAL LAUNCHING IS A PENALTY, NOT A CREDENTIAL -- measured on 16,173
            # curves with a known dev against a 9.5% base rate for reaching the
            # heating threshold:
            #     1 launch   11.6%   +2.2
            #     2           7.2%   -2.3
            #     3-4         6.2%   -3.2
            #     5-9         7.6%   -1.9
            #     10+         2.8%   -6.7
            # Monotonic, and a 4x gap end to end. Inspecting the repeat "winners"
            # shows why: they are farms cycling variants (AMZN/MSFT/NVDA/TSLA,
            # BILLY/LARRY/TAMMY/TOMMY), so their hits are volume rather than skill.
            #
            # This is the OPPOSITE of the Phase 0 deployer-reputation thesis, which
            # looked for proven devs to follow. There is signal in the deployer; it
            # just runs the other way.
            nl = v.get("launches", 0)
            if nl >= 10:
                o["flags"].append(f"SERIAL {nl}")
            elif nl >= 3:
                o["flags"].append(f"serial {nl}")
            if nl >= 100 and prior == 0:
                o["flags"].append("BOT-DEV")
        else:
            o["dev_launches"] = 0
            o["dev_prior_grads"] = 0
            # A DEV WE HAVE NEVER SEEN IS THE BEST BUCKET, not a warning. One-off
            # devs hit 11.6% vs 2.8% for serial ones, so "NEW-DEV" was flagging the
            # single most favourable case as a risk.
            o["good"].append("FIRST LAUNCH")

    # --- MEASURED KILL FLAGS ---------------------------------------------------
    # Two states that are strongly associated with a curve going nowhere. Both are
    # computed from data already on the row; neither cost an extra call.
    #
    # 1. EXACTLY ONE EXEMPT ADDRESS. Rate of reaching near-graduation, by exempt
    #    count (n=38,153 curves, 2.24% base):
    #        0 exempts    6.6%   2.9x
    #        1 exempt     0.2%   0.1x   <- 33x worse than zero, on 18,390 curves
    #        2            0.4%
    #        3-5          1.1%
    #        11+         11.1%   5.0x
    #    Non-monotonic, and the single-exempt case is the worst state on the board.
    #    It reads as the dev exempting themselves, where many exempts look like a
    #    real distribution. Found independently by pattern_miner.py AND by comparing
    #    runners against stalls, which is why it is trusted enough to flag.
    #
    # 2. TOP-1 BUYER HOLDING 50-80% OF BUYS (n=3,426):
    #        <10%      4.6%      10-25%   8.5%
    #        25-50%    1.5%      50-80%   0.9%   <- 3.6x worse than base
    #        80-100%   3.0%  (a launch artifact: the first buyer trivially holds all)
    #
    # These are FLAGS, not blocks. They are in-sample associations on one window and
    # have not been forward-tested, so they inform the operator rather than gate a
    # buy -- the same standard applied to the wallet-vetting signals.
    
    try:
        # a scan that raised must not read as "no trades" -- that is the silent-hole
        # failure this codebase keeps re-learning, so it is surfaced as a flag
        try:
            # TIMEOUT IS MANDATORY. An unbounded .result() hung the enrich worker
            # permanently: one row popped, the future never resolved, and the queue
            # grew without limit while every row stayed "enriching" forever. Caught
            # by dumping thread stacks -- the worker was parked in futures wait.
            #
            # A missing trade scan costs some columns; a hung worker costs the
            # entire table. Twenty seconds is far above the measured p90 for this
            # scan (single-chunk reads finish well under a second).
            logs = scan.result(timeout=20)
        except Exception:  # noqa: BLE001
            logs = []
            o["flags"].append("SCAN-TIMEOUT")
            scan.cancel()
        tape_metrics(o, logs)
    except Exception:  # noqa: BLE001
        pass

    # fee drag -- matters most on a small bankroll
    if o["total_fee_bps"] >= 200:
        o["flags"].append(f"FEE {o['total_fee_bps']/100:.1f}%")
    elif o["total_fee_bps"] and o["total_fee_bps"] <= 100:
        o["good"].append("LOW-FEE")
    return o


def pool_exists_untraded(token, block, window=6000):
    """Was a v4 pool initialized for this token, even with no swaps yet?"""
    if not token:
        return False
    tt = "0x" + "0" * 24 + token[2:]
    for slot in (3, 2):
        tp = [T_V4_INITIALIZE, None, None, None][:slot + 1]
        tp[slot] = tt
        if get_logs({"fromBlock": hex(max(0, block - 300)),
                     "toBlock": hex(block + window),
                     "address": POOL_MANAGER, "topics": tp}):
            return True
    return False


def enrich_pool(token, block, stake_usd, window=6000):
    """
    Delegates to investigate.pool_state -- the single source of truth for
    orientation and quote pricing. This used to be a private copy that missed
    both fixes and reported a $22k pool as $231M.
    """
    if not token or shared_pool_state is None:
        return {}
    head = head_block()
    st = shared_pool_state(token, max(0, block - 300), min(head, block + window),
                           head, stake_usd)
    if not st:
        return {}
    out = {"pool_id": st.get("pool_id"), "n_swaps": st.get("n_swaps", 0),
           "quote_symbol": st.get("quote_symbol"),
           # carried so live swap decoding can orient itself instead of assuming --
           # the assumption that corrupted 13.5% of the RH paper trades
           "token_is_currency1": st.get("token_is_currency1"),
           "slippage_pct": (round(st["slippage_pct"], 4)
                            if st.get("slippage_pct") is not None else None),
           "active_liq_usd": st.get("active_liq_usd")}
    if st.get("pool_init_block") is not None:
        out["pool_lag_blocks"] = st["pool_init_block"] - block
    return out


# --- curve-native market metrics --------------------------------------------
# WHY THIS EXISTS. mcap / vol1h / holders came only from DexScreener, and liq /
# slip% came only from a Uniswap v4 pool. Both sources are EMPTY for the rows this
# bot actually watches:
#
#   * DexScreener indexed 0 of the 12 most recent RH launches (measured 2026-09-09).
#     By the time it indexes a token the snipe window is over.
#   * A Pons bonding curve has no v4 pool at all, so `enrich_pool` is skipped by
#     design for every pre-graduation row -- which is the whole default view.
#
# Measured fill rates in the feed journal before this: mcap 0/432, liq 30/432.
# The columns were not flickering, they were never populated.
#
# The curve itself carries all three, and answers at the launch second. Prices come
# from simulating the REAL buy() against the REAL curve with an eth_call state
# override for the caller's balance -- verified supported on both RH endpoints. That
# is a transactable price, not a mid, so it already contains fees and creator tax.
CURVE_PROBE_USD = 0.10

# ------------------------------------------------------------- volume floor
# A DEAD CHART IS NOT A CANDIDATE. Measured on 1,405 live rows, rate of reaching
# near-graduation by the volume actually shown on the board:
#       <$100      n=560   2.7%     <- 40% of every row on screen
#       $100-1k    n=342   5.0%
#       $1k-10k    n=326  14.4%
#       >$10k      n=177  44.6%
# A 16.5x spread, and the largest single bucket was the worst one. The board was
# mostly tokens nobody had traded, which is exactly what it looked like.
#
# $250 WAS TOO LOW -- roughly one small buy, and the board still read as dead.
# Re-measured on 1,667 curves, near-graduation rate against rows kept:
#       floor      kept   near-grad
#       $0         100%     11.5%
#       $250        50%     19.6%   <- previous default, barely moved the needle
#       $2,000      29%     28.9%   <- operator's call, and the right shape
#       $10,000     13%     41.3%
#
# $2,000 is the default: it roughly 2.5x's the base rate while still leaving
# under a third of the board, so the table stays populated. Above that the gain
# per row discarded flattens, and below it the dead charts come back.
# --min-vol overrides, 0 disables, and the header always states what is in force,
# because a filter that silently hides rows is indistinguishable from a dead feed.
MIN_VOL_USD = 2000.0

# DISTINCT BUYERS, NOT JUST DOLLARS. Volume is one number and one wallet can post
# any volume it likes; a wallet can only be NEW once, so buyer COUNT is the part
# that is expensive to fake. Measured on 3,321 curves (base near-grad 10.2%):
#
#     filter                      kept   precision   recall
#     volume >= $2,000             895      26.4%     69.6%
#     buyers >= 10                 734      27.8%     60.2%
#     buyers >= 20                 425      36.9%     46.3%
#     vol>=2k AND buyers>=10       558      34.6%     56.9%   <- this
#
# Buyer count alone beats the volume floor, and the two together beat either. The
# pair was chosen over buyers>=20 (36.9% precision) because that drops recall to
# 46% -- it would hide more than half of everything that goes on to run.
#
# This only became usable once refresh_worker existed. Buyer counts used to be
# frozen at discovery, when a curve genuinely has 0-1 buyers, so a gate on them
# would have hidden the entire board.
MIN_BUYERS = 10

# ---------------------------------------------------------- bad-wallet index
# Wallets whose presence in the first buys predicts a token goes nowhere. Built by
# scripts/bad_wallets.py and validated walk-forward on 23,557 tokens the scoring
# never saw (share of first 20 buyers that are known-bad -> rate of reaching 2x):
#     <15%  54.0%      35-60%  17.8%
#     15-35% 41.8%     >60%     5.7%
# Monotonic; a 9.5x spread. Survives controlling for token activity. See that file.
_BAD_WALLETS = None


def bad_wallets():
    """
    Lazy-load (scored, cutoff). A missing index yields ({}, 0.0) -- the flag goes
    quiet, never crashes, and MUST NOT be read as 'no bad wallets present'.
    """
    global _BAD_WALLETS
    if _BAD_WALLETS is None:
        try:
            import bad_wallets as _bw
            _BAD_WALLETS = _bw.load()
        except Exception:  # noqa: BLE001
            _BAD_WALLETS = ({}, 0.0)
    return _BAD_WALLETS

# ---------------------------------------------------------------- curve progress
# Every Pons curve sells the same 714,285,714 tokens before graduating (the other
# 285,714,285 are reserved for the LP). Verified constant on every graduated curve
# sampled, so progress is uniform across curves and -- unlike quote raised -- needs
# no decimals and no quote lookup. That matters: the quote is native ETH on only
# 62.9% of curves, the rest are USDG (6 dec) and tokenized equities (GOOGL, NVDA,
# SPY, SPCX), each with its own graduation target.
# exact value returned by sellableTokens() on an untouched curve -- do not round
# it to 714_285_714e18, that is 4e-10 low and makes a brand-new curve compute as
# fractionally negative progress
CURVE_SUPPLY = 714285714285714285714285715

# HOW FAR UP THE CURVE IS TOO FAR. Measured by replaying 1,173 cached tapes
# (64 graduations, 5.5%) -- see scripts/curve_replay.py:
#
#     entry at   net mean   profit factor
#       10%       1.309x        2.79
#       15%       1.234x        2.06
#       25%       1.099x        1.32
#       40%       0.896x        0.77   <- loses
#
# Monotonic, and it crosses into losing between 25% and 40%. The reason is
# structural, not statistical: the bonding curve is flat at the bottom and convex
# at the top, so entering at 10% leaves the floor 9% below and graduation 9x above,
# while entering at 40% leaves the floor 50% below and graduation only 5x above.
#
# THIS IS A CEILING, NOT A FLOOR. A $300-liquidity curve is not noise -- graduation
# is ~$10,290 of raise, so $300 IS the early entry. The dangerous rows are the
# expensive-looking ones: $800 of liquidity is already 25% up the curve.
CURVE_LATE_PCT = 0.25

# Round-trip price impact measured off the tapes is ~1.6x your share of the pot,
# so a flat $25 into a $300 curve costs 13.3% and eats most of the edge; $40 turns
# the trade negative outright. Size as a share of the pot instead.
#     stake at $300 pot:  $6 -> +$0.91 EV,  $15 -> +$1.40,  $25 -> +$0.71,  $40 -> -$2.75
POT_PCT = 0.05
MIN_STAKE_USD = 3.0


def curve_progress(e):
    """
    Fraction of sellable supply already bought, from the live sellableTokens()
    read. Returns None when unknown -- callers must NOT treat that as 0.0, which
    would make an unreadable curve look like a brand-new one.
    """
    s = e.get("curve_sellable")
    if s is None or s <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - s / CURVE_SUPPLY))


def stake_for(e, base_stake):
    """
    Position size capped at POT_PCT of the curve's own exit liquidity.

    A flat stake is wrong at both ends: too large on a $300 curve (where it is
    8.3% of the pot and costs 13.3% round trip) and needlessly timid on a deep
    one. Falls back to the flat stake when liquidity is unknown rather than
    guessing a size from a number we do not have.
    """
    liq = e.get("active_liq_usd")
    if not liq or liq <= 0:
        return base_stake, None
    sized = max(MIN_STAKE_USD, min(base_stake, POT_PCT * liq))
    return sized, 100.0 * sized / liq
SEL_BUY_CURVE = "59a87bc1"        # buy(uint256 wei, uint256 minOut, address to)
SEL_SELLABLE = "0x808bcddc"       # sellableTokens()
SEL_LAUNCHSUPPLY = "0x3f7ed6b7"   # launchSupply()
PROBE_WHO = "0x1111111111111111111111111111111111111111"
PROBE_OV = {PROBE_WHO: {"balance": "0x56bc75e2d63100000"}}      # 100 ETH, read-only


def _u(n):
    return hex(n & ((1 << 256) - 1))[2:].rjust(64, "0")


def _sim_buy(curve, wei):
    """
    tokensOut for a buy of `wei`, or None if the buy REVERTS.

    None here is load-bearing: a curve whose buy() reverts at any size is not a
    tradeable launch, and that is a far stronger signal than any market metric.
    Measured on live curves -- pristine curves (untouched token balance, zero ETH)
    revert at every size including $0.10, while traded ones fill at all of them.

    If an endpoint ignored the override it would fail with "insufficient funds"
    rather than silently returning a wrong number, so a bad node cannot fake a fill.
    """
    r = rpc("eth_call", [{"from": PROBE_WHO, "to": curve, "value": hex(wei),
                          "data": "0x" + SEL_BUY_CURVE + _u(wei) + _u(0)
                                  + "0" * 24 + PROBE_WHO[2:]},
                         "latest", PROBE_OV])
    return call_int(r.get("result")) if "result" in r else None


def curve_metrics(curve, stake_usd, eth_usd=ETH_USD):
    """
    mcap / liquidity / slippage read off the bonding curve. {} when unreadable.

    Slippage is measured, not modelled: fill price for `stake_usd` against fill
    price for a $0.10 dust probe on the same curve in the same block. That is the
    cost of the size you actually intend to send.
    """
    if not curve:
        return {}
    out = {}
    res = rpc_batch([("eth_call", [{"to": curve, "data": SEL_LAUNCHSUPPLY}, "latest"]),
                     ("eth_call", [{"to": curve, "data": SEL_SELLABLE}, "latest"]),
                     ("eth_getBalance", [curve, "latest"])])
    supply = call_int((res[0] or {}).get("result"))
    sellable = call_int((res[1] or {}).get("result"))
    eth_bal = call_int((res[2] or {}).get("result"))

    # The pot behind the curve IS its exit liquidity -- it is what every holder is
    # selling back into. (An older note in exit_manager claims eth_getBalance is 0
    # for every curve; that is not what live curves return, traded ones hold real
    # balances. Kept as a fact, not inherited as an assumption.)
    if eth_bal is not None:
        out["active_liq_usd"] = eth_bal / 1e18 * eth_usd
    if sellable is not None:
        out["curve_sellable"] = sellable
        # how far up the curve this already is -- the single strongest predictor
        # of whether the trade is still worth taking (see CURVE_LATE_PCT)
        prog = curve_progress({"curve_sellable": sellable})
        if prog is not None:
            out["curve_progress"] = prog

    dust_wei = max(int(CURVE_PROBE_USD / eth_usd * 1e18), 1)
    dust_out = _sim_buy(curve, dust_wei)
    if not dust_out:
        out["curve_tradeable"] = False       # buy() reverts -- not a live market
        return out
    out["curve_tradeable"] = True
    price_eth = dust_wei / dust_out
    out["price_usd"] = price_eth * eth_usd
    if supply:
        out["mcap"] = out["price_usd"] * (supply / 1e18)

    stake_wei = max(int(stake_usd / eth_usd * 1e18), 1)
    stake_out = _sim_buy(curve, stake_wei)
    if stake_out:
        eff = stake_wei / stake_out
        out["slippage_pct"] = max(0.0, (eff / price_eth - 1) * 100)
        out["curve_fill_tokens"] = stake_out
    return out


# ---------------------------------------------------------------- state
class Monitor:
    def __init__(self, args):
        self.args = args
        self.reg, self.c2d = load_registry()
        self.ticker = deque(maxlen=400)      # recent new curves
        self.detail = OrderedDict()          # curve -> enriched graduation
        self.q = deque()                     # enrichment queue
        # RLock, NOT Lock. Several paths legitimately re-enter: _evict() runs while
        # the caller already holds the lock and needs is_moving(), which locks too.
        # With a plain Lock that is a self-deadlock -- the monitor kept running and
        # silently stopped writing rows. It was invisible in tests because the test
        # harness constructed the Monitor with an RLock, so the very thing that
        # breaks in production could not break in the test.
        self.lock = threading.RLock()
        self.stats = {"new": 0, "grads": 0, "enriched": 0, "started": time.time()}
        self.journal = os.path.join(DATA, "monitor_feed.jsonl")
        self.sel = 0                 # highlighted row in the detail pane
        self.sel_curve = None        # selection ANCHOR — see _sync_sel()
        self.last_key = ""           # shown in the footer so input is visible
        self.inv = {}                # curve -> {"state","probes","ctx"}
        self.inv_for = None          # curve currently shown in the probe panel
        self.retry = []              # [(due_ts, curve)] re-enrichment backlog
        self.watch = {w.strip().upper() for w in (args.watch or "").split(",") if w.strip()}
        self.watch_norm = {Monitor._norm(w) for w in self.watch}
        self.hits = []               # watchlist matches, newest first
        # Default to MOVING. At ~21 launches/min with 58% never bought at all, ALL
        # is mostly corpses. Nothing is lost: this is a view filter over live tape
        # and [f] cycles back to ALL in one keypress.
        # Default to PRIME: clean of kill flags AND past heating. 21.7% of these
        # reach near-graduation vs 3.33% for the raw feed, at ~4 rows/hour. ALL is
        # one keypress away and nothing is discarded -- this is a view, not a filter
        # on the data.
        self.only_tradeable = 5        # 0=all 1=tradeable 2=actionable 3=hot 4=moving 5=prime
        # Venue filter. Pons is the priority venue; Bankr/o1 are occasional, so
        # they must be dismissable without losing them entirely.
        self.venue_modes = ["pons", "all", "bankr", "o1"]
        self.venue_i = 0 if (args.venues or "pons") == "pons" else \
            self.venue_modes.index(args.venues) if args.venues in self.venue_modes else 1
        self.running = True
        # 0 means "never heard from the stream", which correctly reads as silent
        # and lets the poller take over immediately on a dead-WS start
        self.ws_last_msg = 0.0
        # the one validated signal -- see smart_money.py for what was tested
        self.smart = SmartWatch() if SmartWatch else None
        self.autovet_q = deque()       # curves awaiting background vetting
        self.autovet_done = set()
        self.arm = getattr(args, "arm", False)
        self.pending = None            # a built-but-unsent buy awaiting [y]
        self.smart_alerts = []         # 2+ validated wallets in one token
        self.hot_q = deque()           # tokens that crossed HOT_AT, awaiting auto-arm
        self.hot_curve = None          # what the flashing banner is about
        self.gasband = {}              # live cost of actually trading right now
        self.last_head = 0             # for the age column
        self.confirm_quit = False
        self.show_help = False
        self.hot_since = 0.0
        # curve -> first time it crossed HOT_AT. `hot_curve` alone kept only the
        # MOST RECENT one, so when three tokens went hot in the same minute the
        # first two were silently overwritten and never shown. Smart wallets move
        # in clusters, so simultaneous HOTs are the normal case, not the edge one.
        self.hot_active = OrderedDict()
        self.crossed = {}              # curve -> set of thresholds already journalled
        self.pool2tok = {}             # v4 poolId -> {token, curve, symbol, c1}
        self.runner = {}               # token -> live post-graduation trade state
        self.curveflow = {}            # curve -> live pre-graduation tape
        self.hot_meta = {}             # curve -> {symbol, token} for tokens not yet in the table
        self.buyflow = {}              # curve -> {wallet: [n_buys, quote_wei, first_ts]}
        self.sort_modes = ["time", "accel", "smart", "mcap"]
        self.sort_i = 0
        self.acct = {}                 # wallet / balance / caps / realised P&L
        self.buyflow = {}              # curve -> {wallet: [count, quote_wei]}
        self.msg = ""                  # one-line status shown under the table

    # ---- visible rows, newest first (what the selection indexes into) ----
    def _sync_sel(self, rows):
        """
        Keep the highlight on the same TOKEN as new launches arrive.

        Rows render newest-first and the list grows at the FRONT — at ~14
        launches/min a new row lands every few seconds and shifts everything down
        one. Anchoring the highlight to a row INDEX meant pressing k (-1) was
        cancelled out by the next arrival (+1), so moving up looked completely
        broken while moving down appeared to jump two. Anchor on the curve
        address instead; the index is derived.
        """
        if self.sel_curve:
            for i, e in enumerate(rows):
                if e.get("curve") == self.sel_curve:
                    self.sel = i
                    return
        # anchor lost (row aged out / filtered away) — clamp and re-anchor
        self.sel = max(0, min(self.sel, len(rows) - 1)) if rows else 0
        self.sel_curve = rows[self.sel].get("curve") if rows else None

    def move_sel(self, delta):
        rows = self.rows()
        if not rows:
            return
        self._sync_sel(rows)
        self.sel = max(0, min(self.sel + delta, len(rows) - 1))
        self.sel_curve = rows[self.sel].get("curve")

    SPARK = "▁▂▃▄▅▆▇█"

    def buy_accel(self, curve, bucket=20.0, n=8):
        """
        NEW distinct buyers per bucket, newest last.

        Volume is the wrong thing to watch for acceleration: one wallet can post
        any volume it likes. A wallet can only be NEW once, so the rate of first-
        time buyers is the part that is expensive to fake. Returns
        (buckets, new_recent, new_previous, sparkline, mature) or None.

        `mature` is False until the curve has existed for the WHOLE window. Without
        it every brand-new launch reads as "accelerating": its entire life fits in
        the recent half, so recent/prev is always n/0 and the column would flag green
        on every fresh token -- precisely when the flag would be acted on. A shape
        can be shown before it means anything; a verdict cannot.
        """
        bf = self.buyflow.get(curve)
        if not bf:
            return None
        now = time.time()
        firsts = [v[2] for v in bf.values() if len(v) > 2]
        if len(firsts) < 3:
            return None
        mature = (now - min(firsts)) >= bucket * n
        buckets = [0] * n
        for t in firsts:
            age = now - t
            idx = n - 1 - int(age // bucket)
            if 0 <= idx < n:
                buckets[idx] += 1
        half = n // 2
        recent, prev = sum(buckets[half:]), sum(buckets[:half])
        hi = max(buckets) or 1
        spark = "".join(self.SPARK[min(len(self.SPARK) - 1,
                                       int(b / hi * (len(self.SPARK) - 1)))]
                        for b in buckets)
        return buckets, recent, prev, spark, mature

    def buy_concentration(self, curve):
        """
        (n_buyers, top1_share_of_buys, top1_share_of_volume) or None.

        A token where one wallet is most of the buy TAPE is a different animal
        from one with real participation -- it is either a single accumulator or
        somebody trading with themselves. Volume alone cannot tell them apart,
        which is exactly why volume on its own is a weak signal.
        """
        bf = self.buyflow.get(curve)
        if not bf:
            return None
        n = len(bf)
        tot_c = sum(v[0] for v in bf.values())
        tot_q = sum(v[1] for v in bf.values())
        if tot_c < 5:
            return None                      # too thin to mean anything
        top_c = max(v[0] for v in bf.values())
        top_q = max(v[1] for v in bf.values()) if tot_q else 0
        return n, top_c / tot_c, (top_q / tot_q if tot_q else 0.0)

    HOT_WINDOW = 180.0

    def hot_now(self):
        """
        Every curve still inside the HOT window, strongest first.

        Expiry happens here rather than at trigger time so a token that keeps
        attracting smart wallets is not aged out while it is still the best thing
        on the screen.
        """
        now = time.time()
        with self.lock:
            items = [(c, t) for c, t in self.hot_active.items()
                     if now - t < self.HOT_WINDOW]
            self.hot_active = OrderedDict(
                (c, t) for c, t in self.hot_active.items() if now - t < self.HOT_WINDOW)
        # strongest cluster first; NEWEST first on a tie, because the point of this
        # list is not missing something that just fired
        items.sort(key=lambda ct: (-(self.smart.count(ct[0]) if self.smart else 0), -ct[1]))
        return items

    def tradeable_now(self, e):
        """
        Can this row be bought right now, under the slip limit?

        BUG THIS FIXES: the filter tested `verdict == "TRADEABLE"`, and a Pons
        pre-graduation row is always verdict CURVE -- never TRADEABLE. So pressing
        [f] on a Pons-only view filtered the ENTIRE list to empty by construction,
        which looked like the feed had died. Curves now have a real measured
        slippage (see curve_metrics), so tradeability can be asked properly instead
        of matching a verdict string that structurally cannot appear.
        """
        if e.get("state") != "done":
            return False
        slip = e.get("slippage_pct")
        if e.get("pre_grad") and not e.get("graduated"):
            return e.get("curve_tradeable") is True and slip is not None \
                and slip <= self.args.max_slip
        return e.get("verdict") == "TRADEABLE"

    def actionable(self, e):
        """
        Would [b] actually build a buy for this row? Same gates, evaluated cheaply.

        Scanning 18 rows to find the one that clears everything is the slow part of
        using this. The header now counts these so the eye goes straight there.
        """
        if e.get("state") != "done":
            return False
        if self.honeypot_state(e.get("curve"))[0] != "PASS":
            return False
        tx = e.get("tax_bps")
        if tx is None or tx >= self.args.max_creator_tax_bps:
            return False
        slip = e.get("slippage_pct")
        if slip is None or slip > self.args.max_slip:
            return False
        return True

    def rows(self):
        with self.lock:
            r = list(self.detail.values())
        vm = self.venue_modes[self.venue_i]
        if vm != "all":
            r = [e for e in r if (e.get("venue") or "pons") == vm]
        # DEAD CHARTS OUT. Applied before every other filter because a token nobody
        # has traded is not a candidate under ANY view. Measured on 1,405 rows, the
        # <$100 band was 40% of the board and reached near-graduation 2.7% of the
        # time against 44.6% above $10k -- a 16.5x spread, with the worst bucket the
        # largest one.
        #
        # Volume UNKNOWN is kept, not cut. It is missing for exactly the tokens
        # worth catching: enrichment can still be running while a curve completes
        # in 2.2 minutes, and treating "not measured yet" as "no volume" would drop
        # the fastest runners -- the same mistake mcap-first ranking made in
        # runners() before it was switched to tape-first.
        mv = getattr(self.args, "min_vol", 0) or 0
        if mv > 0:
            r = [e for e in r
                 if (e.get("vol_h1") or e.get("curve_volume_usd")) is None
                 or (e.get("vol_h1") or e.get("curve_volume_usd") or 0) >= mv]
        mb = getattr(self.args, "min_buyers", 0) or 0
        if mb > 0:
            # unknown is kept for the same reason as volume: it means "not measured
            # yet", and a curve can complete in 2.2 minutes. A row that is genuinely
            # moving is also kept regardless -- the live tape cannot lag, while the
            # buyer count is a scan result that can.
            r = [e for e in r
                 if e.get("n_buyers") is None
                 or (e.get("n_buyers") or 0) >= mb
                 or self.is_moving(e.get("curve"))]
        if self.only_tradeable == 1:
            r = [e for e in r if self.tradeable_now(e)]
        elif self.only_tradeable == 2:
            r = [e for e in r if self.actionable(e)]
        elif self.only_tradeable == 3:
            hs = {c for c, _ in self.hot_now()}
            r = [e for e in r if e.get("curve") in hs]
        elif self.only_tradeable == 5:
            r = [e for e in r if self.is_prime(e)]
        elif self.only_tradeable == 4:
            # MOVING: has a live tape right now.
            #
            # SAFE ONLY BECAUSE IT READS THE ROLLING WINDOW, not the value frozen at
            # enrich time. Measured on 17,338 launches: 58% show zero buyers when
            # first seen -- but so do 37% of the tokens that LATER reached heating
            # and 35% of those that reached near-graduation. Filtering on the
            # enrich-time snapshot would therefore have hidden roughly one in three
            # of everything that went somewhere, permanently, because nothing
            # re-checks a row you already dropped.
            #
            # curveflow is a 5-minute window fed by the live stream, so a token that
            # looked dead at launch and gets bought two minutes later reappears here
            # within seconds. The filter hides the quiet, it does not discard them.
            r = [e for e in r if self.is_moving(e.get("curve"))]
        r = r[::-1]
        sm = self.sort_modes[self.sort_i]
        if sm == "accel":
            def _k(e):
                a = self.buy_accel(e.get("curve"))
                return -(a[1] - a[2]) if a else 1e9      # biggest NEW-buyer jump first
            r.sort(key=_k)
        elif sm == "smart":
            r.sort(key=lambda e: -(self.smart.count(e.get("curve")) if self.smart else 0))
        elif sm == "mcap":
            r.sort(key=lambda e: -(e.get("mcap") or 0))
        return r[:self.args.rows]

    def alert_payload(self, curve):
        """Everything the Discord mini-report needs, from whatever has landed."""
        with self.lock:
            e = dict(self.detail.get(curve) or {})
        meta = self.hot_meta.get(curve) or {}
        iv = self.inv.get(curve) or {}
        probes = iv.get("probes") or []
        fails = [x for x in probes if x["status"] == "FAIL"]
        warns = [x for x in probes if x["status"] == "WARN"]
        why = " · ".join(f"{x['probe']} — {x['detail'][:70]}" for x in (fails + warns)[:3])
        conc = self.buy_concentration(curve)
        acc = self.buy_accel(curve)
        return {
            "symbol": e.get("symbol") or meta.get("symbol") or "?",
            "token": e.get("token") or meta.get("token") or curve,
            "curve": curve,
            "n_smart": self.smart.count(curve) if self.smart else 0,
            "hot": True,
            "block": e.get("block"),
            "mcap": e.get("mcap"),
            "active_liq_usd": e.get("active_liq_usd"),
            "slippage_pct": e.get("slippage_pct"),
            "tax_bps": e.get("tax_bps"),
            "total_fee_bps": e.get("total_fee_bps"),
            "n_buyers": e.get("n_buyers"),
            "n_sellers": e.get("n_sellers"),
            "n_holders": e.get("n_holders"),
            "top1_share": (conc[1] if conc else e.get("top1_share")),
            "dev": e.get("dev"),
            "dev_prior_grads": e.get("dev_prior_grads"),
            "verdict": e.get("verdict"),
            "flow": (acc[3] if acc else None),
            # None = too young to judge, and the alert must say that rather than
            # asserting a trend from a token's first few seconds
            "flow_rising": ((acc[1] > acc[2] * 1.5) if acc[4] else None) if acc else None,
            "vet_state": iv.get("state") or "queued",
            "vet_fails": len(fails),
            "vet_warns": len(warns),
            "vet_why": why,
            "mode": "ARMED" if getattr(self.args, "arm", False) else "DISARMED",
        }

    def alert_hot(self, curve):
        """
        Send the Discord mini-report for a HOT token.

        Waits (briefly) for vetting to land first. Firing instantly would mean the
        phone alert cannot say whether the token is a honeypot -- and an alert that
        prompts you to buy something vetting was about to block is worse than no
        alert. The wait is bounded because a late alert is also useless: past ~25s
        the launch has moved, and the row keeps its own vetting on screen anyway.
        """
        deadline = time.time() + 25
        while time.time() < deadline:
            if (self.inv.get(curve) or {}).get("state") == "done":
                break
            time.sleep(1.5)
        try:
            import alerts
            ok, detail = alerts.send(self.alert_payload(curve))
            self.stats["alerts_sent"] = self.stats.get("alerts_sent", 0) + (1 if ok else 0)
            if not ok:
                # never silently swallow: a webhook that stopped working must be
                # visible, or you stop getting alerts and never learn why
                self.stats["alerts_failed"] = self.stats.get("alerts_failed", 0) + 1
                self.msg = f"discord: {detail[:60]}"
        except Exception as ex:  # noqa: BLE001
            self.stats["alerts_failed"] = self.stats.get("alerts_failed", 0) + 1
            self.msg = f"discord error: {str(ex)[:60]}"

    def _select_curve(self, curve, why=""):
        """
        Move the main selection onto `curve`, whatever filter is active.

        The alert panels (HOT / RUNNERS / NEAR GRADUATION) are display-only -- a
        token can be shown there while the filtered table does not contain its row,
        so selecting it has to clear the filter first or the jump silently fails.
        Once selected, every existing action works on it: [i] report, [c] copy CA,
        [b] build a buy.
        """
        if not curve:
            self.msg = "nothing to jump to"
            return False
        with self.lock:
            known = curve in self.detail
        if not known:
            self.msg = f"{(why or 'row')} is not in the table yet — still enriching"
            return False
        self.only_tradeable = 0
        rs = self.rows()
        for i, e in enumerate(rs):
            if e.get("curve") == curve:
                self.sel, self.sel_curve = i, curve
                return True
        self.msg = "row dropped out of the view"
        return False

    def _cycle_panel(self, items, key, label):
        """Step through one panel's tokens, remembering where we were."""
        if not items:
            self.msg = f"no {label} right now"
            return
        cur = self.sel_curve
        nxt = items[(items.index(cur) + 1) % len(items)] if cur in items else items[0]
        if self._select_curve(nxt, label):
            pos = items.index(nxt) + 1
            self.msg = (f"{label} {pos}/{len(items)} — {self._sym_for(nxt) or nxt[:10]}"
                        + (f"   press {key} again for the next" if len(items) > 1 else ""))

    def nansen_lookup(self):
        """
        [N] — Nansen deep-dive on the highlighted coin's DEPLOYER.

        Explicit keypress only. Nansen credits are finite and this runs two calls;
        autovet touches every row at ~21 launches/min, so wiring it there would
        drain the plan on coins nobody is considering. Cached 72h, so re-pressing
        on the same coin is free.

        It answers the one question our own chain data cannot: who funded this dev.
        A set of "independent" wallets sharing a funder is one actor -- the
        hot-wallet-cluster trap from Phase 0.
        """
        rs = self.rows()
        if not rs:
            self.msg = "nothing selected"
            return
        e = rs[min(self.sel, len(rs) - 1)]
        dev = e.get("dev")
        if not dev:
            self.msg = "no deployer on this row yet — still enriching"
            return
        self.msg = "nansen: looking up the deployer…"

        def work():
            try:
                import nansen
                d = nansen.investigate_address(dev)
                with self.lock:
                    row = self.detail.get(e.get("curve"))
                    if row is not None:
                        row["nansen"] = d
                cached = " (cached)" if d.get("_cached") else ""
                if d.get("errors"):
                    self.msg = f"nansen: {d['errors'][0]}"
                elif d.get("funder"):
                    self.msg = (f"nansen{cached}: dev funded by {d['funder'][:12]}… · "
                                f"{len(d['counterparties'])} counterparties")
                else:
                    self.msg = f"nansen{cached}: no funder found · dev looks standalone"
            except Exception as ex:  # noqa: BLE001
                self.msg = f"nansen failed: {str(ex)[:50]}"

        threading.Thread(target=work, daemon=True).start()

    def copy_ca(self):
        """
        Put the highlighted contract on the clipboard.

        A terminal cannot offer click-to-copy, and selecting an address with the
        mouse is exactly what killed a session once (the stray keypress that led to
        the [q] confirmation). One keystroke is the closest real equivalent.
        """
        rs = self.rows()
        if not rs:
            self.msg = "nothing selected"
            return
        e = rs[min(self.sel, len(rs) - 1)]
        ca = e.get("token") or e.get("curve")
        if not ca:
            self.msg = "no contract on this row yet"
            return
        try:
            subprocess.run(["pbcopy"], input=ca.encode(), check=True)
            self.msg = f"copied {ca}"
        except Exception as ex:  # noqa: BLE001
            self.msg = f"copy failed ({str(ex)[:30]}) — {ca}"

    def open_report(self):
        """
        Write a one-token report and open it in the browser.

        Links belong here rather than in the table: terminal hyperlink support is
        inconsistent, but a browser page is unambiguous -- every address is a real
        anchor, and the CA is selectable text. Investigation results are reused
        from autovet, so this costs a file write, not a re-vet.
        """
        rs = self.rows()
        if not rs:
            self.msg = "nothing selected"
            return
        e = rs[min(self.sel, len(rs) - 1)]
        curve = e.get("curve")
        tok = e.get("token") or curve
        iv = self.inv.get(curve) or {}
        sym = e.get("symbol")
        if not sym or sym == "?":
            # Read it off chain rather than titling the report "?". The row may not
            # have finished enriching when [i] is pressed, and a report you opened
            # deliberately should not be the one place the ticker is missing.
            rsym, rtok = self._resolve_curve(curve)
            if rsym:
                sym = rsym
                with self.lock:
                    row = self.detail.get(curve)
                    if row is not None:
                        row["symbol"] = rsym
                        if rtok and not row.get("token"):
                            row["token"] = rtok
                if rtok and not tok:
                    tok = rtok
        sym = sym or "?"

        def esc(x):
            return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

        rowsh = []
        for pr in iv.get("probes") or []:
            col = {"PASS": "#1a7f37", "WARN": "#9a6700", "FAIL": "#cf222e"}.get(pr["status"], "#57606a")
            rowsh.append(f"<tr><td style='color:{col};font-weight:600'>{esc(pr['status'])}</td>"
                         f"<td>{esc(pr['probe'])}</td><td>{esc(pr['detail'])}</td></tr>")
        if not rowsh:
            rowsh.append("<tr><td colspan=3><i>vetting has not finished for this token</i></td></tr>")

        def metric(label, val, note=""):
            n = f" <span class=sub>{esc(note)}</span>" if note else ""
            return f"<tr><td>{label}</td><td><b>{esc(val)}</b>{n}</td></tr>"

        def pct(v):
            return "—" if v is None else f"{float(v)*100:.1f}%"

        def usd(v):
            try:
                return "$" + format(float(v), ",.0f")
            except Exception:  # noqa: BLE001
                return "—"

        ctxd = iv.get("ctx") or {}

        # --- X account ---------------------------------------------------------
        # ONE paid lookup ($0.010, cached 24h), only on a report the operator asked
        # for. The handle comes from ctx["socials"]["twitter"] first: DexScreener
        # has not indexed 0 of 12 sampled fresh launches, so social_urls is usually
        # empty at exactly this moment.
        xblock = ""
        try:
            import investigate as INV
            socials = ctxd.get("socials") or {}
            urls = ([socials.get("twitter")] if socials.get("twitter") else []) \
                + list(ctxd.get("social_urls") or []) + list(ctxd.get("desc_urls") or [])
            h = INV.x_handle_from_urls(urls)
            if not h:
                xblock = ("<p class=sub>X account — <b>not checked</b> (vetting has "
                          "not produced data yet)</p>" if not ctxd else
                          "<p class=sub>no X account linked by this token.</p>")
            else:
                xp = INV.x_profile(h)
                if xp.get("error"):
                    xblock = (f"<p class=sub>X @{esc(h)} — <b>not checked</b> "
                              f"({esc(xp['error'])})</p>")
                else:
                    age = xp.get("age_days")
                    agestr = (f"{age//365}y {(age%365)//30}m" if age and age >= 365
                              else f"{age}d" if age is not None else "?")
                    tells = xp.get("tells") or []
                    tl = ("".join(f"<li>{esc(t)}</li>" for t in tells)
                          if tells else "<li>nothing undermining the number</li>")
                    xblock = (
                        f"<table>"
                        f"<tr><td>handle</td><td><b><a href='https://x.com/{esc(h)}' "
                        f"target='_blank'>@{esc(h)}</a></b>"
                        f"{' ✔' if xp.get('verified') else ''}</td></tr>"
                        f"<tr><td>followers</td><td><b>{(xp.get('followers') or 0):,}</b></td></tr>"
                        f"<tr><td>account age</td><td><b>{agestr}</b> "
                        f"<span class=sub>({esc((xp.get('created_at') or '')[:10])})</span></td></tr>"
                        f"<tr><td>posts</td><td>{(xp.get('tweets') or 0):,}</td></tr>"
                        f"<tr><td>following</td><td>{(xp.get('following') or 0):,}</td></tr>"
                        f"</table>"
                        f"<p class=sub>Reasons the follower count might not mean what "
                        f"it looks like:</p><ul>{tl}</ul>")
        except Exception as ex:  # noqa: BLE001
            xblock = f"<p class=sub>X lookup failed: {esc(str(ex)[:80])}</p>"

        # Everything below already comes back from the GMGN probe we ALREADY pay
        # for -- it was being fetched and thrown away. 37 top-level fields with
        # four rich sub-objects, of which the report previously rendered two.
        gm = (ctxd.get("gmgn") or {})
        gdev = gm.get("dev") or {}
        gstat = gm.get("stat") or {}
        gtags = gm.get("wallet_tags_stat") or {}
        gprice = gm.get("price") or {}

        slip = e.get("slippage_pct")
        mets = "".join([
            metric("market cap", fmt_usd(e.get("mcap"))),
            metric("liquidity in curve", fmt_usd(e.get("active_liq_usd"))),
            metric("slippage at $%.0f" % self.args.stake,
                   f"{slip:.2f}%" if slip is not None else "unknown"),
            metric("size for this pot",
                   (f"${e['sized_stake_usd']:.0f}"
                    + (f"  ({e['stake_pct_of_pot']:.1f}% of pot)"
                       if e.get("stake_pct_of_pot") is not None else ""))
                   if e.get("sized_stake_usd") is not None else "—",
                   "impact is ~1.6x your share of the pot; $25 into a $300 curve "
                   "costs 13.3% round trip and $40 turns it negative"),
            metric("creator tax", f"{(e.get('tax_bps') or 0)/100:.1f}%",
                   "1-2% is the only measured net-positive band"
                   if 0 < (e.get("tax_bps") or 0) <= 200 else
                   "400+ measured worst" if (e.get("tax_bps") or 0) >= 400 else ""),
            metric("total fees", f"{e.get('total_fee_bps',0)/100:.1f}%"),
            # measured on-chain from sellableTokens(), not taken from GMGN -- this
            # is the number the entry decision turns on, so it must not depend on a
            # third party's indexer. GMGN is the fallback only.
            metric("curve progress",
                   (f"{e['curve_progress']:.1%} of supply sold"
                    if e.get("curve_progress") is not None
                    else pct(gm.get("launchpad_progress"))),
                   "pf 2.79 entering at 10%, 1.32 at 25%, 0.77 at 40% (n=1,173)"),
            metric("price now", gprice.get("price") or gm.get("price") or "—"),
            metric("all-time high", gm.get("ath_price") or "—",
                   "current vs ATH tells you if you are buying the top"),
            metric("smart wallets in", self.smart.count(curve) if self.smart else 0),
            metric("verdict", f"{e.get('verdict','?')} — {e.get('why','')}"),
        ])

        # --- flow: the tape across windows -----------------------------------
        flow_rows = ""
        for w, lbl in (("1m", "1 min"), ("5m", "5 min"), ("1h", "1 hour"),
                       ("24h", "24 hour")):
            b, sl = gprice.get(f"buys_{w}"), gprice.get(f"sells_{w}")
            bv, sv = gprice.get(f"buy_volume_{w}"), gprice.get(f"sell_volume_{w}")
            if b is None and sl is None:
                continue
            try:
                net = float(bv or 0) - float(sv or 0)
            except Exception:  # noqa: BLE001
                net = 0
            col = "#1a7f37" if net > 0 else "#cf222e"
            flow_rows += (f"<tr><td>{lbl}</td><td>{b or 0} buys / {sl or 0} sells</td>"
                          f"<td>{usd(bv)} in / {usd(sv)} out</td>"
                          f"<td style='color:{col};font-weight:600'>"
                          f"{'+' if net >= 0 else ''}{usd(abs(net))[1:] if net else '0'}"
                          f"</td></tr>")
        flowblock = (f"<table><tr><th>window</th><th>trades</th><th>volume</th>"
                     f"<th>net</th></tr>{flow_rows}</table>"
                     if flow_rows else "<p class=sub>no trade data yet</p>")

        # --- distribution: who holds it --------------------------------------
        def risky(v, hi):
            try:
                return float(v) >= hi
            except Exception:  # noqa: BLE001
                return False
        dist = "".join([
            metric("holders", gstat.get("holder_count") or gm.get("holder_count") or "—"),
            metric("top 10 hold", pct(gstat.get("top_10_holder_rate")),
                   "⚠ concentrated" if risky(gstat.get("top_10_holder_rate"), 0.5) else ""),
            metric("dev team holds", pct(gstat.get("dev_team_hold_rate")),
                   "⚠ dev can dump" if risky(gstat.get("dev_team_hold_rate"), 0.05) else ""),
            metric("sniper hold (top70)", pct(gstat.get("top70_sniper_hold_rate")),
                   "⚠ snipers own supply" if risky(gstat.get("top70_sniper_hold_rate"), 0.15) else ""),
            metric("fresh wallets", pct(gstat.get("fresh_wallet_rate")),
                   "⚠ likely one actor" if risky(gstat.get("fresh_wallet_rate"), 0.3) else ""),
            metric("bundler traders", pct(gstat.get("top_bundler_trader_percentage"))),
            metric("rat traders", pct(gstat.get("top_rat_trader_percentage"))),
            metric("bot degens", pct(gstat.get("bot_degen_rate"))),
        ])
        tagrow = " · ".join(f"<b>{v}</b> {k.replace('_wallets','')}"
                            for k, v in gtags.items() if v)
        tagblock = f"<p>{tagrow}</p>" if tagrow else "<p class=sub>no wallet tags</p>"

        # --- deployer ---------------------------------------------------------
        # twitter_name_change_history is the handle-rename record -- the strongest
        # bought/hijacked tell there is, and the thing our own heuristics could only
        # INFER from mismatched follower/age/post counts.
        hist = gdev.get("twitter_name_change_history") or []
        histblock = ("<ul>" + "".join(f"<li>{esc(json.dumps(h))[:160]}</li>"
                                      for h in hist[:6]) + "</ul>") if hist else                     "<p class=sub>no handle renames recorded</p>"
        dl = e.get("dev_launches")
        devblock = "".join([
            metric("deployer", e.get("dev") or "—"),
            metric("launches seen", dl if dl is not None else "—",
                   "first launch — 11.6% hit rate, the best bucket" if (dl or 0) <= 1
                   else "10+ launches — 2.8% hit rate, 4x worse" if (dl or 0) >= 10
                   else "below base rate"),
            metric("funded by", gdev.get("fund_from") or "—",
                   "shared funders across devs = one operator"),
            metric("tokens from this X account", gdev.get("twitter_create_token_count") or 0,
                   "⚠ serial launcher" if (gdev.get("twitter_create_token_count") or 0) >= 3 else ""),
            metric("deleted posts", gdev.get("twitter_del_post_token_count") or 0),
            metric("CTO'd", "yes" if gdev.get("cto_flag") else "no"),
            metric("paid DexScreener promo",
                   "yes" if (gdev.get("dexscr_ad") or gdev.get("dexscr_boost_fee")) else "no"),
        ])

        dev = e.get("dev") or ""
        links = [("Blockscout — token", f"https://robinhoodchain.blockscout.com/token/{tok}"),
                 ("Blockscout — curve", f"https://robinhoodchain.blockscout.com/address/{curve}"),
                 ("DexScreener", f"https://dexscreener.com/robinhood/{tok}"),
                 ("GMGN", f"https://gmgn.ai/robinhood/token/{tok}")]
        if dev:
            links.append(("Deployer wallet",
                          f"https://robinhoodchain.blockscout.com/address/{dev}"))
        if gdev.get("fund_from"):
            links.append(("Deployer's FUNDER",
                          f"https://robinhoodchain.blockscout.com/address/{gdev['fund_from']}"))
        linkh = "".join(f"<li><a href='{u}' target='_blank'>{n}</a></li>" for n, u in links)
        html_doc = f"""<!doctype html><meta charset=utf-8>
<title>{esc(sym)} — Hood Sniper</title>
<style>
 body{{font:14px -apple-system,Segoe UI,sans-serif;margin:28px;max-width:940px;
      color:#1f2328;background:#fff}}
 @media(prefers-color-scheme:dark){{body{{background:#0d1117;color:#e6edf3}}
      td,th{{border-color:#30363d!important}} a{{color:#6cb6ff}}
      .ca{{background:#161b22!important}}}}
 h1{{margin:0 0 2px;font-size:22px}}
 h3{{margin:22px 0 6px;padding-bottom:4px;border-bottom:2px solid #d0d7de}}
 h4{{margin:14px 0 4px;font-size:13px;color:#57606a}}
 .ca{{font:13px ui-monospace,Menlo,monospace;background:#f6f8fa;padding:8px 10px;
      border-radius:6px;user-select:all;word-break:break-all}}
 table{{border-collapse:collapse;width:100%;margin:6px 0}}
 td,th{{border-bottom:1px solid #d0d7de;padding:6px 8px;text-align:left;
        vertical-align:top}}
 td:first-child{{color:#57606a;width:210px}}
 .sub{{color:#57606a;font-size:12px}} ul{{padding-left:18px;margin:4px 0}}
</style>
<h1>${esc(sym)}</h1>
<div class=sub>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} ·
 triple-click the address to select it all</div>
<p class=ca>{esc(tok)}</p>
<h3>Market</h3><table>{mets}</table>
<h3>Flow — who is trading it</h3>{flowblock}
<h3>Distribution — who holds it</h3><table>{dist}</table>
<h4>wallet tags</h4>{tagblock}
<h3>Deployer</h3><table>{devblock}</table>
<h4>X handle rename history</h4>{histblock}
<h3>X account</h3>{xblock}
<h3>Vetting</h3><table><tr><th>status</th><th>probe</th><th>detail</th></tr>
{''.join(rowsh)}</table>
<h3>Dig deeper</h3><ul>{linkh}</ul>
<p class=sub>Written by launch_monitor. Market, flow, distribution and deployer
data come from the GMGN probe autovet already ran — no extra calls.</p>"""
        path = os.path.join(DATA, f"token_{(tok or 'unknown')[2:10]}.html")
        try:
            with open(path, "w") as f:
                f.write(html_doc)
            subprocess.run(["open", path], check=False)
            self.msg = f"report opened for ${sym}"
        except Exception as ex:  # noqa: BLE001
            self.msg = f"report failed: {str(ex)[:40]}"

    def investigate_selected(self):
        rs = self.rows()
        if not rs or run_investigation is None:
            return
        e = rs[min(self.sel, len(rs) - 1)]
        tok, curve = e.get("token"), e.get("curve")
        if not tok:
            return
        self.inv_for = curve
        if self.inv.get(curve, {}).get("state") == "running":
            return
        self.inv[curve] = {"state": "running", "probes": [], "symbol": e.get("symbol")}

        def work():
            try:
                ctx, probes = run_investigation(tok, self.args.stake)
                ctx.pop("registry", None)
                ctx.pop("ds_pair", None)
                self.inv[curve] = {"state": "done", "probes": probes,
                                   "ctx": ctx, "symbol": e.get("symbol")}
                with open(os.path.join(DATA, "investigations.jsonl"), "a") as f:
                    f.write(json.dumps({"token": tok, "curve": curve,
                                        "probes": probes}, default=str) + "\n")
            except Exception as ex:  # noqa: BLE001
                self.inv[curve] = {"state": f"error: {ex}"[:60], "probes": [],
                                   "symbol": e.get("symbol")}
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _norm(t):
        """
        Normalise a ticker for matching.

        Symbols on this chain are inconsistent about the leading '$' -- a live
        sample had PAIN, DOGE and ROBIN alongside $MOAR and $INTERN. Matching the
        raw string meant watching HOOJA silently missed a token literally called
        $HOOJA, and the miss looked exactly like "no launch yet". Also strips
        whitespace and zero-width characters, which are a squatting technique.
        """
        t = (t or "").upper().strip()
        for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", " ", "\t"):
            t = t.replace(ch, "")
        return t.lstrip("$")

    def check_watch(self, token, curve, blk, now, venue):
        """
        Ticker watch for an ANTICIPATED launch.

        12.7% of symbols on this chain already have more than one contract
        ($GPRO 12, $FOMO 9, $HOOD 5), so a hyped ticker attracts squatters.
        This never picks a winner -- it surfaces EVERY match with its vetting
        so the choice stays explicit.
        """
        r = rpc("eth_call", [{"to": token, "data": SEL_SYMBOL}, "latest"])
        sym = (call_str((r or {}).get("result")) or "").strip()
        if not sym:
            return
        name_hit = self._norm(sym) in self.watch_norm
        if not name_hit:
            r2 = rpc("eth_call", [{"to": token, "data": SEL_NAME}, "latest"])
            nm = self._norm(call_str((r2 or {}).get("result")))
            name_hit = any(w in nm for w in self.watch_norm if w)
        if not name_hit:
            return
        key = curve if venue == "pons" else token
        with self.lock:
            self.hits.append({"t": now, "symbol": sym, "token": token,
                              "curve": curve, "block": blk, "venue": venue})
            self.stats["watch_hits"] = self.stats.get("watch_hits", 0) + 1
            if key not in self.detail:
                self.detail[key] = {"curve": key, "block": blk, "t": now,
                                    "state": "enriching", "venue": venue,
                                    "token": token, "symbol": sym,
                                    "watch_hit": True, "flags": [], "good": ["WATCH"]}
                self.q.appendleft(key)          # jump the queue
        # a watch hit is the one case worth investigating unprompted
        if run_investigation:
            def work():
                try:
                    ctx, probes = run_investigation(token, self.args.stake)
                    ctx.pop("registry", None); ctx.pop("ds_pair", None)
                    self.inv[key] = {"state": "done", "probes": probes,
                                     "ctx": ctx, "symbol": sym}
                    self.inv_for = key
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=work, daemon=True).start()

    def enrich_o1(self, rec):
        """o1 hands us token/dev/fee in the launch event; only the pool is left."""
        o = {k: rec.get(k) for k in ("curve", "block", "venue", "token", "dev",
                                     "quote", "total_fee_bps")}
        o["flags"], o["good"] = [], ["o1"]
        token = rec.get("token")
        if not token:
            o["verdict"], o["why"] = "UNKNOWN", "no token in event"
            return o
        r = rpc_batch([("eth_call", [{"to": token, "data": SEL_SYMBOL}, "latest"]),
                       ("eth_call", [{"to": token, "data": SEL_NAME}, "latest"])])
        o["symbol"] = call_str((r[0] or {}).get("result")) or "?"
        o["name"] = call_str((r[1] or {}).get("result")) or ""
        dev = (rec.get("dev") or "").lower()
        v = self.reg.get(dev)
        if v:
            o["dev_launches"] = v.get("launches", 0)
            o["dev_prior_grads"] = sum(1 for b in v.get("grad_blocks", [])
                                       if b < rec.get("block", 0))
        if (rec.get("total_fee_bps") or 0) >= 200:
            o["flags"].append(f"FEE {rec['total_fee_bps']/100:.1f}%")
        o.update(enrich_pool(token, rec["block"], self.args.stake))
        return o

    def enrich_bankr(self, rec):
        """Bankr/Doppler: no curve, so identify the clone side and read the pool."""
        o = {"curve": rec["curve"], "block": rec["block"], "venue": "bankr",
             "flags": [], "good": []}
        token = None
        # eth_getCode on a contract from the CURRENT block often comes back empty:
        # the websocket delivers the log before the RPC node serving eth_getCode has
        # the state. Enriching instantly then failed to spot the Doppler clone and
        # the row was written off as "no Doppler clone side". Retry briefly here
        # rather than burning a whole retry cycle on propagation lag.
        for c in (rec.get("c1"), rec.get("c0")):
            if not c or int(c, 16) == 0:
                continue
            code = (rpc("eth_getCode", [c, "latest"]) or {}).get("result") or ""
            if len(code) == 90 and DOPPLER_IMPL in code.lower():
                token = c
                break
        if not token:
            o["state"] = "done"
            o["verdict"], o["why"] = "UNKNOWN", "no Doppler clone side"
            return o
        o["token"] = token
        r = rpc_batch([("eth_call", [{"to": token, "data": SEL_SYMBOL}, "latest"]),
                       ("eth_call", [{"to": token, "data": SEL_NAME}, "latest"])])
        o["symbol"] = call_str((r[0] or {}).get("result")) or "?"
        o["name"] = call_str((r[1] or {}).get("result")) or ""
        o["good"].append("BANKR")
        o["total_fee_bps"] = 0        # no curve fee; v4 pool fee only
        return o                      # enrich_worker calls enrich_pool -- do not duplicate it

    # ---- raw-mode key reader; rich.Live does not handle input itself ----
    def key_worker(self):
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except Exception:  # noqa: BLE001
            return                      # not a tty (piped) -- keys disabled
        try:
            tty.setcbreak(fd)
            while self.running:
                if not select.select([sys.stdin], [], [], 0.2)[0]:
                    continue
                ch = sys.stdin.read(1)
                if ch == "\x1b":                       # arrow escape sequence
                    seq = ""
                    # read up to 2 more bytes, tolerating them arriving separately
                    for _ in range(2):
                        if select.select([sys.stdin], [], [], 0.08)[0]:
                            seq += sys.stdin.read(1)
                    ch = {"[A": "k", "[B": "j", "OA": "k", "OB": "j"}.get(seq, "")
                if ch and ch.isalpha():
                    ch = ch.lower()          # 'K' is not 'k'; do not silently ignore it
                self.last_key = ch
                n = len(self.rows())
                if ch in ("j",):
                    self.move_sel(+1)
                elif ch in ("k",):
                    self.move_sel(-1)
                elif ch in ("i", "\r", "\n"):
                    # [i] used to KICK OFF an investigation. autovet already runs one
                    # for every token before it can be selected, so the useful action
                    # here is opening the readable version with working links.
                    self.open_report()
                elif getattr(self, "confirm_quit", False):
                    if ch == "y":
                        self.running = False
                    else:
                        self.confirm_quit = False
                        self.msg = "still running"
                elif ch == "b":
                    self.arm_buy()
                elif ch == "y":
                    self.confirm_buy()
                elif ch == "n":
                    self.pending = None
                    self.msg = "cancelled"
                elif ch == "c":
                    self.copy_ca()
                elif ch == "N":
                    self.nansen_lookup()
                elif ch == "R":
                    # forcing a re-vet is still worth having: autovet runs once per
                    # token, and a curve changes underneath it
                    rs = self.rows()
                    if rs:
                        cv = rs[min(self.sel, len(rs) - 1)].get("curve")
                        self.autovet_done.discard(cv)
                        self.autovet_q.appendleft(cv)
                        self.msg = "re-vetting this token…"
                elif ch == "f":
                    self.only_tradeable = (self.only_tradeable + 1) % 6
                    self.sel = 0
                    r = self.rows()
                    self.sel_curve = r[0].get("curve") if r else None
                    # [f] used to change the view silently, so an empty result was
                    # indistinguishable from the feed dying. Always say what is
                    # being shown, how many rows survived, and how to get back.
                    lbl = {0: "ALL launches",
                           1: f"TRADEABLE — buyable now under {self.args.max_slip}% slip",
                           2: "ACTIONABLE — passes every gate [b] checks",
                           3: "HOT — smart-wallet clusters still live",
                           4: "MOVING — live tape in the last 5 min "
                              "(quiet rows hidden, not dropped)",
                           5: "PRIME — clean of kill flags AND past heating "
                              "(21.7% reach near-grad, 6.5x base)"}[self.only_tradeable]
                    self.msg = (f"filter: {lbl}  ({len(r)} rows)"
                                + ("  — nothing matches; press f again to cycle back"
                                   if not r else ""))
                elif ch == "s":
                    self.sort_i = (self.sort_i + 1) % len(self.sort_modes)
                    self.msg = f"sort: {self.sort_modes[self.sort_i].upper()}"
                    r = self.rows()
                    self.sel_curve = r[0].get("curve") if r else None
                    self.sel = 0
                elif ch == "v":
                    self.venue_i = (self.venue_i + 1) % len(self.venue_modes)
                    self.sel = 0
                    r = self.rows()
                    self.sel_curve = r[0].get("curve") if r else None
                    self.msg = f"venue filter: {self.venue_modes[self.venue_i].upper()}"
                elif ch == "r":
                    self._cycle_panel([e.get("curve") for e in self.runners()],
                                      "r", "RUNNER")
                elif ch == "g":
                    self._cycle_panel([e.get("curve") for e in self.near_grad_rows()],
                                      "g", "NEAR GRAD")
                elif ch == ".":
                    # CYCLE through every live HOT, not just the newest. With one
                    # target this key kept re-selecting the same token while the
                    # others expired unseen.
                    live = [c for c, _ in self.hot_now()]
                    if live:
                        cur = self.sel_curve
                        nxt = live[(live.index(cur) + 1) % len(live)] if cur in live else live[0]
                        self.only_tradeable = 0
                        rs = self.rows()
                        for i, e in enumerate(rs):
                            if e.get("curve") == nxt:
                                self.sel = i
                                self.sel_curve = nxt
                                break
                        self.investigate_selected()
                        pos = live.index(nxt) + 1
                        self.msg = (f"HOT {pos}/{len(live)} — {self._sym_for(nxt) or nxt[:10]}"
                                    + ("   press . again for the next one"
                                       if len(live) > 1 else ""))
                    else:
                        self.msg = "no live HOT tokens right now"
                elif ch in ("q",):
                    # Explicit confirmation. A single stray keypress used to kill a
                    # live session -- which happened while trying to copy an address
                    # off the screen, losing a 21-wallet alert with it.
                    self.confirm_quit = True
                    self.msg = "QUIT?  [y] yes, stop the bot   [n] no, keep running"
                elif ch == "?":
                    self.show_help = not self.show_help
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:  # noqa: BLE001
                pass

    # ---- classification: what is worth showing prominently ----
    def verdict(self, e):
        # See the docstring below. UNTRADED is separated out first because it is a
        # different fact from "cannot price it yet" and must not retry forever.
        """
        Deliberately conservative. Ranking uses only what is MEASURED to matter:
        executable liquidity and fee drag. The wallet-vetting flags are shown
        for context but do NOT move the verdict -- they failed out-of-sample
        (zero-sniper signal reversed, p=0.33) and must not be dressed up as
        predictive.
        """
        if e.get("pre_grad") and not e.get("graduated"):
            # A curve whose buy() reverts at ANY size is not a market you can enter.
            # That is a stronger and earlier fact than any market metric, and it is
            # common: pristine curves (untouched token balance, zero ETH) revert at
            # every size including a $0.10 probe.
            if e.get("curve_tradeable") is False:
                return "NO-BID", "buy() reverts at every size — the curve is not open"
            gp = e.get("grad_pct")
            slip = e.get("slippage_pct")
            bits = []
            if gp is not None:
                bits.append(f"{gp:.0f}% full")
            if e.get("active_liq_usd") is not None:
                bits.append(f"{fmt_usd(e['active_liq_usd'])} in the curve")
            if slip is not None:
                bits.append(f"slip {slip:.2f}%")
            return "CURVE", "on the curve" + (" · " + " · ".join(bits) if bits else "")
        slip = e.get("slippage_pct")
        if slip is None:
            # A Bankr/Doppler launch goes STRAIGHT to a pool, so it exists but has
            # had zero swaps -- and pool_state needs a swap to read a price. That
            # is not "no pool yet", it is "nobody has traded it", which is a real
            # and final answer. Conflating the two made 37/40 rows sit in an
            # endless retry loop showing nothing.
            if e.get("untraded"):
                return "UNTRADED", "pool live, zero swaps — nobody has bought it"
            return "UNKNOWN", "no pool yet"
        if slip > self.args.max_slip:
            return "UNTRADEABLE", f"slip {slip:.2f}%"
        if e.get("total_fee_bps", 0) >= 300:
            return "COSTLY", f"fees {e['total_fee_bps']/100:.1f}%"
        return "TRADEABLE", f"slip {slip:.2f}%"

    def on_log(self, lg):
        topic = lg["topics"][0]
        if topic == T_V4_INITIALIZE:
            self.on_v4_init(lg)
            return
        if topic == T_V4_SWAP:
            self.on_v4_swap(lg)
            return
        addr = lg["address"].lower()
        blk = int(lg["blockNumber"], 16)
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if topic in (T_CURVE_BUY, T_CURVE_SELL):
            self.on_curve_trade(lg, topic == T_CURVE_BUY)
        if topic == T_CURVE_SELL:
            return
        if topic == T_CURVE_BUY:
            # topics[2] is the RECIPIENT -- the actual buyer. Using the sender
            # would attribute every routed buy to the router.
            if not self.smart or len(lg["topics"]) < 3:
                return
            d = lg["data"][2:]
            if len(d) < 128:
                return
            w = "0x" + lg["topics"][2][-40:]
            # Buy concentration. We already consume every curve buy for the smart
            # money watch, so tracking WHO is buying costs nothing extra. One
            # wallet owning most of the buys is a different token from one with
            # broad participation, and the tape does not show that anywhere else.
            try:
                _q = int(lg["data"][2:][0:64], 16)
                bf = self.buyflow.setdefault(addr, {})
                rowd = bf.setdefault(w.lower(), [0, 0, time.time()])
                rowd[0] += 1
                rowd[1] += _q
                if len(self.buyflow) > 4000:
                    self.buyflow.pop(next(iter(self.buyflow)))
            except Exception:  # noqa: BLE001
                pass
            sym0 = self._sym_for(addr)
            if sym0 is None and self.smart.count(addr) + 1 >= self.smart.alert_at:
                # Resolve the ticker NOW. HOT fires on curve BUYS (pre-graduation)
                # while the table only lists GRADUATIONS, so the hot token is
                # usually not in the table at all -- the banner had no ticker and
                # nothing to jump to, which made the loudest signal unactionable.
                sym0, tok0 = self._resolve_curve(addr)
                if tok0:
                    with self.lock:
                        self.hot_meta[addr] = {"symbol": sym0, "token": tok0}
            a = self.smart.note_buy(addr, w, int(d[0:64], 16), int(d[64:128], 16),
                                    blk, symbol=sym0)
            if a:
                with self.lock:
                    self.smart_alerts.insert(0, {"t": now, "curve": addr,
                                                 "n": a["n"],
                                                 "symbol": a.get("symbol"),
                                                 "block": blk,
                                                 "wallets": a["wallets"]})
                    del self.smart_alerts[40:]
                    self.stats["smart_alerts"] = self.stats.get("smart_alerts", 0) + 1
                try:
                    with open(os.path.join(DATA, "smart_alerts.jsonl"), "a") as f:
                        f.write(json.dumps({
                            "ts_utc": datetime.now(timezone.utc).isoformat(),
                            "curve": addr,
                            "token": (self.hot_meta.get(addr) or {}).get("token")
                                     or self._token_for(addr),
                            "symbol": a.get("symbol"), "n_smart": a["n"],
                            "hot": bool(a.get("hot")), "block": blk,
                            "wallets": [w["wallet"] for w in a["wallets"]]}) + "\n")
                except Exception:  # noqa: BLE001
                    pass
                with self.lock:
                    if a.get("hot"):
                        self.hot_q.append(addr)
                        self.hot_curve, self.hot_since = addr, time.time()
                        self.hot_active.setdefault(addr, time.time())
                        # off-thread: a webhook round trip must never stall the log
                        # loop, and HOT is rare enough that a thread each is fine
                        if not getattr(self.args, "no_discord", False):
                            threading.Thread(target=self.alert_hot, args=(addr,),
                                             daemon=True).start()
                        while len(self.hot_active) > 24:
                            self.hot_active.popitem(last=False)
                        self.stats["hot"] = self.stats.get("hot", 0) + 1
            return
        self.last_head = max(self.last_head, blk)
        if topic == T_O1_LAUNCH:
            # topics: [t0, token, poolId, creator]; data: quote, supply, feeBps
            if len(lg["topics"]) < 4:
                return
            token = "0x" + lg["topics"][1][-40:]
            dev = "0x" + lg["topics"][3][-40:]
            d = lg["data"][2:]
            quote = "0x" + d[24:64] if len(d) >= 64 else None
            fee = int(d[128:192], 16) if len(d) >= 192 else 0
            key = lg["topics"][1]
            with self.lock:
                self.stats["grads"] += 1
                if key not in self.detail:
                    self.detail[key] = {"curve": key, "block": blk, "t": now,
                                        "state": "enriching", "venue": "o1",
                                        "token": token, "dev": dev,
                                        "quote": quote, "total_fee_bps": fee,
                                        "flags": [], "good": []}
                    self.q.append(key)
                    while len(self.detail) > self.args.rows * 6:
                        self._evict()
            if self.watch:
                self.check_watch(token, key, blk, now, "o1")
            return
        if topic == T_V4_INITIALIZE:
            d = lg["data"][2:]
            if len(d) < 192 or ("0x" + d[152:192]).lower() != DOPPLER_HOOK:
                return                       # not Bankr -- ignore other v4 pools
            # Bankr launches straight into a tradeable pool, so treat the
            # Initialize itself the way a Pons graduation is treated.
            c0 = "0x" + lg["topics"][2][-40:]
            c1 = "0x" + lg["topics"][3][-40:]
            key = lg["topics"][1]
            with self.lock:
                self.stats["grads"] += 1
                if key not in self.detail:
                    self.detail[key] = {"curve": key, "block": blk, "t": now,
                                        "state": "enriching", "venue": "bankr",
                                        "c0": c0, "c1": c1,
                                        "flags": [], "good": []}
                    self.q.append(key)
                    while len(self.detail) > self.args.rows * 3:
                        self._evict()
            return
        if topic == T_INITIALIZED:
            # Pons LAUNCHES now become rows. Previously only GRADUATIONS did, and
            # those are rare (0 in a 65s sample) while Bankr pools land on every
            # launch -- so the table was all Bankr and the actual Robinhood Chain
            # launchpad was invisible. A pre-graduation curve is exactly where an
            # early entry happens, so it belongs in the table, not a ticker strip.
            d0 = lg["data"][2:]
            tok0 = "0x" + d0[24:64] if len(d0) >= 64 else None
            with self.lock:
                self.stats["new"] += 1
                self.ticker.append({"t": now, "curve": addr, "block": blk})
                if addr not in self.detail:
                    self.detail[addr] = {"curve": addr, "block": blk, "t": now,
                                         "state": "enriching", "venue": "pons",
                                         "token": tok0, "pre_grad": True,
                                         "flags": [], "good": []}
                    self.q.append(addr)
                    while len(self.detail) > self.args.rows * 6:
                        self._evict()
            if self.watch:
                # data of Initialized(address) is the token itself, so the
                # ticker is checkable one eth_call after the launch lands
                d = lg["data"][2:]
                tok = "0x" + d[24:64] if len(d) >= 64 else None
                if tok:
                    self.check_watch(tok, addr, blk, now, "pons")
        elif topic == T_CURVE_COMPLETED:
            # A graduation is an EXIT event, not an entry one. Measured on 197 real
            # graduations: the median token is at 0.702x after 5 min, 0.310x after
            # 15, and 0.160x after an hour -- 71% have lost half or more by then.
            # 36% peak within 30 SECONDS of migrating. So the useful alert here is
            # "the clock started", fired only for tokens actually being followed.
            self._maybe_grad_alert(addr, blk)
            with self.lock:
                self.stats["grads"] += 1
                if addr not in self.detail:
                    self.detail[addr] = {"curve": addr, "block": blk, "t": now,
                                         "state": "enriching", "flags": [], "good": []}
                    self.q.append(addr)
                    while len(self.detail) > self.args.rows * 3:
                        self._evict()

    # THE BAND IS THE CURVE'S OWN RANGE, not a number carried over from another
    # venue. Verified against GMGN on three live tokens (mine $5,314/$8,432/$11,943
    # vs GMGN $5,072/$8,226/$11,660 -- within 3%), a Pons curve runs about $4.2k at
    # launch to ~$48k at graduation. A $50k-$150k floor would match nothing here.
    #
    # THE FLOOR IS MEASURED, not guessed. Rate at which a curve goes on to reach
    # near-graduation, by the peak mcap it has recorded (n=14,783 curves):
    #
    #     $4k-6k    10,720 curves    0.5%   <- 72% of everything lives here
    #     $6k-8k       895           4.0%
    #     $8k-12k      658          12.3%   <- the old floor, near-worthless
    #     $12k-20k     591          32.1%
    #     $20k-35k     216          86.1%   <- highest conviction
    #
    # The launch floor is about $4,200, so a $6k token is 1.4x -- noise, not a
    # runner, and calling it one is what made this panel untrustworthy. $12k is
    # where the rate first clears a useful bar (32%, a 64x lift over $4-6k).
    RUN_LO, RUN_HI = 12_000, 48_000
    RUN_PRIME_LO, RUN_PRIME_HI = 20_000, 35_000     # the 86% zone
    RUN_WINDOW = 300.0                   # 5 min of tape

    def on_curve_trade(self, lg, is_buy):
        """
        Curve tape: volume, side and distinct wallets, straight off the stream.

        This is the pre-graduation equivalent of a swap feed and it costs nothing --
        these events are already subscribed for the smart-money watch. Price is NOT
        taken from here: the curve publishes no reserves per trade, so mcap comes
        from the periodic curve_metrics read instead, and only the FLOW is measured
        here. Mixing a live flow window with a stale price would be worse than using
        neither.
        """
        addr = lg["address"].lower()
        d = lg["data"][2:]
        if len(d) < 64:
            return
        quote_wei = int(d[0:64], 16)
        usd = quote_wei / 1e18 * ETH_USD
        w = ("0x" + lg["topics"][2][-40:]).lower() if len(lg["topics"]) > 2 else None
        now = time.time()
        with self.lock:
            r = self.curveflow.setdefault(addr, {"trades": [], "wallets": set(),
                                                 "first_seen": now})
            r["trades"].append((now, is_buy, usd))
            if is_buy and w:
                r["wallets"].add(w)
            cut = now - self.RUN_WINDOW
            r["trades"] = [t for t in r["trades"] if t[0] >= cut]
            known = addr in self.detail
        # A COIN THAT WAKES UP. Curve tape arrives on the stream for every token,
        # including ones launched hours ago that were never enriched or were evicted
        # after going quiet. runners() reads self.detail, so without this an old coin
        # that suddenly runs is invisible -- the exact case of a slow burner finally
        # catching a bid. Requiring real tape (not one stray trade) keeps this from
        # re-adding every dead row that gets a dust buy.
        # THRESHOLD MUST MATCH runners(), or curves qualify as runners while having
        # no row to be shown in. Measured live: 47 curves had tape while only 18 had
        # a row -- 29 were moving and structurally invisible, because the wake-up bar
        # (5 trades / $400) was stricter than the runner bar (3 trades / $300).
        if not known and is_buy and self.is_moving(addr, min_trades=3, min_usd=300.0):
            with self.lock:
                if addr not in self.detail:
                    self.detail[addr] = {"curve": addr, "block": self.last_head,
                                         "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                                         "state": "enriching", "flags": [], "good": [],
                                         "woke_up": True, "pre_grad": True}
                    self.q.appendleft(addr)      # jump the queue -- it is running NOW
                    self.stats["woke"] = self.stats.get("woke", 0) + 1
            if len(self.curveflow) > 1200:
                # evict by LAST TRADE, not first seen. A coin that has been running
                # for an hour has the oldest first_seen and the freshest tape, and
                # dropping it here would blind is_moving() -- which then lets _evict
                # discard the row as well.
                cut = time.time() - self.RUN_WINDOW
                stale = [k for k, v in self.curveflow.items()
                         if not v["trades"] or v["trades"][-1][0] < cut]
                for k in (stale or [min(self.curveflow,
                                        key=lambda x: self.curveflow[x]["first_seen"])]):
                    self.curveflow.pop(k, None)
                    if len(self.curveflow) <= 1200:
                        break

    def _evict(self):
        """
        Drop the oldest row that is NOT currently running.

        Plain FIFO eviction threw away live runners: a coin that had been climbing
        for twenty minutes was exactly the oldest row, so it was first out -- and it
        vanished from the RUNNERS panel at the moment it was most worth watching.
        Age is the wrong thing to evict on; being finished is the right thing.

        Falls back to true-oldest only if EVERY row is running, so the cap still
        holds and memory cannot grow without bound.
        """
        now = time.time()
        cut = now - self.RUN_WINDOW
        for c in list(self.detail.keys()):
            r = self.curveflow.get(c)          # read directly: caller holds the lock
            live = bool(r and len(r["trades"]) >= 3
                        and sum(t[2] for t in r["trades"]) >= 100.0
                        and r["trades"][-1][0] >= cut)
            if not live:
                self.detail.pop(c, None)
                return
        self.detail.popitem(last=False)

    def _mark_progress(self, e):
        """
        Annotate how far up the curve a row is, and flag the ones that are past
        the point where the trade pays. Measured: pf 2.79 entering at 10% of
        supply sold, 1.32 at 25%, 0.77 at 40%.

        A row with unknown progress is NOT flagged -- unreadable must not read as
        "fine", and it must not read as "brand new" either.
        """
        # sizing depends only on liquidity, so it must run even when progress is
        # unreadable -- an early return here silently left every unknown-progress
        # row on the flat stake
        sized, pct = stake_for(e, self.args.stake)
        e["sized_stake_usd"] = sized
        if pct is not None:
            e["stake_pct_of_pot"] = pct

        prog = e.get("curve_progress")
        if prog is None:
            return
        if prog >= CURVE_LATE_PCT:
            e.setdefault("flags", []).append(f"LATE {prog:.0%}")
        elif prog <= 0.15:
            e.setdefault("good", []).append(f"EARLY {prog:.0%}")

    def is_clean(self, e):
        """
        No measured kill flag. 6.03% reach near-grad vs 0.71% flagged.

        FLAT-CLIPS earns its place here on the cheapest terms of any gate so far.
        Replayed over the 883 taped curves that got past the heating level:

            past heating                 883 rows   64 graduations   7.25%
            + exclude FLAT-CLIPS         740 rows   63 graduations   8.51%

        It removes 16.2% of rows and costs exactly ONE graduation out of 64. A gate
        that discards a sixth of the board for a single missed winner is close to
        free, which is not true of the other two flags here.
        """
        fl = e.get("flags") or []
        return not any(f == "SOLO-EXEMPT" or f.startswith("CONCENTRATED")
                       or f.startswith("FLAT-CLIPS") for f in fl)

    def is_prime(self, e):
        """
        The highest-conviction view: clean of kill flags AND past the heating
        threshold. Measured on 47,048 curves against a 3.33% base:

            ALL launches        3.33%   1.00x   27 rows/hr
            clean only          6.03%   1.81x   13
            past heating       17.86%   5.37x    5
            clean AND heating  21.74%   6.53x    4     <- this
            + spread           27.08%   8.14x    0     (192 rows total, too rare)

        Roughly one in five of these reaches near-graduation, at four rows an hour.
        Tighter than that stops producing candidates at all, which is a worse
        failure than showing a few too many.
        """
        c = e.get("curve")
        if not (c and "heating" in (self.crossed.get(c) or set())
                and self.is_clean(e)):
            return False
        # NOT TOO FAR UP THE CURVE. Added after replaying 1,173 tapes: entering at
        # 10% of supply sold is pf 2.79, at 40% it is 0.77. The old view had no
        # ceiling at all, so a curve 80% of the way to graduation ranked the same
        # as one at 5% -- and near_grad, the level PRIME is tuned to predict, sits
        # at 75.5% of supply sold, where breakeven needs a ~30% graduation rate.
        #
        # Unknown progress does NOT disqualify. Gating on a value that is often
        # unread would empty the table, which is the failure mode this filter
        # already hit once.
        prog = e.get("curve_progress")
        return prog is None or prog < CURVE_LATE_PCT

    def is_moving(self, curve, min_trades=3, min_usd=100.0):
        """Any real tape in the last 5 minutes? Cheap — no RPC, pure stream state."""
        if not curve:
            return False
        with self.lock:
            r = self.curveflow.get(curve)
            if not r:
                return False
            tr = r["trades"]
            return len(tr) >= min_trades and sum(t[2] for t in tr) >= min_usd

    def violence(self, e, window=90.0):
        """
        Percent change in the curve's raised amount over `window` seconds.

        None when there is not enough history -- two samples 25s apart is not a
        trend, and calling it one would put a made-up number next to real ones.
        """
        h = e.get("eth_hist") or []
        if len(h) < 3:
            return None
        now = time.time()
        recent = [x for x in h if now - x[0] <= window]
        if len(recent) < 3:
            recent = h[-3:]
        first, last = recent[0][1], recent[-1][1]
        if not first:
            return None
        return 100.0 * (last - first) / first

    def curve_motion(self, curve):
        """
        (buy_usd, sell_usd, buy_share, n_trades, n_wallets) over the window.
        None when the tape is too thin to mean anything.
        """
        with self.lock:
            r = self.curveflow.get(curve)
            if not r:
                return None
            tr = list(r["trades"])
            nw = len(r["wallets"])
        if len(tr) < 4:
            return None
        b = sum(t[2] for t in tr if t[1])
        sl = sum(t[2] for t in tr if not t[1])
        tot = b + sl
        return b, sl, (b / tot if tot else 0.0), len(tr), nw

    def postgrad_runners(self, n=6):
        """
        GRADUATED tokens that are moving, in the $50k-$150k band.

        The curve tops out near $48k, so everything above that is a v4 pool -- and
        that is where a token doing 2x after graduation actually lives. `runners()`
        is gated on `pre_grad`, so this whole population was invisible: the v4 swap
        stream has been collected since it was wired in and nothing ever read it.

        Measured on 197 post-graduation paths: 35% touch >=2x, and the ones that do
        take a median 14 minutes. That is slow enough to act on, unlike the curve.
        """
        out = []
        with self.lock:
            rs = list(self.runner.values())
        for r in rs:
            m = self.postgrad_motion(r)
            if not m:
                continue
            mcap, pct, bshare, vol, ntr = m
            if not (50_000 <= mcap <= 150_000):
                continue
            if pct < 5.0 or bshare < 0.55 or vol < 500:
                continue
            out.append(dict(r, _mcap=mcap, _pct=pct, _buy=bshare, _vol=vol, _n=ntr))
        out.sort(key=lambda x: -x["_pct"])
        return out[:n]

    def runners(self, n=8):
        """
        Curves in the band that are MOVING -- volume, buy pressure and fresh wallets
        at once. Market cap is the gate; motion is the signal.

        A token sitting at $20k with no tape is not a runner, and the whole point of
        this panel is that size alone never says which is which.
        """
        out = []
        with self.lock:
            rows = [e for e in self.detail.values()
                    if e.get("pre_grad") and not e.get("graduated") and e.get("curve")]
        for e in rows:
            # TAPE FIRST, MCAP SECOND.
            #
            # This used to read `mc = e.get("mcap") or 0` and then require the band,
            # so a token whose mcap was UNKNOWN scored 0 and was dropped -- however
            # hard it was being bought. That is backwards: the tape arrives live on
            # the log stream and cannot lag, while mcap comes from a probe that
            # fails for exactly the tokens worth catching.
            #
            # Measured: of 12 curves that graduated in one hour, 8 were sitting at
            # verdict NO-BID with mcap=None. Median launch->graduation is 2.2 MIN
            # and the fastest was 14 blocks -- 1.4 seconds. A token can run its
            # whole curve before enrichment finishes, and the probe then reverts
            # because the curve is nearly full, which is the opposite of "dead".
            #
            # So: strong tape qualifies on its own. An unknown mcap is shown as
            # unknown rather than treated as zero.
            m = self.curve_motion(e["curve"])
            if not m:
                continue
            buy_usd, sell_usd, bshare, ntr, nw = m
            if buy_usd < 300 or bshare < 0.6 or nw < 3:
                continue                 # not moving, whatever the size
            mc = e.get("mcap")
            if mc is not None and not (self.RUN_LO <= mc <= self.RUN_HI):
                continue                 # known size, and it is out of the band
            mc = mc or 0
            out.append(dict(e, _mcap=mc, _buy=buy_usd, _sell=sell_usd,
                            _bshare=bshare, _n=ntr, _w=nw,
                            _net=buy_usd - sell_usd,
                            _vel=self.violence(e)))
        # rank by VIOLENCE first when it is known -- that is the measured signal.
        # Net dollars breaks ties and carries rows whose history is still too short.
        out.sort(key=lambda x: (-(x["_vel"] if x["_vel"] is not None else -1e9),
                                -x["_net"]))
        return out[:n]

    def seed_crossed(self, log=print):
        """
        Reload threshold crossings from the journal at startup.

        `crossed` is built live as curves pass 0.10/2.0 ETH, so on a fresh start it
        is EMPTY -- and the PRIME view requires "heating in crossed". The result was
        a completely blank main table for however long it took a token to cross
        while we watched, which reads as a broken screen, not as a strict filter.
        The journal already holds every crossing; there is no reason to relearn them.
        """
        path = os.path.join(DATA, "grad_forward.jsonl")
        if not os.path.exists(path):
            return
        n = 0
        try:
            for line in open(path):
                try:
                    x = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                c, lv = x.get("curve"), x.get("level")
                if c and lv:
                    self.crossed.setdefault(c, set()).add(lv)
                    n += 1
        except Exception:  # noqa: BLE001
            return
        log(f"crossings reloaded: {len(self.crossed):,} curves from {n:,} records",
            flush=True)

    def seed_pools(self, blocks=60_000, log=print):
        """
        Backfill poolId -> token from recent pool creations.

        Learning pools only from the live stream means we know about pools created
        SINCE startup -- but swaps arrive for pools created hours ago, so every one
        of them is unattributable and the post-graduation panel stays empty.
        Measured: pools_known reached 9 after 80s while postgrad_tracked stayed 0.

        One scan at boot fixes it. ~60k blocks is about 100 minutes of chain, which
        comfortably covers anything still trading in the $50k-$150k band.
        """
        try:
            head = self.last_head or head_block()
            lg = get_logs({"fromBlock": hex(max(0, head - blocks)), "toBlock": hex(head),
                           "address": POOL_MANAGER, "topics": [T_V4_INITIALIZE]})
            n = 0
            for e in lg:
                self.on_v4_init(e)
                n += 1
            log(f"pool map seeded: {len(self.pool2tok)} pools from {n} creations",
                flush=True)
        except Exception as ex:  # noqa: BLE001
            log(f"pool seed failed: {str(ex)[:60]}", flush=True)

    def on_v4_init(self, lg):
        """
        Learn poolId -> token from the pool's own creation event.

        Layout verified against live logs: topics are
        [Initialize, poolId, currency0, currency1].

        Orientation is decided here once, from which side is the QUOTE. Native
        (0x0) and WETH are quotes; the token is the other currency. Getting this
        wrong inverts every price downstream, which is the bug that corrupted 13.5%
        of the RH paper trades.
        """
        tp = lg.get("topics") or []
        if len(tp) < 4:
            return
        pid = tp[1]
        c0 = ("0x" + tp[2][-40:]).lower()
        c1 = ("0x" + tp[3][-40:]).lower()
        QUOTES = {"0x" + "0" * 40}      # native ETH is currency0 on Pons pools
        if c0 in QUOTES:
            tok, tok_is_c1 = c1, True
        elif c1 in QUOTES:
            tok, tok_is_c1 = c0, False
        else:
            # neither side is a known quote -- record it but do not guess a price
            return
        with self.lock:
            if pid in self.pool2tok:
                return
            self.pool2tok[pid] = {"token": tok, "curve": None, "symbol": None,
                                  "tok_is_c1": tok_is_c1}
            if len(self.pool2tok) > 1500:
                self.pool2tok.pop(next(iter(self.pool2tok)))

    def on_v4_swap(self, lg):
        """
        Post-graduation tape: volume, side, distinct buyers and price, all decoded
        from the Swap log itself.

        Orientation is read from the pool's stored currencies, never assumed. That
        assumption is what corrupted 13.5% of the RH paper trades once already:
        v4 Swap amounts are from the POOL's perspective and always carry opposite
        signs, so the pool RECEIVES the positive currency. For a token sitting on
        currency1, a BUY is amount0>0 / amount1<0.
        """
        pid = lg["topics"][1] if len(lg.get("topics") or []) > 1 else None
        meta = self.pool2tok.get(pid)
        if not meta:
            return                       # a pool we do not follow
        d = lg["data"][2:]
        if len(d) < 192:
            return
        a0, a1 = s256(d[0:64]), s256(d[64:128])
        sqrt = int(d[128:192], 16)
        if not sqrt:
            return
        tok_is_c1 = meta.get("tok_is_c1", True)
        tok_amt = a1 if tok_is_c1 else a0
        quote_amt = a0 if tok_is_c1 else a1
        is_buy = tok_amt < 0             # pool sent the token out -> someone bought
        # price of the TOKEN in quote units, oriented
        p = (sqrt / (2 ** 96)) ** 2
        price = (1 / p) if tok_is_c1 else p
        if not price:
            return
        mcap = price * 1e9 * ETH_USD     # every Pons token mints 1e9
        usd = abs(quote_amt) / 1e18 * ETH_USD
        now = time.time()
        with self.lock:
            r = self.runner.setdefault(meta["token"], {
                "token": meta["token"], "curve": meta.get("curve"),
                "symbol": meta.get("symbol"), "trades": [], "samples": [],
                "buyers": set(), "first_seen": now})
            r["symbol"] = r.get("symbol") or meta.get("symbol")
            r["mcap"] = mcap
            r["trades"].append((now, is_buy, usd))
            r["samples"].append((now, mcap))
            if is_buy and lg.get("transactionHash"):
                r["buyers"].add(lg["transactionHash"][:20])
            cut = now - self.RUN_WINDOW
            r["trades"] = [t for t in r["trades"] if t[0] >= cut]
            known = addr in self.detail
        # A COIN THAT WAKES UP. Curve tape arrives on the stream for every token,
        # including ones launched hours ago that were never enriched or were evicted
        # after going quiet. runners() reads self.detail, so without this an old coin
        # that suddenly runs is invisible -- the exact case of a slow burner finally
        # catching a bid. Requiring real tape (not one stray trade) keeps this from
        # re-adding every dead row that gets a dust buy.
        # THRESHOLD MUST MATCH runners(), or curves qualify as runners while having
        # no row to be shown in. Measured live: 47 curves had tape while only 18 had
        # a row -- 29 were moving and structurally invisible, because the wake-up bar
        # (5 trades / $400) was stricter than the runner bar (3 trades / $300).
        if not known and is_buy and self.is_moving(addr, min_trades=3, min_usd=300.0):
            with self.lock:
                if addr not in self.detail:
                    self.detail[addr] = {"curve": addr, "block": self.last_head,
                                         "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                                         "state": "enriching", "flags": [], "good": [],
                                         "woke_up": True, "pre_grad": True}
                    self.q.appendleft(addr)      # jump the queue -- it is running NOW
                    self.stats["woke"] = self.stats.get("woke", 0) + 1
            r["samples"] = [x for x in r["samples"] if x[0] >= cut]
            if len(self.runner) > 400:
                oldest = min(self.runner, key=lambda k: self.runner[k]["first_seen"])
                self.runner.pop(oldest, None)

    def postgrad_motion(self, r):
        """
        Post-graduation motion, kept for later use. NOT the RUNNERS panel source.

        The curve is where the hunt actually happens ($4.2k-$48k mcap, verified
        against GMGN), so `runners()` reads curve tape. This stayed because the v4
        swap stream is already subscribed and the data is free -- but it defined a
        second `runners()` further down the class, which silently SHADOWED the curve
        one. Python takes the last definition, so the panel was reading an empty
        post-graduation dict and showing nothing.
        """
        tr, sm = r.get("trades") or [], r.get("samples") or []
        if len(tr) < 4 or len(sm) < 2:
            return None
        vol = sum(t[2] for t in tr)
        buys = sum(1 for t in tr if t[1])
        first, last = sm[0][1], sm[-1][1]
        pct = 100 * (last - first) / first if first else 0.0
        return last, pct, (buys / len(tr)), vol, len(tr)

    def _followed(self, curve):
        """
        Is this curve one we are actually following? Returns a reason, or None.

        The alert is gated deliberately. ~1.6% of 21 launches/min still graduates
        several times an hour, and a phone that buzzes for every graduation on the
        chain is a phone you mute -- which costs you the one that mattered.
        """
        if curve in getattr(self, "open_positions", set()):
            return "you hold this"
        if curve in self.hot_active:
            return "flagged HOT"
        if "near_grad" in (self.crossed.get(curve) or set()):
            return "was in NEAR GRAD"
        if self.smart and self.smart.count(curve) >= 2:
            return f"{self.smart.count(curve)} smart wallets were in it"
        return None

    def _maybe_grad_alert(self, curve, blk):
        why = self._followed(curve)
        if not why or getattr(self.args, "no_discord", False):
            return
        threading.Thread(target=self._grad_alert, args=(curve, blk, why),
                         daemon=True).start()

    def _grad_alert(self, curve, blk, why):
        try:
            import alerts
            with self.lock:
                e = dict(self.detail.get(curve) or {})
            meta = self.hot_meta.get(curve) or {}
            ok, detail = alerts.send_graduated({
                "symbol": e.get("symbol") or meta.get("symbol") or "?",
                "token": e.get("token") or meta.get("token") or curve,
                "curve": curve, "block": blk, "why": why,
                "peak_eth": e.get("curve_peak_eth"),
                "n_smart": self.smart.count(curve) if self.smart else 0,
                "mode": "ARMED" if getattr(self.args, "arm", False) else "DISARMED",
            })
            if not ok:
                self.stats["alerts_failed"] = self.stats.get("alerts_failed", 0) + 1
        except Exception as ex:  # noqa: BLE001
            self.stats["alerts_failed"] = self.stats.get("alerts_failed", 0) + 1
            self.msg = f"grad alert error: {str(ex)[:50]}"

    def _resolve_curve(self, curve):
        """(symbol, token) straight from the curve — for a hot token not yet in the table."""
        r = rpc_batch([("eth_call", [{"to": curve, "data": SEL_TOKEN}, "latest"])])
        tok = call_addr((r[0] or {}).get("result"))
        if not tok:
            return None, None
        r2 = rpc_batch([("eth_call", [{"to": tok, "data": SEL_SYMBOL}, "latest"])])
        return call_str((r2[0] or {}).get("result")), tok

    def _token_for(self, key):
        with self.lock:
            return (self.detail.get(key) or {}).get("token")

    def _sym_for(self, key):
        with self.lock:
            e = self.detail.get(key)
        return (e or {}).get("symbol")

    def autovet_worker(self):
        """
        Pre-run the DYOR checks on new rows so the answer is already there.

        Vetting on demand costs 10-20s, which is exactly the latency that made
        manual entry negative-EV. Doing it as rows arrive means the honeypot
        verdict is cached before the row is ever selected. One at a time on
        purpose: launches arrive far faster than the RPC can be probed, and a
        thundering herd would starve the enrichment queue that feeds the table.
        """
        while self.running:
            curve = None
            with self.lock:
                while self.autovet_q:
                    c = self.autovet_q.popleft()
                    if c not in self.autovet_done:
                        curve = c
                        break
            if curve is None:
                time.sleep(0.5)
                continue
            with self.lock:
                e = self.detail.get(curve)
            tok = (e or {}).get("token")
            if not tok or run_investigation is None:
                continue
            self.autovet_done.add(curve)
            self.inv.setdefault(curve, {"state": "running", "probes": [],
                                        "symbol": (e or {}).get("symbol")})
            try:
                ctx, probes = run_investigation(tok, self.args.stake)
                ctx.pop("registry", None)
                ctx.pop("ds_pair", None)
                self.inv[curve] = {"state": "done", "probes": probes, "ctx": ctx,
                                   "symbol": (e or {}).get("symbol"), "auto": True}
                # lift the ENTRY metrics onto the row so they are visible without
                # opening the investigation panel
                with self.lock:
                    row = self.detail.get(curve)
                    if row:
                        for k in ("mcap", "vol_h1", "vol_h24", "n_holders", "price_usd"):
                            if ctx.get(k) is not None:
                                row[k] = ctx[k]
            except Exception as ex:  # noqa: BLE001
                self.inv[curve] = {"state": f"error: {ex}"[:60], "probes": [],
                                   "symbol": (e or {}).get("symbol"), "auto": True}
            if self.smart:
                self.smart.maybe_reload()

    # Graduation happens at ~4.0 ETH raised -- MEASURED off real CurveCompleted
    # events, not assumed: 3.9359 / 4.0221 / 4.0939 across sampled graduations, and
    # the venue exposes no readable graduationThreshold() (that selector returns
    # nothing on every live curve).
    GRAD_ETH = 4.0
    HEATING_ETH = 0.10          # 2.5% of the curve, ~$4.8k mcap
    NEAR_GRAD_ETH = 2.0         # 50% of the curve, ~$20k mcap

    # THRESHOLDS IN TOKEN PROGRESS, NOT ETH RAISED.
    #
    # The ETH thresholds above are BLIND TO 37% OF THE MARKET. They were read from
    # eth_getBalance, which is ~0 forever on a curve quoted in USDG or a tokenized
    # equity (GOOGL / NVDA / SPY / AAPL / SPCX) because that curve holds its raise
    # as an ERC-20. Those curves never crossed heating, never entered `crossed`,
    # and `is_prime` requires "heating in crossed" -- so they were structurally
    # invisible to the highest-conviction view. Proof: all 64 graduations in the
    # 1,173-curve tape set are ETH-quoted, while 37.4% of the graduation journal is
    # NOT ETH-quoted. Both can only be true if the detector cannot see them.
    #
    # sellableTokens() works on every curve regardless of quote, and token progress
    # is the thing that actually predicts the return (pf 2.79 entering at 10% of
    # supply sold, 1.32 at 25%, 0.77 at 40%). Converted from the ETH levels by
    # measuring where each lands on ETH-quoted graduated curves:
    #     0.1 ETH  -> 9.3% of supply sold
    #     2.0 ETH  -> 75.5% of supply sold
    HEATING_PROG = 0.093
    NEAR_GRAD_PROG = 0.755

    def progress_worker(self, period=25.0):
        """
        Keep curve balances CURRENT for the near-graduation panel.

        The balance captured at enrich time is a snapshot of one moment; a curve that
        is "taking off" is by definition one whose balance is moving. Re-reading is
        the only way the panel means anything.

        Bounded on purpose: newest rows only, batched, and pre-graduation curves
        only. Measured context for why the cap is safe -- of 220 sampled live curves,
        177 never passed 0.01 ETH and none passed 0.5, so almost every row is flat
        and the interesting ones are a handful.
        """
        while self.running:
            try:
                with self.lock:
                    cands = [c for c, e in list(self.detail.items())[-400:]
                             if e.get("pre_grad") and not e.get("graduated")]
                for i in range(0, len(cands), 25):
                    chunk = cands[i:i + 25]
                    # TWO reads per curve, interleaved: the ETH balance still drives
                    # the liquidity column, but sellableTokens() is what the crossing
                    # test uses, because it is the only one that works on a curve
                    # quoted in something other than native ETH.
                    calls = []
                    for c in chunk:
                        calls.append(("eth_getBalance", [c, "latest"]))
                        calls.append(("eth_call", [{"to": c, "data": SEL_SELLABLE},
                                                   "latest"]))
                    res = rpc_batch(calls)
                    for j, c in enumerate(chunk):
                        r = res[2 * j] if 2 * j < len(res) else None
                        rs = res[2 * j + 1] if 2 * j + 1 < len(res) else None
                        sellable = call_int((rs or {}).get("result"))
                        prog = (curve_progress({"curve_sellable": sellable})
                                if sellable is not None else None)
                        v = (r or {}).get("result")
                        if v:
                            with self.lock:
                                e0 = self.detail.get(c)
                                if e0 is not None and prog is not None:
                                    e0["curve_progress"] = prog
                                    e0["curve_sellable"] = sellable
                        if prog is not None:
                            with self.lock:
                                e0 = self.detail.get(c)
                                prevp = (e0 or {}).get("_prev_prog")
                                if e0 is not None:
                                    e0["_prev_prog"] = prog
                            self._note_crossing_prog(c, prevp, prog)
                        if not v:
                            continue           # unreadable is not zero
                        eth = int(v, 16) / 1e18
                        with self.lock:
                            e = self.detail.get(c)
                            if not e:
                                continue
                            prev = e.get("curve_eth")
                            e["curve_eth"] = eth
                            e["curve_peak_eth"] = max(e.get("curve_peak_eth") or 0.0, eth)
                            # keep a short series so VIOLENCE (rate of move) can be
                            # measured, not just level. Measured on 72 post-grad
                            # paths: tokens in the top third by 60s velocity peaked
                            # at a 2.33x median vs 1.57x for the middle third, and
                            # hit 2x 58% vs 42%. Level says where it is; velocity
                            # says whether anything is happening.
                            hist = e.setdefault("eth_hist", [])
                            hist.append((time.time(), eth))
                            if len(hist) > 24:
                                del hist[:-24]
                        self._note_crossing(c, prev, eth)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(period)

    # ---------------------------------------------------------- rolling refresh
    REFRESH_EVERY = 20.0        # seconds between passes
    REFRESH_COOLDOWN = 75.0     # min seconds before the same curve is rescanned
    REFRESH_MAX = 6             # curves per pass -- one getLogs each, so bounded

    def refresh_worker(self):
        """
        Re-read the trade tape of curves that are actually MOVING.

        THE BUG THIS FIXES. Every buyer-derived column was frozen at discovery.
        enrich_curve runs seconds after launch, when a curve genuinely has 0-1
        buyers, and the row was never scanned again -- so n_buyers, top1_share,
        the flow sparkline, the clip score and the bad-buyer share all showed the
        state at birth forever.

        Measured on 25 sampled curves: the board understated the buyer count on
        21 of them, median 10 missed, max 79 -- one row read 0 on screen while the
        chain showed 79 buyers. That single staleness explains four separate
        "signals that never fire": FLOW blank on 86% of rows, CLIPPED needing 15
        buys, DIRTY-BUYERS needing 3 scored ones, and the entry-timing gap.

        BOUNDED ON PURPOSE. One getLogs per curve per pass is real cost, so this
        refreshes only pre-graduation curves that have MOVED since the last look,
        newest first, at most REFRESH_MAX per pass and never more often than
        REFRESH_COOLDOWN. A curve sitting still needs no rescan -- its tape has
        not changed.
        """
        while self.running:
            try:
                now = time.time()
                with self.lock:
                    cands = []
                    for c, e in list(self.detail.items()):
                        if e.get("graduated") or not e.get("pre_grad"):
                            continue
                        if now - (e.get("_refreshed") or 0) < self.REFRESH_COOLDOWN:
                            continue
                        # "moving" = progress advanced since the last refresh, or
                        # never refreshed at all (the common case right after birth)
                        moved = (e.get("_refreshed") is None
                                 or (e.get("curve_progress") or 0)
                                 > (e.get("_refresh_prog") or 0) + 1e-9)
                        if moved:
                            cands.append((e.get("block") or 0, c))
                    cands.sort(reverse=True)          # newest first
                    cands = [c for _b, c in cands[:self.REFRESH_MAX]]
                head = self.last_head or head_block()
                for c in cands:
                    with self.lock:
                        e = self.detail.get(c)
                        blk = (e or {}).get("block")
                    if not e or not blk:
                        continue
                    try:
                        logs = curve_trade_logs(c, blk, head, True)
                    except Exception:  # noqa: BLE001
                        continue      # a failed rescan must not blank live fields
                    if not logs:
                        with self.lock:
                            e["_refreshed"] = time.time()
                        continue
                    fresh = {"flags": [], "good": [],
                             "total_fee_bps": e.get("total_fee_bps") or 0}
                    try:
                        tape_metrics(fresh, logs)
                    except Exception:  # noqa: BLE001
                        continue
                    with self.lock:
                        # keep flags that do NOT come from the tape (fee, code,
                        # dev history, LATE) and replace only the tape-derived ones
                        keep_f = [f for f in (e.get("flags") or [])
                                  if f.startswith(("FEE", "NONSTD", "SERIAL",
                                                   "serial", "BOT-DEV", "LATE",
                                                   "SCAN-TIMEOUT"))]
                        keep_g = [g for g in (e.get("good") or [])
                                  if g.startswith(("FIRST", "DEV", "LOW-FEE",
                                                   "EARLY", "o1"))]
                        for k, v in fresh.items():
                            if k in ("flags", "good", "total_fee_bps"):
                                continue
                            e[k] = v
                        e["flags"] = keep_f + [f for f in fresh["flags"]
                                               if f not in keep_f]
                        e["good"] = keep_g + [g for g in fresh["good"]
                                              if g not in keep_g]
                        e["_refreshed"] = time.time()
                        e["_refresh_prog"] = e.get("curve_progress") or 0
                        # feed the flow sparkline the first-buy times we just read
                        fb = fresh.pop("buy_firsts", None) or {}
                        bf = self.buyflow.setdefault(c, {})
                        for w, b in fb.items():
                            ts = time.time() - max(0, (head - b)) * BLOCK_TIME
                            cur = bf.get(w)
                            if cur is None:
                                bf[w] = [1, 0, ts]
                            elif ts < cur[2]:
                                cur[2] = ts
                    self.stats["refreshed"] = self.stats.get("refreshed", 0) + 1
                    # JOURNAL THE REFRESHED ROW. The feed was written only at
                    # enrich, so every record in it was a birth snapshot and the
                    # file could never show how a curve developed -- which is the
                    # exact question the tape work needs answered. Marked
                    # refresh=true so a reader can tell a rescan from a discovery.
                    try:
                        with open(self.journal, "a") as jf:
                            jf.write(json.dumps(
                                {"ts": datetime.now(timezone.utc).isoformat(),
                                 "refresh": True,
                                 **{k: x for k, x in e.items()
                                    if k not in ("flags", "good")
                                    and not k.startswith("_")},
                                 "flags": e.get("flags"),
                                 "good": e.get("good")}) + "\n")
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as ex:  # noqa: BLE001
                # A worker that fails silently is how this project lost a day to a
                # frozen enrich loop. Surface it on the status line instead.
                self.stats["refresh_err"] = f"{type(ex).__name__}: {ex}"[:80]
            time.sleep(self.REFRESH_EVERY)

    # -------------------------------------------------- websocket failover
    WS_SILENT_AFTER = 45.0      # seconds of silence before we stop trusting the stream
    POLL_EVERY = 6.0            # seconds between catch-up polls
    POLL_MAX_BLOCKS = 6000      # ~10 min of chain; never replay hours on resume

    def poll_worker(self):
        """
        Catch launches by polling when the websocket goes quiet.

        THE FAILURE THIS EXISTS FOR. wss://robinhood-rpc.publicnode.com accepts an
        eth_subscribe, returns a valid subscription id, and then delivers NOTHING.
        Measured 2026-09-17: 0 messages in 20s where the same subscription had
        returned 377 messages in 20s earlier the same day. There is no error and no
        disconnect, so ws_worker sat in recv(), timed out after 90s, reconnected,
        re-subscribed, and went back to waiting -- forever, silently.

        Cost of that: new-launch detection stopped dead for 151 minutes while the
        process looked perfectly healthy, refreshes kept flowing, and the board kept
        rendering stale rows. Checked against chain: 13 launches in the window, 0
        captured -- 100% missed.

        It is the only WS host that works at all (the primary RPC refuses upgrade),
        so there is nothing to fail over TO. Polling is the failover. eth_getLogs on
        the primary endpoint is healthy, and at ~0.1s blocks a 6s poll loses nothing
        that matters.

        Runs ONLY while the stream is silent, so the normal path stays push-based
        and buyflow is not double-counted. It hands logs to the same on_log(), so
        there is one code path for both sources.
        """
        topics = [t for v in VENUES.values() if v["enabled"]
                  for t in (v["new_topic"], v["grad_topic"]) if t]
        topics.append(T_CURVE_BUY)
        topics.append(T_CURVE_SELL)
        topics.append(T_V4_INITIALIZE)
        last = None
        while self.running:
            try:
                quiet = time.time() - (self.ws_last_msg or 0)
                if quiet < self.WS_SILENT_AFTER:
                    time.sleep(self.POLL_EVERY)
                    continue
                head = head_block()
                if not head:
                    time.sleep(self.POLL_EVERY)
                    continue
                lo = max((last + 1) if last else head - 300,
                         head - self.POLL_MAX_BLOCKS)
                if lo > head:
                    time.sleep(self.POLL_EVERY)
                    continue
                logs = get_logs({"fromBlock": hex(lo), "toBlock": hex(head),
                                 "topics": [topics]})
                self.stats["polled"] = self.stats.get("polled", 0) + len(logs)
                self.stats["ws_quiet_s"] = round(quiet)
                for lg in sorted(logs, key=lambda x: (int(x["blockNumber"], 16),
                                                      int(x.get("logIndex", "0x0"), 16))):
                    try:
                        self.on_log(lg)
                    except Exception:  # noqa: BLE001
                        pass
                last = head
            except Exception as ex:  # noqa: BLE001
                self.stats["poll_err"] = f"{type(ex).__name__}: {ex}"[:80]
            time.sleep(self.POLL_EVERY)

    def _note_crossing_prog(self, curve, prev, prog):
        """
        Journal a threshold crossing measured in TOKEN PROGRESS.

        This is the quote-agnostic twin of _note_crossing. It exists because the
        ETH-balance version cannot see a curve quoted in USDG or a tokenized
        equity, which is 37.4% of graduations -- those rows never entered
        `crossed` and so could never be PRIME.

        Rows are journalled with method="progress" so they stay distinguishable
        from the ETH-measured history already in the file. Mixing them silently
        would corrupt the forward test that is the point of the journal.
        """
        for name, lvl in (("heating", self.HEATING_PROG),
                          ("near_grad", self.NEAR_GRAD_PROG)):
            if prog < lvl or (prev is not None and prev >= lvl):
                continue
            with self.lock:
                e = dict(self.detail.get(curve) or {})
                seen = self.crossed.setdefault(curve, set())
                if name in seen:
                    continue
                seen.add(name)
            try:
                with open(os.path.join(DATA, "grad_forward.jsonl"), "a") as f:
                    f.write(json.dumps({
                        "ts": time.time(), "level": name, "method": "progress",
                        "threshold_prog": lvl, "progress": round(prog, 4),
                        "curve": curve, "token": e.get("token"),
                        "symbol": e.get("symbol"), "block": self.last_head,
                        "mcap": e.get("mcap"), "n_buyers": e.get("n_buyers"),
                        "tax_bps": e.get("tax_bps"),
                        "n_smart": self.smart.count(curve) if self.smart else 0,
                    }) + "\n")
            except Exception:  # noqa: BLE001
                pass

    def _note_crossing(self, curve, prev, now_eth):
        """
        Journal the FIRST time a curve crosses each threshold.

        This is the forward test. Nothing here yet shows that reaching a level
        predicts graduating -- the population is measured, the conditional
        probability is not. Logging crossings as they happen is what makes that
        answerable later, and it is the same discipline that killed the deployer
        filter and the dip strategy rather than shipping them on a plausible story.
        """
        for name, lvl in (("heating", self.HEATING_ETH), ("near_grad", self.NEAR_GRAD_ETH)):
            if now_eth >= lvl and (prev is None or prev < lvl):
                with self.lock:
                    e = dict(self.detail.get(curve) or {})
                    seen = self.crossed.setdefault(curve, set())
                    if name in seen:
                        continue
                    seen.add(name)
                try:
                    with open(os.path.join(DATA, "grad_forward.jsonl"), "a") as f:
                        f.write(json.dumps({
                            "ts": time.time(), "level": name, "threshold_eth": lvl,
                            "curve": curve, "token": e.get("token"),
                            "symbol": e.get("symbol"), "eth": round(now_eth, 6),
                            "block": self.last_head,
                            "mcap": e.get("mcap"), "n_buyers": e.get("n_buyers"),
                            "tax_bps": e.get("tax_bps"),
                            "n_smart": self.smart.count(curve) if self.smart else 0,
                        }) + "\n")
                except Exception:  # noqa: BLE001
                    pass

    def near_grad_rows(self):
        """Curves at or above the HEATING line, hottest first."""
        with self.lock:
            rs = [e for e in self.detail.values()
                  if (e.get("curve_eth") or 0) >= self.HEATING_ETH
                  and e.get("pre_grad") and not e.get("graduated")]
        rs.sort(key=lambda e: -(e.get("curve_eth") or 0))
        return rs[:8]

    def account_worker(self):
        """Wallet balance, daily caps and realised P&L. Cheap, once every 30s."""
        while self.running:
            try:
                a = {}
                addr = self._sender()
                a["addr"] = addr
                if addr:
                    r = rpc("eth_getBalance", [addr, "latest"])
                    if "result" in r:
                        a["bal"] = int(r["result"], 16) / 1e18
                if EXEC is not None:
                    try:
                        sp, op = EXEC.today_spent()
                        a["spent"], a["open"] = sp, op
                    except Exception:  # noqa: BLE001
                        pass
                # realised P&L: sells recorded by exit_manager minus what went in
                real, n = 0.0, 0
                ep = os.path.join(DATA, "exits.jsonl")
                if os.path.exists(ep):
                    for line in open(ep):
                        try:
                            d = json.loads(line)
                        except Exception:  # noqa: BLE001
                            continue
                        if d.get("action", "").startswith("SELL") and d.get("tx"):
                            n += 1
                            real += (d.get("quote_out") or 0) / 1e18
                a["realised"], a["trades"] = real, n
                self.acct = a
            except Exception:  # noqa: BLE001
                pass
            time.sleep(30)

    def cost_worker(self):
        """
        Live cost-of-trading ticker, sampled from REAL buy receipts.

        Deliberately not an entry gate: measured on 60 real buys, gasPrice is
        flat (0.3166-0.3211 gwei) and gasUsed explains 100% of the cost spread.
        There is no "gas is spiking, wait" state to wait out on this chain --
        the spike is per-transaction. This is situational awareness, and the
        actual protection is --max-gas-pct on each trade's own estimate.
        """
        import statistics as stx
        while self.running:
            try:
                head = head_block()
                lg = get_logs({"fromBlock": hex(max(0, head - 3000)),
                               "toBlock": hex(head), "topics": [T_CURVE_BUY]}) or []
                seen, costs, used = set(), [], []
                for x in lg:
                    h = x["transactionHash"]
                    if h in seen:
                        continue
                    seen.add(h)
                    r = (rpc("eth_getTransactionReceipt", [h]) or {}).get("result")
                    if not r:
                        continue
                    g = int(r["gasUsed"], 16)
                    px = int(r["effectiveGasPrice"], 16)
                    used.append(g)
                    costs.append(g * px / 1e18 * 2450.0)
                    if len(costs) >= 12:
                        break
                if costs:
                    costs.sort()
                    self.gasband = {"med": stx.median(costs),
                                    "p90": costs[int(0.9 * (len(costs) - 1))],
                                    "gas_med": stx.median(used), "n": len(costs),
                                    "ts": time.time()}
            except Exception:  # noqa: BLE001
                pass
            time.sleep(60)

    def hot_worker(self):
        """
        Pre-build the buy for a token that crossed HOT_AT, so approval is one key.

        It ARMS, it never sends -- [y] is still required, and every hard gate
        (honeypot FAIL / PENDING / inconclusive, caps, snipe tax, gas revert)
        still applies. If the row has not finished vetting the arm is retried
        rather than waved through: a HOT token with an unchecked honeypot is
        exactly the trade that should not be one keypress away.
        """
        while self.running:
            curve = None
            with self.lock:
                if self.hot_q:
                    curve = self.hot_q[0]
            if curve is None:
                time.sleep(0.4)
                continue
            if self.pending:                    # never clobber a live decision
                time.sleep(0.5)
                continue
            hp, _ = self.honeypot_state(curve)
            if hp == "PENDING":
                with self.lock:
                    if curve not in self.autovet_done and curve in self.detail:
                        self.autovet_q.appendleft(curve)     # jump the vetting queue
                time.sleep(1.0)
                continue
            with self.lock:
                if self.hot_q and self.hot_q[0] == curve:
                    self.hot_q.popleft()
            self.arm_buy(curve)

    def honeypot_state(self, curve):
        """('PASS'|'FAIL'|'PENDING'|'UNKNOWN', detail) for the hard block."""
        v = self.inv.get(curve)
        if not v:
            return "PENDING", "not vetted yet"
        if v.get("state") == "running":
            return "PENDING", "vetting"
        for pr in v.get("probes") or []:
            if pr.get("probe") == "honeypot":
                st = pr.get("status")
                if st == "FAIL":
                    return "FAIL", pr.get("detail", "")
                if st == "PASS":
                    return "PASS", pr.get("detail", "")
                return "UNKNOWN", pr.get("detail", "")
        return "UNKNOWN", "no honeypot probe"

    def _sender(self):
        """
        Sender address, derived from the key when present.

        Deriving beats trusting an env var: if HOOD_SNIPER_EXPECT_ADDRESS ever
        disagrees with the key, the tx would be built for one account and
        signed by another, and the nonce would be wrong.
        """
        key = os.environ.get("HOOD_SNIPER_PRIVATE_KEY", "").strip()
        if key:
            try:
                import ethsign
                a = ethsign.priv_to_addr(key)
                want = os.environ.get("HOOD_SNIPER_EXPECT_ADDRESS", "").strip().lower()
                if want and want != a.lower():
                    self.msg = f"BLOCKED: key address {a[:10]}… != EXPECT_ADDRESS"
                    return None
                return a
            except Exception:  # noqa: BLE001
                return None
        return os.environ.get("HOOD_SNIPER_EXPECT_ADDRESS", "").strip() or None

    def arm_buy(self, curve=None):
        """Build (never send) a buy, refusing on hard gates. curve=None -> selection."""
        if curve is None:
            rs = self.rows()
            if not rs:
                return
            e = rs[min(self.sel, len(rs) - 1)]
        else:
            with self.lock:
                e = self.detail.get(curve)
            if not e:
                return
        curve, tok = e.get("curve"), e.get("token")
        if EXEC is None:
            self.msg = "executor unavailable"
            return
        hp, why = self.honeypot_state(curve)
        if hp == "FAIL":
            self.pending = None
            self.msg = f"BLOCKED honeypot: {why}"[:110]      # hard block
            return
        if hp == "PENDING":
            self.pending = None
            self.msg = "BLOCKED: not vetted yet — honeypot check still running"
            return
        if hp == "UNKNOWN":
            self.pending = None
            self.msg = f"BLOCKED: honeypot result inconclusive ({why})"[:110]
            return
        # HARD BLOCK on an extractive creator tax. Measured on 197 random
        # graduations: 400+ bps tokens hit 2x only 6.7% of the time (vs 20%
        # baseline) AND cost 8%+ round trip -- worst on every axis at once.
        # The 1-200 bps band is deliberately NOT blocked; it is the best group
        # in the data (+4.38% net, 32.8% hit 2x), so a blanket tax gate would
        # cut the runners.
        ctax = e.get("tax_bps")
        if ctax is None:
            self.pending = None
            self.msg = "BLOCKED: creator tax unreadable — not assuming it is zero"
            return
        if ctax >= self.args.max_creator_tax_bps:
            self.pending = None
            self.msg = (f"BLOCKED: creator tax {ctax/100:.1f}%/side "
                        f"({2*ctax/100:.1f}% round trip) ≥ "
                        f"{self.args.max_creator_tax_bps/100:.1f}% — this band hits 2x "
                        f"6.7% of the time vs 20% baseline. --max-creator-tax-bps to override.")
            return
        addr = self._sender()
        if not addr:
            self.msg = ("BLOCKED: no sender — set HOOD_SNIPER_PRIVATE_KEY "
                        "(or HOOD_SNIPER_EXPECT_ADDRESS to dry-run)")
            return
        try:
            # preflight reads args.address; the monitor's parser has no such flag
            self.args.address = addr
            fails, warns, info = EXEC.preflight(curve, self.args.stake,
                                                self.args.max_tax_bps, self.args)
            if fails:
                self.pending = None
                self.msg = "BLOCKED: " + "; ".join(fails)[:100]
                return
            tx, meta = EXEC.build_tx(curve, self.args.stake, addr,
                                     self.args.slippage_bps)
            if meta.get("estimate_error"):
                self.pending = None
                self.msg = f"BLOCKED: gas estimate reverts — {meta['estimate_error']}"[:110]
                return
            # "wait for gas to stop spiking" does not work here: measured on 60
            # real buys, gasUsed explains 100% of the cost spread and gasPrice
            # 0% (0.3166-0.3211 gwei, flat). The spike is per-TRANSACTION, so
            # the only real protection is this trade's own estimate.
            gas_est = meta.get("gas_estimate") or tx.get("gas", 0)
            gas_now = (gas_est * (tx.get("maxFeePerGas", 0) // 2)) / 1e18 * 2450.0
            if gas_now > self.args.max_gas_pct / 100.0 * self.args.stake:
                self.pending = None
                self.msg = (f"BLOCKED: this buy would burn ${gas_now:.2f} of gas "
                            f"({gas_now/self.args.stake:.0%} of a ${self.args.stake:.0f} "
                            f"trade) — {gas_est:,} gas. Raise --max-gas-pct to allow.")
                return
            # all-in round-trip cost, shown BEFORE the key is pressed.
            # Measured: the Pons curve charges 100 bps/side on every curve
            # sampled, and that is charged by the CONTRACT -- every router pays
            # it. The variable that actually hurts a small bankroll is the
            # per-token creator tax: 45% of tokens charge one, up to 300 bps/side,
            # which on $25 costs more than any platform-fee difference.
            # already fetched during enrichment -- no extra RPC on the hot path
            fee_bps = e.get("fee_bps") or 0
            ctax_bps = e.get("tax_bps") or 0
            stake = self.args.stake
            pct_rt = 2 * (fee_bps + ctax_bps) / 10000.0
            # Show the EXPECTED spend, not the ceiling. Unused gas is refunded
            # on EIP-1559, and the ceiling (limit x maxFee) ran ~5x the truth:
            # measured on 40 real DIRECT curve.buy() txs, gas is 80,595 median
            # / 147,548 max = $0.061 / $0.112. The 4.2M-gas tail belongs to
            # ROUTED aggregator txs (8+ logs), which this path never sends.
            gas_est = meta.get("gas_estimate") or tx.get("gas", 0)
            eff_price = tx.get("maxFeePerGas", 0) // 2      # ~ base + tip
            gas_exp = (gas_est * eff_price) / 1e18 * 2450.0
            gas_ceil = (tx.get("gas", 0) * tx.get("maxFeePerGas", 0) / 1e18) * 2450.0
            cost = stake * pct_rt + gas_exp * 2      # buy + sell
            self.pending = {"tx": tx, "meta": meta, "curve": curve, "token": tok,
                            "symbol": e.get("symbol"), "warns": warns,
                            "cost_usd": cost}
            ctag = (f" · [creator tax {ctax_bps/100:.1f}%/side]" if ctax_bps else "")
            self.msg = (f"ARMED ${stake:.0f} {e.get('symbol') or ''} "
                        f"minOut={meta['min_tokens_out']} · all-in ~${cost:.2f} "
                        f"({cost/stake:.1%}, gas {gas_est:,} ≈ ${gas_exp:.3f}, "
                        f"max ${gas_ceil:.2f}){ctag} — [y] sign, [n] cancel"
                        + ("  !" + ";".join(warns)[:36] if warns else ""))
        except Exception as ex:  # noqa: BLE001
            self.pending = None
            self.msg = f"arm failed: {ex}"[:110]

    def confirm_buy(self):
        if not self.pending:
            return
        if not self.arm:
            self.msg = "NOT ARMED — restart with --arm to allow signing"
            self.pending = None
            return
        key = os.environ.get("HOOD_SNIPER_PRIVATE_KEY", "").strip()
        if not key:
            self.msg = "HOOD_SNIPER_PRIVATE_KEY not set — nothing to sign"
            self.pending = None
            return
        try:
            import ethsign
            raw = ethsign.sign_1559(self.pending["tx"], key)
            r = rpc("eth_sendRawTransaction", [raw])
            if "result" in r:
                self.msg = f"SENT {r['result'][:20]}…"
                with open(os.path.join(DATA, "trades.jsonl"), "a") as f:
                    f.write(json.dumps({"ts": time.time(), "tx": r["result"],
                                        "curve": self.pending["curve"],
                                        "token": self.pending["token"],
                                        "usd": self.args.stake}) + "\n")
            else:
                self.msg = f"SEND FAILED: {(r.get('error') or {}).get('message','?')}"[:110]
        except Exception as ex:  # noqa: BLE001
            self.msg = f"sign/send failed: {ex}"[:110]
        self.pending = None

    def enrich_worker(self):
        while True:
            curve = None
            now_ts = time.time()
            with self.lock:
                due = [c for t, c in self.retry if t <= now_ts]
                if due:
                    self.retry = [(t, c) for t, c in self.retry if t > now_ts]
                    self.q.extend(due)
                if self.q:
                    curve = self.q.popleft()
            if not curve:
                time.sleep(0.4)
                continue
            _t0 = time.time()
            _trace = {"curve": curve}
            try:
                rec = self.detail.get(curve)
                if not rec:
                    # The row was EVICTED by the size cap while still queued. Before
                    # this guard it fell through to the Pons branch and called
                    # token() on a v4 pool_id, which always fails -- producing a
                    # stream of tokenless "no pool yet" rows that looked like a
                    # resolution bug and was really a queue/eviction race.
                    continue
                blk = rec.get("block", 0)
                if rec.get("venue") == "o1":
                    e = self.enrich_o1(rec)
                elif rec.get("venue") == "bankr":
                    e = self.enrich_bankr(rec)
                else:
                    e = enrich_curve(curve, blk, self.reg, self.c2d, self.args.stake,
                                     head=self.last_head or None,
                                     pre_grad=bool(rec.get("pre_grad")))
                # Carry the SOURCE fields forward. Enrichment replaces the row, and
                # the replacement did not include c0/c1 -- so every Bankr retry
                # re-ran with no currencies to inspect, could never find the Doppler
                # clone, and the row was stuck at token=None for good. That is why
                # the table filled with permanent "retry".
                for k in ("c0", "c1", "venue", "watch_hit", "token", "symbol"):
                    if rec.get(k) is not None and e.get(k) is None:
                        e[k] = rec[k]
                if not (rec.get("pre_grad") and not e.get("graduated")):
                    e.update(enrich_pool(e.get("token"), blk, self.args.stake))
                else:
                    # pre-graduation: there is no v4 pool to price against, so read
                    # mcap / liquidity / slippage off the curve itself
                    e.update(curve_metrics(curve, self.args.stake))
                    self._mark_progress(e)
                    # RECORD THE CROSSING HERE, not only in progress_worker.
                    # progress_worker re-reads on a 25s cycle, so after a restart
                    # every row sat below "heating" for a minute or more and PRIME
                    # rendered empty -- indistinguishable from a broken filter.
                    # Progress is already known at this point; use it.
                    pg = e.get("curve_progress")
                    if pg is not None:
                        with self.lock:
                            prevp = e.get("_prev_prog")
                            e["_prev_prog"] = pg
                        self._note_crossing_prog(curve, prevp, pg)
                e["pre_grad"] = rec.get("pre_grad")
                if e.get("slippage_pct") is None and e.get("token") and not e.get("pre_grad"):
                    e["untraded"] = pool_exists_untraded(e["token"], blk)
                e["t"] = rec.get("t")
                e["block"] = blk
                e["state"] = "done"
                v, why = self.verdict(e)
                e["verdict"], e["why"] = v, why
                e["tries"] = (rec.get("tries") or 0) + 1
                # Seed the flow tracker from the scanned history. Block numbers
                # convert to wall clock via head, so a wallet's first buy keeps its
                # real time rather than the moment this process happened to see it.
                # Register the v4 pool so post-graduation swaps can be attributed.
                # Without this the Swap stream is anonymous pool ids and the runner
                # panel can never name a token.
                if e.get("pool_id") and e.get("token"):
                    with self.lock:
                        self.pool2tok[e["pool_id"]] = {
                            "token": e["token"], "curve": curve,
                            "symbol": e.get("symbol"),
                            "tok_is_c1": bool(e.get("token_is_currency1", True))}
                        if len(self.pool2tok) > 800:
                            self.pool2tok.pop(next(iter(self.pool2tok)))
                _fb = e.pop("buy_firsts", None)
                if _fb:
                    _hd = self.last_head or blk
                    with self.lock:
                        bf = self.buyflow.setdefault(curve, {})
                        for _w, _b in _fb.items():
                            _ts = time.time() - max(0, (_hd - _b)) * BLOCK_TIME
                            cur = bf.get(_w)
                            if cur is None:
                                bf[_w] = [1, 0, _ts]
                            elif _ts < cur[2]:
                                cur[2] = _ts        # earlier evidence wins
                with self.lock:
                    self.detail[curve] = e
                    self.stats["enriched"] += 1
                    if e.get("token") and curve not in self.autovet_done:
                        self.autovet_q.append(curve)      # vet BEFORE it is selected
                    # a just-launched pool has no swaps yet and cannot be priced;
                    # requeue a few times rather than leaving it UNKNOWN forever
                    #
                    # NO-BID RETRIES TOO, and it must. A curve whose buy() reverts
                    # the instant it is seen may simply not be open yet -- measured
                    # by re-testing 12 rows marked NO-BID, 1 (PRISMOSS) had become
                    # buyable minutes later. Without a retry that token is labelled
                    # dead permanently and can never be traded: a false negative that
                    # hides real launches, which is worse than a slow row. Its budget
                    # is smaller than UNKNOWN's because a curve that has not opened
                    # within a couple of minutes is almost certainly never going to.
                    _budget = 8 if v == "UNKNOWN" else (5 if v == "NO-BID" else 0)
                    if e["tries"] < _budget:                # CURVE/UNTRADED do not retry
                        e["state"] = "retry"
                        # 4s, 8s, 15s, 30s, 60s, 120s… — the pool usually shows up
                        # in the first few seconds; waiting a minute wasted the row
                        delay = min(4 * (2 ** (e["tries"] - 1)), 240)
                        self.retry.append((time.time() + delay, curve))
                if ENRICH_TRACE:
                    _trace.update({"venue": rec.get("venue") or "pons",
                                   "branch": ("o1" if rec.get("venue") == "o1"
                                              else "bankr" if rec.get("venue") == "bankr"
                                              else "curve"),
                                   "had_c0c1": bool(rec.get("c0") or rec.get("c1")),
                                   "token": e.get("token"), "symbol": e.get("symbol"),
                                   "slip": e.get("slippage_pct"), "verdict": v,
                                   "why": why, "tries": e["tries"],
                                   "ms": round((time.time() - _t0) * 1000),
                                   "qlen": len(self.q), "retrylen": len(self.retry),
                                   "detail": len(self.detail)})
                    with open(os.path.join(DATA, "enrich_trace.jsonl"), "a") as tf:
                        tf.write(json.dumps(_trace) + "\n")
                with open(self.journal, "a") as f:
                    f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                        **{k: x for k, x in e.items()
                                           if k not in ("flags", "good")},
                                        "flags": e.get("flags"),
                                        "good": e.get("good")}) + "\n")
            except Exception as ex:  # noqa: BLE001
                if ENRICH_TRACE:
                    _trace.update({"exception": f"{type(ex).__name__}: {ex}"[:120],
                                   "ms": round((time.time() - _t0) * 1000)})
                    with open(os.path.join(DATA, "enrich_trace.jsonl"), "a") as tf:
                        tf.write(json.dumps(_trace) + "\n")
                with self.lock:
                    if curve in self.detail:
                        self.detail[curve]["state"] = f"err {ex}"[:40]

    def ws_worker(self):
        import websocket
        topics = [t for v in VENUES.values() if v["enabled"]
                  for t in (v["new_topic"], v["grad_topic"]) if t]
        if self.smart:
            topics.append(T_CURVE_BUY)   # needed to see validated wallets enter
        # SELLS matter as much as buys here: "buy pressure" without the sell side is
        # just activity. Both are already free on the stream we are subscribed to.
        topics.append(T_CURVE_SELL)
        # Post-graduation trading. The curve tops out around $48k mcap, so anything
        # in the $50k-$150k band being hunted for a runner is a V4 pool, not a curve.
        # Every Swap log already carries amounts AND sqrtPriceX96, so volume, side
        # and price all come from the stream with no extra RPC.
        topics.append(T_V4_SWAP)
        # POOL CREATION. Without this, pool2tok is populated only during enrichment
        # of an ALREADY-graduated token -- which requires a re-enrich that usually
        # never happens, so the map stayed empty and every v4 Swap arrived for an
        # unknown pool. Measured live: pools_known=0 after 80s despite the swap
        # stream running. Learning pools at creation fixes it at the source.
        topics.append(T_V4_INITIALIZE)
        while True:
            try:
                ws = websocket.create_connection(WSS, timeout=25)
                ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "method": "eth_subscribe",
                                    "params": ["logs", {"topics": [topics]}]}))
                ws.recv()
                ws.settimeout(90)
                self.ws_last_msg = time.time()
                while True:
                    msg = json.loads(ws.recv())
                    res = msg.get("params", {}).get("result")
                    if res:
                        self.ws_last_msg = time.time()
                        self.on_log(res)
            except Exception:  # noqa: BLE001
                time.sleep(3)   # reconnect


# ---------------------------------------------------------------- rendering
def _prime_empty_why(mon):
    """
    An empty PRIME must say WHICH gate emptied it.

    "no candidate passes" reads as a broken filter, and that ambiguity cost a real
    debugging session: the view was working exactly as designed and simply had not
    filled yet after a restart. Counting the rejections separates "strict" from
    "stalled" at a glance.
    """
    try:
        with mon.lock:
            rows = list(mon.detail.values())
    except Exception:  # noqa: BLE001
        return "PRIME is empty"
    if not rows:
        return "no rows on the board yet — the feed is still warming up"
    early = dirty = late = 0
    for e in rows:
        c = e.get("curve")
        if "heating" not in (mon.crossed.get(c) or set()):
            early += 1
        elif not mon.is_clean(e):
            dirty += 1
        else:
            late += 1
    return (f"0 of {len(rows)} rows qualify — {early} not yet past heating, "
            f"{dirty} carry a kill flag, {late} are LATE (past "
            f"{CURVE_LATE_PCT:.0%} of supply sold). PRIME fills over the first "
            f"few minutes after a restart.")


def fmt_usd(v):
    if v is None:
        return "-"
    if v >= 1_000_000:
        return f"${v/1e6:.1f}M"
    if v >= 1000:
        return f"${v/1000:.0f}k"
    # Curve liquidity is often cents. Rounding those to "$0" reads as "unknown"
    # when it actually means "there is nothing in here" -- a decision-changing
    # difference, so small values keep their digits.
    if v >= 10:
        return f"${v:.0f}"
    if v > 0:
        return f"${v:.2f}"
    return "$0"


def build_view(mon):
    from rich.table import Table
    from rich.panel import Panel
    from rich.console import Group
    from rich.text import Text

    with mon.lock:
        tick = list(mon.ticker)[-14:]
        st = dict(mon.stats)
    st["head"] = mon.last_head
    rows = mon.rows()
    mon._sync_sel(rows)

    up = time.time() - st["started"]
    rate_new = st["new"] / max(up / 60, 0.01)
    rate_g = st["grads"] / max(up / 60, 0.01)
    vm = mon.venue_modes[mon.venue_i]
    vlbl = ("[bold cyan]PONS[/]" if vm == "pons" else
            "[dim]all venues[/]" if vm == "all" else f"[yellow]{vm.upper()}[/]")
    filt = {0: "all", 1: "[yellow]TRADEABLE[/]",
            2: "[bold green]ACTIONABLE[/]",
            3: "[bold white on red]HOT[/]",
            4: "[bold bright_magenta]MOVING[/]",
            5: "[bold black on green]PRIME[/]"}[mon.only_tradeable]
    with mon.lock:
        _all = list(mon.detail.values())
    n_act = sum(1 for e in _all if mon.actionable(e))
    wh = st.get("watch_hits", 0)
    hdr = (f"[bold]HOOD SNIPER[/]  up {up/60:.0f}m · new {st['new']} "
           f"({rate_new:.1f}/min) · grads {st['grads']} ({rate_g:.2f}/min) · "
           f"venue {vlbl} · filter {filt} · " +
           # state the volume floor OUT LOUD. A filter that silently removes 40% of
           # the board reads as a dead feed, which is exactly how it was reported.
           (f"[dim]vol>={fmt_usd(getattr(mon.args,'min_vol',0))}[/] · "
            if getattr(mon.args, "min_vol", 0) else "[dim]vol floor off[/] · ") +
           (f"[dim]buyers>={getattr(mon.args,'min_buyers',0)}[/] · "
            if getattr(mon.args, "min_buyers", 0) else "") +
           (f"[bold green]{n_act} ACTIONABLE[/]" if n_act else "[dim]0 actionable[/]") +
           (f" · [bold red]WATCH HITS {wh}[/]" if wh else
            (f" · watching {','.join(sorted(mon.watch))}" if mon.watch else "")) +
           (" · [bold red]ARMED[/]" if getattr(mon.args, "arm", False)
            else " · [dim]disarmed[/]"))
    if LOG_STATS["dropped"]:
        hdr += f"  ·  [bold red]FEED DEGRADED: {LOG_STATS['dropped']} log chunks unreadable[/]"
    gb = mon.gasband
    if gb:
        stake = mon.args.stake
        allin = (2 * 0.01 * stake + gb["med"] * 2.2) / stake      # curve 2% RT + gas
        gcol = "green" if gb["med"] * 2.2 < 0.04 * stake else (
            "yellow" if gb["med"] * 2.2 < 0.08 * stake else "red")
        hdr += (f" · [b]COST[/] gas [{gcol}]${gb['med']:.2f}[/]/${gb['p90']:.2f} · "
                f"${stake:.0f} RT [{gcol}]{allin:.1%}[/]")

    tk = Text(" · ".join(r["curve"][:8] for r in tick) or "waiting…",
              style="dim", overflow="ellipsis", no_wrap=True)
    _show_ticker = False        # 3 lines of unactionable hex; folded into the header

    t = Table(expand=True, header_style="bold", box=None, pad_edge=False)
    # Pinned widths for the columns that CHANGE CONTENT BETWEEN FRAMES. Equal-width
    # blink states already stop the jitter; pinning makes it structural, so a future
    # edit to any of these cells cannot start the whole table reflowing again.
    FIXED = {"tax": 7, "smart": 6, "flow": 8}
    for c, j in ((" ", "left"), ("time", "left"), ("symbol", "left"),
                 ("hp", "left"), ("tax", "right"), ("smart", "right"),
                 ("flow", "left"), ("mcap", "right"), ("vol1h", "right"), ("hold", "right"),
                 ("liq", "right"), ("slip%", "right"), ("fee%", "right"),
                 ("age", "right"), ("flags", "left")):
        t.add_column(c, justify=j, no_wrap=(c != "flags"), width=FIXED.get(c))

    hot_set = {c for c, _ in mon.hot_now()}
    for i, e in enumerate(rows):
        sel_row = (i == mon.sel)
        is_hot = e.get("curve") in hot_set
        # A HOT token must be findable in the LIST, not only in the banner. The
        # banner scrolls past and shows a handful; the list is what gets searched,
        # sorted and acted on, so the mark lives on the row itself.
        cur = ("[bold white on red]★[/]" if is_hot and not sel_row
               else "[bold]▶[/]" if sel_row else " ")
        row_style = ("bold on grey35" if sel_row and is_hot else
                     "on grey30" if sel_row else
                     "on grey19" if is_hot else None)
        if e.get("state") not in ("done",):
            lbl = "[dim]retry…[/]" if e.get("state") == "retry" else "[dim]enriching[/]"
            t.add_row(cur, e.get("t", ""), (e.get("symbol") or "…")[:11], lbl,
                      "", "", "", "", "", "", "", "", "", "", style=row_style)
            continue
        v = e.get("verdict", "?")
        color = {"TRADEABLE": "green", "UNTRADEABLE": "red", "COSTLY": "yellow",
                 "CURVE": "bold cyan", "UNTRADED": "dim cyan",
                 "NO-BID": "dim red",
                 "UNKNOWN": "dim"}.get(v, "white")
        slip = e.get("slippage_pct")
        liq = e.get("active_liq_usd")
        dev = e.get("dev") or ""
        devs = f"{dev[:6]}·{e.get('dev_prior_grads',0)}G" if dev else "-"
        inv = mon.inv.get(e.get("curve"), {})
        mark = {"running": " [cyan]⟳[/]", "done": " [cyan]✓inv[/]"}.get(inv.get("state"), "")
        sym = (e.get("symbol") or "?")[:11] + mark
        if e.get("woke_up"):
            sym = "[bold magenta]↑[/]" + sym      # was quiet, started running
        # Creator tax is NOT a warning by default -- it is an inverted U, measured
        # on 197 randomly sampled graduations with forward price paths:
        #   none      n=110  gross -7.59%  net  -9.44%  hits 2x 20.0%
        #   1-200bps  n= 61  gross +9.60%  net  +4.38%  hits 2x 32.8%  <- best, and
        #                    the ONLY net-positive group (Fisher p=0.048 vs none)
        #   200-400   n= 11  gross +1.82%  net  -6.27%  (too few to read)
        #   400+      n= 15  gross -26.0%  net -34.76%  hits 2x  6.7%  <- worst
        # A modest tax reads as aligned fee capture; a heavy one as extraction.
        # Still never a block -- 400+ is rare and Slim judges the narrative.
        tx_bps = e.get("tax_bps") or 0
        if tx_bps >= 400:
            # BOTH blink states must render the SAME NUMBER OF CHARACTERS.
            # They did not: " 6.0% " (6) alternating with "6.0%" (4). Rich recomputes
            # column widths every frame, so the tax column oscillated twice a second
            # and shoved every column right of it back and forth -- one flashing row
            # made the WHOLE TABLE jump, which is unreadable when several rows carry
            # a high tax. Only the STYLE may change between frames, never the text.
            blink = int(time.time() * 2) % 2 == 0
            taxs = (f"[bold white on red] {tx_bps/100:.1f}% [/]" if blink
                    else f"[bold red] {tx_bps/100:.1f}% [/]")
        elif tx_bps > 200:
            taxs = f"[yellow] {tx_bps/100:.1f}% [/]"
        elif tx_bps > 0:
            taxs = f"[bold green] {tx_bps/100:.1f}% [/]"    # measured favourable
        else:
            taxs = "[dim] - [/]"
        blk = e.get("block")
        if blk and st.get("head"):
            secs = max(0, (st["head"] - blk)) * 0.101
            agestr = (f"{secs:.0f}s" if secs < 90 else
                      f"{secs/60:.0f}m" if secs < 5400 else f"{secs/3600:.1f}h")
            if secs > 900:
                agestr = f"[dim]{agestr}[/]"          # stale, de-emphasised
        else:
            agestr = "-"
        _a = mon.buy_accel(e.get("curve"))
        if _a:
            _, _rec, _prev, _sp, _mature = _a
            if not _mature:
                _c = "cyan"          # shape only — too young to call a trend
            elif _rec > _prev * 1.5:
                _c = "bold green"
            elif _rec * 1.5 < _prev:
                _c = "red"
            else:
                _c = "dim"
            flow_s = f"[{_c}]{_sp}[/]"
        else:
            flow_s = "[dim]········[/]"
        hp, _why = mon.honeypot_state(e.get("curve"))
        hpc = {"PASS": "[green]ok[/]", "FAIL": "[bold red]HONEYPOT[/]",
               "PENDING": "[dim]…[/]", "UNKNOWN": "[yellow]?[/]"}.get(hp, "")
        ns = mon.smart.count(e.get("curve")) if mon.smart else 0
        hot_at = mon.smart.hot_at if mon.smart else 4
        if ns >= hot_at:
            smart = f"[bold white on red] ★{ns} [/]"      # HOT
        elif ns >= 2:
            smart = f"[bold yellow]★{ns}[/]"
        else:
            smart = f"★{ns}" if ns else "-"
        def _mag(v, hi, mid):
            if not v:
                return "[dim]-[/]"
            c = "bold green" if v >= hi else "yellow" if v >= mid else "dim"
            return f"[{c}]{fmt_usd(v)}[/]"
        t.add_row(
            cur, e.get("t", ""), sym, hpc, taxs, smart, flow_s,
            _mag(e.get("mcap"), 500_000, 100_000),
            # pre-graduation there is no DexScreener pair yet -- show the CURVE
            # volume instead of an empty cell, so an early row is still readable
            _mag(e.get("vol_h1") or e.get("curve_volume_usd"), 50_000, 5_000),
            (f"{e['n_holders']:,}" if e.get("n_holders") else "-"),
            fmt_usd(liq), f"{slip:.2f}" if slip is not None else "-",
            f"{e.get('total_fee_bps',0)/100:.1f}", agestr,
            (f"[green]{' '.join(e.get('good',[])[:2])}[/] "
             f"[red]{' '.join(e.get('flags',[])[:3])}[/]").strip(),
            style=row_style,
        )

    if not rows:
        why = {0: "no launches seen yet — the feed is still warming up",
               1: f"no row is buyable under {mon.args.max_slip}% slip right now",
               2: "nothing passes every gate [b] checks right now",
               3: "no smart-wallet cluster is live right now",
               4: "nothing has traded in the last 5 min",
               5: _prime_empty_why(mon)}.get(mon.only_tradeable, "")
        t.add_row("", "[dim]" + why + "[/]", "", "", "", "", "", "", "", "", "",
                  "", "", "", "[dim]press f to widen the filter[/]")
    panels = [Panel(t, title=hdr)]

    # ---- RUNNERS -----------------------------------------------------------
    # The band alone is not a signal. Most tokens sitting at $80k are SITTING there.
    # A runner needs price up, buy-side pressure and real volume at the same time --
    # which is exactly "volume, buy/sell and holders" rather than market cap alone.
    rr = mon.runners()
    if rr:
        rt = Table(expand=True, box=None, show_header=True, header_style="bold")
        # CONTRACT IS PRINTED IN FULL, and it is the reason this panel is usable.
        # It previously had no contract column at all, so a runner could be spotted
        # and then not acted on -- the panel showed a ticker and nothing you could
        # paste anywhere. A truncated address is the same as no address.
        for c, j in (("symbol", "left"), ("mcap", "right"), ("move", "right"),
                     ("net 5m", "right"), ("buy side", "right"),
                     ("wallets", "right"), ("trades", "right"), ("smart", "right"),
                     ("contract", "left")):
            rt.add_column(c, justify=j, no_wrap=True)
        for r in rr:
            bs, net = r["_bshare"], r["_net"]
            bc = "bold green" if bs >= 0.8 else ("green" if bs >= 0.7 else "yellow")
            nc = "bold green" if net >= 2000 else ("green" if net >= 800 else "yellow")
            ns = mon.smart.count(r.get("curve")) if mon.smart else 0
            v = r.get("_vel")
            if v is None:
                vs = "[dim]…[/]"
            else:
                vc = ("bold white on green" if v >= 50 else "bold green" if v >= 20
                      else "green" if v > 0 else "red")
                vs = f"[{vc}]{v:+.0f}%[/]"
            _mc = r["_mcap"]
            if not _mc:
                mcs = "[dim]?[/]"
            elif mon.RUN_PRIME_LO <= _mc <= mon.RUN_PRIME_HI:
                mcs = f"[bold white on green]{fmt_usd(_mc)}[/]"   # 86% zone
            else:
                mcs = fmt_usd(_mc)
            rt.add_row((r.get("symbol") or "?")[:12], mcs, vs,
                       f"[{nc}]+{fmt_usd(net)}[/]", f"[{bc}]{bs:.0%}[/]",
                       str(r["_w"]), str(r["_n"]),
                       f"[bold yellow]★{ns}[/]" if ns >= 2 else (f"★{ns}" if ns else "-"),
                       r.get("token") or r.get("curve") or "-")
        panels.append(Panel(rt, border_style="bright_magenta",
                            title=f"[bold bright_magenta]RUNNERS[/] [bold white on magenta] r [/] — on the curve, "
                                  f"{fmt_usd(mon.RUN_LO)}-{fmt_usd(mon.RUN_HI)} mcap AND moving "
                                  f"[dim](green = {fmt_usd(mon.RUN_PRIME_LO)}-"
                                  f"{fmt_usd(mon.RUN_PRIME_HI)}, 86% reach near-grad)[/] "
                                  f"[dim](ranked by 90s VELOCITY — top-third movers "
                                  f"peaked 2.33x vs 1.57x, n=72)[/]"))

    # ---- POST-GRADUATION RUNNERS -------------------------------------------
    pg = mon.postgrad_runners()
    if pg:
        pt = Table(expand=True, box=None, show_header=True, header_style="bold")
        for c, j in (("symbol", "left"), ("mcap", "right"), ("5m move", "right"),
                     ("buy side", "right"), ("5m vol", "right"), ("trades", "right"),
                     ("contract", "left")):
            pt.add_column(c, justify=j, no_wrap=True)
        for r in pg:
            pc = ("bold white on green" if r["_pct"] >= 25
                  else "bold green" if r["_pct"] >= 10 else "green")
            bc = "bold green" if r["_buy"] >= 0.7 else "green"
            pt.add_row((r.get("symbol") or "?")[:12], fmt_usd(r["_mcap"]),
                       f"[{pc}]+{r['_pct']:.0f}%[/]", f"[{bc}]{r['_buy']:.0%}[/]",
                       fmt_usd(r["_vol"]), str(r["_n"]),
                       # full address, not [:22] -- a cut-off contract cannot be
                       # pasted into a scanner or a buy, so it may as well be blank
                       (r.get("token") or "-"))
        panels.append(Panel(pt, border_style="bright_cyan",
                            title="[bold bright_cyan]POST-GRAD RUNNERS[/] — "
                                  "$50k-$150k in the v4 pool "
                                  "[dim](35% of graduates touch 2x, median 14 min)[/]"))

    # ---- NEAR GRADUATION ---------------------------------------------------
    # Graduation is at 4.0 ETH (measured). Progress is shown as % OF THE CURVE
    # rather than mcap: the curve is the native unit and mcap is derived from it,
    # so a change in ETH price would move a mcap threshold without anything about
    # the token having changed.
    ng = mon.near_grad_rows()
    if ng:
        gt = Table(expand=True, box=None, show_header=True, header_style="bold")
        for c, j in (("tier", "left"), ("symbol", "left"), ("progress", "left"),
                     ("raised", "right"), ("of curve", "right"), ("mcap", "right"),
                     ("smart", "right"), ("tax", "right"), ("age", "right")):
            gt.add_column(c, justify=j, no_wrap=True)
        for e in ng:
            eth = e.get("curve_eth") or 0.0
            pct = 100 * eth / mon.GRAD_ETH
            near = eth >= mon.NEAR_GRAD_ETH
            tier = ("[bold white on green] NEAR GRAD [/]" if near
                    else "[bold yellow]heating[/]")
            filled = int(min(pct, 100) / 10)
            bar = f"[{'bold green' if near else 'yellow'}]{'█'*filled}[/][dim]{'░'*(10-filled)}[/]"
            ns = mon.smart.count(e.get("curve")) if mon.smart else 0
            blk = e.get("block")
            secs = max(0, (mon.last_head - blk)) * BLOCK_TIME if blk and mon.last_head else None
            agestr = ("-" if secs is None else f"{secs:.0f}s" if secs < 90
                      else f"{secs/60:.0f}m" if secs < 5400 else f"{secs/3600:.1f}h")
            tx = e.get("tax_bps")
            gt.add_row(tier, (e.get("symbol") or "?")[:12], bar, fmt_usd(eth * ETH_USD),
                       f"{pct:.0f}%", fmt_usd(e.get("mcap")),
                       f"[bold yellow]★{ns}[/]" if ns >= 2 else (f"★{ns}" if ns else "-"),
                       f"{tx/100:.1f}%" if tx is not None else "-", agestr)
        n_near = sum(1 for e in ng if (e.get("curve_eth") or 0) >= mon.NEAR_GRAD_ETH)
        panels.append(Panel(gt, border_style="green",
                            title=f"[bold green]NEAR GRADUATION[/] [bold black on green] g [/] — "
                                  f"grad at {fmt_usd(mon.GRAD_ETH*ETH_USD)} raised "
                                  f"(~{fmt_usd(48000)} mcap) · "
                                  f"heating {fmt_usd(mon.HEATING_ETH*ETH_USD)} · "
                                  f"near {fmt_usd(mon.NEAR_GRAD_ETH*ETH_USD)} "
                                  f"[dim](only ~1.6% of launches ever graduate)[/]"
                                  + (f"  [bold]{n_near} NEAR[/]" if n_near else "")))

    # HOT banner -- alternates twice a second so it is impossible to miss on a
    # screen that is already scrolling. Nothing here sends; [y] is still required.
    live_hot = mon.hot_now()
    if live_hot:
        on = int(time.time() * 2) % 2 == 0
        banner = Table.grid(expand=True)
        banner.add_column(justify="center")
        # Every live HOT gets its own line. Showing one meant that when smart money
        # hit three coins in the same minute, two were invisible -- and those are
        # exactly the minutes worth trading.
        for c, since in live_hot[:6]:
            with mon.lock:
                he = mon.detail.get(c) or {}
            meta = mon.hot_meta.get(c) or {}
            n = mon.smart.count(c) if mon.smart else 0
            sym = he.get("symbol") or meta.get("symbol") or "?"
            ca = he.get("token") or meta.get("token") or c
            age = time.time() - since
            if mon.pending and mon.pending.get("curve") == c:
                tail = "BUY BUILT — [y] sign  [n] cancel"
            else:
                hp, _w = mon.honeypot_state(c)
                tail = ("vetting…" if hp == "PENDING"
                        else "HONEYPOT — blocked" if hp == "FAIL"
                        else mon.msg[8:][:46] if mon.msg.startswith("BLOCKED")
                        else "arming…")
            # only the strongest line flashes, or six blinking rows are unreadable
            style = ("bold white on red" if (on and c == live_hot[0][0])
                     else "bold red")
            banner.add_row(Text(f"  ★{n} SMART  ${sym}  {ca}   {age:.0f}s   {tail}  ",
                                style=style))
        extra = f" (+{len(live_hot)-6} more)" if len(live_hot) > 6 else ""
        panels.insert(0, Panel(banner, height=min(len(live_hot), 6) + 2,
                               border_style="red",
                               title=f"[bold red]HOT ×{len(live_hot)}{extra}[/] [bold white on red] . [/][bold red] — "
                                     f"contracts in full; also data/smart_alerts.jsonl[/]"))
    with mon.lock:
        hits = list(mon.hits)[-6:][::-1]
    if hits:
        ht = Table(expand=True, box=None, show_header=True, header_style="bold red")
        for c in ("time", "ticker", "venue", "token (CA)"):
            ht.add_column(c, no_wrap=True)
        for h in hits:
            ht.add_row(h["t"], f"[bold]${h['symbol']}[/]", h["venue"], h["token"])
        panels.append(Panel(ht, title="[bold red]WATCHLIST HITS — verify the CA, squatters are common[/]"))

    with mon.lock:
        sal = list(mon.smart_alerts)[:5]
    if sal:
        at = Table(expand=True, box=None, show_header=True, header_style="bold yellow")
        for c in ("time", "n", "ticker", "curve (CA)", "wallets"):
            at.add_column(c, no_wrap=(c != "wallets"))
        for h in sal:
            ws = " ".join(w["wallet"][:8] for w in h["wallets"][:4])
            at.add_row(h["t"], f"[bold]{h['n']}[/]", f"${h.get('symbol') or '?'}",
                       h["curve"], ws)
        panels.append(Panel(at, title="[bold yellow]SMART MONEY — 2+ wallets whose PAST PICKS "
                                      "ran (2+ → 33.5% hit 2x vs 22.4%, p=3e-07)[/]"))

    if mon.show_help:
        ht = Table.grid(padding=(0, 3))
        ht.add_column(style="bold"); ht.add_column()
        for k, v in (("↑↓ / j k", "move selection"),
                     ("i or ⏎", "investigate the selected token (full DYOR)"),
                     (".", "cycle through the live HOT tokens and investigate each"),
                     ("r", "step through the RUNNERS panel — selects each row"),
                     ("g", "step through NEAR GRADUATION — selects each row"),
                     ("i", "open the HTML report for this coin (clickable links)"),
                     ("c", "copy this coin's contract address to the clipboard"),
                     ("N", "Nansen: who funded this dev + counterparties (uses credits)"),
                     ("R", "re-run vetting on this coin"),
                     ("f", "filter: ALL → TRADEABLE → ACTIONABLE → HOT → MOVING → PRIME"),
                     ("v", "cycle venue: PONS → all → Bankr → o1"),
                     ("s", "cycle sort: time → ACCEL → smart → mcap"),
                     ("b", "build a buy (shows size, minOut, all-in cost) — never sends"),
                     ("y", "sign and send the armed buy (needs --arm)"),
                     ("n", "cancel the armed buy"),
                     ("g", "jump to newest row"),
                     ("?", "show / hide this help"),
                     ("q then y", "quit (asks for confirmation)")):
            ht.add_row(k, v)
        panels.append(Panel(ht, title="[bold]KEYS[/]", border_style="cyan"))

    if mon.msg:
        style = ("bold white on red" if mon.msg.startswith("QUIT?")
                 else "bold red" if mon.msg.startswith("BLOCKED")
                 else "bold yellow" if mon.msg.startswith("ARMED")
                 else "bold green" if mon.msg.startswith("SENT") else "white")
        panels.append(Panel(Text(mon.msg, style=style), height=3,
                            title="[b]↑↓ move · i investigate · b build buy · "
                                  "y sign · n cancel · f filter · q quit[/]"))

    # ---- ACCOUNT / P&L ----------------------------------------------------
    # Sits directly under the header: size, remaining budget and realised P&L are
    # what decide whether to take the next trade at all, so they belong on screen
    # rather than in a file you have to go and read.
    acc = Table.grid(padding=(0, 3))
    for _ in range(8):
        acc.add_column()
    a_addr = mon.acct.get("addr")
    a_bal = mon.acct.get("bal")
    spent, opened = mon.acct.get("spent", 0.0), mon.acct.get("open", 0)
    real = mon.acct.get("realised", 0.0)
    ntr = mon.acct.get("trades", 0)
    cap_c = "red" if spent >= 150 else ("yellow" if spent >= 100 else "green")
    acc.add_row(
        Text("WALLET", style="dim"),
        Text(a_addr[:10] + "…" if a_addr else "not set", style="bold" if a_addr else "dim red"),
        Text("BAL", style="dim"),
        Text(f"{a_bal:.4f} ETH" if a_bal is not None else "—", style="bold"),
        Text("TODAY", style="dim"),
        Text(f"${spent:.0f}/$150 · {opened}/3 open", style=cap_c),
        Text("REALISED P&L", style="dim"),
        Text(f"{real:+.4f} ETH  ({ntr} trades)",
             style="bold green" if real > 0 else ("bold red" if real < 0 else "dim")))
    panels.append(Panel(acc, height=3, border_style="grey37"))

    # ---- DETAIL for the highlighted row -----------------------------------
    if rows:
        se = rows[min(mon.sel, len(rows) - 1)]
        dt = Table.grid(padding=(0, 2))
        dt.add_column(style="dim", width=13); dt.add_column(width=30)
        dt.add_column(style="dim", width=13); dt.add_column()
        ca = se.get("token") or se.get("curve") or ""
        conc = mon.buy_concentration(se.get("curve"))
        ns = mon.smart.count(se.get("curve")) if mon.smart else 0
        hpv, hpw = mon.honeypot_state(se.get("curve"))
        tax = se.get("tax_bps")
        gp = se.get("grad_pct")
        if conc:
            nb, cshare, vshare = conc
            conc_s = (f"{nb} buyers · top1 [{'bold red' if cshare > .5 else 'green'}]"
                      f"{cshare:.0%}[/] of buys, {vshare:.0%} of size")
        else:
            conc_s = "[dim]too few buys to judge[/]"
        dt.add_row("contract", f"[bold]{ca}[/]", "venue", str(se.get("venue") or "pons"))
        dt.add_row("market cap", fmt_usd(se.get("mcap")),
                   "holders", f"{se['n_holders']:,}" if se.get("n_holders") else "-")
        dt.add_row("vol 1h", fmt_usd(se.get("vol_h1") or se.get("curve_volume_usd")),
                   "liquidity", fmt_usd(se.get("active_liq_usd")))
        dt.add_row("buy flow", conc_s,
                   "★ smart in", f"[bold yellow]{ns}[/]" if ns else "0")
        dt.add_row("honeypot", f"{hpv} · {hpw[:26]}",
                   "creator tax", f"{tax/100:.1f}%" if tax is not None else "?")
        dt.add_row("fees",
                   f"{se.get('total_fee_bps',0)/100:.1f}% total",
                   "curve", f"{gp:.1f}% full" if gp is not None else "-")
        _dl = se.get("dev_launches")
        if _dl is None:
            devs_s = str(se.get("dev") or "-")[:22]
        elif _dl >= 10:
            devs_s = f"[bold red]{_dl} launches[/] — 2.8% hit rate"
        elif _dl >= 3:
            devs_s = f"[yellow]{_dl} launches[/] — below base"
        elif _dl <= 1:
            devs_s = f"[bold green]first launch[/] — 11.6% hit rate"
        else:
            devs_s = f"{_dl} launches"
        dt.add_row("slippage", f"{se['slippage_pct']:.2f}%" if se.get("slippage_pct") is not None else "-",
                   "deployer", devs_s)
        # One-line read of the vetting, so the verdict is visible without parsing
        # the probe list underneath it.
        _iv = mon.inv.get(se.get("curve")) or {}
        if _iv.get("state") == "done":
            _p = _iv.get("probes") or []
            _f = [x for x in _p if x["status"] == "FAIL"]
            _w = [x for x in _p if x["status"] == "WARN"]
            # The reason lives in the WIDE column -- squeezing it next to the
            # verdict truncated the probe name to nothing, which is the one part
            # that says what to do about it.
            if _f:
                vet = f"[bold red]BLOCKED[/] · {len(_f)} fail, {len(_w)} warn"
                vwhy = f"[red]{_f[0]['probe']}[/] — {_f[0]['detail'][:60]}"
            elif _w:
                vet = f"[yellow]CAUTION[/] · {len(_w)} warn"
                vwhy = f"[yellow]{_w[0]['probe']}[/] — {_w[0]['detail'][:60]}"
            else:
                vet = f"[bold green]CLEAR[/] · {len(_p)} probes"
                vwhy = "[dim]nothing flagged[/]"
        elif _iv.get("state") == "running":
            vet, vwhy = "[cyan]vetting…[/]", "[dim]runs automatically, ~15s[/]"
        elif _iv:
            vet, vwhy = "[red]vet error[/]", f"[red]{str(_iv.get('state'))[:60]}[/]"
        else:
            vet, vwhy = "[dim]queued[/]", "[dim]waiting for the vetting worker[/]"
        dt.add_row("vetting", vet, "why", vwhy)
        nz = se.get("nansen")
        if nz:
            f = nz.get("funder")
            cp = nz.get("counterparties") or []
            top = max(cp, key=lambda x: x.get("usd") or 0) if cp else None
            dt.add_row("dev funder",
                       f"[bold]{f[:18]}…[/]" if f else "[dim]none found[/]",
                       "counterparties",
                       (f"{len(cp)}"
                        + (f" · top ${top['usd']:,.0f}" if top and top.get("usd") else ""))
                       if cp else "[dim]-[/]")
        elif se.get("dev"):
            dt.add_row("nansen", "[dim]press N to look up the dev[/]", "", "")
        fl = " ".join(se.get("flags", [])[:4]); gd = " ".join(se.get("good", [])[:3])
        if fl or gd:
            dt.add_row("signals", f"[green]{gd}[/]", "", f"[red]{fl}[/]")
        panels.append(Panel(dt, title=f"[bold]{se.get('symbol') or '?'}[/]  "
                                      f"[dim]— highlighted[/]", border_style="cyan"))

    # Investigation panel follows the SELECTION.
    # autovet_worker already runs a full investigation on EVERY row with a token,
    # queued at enrich time and on by default -- but this panel used to render only
    # for `inv_for`, which only the [i] key ever set. So the work was being done for
    # every token and then thrown away visually unless you went and asked for it
    # again. Hovering a row is the ask.
    cur_curve = None
    if rows:
        _se = rows[min(mon.sel, len(rows) - 1)]
        if _se.get("curve") in mon.inv:
            cur_curve = _se.get("curve")
    if cur_curve is None:
        cur_curve = mon.inv_for
    if cur_curve and cur_curve in mon.inv:
        iv = mon.inv[cur_curve]
        body = Table(expand=True, box=None, show_header=False, pad_edge=False)
        body.add_column(width=3)
        body.add_column(width=10)
        body.add_column(width=13)
        body.add_column(overflow="fold")
        if iv["state"] == "running":
            body.add_row("", "", "", "[cyan]investigating… (~15s)[/]")
        elif iv["state"] != "done":
            body.add_row("", "", "", f"[red]{iv['state']}[/]")
        else:
            ic = {"PASS": "[green]✓[/]", "WARN": "[yellow]![/]",
                  "FAIL": "[red]✗[/]", "N-A": "[dim]–[/]",
                  "UNKNOWN": "[dim]?[/]", "NEEDS-KEY": "[magenta]🔑[/]"}
            for pr in iv["probes"]:
                body.add_row(ic.get(pr["status"], "?"), pr["status"],
                             pr["probe"], pr["detail"][:160])
            f = sum(1 for x in iv["probes"] if x["status"] == "FAIL")
            w = sum(1 for x in iv["probes"] if x["status"] == "WARN")
            verd = "[red]BLOCKED[/]" if f else ("[yellow]CAUTION[/]" if w else "[green]CLEAR[/]")
            body.add_row("", "", "", f"{verd}  {f} fail · {w} warn")
        panels.append(Panel(body, title=f"investigation · {iv.get('symbol','?')}"))

    _rs = mon.rows()
    _pos = f"{mon.sel + 1}/{len(_rs)}" if _rs else "0/0"
    _lk = repr(mon.last_key)[1:-1] if mon.last_key else "—"
    foot = Text.assemble(
        (f" row {_pos} ", "bold black on cyan"),
        (f"  key:{_lk}  ", "dim"),
        ("↑↓", "bold"), (" move  ", "dim"),
        ("i", "bold"), (" report  ", "dim"),
        ("c", "bold"), (" copy CA  ", "dim"),
        ("N", "bold"), (" nansen  ", "dim"),
        ("b", "bold"), (" build buy  ", "dim"),
        ("y", "bold"), (" sign  ", "dim"),
        ("f", "bold"), (" filter  ", "dim"),
        ("v", "bold"), (" venue  ", "dim"),
        ("s", "bold"), (" sort  ", "dim"),
        (".", "bold"), (" hot  ", "dim"),
        ("r", "bold"), (" runners  ", "dim"),
        ("g", "bold"), (" nearGrad  ", "dim"),
        ("?", "bold"), (" keys  ", "dim"),
        ("q", "bold"), (" quit", "dim"))
    panels.append(Panel(foot, height=3))
    return Group(*panels)


def run_tui(mon):
    from rich.live import Live
    from rich.console import Console
    console = Console()
    threading.Thread(target=mon.key_worker, daemon=True).start()
    with Live(build_view(mon), console=console, refresh_per_second=4,
              screen=True) as live:
        while mon.running:
            time.sleep(0.25)
            live.update(build_view(mon))


def run_plain(mon):
    seen = set()
    while True:
        time.sleep(1)
        with mon.lock:
            rows = list(mon.detail.items())
        for c, e in rows:
            if c in seen or e.get("state") != "done":
                continue
            seen.add(c)
            print(f"{e.get('t')} {e.get('symbol','?'):10} {e.get('verdict','?'):12} "
                  f"slip={e.get('slippage_pct')} fee={e.get('total_fee_bps',0)/100:.1f}% "
                  f"dev={str(e.get('dev'))[:10]}·{e.get('dev_prior_grads',0)}G "
                  f"flags={','.join(e.get('flags',[]))}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Hood Sniper live launch monitor. Disarmed unless --arm.")
    ap.add_argument("--stake", type=float, default=25.0, help="size used for slippage math")
    ap.add_argument("--max-slip", type=float, default=2.0, help="tradeable threshold %%")
    ap.add_argument("--rows", type=int, default=18)
    ap.add_argument("--min-buyers", type=int, default=MIN_BUYERS,
                    help="hide rows with fewer distinct buyers. 0 disables. "
                         "Default 10; with the volume floor that is 34.6%% "
                         "precision at 56.9%% recall vs 26.4%%/69.6%% on volume "
                         "alone. 20 gives 36.9%% but drops recall to 46%%.")
    ap.add_argument("--min-vol", type=float, default=MIN_VOL_USD,
                    help="hide rows below this volume. 0 shows everything. "
                         "Default 2000: keeps 29%% of rows at a 28.9%% near-grad "
                         "rate vs 11.5%% unfiltered.")
    ap.add_argument("--watch", default="",
                    help="comma-separated tickers to alert on, e.g. --watch HOOJA,PEPE2. "
                         "Matches symbol() exactly or the name containing the term. "
                         "Surfaces EVERY match -- 12.7%% of tickers here have multiple "
                         "contracts, so it will not choose one for you.")
    ap.add_argument("--venues", default="pons",
                    help="which venue to show at startup: pons (default) / all / "
                         "bankr / o1. Cycle live with the v key.")
    ap.add_argument("--no-tui", action="store_true")
    ap.add_argument("--arm", action="store_true",
                    help="allow [y] to actually sign and send. Without it the "
                         "monitor builds and shows the tx but never signs.")
    ap.add_argument("--max-tax-bps", type=int, default=200,
                    help="refuse to buy above this snipe tax (Pons charges "
                         "9900bps in the launch second)")
    ap.add_argument("--slippage-bps", type=int, default=300)
    ap.add_argument("--max-creator-tax-bps", type=int, default=400,
                    help="hard-block a buy at or above this creator tax (per side). "
                         "Default 400: measured worst-on-every-axis. Low tax "
                         "(1-200) is the BEST group in the data and is never blocked.")
    ap.add_argument("--max-gas-pct", type=float, default=8.0,
                    help="refuse a buy whose OWN estimated gas exceeds this %% of "
                         "the stake. Gas price on RH Chain is flat; gasUsed is what "
                         "spikes (80k-4.2M observed), so this is the gate that works.")
    ap.add_argument("--hot-at", type=int, default=None,
                    help="distinct validated wallets that trigger the HOT banner "
                         "and pre-build the buy (default 4)")
    ap.add_argument("--no-discord", action="store_true",
                    help="do not send Discord alerts on HOT")
    ap.add_argument("--no-autovet", action="store_true",
                    help="disable background DYOR pre-vetting")
    args = ap.parse_args()

    mon = Monitor(args)
    print(f"dev registry: {len(mon.reg)} devs, "
          f"{sum(1 for v in mon.reg.values() if v.get('graduations',0)>0)} with a graduation")
    print(f"venues enabled: {[k for k,v in VENUES.items() if v['enabled']]}")
    if mon.watch:
        print(f"WATCHING tickers: {sorted(mon.watch)}  "
              f"(all matches shown; squatters are common — nothing is auto-picked)")
    if mon.smart:
        if args.hot_at:
            mon.smart.hot_at = args.hot_at
        print(f"smart-money: {len(mon.smart.smart)} validated wallets loaded "
              f"(alert at {mon.smart.alert_at}+, HOT auto-arm at {mon.smart.hot_at}+)")
    print("ARMED — [y] will sign and send" if args.arm
          else "DISARMED — [b] builds and shows a tx, [y] will NOT sign "
               "(restart with --arm to enable)")
    threading.Thread(target=mon.ws_worker, daemon=True).start()
    for _ in range(6):     # enrichment is ~7s/row and RPC-bound, not CPU-bound
        threading.Thread(target=mon.enrich_worker, daemon=True).start()
    if not args.no_autovet:
        threading.Thread(target=mon.autovet_worker, daemon=True).start()
    threading.Thread(target=mon.progress_worker, daemon=True).start()
    threading.Thread(target=mon.refresh_worker, daemon=True).start()
    threading.Thread(target=mon.poll_worker, daemon=True).start()
    # seed off-thread: it is one scan and must not delay the first frame
    threading.Thread(target=mon.seed_pools, daemon=True).start()
    mon.seed_crossed()          # cheap, and PRIME is unusable without it
    threading.Thread(target=mon.hot_worker, daemon=True).start()
    threading.Thread(target=mon.cost_worker, daemon=True).start()
    threading.Thread(target=mon.account_worker, daemon=True).start()
    try:
        run_plain(mon) if args.no_tui else run_tui(mon)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
