"""Our own live price feed, built from free public sources, no broker and no paid API.

1. NSE's own push stream (the one its quote pages use): one connection per followed stock (unlock week and
   watchlists). Each message has the last price, volume and the order book. This drives the price alerts.
2. Polling NSE's and BSE's quote APIs, for any followed stock the stream has not updated in 45 seconds.
3. Order flow: successive NSE quotes are turned into buyer-initiated versus seller-initiated volume per minute,
   plus the shares waiting to buy and sell. Saved every day as our own order-flow history.
4. Pre-open (9:00-9:08): NSE's pre-open book for every stock, saved, with alerts on strong imbalances.
5. Yahoo's public websocket for 1-minute bars of every recent IPO (Yahoo marks NSE as delayed; fine for history).

Started from run.session() in a background thread. Price alerts are sent only when PRICE_ALERTS_LIVE=1.
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
                "ask": ob.get("sellPrice1") or None, "time": ex_time, "name": md.get("companyName"),
                "depth": parse_book(ob)}

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


# ---------- order books and NSE's own push stream ----------
def parse_time(x):
    if x in (None, "", 0):
        return None
    if isinstance(x, (int, float)):
        return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, IST)
    for f in ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(x)[:20].strip(), f).replace(tzinfo=IST)
        except Exception:
            pass
    return None


def parse_book(ob):
    """Order book in any of NSE's shapes -> {bids: [(price, qty)], asks: [...], tot_buy, tot_sell} or None."""
    if not ob:
        return None
    bids, asks, tb, ts = [], [], None, None
    if isinstance(ob, dict):
        for i in range(1, 6):
            bp, bq = ob.get(f"buyPrice{i}"), ob.get(f"buyQuantity{i}")
            sp, sq = ob.get(f"sellPrice{i}"), ob.get(f"sellQuantity{i}")
            if bp:
                bids.append((float(bp), float(bq or 0)))
            if sp:
                asks.append((float(sp), float(sq or 0)))
        for k in ("bids", "buy", "bid"):
            for x in ob.get(k) or []:
                if isinstance(x, dict) and x.get("price"):
                    bids.append((float(x["price"]), float(x.get("quantity") or x.get("qty") or 0)))
        for k in ("asks", "sell", "ask", "offers"):
            for x in ob.get(k) or []:
                if isinstance(x, dict) and x.get("price"):
                    asks.append((float(x["price"]), float(x.get("quantity") or x.get("qty") or 0)))
        tb, ts = ob.get("totalBuyQuantity") or ob.get("totBuyQty"), ob.get("totalSellQuantity") or ob.get("totSellQty")
    elif isinstance(ob, list):
        for x in ob:
            if isinstance(x, dict) and x.get("price"):
                if x.get("buyQuantity"):
                    bids.append((float(x["price"]), float(x["buyQuantity"])))
                if x.get("sellQuantity"):
                    asks.append((float(x["price"]), float(x["sellQuantity"])))
        bids.sort(reverse=True)
        asks.sort()
    if not (bids or asks or tb or ts):
        return None
    return {"bids": bids[:5], "asks": asks[:5], "tot_buy": float(tb) if tb else None, "tot_sell": float(ts) if ts else None}


def from_stream(m):
    """One NSE stream message -> quote dict (None for heartbeats and closed-market messages)."""
    if not isinstance(m, dict) or m.get("symbol") in (None, "HEARTBEAT") or not m.get("ltp"):
        return None
    if (m.get("mktStatus") or "").upper() == "CLOSE":
        return None
    p = float(m["ltp"])
    chg = m.get("pchange")
    if chg in (None, 0) and m.get("change") not in (None, 0):
        prev = p - float(m["change"])
        chg = 100 * float(m["change"]) / prev if prev else None
    depth = parse_book(m.get("orderBook"))
    return {"source": "NSE stream", "price": p, "chg": float(chg) if chg is not None else None,
            "volume": m.get("volume"), "bid": depth["bids"][0][0] if depth and depth["bids"] else None,
            "ask": depth["asks"][0][0] if depth and depth["asks"] else None,
            "time": parse_time(m.get("timestamp")), "sent": parse_time(m.get("dessiminationTime")),
            "depth": depth, "name": None}


