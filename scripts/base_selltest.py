#!/usr/bin/env python3
"""
Prove the Base v4 SELL encoder without owning the token or spending anything.

Why this exists: the Base stack could buy long before it could be shown to sell, and
"we'll find out at exit" is the worst possible time to discover a bad encoder. A plain
simulation from an empty address always reverts (no balance, no approval), which proves
nothing. This uses eth_call STATE OVERRIDES to fake exactly the two things a real
position would have -- a token balance and the Permit2 approval chain -- and then runs
the REAL build_sell_calldata against a REAL pool.

Storage slots are discovered by probing rather than assumed, because they differ per
token (this one: balances slot 5, allowance slot 6; Permit2 allowance slot 1).

    python3 base_selltest.py --token 0x… --pool-id 0x…      # or auto-pick a live pool
"""
import argparse, itertools, json, sys, os, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from ethsign import keccak256
from v4sell import build_sell_calldata
from base_buy import UNIVERSAL_ROUTER, PERMIT2

WHO = "0x1111111111111111111111111111111111111111"
SEL_BAL, SEL_ALW, SEL_P2ALW = "0x70a08231", "0xdd62ed3e", "0x927da105"
# only these endpoints accept state overrides (meowrpc/tenderly do not)
URLS = itertools.cycle(["https://base.drpc.org", "https://mainnet.base.org",
                        "https://base-rpc.publicnode.com"])


def rpc(method, params, tries=6):
    for _ in range(tries):
        u = next(URLS)
        try:
            req = urllib.request.Request(
                u, data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "method": method, "params": params}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return {}


def _m(k, s):
    return keccak256(bytes.fromhex(k[2:].rjust(64, "0") + hex(s)[2:].rjust(64, "0")))


def bal_slot_of(tok, s):      return "0x" + _m(WHO, s).hex()
def alw_slot_of(tok, sp, s):  return "0x" + keccak256(bytes.fromhex(sp[2:].rjust(64, "0") + _m(WHO, s).hex())).hex()
def p2_slot_of(tok, sp, s):
    a = _m(WHO, s)
    b = keccak256(bytes.fromhex(tok[2:].rjust(64, "0") + a.hex()))
    return "0x" + keccak256(bytes.fromhex(sp[2:].rjust(64, "0") + b.hex())).hex()


def discover(tok, log=print):
    """Probe for the storage slots. Assuming them silently produces false passes."""
    MAGIC = 12345 * 10 ** 18
    bal = alw = p2 = None
    for s in range(10):
        ov = {tok: {"stateDiff": {bal_slot_of(tok, s): "0x" + hex(MAGIC)[2:].rjust(64, "0")}}}
        r = rpc("eth_call", [{"to": tok, "data": SEL_BAL + "0" * 24 + WHO[2:]}, "latest", ov])
        if r.get("result") and int(r["result"], 16) == MAGIC:
            bal = s; break
        time.sleep(0.2)
    for s in range(10):
        ov = {tok: {"stateDiff": {alw_slot_of(tok, PERMIT2, s): "0x" + "f" * 64}}}
        r = rpc("eth_call", [{"to": tok, "data": SEL_ALW + "0" * 24 + WHO[2:] + "0" * 24 + PERMIT2[2:]},
                             "latest", ov])
        if r.get("result") and int(r["result"], 16) > 10 ** 30:
            alw = s; break
        time.sleep(0.2)
    AMT = (1 << 160) - 1
    PACKED = (((1 << 48) - 1) << 160) | AMT
    data = SEL_P2ALW + "0" * 24 + WHO[2:] + "0" * 24 + tok[2:] + "0" * 24 + UNIVERSAL_ROUTER[2:]
    for s in range(6):
        ov = {PERMIT2: {"stateDiff": {p2_slot_of(tok, UNIVERSAL_ROUTER, s):
                                      "0x" + hex(PACKED)[2:].rjust(64, "0")}}}
        r = rpc("eth_call", [{"to": PERMIT2, "data": data}, "latest", ov])
        res = r.get("result")
        if res and len(res) >= 66 and int(res[2:66], 16) == AMT:
            p2 = s; break
        time.sleep(0.2)
    log(f"  slots — balance {bal} · allowance {alw} · permit2 {p2}")
    return bal, alw, p2


def prove(tok, key, amount=10 ** 18, log=print):
    bal, alw, p2 = discover(tok, log)
    if None in (bal, alw, p2):
        log("  could not locate all slots — INCONCLUSIVE (not a pass)")
        return False
    AMT = (1 << 160) - 1
    PACKED = (((1 << 48) - 1) << 160) | AMT
    ov = {tok: {"stateDiff": {bal_slot_of(tok, bal): "0x" + hex(amount)[2:].rjust(64, "0"),
                              alw_slot_of(tok, PERMIT2, alw): "0x" + "f" * 64}},
          PERMIT2: {"stateDiff": {p2_slot_of(tok, UNIVERSAL_ROUTER, p2):
                                  "0x" + hex(PACKED)[2:].rjust(64, "0")}},
          WHO: {"balance": "0xde0b6b3a7640000"}}
    cd = build_sell_calldata(tok, key["c0"], key["c1"], key["fee"], key["tsp"],
                             key["hooks"], amount, 0, int(time.time()) + 600)
    r = rpc("eth_call", [{"from": WHO, "to": UNIVERSAL_ROUTER, "value": "0x0", "data": cd},
                         "latest", ov])
    if "result" in r:
        log("  *** SELL SIMULATION SUCCEEDED — exit route proven ***")
        return True
    log(f"  REVERTED: {(r.get('error') or {}).get('message', '?')[:150]}")
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--c0", required=True); ap.add_argument("--c1", required=True)
    ap.add_argument("--fee", type=int, required=True)
    ap.add_argument("--tick-spacing", type=int, required=True)
    ap.add_argument("--hooks", default="0x" + "0" * 40)
    ap.add_argument("--amount", type=int, default=10 ** 18)
    a = ap.parse_args()
    ok = prove(a.token.lower(),
               {"c0": a.c0, "c1": a.c1, "fee": a.fee,
                "tsp": a.tick_spacing, "hooks": a.hooks}, a.amount)
    sys.exit(0 if ok else 1)
