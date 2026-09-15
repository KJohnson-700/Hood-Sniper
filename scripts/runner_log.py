#!/usr/bin/env python3
"""
Weekly runner log — which coins ran, and who launched them.

WHY, given Phase 0 already killed the deployer filter. Phase 0 asked "does a dev's
PAST success predict their next launch" and answered no, because devs did not repeat
(92% launched once). That number has moved: on 45,891 launches now recorded, only
48.8% of devs launch once and 26.9% launch five or more times.

So the question is worth re-asking, and re-asked against outcomes it gives a clear
answer -- just not the hoped-for one:

    dev launches   curves   heated   vs base(9.5%)
      1            10,947    11.6%     +2.2
      2             1,326     7.2%     -2.3
      3-4             801     6.2%     -3.2
      5-9             568     7.6%     -1.9
      10+           2,531     2.8%     -6.7

Serial launchers are MONOTONICALLY worse -- 2.8% vs 11.6%, a 4x gap. There is real
signal in the deployer, it just points the other way: this is a penalty on spammers,
not a bonus for veterans. That is why the monitor flags SERIAL rather than trusting
a "proven dev".

This script keeps the rolling record so the finding can be re-checked as data grows,
and so a dev who does start repeating winners would eventually show up.

    python3 runner_log.py --update     # fold today's runners into the log
    python3 runner_log.py --report     # the week: runners, devs, repeat check
"""
import argparse
import json
import os
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
FEED = os.path.join(DATA, "monitor_feed.jsonl")
CROSS = os.path.join(DATA, "grad_forward.jsonl")
LOG = os.path.join(DATA, "runner_log.json")


def _load(p):
    out = []
    if not os.path.exists(p):
        return out
    for line in open(p):
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def update(log=print):
    """Fold every recorded threshold crossing into a persistent per-dev record."""
    feed = _load(FEED)
    cross = _load(CROSS)
    curve = {}
    for r in feed:
        c = r.get("curve")
        if c:
            # keep the richest row we have for each curve
            prev = curve.get(c) or {}
            curve[c] = {**prev, **{k: v for k, v in r.items() if v is not None}}
    store = {}
    if os.path.exists(LOG):
        try:
            store = json.load(open(LOG))
        except Exception:  # noqa: BLE001
            store = {}
    runners = store.setdefault("runners", {})
    for x in cross:
        c = x.get("curve")
        if not c:
            continue
        row = curve.get(c) or {}
        rec = runners.setdefault(c, {"curve": c, "first_seen": x.get("ts")})
        rec["level"] = "near_grad" if x.get("level") == "near_grad" else rec.get("level", "heating")
        rec["ts"] = max(rec.get("ts", 0), x.get("ts", 0))
        for k in ("symbol", "token", "dev", "tax_bps", "mcap"):
            if row.get(k) is not None:
                rec[k] = row[k]
        rec["peak_eth"] = max(rec.get("peak_eth", 0) or 0, x.get("eth") or 0)
    store["updated"] = time.time()
    json.dump(store, open(LOG, "w"), indent=1)
    log(f"  runners logged: {len(runners):,}")
    return store


def report(days=7, log=print):
    store = update(log=lambda *a: None)
    runners = store.get("runners", {})
    cut = time.time() - days * 86400
    recent = [r for r in runners.values() if (r.get("ts") or 0) >= cut]
    log(f"runners in the last {days}d: {len(recent):,}  (all time {len(runners):,})\n")
    ng = [r for r in recent if r.get("level") == "near_grad"]
    log(f"  reached NEAR GRADUATION: {len(ng)}")

    devs = Counter(r["dev"] for r in recent if r.get("dev"))
    log(f"  distinct devs behind them: {len(devs):,}")
    rep = {d: n for d, n in devs.items() if n > 1}
    log(f"  devs with MORE THAN ONE runner: {len(rep)}")
    if rep:
        log(f"\n  {'dev':<44}{'runners':>9}")
        for d, n in sorted(rep.items(), key=lambda kv: -kv[1])[:12]:
            names = [r.get("symbol") for r in recent if r.get("dev") == d][:4]
            log(f"    {d:<42}{n:>9}   {', '.join(str(x) for x in names if x)}")
        log("\n  These are the only wallets with any repeat evidence. Treat a short")
        log("  list as anecdote -- the measured gradient says serial launchers do")
        log("  WORSE, so a repeat winner is the exception that needs explaining.")
    else:
        log("\n  No dev produced more than one runner this week. That is the finding,")
        log("  not a gap: it is what a 4x penalty on serial launchers looks like.")

    top = sorted(recent, key=lambda r: -(r.get("peak_eth") or 0))[:10]
    if top:
        log(f"\n  biggest runners of the week:")
        log(f"    {'symbol':<14}{'peak raised':>13}{'level':>12}  dev")
        for r in top:
            log(f"    {str(r.get('symbol'))[:12]:<14}"
                f"${(r.get('peak_eth') or 0)*2450:>11,.0f}"
                f"{str(r.get('level')):>12}  {str(r.get('dev'))[:20]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    if a.update:
        update()
    elif a.report:
        report(a.days)
    else:
        ap.print_help()