STREAM_URL = "wss://streamer.nseindia.com/streams/equity/high/equityStockBySymbol?symbol={}"
CAS_URL = "wss://streamer.nseindia.com/streams/cm/cas"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


async def nse_streams(symbols, out_q, stop, stats, cookies, samples):
    """One NSE push connection per followed stock, plus the closing-auction stream (recorded to learn its format)."""
    import websockets

    async def one(url, key):
        wait = 2
        while not stop.is_set():
            try:
                async with websockets.connect(url, open_timeout=20, max_size=None, ping_interval=20, user_agent_header=UA,
                                              additional_headers={"Origin": "https://www.nseindia.com", "Cookie": cookies()}) as ws:
                    wait = 2
                    stats["connected"].add(key)
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 5)
                        except asyncio.TimeoutError:
                            continue
                        stats["msgs"] += 1
                        try:
                            m = json.loads(raw)
                        except Exception:
                            continue
                        beat = isinstance(m, dict) and "HEARTBEAT" in (m.get("symbol"), m.get("indexName"))
                        if not beat:
                            stats["data"] += 1
                            if samples.get(key, 0) < 40:
                                samples[key] = samples.get(key, 0) + 1
                                stats["samples"].append({"stream": key, "at": datetime.now(IST).strftime("%H:%M:%S"), "msg": m})
                                if samples[key] <= 2:
                                    print(f"NSE stream sample [{key}]:", json.dumps(m)[:700], flush=True)
                        if key != "cas":
                            q = from_stream(m)
                            if q:
                                out_q.put((key, q))
            except Exception as e:
                stats["connected"].discard(key)
                if stop.is_set():
                    break
                stats["drops"] += 1
                if stats["drops"] <= 5:
                    print(f"NSE stream {key} dropped ({type(e).__name__}: {str(e)[:80]}); retrying in {wait}s", flush=True)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 120)

    await asyncio.gather(*(one(STREAM_URL.format(s), s) for s in symbols), one(CAS_URL, "cas"))


class Flow:
    """Turns successive NSE quotes into order flow: who was in a hurry, buyers or sellers, minute by minute."""

    def __init__(self):
        self.prev, self.minutes, self.depth_rows = {}, {}, []

    def update(self, t, q, now):
        vol, price, d = q.get("volume"), q["price"], q.get("depth")
        m = self.minutes.setdefault((t, now.strftime("%H:%M")), [0.0, 0.0, 0.0, 0, None, None, price])
        m[3] += 1
        m[6] = price
        bid, ask = q.get("bid"), q.get("ask")
        if d:
            m[4], m[5] = d.get("tot_buy"), d.get("tot_sell")
            if len(self.depth_rows) < 600000:
                b = (d["bids"] + [(None, None)] * 5)[:5]
                a = (d["asks"] + [(None, None)] * 5)[:5]
                self.depth_rows.append([now.strftime("%H:%M:%S"), q["source"], t, price, vol]
                                       + [x for pq in b for x in pq] + [x for pq in a for x in pq]
                                       + [d.get("tot_buy"), d.get("tot_sell")])
        p = self.prev.get(t)
        sidev = 0
        if p and vol and p["vol"] and vol > p["vol"]:
            dv = vol - p["vol"]
            if p["ask"] and price >= p["ask"]:
                sidev = 1
            elif p["bid"] and price <= p["bid"]:
                sidev = -1
            elif price != p["price"]:
                sidev = 1 if price > p["price"] else -1
            else:
                sidev = p["side"]
            if sidev > 0:
                m[0] += dv
            elif sidev < 0:
                m[1] += dv
            else:
                m[2] += dv
        if vol:
            self.prev[t] = {"vol": vol, "price": price, "bid": bid or (p or {}).get("bid"),
                            "ask": ask or (p or {}).get("ask"), "side": sidev or (p or {}).get("side", 0)}

    def recent(self, t, now, minutes=15):
        buy = sell = 0.0
        tb = ts = None
        for k in range(minutes):
            m = self.minutes.get((t, (now - timedelta(minutes=k)).strftime("%H:%M")))
            if m:
                buy, sell = buy + m[0], sell + m[1]
                if tb is None and m[4]:
                    tb, ts = m[4], m[5]
        return buy, sell, tb, ts

    def save(self, day):
        os.makedirs(OUT, exist_ok=True)
        with open(f"{OUT}/flow_{day}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker", "minute_ist", "buyer_initiated_qty", "seller_initiated_qty", "unclassified_qty", "updates",
                        "waiting_to_buy", "waiting_to_sell", "last_price"])
            for (t, mm), v in sorted(self.minutes.items()):
                w.writerow([t, mm] + v)
        with open(f"{OUT}/depth_{day}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["received_ist", "source", "ticker", "price", "volume"]
                       + [f"bid{i}_{k}" for i in range(1, 6) for k in ("price", "qty")]
                       + [f"ask{i}_{k}" for i in range(1, 6) for k in ("price", "qty")] + ["waiting_to_buy", "waiting_to_sell"])
            w.writerows(self.depth_rows)


