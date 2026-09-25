import os
import asyncio
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
BACKEND_URL = os.getenv('TRADEPILOT_BACKEND_URL', 'https://tradepilot-live-backend.onrender.com').rstrip('/')
POLL_SECONDS = int(os.getenv('AUTO_POLL_SECONDS', '15'))

app = Flask(__name__)
subscriptions = {}  # chat_id -> {symbol, tf, task, last_candle, enabled}
lock = threading.Lock()

SYMBOLS = ['BTCUSD','ETHUSD','XAUUSD','EURUSD','GBPUSD','USDJPY']
TFS = ['1m','5m','15m','30m','1h']


def api_market(symbol, tf):
    r = requests.get(f'{BACKEND_URL}/market', params={'symbol': symbol, 'tf': tf}, timeout=25)
    r.raise_for_status()
    return r.json()


def fmt_price(v):
    if v is None: return '—'
    if abs(v) >= 100: return f'{v:,.2f}'
    return f'{v:,.5f}'


def signal_text(d, symbol, tf, auto=False):
    if d.get('error'):
        err = d.get('error')
        if err == 'market_closed':
            return f'⏸ <b>{symbol} · {tf}</b>\n\n🔒 Market closed.'
        if err == 'data_stale':
            return f'⚠️ <b>{symbol} · {tf}</b>\n\n🕐 Live data is stale. No signal generated.'
        return f'⚠️ <b>{symbol} · {tf}</b>\n\n❌ Live data unavailable.\n<code>{d.get("message", err)}</code>'

    s = d.get('signal','WAIT')
    icon = {'BUY':'🟢','SELL':'🔴','CALL':'🟢','PUT':'🔴','WAIT':'🟡'}.get(s,'🟡')
    lines = [
        f'{icon} <b>{s}</b>  |  <b>{symbol} · {tf}</b>',
        '',
        f'💰 Price: <b>{fmt_price(d.get("entry"))}</b>',
        f'📈 Trend: <b>{d.get("trend","—")}</b>',
        f'🎯 Confidence: <b>{d.get("confidence",0)}%</b>',
        f'📊 RSI: <b>{d.get("rsi","—")}</b>',
        f'〽️ MACD: <b>{d.get("macd","—")}</b>',
        f'💪 ADX: <b>{d.get("adx","—")}</b>',
        f'🏗 Structure: <b>{d.get("structure","—")}</b>',
        '',
        f'📡 {d.get("source","verified data")}',
        f'🕐 Candle: <code>{d.get("timestamp","—")}</code>',
    ]
    if s in ('BUY','SELL','CALL','PUT'):
        lines += [f'🛑 SL: <b>{fmt_price(d.get("sl"))}</b>', f'🎯 TP1: <b>{fmt_price(d.get("tp1"))}</b>', f'🎯 TP2: <b>{fmt_price(d.get("tp2"))}</b>']
    lines += ['', f'💡 {d.get("reason", "No setup.")}']
    if auto: lines += ['', '🤖 <i>Auto Signal</i>']
    return '\n'.join(lines)


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📊 Get Signal', callback_data='menu_signal'), InlineKeyboardButton('🤖 Auto Signal', callback_data='menu_auto')],
        [InlineKeyboardButton('⚙️ Settings', callback_data='menu_settings'), InlineKeyboardButton('⏹ Stop Auto', callback_data='stop')],
    ])


def symbol_menu(prefix='sig'):
    rows=[]
    for i in range(0,len(SYMBOLS),3):
        rows.append([InlineKeyboardButton(x, callback_data=f'{prefix}_sym_{x}') for x in SYMBOLS[i:i+3]])
    rows.append([InlineKeyboardButton('⬅️ Back', callback_data='home')])
    return InlineKeyboardMarkup(rows)


def tf_menu(prefix='sig', symbol=None):
    rows=[]
    for i in range(0,len(TFS),3):
        rows.append([InlineKeyboardButton(x, callback_data=f'{prefix}_tf_{symbol}_{x}') for x in TFS[i:i+3]])
    rows.append([InlineKeyboardButton('⬅️ Back', callback_data='menu_signal')])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🚀 <b>TradePilot Telegram</b>\n\nLive market analysis with verified backend data.\n\nChoose an option:',
        parse_mode='HTML', reply_markup=main_menu())


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('📊 Select symbol:', reply_markup=symbol_menu('sig'))


