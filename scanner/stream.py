"""Our own live price feed, built from free public sources, no broker and no paid API.

Two jobs, two kinds of source:

1. Alerts need fresh prices. The exchanges' own websites are not delayed, so the poller asks NSE's quote API
   and BSE's quote API in turn for the stocks that matter today (unlock week + watchlists). Each exchange
   gets about 10 requests a minute, so with 10 stocks each one is refreshed about every 30 seconds.
2. Research needs complete minute-by-minute history. Yahoo's public websocket pushes every price change for
   all recent IPOs; we decode it ourselves and build 1-minute bars. Yahoo labels NSE data as 15 minutes
   delayed, which does not matter for history, and the lag report measures the real delay of every source.

Started from run.session() in a background thread. Alerts stay dry-run unless ALERTS_LIVE=1.
"""
import asyncio
import base64
import csv
import json
import os
import re
import statistics
import struct
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from curl_cffi import requests as cffi

IST = timezone(timedelta(hours=5, minutes=30))
SITE = os.environ.get("DASHBOARD_URL", "https://nse-lockin-tracker.netlify.app").rstrip("/")
KEY = os.environ.get("SUBSCRIBERS_KEY", "")
YAHOO_WS = "wss://streamer.finance.yahoo.com/?version=2"
POLL_GAP = 3.0            # seconds between quote requests (alternating NSE / BSE)
OPEN = "09:15"
FAST_MOVE = 3.0           # % move within FAST_WINDOW minutes
FAST_WINDOW = 10
OUT = os.environ.get("STREAM_OUT", "stream_data")
BSE_H = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}


# ---------- Yahoo: a minimal protobuf reader (no extra packages) ----------
FIELDS = {1: ("id", "str"), 2: ("price", "f32"), 3: ("time", "sint"), 7: ("market_hours", "int"),
          8: ("change_percent", "f32"), 9: ("day_volume", "sint"), 16: ("previous_close", "f32"),
          22: ("last_size", "sint")}


