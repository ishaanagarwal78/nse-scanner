"""Market-hours scanner: NSE company announcements -> scored Telegram alerts.

Run:  python -m scanner.run --until 13:00          (loop until 1 pm IST)
      python -m scanner.run --backfill 2026-10-08   (score one past day, print alerts, send nothing)
Env:  TELEGRAM_BOT_TOKEN, SUBSCRIBERS_KEY, DASHBOARD_URL   (GitHub Actions secrets / variables)
      ALERTS_LIVE=1 to actually send; anything else prints only (dry run).
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from curl_cffi import requests as cffi

from .rules import amount_crore, classify, size_order

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
KEY = os.environ.get("SUBSCRIBERS_KEY", "")
SITE = os.environ.get("DASHBOARD_URL", "https://nse-lockin-tracker.netlify.app").rstrip("/")
LIVE = os.environ.get("ALERTS_LIVE") == "1"
MIN_MCAP_CR = 300        # market-wide alerts only for companies worth at least this much
POLL_SECONDS = 90
DIGEST_TIMES = ("12:30", "16:00")


class NSE:
    def __init__(self):
        self.s = cffi.Session(impersonate="chrome")
        self.s.get("https://www.nseindia.com/", timeout=30)
        self.mcap = {}

    def get(self, url, referer):
        for i in range(3):
            try:
                r = self.s.get(url, headers={"Referer": referer}, timeout=40)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
            time.sleep(5 * (i + 1))
            self.s.get("https://www.nseindia.com/", timeout=30)
        raise RuntimeError(f"NSE unreachable: {url}")

    def announcements(self, day):
        d = day.strftime("%d-%m-%Y")
        return self.get(f"https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={d}&to_date={d}",
                        "https://www.nseindia.com/companies-listing/corporate-filings-announcements")

    def market_cap(self, symbol):
        """₹ crore from the bulk size table (never a per-company NSE request: those trigger NSE's rate limits)."""
        if not self.mcap:
            self.mcap = load_market_caps()
        return self.mcap.get(symbol)


def load_market_caps():
    """{NSE symbol: market value in ₹ crore}.

    1) AMFI's half-yearly list of every listed company's 6-month average market value (about 5,400 companies);
    2) recent IPOs from the lock-in tracker's data (share count x latest price), which AMFI's list predates.
    """
    import io
    import openpyxl
    caps = {}
    now = datetime.now(IST)
    names = []
    for y in (now.year, now.year - 1):
        names += [f"AverageMarketCapitalization30Jun{y}.xlsx", f"AverageMarketCapitalization31Dec{y - 1}.xlsx"]
    for n in names:
        r = requests.get(f"https://www.amfiindia.com/Themes/Theme1/downloads/{n}", headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        if r.status_code == 200 and r.content[:2] == b"PK":
            ws = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True).active
            for row in ws.iter_rows(min_row=3, values_only=True):
                sym, cap, bcap = row[5], row[6], row[4]
                v = cap if isinstance(cap, (int, float)) else bcap if isinstance(bcap, (int, float)) else None
                if sym and sym != "-" and v:
                    caps[str(sym).strip()] = float(v)
            print(f"market values: {len(caps)} companies from AMFI {n}")
            break
    try:
        cal = requests.get(f"{SITE}/api/data/calendar", timeout=60).json()
        for e in cal:
            if e.get("post_shares") and e.get("last_close"):
                caps[e["ticker"].replace(".NS", "")] = e["post_shares"] * e["last_close"] / 1e7
    except Exception as ex:
        print("tracker data unavailable:", ex)
    return caps


def score(nse, a):
    desc, text = a.get("desc", ""), a.get("attchmntText", "") or ""
    cat, imp, tone = classify(desc, text)
    if imp == "noise":
        return None
    ev = {"id": a.get("seq_id") or a.get("an_dt") + a.get("symbol", ""), "symbol": a.get("symbol"), "name": a.get("sm_name"),
          "category": cat, "importance": imp, "tone": tone, "desc": desc, "text": text.strip(), "time": a.get("an_dt"),
          "pdf": a.get("attchmntFile"), "amount_cr": None, "mcap_cr": None}
    if imp == "sized" or imp in ("high", "medium"):
        ev["mcap_cr"] = nse.market_cap(ev["symbol"])
    if imp == "sized":
        ev["amount_cr"] = amount_crore(text)
        ev["importance"] = size_order(ev["amount_cr"], ev["mcap_cr"])
    return ev


def esc(s):
    return html.escape(str(s or ""), quote=False)


def crore(x):
    if x is None:
        return None
    return f"₹{x / 1e5:,.2f} lakh cr" if x >= 1e5 else f"₹{x:,.0f} cr"


def fmt(ev):
    icon = {"positive": "🟢", "negative": "🔴"}.get(ev["tone"], "🟡")
    name = re.sub(r"\s+(Limited|Ltd\.?)$", "", ev["name"] or ev["symbol"] or "", flags=re.I)
    lines = [f"{icon} <b>{esc(ev['category'])}</b> · {esc(name)} <code>{esc(ev['symbol'])}</code>"]
    if ev["category"] == "Order win" and ev["amount_cr"]:
        share = f", about <b>{100 * ev['amount_cr'] / ev['mcap_cr']:.1f}%</b> of its {crore(ev['mcap_cr'])} market value" if ev["mcap_cr"] else ""
        lines.append(f"Orders worth ≈ <b>{crore(ev['amount_cr'])}</b>{share}.")
    elif ev["mcap_cr"]:
        lines.append(f"Market value {crore(ev['mcap_cr'])}.")
    snippet = re.sub(r"\s+", " ", ev["text"])[:230]
    if snippet:
        lines.append(f"<i>“{esc(snippet)}{'…' if len(ev['text']) > 230 else ''}”</i>")
    t = (ev["time"] or "")[-8:-3]
    lines.append(f"🕒 {t} IST · NSE filing")
    kb = [[{"text": "📄 Read the filing", "url": ev["pdf"]}]] if ev.get("pdf", "").startswith("http") else []
    kb.append([{"text": "📊 Stock details", "callback_data": f"s:{ev['symbol']}"}])
    return {"text": "\n".join(lines), "keyboard": kb}


def digest(events, title):
    lines = [f"📰 <b>{esc(title)}</b>", "<i>Medium-impact company news since the last update, biggest companies first.</i>", ""]
    for ev in sorted(events, key=lambda e: -(e["mcap_cr"] or 0))[:10]:
        icon = {"positive": "🟢", "negative": "🔴"}.get(ev["tone"], "🟡")
        name = re.sub(r"\s+(Limited|Ltd\.?)$", "", ev["name"] or ev["symbol"], flags=re.I)
        lines.append(f"{icon} <b>{esc(ev['category'])}</b> · {esc(name)} ({crore(ev['mcap_cr']) or 'size n/a'})")
    return {"text": "\n".join(lines), "keyboard": [[{"text": f"📊 {e['symbol']}", "callback_data": f"s:{e['symbol']}"}]
                                                   for e in sorted(events, key=lambda e: -(e["mcap_cr"] or 0))[:4]]}


def chats():
    if not KEY:
        return []
    r = requests.get(f"{SITE}/api/subscribers", headers={"x-subscribers-key": KEY}, timeout=30)
    r.raise_for_status()
    return [c for c in r.json().get("chats", []) if (c.get("prefs") or {}).get("news", True)]


def send(chat_id, msg):
    if not LIVE:
        print(f"[dry run -> {chat_id}]\n{msg['text']}\n")
        return
    body = {"chat_id": chat_id, "text": msg["text"], "parse_mode": "HTML", "disable_web_page_preview": True}
    if msg.get("keyboard"):
        body["reply_markup"] = {"inline_keyboard": msg["keyboard"]}
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=body, timeout=30)
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 3))
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=body, timeout=30)
    time.sleep(0.3)