async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 Select symbol for Auto Signal:', reply_markup=symbol_menu('auto'))


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_for_chat(update.effective_chat.id)
    await update.message.reply_text('⏹ Auto Signal stopped.', reply_markup=main_menu())


async def stop_for_chat(chat_id):
    with lock:
        sub = subscriptions.pop(chat_id, None)
    if sub and sub.get('task'):
        sub['task'].cancel()


async def auto_loop(application, chat_id, symbol, tf):
    last_candle = None
    while True:
        try:
            data = await asyncio.to_thread(api_market, symbol, tf)
            candle = data.get('timestamp')
            if candle and candle != last_candle:
                last_candle = candle
                if not data.get('error'):
                    await application.bot.send_message(chat_id, signal_text(data, symbol, tf, True), parse_mode='HTML')
                elif data.get('error') == 'market_closed':
                    await application.bot.send_message(chat_id, signal_text(data, symbol, tf, True), parse_mode='HTML')
            await asyncio.sleep(max(POLL_SECONDS, 15))
        except asyncio.CancelledError:
            break
        except Exception as e:
            await asyncio.sleep(max(POLL_SECONDS, 20))


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat_id = q.message.chat_id
    if data == 'home':
        await q.edit_message_text('🚀 <b>TradePilot</b>\n\nChoose an option:', parse_mode='HTML', reply_markup=main_menu()); return
    if data == 'menu_signal':
        await q.edit_message_text('📊 Select symbol:', reply_markup=symbol_menu('sig')); return
    if data == 'menu_auto':
        await q.edit_message_text('🤖 Select symbol for Auto Signal:', reply_markup=symbol_menu('auto')); return
    if data == 'menu_settings':
        await q.edit_message_text(f'⚙️ <b>Current settings</b>\n\nAuto polling: {POLL_SECONDS}s\nBackend: {BACKEND_URL}', parse_mode='HTML', reply_markup=main_menu()); return
    if data == 'stop':
        await stop_for_chat(chat_id)
        await q.edit_message_text('⏹ Auto Signal stopped.', reply_markup=main_menu()); return
    parts=data.split('_')
    if len(parts) >= 3 and parts[1] == 'sym':
        mode, symbol = parts[0], parts[2]
        await q.edit_message_text(f'{symbol} selected. Choose timeframe:', reply_markup=tf_menu(mode, symbol)); return
    if len(parts) >= 4 and parts[1] == 'tf':
        mode, symbol, tf = parts[0], parts[2], parts[3]
        if mode == 'sig':
            await q.edit_message_text('⏳ Checking verified live market…')
            try:
                d = await asyncio.to_thread(api_market, symbol, tf)
                await q.edit_message_text(signal_text(d, symbol, tf), parse_mode='HTML', reply_markup=main_menu())
            except Exception as e:
                await q.edit_message_text(f'❌ Backend error: <code>{e}</code>', parse_mode='HTML', reply_markup=main_menu())
        else:
            await stop_for_chat(chat_id)
            task = asyncio.create_task(auto_loop(context.application, chat_id, symbol, tf))
            with lock:
                subscriptions[chat_id] = {'symbol': symbol, 'tf': tf, 'task': task, 'enabled': True}
            await q.edit_message_text(f'🤖 <b>Auto Signal ON</b>\n\nSymbol: <b>{symbol}</b>\nTimeframe: <b>{tf}</b>\n\nThe bot will analyze new candles automatically.\nUse /stop to stop it.', parse_mode='HTML', reply_markup=main_menu()); return


@app.get('/')
def root(): return jsonify({'service':'TradePilot Telegram Bot','status':'ok'})

@app.get('/health')
def health(): return jsonify({'status':'ok','subscriptions':len(subscriptions),'time':datetime.now(timezone.utc).isoformat()})


def run_web():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT','10000')))


def main():
    if not BOT_TOKEN:
        raise SystemExit('TELEGRAM_BOT_TOKEN is not set')
    threading.Thread(target=run_web, daemon=True).start()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('signal', signal_cmd))
    application.add_handler(CommandHandler('auto', auto_cmd))
    application.add_handler(CommandHandler('stop', stop_cmd))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
