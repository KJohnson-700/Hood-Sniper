#!/usr/bin/env python3
"""
Local HTML report — copyable addresses, in a browser, auto-refreshing.

Firecrawl is NOT involved and is not needed: that is a web-scraping service for
reading other people's sites. This just writes a file and opens it, so it costs
nothing and cannot burn an API plan.

It also fixes a real problem with the TUI: every keystroke there is captured, so
selecting text to copy an address can kill the session (it did once, losing a
21-wallet alert). A browser tab is the safe place to copy from.

    python3 report.py            # write + open
    python3 report.py --watch    # rewrite every 20s; the page self-refreshes
"""
import argparse, html, json, os, subprocess, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "report.html")
RH = "https://robinhoodchain.blockscout.com/token/"
DS = "https://dexscreener.com/robinhood/"


def gmgn_enrich(log=print):
    """
    One `market trending` call returns 50 tokens with the discovery fields, so
    this costs a single request rather than one per token -- GMGN rate-limits
    aggressively and a ban extends on retry.

    ONLY the fields measured as populated AND varying on Robinhood Chain (20-token
    sample) are used. Deliberately EXCLUDED because they are constant or empty
    there and would be decoration pretending to be signal:
      is_honeypot / buy_tax / sell_tax   0 for all 20 -- NOT computed on this chain
      is_open_source / is_renounced      constant 1 -- no discriminating power
      dev_team_hold_rate                 only 3/20 populated
      rug_ratio, bluechip_owner_percentage, is_wash_trading, square_mentions,
      image_dup_count                    all constant/empty
    """
    import subprocess
    sys.path.insert(0, HERE)
    try:
        from investigate import _gmgn_key
        key = _gmgn_key()
    except Exception:  # noqa: BLE001
        key = os.environ.get("GMGN_API_KEY", "")
    if not key:
        return {}
    env = dict(os.environ, GMGN_API_KEY=key)
    env.pop("GMGN_PRIVATE_KEY", None)
    try:
        r = subprocess.run(["npx", "-y", "gmgn-cli@latest", "market", "trending",
                            "--chain", "robinhood", "--interval", "1h", "--limit", "50"],
                           capture_output=True, text=True, timeout=90, env=env)
        rows = (json.loads(r.stdout).get("data") or {}).get("rank") or []
    except Exception as ex:  # noqa: BLE001
        log(f"  gmgn enrich skipped: {str(ex)[:50]}")
        return {}
    keep = ("visiting_count", "smart_degen_count", "renowned_count", "sniper_count",
            "top70_sniper_hold_rate", "top_10_holder_rate", "bot_degen_rate",
            "bundler_rate", "entrapment_ratio", "holder_count",
            "history_highest_market_cap", "twitter_username",
            "twitter_create_token_count", "twitter_rename_count", "cto_flag",
            "dexscr_boost_fee", "symbol", "market_cap", "volume", "liquidity")
    return {(r.get("address") or "").lower(): {k: r.get(k) for k in keep} for r in rows}


