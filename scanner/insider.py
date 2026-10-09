"""Insider and big-holder trades from NSE filings.

Insider trades (SEBI insider-trading rules, Reg 7(2)): promoters, directors and senior staff must report trades
within 2 working days. NSE publishes each report as an XBRL file; we read the person, their role, buy or sell,
how (market purchase, off-market, gift ...), shares and value.

Big holders (SEBI takeover rules, Reg 29): anyone crossing 5% of a company, and holders above 5% moving 2% or more.

Rules (no AI):
  - Promoter / director / senior staff BUYING in the open market worth ₹1 cr or more: instant alert
    (₹25 lakh or more if the stock is on someone's watchlist).
  - Promoter selling in the open market worth ₹1 cr or more: instant for watchlist and unlock-week stocks.
  - Everything else of note goes into the evening digest.
"""
import html
import re
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
INSIDER_ROLES = re.compile(r"promoter|director|key managerial|kmp|designated|immediate relative", re.I)
PROMOTER = re.compile(r"promoter", re.I)
TAG = re.compile(r'<in-bse-co:([A-Za-z]+) contextRef="([^"]+)"[^>]*>([^<]*)</in-bse-co:\1>')
BUY_MIN_CR, WATCH_BUY_MIN_CR, SELL_MIN_CR = 1.0, 0.25, 1.0


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def parse_xbrl(xml):
    """One filing -> list of trades (one per person/disclosure context)."""
    ctx = {}
    for name, ref, val in TAG.findall(xml):
        ctx.setdefault(ref, {})[name] = html.unescape(val.strip())
    main = ctx.get("MainI", {})
    out = []
    for ref, f in ctx.items():
        if ref == "MainI" or "NameOfThePerson" not in f:
            continue
        out.append({
            "symbol": main.get("Symbol") or f.get("Symbol"),
            "company": main.get("NameOfTheCompany"),
            "person": f.get("NameOfThePerson"),
            "role": f.get("CategoryOfPerson", ""),
            "type": f.get("SecuritiesAcquiredOrDisposedTransactionType", ""),
            "mode": f.get("ModeOfAcquisitionOrDisposal", ""),
            "instrument": f.get("TypeOfInstrument", ""),
            "shares": _num(f.get("SecuritiesAcquiredOrDisposedNumberOfSecurity")),
            "value_cr": (_num(f.get("SecuritiesAcquiredOrDisposedValueOfSecurity")) or 0) / 1e7,
            "pct_before": _num(f.get("SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding")),
            "pct_after": _num(f.get("SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding")),
            "from": f.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate"),
            "to": f.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate"),
        })
    return out


def side(t):
    """'buy' / 'sell' for open-market trades by insiders; None for gifts, pledges, ESOPs, off-market transfers."""
    if not INSIDER_ROLES.search(t["role"] or "") or "equity" not in (t["instrument"] or "equity").lower():
        return None
    mode, typ = (t["mode"] or "").lower(), (t["type"] or "").lower()
    if "market" not in mode or "off" in mode:
        return None
    if "buy" in typ or "acqui" in typ or "purchase" in mode:
        return "buy"
    if "sell" in typ or "sale" in typ or "dispos" in typ or "sale" in mode:
        return "sell"
    return None


