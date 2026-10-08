"""Evening money-flow report: who bought and sold today, and how traders are positioned for tomorrow.

Sources, all published by NSE every trading evening:
  - FII and DII cash-market buying and selling (provisional figures).
  - Participant-wise open interest: futures and options positions held by foreign investors (FII),
    domestic institutions (DII), professional traders (Pro) and retail/other clients, today versus yesterday.
  - Nifty option chain for the nearest expiry: put-call ratio, and the strikes with the most open calls and puts,
    which traders watch as resistance and support.
"""
import csv
import io
from datetime import datetime, timedelta

GROUPS = ("Client", "DII", "FII", "Pro")


def fii_dii(nse):
    rows = nse.get("https://www.nseindia.com/api/fiidiiTradeReact", "https://www.nseindia.com/reports/fii-dii")
    out = {}
    for r in rows or []:
        cat = "FII" if "FII" in r.get("category", "") else "DII" if "DII" in r.get("category", "") else None
        if cat:
            out[cat] = {"net": float(r["netValue"]), "buy": float(r["buyValue"]), "sell": float(r["sellValue"]), "date": r.get("date")}
    return out


def participant_oi(nse, day):
    """{group: {column: contracts}} for `day`, or None if the file is not out."""
    url = f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{day:%d%m%Y}.csv"
    try:
        r = nse.s.get(url, timeout=30)
    except Exception:
        return None
    if r.status_code != 200 or "Client Type" not in r.text:
        return None
    lines = r.text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Client Type"))
    out = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        row = {k.strip(): v for k, v in row.items() if k}
        g = (row.get("Client Type") or "").strip()
        if g in GROUPS:
            out[g] = {k: float(v) for k, v in row.items() if k != "Client Type" and v and v.strip().replace(".", "").isdigit()}
    return out or None


def previous_oi(nse, day):
    d = day - timedelta(days=1)
    for _ in range(7):
        if d.weekday() < 5:
            x = participant_oi(nse, d)
            if x:
                return d, x
        d -= timedelta(days=1)
    return None, None


def option_chain(nse):
    ref = "https://www.nseindia.com/option-chain"
    base = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName="
    exp = nse.get(f"{base}getOptionChainDropdown&symbol=NIFTY", ref).get("expiryDates", [None])[0]
    j = nse.get(f"{base}getOptionChainData&symbol=NIFTY&params=expiryDate={exp}", ref)
    rows = j.get("data", [])
    ce = {r["strikePrice"]: (r.get("CE") or {}).get("openInterest") or 0 for r in rows}
    pe = {r["strikePrice"]: (r.get("PE") or {}).get("openInterest") or 0 for r in rows}
    tot_ce, tot_pe = sum(ce.values()), sum(pe.values())
    return {"expiry": exp, "spot": j.get("underlyingValue"), "pcr": tot_pe / tot_ce if tot_ce else None,
            "call_wall": max(ce, key=ce.get) if ce else None, "put_wall": max(pe, key=pe.get) if pe else None}


def nifty_close(nse):
    d = nse.get("https://www.nseindia.com/api/NextApi/apiClient?functionName=getIndexData&&index=NIFTY%2050",
                "https://www.nseindia.com/market-data/live-equity-market")["data"][0]
    return d.get("last"), d.get("percChange"), d.get("timeVal")


def long_share(g):
    lo, sh = g.get("Future Index Long", 0), g.get("Future Index Short", 0)
    return 100 * lo / (lo + sh) if lo + sh else None


def mood(pct):
    if pct is None:
        return ""
    return "mostly betting on a rise" if pct >= 60 else "mostly betting on a fall" if pct <= 40 else "split"


def report(nse, day, esc):
    lines = [f"💰 <b>Money flows, {day:%d %b}</b>", "<i>Who bought and sold today, and how traders are set up for tomorrow.</i>", ""]
    try:
        last, chg, _ = nifty_close(nse)
        lines.append(f"Nifty 50 closed at <b>{last:,.2f}</b> ({chg:+.2f}%).")
    except Exception:
        pass
    try:
        f = fii_dii(nse)
        fresh = f and all(datetime.strptime(v["date"], "%d-%b-%Y").date() == day.date() for v in f.values())
        if fresh:
            lines += ["", "<b>Cash market</b> (provisional)"]
            for k, label in (("FII", "Foreign investors"), ("DII", "Indian institutions")):
                v = f[k]["net"]
                lines.append(f"{'🟢' if v >= 0 else '🔴'} {label} {'bought' if v >= 0 else 'sold'} a net <b>₹{abs(v):,.0f} cr</b>")
        else:
            lines += ["", "<i>Today's FII and DII figures are not out yet.</i>"]
    except Exception:
        lines += ["", "<i>FII and DII figures unavailable.</i>"]
    oi = participant_oi(nse, day)
    if oi:
        pday, prev = previous_oi(nse, day)
        lines += ["", "<b>Index futures positions</b> (share of contracts that are bets on a rise)"]
        for g, label in (("FII", "Foreign investors"), ("Pro", "Professional traders"), ("Client", "Retail and others")):
            now = long_share(oi.get(g, {}))
            if now is None:
                continue
            was = long_share(prev.get(g, {})) if prev else None
            delta = f", {now - was:+.1f} pts vs {pday:%d %b}" if was is not None else ""
            lines.append(f"{label}: <b>{now:.0f}%</b> long, {mood(now)}{delta}")
        fii = oi.get("FII", {})
        net_calls = fii.get("Option Index Call Long", 0) - fii.get("Option Index Call Short", 0)
        net_puts = fii.get("Option Index Put Long", 0) - fii.get("Option Index Put Short", 0)
        lines.append(f"Foreign investors in index options: net {'long' if net_calls >= 0 else 'short'} {abs(net_calls):,.0f} calls, "
                     f"net {'long' if net_puts >= 0 else 'short'} {abs(net_puts):,.0f} puts.")
    else:
        lines += ["", "<i>Today's futures position data is not out yet.</i>"]
    try:
        oc = option_chain(nse)
        if oc["pcr"]:
            lean = ("more open puts than calls" if oc["pcr"] > 1 else "more open calls than puts")
            lines += ["", f"<b>Nifty options</b>, expiry {esc(oc['expiry'])}",
                      f"Put-call ratio <b>{oc['pcr']:.2f}</b>: {lean}. Readings below 0.7 or above 1.3 are often "
                      f"treated as stretched.",
                      f"Most open calls at {oc['call_wall']:,.0f} (watched as resistance), "
                      f"most open puts at {oc['put_wall']:,.0f} (watched as support)."]
    except Exception:
        pass
    lines += ["", "<i>Source: NSE end-of-day reports. FII and DII cash figures are provisional.</i>"]
    return {"text": "\n".join(lines)}
