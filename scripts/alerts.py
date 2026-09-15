#!/usr/bin/env python3
"""
Discord alerts for Hood Sniper.

DESIGN GOAL: an alert you can ACT on from a phone without opening the terminal.
That means the decision inputs are in the message itself -- can I get out, what
does it cost, who is in it, did vetting object -- and the contract address is in a
code block so one tap copies it clean.

What is deliberately NOT here:
  * no price predictions or scores. Nothing in this project has earned that.
  * no "BUY NOW". The bot is disarmed; an alert is a prompt to look, not an order.
  * no raw dumps. A wall of JSON on a phone is unreadable and gets ignored, and an
    ignored alert is worse than none.

Colour encodes the VETTING verdict, not excitement: red = blocked, amber = caution,
green = clear. So the thing that decides whether to touch it is visible before a
single word is read.

    python3 alerts.py --test          # send a sample alert to the webhook
    python3 alerts.py --status        # is a webhook configured (no value shown)
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
sys.path.insert(0, HERE)
from investigate import env_key  # noqa: E402

SENT_LOG = os.path.join(DATA, "alerts_sent.jsonl")
COOLDOWN_SEC = 900          # one alert per token per 15 min

COLOR_CLEAR = 0x2ECC71
COLOR_CAUTION = 0xF1C40F
COLOR_BLOCKED = 0xE74C3C
COLOR_INFO = 0x5865F2

EXPLORER = "https://robinhoodchain.blockscout.com"


def webhook_url():
    return env_key("DISCORD_WEBHOOK_URL")


def _fmt_usd(v):
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"${v/1e6:.1f}M"
    if v >= 1000:
        return f"${v/1000:.1f}k"
    if v >= 10:
        return f"${v:.0f}"
    return f"${v:.2f}"


def build_embed(a):
    """
    One alert. `a` is a plain dict so this can be built from a live row, a journal
    line, or a test fixture without the caller knowing which.
    """
    sym = a.get("symbol") or "?"
    tok = a.get("token") or a.get("curve") or ""
    n = a.get("n_smart") or 0
    hot = bool(a.get("hot"))
    fails = a.get("vet_fails") or 0
    warns = a.get("vet_warns") or 0

    # Incomplete vetting is NOT clear. Green on an unvetted token is the single most
    # dangerous thing this message could do, so unfinished vetting renders amber.
    if fails:
        colour, verdict = COLOR_BLOCKED, f"BLOCKED · {fails} fail, {warns} warn"
    elif a.get("vet_state") not in (None, "done"):
        colour, verdict = COLOR_CAUTION, "UNVETTED"
    elif warns:
        colour, verdict = COLOR_CAUTION, f"CAUTION · {warns} warn"
    else:
        colour, verdict = COLOR_CLEAR, "CLEAR"

    slip = a.get("slippage_pct")
    tax = a.get("tax_bps")
    fee = a.get("total_fee_bps")
    conc = a.get("top1_share")

    # Exit cost first. Everything else is opinion; this is the number that decides
    # whether the position can be left at all.
    fields = [
        {"name": "Exit cost",
         "value": (f"slip **{slip:.2f}%** · fees **{(fee or 0)/100:.1f}%**\n"
                   f"round trip ≈ **{(2*(fee or 0)/100 + 2*(slip or 0)):.1f}%**")
         if slip is not None else "**unpriced** — cannot quote an exit",
         "inline": True},
        {"name": "Size",
         "value": f"mcap **{_fmt_usd(a.get('mcap'))}**\nliq **{_fmt_usd(a.get('active_liq_usd'))}**",
         "inline": True},
        {"name": "Smart money",
         "value": (f"**★{n}** wallet{'s' if n != 1 else ''}"
                   + (f"\ntop1 holds **{conc:.0%}** of buys" if conc else "")),
         "inline": True},
    ]
    if tax is not None:
        fields.append({"name": "Creator tax",
                       "value": (f"**{tax/100:.1f}%**"
                                 + (" ⚠️ extraction band" if tax >= 400
                                    else " ✅ measured-favourable band" if 0 < tax <= 200
                                    else "")),
                       "inline": True})
    if a.get("n_buyers") is not None:
        tape = f"**{a.get('n_buyers')}** buyers · **{a.get('n_sellers') or 0}** sellers"
        if a.get("n_holders"):
            tape += f"\n{a['n_holders']:,} holders"
        fields.append({"name": "Tape", "value": tape, "inline": True})

    # Buy-flow acceleration travels as the same sparkline the terminal draws, so the
    # phone and the desk show the SAME picture rather than two different summaries.
    if a.get("flow"):
        r = a.get("flow_rising")
        arrow = ("📈 accelerating" if r is True
                 else "flat / fading" if r is False
                 else "too new to call a trend")
        fields.append({"name": "New buyers", "value": f"`{a['flow']}`\n{arrow}", "inline": True})

    if a.get("dev"):
        g = a.get("dev_prior_grads")
        fields.append({"name": "Deployer",
                       "value": (f"`{a['dev'][:10]}…`\n"
                                 + (f"**{g}** prior graduation{'s' if g != 1 else ''}"
                                    if g else "no prior graduations")),
                       "inline": True})

    vet_state = a.get("vet_state")
    if vet_state and vet_state != "done":
        fields.append({"name": "Vetting",
                       "value": f"**{vet_state.upper()}** — not finished, treat as unknown",
                       "inline": True})
    else:
        fields.append({"name": "Vetting", "value": f"**{verdict}**", "inline": True})
    if a.get("vet_why"):
        fields.append({"name": "Why", "value": str(a["vet_why"])[:300], "inline": False})

    # The CA gets its own block: tap-to-copy on mobile, and no autolinking mangling.
    fields.append({"name": "Contract", "value": f"```\n{tok}\n```", "inline": False})
    fields.append({"name": "Dig in",
                   "value": (f"[Blockscout]({EXPLORER}/token/{tok}) · "
                             f"[DexScreener](https://dexscreener.com/robinhood/{tok}) · "
                             f"[GMGN](https://gmgn.ai/robinhood/token/{tok})"),
                   "inline": False})

    # An unresolved ticker must SAY so. "$?" on a phone is indistinguishable from a
    # token genuinely named that -- and tokens really are named NULL, ◢ and worse, so
    # the reader cannot tell a real name from a failed lookup without being told.
    if sym in ("?", "", None):
        title_sym = f"{tok[:10]}… (ticker unresolved)"
    else:
        title_sym = f"${sym}"
    return {
        "title": f"{'🔥 HOT · ' if hot else ''}{title_sym}",
        "description": (f"**{n} validated wallets** are in this launch."
                        if hot else f"{n} validated wallets seen."),
        "color": colour,
        "fields": fields,
        "footer": {"text": f"Hood Sniper · Robinhood Chain · bot is {a.get('mode','DISARMED')}"
                           f" · block {a.get('block','?')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _recently_sent(token):
    """Do not re-alert the same token within the cooldown. A repeated ping for one
    coin trains you to ignore the channel, which costs you the next real one."""
    if not os.path.exists(SENT_LOG):
        return False
    cut = time.time() - COOLDOWN_SEC
    try:
        with open(SENT_LOG) as f:
            for line in f:
                r = json.loads(line)
                if r.get("token") == token and r.get("ts", 0) > cut:
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def send(alert, force=False):
    """(ok, detail). Never raises — a failed alert must not take the monitor down."""
    url = webhook_url()
    if not url:
        return False, "DISCORD_WEBHOOK_URL not set"
    tok = alert.get("token") or alert.get("curve") or ""
    if not force and _recently_sent(tok):
        return False, "suppressed — already alerted within the cooldown"
    body = json.dumps({"embeds": [build_embed(alert)]}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "HoodSniper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code = r.status
    except Exception as e:  # noqa: BLE001
        return False, f"send failed: {str(e)[:120]}"
    if code not in (200, 204):
        return False, f"unexpected HTTP {code}"
    with open(SENT_LOG, "a") as f:
        f.write(json.dumps({"ts": time.time(), "token": tok,
                            "symbol": alert.get("symbol")}) + "\n")
    return True, f"sent (HTTP {code})"


SAMPLE = {"symbol": "SAMPLE", "token": "0x8925994e3625618052aaa6c7736045cc1cba97f4",
          "curve": "0x83d01a53a63cf288cca8a6e5c44d9470f08891be",
          "n_smart": 4, "hot": True, "block": 58390560, "mcap": 42800.0,
          "active_liq_usd": 489.05, "slippage_pct": 0.53, "tax_bps": 150,
          "total_fee_bps": 250, "n_buyers": 9, "n_sellers": 2, "top1_share": 0.31,
          "n_holders": 41, "dev": "0x0ea26a02ff109846c187aecd464ae9ae9e758ff3",
          "dev_prior_grads": 2, "flow": "▄▂▁▂▂▄▆█", "flow_rising": True,
          "vet_state": "done", "vet_fails": 0, "vet_warns": 2,
          "vet_why": "dexscreener — not indexed yet · dev — no history on record (new wallet)",
          "mode": "DISARMED"}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send a sample alert")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--preview", action="store_true", help="print the payload, send nothing")
    a = ap.parse_args()
    if a.status:
        u = webhook_url()
        print(f"DISCORD_WEBHOOK_URL: {'SET (%d chars)' % len(u) if u else 'not set'}")
    elif a.preview:
        print(json.dumps(build_embed(SAMPLE), indent=2))
    elif a.test:
        ok, detail = send(SAMPLE, force=True)
        print(("OK  " if ok else "FAIL ") + detail)
        sys.exit(0 if ok else 1)
    else:
        ap.print_help()


# --- graduation = exit clock -------------------------------------------------
# Measured on 197 real Pons graduations with forward-only price paths:
#
#     horizon   median    >=1.0x   <=0.5x
#       5m      0.702x      36%      42%
#      15m      0.310x      28%      57%
#       1h      0.160x      17%      71%
#
#   time to peak: p25 11s · median 89s · p75 17m · 36% peak within 30 SECONDS
#   35% touch >=2x at some point, but those take a median 14 min to get there
#
# So this alert exists to start a clock, not to suggest an entry. It deliberately
# carries no "buy" framing and no green.
COLOR_EXIT = 0xE67E22          # orange: act, but it is not an emergency

GRAD_STATS = [
    ("5 min", "0.702x", "42% already halved"),
    ("15 min", "0.310x", "57% halved"),
    ("1 hour", "0.160x", "71% halved"),
]


def build_graduated_embed(a):
    tok = a.get("token") or a.get("curve") or ""
    sym = a.get("symbol") or "?"
    title_sym = f"${sym}" if sym not in ("?", "", None) else f"{tok[:10]}…"

    decay = "\n".join(f"`{h:<7}` median **{m}** · {n}" for h, m, n in GRAD_STATS)
    fields = [
        {"name": "Why you're seeing this", "value": a.get("why", "followed"), "inline": False},
        {"name": "What happens next, measured",
         "value": decay + "\n\n*n=197 graduations, forward-only paths*", "inline": False},
        {"name": "Timing",
         "value": ("peak at **median 89s** · **36%** peak within **30s**\n"
                   "the ones that reach 2x take a median **14 min**"),
         "inline": True},
    ]
    if a.get("peak_eth"):
        fields.append({"name": "Curve raised",
                       "value": f"**{a['peak_eth']:.2f} ETH** at its peak", "inline": True})
    if a.get("n_smart"):
        fields.append({"name": "Smart money", "value": f"★{a['n_smart']} were in it",
                       "inline": True})
    fields.append({"name": "Contract", "value": f"```\n{tok}\n```", "inline": False})
    fields.append({"name": "Check it",
                   "value": (f"[DexScreener](https://dexscreener.com/robinhood/{tok}) · "
                             f"[GMGN](https://gmgn.ai/robinhood/token/{tok}) · "
                             f"[Blockscout]({EXPLORER}/token/{tok})"),
                   "inline": False})
    return {
        "title": f"🎓 GRADUATED · {title_sym}",
        "description": ("**The exit clock started.** Post-graduation is where the "
                        "median token gives it back — this is not an entry signal."),
        "color": COLOR_EXIT,
        "fields": fields,
        "footer": {"text": f"Hood Sniper · bot is {a.get('mode','DISARMED')} · "
                           f"block {a.get('block','?')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_graduated(a, force=False):
    url = webhook_url()
    if not url:
        return False, "DISCORD_WEBHOOK_URL not set"
    tok = a.get("token") or a.get("curve") or ""
    key = "grad:" + tok
    if not force and _recently_sent(key):
        return False, "suppressed — already alerted"
    body = json.dumps({"embeds": [build_graduated_embed(a)]}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "HoodSniper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code = r.status
    except Exception as ex:  # noqa: BLE001
        return False, f"send failed: {str(ex)[:120]}"
    if code not in (200, 204):
        return False, f"unexpected HTTP {code}"
    with open(SENT_LOG, "a") as f:
        f.write(json.dumps({"ts": time.time(), "token": key,
                            "symbol": a.get("symbol")}) + "\n")
    return True, f"sent (HTTP {code})"
