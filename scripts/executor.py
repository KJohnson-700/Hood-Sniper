#!/usr/bin/env python3
"""
Hood Sniper -- executor. Builds, confirms and (only when armed) signs a buy.

SAFETY MODEL
------------
* **Disarmed by default.** Without `--arm` it prints exactly what it would do
  and exits. Arming additionally requires a key in the environment.
* **Hard caps are compiled in** and cannot be raised by a flag: $25/trade,
  $150/day, 3 open positions. A bug or a bad filter cannot exceed them.
* **You confirm every trade.** The transaction is pre-built before the prompt so
  the keypress is the only latency added (~1s), not the RPC round trips.
* **Snipe-tax guard.** Pons charges 99% in the launch second, decaying to 0 over
  3s. The executor reads `currentSnipeTaxBps` and refuses above --max-tax-bps.
* The key is read from the environment, never logged, never written to disk.
  Use a burner funded with trading float only.

    python3 executor.py --curve 0x… --usd 25              # dry run
    python3 executor.py --curve 0x… --usd 25 --arm        # will prompt to sign
"""
import argparse
import itertools
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
from ethsign import keccak256, priv_to_addr, sign_1559  # noqa: E402

JOURNAL = os.path.join(DATA, "executions.jsonl")
RPCS = ["https://rpc.mainnet.chain.robinhood.com",
        "https://robinhood-rpc.publicnode.com"]
UA = "Mozilla/5.0"
_rr = itertools.cycle(RPCS)

CHAIN_ID = 4663
ETH_USD = 2450.0

# --- HARD CAPS. Not configurable by flag. -----------------------------------
MAX_TRADE_USD = 25.0
MAX_DAILY_USD = 150.0
MAX_OPEN = 3

SEL_BUY = "0x59a87bc1"          # buy(uint256,uint256,address)
SEL_GRADUATED = "0xe7c2b772"
SEL_READY = "0xc68360a5"
SEL_SNIPETAX = "0xd7e1ef39"     # currentSnipeTaxBps(address)
SEL_TOKEN = "0xfc0c546a"
SEL_PAIRTOKEN = "0x3de35b79"


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


def call(to, data):
    return (rpc("eth_call", [{"to": to, "data": data}, "latest"]) or {}).get("result")


def as_int(r):
    try:
        return int(r, 16) if r and r != "0x" else None
    except (TypeError, ValueError):
        return None


def enc_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def enc_uint(n):
    return hex(n)[2:].rjust(64, "0")


def today_spent():
    if not os.path.exists(JOURNAL):
        return 0.0, 0
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    spent, opened = 0.0, 0
    with open(JOURNAL) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("sent") and str(r.get("ts_utc", "")).startswith(day):
                spent += r.get("usd", 0)
                opened += 1
    return spent, opened


def preflight(curve, usd, max_tax_bps, args):
    """Every reason to refuse, checked before a key is ever touched."""
    fails, warns, info = [], [], {}

    if usd > MAX_TRADE_USD:
        fails.append(f"size ${usd:.2f} exceeds hard cap ${MAX_TRADE_USD:.2f}")
    spent, opened = today_spent()
    info["spent_today_usd"] = round(spent, 2)
    info["open_today"] = opened
    if spent + usd > MAX_DAILY_USD:
        fails.append(f"daily cap: ${spent:.2f} spent + ${usd:.2f} > ${MAX_DAILY_USD:.2f}")
    if opened >= MAX_OPEN:
        fails.append(f"already {opened} positions opened today (max {MAX_OPEN})")

    grad = as_int(call(curve, SEL_GRADUATED))
    ready = as_int(call(curve, SEL_READY))
    info["graduated"] = bool(grad)
    if grad:
        fails.append("curve already graduated — buy() reverts; trade the V4 pool instead")
    if ready:
        warns.append("curve is ready to graduate; buy may revert on the race")

    tok = call(curve, SEL_TOKEN)
    info["token"] = ("0x" + tok[-40:]) if tok and len(tok) >= 66 else None
    pair = call(curve, SEL_PAIRTOKEN)
    pair_addr = ("0x" + pair[-40:]) if pair and len(pair) >= 66 else None
    info["pair_token"] = pair_addr
    native = pair_addr is None or int(pair_addr, 16) == 0
    info["native_quote"] = native
    if not native:
        fails.append(f"quote is ERC-20 ({pair_addr}); this path only handles native ETH "
                     "(an approve would be required first)")

    addr = args.address or "0x" + "0" * 40
    tax = as_int(call(curve, SEL_SNIPETAX + enc_addr(addr)))
    info["snipe_tax_bps"] = tax
    if tax is None:
        warns.append("could not read currentSnipeTaxBps")
    elif tax > max_tax_bps:
        fails.append(f"snipe tax {tax} bps ({tax/100:.2f}%) exceeds --max-tax-bps "
                     f"{max_tax_bps} — Pons charges 99% in the launch second")
    return fails, warns, info


def simulate_buy(curve, wei, addr):
    """
    eth_call the buy to learn tokensOut, so minTokensOut can be set properly.

    Sending minTokensOut=0 means accepting ANY fill -- a sandwich could return
    dust and it would still succeed. This is the difference between a slippage
    limit and none at all.
    """
    data = SEL_BUY[2:] + enc_uint(wei) + enc_uint(0) + enc_addr(addr)
    r = rpc("eth_call", [{"from": addr, "to": curve, "value": hex(wei),
                          "data": "0x" + data}, "latest"])
    return as_int(r.get("result")) if "result" in r else None


