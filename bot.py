import os
import asyncio
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BACKEND_URL = os.getenv(
    "TRADEPILOT_BACKEND_URL",
    "https://tradepilot-live-backend.onrender.com",
).rstrip("/")
POLL_SECONDS = max(15, int(os.getenv("AUTO_POLL_SECONDS", "15")))
PORT = int(os.getenv("PORT", "10000"))

app = Flask(__name__)
subscriptions = {}
subscriptions_lock = threading.Lock()

# These match the current backend timeframes.
SYMBOLS = ["BTCUSD", "ETHUSD", "XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]
TFS = ["1m", "5m", "15m", "1h", "4h"]


def api_market(symbol: str, tf: str) -> dict:
    response = requests.get(
        f"{BACKEND_URL}/market",
        params={"symbol": symbol, "tf": tf},
        timeout=25,
        headers={"User-Agent": "TradePilot-Telegram/2.0"},
    )
    response.raise_for_status()
    return response.json()


def fmt_price(value):
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{value:,.2f}" if abs(value) >= 100 else f"{value:,.5f}"


def signal_text(data: dict, symbol: str, tf: str, auto: bool = False) -> str:
    if data.get("error"):
        error = data.get("error")
        if error == "market_closed":
            return f"⏸ <b>{symbol} · {tf}</b>\n\n🔒 Market closed."
        if error in {"data_stale", "live_data_unavailable"}:
            return f"⚠️ <b>{symbol} · {tf}</b>\n\n🕐 Verified live data is unavailable or stale.\nNo signal generated."
        return (
            f"⚠️ <b>{symbol} · {tf}</b>\n\n"
            f"❌ Live data unavailable.\n"
            f"<code>{str(data.get('message', error))[:500]}</code>"
        )

    signal = str(data.get("signal", "WAIT")).upper()
    icon = {"BUY": "🟢", "SELL": "🔴", "CALL": "🟢", "PUT": "🔴", "WAIT": "🟡"}.get(signal, "🟡")
    lines = [
        f"{icon} <b>{signal}</b>  |  <b>{symbol} · {tf}</b>",
        "",
        f"💰 Price: <b>{fmt_price(data.get('entry'))}</b>",
        f"📈 Trend: <b>{data.get('trend', '—')}</b>",
        f"🎯 Confidence: <b>{data.get('confidence', 0)}%</b>",
        f"📊 RSI: <b>{data.get('rsi', '—')}</b>",
        f"〽️ MACD: <b>{data.get('macd', '—')}</b>",
        f"💪 ADX: <b>{data.get('adx', '—')}</b>",
        f"🏗 Structure: <b>{data.get('structure', '—')}</b>",
        "",
        f"📡 {data.get('source', 'verified data')}",
        f"🕐 Candle: <code>{data.get('timestamp', '—')}</code>",
    ]
    if signal in {"BUY", "SELL", "CALL", "PUT"}:
        lines.extend([
            f"🛑 SL: <b>{fmt_price(data.get('sl'))}</b>",
            f"🎯 TP1: <b>{fmt_price(data.get('tp1'))}</b>",
            f"🎯 TP2: <b>{fmt_price(data.get('tp2'))}</b>",
        ])
    lines.extend(["", f"💡 {data.get('reason', 'No setup.')}" ])
    if auto:
        lines.extend(["", "🤖 <i>Automatic signal</i>"])
    return "\n".join(lines)


def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Get Signal", callback_data="menu_signal"),
            InlineKeyboardButton("🤖 Auto Signal", callback_data="menu_auto"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"),
            InlineKeyboardButton("⏹ Stop Auto", callback_data="stop"),
        ],
    ])


