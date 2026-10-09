"""Market-hours scanner: NSE company announcements -> scored Telegram alerts.

Run:  python -m scanner.run --until 13:00          (loop until 1 pm IST)
      python -m scanner.run --backfill 2026-10-08   (score one past day, print alerts, send nothing)
      python -m scanner.run --evening               (evening reports: money flows, insiders, shareholding)
Env:  TELEGRAM_BOT_TOKEN, SUBSCRIBERS_KEY, DASHBOARD_URL   (GitHub Actions secrets / variables)
      ALERTS_LIVE=1 to actually send; anything else prints only (dry run).
      PRICE_ALERTS_LIVE=1 to also send live price alerts (off while the tracker site still sends its own).
      PREVIEW_CHAT=<chat id>: newer message types (brief, pre-open, insiders, flows, shareholding) go only there.
"""
import argparse
import html
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from curl_cffi import requests as cffi

from . import memo, ops
from .rules import amount_crore, classify, size_order

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
KEY = os.environ.get("SUBSCRIBERS_KEY", "")
SITE = os.environ.get("DASHBOARD_URL", "https://nse-lockin-tracker.netlify.app").rstrip("/")
LIVE = os.environ.get("ALERTS_LIVE") == "1"
PRICE_LIVE = os.environ.get("PRICE_ALERTS_LIVE") == "1"
PREVIEW = os.environ.get("PREVIEW_CHAT", "").strip()
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
    """Money in ₹ crore, written the way Indian readers expect: ₹45 lakh, ₹12.5 cr, ₹980 cr, ₹1.25 lakh cr."""
    if x is None:
        return None
    if x >= 1e5:
        return f"₹{x / 1e5:,.2f} lakh cr"
    if x >= 100:
        return f"₹{x:,.0f} cr"
    if x >= 1:
        return f"₹{x:,.1f} cr"
    return f"₹{x * 100:,.0f} lakh"


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


def chats(news_only=True):
    """Subscribed chats. news_only drops chats that turned company news off in the bot's settings."""
    if not KEY:
        return []
    r = requests.get(f"{SITE}/api/subscribers", headers={"x-subscribers-key": KEY}, timeout=30)
    r.raise_for_status()
    return [c for c in r.json().get("chats", []) if not news_only or (c.get("prefs") or {}).get("news", True)]


SEND_LOCK = threading.Lock()


def send(chat_id, msg, kind="news"):
    """kind: 'news' (company-news alerts), 'price' (live price alerts), 'new' (newer message types)."""
    with SEND_LOCK:
        if kind == "price" and not PRICE_LIVE:
            print(f"[price alert, not sent -> {chat_id}]\n{msg['text']}\n")
            return
        if kind == "new" and PREVIEW:
            if str(chat_id) != PREVIEW:
                return
            msg = {**msg, "text": "🧪 <i>Preview: only you get this for now.</i>\n" + msg["text"]}
        _send(chat_id, msg)


def _send(chat_id, msg):
    if not LIVE:
        print(f"[dry run -> {chat_id}]\n{msg['text']}\n")
        return
    body = {"chat_id": chat_id, "text": msg["text"], "parse_mode": "HTML", "disable_web_page_preview": True}
    if msg.get("keyboard"):
        body["reply_markup"] = {"inline_keyboard": msg["keyboard"]}
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=body, timeout=30)
    print(f"sent -> {chat_id}: {msg['text'].splitlines()[0][:80]}", flush=True)
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


def dedupe(events, seen_keys):
    """One alert per company and category per day."""
    out = []
    for e in events:
        k = (e["symbol"], e["category"])
        if k not in seen_keys:
            seen_keys.add(k)
            out.append(e)
    return out


def backfill(day, nse=None):
    nse = nse or NSE()
    raw = nse.announcements(day)
    evs = dedupe([e for e in (score(nse, a) for a in raw) if e], set())
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


def wait_until(hhmm):
    while datetime.now(IST).strftime("%H:%M") < hhmm:
        time.sleep(20)