def build_tx(curve, usd, addr, slippage_bps, gas_mult=1.3):
    wei = int(usd / ETH_USD * 1e18)
    expected = simulate_buy(curve, wei, addr)
    if expected:
        min_out = expected * (10_000 - slippage_bps) // 10_000
    else:
        min_out = 0
    # SEL_* already carry the 0x prefix; strip it before concatenating or the
    # payload becomes "0x0x59a87bc1…" and the node rejects the whole call.
    data = SEL_BUY[2:] + enc_uint(wei) + enc_uint(min_out) + enc_addr(addr)
    nonce = as_int((rpc("eth_getTransactionCount", [addr, "pending"]) or {}).get("result")) or 0
    tip = as_int((rpc("eth_maxPriorityFeePerGas", []) or {}).get("result")) or 10 ** 8
    blk = (rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("result") or {}
    base = as_int(blk.get("baseFeePerGas")) or 10 ** 8
    est = rpc("eth_estimateGas", [{"from": addr, "to": curve,
                                   "value": hex(wei), "data": "0x" + data}])
    gas = as_int(est.get("result")) if "result" in est else None
    err = (est.get("error") or {}).get("message") if "error" in est else None
    return {
        "chainId": CHAIN_ID, "nonce": nonce,
        "maxPriorityFeePerGas": tip,
        "maxFeePerGas": int(base * 2 + tip),
        "gas": int((gas or 400_000) * gas_mult),
        "to": curve, "value": wei, "data": "0x" + data,
    }, {"gas_estimate": gas, "estimate_error": err, "wei": wei,
        "expected_tokens": expected, "min_tokens_out": min_out}


def main():
    ap = argparse.ArgumentParser(description="Hood Sniper executor (disarmed by default)")
    ap.add_argument("--curve", required=True, help="Pons curve address to buy from")
    ap.add_argument("--usd", type=float, default=25.0)
    ap.add_argument("--slippage-bps", type=int, default=300,
                    help="max acceptable shortfall vs the simulated fill "
                         "(default 300 = 3%%). Sets minTokensOut so a sandwich "
                         "cannot hand you dust.")
    ap.add_argument("--max-tax-bps", type=int, default=50,
                    help="refuse if the snipe tax is above this (default 50 = 0.5%%)")
    ap.add_argument("--arm", action="store_true", help="allow signing after confirmation")
    ap.add_argument("--address", help="override sender (defaults to the key's address)")
    a = ap.parse_args()

    key = os.environ.get("HOOD_SNIPER_PRIVATE_KEY", "").strip()
    if key:
        try:
            a.address = a.address or priv_to_addr(key)
        except Exception as e:  # noqa: BLE001
            print(f"key present but unusable: {e}")
            return
        want = os.environ.get("HOOD_SNIPER_EXPECT_ADDRESS", "").strip().lower()
        if want and want != a.address.lower():
            print(f"REFUSING: key derives to {a.address} but HOOD_SNIPER_EXPECT_ADDRESS "
                  f"is {want}")
            return

    print(f"\n  curve   {a.curve}")
    print(f"  size    ${a.usd:.2f}   sender {a.address or '(no key loaded)'}")
    fails, warns, info = preflight(a.curve, a.usd, a.max_tax_bps, a)
    print(f"  token   {info.get('token')}")
    tax = info.get("snipe_tax_bps")
    print(f"  snipe tax {tax} bps ({tax/100:.2f}%)" if tax is not None else "  snipe tax ?")
    print(f"  today   ${info['spent_today_usd']:.2f} spent · {info['open_today']}/{MAX_OPEN} opened")
    for w in warns:
        print(f"  WARN  {w}")
    for f in fails:
        print(f"  BLOCK {f}")
    if fails:
        print("\n  refusing — preflight failed.\n")
        return

    tx, meta = build_tx(a.curve, a.usd, a.address or "0x" + "0" * 40, a.slippage_bps)
    if meta["estimate_error"]:
        print(f"  BLOCK gas estimate reverted: {meta['estimate_error']}")
        print("  refusing — the buy would revert on-chain.\n")
        return
    print(f"  gas     est {meta['gas_estimate']} → limit {tx['gas']}   "
          f"maxFee {tx['maxFeePerGas']/1e9:.2f} gwei")
    print(f"  value   {meta['wei']/1e18:.6f} ETH")
    exp, mo = meta.get("expected_tokens"), meta.get("min_tokens_out")
    if exp:
        print(f"  fill    expect {exp/1e18:,.0f} tokens · minOut {mo/1e18:,.0f} "
              f"({a.slippage_bps/100:.1f}% tolerance)")
    else:
        print("  fill    WARN could not simulate — minTokensOut is 0 (NO slippage floor)")

    if not a.arm:
        print("\n  DRY RUN — not armed. Re-run with --arm to be prompted to sign.\n")
        return
    if not key:
        print("\n  ARMED but HOOD_SNIPER_PRIVATE_KEY is not set — nothing to sign.\n")
        return

    print(f"\n  BUY ${a.usd:.2f}?  [y] sign and send   [any other key] cancel: ", end="", flush=True)
    ans = sys.stdin.readline().strip().lower()
    if ans != "y":
        print("  cancelled.\n")
        return

    raw = sign_1559(tx, key)
    res = rpc("eth_sendRawTransaction", [raw])
    txh = res.get("result")
    err = (res.get("error") or {}).get("message")
    rec = {"ts_utc": datetime.now(timezone.utc).isoformat(), "curve": a.curve,
           "token": info.get("token"), "usd": a.usd, "sent": bool(txh),
           "tx": txh, "error": err, "snipe_tax_bps": tax, "nonce": tx["nonce"],
           "expected_tokens": meta.get("expected_tokens"),
           "min_tokens_out": meta.get("min_tokens_out"),
           "slippage_bps": a.slippage_bps}
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"  {'SENT ' + txh if txh else 'FAILED: ' + str(err)}\n")


if __name__ == "__main__":
    main()
