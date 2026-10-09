"""Publish the scanner's data to the tracker site (/api/data/<name>), so its website and bot can show it.

    insiders  insider and big-holder trades, last 30 days (merged with what is already published)
    flows     the evening money-flow report, plus a 30-day history of FII/DII flows and futures positions
    live      a snapshot of the live feed: price, order book and order flow for every followed stock

Uses SUBSCRIBERS_KEY as the write key. PUBLISH_URL overrides the site (the local preview uses http://localhost:8888).
"""
import json
import os
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
SITE = (os.environ.get("PUBLISH_URL") or os.environ.get("DASHBOARD_URL") or "https://nse-lockin-tracker.netlify.app").rstrip("/")
KEY = os.environ.get("SUBSCRIBERS_KEY", "")
_warned = set()


def get(name):
    try:
        r = requests.get(f"{SITE}/api/data/{name}", timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def put(name, doc):
    if not KEY:
        return False
    try:
        r = requests.post(f"{SITE}/api/data/{name}", headers={"x-tracker-key": KEY},
                          data=json.dumps(doc, default=str), timeout=30)
        if r.status_code != 200 and name not in _warned:
            _warned.add(name)
            print(f"publish {name}: HTTP {r.status_code} {r.text[:80]} (the site may need the new data name deployed)")
            if r.status_code != 404:   # 404 only means the site has not been deployed with this data name yet
                from . import ops
                ops.alert(f"Uploading '{name}' to the website failed: HTTP {r.status_code}", key=f"pub-{name}", every=6 * 3600)
        return r.status_code == 200
    except Exception as e:
        if name not in _warned:
            _warned.add(name)
            print(f"publish {name} failed: {e}")
            from . import ops
            ops.alert(f"Uploading '{name}' to the website failed: {e}", key=f"pub-{name}", every=6 * 3600)
        return False


def _trade_row(t):
    return {"filed": t["filed"].strftime("%Y-%m-%d %H:%M") if t.get("filed") else None, "symbol": t["symbol"],
            "company": t.get("company"), "person": t.get("person"), "role": t.get("role"), "side": t["side"],
            "mode": t.get("mode"), "value_cr": round(t.get("value_cr") or 0, 3), "shares": t.get("shares"),
            "pct_before": t.get("pct_before"), "pct_after": t.get("pct_after"), "from": t.get("from"), "to": t.get("to")}


def insiders(trades, holders, day, caps=None, keep_days=30):
    """Merge today's insider trades and big-holder disclosures into the published 30-day history."""
    old = get("insiders") or {}
    d = day.strftime("%Y-%m-%d")
    cut = (day - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    rows = {(r["filed"], r["symbol"], r["person"], r["side"]): r for r in old.get("trades", []) if (r.get("filed") or "") >= cut}
    for t in trades:
        r = _trade_row(t)
        r["mcap_cr"] = (caps or {}).get(r["symbol"])
        rows[(r["filed"], r["symbol"], r["person"], r["side"])] = r
    hold = {(h["date"], h["symbol"], h["who"]): h for h in old.get("holders", []) if h.get("date", "") >= cut}
    for h in holders:
        x = {**h, "date": d, "mcap_cr": (caps or {}).get(h["symbol"])}
        hold[(d, x["symbol"], x["who"])] = x
    # the same trade is sometimes filed twice (original and revision): keep the latest filing of each
    uniq = {}
    for r in sorted(rows.values(), key=lambda r: r.get("filed") or ""):
        uniq[(r["symbol"], r.get("person"), r["side"], round(r.get("value_cr") or 0, 2), r.get("from"))] = r
    doc = {"generated_at": datetime.now(IST).isoformat(timespec="seconds"),
           "trades": sorted(uniq.values(), key=lambda r: r.get("filed") or "", reverse=True),
           "holders": sorted(hold.values(), key=lambda h: h["date"], reverse=True)}
    put("insiders", doc)
    return doc


def flows(today_doc, keep_days=45):
    old = get("flows") or {}
    hist = {h["date"]: h for h in old.get("history", [])}
    fii, dii = today_doc.get("fii") or {}, today_doc.get("dii") or {}
    fut = {f["group"]: f["long_pct"] for f in today_doc.get("futures", [])}
    if fii.get("date"):
        h = hist.get(fii["date"], {"date": fii["date"]})
        h.update({"fii_net": fii.get("net"), "dii_net": dii.get("net")})
        hist[fii["date"]] = h
    if today_doc.get("oi_date") and fut:
        h = hist.get(today_doc["oi_date"], {"date": today_doc["oi_date"]})
        h.update({"fii_long_pct": fut.get("FII"), "client_long_pct": fut.get("Client"), "pro_long_pct": fut.get("Pro")})
        if today_doc.get("nifty"):
            h["nifty"] = today_doc["nifty"].get("last")
        hist[today_doc["oi_date"]] = h
    doc = {**today_doc, "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
           "history": sorted(hist.values(), key=lambda h: h["date"])[-keep_days:]}
    put("flows", doc)
    return doc
