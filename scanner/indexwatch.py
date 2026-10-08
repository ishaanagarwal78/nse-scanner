"""Index changes from NSE Indices press releases (niftyindices.com).

When a stock joins or leaves an index, funds tracking that index must buy or sell it at the close of the day before
the change takes effect. We list new inclusion/exclusion announcements, flag stocks our users track, and remind
on the day index funds trade.
"""
import io
import re
from datetime import datetime, timedelta

from curl_cffi import requests as cffi

BASE = "https://www.niftyindices.com"
ITEM = re.compile(r'<div class="pressItem" data-date="([^"]+)"[^>]*>.*?<a href=\'([^\']+)\'[^>]*>([^<]+)</a>', re.S)
RELEVANT = re.compile(r"inclusion|exclusion|replacement|change in (the )?(constituents|composition)|reconstitution|"
                      r"addition|deletion|semi-annual review", re.I)
WEF = re.compile(r"w\.?\s*e\.?\s*f\.?\s*([A-Z][a-z]+ \d{1,2}, \d{4})", re.I)


def releases(days=40):
    s = cffi.Session(impersonate="chrome")
    h = s.get(f"{BASE}/press-release", timeout=40).text
    cut = datetime.now() - timedelta(days=days)
    out = []
    for date, href, title in ITEM.findall(h):
        try:
            d = datetime.strptime(date.strip(), "%b %d, %Y")
        except Exception:
            continue
        title = " ".join(title.split())
        if d < cut or not RELEVANT.search(title) or re.search(r"\blaunch", title, re.I):
            continue
        m = WEF.search(title)
        eff = None
        if m:
            try:
                eff = datetime.strptime(m.group(1), "%B %d, %Y")
            except Exception:
                eff = None
        out.append({"date": d, "title": title, "url": BASE + href if href.startswith("/") else href, "effective": eff})
    return out, s


def pdf_text(s, url):
    try:
        import pypdf
        b = s.get(url, timeout=60).content
        return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(b)).pages)
    except Exception:
        return ""


ROW = re.compile(r"^\s*\d+\s+(.+?)\s+([A-Z][A-Z0-9&\-]{1,19})\s*$", re.M)


def changes(text):
    """(included, excluded) company names from a release's tables."""
    inc, exc, mode = [], [], None
    for line in text.splitlines():
        low = line.lower()
        if "about nse indices" in low:
            break
        if re.search(r"being included|following inclusion|inclusions?:", low):
            mode = "inc"
        elif re.search(r"being excluded|following exclusion|exclusions?:", low) and "no exclusion" not in low:
            mode = "exc"
        m = ROW.match(line)
        if m and mode:
            (inc if mode == "inc" else exc).append(" ".join(m.group(1).replace("Ltd.", "").replace("Limited", "").split()))
    return inc, exc


def tracked_in(text, tracked):
    """Tracked tickers (symbol -> name) that appear in the release, by NSE symbol."""
    words = set(re.findall(r"\b[A-Z][A-Z0-9&\-]{1,19}\b", text))
    return [name for sym, name in tracked.items() if sym in words]


def last_weekday_before(d):
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def brief_lines(today, tracked, esc):
    """Lines for the morning brief: announcements since the previous trading day, and index-fund trading today."""
    try:
        rel, s = releases()
    except Exception:
        return []
    since = last_weekday_before(today)
    new = [r for r in rel if r["date"].date() >= since.date()]
    trade_today = [r for r in rel if r["effective"] and last_weekday_before(r["effective"]).date() == today.date()]
    lines = []
    for r in new[:4]:
        text = pdf_text(s, r["url"])
        inc, exc = changes(text)
        hits = tracked_in(text, tracked)
        parts = []
        if inc:
            parts.append("in: " + ", ".join(inc[:6]) + (" and more" if len(inc) > 6 else ""))
        if exc:
            parts.append("out: " + ", ".join(exc[:6]) + (" and more" if len(exc) > 6 else ""))
        detail = f" ({esc('; '.join(parts))})" if parts else ""
        extra = f" Stocks you track: {esc(', '.join(hits[:5]))}." if hits and not parts else ""
        lines.append(f"🗂️ <a href=\"{r['url']}\">{esc(r['title'])}</a>{detail}.{extra}")
    for r in trade_today[:3]:
        inc, exc = changes(pdf_text(s, r["url"]))
        who = ", ".join(inc[:4] + [f"{x} (out)" for x in exc[:4]])
        lines.append(f"⏰ Index funds trade at today's close: {esc(r['title'])}" + (f", {esc(who)}" if who else "") + ".")
    return lines
