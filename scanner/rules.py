"""Turn raw NSE corporate announcements into scored events.

Every announcement gets: category, importance (high / medium / low / noise), tone (positive / negative / neutral),
and, for order wins, the order size as a share of the company's market value.
Rules only, no AI: transparent and free.
"""
import re

NOISE = {
    "Certificate under SEBI (Depositories and Participants) Regulations, 2018", "Shareholders meeting",
    "Copy of Newspaper Publication", "Trading Window", "Structural Digital Database", "ESOP/ESOS/ESPS",
    "Committee Meeting Updates", "Investor Presentation", "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Spurt in Volume", "Price movement", "News Verification", "Loss of Share Certificates",
    "Duplicate Share Certificate", "Change in Registrar and Share Transfer Agent", "Compliance Certificate",
    "Statement of deviation(s) or variation(s) under Reg. 32", "Disclosure under Regulation 30A of LODR",
}
ORDER_WORDS = re.compile(r"\b(order|contract|letter of award|LoA|letter of intent|work order|purchase order)s?\b", re.I)
LEADERS = re.compile(r"\b(chief executive|CEO|managing director|MD\b|chief financial|CFO|whole[- ]time director|chairman)", re.I)

# "Rs. 1,250 crore", "INR 450 Cr", "₹12.5 crores", "Rs 980 lakh", "USD 20 million", "Rs. 1.2 billion"
AMOUNT = re.compile(
    r"(?P<cur>rs\.?|inr|₹|usd|us\$|\$|eur|€)\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>crores?|cr\b\.?|lakhs?|lacs?|lakh|millions?|mn\b|billions?|bn\b)",
    re.I)
FX = {"usd": 88.0, "us$": 88.0, "$": 88.0, "eur": 95.0, "€": 95.0}  # approximate rupees per unit


def amount_crore(text):
    """Largest money amount mentioned, in ₹ crore (None if none). Foreign currency uses an approximate rate."""
    best = None
    for m in AMOUNT.finditer(text or ""):
        num = float(m.group("num").replace(",", ""))
        unit = m.group("unit").lower().rstrip(".")
        cur = m.group("cur").lower()
        rupees = num * (FX.get(cur, 1.0))
        if unit.startswith("cr"):
            cr = rupees
        elif unit.startswith(("lakh", "lac")):
            cr = rupees / 100
        elif unit.startswith(("million", "mn")):
            cr = rupees * 1e6 / 1e7
        else:  # billion
            cr = rupees * 1e9 / 1e7
        best = cr if best is None else max(best, cr)
    return best


def classify(desc, text):
    """(category, importance, tone) before market-cap sizing. importance: high/medium/low/noise."""
    d, t = (desc or "").strip(), (text or "")
    low = t.lower()
    if d in NOISE:
        return ("routine", "noise", "neutral")
    if d == "Bagging/Receiving of orders/contracts" or (d in ("Press Release", "General Updates", "Updates") and ORDER_WORDS.search(t)
                                                          and re.search(r"\b(receiv|bag|won|secur|award)", low)):
        return ("Order win", "sized", "positive")
    if d == "Corporate Insolvency Resolution Process" or "insolvency" in low:
        # Only a fresh admission/initiation is news; updates, CoC meetings and creditor filings are routine
        fresh = re.search(r"admi(t|ssion)|initiat|commence|appoint\w* (an? )?(interim )?resolution professional", low)
        routine = re.search(r"update|reconciliation|postpone|coc meeting|meeting of the committee", low)
        if fresh and not routine:
            return ("Insolvency case admitted", "high", "negative")
        return ("Insolvency update", "low", "negative")
    if d == "Change in Auditors" and re.search(r"resign", low):
        return ("Auditor resigned", "high", "negative")
    if re.search(r"\bbuy[- ]?back\b", (d + " " + t).lower()):
        both = (d + " " + t).lower()
        if re.search(r"post[- ]?buy|closure|complet|extinguish|outcome of buy|corrigendum|dispatch|daily report", both):
            return ("Buyback (process step)", "low", "neutral")
        if re.search(r"public announcement|letter of offer|approv|consider|board", both):
            return ("Buyback", "high", "positive")
        return ("Buyback update", "medium", "positive")
    if re.search(r"\bbonus\b", low) and re.search(r"issue|share|approv|recommend", low):
        return ("Bonus shares", "high", "positive")
    if re.search(r"\bopen offer\b", low) or (d == "Disclosure under SEBI Takeover Regulations" and "offer" in low):
        if re.search(r"pre-?offer|post-?offer|corrigendum|letter of offer|recommendation|dispatch|completion|advertisement", low):
            return ("Open offer (process step)", "low", "neutral")
        return ("Open offer", "high", "positive")
    if re.search(r"sub-?division|stock split|split of (equity )?shares", low):
        return ("Stock split", "medium", "positive")
    if d.startswith("Credit Rating"):
        if re.search(r"upgrad", low):
            return ("Rating upgrade", "medium", "positive")
        if re.search(r"downgrad|negative outlook|watch with negative", low):
            return ("Rating downgrade", "medium", "negative")
        return ("Credit rating", "low", "neutral")
    if d in ("Resignation", "Resignation of Director/KMP/SMP", "Change in Management", "Cessation") and LEADERS.search(t):
        return ("Top leadership exit" if re.search(r"resign|cessation|step(s|ped)? down", low) else "Leadership change", "medium", "negative")
    if d in ("Acquisition", "Amalgamation/Merger", "Agreements") or re.search(r"\b(acquisition|acquire|merger|amalgamation)\b", low) and d not in ("Updates",):
        return ("Deal / acquisition", "medium", "neutral")
    if re.search(r"\b(qip|qualified institutions placement|preferential issue|rights issue|fund rais)", low):
        return ("Fund raise", "medium", "neutral")
    if d == "Disclosure under SEBI Takeover Regulations" and re.search(r"pledge|encumbr", low):
        return ("Shares pledged", "medium", "negative")
    if d == "Disclosure of material issue" or d.startswith("Action(s)"):
        return ("Regulatory / legal", "medium", "negative")
    if d == "Commencement of commercial production/operations":
        return ("New capacity live", "medium", "positive")
    if d in ("Outcome of Board Meeting", "Financial Result Updates", "Integrated Filing- Financial") or "financial results" in low:
        return ("Results", "low", "neutral")
    if d in ("Monthly Business Updates", "Pendency of Litigation(s)/dispute(s) or the outcome impacting the Company"):
        return (d.split("(")[0].strip(), "low", "neutral")
    return (d or "Update", "low", "neutral")


def size_order(amount_cr, mcap_cr):
    """Importance of an order win by its size relative to market value."""
    if amount_cr is None or not mcap_cr:
        return "low"
    share = amount_cr / mcap_cr
    return "high" if share >= 0.10 else "medium" if share >= 0.03 else "low"
