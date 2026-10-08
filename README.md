# NSE company-news scanner

Reads every corporate announcement filed on NSE during market hours, filters out routine filings,
scores the rest by rules, and sends Telegram alerts for the ones that matter.

- **High impact, sent instantly** (companies worth ₹300 cr or more): order wins worth 10%+ of the company's
  market value, buybacks, bonus issues, open offers, auditor resignations, insolvency proceedings.
- **Medium impact, sent in digests** at about 12:30 pm and 4 pm: rating changes, CEO/CFO exits,
  acquisitions, fund raises, pledges, regulatory actions, orders worth 3-10% of market value.
- **Watchlist stocks** get every relevant item instantly.

Company size comes from AMFI's half-yearly list of average market values, plus recent IPOs from the
lock-in tracker. The scanner makes one NSE request every 90 seconds, nothing more.

Two sessions run on weekdays (GitHub limits a job to 6 hours): 08:55-13:00 IST and 13:00-19:00 IST.

## Settings

| Name | Where | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Actions secret | Bot that sends alerts |
| `SUBSCRIBERS_KEY` | Actions secret | Reads subscribed chats and watchlists from the tracker |
| `DASHBOARD_URL` | Actions variable | The tracker site |
| `ALERTS_LIVE` | Actions variable | `1` sends alerts; anything else is a dry run that only logs |

Test locally without sending anything:

```
python -m scanner.run --backfill 2026-10-08
```

This repository holds code only. No data or credentials are stored here.