def session(until, opening=True, catchup_minutes=3):
    from . import brief, insider, stream
    nse = NSE()
    people = chats()
    everyone = chats(news_only=False)
    print(f"{len(people)} chats; live={LIVE}; price alerts live={PRICE_LIVE}; preview={PREVIEW or 'off'}; until {until} IST")
    try:
        unlocks = stream.targets(datetime.now(IST).date())[0]
    except Exception:
        unlocks = {}
    if opening and datetime.now(IST).strftime("%H:%M") > "09:10":
        opening = False   # started late (restart or power cut): no "before the open" messages mid-morning
    if opening:
        wait_until("08:30")
    # Live prices run alongside in their own thread (NSE push stream, polling fallback, pre-open, Yahoo bars)
    feed = threading.Thread(target=stream.run, args=(until, send, opening), daemon=True)
    feed.start()
    seen, pending, sent_keys, ins_seen = set(), [], set(), set()
    first, first_ins, last_ins = True, True, 0.0
    ann_streak = ops.Streak("NSE company announcements", 3)
    # digest times already past at start (a restart or late start) are skipped, not sent late
    sent_digests = {t for t in DIGEST_TIMES if datetime.now(IST).strftime("%H:%M") >= t}
    # restarted today: carry on from what was saved (no repeats; catch up on what arrived while it was down)
    day = datetime.now(IST).date().isoformat()
    prev, catch_from = memo.load(day), None
    if prev:
        seen = set(prev.get("news_seen", []))
        sent_keys = {tuple(k) for k in prev.get("news_keys", [])}
        ins_seen = set(prev.get("insider_seen", []))
        sent_digests |= set(prev.get("digests", []))
        catch_from = memo.down_since(prev, datetime.now(IST))
        print(f"restart: carrying on from {prev.get('saved_at')} ({len(seen)} filings, {len(ins_seen)} insider files "
              f"already handled); catching up from {catch_from:%H:%M}", flush=True)
    brief_done = ops.marked_today("brief")   # a restart after 8:30 must not send the brief again
    while True:
        now = datetime.now(IST)
        if now.strftime("%H:%M") >= until:
            break
        try:
            raw = nse.announcements(now)
        except Exception as e:
            print("fetch failed:", e)
            ann_streak.fail(e)
            time.sleep(POLL_SECONDS)
            continue
        ann_streak.ok()
        fresh = [a for a in raw if (a.get("seq_id") or a.get("an_dt")) not in seen]
        for a in fresh:
            seen.add(a.get("seq_id") or a.get("an_dt"))
        events = dedupe([e for e in (score(nse, a) for a in fresh) if e], sent_keys)
        if first and opening and not brief_done:
            # morning session: overnight filings go into the 8:30 brief, not individual pings
            early = [e for e in events if e["importance"] in ("high", "medium") and (e["mcap_cr"] or 0) >= MIN_MCAP_CR]
            try:
                cal = requests.get(f"{SITE}/api/data/calendar", timeout=60).json() or []
                tracked = {e["ticker"].replace(".NS", ""): e["company"] for e in cal if e.get("ticker")}
                for c in everyone:
                    tracked.update({t.replace(".NS", ""): t.replace(".NS", "") for t in c.get("watch") or []})
                b = brief.build(nse, now, cal, nse.mcap or load_market_caps(), early, tracked, esc, crore, SITE)
                for c in everyone:
                    send(c["id"], b, "new")
                ops.mark("brief")
            except Exception as ex:
                print("brief failed:", ex)
                ops.alert(f"The 8:30 brief failed: {ex}", key="brief")
            if early:
                msg = digest(early, "Before the open: company news overnight")
                for c in people:
                    if not PREVIEW or str(c["id"]) != PREVIEW:   # the preview chat already has it in the brief
                        send(c["id"], msg)
            first = False
        elif first:
            # later session: only catch up on the last few minutes; older filings were handled by the earlier session
            start = catch_from or now - timedelta(minutes=catchup_minutes)
            recent = [e for e in events if _when(e) >= start]
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
        if time.time() - last_ins >= 600:   # insider trades, every 10 minutes
            last_ins = time.time()
            try:
                since = (catch_from or now - timedelta(minutes=catchup_minutes)) if first_ins and (prev or not opening) else None
                found = insider.fetch(nse, now, ins_seen, since)
                if found:
                    from . import publish
                    publish.insiders(found, [], now, nse.mcap)
                for t in found:
                    mcap = nse.market_cap(t["symbol"])
                    for c in people:
                        watched = f"{t['symbol']}.NS" in (c.get("watch") or [])
                        if insider.instant(t, mcap, watched, f"{t['symbol']}.NS" in unlocks):
                            send(c["id"], insider.fmt(t, mcap, esc, crore), "new")
            except Exception as ex:
                print("insider check failed:", ex)
                ops.alert(f"Insider-trade check failed: {ex}", key="insider")
            first_ins = False
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
                people, everyone = chats(), chats(news_only=False)
            except Exception:
                pass
        try:
            memo.save(day, news_seen=seen, news_keys=[list(k) for k in sent_keys], insider_seen=ins_seen,
                      digests=sent_digests)
        except Exception as ex:
            print("could not save state:", ex)
        time.sleep(POLL_SECONDS)
    feed.join(timeout=60)
    print("session finished")