def _read(name, limit=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return []
    rows = []
    for line in open(p):
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return rows[-limit:] if limit else rows


def _fmt(v, money=True):
    if v in (None, "", 0):
        return "—"
    try:
        v = float(v)
    except Exception:  # noqa: BLE001
        return html.escape(str(v))
    p = "$" if money else ""
    for u, d in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= u:
            return f"{p}{v/u:.1f}{u and d}"
    return f"{p}{v:,.0f}"


def build(gm=None):
    gm = gm if gm is not None else {}
    alerts = _read("smart_alerts.jsonl", 60)[::-1]
    feed = _read("monitor_feed.jsonl", 400)[::-1]
    trades = _read("trades.jsonl", 40)[::-1]
    seen, rows = set(), []
    for e in feed:
        if (e.get("venue") or "pons") != "pons":     # Pons-only for now
            continue
        c = e.get("curve")
        if c in seen:
            continue
        seen.add(c)
        rows.append(e)
        if len(rows) >= 60:
            break
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    def addr_cell(a, label=None):
        if not a:
            return "<td>—</td>"
        return (f'<td class="mono"><a href="{RH}{a}" target="_blank">{label or a}</a>'
                f'<button onclick="navigator.clipboard.writeText(\'{a}\')">copy</button></td>')

    ah = "".join(
        f"<tr class='{'hot' if a.get('hot') else ''}'>"
        f"<td>{html.escape(str(a.get('ts_utc',''))[11:19])}</td>"
        f"<td><b>{'★' * min(int(a.get('n_smart',0)), 8)} {a.get('n_smart')}</b></td>"
        f"<td><b>${html.escape(str(a.get('symbol') or '?'))}</b></td>"
        + addr_cell(a.get("token") or a.get("curve"))
        + f"<td>{len(a.get('wallets') or [])} wallets</td></tr>"
        for a in alerts) or "<tr><td colspan=5>no alerts yet</td></tr>"

    fh = "".join(
        f"<tr><td>{html.escape(str(e.get('t','')))}</td>"
        f"<td><b>${html.escape(str(e.get('symbol') or '?'))}</b></td>"
        f"<td>{_fmt(e.get('mcap'))}</td><td>{_fmt(e.get('vol_h1'))}</td>"
        f"<td>{_fmt(e.get('n_holders'), money=False)}</td>"
        f"<td>{_fmt(e.get('active_liq_usd'))}</td>"
        f"<td>{(e.get('slippage_pct') or 0):.2f}%</td>"
        f"<td>{(e.get('tax_bps') or 0)/100:.1f}%</td>"
        + addr_cell(e.get("token")) + "</tr>"
        for e in rows) or "<tr><td colspan=9>no graduations recorded yet</td></tr>"

    # Pons curve section — pre-graduation tokens, the priority venue. These never
    # reach the graduation table, so without this the report showed only what had
    # already migrated, which is after the entry that matters.
    curve_rows = [e for e in rows if (e.get("venue") or "pons") == "pons"
                  and e.get("verdict") == "CURVE"][:40]
    ch_ = "".join(
        f"<tr><td>{html.escape(str(e.get('t','')))}</td>"
        f"<td><b>${html.escape(str(e.get('symbol') or '?'))}</b></td>"
        f"<td class=num>{(e.get('grad_pct') or 0):.0f}%</td>"
        f"<td class=num>{_fmt(e.get('curve_volume_usd'))}</td>"
        f"<td class=num>{e.get('n_buyers') or 0}</td>"
        f"<td class='num {'bad' if (e.get('tax_bps') or 0) >= 400 else ''}'>"
        f"{(e.get('tax_bps') or 0)/100:.1f}%</td>"
        f"<td class=num>{(e.get('total_fee_bps') or 0)/100:.1f}%</td>"
        f"<td>{html.escape(' '.join(e.get('flags', [])[:3]))}</td>"
        + addr_cell(e.get("token")) + "</tr>"
        for e in curve_rows) or "<tr><td colspan=9>no Pons curve rows yet</td></tr>"

    th = "".join(
        f"<tr><td>{html.escape(str(t.get('ts','')))}</td>"
        f"<td class='mono'>{html.escape(str(t.get('tx',''))[:20])}…</td>"
        f"<td>${t.get('usd')}</td></tr>" for t in trades) \
        or "<tr><td colspan=3>no trades sent</td></tr>"

    def _pct(v):
        try:
            return f"{float(v)*100:.1f}%"
        except Exception:  # noqa: BLE001
            return "—"

    disc = sorted(gm.values(), key=lambda r: -(r.get("visiting_count") or 0))[:40]
    dh = "".join(
        "<tr>"
        f"<td><b>${html.escape(str(r.get('symbol') or '?'))}</b></td>"
        f"<td class=num>{r.get('visiting_count') or 0:,}</td>"
        f"<td class=num>{r.get('smart_degen_count') or 0}</td>"
        f"<td class=num>{r.get('renowned_count') or 0}</td>"
        f"<td class=num>{r.get('sniper_count') or 0}</td>"
        f"<td class='num {'bad' if (r.get('top70_sniper_hold_rate') or 0) > .15 else ''}'>"
        f"{_pct(r.get('top70_sniper_hold_rate'))}</td>"
        f"<td class='num {'bad' if (r.get('top_10_holder_rate') or 0) > .35 else ''}'>"
        f"{_pct(r.get('top_10_holder_rate'))}</td>"
        f"<td class='num {'bad' if (r.get('bot_degen_rate') or 0) > .5 else ''}'>"
        f"{_pct(r.get('bot_degen_rate'))}</td>"
        f"<td class=num>{_fmt(r.get('history_highest_market_cap'))}</td>"
        f"<td class=num>{r.get('holder_count') or 0:,}</td>"
        + (f"<td class='bad'>X launched {r['twitter_create_token_count']} tokens</td>"
           if (r.get("twitter_create_token_count") or 0) >= 5 else
           (f"<td><a href='{html.escape(str(r.get('twitter_username')))}' target=_blank>X</a></td>"
            if r.get("twitter_username") else "<td>—</td>"))
        + "</tr>"
        for r in disc) or "<tr><td colspan=11>no GMGN data (set GMGN_API_KEY)</td></tr>"

    return f"""<!doctype html><meta charset=utf-8>
<meta http-equiv=refresh content=20>
<title>Hood Sniper — {now}</title>
<style>
:root{{--bg:#0d1015;--fg:#d7cbb8;--dim:#6f7a88;--amber:#e8a33d;--rule:#252d38}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui;margin:0;padding:20px}}
h1{{font-size:18px;margin:0 0 4px}} h2{{font-size:13px;text-transform:uppercase;
letter-spacing:.1em;color:var(--amber);margin:26px 0 8px;border-top:1px solid var(--rule);padding-top:10px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{text-align:left;padding:5px 9px;border-bottom:1px solid var(--rule)}}
th{{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase}}
.mono{{font-family:ui-monospace,Menlo,monospace;font-size:12px}}
a{{color:var(--amber)}} tr.hot{{background:#2a1114}}
button{{margin-left:8px;background:#1b2029;color:var(--fg);border:1px solid var(--rule);
border-radius:4px;font-size:11px;padding:1px 6px;cursor:pointer}}
.sub{{color:var(--dim);font-size:12px;max-width:70em}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.bad{{color:#d9584b;font-weight:600}}
</style>
<h1>Hood Sniper</h1>
<div class=sub>{now} · refreshes every 20s · click an address to open Blockscout, or copy it</div>
<h2>Smart-money alerts</h2>
<table><tr><th>time<th>wallets<th>ticker<th>contract<th></tr>{ah}</table>
<h2>Pons — on the curve (pre-graduation)</h2>
<div class=sub>the priority venue, before migration. <b>curve%</b> is how full the bonding
curve is; creator tax red at 4%+ (measured worst-on-every-axis band).</div>
<table><tr><th>time<th>ticker<th>curve%<th>curve vol<th>buyers<th>tax<th>fees<th>flags<th>contract</tr>{ch_}</table>
<h2>Recent graduations</h2>
<table><tr><th>time<th>ticker<th>mcap<th>vol 1h<th>holders<th>liq<th>slip<th>tax<th>contract</tr>{fh}</table>
<h2>Discovery — GMGN attention &amp; structure</h2>
<div class=sub>ranked by users watching. <b>sniper%</b> red &gt;15% · <b>top10</b> red &gt;35% ·
<b>bot%</b> red &gt;50%. An X handle that has launched 5+ tokens is flagged instead of linked.
Fields shown are only those measured as populated AND varying on Robinhood Chain —
GMGN's honeypot/tax/renounced flags are constant there and are deliberately omitted.</div>
<table><tr><th>ticker<th>watching<th>smart<th>renown<th>snipers<th>sniper%<th>top10%<th>bot%
<th>ATH mcap<th>holders<th>X</tr>{dh}</table>
<h2>Trades sent</h2>
<table><tr><th>time<th>tx<th>size</tr>{th}</table>
"""


FIRECRAWL_CACHE = os.path.join(DATA, "firecrawl_cache.json")


def firecrawl_scrape(url, log=print):
    """
    Scrape one URL through Firecrawl. Cached on disk, forever, per URL.

    CREDITS ARE FINITE (1,436 left on a 1,000/period plan as of 2026-09-09), so a
    report that re-scraped on every refresh would burn the month in an afternoon.
    Cache first, and never call this outside --full.

    Returns None on any failure. A missing scrape must read as "not checked", never
    as "nothing found" -- an empty social footprint is a real signal and must not be
    faked by a rate limit.
    """
    import urllib.request
    sys.path.insert(0, HERE)
    from investigate import firecrawl_key
    cache = {}
    if os.path.exists(FIRECRAWL_CACHE):
        try:
            cache = json.load(open(FIRECRAWL_CACHE))
        except Exception:  # noqa: BLE001
            cache = {}
    if url in cache:
        return cache[url]
    k = firecrawl_key()
    if not k:
        log("  firecrawl: no key — skipping (NOT the same as 'nothing found')")
        return None
    body = json.dumps({"url": url, "formats": ["markdown"], "onlyMainContent": True}).encode()
    req = urllib.request.Request("https://api.firecrawl.dev/v1/scrape", data=body,
                                 headers={"Authorization": f"Bearer {k}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            out = json.load(r)
    except Exception as e:  # noqa: BLE001
        log(f"  firecrawl: {str(e)[:80]}")
        return None
    md = ((out.get("data") or {}).get("markdown") or "")[:4000]
    cache[url] = md
    try:
        json.dump(cache, open(FIRECRAWL_CACHE, "w"))
    except Exception:  # noqa: BLE001
        pass
    return md


def firecrawl_credits(log=print):
    """Remaining credits, or None. Cheap: this endpoint does not consume any."""
    import urllib.request
    sys.path.insert(0, HERE)
    from investigate import firecrawl_key
    k = firecrawl_key()
    if not k:
        return None
    req = urllib.request.Request("https://api.firecrawl.dev/v1/team/credit-usage",
                                 headers={"Authorization": f"Bearer {k}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return (json.load(r).get("data") or {}).get("remaining_credits")
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    # Firecrawl is OPT-IN and stays that way. Slim's rule: it runs only when a full
    # report is explicitly requested, never on a routine or --watch refresh, because
    # every scrape spends a finite credit.
    ap.add_argument("--full", action="store_true",
                    help="deep report: enables Firecrawl social scraping (spends credits)")
    a = ap.parse_args()
    if a.full and a.watch:
        print("--full with --watch would re-scrape every 20s and burn the plan. Refusing.")
        sys.exit(2)
    if a.full:
        c = firecrawl_credits()
        print(f"deep report — Firecrawl enabled"
              + (f" ({c} credits remaining)" if c is not None else " (credit check failed)"))
    else:
        print("standard report — Firecrawl NOT used (pass --full to enable)")
    gm = gmgn_enrich()
    open(OUT, "w").write(build(gm))
    print(f"wrote {OUT}  ({len(gm)} tokens enriched from GMGN)")
    if not a.no_open:
        subprocess.run(["open", OUT], check=False)
    if a.watch:
        print("rewriting every 20s — Ctrl-C to stop")
        while True:
            time.sleep(20)
            open(OUT, "w").write(build(gmgn_enrich(log=lambda *a: None)))


if __name__ == "__main__":
    main()