def fetch(nse, day, seen=None, since=None):
    """New insider trades filed on `day` (skipping filing files in `seen`; only those broadcast after `since`)."""
    d = day.strftime("%d-%m-%Y")
    j = nse.get(f"https://www.nseindia.com/api/corporates-pit-gg?index=equities&from_date={d}&to_date={d}",
                "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading")
    rows = j.get("data", []) if isinstance(j, dict) else j
    trades = []
    for r in rows:
        key = r.get("xmlFileName")
        if not key or (seen is not None and key in seen):
            continue
        if seen is not None:
            seen.add(key)
        try:
            when = datetime.strptime(r.get("broadcastDateTime", ""), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except Exception:
            when = None
        if since and when and when < since:
            continue
        try:
            xml = nse.s.get(key, timeout=30).text
        except Exception:
            continue
        for t in parse_xbrl(xml):
            t["symbol"] = t["symbol"] or r.get("symbol")
            t["filed"] = when
            t["side"] = side(t)
            if t["side"]:
                trades.append(t)
        time.sleep(0.4)
    return combine(trades)


def combine(trades):
    """One line per company, person and side (a person often files several trades together)."""
    out = {}
    for t in trades:
        k = (t["symbol"], t["person"], t["side"])
        if k in out:
            o = out[k]
            o["value_cr"] += t["value_cr"]
            o["shares"] = (o["shares"] or 0) + (t["shares"] or 0)
            o["pct_after"] = t["pct_after"] if t["pct_after"] is not None else o["pct_after"]
        else:
            out[k] = dict(t)
    return list(out.values())


def instant(t, mcap, watched, unlocking):
    if t["side"] == "buy":
        return t["value_cr"] >= (WATCH_BUY_MIN_CR if watched else BUY_MIN_CR) and ((mcap or 0) >= 300 or watched)
    return PROMOTER.search(t["role"]) and t["value_cr"] >= SELL_MIN_CR and (watched or unlocking)


def _name(s):
    return re.sub(r"\s+(Limited|Ltd\.?)$", "", " ".join(str(s or "").split()).title(), flags=re.I)


def role(t):
    r = t["role"] or ""
    if re.search(r"promoter", r, re.I):
        return "promoter"
    if re.search(r"director", r, re.I):
        return "director"
    return "senior staff" if re.search(r"key managerial|kmp|designated", r, re.I) else "insider"


def fmt(t, mcap, esc, crore):
    buy = t["side"] == "buy"
    lines = [f"{'🟢' if buy else '🔴'} <b>Insider {'buying' if buy else 'selling'}</b> · {esc(_name(t['company'] or t['symbol']))} "
             f"<code>{esc(t['symbol'])}</code>",
             f"{esc(_name(t['person']))}, a {role(t)}, {'bought' if buy else 'sold'} shares worth ≈ <b>{crore(t['value_cr'])}</b> "
             f"in the open market."]
    if t["pct_after"] is not None:
        lines.append(f"Their holding is now {t['pct_after']:.2f}% of the company.")
    if mcap:
        lines.append(f"Company value {crore(mcap)}.")
    when = f"{t['from']}" + (f" to {t['to']}" if t.get("to") and t["to"] != t["from"] else "")
    lines.append(f"<i>Trade dates {esc(when)}. Filed with NSE{t['filed'].strftime(' at %H:%M IST') if t.get('filed') else ''}.</i>")
    return {"text": "\n".join(lines), "keyboard": [[{"text": "📊 Stock details", "callback_data": f"s:{t['symbol']}"}]]}


def big_holders(nse, day):
    """Takeover-rule disclosures for `day`: new 5% holders buying in the market, and promoters adding 2%+."""
    d = day.strftime("%d-%m-%Y")
    j = nse.get(f"https://www.nseindia.com/api/corporate-sast-reg29?index=equities&from_date={d}&to_date={d}",
                "https://www.nseindia.com/companies-listing/corporate-filings-regulation-29")
    out = []
    for r in j.get("data", []) if isinstance(j, dict) else []:
        kind_, mode = (r.get("acqSaleType") or "").lower(), (r.get("acquisitionMode") or "").lower()
        after = _num(r.get("totAftShare"))
        if after is None or "inter-se" in mode:
            continue
        promoter = (r.get("promoterType") or "").upper() == "Y"
        reg1 = r.get("regType") == "Reg29(1)"
        if kind_ == "acquisition" and not promoter and reg1 and "preferential" not in mode:
            kind = "new 5% holder"
        elif kind_ == "acquisition" and promoter and "market" in mode:
            kind = "promoter bought more in the market"
        elif kind_ == "sale" and not promoter and "market" in mode:
            kind = "big holder sold in the market"
        else:
            continue
        out.append({"symbol": r["symbol"], "company": html.unescape(r.get("company") or ""), "who": html.unescape(r.get("acquirerName") or ""), "after": after,
                    "kind": kind})
    seen, uniq = set(), []
    for x in out:
        if (x["symbol"], x["who"]) not in seen:
            seen.add((x["symbol"], x["who"]))
            uniq.append(x)
    return uniq


def digest(trades, holders, caps, esc, crore, title):
    buys = sorted([t for t in trades if t["side"] == "buy"], key=lambda t: -t["value_cr"])
    sells = sorted([t for t in trades if t["side"] == "sell" and PROMOTER.search(t["role"])], key=lambda t: -t["value_cr"])
    buys = [t for t in buys if t["value_cr"] >= 0.1][:8]
    sells = [t for t in sells if t["value_cr"] >= 0.5][:6]
    if not (buys or sells or holders):
        return None
    lines = [f"🕵️ <b>{esc(title)}</b>", "<i>Open-market trades reported to NSE today, biggest first.</i>"]
    if buys:
        lines += ["", "<b>Insiders buying</b>"]
        lines += [f"🟢 {esc(_name(t['company'] or t['symbol']))}: {role(t)} bought {crore(t['value_cr'])}" for t in buys]
    if sells:
        lines += ["", "<b>Promoters selling</b>"]
        lines += [f"🔴 {esc(_name(t['company'] or t['symbol']))}: sold {crore(t['value_cr'])}" for t in sells]
    if holders:
        lines += ["", "<b>Big holders</b> (5% or more of a company)"]
        for h in sorted(holders, key=lambda h: -(caps.get(h["symbol"]) or 0))[:6]:
            held = f", now holds {h['after']:.2f}%" if h["after"] >= 5 or h["kind"] == "new 5% holder" else ""
            lines.append(f"🔵 {esc(_name(h['company'] or h['symbol']))}: {esc(_name(h['who']))}, {h['kind']}{held}")
    return {"text": "\n".join(lines)}