def symbol_menu(prefix: str):
    rows = []
    for i in range(0, len(SYMBOLS), 3):
        rows.append([
            InlineKeyboardButton(symbol, callback_data=f"{prefix}_sym_{symbol}")
            for symbol in SYMBOLS[i:i + 3]
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def tf_menu(prefix: str, symbol: str):
    rows = []
    for i in range(0, len(TFS), 3):
        rows.append([
            InlineKeyboardButton(tf, callback_data=f"{prefix}_tf_{symbol}_{tf}")
            for tf in TFS[i:i + 3]
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_signal")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>TradePilot Telegram</b>\n\n"
        "Verified live market analysis.\n\nChoose an option:",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Select symbol:", reply_markup=symbol_menu("sig"))


async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Select symbol for Auto Signal:", reply_markup=symbol_menu("auto"))


async def stop_for_chat(chat_id: int):
    with subscriptions_lock:
        subscription = subscriptions.pop(chat_id, None)
    if subscription:
        task = subscription.get("task")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_for_chat(update.effective_chat.id)
    await update.message.reply_text("⏹ Auto Signal stopped.", reply_markup=main_menu())


async def auto_loop(application: Application, chat_id: int, symbol: str, tf: str):
    last_candle = None
    try:
        while True:
            try:
                data = await asyncio.to_thread(api_market, symbol, tf)
                candle = data.get("timestamp")

                # Only send once for each new backend candle.
                if candle and candle != last_candle:
                    last_candle = candle
                    # Do not generate a signal when data is unavailable/stale.
                    if not data.get("error") or data.get("error") == "market_closed":
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=signal_text(data, symbol, tf, auto=True),
                            parse_mode="HTML",
                        )
            except requests.RequestException:
                pass
            except Exception:
                pass

            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        raise


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat_id

    if data == "home":
        await query.edit_message_text(
            "🚀 <b>TradePilot</b>\n\nChoose an option:",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "menu_signal":
        await query.edit_message_text("📊 Select symbol:", reply_markup=symbol_menu("sig"))
        return

    if data == "menu_auto":
        await query.edit_message_text("🤖 Select symbol for Auto Signal:", reply_markup=symbol_menu("auto"))
        return

    if data == "menu_settings":
        await query.edit_message_text(
            f"⚙️ <b>Settings</b>\n\nAuto polling: {POLL_SECONDS}s\nBackend: <code>{BACKEND_URL}</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "stop":
        await stop_for_chat(chat_id)
        await query.edit_message_text("⏹ Auto Signal stopped.", reply_markup=main_menu())
        return

    parts = data.split("_")
    if len(parts) >= 3 and parts[1] == "sym":
        mode, symbol = parts[0], parts[2]
        await query.edit_message_text(
            f"<b>{symbol}</b> selected. Choose timeframe:",
            parse_mode="HTML",
            reply_markup=tf_menu(mode, symbol),
        )
        return

    if len(parts) >= 4 and parts[1] == "tf":
        mode, symbol, tf = parts[0], parts[2], parts[3]

        if mode == "sig":
            await query.edit_message_text("⏳ Checking verified live market…")
            try:
                market = await asyncio.to_thread(api_market, symbol, tf)
                await query.edit_message_text(
                    signal_text(market, symbol, tf),
                    parse_mode="HTML",
                    reply_markup=main_menu(),
                )
            except Exception as exc:
                await query.edit_message_text(
                    f"❌ Backend error:\n<code>{str(exc)[:700]}</code>",
                    parse_mode="HTML",
                    reply_markup=main_menu(),
                )
            return

        await stop_for_chat(chat_id)
        task = asyncio.create_task(auto_loop(context.application, chat_id, symbol, tf))
        with subscriptions_lock:
            subscriptions[chat_id] = {
                "symbol": symbol,
                "tf": tf,
                "task": task,
                "enabled": True,
            }
        await query.edit_message_text(
            f"🤖 <b>Auto Signal ON</b>\n\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Timeframe: <b>{tf}</b>\n"
            f"Check interval: <b>{POLL_SECONDS}s</b>\n\n"
            "The bot will automatically check for each new candle.\n"
            "Use /stop to stop it.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )


@app.get("/")
def root():
    return jsonify({
        "service": "TradePilot Telegram Bot",
        "status": "ok",
        "backend": BACKEND_URL,
        "subscriptions": len(subscriptions),
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "subscriptions": len(subscriptions),
        "time": datetime.now(timezone.utc).isoformat(),
    })


def run_web():
    # Flask only supplies Render's health/HTTP port; Telegram runs on the main asyncio loop.
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


async def telegram_main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("signal", signal_cmd))
    application.add_handler(CommandHandler("auto", auto_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CallbackQueryHandler(callbacks))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def main_async():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    threading.Thread(target=run_web, daemon=True, name="render-health-server").start()
    await telegram_main()


if __name__ == "__main__":
    asyncio.run(main_async())