def route(ev, people):
    """Who gets this event right now: high + big enough -> everyone; anything relevant -> watchers."""
    tick = f"{ev['symbol']}.NS"
    big_enough = (ev["mcap_cr"] or 0) >= MIN_MCAP_CR
    out = []
    for c in people:
        watched = tick in (c.get("watch") or [])
        if (ev["importance"] == "high" and big_enough) or (watched and ev["importance"] in ("high", "medium", "low")):
            out.append(c["id"])
    return out


def _when(e):
    try:
        return datetime.strptime(e["time"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
    except Exception:
        return datetime.now(IST)


def backfill(day):
    nse = NSE()
    raw = nse.announcements(day)
    evs = [e for e in (score(nse, a) for a in raw) if e]
    by = {}
    for e in evs:
        by.setdefault(e["importance"], []).append(e)
    print(f"{day:%d %b %Y}: {len(raw)} announcements -> {len(evs)} kept "
          + ", ".join(f"{k} {len(v)}" for k, v in sorted(by.items())))
    for e in sorted(by.get("high", []), key=lambda e: -(e["mcap_cr"] or 0)):
        flag = "" if (e["mcap_cr"] or 0) >= MIN_MCAP_CR else "   (below ₹300 cr: watchlist only)"
        print(f"\n--- HIGH{flag}\n{fmt(e)['text']}")
    med = [e for e in by.get("medium", []) if (e["mcap_cr"] or 0) >= MIN_MCAP_CR]
    if med:
        print("\n--- DIGEST\n" + digest(med, "Company news digest")["text"])
    return evs


def session(until, opening=True, catchup_minutes=10):
    nse = NSE()
    people = chats()
    print(f"{len(people)} chats; live={LIVE}; until {until} IST")
    seen, pending = set(), []
    now = datetime.now(IST)
    # Overnight and pre-market filings since yesterday's close: one opening digest, no individual pings
    first = True
    sent_digests = set()
    while True:
        now = datetime.now(IST)
        if now.strftime("%H:%M") >= until:
            break
        try:
            raw = nse.announcements(now)
        except Exception as e:
            print("fetch failed:", e)
            time.sleep(POLL_SECONDS)
            continue
        fresh = [a for a in raw if (a.get("seq_id") or a.get("an_dt")) not in seen]
        for a in fresh:
            seen.add(a.get("seq_id") or a.get("an_dt"))
        events = [e for e in (score(nse, a) for a in fresh) if e]
        if first and opening:
            # morning session: overnight and pre-market filings go out as one digest, not individual pings
            early = [e for e in events if e["importance"] in ("high", "medium") and (e["mcap_cr"] or 0) >= MIN_MCAP_CR]
            if early:
                msg = digest(early, "Before the open: company news overnight")
                for c in people:
                    send(c["id"], msg)
            first = False
        elif first:
            # later session: only catch up on the last few minutes; older filings were handled by the earlier session
            cutoff = (now - timedelta(minutes=catchup_minutes)).strftime("%d-%b-%Y %H:%M:%S")
            recent = [e for e in events if _when(e) >= now - timedelta(minutes=catchup_minutes)]
            for e in recent:
                for cid in route(e, people):
                    send(cid, fmt(e))
            first = False
        else:
            for e in events:
                for cid in route(e, people):
                    send(cid, fmt(e))
                if e["importance"] == "medium" and (e["mcap_cr"] or 0) >= MIN_MCAP_CR:
                    pending.append(e)
        hhmm = now.strftime("%H:%M")
        for dt_ in DIGEST_TIMES:
            if hhmm >= dt_ and dt_ not in sent_digests and pending:
                msg = digest(pending, f"Company news digest, {dt_}")
                for c in people:
                    watched = set(c.get("watch") or [])
                    if any(f"{e['symbol']}.NS" not in watched for e in pending):
                        send(c["id"], msg)
                pending, _ = [], sent_digests.add(dt_)
        if int(time.time()) % 900 < POLL_SECONDS:  # refresh subscribers every ~15 minutes
            try:
                people = chats()
            except Exception:
                pass
        time.sleep(POLL_SECONDS)
    print("session finished")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--until", default="15:40")
    p.add_argument("--backfill")
    p.add_argument("--no-opening", action="store_true", help="later session: skip the overnight digest")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.backfill:
        backfill(datetime.strptime(args.backfill, "%Y-%m-%d"))
    else:
        session(args.until, opening=not args.no_opening)
