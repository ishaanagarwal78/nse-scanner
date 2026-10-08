"""Shareholding shifts: companies whose quarterly shareholding report, filed today, shows the promoter stake moving.

Every listed company files its shareholding pattern within 21 days of each quarter's end. NSE's summary gives the
promoter-group share; we compare it with the company's previous quarter. Promoters raising their stake is usually
read as confidence; a falling stake (selling, or dilution from new shares) is worth a look.
"""
import time
from datetime import datetime

MIN_MCAP_CR = 500
MIN_MOVE = 0.5   # percentage points


def _pct(x):
    try:
        return float(x)
    except Exception:
        return None


def _date(x):
    try:
        return datetime.strptime(x, "%d-%b-%Y")
    except Exception:
        return datetime.min


def shifts(nse, day, caps):
    d = day.strftime("%d-%m-%Y")
    ref = "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"
    rows = nse.get(f"https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&from_date={d}&to_date={d}", ref)
    latest = {}
    for r in rows or []:
        sym = r.get("symbol")
        if sym and (caps.get(sym) or 0) >= MIN_MCAP_CR and _pct(r.get("pr_and_prgrp")) is not None:
            if sym not in latest or _date(r["date"]) > _date(latest[sym]["date"]):
                latest[sym] = r
    out = []
    for sym, r in latest.items():
        try:
            hist = nse.get(f"https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={sym}", ref)
        except Exception:
            continue
        older = sorted((h for h in hist or [] if _date(h["date"]) < _date(r["date"]) and _pct(h.get("pr_and_prgrp")) is not None),
                       key=lambda h: _date(h["date"]))
        time.sleep(1.2)
        if not older:
            continue
        now, was = _pct(r["pr_and_prgrp"]), _pct(older[-1]["pr_and_prgrp"])
        if abs(now - was) >= MIN_MOVE:
            out.append({"symbol": sym, "name": r.get("name"), "now": now, "was": was, "quarter": r["date"],
                        "prev_quarter": older[-1]["date"], "mcap": caps.get(sym)})
    return out


def digest(rows, esc, crore, title):
    if not rows:
        return None
    up = sorted([r for r in rows if r["now"] > r["was"]], key=lambda r: r["was"] - r["now"])[:8]
    down = sorted([r for r in rows if r["now"] < r["was"]], key=lambda r: r["now"] - r["was"])[:8]
    clean = lambda s: " ".join(str(s).replace("Limited", "").replace("Ltd.", "").split())
    lines = [f"📋 <b>{esc(title)}</b>", f"<i>Quarterly shareholding reports filed today (companies worth ₹{MIN_MCAP_CR} cr+), "
             "promoter stake versus the previous quarter.</i>"]
    if up:
        lines += ["", "<b>Promoters raised their stake</b>"]
        lines += [f"🟢 {esc(clean(r['name']))}: {r['was']:.2f}% → <b>{r['now']:.2f}%</b> ({crore(r['mcap'])})" for r in up]
    if down:
        lines += ["", "<b>Promoter stake fell</b> (selling or new shares issued)"]
        lines += [f"🔴 {esc(clean(r['name']))}: {r['was']:.2f}% → <b>{r['now']:.2f}%</b> ({crore(r['mcap'])})" for r in down]
    return {"text": "\n".join(lines)}
