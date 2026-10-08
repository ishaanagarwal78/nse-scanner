"""The 8:30 am brief: one message with everything worth knowing before the market opens.

  - Overnight world markets and GIFT Nifty (NSE's own feed for the GIFT City Nifty future).
  - Yesterday's FII and DII cash buying and selling.
  - Lock-ins ending today (from the tracker).
  - Big companies announcing results today (NSE's board-meeting calendar).
  - Overnight company news that scored medium or high.
  - Index changes announced since the last trading day, and index-fund trades due today.
"""
import re


from curl_cffi import requests as cffi

from . import flows, indexwatch

WORLD = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^N225", "Nikkei"), ("^HSI", "Hang Seng"), ("BZ=F", "Brent crude")]
EVENT_NAMES = {"preipo_6m": "6-month lock-in ends", "anchor_30d": "anchor lock-in ends (half)",
               "anchor_90d": "anchor lock-in ends (rest)", "promoter_18m": "promoter lock-in ends"}


def world():
    s, out = cffi.Session(impersonate="chrome"), []
    for t, name in WORLD:
        try:
            m = s.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1d&range=5d", timeout=20).json()
            closes = [c for c in m["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
            if len(closes) >= 2:
                out.append((name, 100 * (closes[-1] / closes[-2] - 1)))
        except Exception:
            pass
    return out


def gift(nse):
    j = nse.get("https://www.nseindia.com/api/NextApi/apiClient?functionName=getGiftNifty",
                "https://www.nseindia.com/")["data"]
    g, fx = j.get("giftNifty") or {}, j.get("usdInr") or {}
    return g.get("lastprice"), g.get("perchange"), g.get("timestmp"), fx.get("ltp")


def results_today(nse, today, caps):
    d0 = today.strftime("%d-%m-%Y")
    rows = nse.get(f"https://www.nseindia.com/api/event-calendar?index=equities&from_date={d0}&to_date={d0}",
                   "https://www.nseindia.com/")
    d = today.strftime("%d-%b-%Y")
    res = [r for r in rows or [] if r.get("date") == d and "result" in (r.get("purpose") or "").lower()]
    res.sort(key=lambda r: -(caps.get(r["symbol"]) or 0))
    return res


def build(nse, today, cal, caps, overnight, tracked, esc, crore, site):
    sign = lambda x: f"{x:+.1f}%"
    lines = [f"☀️ <b>Before the open, {today:%a %d %b}</b>", ""]
    try:
        last, pc, ts, usd = gift(nse)
        if last:
            lines.append(f"GIFT Nifty <b>{last:,.0f}</b> ({sign(pc)} since its last close). USD/INR {usd}.")
    except Exception:
        pass
    w = world()
    if w:
        lines.append("Overnight: " + ", ".join(f"{n} {sign(c)}" for n, c in w) + ".")
    try:
        f = flows.fii_dii(nse)
        if f:
            fi, di = f.get("FII", {}).get("net"), f.get("DII", {}).get("net")
            lines.append(f"Yesterday ({f['FII']['date']}): foreign investors {'bought' if fi >= 0 else 'sold'} ₹{abs(fi):,.0f} cr, "
                         f"Indian institutions {'bought' if di >= 0 else 'sold'} ₹{abs(di):,.0f} cr.")
    except Exception:
        pass

    unl = [e for e in cal if e.get("free_day") == today.date().isoformat()]
    lines += ["", "<b>Lock-ins ending today</b>"]
    if unl:
        icon = {"high": "🔴", "medium": "🟠", "low": "🟢"}
        for e in sorted(unl, key=lambda e: -(e.get("unlock_value_cr") or 0))[:6]:
            name = re.sub(r"\s+(Ltd\.?|Limited)$", "", e["company"], flags=re.I)
            val = f", shares worth ≈ {crore(e['unlock_value_cr'])}" if e.get("unlock_value_cr") else ""
            lines.append(f"{icon.get(e.get('risk'), '⚪')} {esc(name)}: {EVENT_NAMES.get(e['event'], 'lock-in ends')}{val}")
    else:
        lines.append("None today.")

    try:
        res = results_today(nse, today, caps)
        if res:
            big = [r for r in res if (caps.get(r["symbol"]) or 0) >= 1000][:8]
            names = ", ".join(esc(re.sub(r"\s+(Limited|Ltd\.?)$", "", r["company"], flags=re.I)) for r in big)
            lines += ["", f"<b>Results today</b> ({len(res)} companies)"]
            if names:
                lines.append(f"Biggest: {names}.")
    except Exception:
        pass

    if overnight:
        lines += ["", "<b>Company news overnight</b>"]
        for ev in sorted(overnight, key=lambda e: -(e["mcap_cr"] or 0))[:6]:
            icon = {"positive": "🟢", "negative": "🔴"}.get(ev["tone"], "🟡")
            name = re.sub(r"\s+(Limited|Ltd\.?)$", "", ev["name"] or ev["symbol"], flags=re.I)
            lines.append(f"{icon} <b>{esc(ev['category'])}</b> · {esc(name)} ({crore(ev['mcap_cr']) or 'size n/a'})")

    idx = indexwatch.brief_lines(today, tracked, esc)
    if idx:
        lines += ["", "<b>Index changes</b>"] + idx

    lines += ["", "<i>Pre-open orders are collected 9:00 to 9:08; trading starts 9:15.</i>"]
    kb = [[{"text": "🌐 Open the tracker", "url": site}]]   # link only: a bot button would replace this message
    return {"text": "\n".join(lines)[:4000], "keyboard": kb}