def _varint(b, i):
    shift = val = 0
    while True:
        x = b[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not x & 0x80:
            return val, i
        shift += 7


def decode(b64):
    """Yahoo PricingData message (base64 protobuf) -> dict of the fields we use."""
    b, i, out = base64.b64decode(b64), 0, {}
    while i < len(b):
        tag, i = _varint(b, i)
        num, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = struct.unpack_from("<d", b, i)[0], i + 8
        elif wt == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        elif wt == 5:
            v, i = struct.unpack_from("<f", b, i)[0], i + 4
        else:
            break
        if num in FIELDS:
            name, kind = FIELDS[num]
            if kind == "str":
                v = v.decode("utf-8", "replace")
            elif kind == "sint":
                v = (v >> 1) ^ -(v & 1)
            out[name] = v
    return out


# ---------- shared state ----------
class Feed:
    def __init__(self):
        self.lock = threading.Lock()
        self.bars = {}          # (ticker, "HH:MM") -> [o, h, l, c, v]
        self.last_vol = {}      # ticker -> day volume seen last (turns cumulative volume into per-minute)
        self.yahoo_lag = []     # seconds between the trade time Yahoo reports and when we received it
        self.yahoo_msgs = 0
        self.quotes = []        # polled exchange quotes, kept for the day's file
        self.age = []           # seconds between NSE's last-update time and our request

    def yahoo_tick(self, d):
        t, p = d.get("id"), d.get("price")
        if not t or not p or not d.get("time"):
            return
        ts = d["time"] / 1000
        with self.lock:
            self.yahoo_msgs += 1
            self.yahoo_lag.append(time.time() - ts)
            m = datetime.fromtimestamp(ts, IST).strftime("%H:%M")
            vol = d.get("day_volume") or 0
            dv = max(0, vol - self.last_vol.get(t, vol))
            self.last_vol[t] = vol
            b = self.bars.get((t, m))
            if b:
                b[1], b[2], b[3], b[4] = max(b[1], p), min(b[2], p), p, b[4] + dv
            else:
                self.bars[(t, m)] = [p, p, p, p, dv]

    def save(self, day):
        os.makedirs(OUT, exist_ok=True)
        with self.lock:
            bars = sorted(self.bars.items())
            quotes = list(self.quotes)
        with open(f"{OUT}/bars_{day}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker", "minute_ist", "open", "high", "low", "close", "volume"])
            for (t, m), (o, h, l, c, v) in bars:
                w.writerow([t, m, round(o, 2), round(h, 2), round(l, 2), round(c, 2), v])
        with open(f"{OUT}/quotes_{day}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["received_ist", "source", "ticker", "price", "change_pct", "volume", "bid", "ask", "exchange_time"])
            w.writerows(quotes)

    def lag_report(self):
        with self.lock:
            y, n = list(self.yahoo_lag[-2000:]), list(self.age[-200:])
        med = lambda xs: f"{statistics.median(xs):.0f}s (n={len(xs)})" if xs else "no data"
        return f"lag check: Yahoo stream is {med(y)} behind the trade; NSE quote age {med(n)}"


async def yahoo_stream(feed, tickers, stop):
    """Keep a Yahoo websocket open until stop is set; reconnect with backoff; re-subscribe every 15 s."""
    import websockets
    wait = 2
    while not stop.is_set():
        try:
            async with websockets.connect(YAHOO_WS, max_size=None, open_timeout=20,
                                          additional_headers={"Origin": "https://finance.yahoo.com"}) as ws:
                sub = json.dumps({"subscribe": tickers})
                await ws.send(sub)
                wait, last_sub = 2, time.time()
                while not stop.is_set():
                    if time.time() - last_sub > 15:
                        await ws.send(sub)
                        last_sub = time.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                        if msg.get("message"):
                            feed.yahoo_tick(decode(msg["message"]))
                    except Exception:
                        pass
        except Exception as e:
            if stop.is_set():
                break
            print(f"yahoo stream dropped ({type(e).__name__}); retrying in {wait}s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)


# ---------- exchange quotes (fresh) ----------
class Exchanges:
    def __init__(self):
        self.nse = cffi.Session(impersonate="chrome")
        self.bse = cffi.Session(impersonate="chrome")
        self.bse_code = {}      # ISIN -> BSE scrip code
        self.isin = {}          # NSE symbol -> ISIN (learned from NSE quotes)
        self.series = {}        # NSE symbol -> trading series when not EQ
        self.names = {}         # NSE symbol -> company name
        self._nse_warm()

    def _nse_warm(self):
        try:
            self.nse.get("https://www.nseindia.com/", timeout=30)
        except Exception:
            pass

    def nse_quote(self, symbol):
        series = self.series.get(symbol, "EQ")
        ref = {"Referer": f"https://www.nseindia.com/get-quote/equity/{symbol}"}
        base = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName="
        r = self.nse.get(f"{base}getSymbolData&marketType=N&series={series}&symbol={symbol}", headers=ref, timeout=20)
        if r.status_code in (401, 403):
            self._nse_warm()
        if r.status_code == 404 and symbol not in self.series:
            # not in the main EQ series (e.g. BE trade-for-trade): ask NSE which series it trades in, once
            m = self.nse.get(f"{base}getMetaData&symbol={symbol}", headers=ref, timeout=20)
            act = (m.json().get("activeSeries") or ["EQ"]) if m.status_code == 200 else ["EQ"]
            self.series[symbol] = act[0]
            if act[0] != "EQ":
                return self.nse_quote(symbol)
        if r.status_code != 200:
            return None
        e = r.json()["equityResponse"][0]
        md, ti, ob = e.get("metaData") or {}, e.get("tradeInfo") or {}, e.get("orderBook") or {}
        p = ti.get("lastPrice") or ob.get("lastPrice")
        prev = md.get("previousClose")
        if not p or not prev:
            return None
        if md.get("isinCode"):
            self.isin[symbol] = md["isinCode"]
        if md.get("companyName"):
            self.names[symbol] = md["companyName"]
        try:
            ex_time = datetime.strptime(e.get("lastUpdateTime", ""), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except Exception:
            ex_time = None
        return {"source": "NSE", "price": float(p), "chg": 100 * (float(p) / float(prev) - 1),
                "volume": ti.get("totalTradedVolume"), "bid": ob.get("buyPrice1") or None,
                "ask": ob.get("sellPrice1") or None, "time": ex_time, "name": md.get("companyName")}

    def load_bse_codes(self):
        """ISIN -> BSE scrip code from one bulk list, so per-stock BSE requests need no lookups."""
        try:
            r = self.bse.get("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry="
                             "&segment=Equity&status=Active", headers=BSE_H, timeout=60)
            for x in r.json():
                if x.get("ISIN_NUMBER") and x.get("SCRIP_CD"):
                    self.bse_code[x["ISIN_NUMBER"]] = x["SCRIP_CD"]
            print(f"BSE codes: {len(self.bse_code)}")
        except Exception as e:
            print("BSE code list unavailable:", e)

    def bse_quote(self, symbol):
        code = self.bse_code.get(self.isin.get(symbol, ""))
        if not code:
            return None
        r = self.bse.get(f"https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?Debtflag=&scripcode={code}&seriesid=",
                         headers=BSE_H, timeout=20)
        if r.status_code != 200:
            return None
        h = r.json().get("Header") or {}
        try:
            p, prev = float(h["LTP"]), float(h["PrevClose"])
        except Exception:
            return None
        return {"source": "BSE", "price": p, "chg": 100 * (p / prev - 1), "volume": None,
                "bid": None, "ask": None, "time": None, "name": self.names.get(symbol)}

    def nifty(self):
        r = self.nse.get("https://www.nseindia.com/api/NextApi/apiClient?functionName=getIndexData&&index=NIFTY%2050",
                         headers={"Referer": "https://www.nseindia.com/market-data/live-equity-market"}, timeout=20)
        return r.json()["data"][0].get("percChange")


# ---------- who watches what ----------
def weekdays_between(a, b):
    n, x = 0, a
    while x < b:
        if x.weekday() < 5:
            n += 1
        x += timedelta(days=1)
    return n


def targets(today):
    """(unlocks in their first 5 sessions, {ticker: [watching chat ids]}, chats with prefs, every IPO ticker)."""
    cal = requests.get(f"{SITE}/api/data/calendar", timeout=60).json() or []
    lo = (today - timedelta(days=7)).isoformat()
    unlocks = {e["ticker"]: e for e in cal
               if e.get("event") == "preipo_6m" and lo <= e.get("free_day", "") <= today.isoformat()
               and weekdays_between(datetime.fromisoformat(e["free_day"]).date(), today) <= 4}
    people = []
    if KEY:
        r = requests.get(f"{SITE}/api/subscribers", headers={"x-subscribers-key": KEY}, timeout=30)
        people = r.json().get("chats", [])
    watchers = {}
    for c in people:
        for t in c.get("watch") or []:
            watchers.setdefault(t, []).append(c["id"])
    every = sorted({e["ticker"] for e in cal if e.get("ticker")})
    return unlocks, watchers, people, every


def wants(chat, risk, ticker):
    """Same rule as the bot: live alerts can be turned off, and 'big only' keeps high-risk unlocks."""
    p = {"big": False, "live": True, **(chat.get("prefs") or {})}
    watched = ticker in (chat.get("watch") or [])
    if not p["live"]:
        return watched
    if p["big"]:
        return risk == "high" or watched
    return True


def avg_volumes(tickers):
    """20-day average daily NSE volume per ticker from Yahoo daily candles (history may come from Yahoo)."""
    out, s = {}, cffi.Session(impersonate="chrome")
    for t in tickers:
        try:
            j = s.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1d&range=2mo", timeout=20).json()
            v = [x for x in j["chart"]["result"][0]["indicators"]["quote"][0]["volume"][:-1] if x]
            if v:
                out[t] = sum(v[-20:]) / len(v[-20:])
        except Exception:
            pass
        time.sleep(0.3)
    return out


def clean(s):
    return re.sub(r"\s+(Ltd\.?|Limited)$", "", str(s or ""), flags=re.I)


# ---------- the loop ----------
def run(until, send, opening=True, stop=None):
    stop = stop or threading.Event()
    today = datetime.now(IST).date()
    feed = Feed()
    unlocks, watchers, people, every = targets(today)
    focus = sorted(set(unlocks) | set(watchers))
    stream_list = sorted(set(every) | set(focus) | {"^NSEI"})
    print(f"live feed: {len(focus)} stocks on alert watch ({len(unlocks)} in unlock week), "
          f"{len(stream_list)} recorded from the stream")

    loop = asyncio.new_event_loop()
    th = threading.Thread(target=lambda: loop.run_until_complete(yahoo_stream(feed, stream_list, stop)), daemon=True)
    th.start()

    ex = Exchanges()
    ex.load_bse_codes()
    avg = avg_volumes(focus)
    done = {t: set() for t in focus}       # thresholds already alerted today
    seen = set()                            # later sessions: the first look at a stock only records what already happened
    hist = {t: [] for t in focus}           # (time, price) for the fast-move rule
    last_fast, market, traded = {}, None, set()
    i, last_report, last_save, last_mkt = 0, 0.0, time.time(), 0.0

    while not stop.is_set():
        now = datetime.now(IST)
        hhmm = now.strftime("%H:%M")
        if hhmm >= until or hhmm > "15:35":
            break
        if hhmm < OPEN or not focus:
            time.sleep(10)
            continue
        if time.time() - last_mkt > 60:
            try:
                market = ex.nifty()
            except Exception:
                pass
            last_mkt = time.time()
        t = focus[(i // 2) % len(focus)]
        use_bse = i % 2 == 1 and ex.bse_code and t.replace(".NS", "") in ex.isin
        i += 1
        sym = t.replace(".NS", "")
        try:
            q = ex.bse_quote(sym) if use_bse else ex.nse_quote(sym)
        except Exception:
            q = None
        if q:
            with feed.lock:
                feed.quotes.append([now.strftime("%H:%M:%S"), q["source"], t, q["price"], round(q["chg"], 2),
                                    q["volume"], q["bid"], q["ask"], q["time"].strftime("%H:%M:%S") if q["time"] else ""])
                if q["time"]:
                    feed.age.append((now - q["time"]).total_seconds())
            if q["time"] and q["time"].date() == today:
                traded.add(t)
            if t in traded:   # NSE confirmed a trade today: skips holidays and stocks not traded yet
                check(t, q, now, unlocks, watchers, people, avg, done, hist, last_fast, market, send,
                      silent=not opening and t not in seen)
                seen.add(t)
        if time.time() - last_report > 900:
            print(now.strftime("%H:%M"), feed.lag_report(), flush=True)
            last_report = time.time()
        if time.time() - last_save > 600:
            feed.save(today.isoformat())
            last_save = time.time()
        time.sleep(POLL_GAP)

    stop.set()
    th.join(timeout=15)
    feed.save(today.isoformat())
    print(feed.lag_report())
    print(f"live feed finished: {feed.yahoo_msgs} stream updates, {len(feed.bars)} minute bars, "
          f"{len(feed.quotes)} exchange quotes")
    return feed


def check(t, q, now, unlocks, watchers, people, avg, done, hist, last_fast, market, send, silent):
    e = unlocks.get(t)
    chg = q["chg"]
    fired = []
    fall = "fall10" if chg <= -10 else "fall5" if chg <= -5 else None
    rise = None if e else ("rise10" if chg >= 10 else "rise5" if chg >= 5 else None)
    for k in (fall, rise):
        if k and k not in done[t] and not (k.endswith("5") and k[:-1] + "10" in done[t]):
            fired.append(k)
    pace = None
    mins = now.hour * 60 + now.minute
    if q["volume"] and avg.get(t):
        pace = q["volume"] / (avg[t] * min(1, max(0.05, (mins - 555) / 375)))
        if pace >= 3 and q["volume"] > 10000 and "vol3" not in done[t] and mins >= 585:
            fired.append("vol3")
    h = hist[t]
    h.append((now, q["price"]))
    while h and (now - h[0][0]).total_seconds() > FAST_WINDOW * 60:
        h.pop(0)
    lo, hi = min(p for _, p in h), max(p for _, p in h)
    fast = None
    if q["price"] <= hi * (1 - FAST_MOVE / 100):
        fast = -100 * (1 - q["price"] / hi)
    elif q["price"] >= lo * (1 + FAST_MOVE / 100):
        fast = 100 * (q["price"] / lo - 1)
    if fast is not None and (now - last_fast.get(t, now - timedelta(hours=1))).total_seconds() >= 1800:
        fired.append("fast")
        last_fast[t] = now
    if not fired:
        return
    done[t].update(f for f in fired if f != "fast")
    if silent:
        return

    k = weekdays_between(datetime.fromisoformat(e["free_day"]).date(), now.date()) if e else None
    where = ("unlock day" if k == 0 else f"day {k} after unlock") if e else "on your watchlist"
    name = clean((e or {}).get("company") or q.get("name") or t.replace(".NS", ""))
    mk = f" (market {market:+.1f}%)" if market is not None else ""
    lines = [f"⚡ <b>{name}</b> · {where}",
             f"{'🔽' if chg < 0 else '🔼'} <b>{abs(chg):.1f}%</b> today at ₹{q['price']:,.2f}{mk}"]
    if "fast" in fired:
        lines.append(f"⏱️ Moved <b>{fast:+.1f}%</b> in the last {FAST_WINDOW} minutes.")
    if pace and pace >= 2:
        lines.append(f"📊 Trading at <b>{pace:.1f}×</b> its usual pace{'. Big sellers may be active.' if e else '.'}")
    if q.get("bid") and q.get("ask"):
        lines.append(f"Best buyer ₹{q['bid']:,.2f}, best seller ₹{q['ask']:,.2f}.")
    top = ((e or {}).get("holders") or {}).get("top") or []
    if top:
        lines.append("👥 Can sell now: " + ", ".join(" ".join(x["name"].split()[:3]) for x in top[:3]))
    lines.append(f"<i>{q['source']} price at {now:%H:%M:%S} IST.</i>")
    msg = {"text": "\n".join(lines), "keyboard": [[{"text": "📊 Details", "callback_data": f"s:{t.replace('.NS', '')}"}]]}
    risk = (e or {}).get("risk", "info")
    for c in people:
        if (e and wants(c, risk, t)) or (not e and c["id"] in watchers.get(t, [])):
            send(c["id"], msg)
