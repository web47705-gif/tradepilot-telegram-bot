# TradePilot Telegram Bot — Rebuilt

This version removes the Binance dependency that was returning HTTP 451 on Render.
Crypto uses Coinbase public candles. FX/gold uses Twelve Data when `TWELVE_DATA_API_KEY`
is configured, with Yahoo Finance as a fallback.

## Render environment variables

Required:
- `TELEGRAM_BOT_TOKEN` = your BotFather token

Optional:
- `TWELVE_DATA_API_KEY` = your Twelve Data API key. If absent, FX/gold will try Yahoo Finance.

Do not put tokens in GitHub source files.

## Render
Build: `pip install -r requirements.txt`
Start: `python bot.py`

After deploy, use `/start` in Telegram.