# ---------- the loop ----------
def run(until, send, opening=True, stop=None):
    import queue
    from . import preopen

    stop = stop or threading.Event()
    today = datetime.now(IST).date()
    day = today.isoformat()
    feed, flow = Feed(), Flow()
    unlocks, watchers, people, every = targets(today)
    focus = sorted(set(unlocks) | set(watchers))
    stream_list = sorted(set(every) | set(focus) | {"^NSEI"})
    print(f"live feed: {len(focus)} stocks on alert watch ({len(unlocks)} in unlock week), "
          f"{len(stream_list)} recorded from the Yahoo stream", flush=True)

    ex = Exchanges()
    ex.load_bse_codes()
    cookies = lambda: "; ".join(f"{k}={v}" for k, v in ex.nse.cookies.items())
    q_in = queue.Queue()
    stats = {"msgs": 0, "data": 0, "drops": 0, "connected": set(), "samples": []}
    samples = {}

    async def streams():
        await asyncio.gather(yahoo_stream(feed, stream_list, stop),
                             nse_streams([t.replace(".NS", "") for t in focus], q_in, stop, stats, cookies, samples))

    loop = asyncio.new_event_loop()
    th = threading.Thread(target=lambda: loop.run_until_complete(streams()), daemon=True)
    th.start()

    avg = avg_volumes(focus)
    done = {t: set() for t in focus}       # thresholds already alerted today
    seen = set()                            # later sessions: the first look at a stock only records what already happened
    hist = {t: [] for t in focus}           # (time, price) for the fast-move rule
    last_fast, market, traded, last_stream, stream_age = {}, None, set(), {}, []
    i, last_poll, last_report, last_save, last_mkt = 0, 0.0, 0.0, time.time(), 0.0
    pre_snaps, pre_done = [], set()
    follow_syms = {t.replace(".NS", "") for t in set(focus) | set(every)}

    def handle(t, q, now):
        if q.get("chg") is None:
            return
        with feed.lock:
            feed.quotes.append([now.strftime("%H:%M:%S"), q["source"], t, q["price"], round(q["chg"], 2),
                                q["volume"], q["bid"], q["ask"], q["time"].strftime("%H:%M:%S") if q["time"] else ""])
            if q["source"] == "NSE" and q["time"]:
                feed.age.append((now - q["time"]).total_seconds())
        if q["source"].startswith("NSE"):
            flow.update(t, q, now)
        if (q["time"] and q["time"].date() == today) or (q["source"] == "NSE stream" and q.get("volume")):
            traded.add(t)
        if t in traded:   # NSE confirmed a trade today: skips holidays and stocks not traded yet
            check(t, q, now, unlocks, watchers, people, avg, done, hist, last_fast, market, send,
                  silent=not opening and t not in seen, flow=flow)
            seen.add(t)

    def preopen_snap(label):
        try:
            snap = preopen.rows(preopen.fetch(ex.nse))
        except Exception as e:
            print("pre-open fetch failed:", e)
            snap = None
        if snap:
            pre_snaps.append((label, snap))

    def preopen_step(now):
        hms = now.strftime("%H:%M:%S")
        for st in preopen.SNAP_TIMES:
            if hms >= st and st not in pre_done and hms < "09:12:00":
                pre_done.add(st)
                preopen_snap(st)
        if hms < "09:08:30":
            return
        pre_done.add("final")
        if not pre_snaps or pre_snaps[-1][0] != preopen.SNAP_TIMES[-1]:
            preopen_snap("final")   # started late: NSE keeps the day's final pre-open book after 9:08
        preopen.save(OUT, day, pre_snaps, follow_syms)
        final = pre_snaps[-1][1] if pre_snaps else {}
        print(f"pre-open: {len(final)} stocks recorded, {len(pre_snaps)} snapshots", flush=True)
        if not opening or hms >= "09:20:00":
            return
        for t in focus:
            sym = t.replace(".NS", "")
            r = final.get(sym)
            if not r:
                continue
            e = unlocks.get(t)
            msg = preopen.alert(sym, r, e, (e or {}).get("company") or ex.names.get(sym) or sym, clean)
            if msg:
                for c in people:
                    if (e and wants(c, e.get("risk", "info"), t)) or (not e and c["id"] in watchers.get(t, [])):
                        send(c["id"], msg, "new")

    while not stop.is_set():
        now = datetime.now(IST)
        hhmm = now.strftime("%H:%M")
        if hhmm >= until or hhmm > "15:35":
            break
        if now.weekday() < 5 and hhmm >= "09:00" and "final" not in pre_done:
            preopen_step(now)
        if hhmm < OPEN or not focus:
            time.sleep(2 if hhmm >= "08:59" else 10)
            continue
        if time.time() - last_mkt > 60:
            try:
                market = ex.nifty()
            except Exception:
                pass
            last_mkt = time.time()
        # 1) everything NSE pushed since the last pass
        while True:
            try:
                sym, q = q_in.get_nowait()
            except queue.Empty:
                break
            t = sym + ".NS"
            last_stream[t] = time.time()
            if q.get("sent"):
                stream_age.append((now - q["sent"]).total_seconds())
            handle(t, q, now)
        # 2) polling for stocks the stream has not updated in the last 45 seconds
        if time.time() - last_poll >= POLL_GAP:
            stale = [t for t in focus if time.time() - last_stream.get(t, 0) > 45]
            if stale:
                last_poll = time.time()
                n, k = len(stale), i // 2
                if i % 2 == 1 and ex.bse_code:
                    t = stale[(k + n // 2) % n]
                    use_bse = t.replace(".NS", "") in ex.isin
                else:
                    t, use_bse = stale[k % n], False
                i += 1
                sym = t.replace(".NS", "")
                try:
                    q = ex.bse_quote(sym) if use_bse else ex.nse_quote(sym)
                except Exception:
                    q = None
                if q:
                    handle(t, q, now)
        if time.time() - last_report > 900:
            med = f"{statistics.median(stream_age[-500:]):.1f}s" if stream_age else "no data"
            print(now.strftime("%H:%M"), feed.lag_report(),
                  f"| NSE push stream: {len(stats['connected'])} connected, {stats['data']} price messages, "
                  f"send-to-receive {med}, {stats['drops']} drops", flush=True)
            last_report = time.time()
        if time.time() - last_save > 600:
            feed.save(day)
            flow.save(day)
            last_save = time.time()
        time.sleep(0.5)

    stop.set()
    th.join(timeout=15)
    feed.save(day)
    flow.save(day)
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/stream_samples_{day}.jsonl", "w") as f:
        for s in stats["samples"]:
            f.write(json.dumps(s) + "\n")
    print(feed.lag_report())
    print(f"live feed finished: {feed.yahoo_msgs} Yahoo updates, {stats['data']} NSE push messages, "
          f"{len(feed.bars)} minute bars, {len(feed.quotes)} quotes, {len(flow.depth_rows)} order-book rows")
    return feed


def check(t, q, now, unlocks, watchers, people, avg, done, hist, last_fast, market, send, silent, flow=None):
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
    if flow:
        buy, sell, tb, ts = flow.recent(t, now)
        if buy + sell > 0:
            share = 100 * sell / (buy + sell)
            if share >= 50:
                lines.append(f"🧾 Last 15 minutes: <b>{share:.0f}%</b> of shares traded were sellers accepting buyers' prices.")
            else:
                lines.append(f"🧾 Last 15 minutes: <b>{100 - share:.0f}%</b> of shares traded were buyers paying sellers' prices.")
        if tb and ts and (ts / tb >= 1.5 or tb / ts >= 1.5):
            lines.append(f"📚 Waiting to sell: {ts / tb:.1f}× the shares waiting to buy." if ts > tb else
                         f"📚 Waiting to buy: {tb / ts:.1f}× the shares waiting to sell.")
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
            send(c["id"], msg, "price")
