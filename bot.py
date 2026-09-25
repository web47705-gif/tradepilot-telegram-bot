import os, time, asyncio, logging, threading
from datetime import datetime, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TWELVE_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
PORT = int(os.getenv("PORT", "10000"))

# Public providers are used first so the bot does not depend on Binance.
COINBASE_URL = "https://api.exchange.coinbase.com/products/{}/candles"
TWELVE_URL = "https://api.twelvedata.com/time_series"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

CRYPTO = {"BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD"}
FOREX = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X", "USDCAD": "USDCAD=X", "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X", "XAUUSD": "XAUUSD=X"
}
TWELVE_SYMBOLS = {
    "EURUSD":"EUR/USD","GBPUSD":"GBP/USD","USDJPY":"USD/JPY",
    "USDCHF":"USD/CHF","USDCAD":"USD/CAD","AUDUSD":"AUD/USD",
    "NZDUSD":"NZD/USD","XAUUSD":"XAU/USD"
}
TF_SECONDS = {"1m":60, "5m":300, "15m":900, "1h":3600, "4h":14400}
TWELVE_TF = {"1m":"1min","5m":"5min","15m":"15min","1h":"1h","4h":"4h"}

# One provider request can serve all Telegram users.
CACHE_TTL = {"1m":45, "5m":120, "15m":240, "1h":600, "4h":1200}
cache = {}
cache_lock = threading.Lock()
user_auto = {}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent":"TradePilot/6.0"})

def utcnow():
    return datetime.now(timezone.utc)

def market_open(symbol):
    if symbol in CRYPTO:
        return True
    d = utcnow()
    day = d.weekday()
    mins = d.hour * 60 + d.minute
    return not (day == 5 and mins >= 1260 or day == 6 or (day == 0 and mins < 1260))

def request_json(url, params=None, timeout=12):
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 451):
                raise RuntimeError(f"PROVIDER_HTTP_{r.status_code}")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("status") == "error":
                raise RuntimeError(data.get("message", "provider error"))
            return data
        except Exception as e:
            last = e
            time.sleep(0.7 * (attempt + 1))
    raise last

def normalize(rows):
    out = []
    for x in rows:
        try:
            out.append({
                "time": int(x["time"]),
                "open": float(x["open"]),
                "high": float(x["high"]),
                "low": float(x["low"]),
                "close": float(x["close"]),
                "volume": float(x.get("volume", 0) or 0),
            })
        except Exception:
            pass
    out.sort(key=lambda z: z["time"])
    if len(out) < 60:
        raise RuntimeError("INSUFFICIENT_DATA")
    return out[-180:]

def fetch_coinbase(symbol, tf):
    product = CRYPTO[symbol]
    gran = TF_SECONDS[tf]
    data = request_json(COINBASE_URL.format(product), {"granularity": gran})
    # Coinbase: [time, low, high, open, close, volume]
    rows = [{"time":int(x[0])*1000, "low":x[1], "high":x[2],
             "open":x[3], "close":x[4], "volume":x[5]} for x in data]
    return normalize(rows), "Coinbase"