def seed_site(days):
    """Fill the site's insider history and money flows from past NSE filings (one-off, or after a gap)."""
    from . import flows, insider, publish
    nse = NSE()
    caps = nse.mcap = load_market_caps()
    end = datetime.now(IST)
    d = end - timedelta(days=days)
    while d.date() <= end.date():
        if d.weekday() < 5:
            try:
                t, h = insider.fetch(nse, d), insider.big_holders(nse, d)
                publish.insiders(t, h, d, caps, keep_days=days + 1)
                print(f"{d:%d %b}: {len(t)} insider trades, {len(h)} big-holder changes")
            except Exception as ex:
                print(f"{d:%d %b}: failed ({ex})")
            time.sleep(2)
        d += timedelta(days=1)
    for back in range(0, 6):   # the most recent day with published end-of-day data
        day = end - timedelta(days=back)
        if day.weekday() < 5 and flows.participant_oi(nse, day):
            doc = publish.flows(flows.data(nse, day))
            print(f"money flows published for {day:%d %b}: FII net {(doc.get('fii') or {}).get('net')}")
            break


def evening():
    """Evening reports (about 7:15 pm): money flows, insider and big-holder trades, shareholding shifts."""
    from . import flows, holdings, insider
    nse = NSE()
    caps = nse.mcap = load_market_caps()
    day = datetime.now(IST)
    people, everyone = chats(), chats(news_only=False)
    print(f"evening reports for {day:%d %b}: {len(everyone)} chats; live={LIVE}; preview={PREVIEW or 'off'}")
    if day.weekday() >= 5:
        print("weekend: nothing to report")
        return
    from . import publish
    trades, holders = [], []
    try:
        trades, holders = insider.fetch(nse, day), insider.big_holders(nse, day)
        publish.insiders(trades, holders, day, caps)
    except Exception as ex:
        print("insider data failed:", ex)
        ops.alert(f"Evening insider data failed: {ex}", key="ev-insider")
    try:
        publish.flows(flows.data(nse, day))
    except Exception as ex:
        print("flows data failed:", ex)
        ops.alert(f"Evening money-flow data failed: {ex}", key="ev-flows")
    jobs = [("money flows", lambda: flows.report(nse, day, esc), everyone),
            ("insiders", lambda: insider.digest(trades, holders, caps, esc, crore,
                                                f"Insider and big-holder trades, {day:%d %b}"), people),
            ("shareholding", lambda: holdings.digest(holdings.shifts(nse, day, caps), esc, crore,
                                                     f"Shareholding shifts, {day:%d %b}"), people)]
    for name, build, to in jobs:
        try:
            msg = build()
        except Exception as ex:
            print(f"{name} failed:", ex)
            ops.alert(f"Evening report '{name}' failed: {ex}", key=f"ev-{name}")
            continue
        if not msg:
            print(f"{name}: nothing notable today")
            continue
        print(f"{name}:\n{msg['text']}\n")
        for c in to:
            send(c["id"], msg, "new")
    ops.mark("evening")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--until", default="15:40")
    p.add_argument("--backfill")
    p.add_argument("--no-opening", action="store_true", help="later session: skip the overnight digest")
    p.add_argument("--evening", action="store_true", help="evening reports")
    p.add_argument("--start", default="", help="wait until this time IST (HH:MM) before starting")
    p.add_argument("--seed-site", type=int, default=0, metavar="DAYS",
                   help="publish insider trades for the last DAYS days and the latest money flows, then exit")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.start and not args.backfill:
        print(f"waiting until {args.start} IST", flush=True)
        wait_until(args.start)
    if args.seed_site:
        seed_site(args.seed_site)
    elif args.evening:
        evening()
    elif args.backfill:
        a, _, b = args.backfill.partition(":")
        d0 = datetime.strptime(a, "%Y-%m-%d")
        d1 = datetime.strptime(b, "%Y-%m-%d") if b else d0
        nse = NSE()
        while d0 <= d1:
            if d0.weekday() < 5:
                backfill(d0, nse)
                print("=" * 60)
                time.sleep(3)
            d0 += timedelta(days=1)
    else:
        session(args.until, opening=not args.no_opening)
