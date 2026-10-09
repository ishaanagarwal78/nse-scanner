"""Local live-feed screen, for testing only:  python -m scanner.dashboard  ->  http://localhost:8765

Connects to NSE's own push stream (one connection per stock) for every stock with a lock-in ending within two weeks,
plus anything you add on the page, and shows live prices, the 5 best buyers and sellers, shares waiting on each
side, and buyer- versus seller-initiated volume per minute. Sends nothing anywhere; nothing is saved.
"""
import argparse
import asyncio
import json
import os
import queue
import statistics
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import stream as S

HERE = os.path.dirname(os.path.abspath(__file__))
EVENT_NAMES = {"preipo_6m": "6-month lock-in", "anchor_30d": "anchor lock-in (half)", "anchor_90d": "anchor lock-in (rest)",
               "promoter_18m": "promoter lock-in"}
MAX_STOCKS = 60


class Live:
    def __init__(self, symbols, info):
        self.lock = threading.Lock()
        self.info = info                      # symbol -> lock-in note
        self.state = {}                       # symbol -> latest quote and counters
        self.ticks = {}                       # symbol -> deque of (epoch, price)
        self.delays = deque(maxlen=2000)
        self.flow = S.Flow()
        self.flow.depth_rows = None           # not recording order books here
        self.q = queue.Queue()
        self.stats = {"msgs": 0, "data": 0, "drops": 0, "connected": set(), "samples": []}
        self.stops = {}
        self.ex = S.Exchanges()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        threading.Thread(target=self.consume, daemon=True).start()
        for s in symbols:
            self.add(s)

    def cookies(self):
        return "; ".join(f"{k}={v}" for k, v in self.ex.nse.cookies.items())

    def add(self, sym):
        sym = sym.strip().upper().replace(".NS", "")
        if not sym or sym in self.stops or len(self.stops) >= MAX_STOCKS:
            return False
        stop = threading.Event()
        self.stops[sym] = stop
        with self.lock:
            self.state.setdefault(sym, {"symbol": sym, "updates": 0})
            self.ticks.setdefault(sym, deque(maxlen=4000))
        asyncio.run_coroutine_threadsafe(
            S.nse_stream(S.STREAM_URL.format(sym), sym, self.q, stop, self.stats, self.cookies, None), self.loop)
        return True

    def remove(self, sym):
        sym = sym.strip().upper()
        stop = self.stops.pop(sym, None)
        if stop:
            stop.set()
            with self.lock:
                self.state.pop(sym, None)
                self.ticks.pop(sym, None)

    def consume(self):
        while True:
            sym, q = self.q.get()
            if sym not in self.stops:
                continue
            now = datetime.now(S.IST)
            with self.lock:
                st = self.state.setdefault(sym, {"symbol": sym, "updates": 0})
                prev = st.get("price")
                st.update({"price": q["price"], "chg": q["chg"], "volume": q["volume"], "bid": q["bid"], "ask": q["ask"],
                           "depth": q.get("depth"), "recv": time.time(), "updates": st["updates"] + 1,
                           "dir": 0 if prev is None else (1 if q["price"] > prev else -1 if q["price"] < prev else st.get("dir", 0)),
                           "nse_time": q["time"].strftime("%H:%M:%S") if q.get("time") else None})
                if q.get("sent"):
                    self.delays.append((now - q["sent"]).total_seconds())
                self.ticks[sym].append((time.time(), q["price"]))
                self.flow.update(sym, q, now)

    def snapshot(self, sel):
        now = datetime.now(S.IST)
        rows = []
        with self.lock:
            for sym, st in self.state.items():
                buy, sell, tb, ts = self.flow.recent(sym, now, 15)
                rows.append({**{k: v for k, v in st.items() if k != "depth"}, "info": self.info.get(sym, ""),
                             "age": round(time.time() - st["recv"], 1) if st.get("recv") else None,
                             "buy15": buy, "sell15": sell, "waitBuy": tb, "waitSell": ts,
                             "live": sym in self.stats["connected"]})
            detail = None
            if sel and sel in self.state:
                st = self.state[sel]
                minutes = []
                for k in range(29, -1, -1):
                    mm = (now.replace(second=0, microsecond=0) - S.timedelta(minutes=k)).strftime("%H:%M")
                    m = self.flow.minutes.get((sel, mm))
                    minutes.append({"t": mm, "buy": m[0] if m else 0, "sell": m[1] if m else 0})
                ticks = list(self.ticks.get(sel, []))
                step = max(1, len(ticks) // 900)
                detail = {"symbol": sel, "info": self.info.get(sel, ""), "depth": st.get("depth"), "minutes": minutes,
                          "ticks": ticks[::step] + (ticks[-1:] if ticks and len(ticks) % step else [])}
        d = list(self.delays)
        return {"clock": now.strftime("%H:%M:%S"), "connected": len(self.stats["connected"] & set(self.stops)),
                "following": len(self.stops), "messages": self.stats["data"], "drops": self.stats["drops"],
                "delay": round(statistics.median(d), 1) if d else None, "rows": rows, "detail": detail}


LIVE = None


def live_doc_now():
    """The same snapshot format the website reads from /api/data/live, built from this screen's state."""
    with LIVE.lock:
        latest = {f"{s}.NS": {**st, "time": None, "name": None} for s, st in LIVE.state.items() if st.get("price") is not None}
        for s, st in LIVE.state.items():
            if st.get("nse_time") and f"{s}.NS" in latest:
                latest[f"{s}.NS"]["time"] = datetime.strptime(st["nse_time"], "%H:%M:%S").replace(year=2000, tzinfo=S.IST)
        # the screen's order flow is keyed by bare symbol; the snapshot expects tickers
        flow = S.Flow()
        flow.minutes = {(f"{k[0]}.NS", k[1]): v for k, v in LIVE.flow.minutes.items()}
    doc = S.live_doc(flow, latest, LIVE.info)
    if STREAM_URL:
        doc["stream_url"] = STREAM_URL
    return doc


STREAM_URL = ""


def publisher(url, every):
    os.environ.setdefault("SUBSCRIBERS_KEY", "local-preview")
    from . import publish
    publish.SITE, publish.KEY = url.rstrip("/"), os.environ["SUBSCRIBERS_KEY"]
    S.LIVE_PUBLISH_SECONDS = every
    while True:
        time.sleep(every)
        try:
            publish.put("live", live_doc_now())
        except Exception as e:
            print("publish failed:", e)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/add":
            return self._send(200, json.dumps({"ok": LIVE.add(qs.get("s", [""])[0])}))
        if u.path == "/remove":
            LIVE.remove(qs.get("s", [""])[0])
            return self._send(200, '{"ok": true}')
        if u.path == "/live-events":
            # the website's Live page connects here directly: a full snapshot every second
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(f"data: {json.dumps(live_doc_now(), default=str)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
        if u.path == "/events":
            sel = (qs.get("sel", [""])[0] or "").upper()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(f"data: {json.dumps(LIVE.snapshot(sel))}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
        self._send(404, '{"error": "not found"}')


def main():
    global LIVE
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--add", default="", help="extra symbols, comma separated")
    p.add_argument("--publish", default="", help="also post live snapshots to this site, e.g. http://localhost:8888")
    p.add_argument("--every", type=float, default=5, help="seconds between published snapshots")
    p.add_argument("--stream-url", default="", help="public address of /live-events, told to the website in each snapshot")
    a = p.parse_args()
    today = datetime.now(S.IST).date()
    try:
        near = S.near_lockins(today)
    except Exception as e:
        print("tracker calendar unavailable:", e)
        near = {}
    info = {}
    for t, e in near.items():
        d = datetime.fromisoformat(e["free_day"]).date()
        when = "today" if d == today else (f"{d:%d %b}" if d > today else f"ended {d:%d %b}")
        info[t.replace(".NS", "")] = f"{EVENT_NAMES.get(e['event'], 'lock-in')} · {when}"
    syms = sorted(info) + [x.strip().upper() for x in a.add.split(",") if x.strip()]
    global STREAM_URL
    STREAM_URL = a.stream_url or (f"http://localhost:{a.port}/live-events" if a.publish.startswith("http://localhost") else "")
    LIVE = Live(syms, info)
    if a.publish:
        threading.Thread(target=publisher, args=(a.publish, a.every), daemon=True).start()
        print(f"publishing live snapshots to {a.publish} every {a.every:g} s", flush=True)
    print(f"following {len(LIVE.stops)} stocks. Open http://localhost:{a.port}  (Ctrl+C to stop)", flush=True)
    p_host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")   # 0.0.0.0 on a server, behind an HTTPS proxy
    ThreadingHTTPServer((p_host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