def fetch_twelve(symbol, tf):
    if not TWELVE_KEY:
        raise RuntimeError("NO_TWELVE_KEY")
    data = request_json(TWELVE_URL, {
        "symbol": TWELVE_SYMBOLS[symbol],
        "interval": TWELVE_TF[tf],
        "outputsize": 180,
        "apikey": TWELVE_KEY,
        "timezone": "UTC",
        "format": "JSON",
    })
    vals = data.get("values")
    if not vals:
        raise RuntimeError("TWELVE_NO_DATA")
    rows = []
    for x in reversed(vals):
        dt = datetime.fromisoformat(x["datetime"].replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        rows.append({"time":int(dt.timestamp()*1000), "open":x["open"],
                     "high":x["high"], "low":x["low"], "close":x["close"],
                     "volume":x.get("volume",0) or 0})
    return normalize(rows), "Twelve Data"

def fetch_yahoo(symbol, tf):
    interval = {"1m":"1m","5m":"5m","15m":"15m","1h":"60m","4h":"1h"}[tf]
    period2 = int(time.time())
    # Yahoo limits short intervals; request a practical recent window.
    days = {"1m":5,"5m":30,"15m":60,"1h":180,"4h":365}[tf]
    period1 = period2 - days*86400
    data = request_json(YAHOO_URL.format(FOREX[symbol]), {
        "period1": period1, "period2": period2, "interval": interval,
        "events":"history", "includeAdjustedClose":"true"
    })
    res = data.get("chart",{}).get("result")
    if not res:
        raise RuntimeError("YAHOO_NO_DATA")
    r = res[0]
    ts = r.get("timestamp",[])
    q = r.get("indicators",{}).get("quote",[{}])[0]
    rows=[]
    for i,t in enumerate(ts):
        if q.get("open",[None]*len(ts))[i] is None:
            continue
        rows.append({"time":int(t)*1000, "open":q["open"][i],
                     "high":q["high"][i], "low":q["low"][i],
                     "close":q["close"][i], "volume":(q.get("volume",[0]*len(ts))[i] or 0)})
    return normalize(rows), "Yahoo Finance"

def get_candles(symbol, tf):
    key=(symbol,tf)
    now=time.time()
    with cache_lock:
        item=cache.get(key)
        if item and now-item["fetched"] < CACHE_TTL[tf]:
            return item["candles"], item["source"]

    errors=[]
    providers = []
    if symbol in CRYPTO:
        providers = [fetch_coinbase]
    else:
        # Twelve Data if configured, otherwise Yahoo; Yahoo is also fallback.
        providers = ([fetch_twelve, fetch_yahoo] if TWELVE_KEY else [fetch_yahoo])

    for provider in providers:
        try:
            candles, source = provider(symbol,tf)
            with cache_lock:
                cache[key]={"fetched":time.time(),"candles":candles,"source":source}
            return candles, source
        except Exception as e:
            errors.append(str(e))
            logging.warning("%s %s %s: %s", provider.__name__, symbol, tf, e)

    # A recent cache is preferable to crashing the bot, but it will be marked stale.
    with cache_lock:
        item=cache.get(key)
    if item:
        return item["candles"], item["source"]
    raise RuntimeError(" / ".join(errors) if errors else "NO_LIVE_DATA")

def ema(values, n):
    if len(values)<n: return [None]*len(values)
    out=[None]*len(values); k=2/(n+1); out[n-1]=sum(values[:n])/n
    for i in range(n,len(values)):
        out[i]=values[i]*k+out[i-1]*(1-k)
    return out

def rsi(values, n=14):
    if len(values)<n+1: return None
    gains=[]; losses=[]
    for i in range(1,len(values)):
        d=values[i]-values[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    g=sum(gains[:n])/n; l=sum(losses[:n])/n
    for i in range(n,len(gains)):
        g=(g*(n-1)+gains[i])/n; l=(l*(n-1)+losses[i])/n
    return 100 if l==0 else 100-100/(1+g/l)

def atr(c,n=14):
    if len(c)<n+1: return None
    tr=[]
    for i in range(1,len(c)):
        tr.append(max(c[i]["high"]-c[i]["low"],
                      abs(c[i]["high"]-c[i-1]["close"]),
                      abs(c[i]["low"]-c[i-1]["close"])))
    return sum(tr[-n:])/n

def analyze(c):
    closes=[x["close"] for x in c]
    e20=ema(closes,20); e50=ema(closes,50)
    r=rsi(closes,14); a=atr(c,14)
    last=c[-1]
    bull=e20[-1] is not None and e50[-1] is not None and e20[-1]>e50[-1]
    bear=e20[-1] is not None and e50[-1] is not None and e20[-1]<e50[-1]
    prev=c[-2]
    bullish_candle=last["close"]>last["open"] and last["close"]>prev["high"]
    bearish_candle=last["close"]<last["open"] and last["close"]<prev["low"]
    bull_score=sum([
        bull,
        r is not None and r>=55,
        bullish_candle,
        a is not None and (last["high"]-last["low"])>=a*0.5
    ])
    bear_score=sum([
        bear,
        r is not None and r<=45,
        bearish_candle,
        a is not None and (last["high"]-last["low"])>=a*0.5
    ])
    if bull_score>=3 and not bear_score>=3:
        sig="BUY"
    elif bear_score>=3 and not bull_score>=3:
        sig="SELL"
    else:
        sig="WAIT"
    conf=50+max(bull_score,bear_score)*10 if sig!="WAIT" else 40+max(bull_score,bear_score)*5
    return {"signal":sig, "confidence":min(90,conf),
            "rsi":None if r is None else round(r,2),
            "trend":"BULLISH" if bull else "BEARISH" if bear else "NEUTRAL",
            "entry":round(last["close"],6), "candleTime":last["time"],
            "reason":"Trend + momentum + candle confirmation aligned."
                    if sig!="WAIT" else "Conditions are not sufficiently aligned."}

def get_signal(symbol,tf):
    if symbol not in CRYPTO and symbol not in FOREX:
        raise RuntimeError("UNSUPPORTED_SYMBOL")
    if tf not in TF_SECONDS:
        raise RuntimeError("UNSUPPORTED_TIMEFRAME")
    if not market_open(symbol):
        return {"error":"MARKET_CLOSED"}
    candles,source=get_candles(symbol,tf)
    d=analyze(candles)
    age=int(time.time()*1000)-d["candleTime"]
    max_age={"1m":180000,"5m":420000,"15m":1000000,"1h":3700000,"4h":14500000}[tf]
    if age<0 or age>max_age:
        return {"error":"DATA_STALE","dataAgeMs":age,"source":source}
    d.update({"source":source,"dataAgeMs":age})
    return d

app=Flask(__name__)
@app.get("/")
def root():
    return jsonify({"service":"TradePilot Telegram","status":"ok"})
@app.get("/health")
def health():
    return jsonify({"status":"ok","service":"TradePilot","time":utcnow().isoformat()})

def symbols_kb():
    syms=list(CRYPTO)+list(FOREX)
    return InlineKeyboardMarkup([[InlineKeyboardButton(s,callback_data=f"s:{s}") for s in syms[i:i+2]]
                                for i in range(0,len(syms),2)])

def tf_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1m",callback_data="t:1m"),InlineKeyboardButton("5m",callback_data="t:5m"),
         InlineKeyboardButton("15m",callback_data="t:15m")],
        [InlineKeyboardButton("1h",callback_data="t:1h"),InlineKeyboardButton("4h",callback_data="t:4h")]
    ])

