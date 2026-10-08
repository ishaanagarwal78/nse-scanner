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

## Live prices (our own feed, no broker)

Free data aggregators delay Indian prices for free users. Yahoo's quote API labels NSE as "Delayed Quote, 15 min",
and TradingView's public stream marks NSE as `delayed_streaming_900`. The exchanges' own websites are not delayed,
so the scanner builds its feed from them:

- **Alerts** use NSE's quote API (price, volume, best buyer and seller) alternating with BSE's, for stocks in their
  unlock week and stocks on someone's watchlist. Each exchange gets about 10 requests a minute, so with 10 stocks each one gets a fresh price about every 30 seconds.
  Alerts fire on falls of 5% and 10%, rises of 5% and 10% (watchlist only), a 3% move within 10 minutes,
  and volume running at 3x the usual pace.
- **Minute bars** for every recent IPO come from Yahoo's public websocket, decoded by our own code. A delay does
  not matter for history. Each day's bars and quotes are kept for 90 days as a download on the workflow run.
- **Lag check:** every 15 minutes the log prints how far behind each source is.

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
