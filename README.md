# NSE market scanner

Free, rule-based market alerts for Indian stocks, sent through the lock-in tracker's Telegram bot.
Everything comes from NSE's, BSE's and NSE Indices' own public pages. No broker, no paid data, no AI.

## What it sends

| When (IST) | Message |
|---|---|
| 8:30 am | **Before the open:** GIFT Nifty, overnight world markets, yesterday's FII and DII flows, lock-ins ending today, today's biggest results, overnight company news, index changes and index-fund trades due today |
| 9:08 am | **Pre-open** alerts for unlock-week and watchlist stocks with a strong opening move or a lopsided order book |
| Market hours | **Company news:** high-impact filings instantly (companies worth ₹300 cr+), medium-impact in digests at 12:30 pm and 4 pm; watchlist stocks get everything |
| Market hours | **Insider buying:** promoters, directors or senior staff buying in the open market worth ₹1 cr+ (₹25 lakh+ for watchlist stocks); promoter selling for watchlist and unlock-week stocks |
| Market hours | **Price alerts** for unlock-week and watchlist stocks: falls of 5% and 10%, rises of 5% and 10% (watchlist only), 3% within 10 minutes, volume at 3x the usual pace, with order-flow context |
| 7:15 pm | **Money flows:** FII and DII cash flows, futures positions of foreign investors, professionals and retail versus yesterday, Nifty put-call ratio and the strikes with the most open options |
| 7:15 pm | **Insider and big-holder trades** of the day, and **shareholding shifts**: promoter stakes up or down in quarterly reports filed that day |

## Live prices (our own feed)

Free aggregators delay Indian prices: Yahoo labels NSE "Delayed Quote, 15 min" and TradingView's public
stream marks NSE `delayed_streaming_900`. NSE's own website is not delayed, so the scanner uses it:

- **NSE's push stream**, the one its quote pages use: one connection per followed stock, carrying the last
  price, volume and order book.
- **Polling fallback:** NSE's and BSE's quote APIs, for any followed stock the stream has not updated in 45 seconds.
- **Order flow:** successive quotes become buyer-initiated versus seller-initiated volume per minute, plus the
  shares waiting to buy and sell. This is our own order-flow history for unlock days.
- **Pre-open book** for every stock at 9:02, 9:05 and 9:08. NSE shows only about 10 price levels, so the
  opening price cannot be recomputed exactly; we keep the book and the buy/sell imbalance.
- **Minute bars** for every recent IPO from Yahoo's public websocket, decoded by our own code (delay does not
  matter for history).
- **Lag check** every 15 minutes in the log, for every source.

Each day's files (bars, quotes, order flow, order books, pre-open, stream samples) are kept for 90 days as a
download on the workflow run.

True tick-by-tick data (every order) is sold by NSE for about ₹50 lakh a year and is not available free.

## Schedule

GitHub limits a job to 6 hours, so weekdays have three runs: 08:25-13:00 IST, 12:55-18:50 IST and 19:15 IST.

## Settings

| Name | Where | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Actions secret | Bot that sends alerts |
| `SUBSCRIBERS_KEY` | Actions secret | Reads subscribed chats and watchlists from the tracker |
| `DASHBOARD_URL` | Actions variable | The tracker site |
| `ALERTS_LIVE` | Actions variable | `1` sends messages; anything else is a dry run that only logs |
| `PRICE_ALERTS_LIVE` | Actions variable | `1` also sends price alerts. Keep `0` while the tracker site still sends its own |
| `PREVIEW_CHAT` | Actions variable | A chat id: newer message types go only to that chat until it is cleared |

Test locally without sending anything:

```
python -m scanner.run --backfill 2026-10-08
python -m scanner.run --evening
```

This repository holds code only. No data or credentials are stored here.