def fmt(s,symbol,tf):
    if "error" in s:
        return f"🟡 {s['error']}\n\n{symbol} • {tf}\nNo verified signal right now."
    icon="🟢" if s["signal"]=="BUY" else "🔴" if s["signal"]=="SELL" else "🟡"
    return (f"{icon} {s['signal']}\n\n📊 {symbol} • {tf}\n"
            f"📡 {s['source']}\n⏱ Data age: {max(0,s['dataAgeMs']//1000)}s\n"
            f"📈 Trend: {s['trend']}\nRSI: {s['rsi']}\n"
            f"Confidence: {s['confidence']}%\n💰 Entry: {s['entry']}\n\n"
            f"📝 {s['reason']}\n\n⚠️ Rule-based analysis; not a profit guarantee.")

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Get Signal",callback_data="signal"),
         InlineKeyboardButton("🤖 Auto Signal",callback_data="auto")],
        [InlineKeyboardButton("⚙️ Settings",callback_data="settings"),
         InlineKeyboardButton("⛔ Stop Auto",callback_data="stop")]
    ])
    await update.message.reply_text("🚀 TradePilot Live\n\nChoose an option:",reply_markup=kb)

async def signal_cmd(update,context):
    context.user_data["mode"]="once"
    await update.message.reply_text("Choose symbol:",reply_markup=symbols_kb())

async def auto_cmd(update,context):
    context.user_data["mode"]="auto"
    await update.message.reply_text("Choose symbol for Auto Signal:",reply_markup=symbols_kb())

async def stop_cmd(update,context):
    user_auto.pop(update.effective_chat.id,None)
    await update.message.reply_text("⛔ Auto Signal stopped.")

async def button(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    chat=q.message.chat_id; data=q.data
    if data=="signal":
        context.user_data["mode"]="once"; await q.edit_message_text("Choose symbol:",reply_markup=symbols_kb()); return
    if data=="auto":
        context.user_data["mode"]="auto"; await q.edit_message_text("Choose symbol:",reply_markup=symbols_kb()); return
    if data=="stop":
        user_auto.pop(chat,None); await q.edit_message_text("⛔ Auto Signal stopped."); return
    if data=="settings":
        await q.edit_message_text("Current default: live market data, 1m/5m/15m/1h/4h, automatic provider fallback."); return
    if data.startswith("s:"):
        context.user_data["symbol"]=data[2:]
        await q.edit_message_text(f"Symbol: {data[2:]}\nChoose timeframe:",reply_markup=tf_kb()); return
    if data.startswith("t:"):
        symbol=context.user_data.get("symbol"); tf=data[2:]; mode=context.user_data.get("mode","once")
        if not symbol:
            await q.edit_message_text("Please choose symbol again."); return
        if mode=="auto":
            user_auto[chat]={"symbol":symbol,"tf":tf,"last_candle":None}
            await q.edit_message_text(f"🟢 Auto Signal ON\n{symbol} • {tf}\n\nChecking each new candle.")
        else:
            try:
                await q.edit_message_text("⏳ Checking live market data...")
                s=get_signal(symbol,tf)
                await q.edit_message_text(fmt(s,symbol,tf))
            except Exception as e:
                await q.edit_message_text(f"🟡 NO LIVE DATA\n\n{symbol} • {tf}\n{e}")

async def auto_job(context):
    for chat,cfg in list(user_auto.items()):
        try:
            s=get_signal(cfg["symbol"],cfg["tf"])
            if "error" in s: continue
            candle=s["candleTime"]
            if cfg["last_candle"]==candle: continue
            cfg["last_candle"]=candle
            await context.bot.send_message(chat_id=chat,text=fmt(s,cfg["symbol"],cfg["tf"]))
        except Exception as e:
            logging.warning("auto %s: %s",chat,e)

async def run_bot():
    app_tg=Application.builder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start",start))
    app_tg.add_handler(CommandHandler("signal",signal_cmd))
    app_tg.add_handler(CommandHandler("auto",auto_cmd))
    app_tg.add_handler(CommandHandler("stop",stop_cmd))
    app_tg.add_handler(CallbackQueryHandler(button))
    app_tg.job_queue.run_repeating(auto_job, interval=30, first=5)
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(drop_pending_updates=True)
    logging.info("Telegram polling started")
    try:
        await asyncio.Event().wait()
    finally:
        await app_tg.updater.stop()
        await app_tg.stop()
        await app_tg.shutdown()

def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    threading.Thread(target=lambda: app.run(host="0.0.0.0",port=PORT,debug=False,use_reloader=False),
                     daemon=True).start()
    asyncio.run(run_bot())

if __name__=="__main__":
    main()
