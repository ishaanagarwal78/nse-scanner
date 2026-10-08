"""Pre-open session capture, 9:00 to 9:08 am.

NSE collects orders without matching them, then sets each stock's opening price at the level where the most shares
can trade. During those minutes it publishes, for every stock, the indicated opening price, the total shares waiting
to buy and to sell, and about 10 price levels of the order book around the indicated price.

NSE shows only those ~10 levels, so the opening price cannot be recomputed exactly from public data; we record the
book and use the buy/sell imbalance, which is the earliest public signal of the day.
"""
import csv
import json
import os

URL = "https://www.nseindia.com/api/NextApi/apiClient/cmPreOpenApi?functionName=getPreOpenData&category=ALL&symbol="
REF = "https://www.nseindia.com/market-data/pre-open-market-cm-and-emerge-market"
SNAP_TIMES = ("09:02:00", "09:05:00", "09:08:30")


def fetch(session):
    r = session.get(URL, headers={"Referer": REF}, timeout=60)
    return r.json() if r.status_code == 200 else None


def rows(snap):
    out = {}
    for x in (snap or {}).get("data", []):
        price = x.get("IEP") or x.get("finalPrice") or x.get("iepOrderBook")
        prev = x.get("basePrice") or x.get("prevClose")   # base price is adjusted for splits and bonus issues
        if not price or not prev:
            continue
        out[x["symbol"]] = {"price": price, "prev": prev, "chg": 100 * (price / prev - 1),
                            "match_qty": x.get("finalQuantity") or x.get("totTradedQty") or 0,
                            "buy": x.get("totalBuyQuantity") or 0, "sell": x.get("totalSellQuantity") or 0,
                            "book": x.get("orderBook") or [], "time": x.get("lastUpdateTime"), "series": x.get("series")}
    return out


def save(out_dir, day, snaps, keep_books):
    """All stocks' final pre-open figures, plus each snapshot's order book for the stocks we follow."""
    os.makedirs(out_dir, exist_ok=True)
    if not snaps:
        return
    final = snaps[-1][1]
    with open(f"{out_dir}/preopen_{day}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "series", "prev_close", "indicated_open", "change_pct", "matched_qty", "buy_qty", "sell_qty", "nse_time"])
        for sym, r in sorted(final.items()):
            w.writerow([sym, r["series"], r["prev"], r["price"], round(r["chg"], 2), r["match_qty"], r["buy"], r["sell"], r["time"]])
    with open(f"{out_dir}/preopen_books_{day}.jsonl", "w") as f:
        for hhmm, snap in snaps:
            for sym in keep_books:
                if sym in snap:
                    f.write(json.dumps({"snap": hhmm, "symbol": sym, **snap[sym]}) + "\n")


def alert(sym, r, e, name, clean_fn):
    """Message for a followed stock with a strong pre-open signal, or None."""
    ratio = (r["sell"] / r["buy"]) if r["buy"] else None
    strong = abs(r["chg"]) >= 3 or (ratio is not None and (ratio >= 3 or ratio <= 1 / 3) and r["sell"] + r["buy"] > 20000)
    if not strong:
        return None
    where = "unlock week" if e else "on your watchlist"
    lines = [f"🌅 <b>Pre-open</b> · {clean_fn(name)} · {where}",
             f"Indicated opening price <b>₹{r['price']:,.2f}</b> ({r['chg']:+.1f}% vs yesterday's close)."]
    if ratio is not None:
        if ratio >= 1:
            lines.append(f"Shares waiting to sell: <b>{ratio:.1f}×</b> the shares waiting to buy.")
        else:
            lines.append(f"Shares waiting to buy: <b>{1 / ratio:.1f}×</b> the shares waiting to sell.")
    lines.append(f"<i>NSE pre-open order book at {r['time'] or '9:08'}; the price can still change before 9:15.</i>")
    return {"text": "\n".join(lines), "keyboard": [[{"text": "📊 Details", "callback_data": f"s:{sym}"}]]}
