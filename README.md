# TradePilot Telegram Bot

Standalone Telegram-only version of TradePilot. It uses the existing Render live backend for verified market candles and signal analysis.

## Environment variables

- `TELEGRAM_BOT_TOKEN` = token from BotFather
- `TRADEPILOT_BACKEND_URL` = existing backend URL
- `AUTO_POLL_SECONDS` = polling interval; default 15 seconds

## Commands

- `/start` — main menu
- `/signal` — one-time live signal
- `/auto` — choose symbol/timeframe and start automatic signals
- `/stop` — stop automatic signals

## Important

The bot sends an automatic message only when the backend reports a new candle timestamp. It does not generate a signal when verified market data is unavailable or stale.

Deploy as a Render Web Service. Add the environment variables before deploying.
