# TradePilot Telegram — Complete

Telegram-only live market signal bot. One Render Web Service; no separate backend.

## Environment variables

- TELEGRAM_BOT_TOKEN — required
- TWELVE_DATA_API_KEY — required for Forex/XAUUSD
- PORT — optional, Render sets it automatically

## Deploy

Build:
`pip install -r requirements.txt`

Start:
`python bot.py`

## Telegram commands

/start
/signal
/auto
/stop

## Supported symbols

Crypto: BTCUSD, ETHUSD
Forex/metal: EURUSD, GBPUSD, USDJPY, USDCHF, USDCAD, AUDUSD, NZDUSD, XAUUSD

## Timeframes

1m, 5m, 15m, 1h, 4h

## Data behavior

Crypto uses Binance Spot candles. Forex/XAUUSD uses Twelve Data when TWELVE_DATA_API_KEY is configured.
Market data is centrally cached by symbol/timeframe to reduce provider requests and protect against 429 rate limits.
The bot never fabricates candles. If verified data is unavailable or stale, it sends no trading signal.
Auto Signal checks every 20 seconds but only sends once per newly observed confirmed candle.
