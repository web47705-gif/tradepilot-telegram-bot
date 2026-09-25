# TradePilot Telegram Bot — Rebuilt

Telegram-only TradePilot bot for Render.

## Fixed
- Replaced `Application.run_polling()` startup with explicit asyncio lifecycle.
- Avoids the `There is no current event loop in thread 'MainThread'` failure seen on Render.
- Keeps Flask health endpoints for Render.
- Automatic signals run on the same asyncio loop as Telegram.
- Sends at most one automatic message per new backend candle.
- Does not generate a signal when the backend reports unavailable/stale data.
- Removed unsupported `30m` timeframe from the Telegram UI; the current backend supports `1m`, `5m`, `15m`, `1h`, `4h`.

## Render
Build Command:

`pip install -r requirements.txt`

Start Command:

`python bot.py`

Environment variables:

- `TELEGRAM_BOT_TOKEN` — fresh token from BotFather
- `TRADEPILOT_BACKEND_URL` — `https://tradepilot-live-backend.onrender.com`
- `AUTO_POLL_SECONDS` — `15`

## Telegram commands
- `/start`
- `/signal`
- `/auto`
- `/stop`
