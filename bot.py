import os, time, threading, asyncio, logging
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

BINANCE_URL = "https://api.binance.com/api/v3/klines"
TWELVE_URL = "https://api.twelvedata.com/time_series"

SYMBOLS = {
    "crypto": {"BTCUSD":"BTCUSDT", "ETHUSD":"ETHUSDT"},
    "forex": {
        "EURUSD":"EUR/USD","GBPUSD":"GBP/USD","USDJPY":"USD/JPY",
        "USDCHF":"USD/CHF","USDCAD":"USD/CAD","AUDUSD":"AUD/USD",
        "NZDUSD":"NZD/USD","XAUUSD":"XAU/USD"
    }
}
TFS = {"1m":"1min","5m":"5min","15m":"15min","1h":"1h","4h":"4h"}
BINANCE_TF = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h","4h":"4h"}

# Central cache: one market request can serve many Telegram users.
CACHE_TTL = {"1m":20, "5m":45, "15m":90, "1h":180, "4h":300}
cache = {}
cache_lock = threading.Lock()
user_auto = {}  # chat_id -> {"symbol":..., "tf":..., "last_candle":...}

def utcnow():
    return datetime.now(timezone.utc)

def market_open(symbol):
    if symbol in SYMBOLS["crypto"]:
        return True
    d=utcnow()
    day=d.weekday()
    mins=d.hour*60+d.minute
    return not (day==5 and mins>=1260 or day==6 or day==0 and mins<1260)

def http_json(url, params, timeout=10):
    r=requests.get(url, params=params, timeout=timeout,
                   headers={"User-Agent":"TradePilot-Live/1.0"})
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    data=r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message","provider error"))
    return data

def fetch_crypto(symbol, tf, limit=180):
    data=http_json(BINANCE_URL, {"symbol":SYMBOLS["crypto"][symbol],
                                 "interval":BINANCE_TF[tf],"limit":limit})
    if not isinstance(data,list) or len(data)<60:
        raise RuntimeError("INSUFFICIENT_DATA")
    return [{"time":int(x[0]),"open":float(x[1]),"high":float(x[2]),
             "low":float(x[3]),"close":float(x[4]),"volume":float(x[5])} for x in data], "Binance Spot"

def fetch_forex(symbol, tf, limit=180):
    if not TWELVE_KEY:
        raise RuntimeError("NO_TWELVE_KEY")
    data=http_json(TWELVE_URL, {"symbol":SYMBOLS["forex"][symbol],
                                "interval":TFS[tf],"outputsize":limit,
                                "apikey":TWELVE_KEY,"timezone":"UTC","format":"JSON"})
    vals=data.get("values")
    if not vals or len(vals)<60:
        raise RuntimeError("INSUFFICIENT_DATA")
    vals=list(reversed(vals))
    out=[]
    for x in vals:
        s=x["datetime"]
        dt=datetime.fromisoformat(s.replace("Z","+00:00"))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        out.append({"time":int(dt.timestamp()*1000),"open":float(x["open"]),
                    "high":float(x["high"]),"low":float(x["low"]),
                    "close":float(x["close"]),"volume":float(x.get("volume") or 0)})
    return out, "Twelve Data"

def get_candles(symbol, tf):
    key=(symbol,tf); now=time.time()
    with cache_lock:
        item=cache.get(key)
        if item and now-item["fetched"] < CACHE_TTL[tf]:
            return item["candles"], item["source"], False
    try:
        if symbol in SYMBOLS["crypto"]:
            candles,source=fetch_crypto(symbol,tf)
        else:
            candles,source=fetch_forex(symbol,tf)
        with cache_lock:
            cache[key]={"fetched":time.time(),"candles":candles,"source":source}
        return candles,source,True
    except Exception as e:
        # Never turn stale data into a fresh signal. Cached data can only be
        # used for display if it is still inside its candle-specific age.
        with cache_lock:
            item=cache.get(key)
        if item:
            return item["candles"], item["source"], False
        raise

def ema(vals,n):
    if len(vals)<n:return [None]*len(vals)
    out=[None]*len(vals); k=2/(n+1); out[n-1]=sum(vals[:n])/n
    for i in range(n,len(vals)): out[i]=vals[i]*k+out[i-1]*(1-k)
    return out

def rsi(vals,n=14):
    if len(vals)<n+1:return None
    gains=[]; losses=[]
    for i in range(1,len(vals)):
        d=vals[i]-vals[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    g=sum(gains[:n])/n; l=sum(losses[:n])/n
    for i in range(n,len(gains)):
        g=(g*(n-1)+gains[i])/n; l=(l*(n-1)+losses[i])/n
    return 100 if l==0 else 100-100/(1+g/l)

def atr(c,n=14):
    if len(c)<n+1:return None
    tr=[]
    for i in range(1,len(c)):
        tr.append(max(c[i]["high"]-c[i]["low"],
                      abs(c[i]["high"]-c[i-1]["close"]),
                      abs(c[i]["low"]-c[i-1]["close"])))
    return sum(tr[-n:])/n

def adx(c,n=14):
    if len(c)<n*2+1:return None
    trs=[]; plus=[]; minus=[]
    for i in range(1,len(c)):
        up=c[i]["high"]-c[i-1]["high"]; down=c[i-1]["low"]-c[i]["low"]
        trs.append(max(c[i]["high"]-c[i]["low"],abs(c[i]["high"]-c[i-1]["close"]),abs(c[i]["low"]-c[i-1]["close"])))
        plus.append(up if up>down and up>0 else 0); minus.append(down if down>up and down>0 else 0)
    a=sum(trs[:n])/n;p=sum(plus[:n])/n;m=sum(minus[:n])/n; dx=[]
    for i in range(n,len(trs)):
        a=(a*(n-1)+trs[i])/n;p=(p*(n-1)+plus[i])/n;m=(m*(n-1)+minus[i])/n
        di1=100*p/max(a,1e-12); di2=100*m/max(a,1e-12)
        dx.append(100*abs(di1-di2)/max(di1+di2,1e-12))
    return sum(dx[-n:])/min(n,len(dx)) if dx else None

def analyze(c,tf):
    # Exclude the still-forming candle for signal confirmation when possible.
    work=c[:-1] if len(c)>70 else c
    closes=[x["close"] for x in work]
    e20=ema(closes,20)[-1]; e50=ema(closes,50)[-1]
    r=rsi(closes); a=adx(work); at=atr(work)
    e12=ema(closes,12); e26=ema(closes,26)
    macd=(e12[-1]-e26[-1]) if e12[-1] is not None and e26[-1] is not None else None
    macd_prev=(e12[-2]-e26[-2]) if e12[-2] is not None and e26[-2] is not None else None
    last=work[-1]; prev=work[-2]
    bull=e20 is not None and e50 is not None and e20>e50 and last["close"]>e20
    bear=e20 is not None and e50 is not None and e20<e50 and last["close"]<e20
    bullish_candle=last["close"]>last["open"] and last["close"]>=prev["close"]
    bearish_candle=last["close"]<last["open"] and last["close"]<=prev["close"]
    bull_score=sum([bull, r is not None and r>50, macd is not None and macd>0 and (macd_prev is None or macd>=macd_prev),
                    a is not None and a>=18, bullish_candle])
    bear_score=sum([bear, r is not None and r<50, macd is not None and macd<0 and (macd_prev is None or macd<=macd_prev),
                    a is not None and a>=18, bearish_candle])
    if bull_score>=4 and not bear:
        sig="BUY"; conf=60+8*min(bull_score-4,3)
    elif bear_score>=4 and not bull:
        sig="SELL"; conf=60+8*min(bear_score-4,3)
    else:
        sig="WAIT"; conf=max(bull_score,bear_score)*15
    # Explicit trend protection: never emit against trend.
    if sig=="BUY" and not bull: sig="WAIT"
    if sig=="SELL" and not bear: sig="WAIT"
    return {
        "signal":sig,"confidence":min(90,int(conf)),
        "rsi":None if r is None else round(r,2),
        "adx":None if a is None else round(a,2),
        "trend":"BULLISH" if bull else "BEARISH" if bear else "NEUTRAL",
        "entry":last["close"],"candleTime":last["time"],
        "reason":("Trend + momentum + confirmation aligned." if sig!="WAIT" else "Conditions are not sufficiently aligned; signal locked to WAIT.")
    }

def get_signal(symbol,tf,binary=False):
    if symbol not in SYMBOLS["crypto"] and symbol not in SYMBOLS["forex"]:
        raise RuntimeError("UNSUPPORTED_SYMBOL")
    if tf not in TFS: raise RuntimeError("UNSUPPORTED_TIMEFRAME")
    if not market_open(symbol): return {"error":"market_closed"}
    candles,source,_=get_candles(symbol,tf)
    d=analyze(candles,tf)
    now_ms=int(time.time()*1000)
    age=now_ms-d["candleTime"]
    # Timeframe-aware freshness. Crypto and FX differ only in provider latency.
    max_age={"1m":180000,"5m":420000,"15m":1000000,"1h":3700000,"4h":14500000}[tf]
    if age<0 or age>max_age:
        return {"error":"data_stale","dataAgeMs":age}
    d.update({"source":source+" • verified live candles","dataAgeMs":age,
              "timestamp":datetime.fromtimestamp(d["candleTime"]/1000,timezone.utc).isoformat()})
    if binary:
        d["signal"]={"BUY":"CALL","SELL":"PUT","WAIT":"WAIT"}[d["signal"]]
    return d

# ---------------- Flask health/API ----------------
app=Flask(__name__)

@app.get("/")
def root(): return jsonify({"service":"TradePilot Telegram","status":"ok"})

@app.get("/health")
def health(): return jsonify({"status":"ok","service":"TradePilot","time":utcnow().isoformat()})

@app.get("/market")
def market():
    symbol=os.getenv("QUERY_SYMBOL","")
    return jsonify({"error":"Use Telegram bot commands; public market endpoint is intentionally disabled."}), 404

# ---------------- Telegram ----------------
async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton("📊 Get Signal",callback_data="signal")],
        [InlineKeyboardButton("⚙️ Auto Signal",callback_data="auto")],
        [InlineKeyboardButton("⛔ Stop Auto",callback_data="stop")]]
    await update.message.reply_text("🚀 TradePilot Live\n\nLive-data signal bot. No verified data = no signal.\n\nChoose an option:",
                                    reply_markup=InlineKeyboardMarkup(kb))

def symbol_kb():
    syms=list(SYMBOLS["crypto"])+list(SYMBOLS["forex"])
    return InlineKeyboardMarkup([[InlineKeyboardButton(s,callback_data=f"s:{s}") for s in syms[i:i+2]] for i in range(0,len(syms),2)])

def tf_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton(x,callback_data=f"t:{x}") for x in ["1m","5m","15m"]],
                                 [InlineKeyboardButton(x,callback_data=f"t:{x}") for x in ["1h","4h"]]])

def fmt_signal(s, symbol, tf):
    if "error" in s:
        m={"market_closed":"🔴 MARKET CLOSED","data_stale":"🟡 DATA STALE","RATE_LIMIT":"🟡 DATA PROVIDER RATE-LIMITED"}
        return f"{m.get(s['error'],'🔴 NO LIVE DATA')}\n\n{symbol} • {tf}\nNo new verified signal was generated."
    sig=s["signal"]
    icon="🟢" if sig in ("BUY","CALL") else "🔴" if sig in ("SELL","PUT") else "🟡"
    return (f"{icon} {sig}\n\n"
            f"📊 {symbol} • {tf}\n"
            f"📡 {s['source']}\n"
            f"⏱ Data age: {max(0,int(s['dataAgeMs']/1000))}s\n"
            f"📈 Trend: {s['trend']}\n"
            f"RSI: {s['rsi'] if s['rsi'] is not None else '—'}\n"
            f"ADX: {s['adx'] if s['adx'] is not None else '—'}\n"
            f"Confidence: {s['confidence']}%\n"
            f"💰 Entry: {s['entry']}\n\n"
            f"📝 {s['reason']}\n\n"
            f"⚠️ Rule-based analysis, not a profit guarantee.")

async def signal_cmd(update,context):
    await update.message.reply_text("Choose symbol:",reply_markup=symbol_kb())

async def auto_cmd(update,context):
    await update.message.reply_text("Choose symbol for Auto Signal:",reply_markup=symbol_kb())

async def stop_cmd(update,context):
    user_auto.pop(update.effective_chat.id,None)
    await update.message.reply_text("⛔ Auto Signal stopped.")

async def button(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    chat=q.message.chat_id
    data=q.data
    if data=="signal":
        context.user_data["mode"]="once"; await q.edit_message_text("Choose symbol:",reply_markup=symbol_kb()); return
    if data=="auto":
        context.user_data["mode"]="auto"; await q.edit_message_text("Choose symbol for Auto Signal:",reply_markup=symbol_kb()); return
    if data=="stop":
        user_auto.pop(chat,None); await q.edit_message_text("⛔ Auto Signal stopped."); return
    if data.startswith("s:"):
        context.user_data["symbol"]=data[2:]
        await q.edit_message_text(f"Symbol: {data[2:]}\nChoose timeframe:",reply_markup=tf_kb()); return
    if data.startswith("t:"):
        symbol=context.user_data.get("symbol"); tf=data[2:]; mode=context.user_data.get("mode","once")
        if not symbol: await q.edit_message_text("Please choose symbol again with /signal."); return
        if mode=="auto":
            user_auto[chat]={"symbol":symbol,"tf":tf,"last_candle":None}
            await q.edit_message_text(f"🟢 Auto Signal ON\n{symbol} • {tf}\n\nWaiting for the next verified candle.")
        else:
            try:
                s=get_signal(symbol,tf,binary=False)
                await q.edit_message_text(fmt_signal(s,symbol,tf))
            except Exception as e:
                await q.edit_message_text(f"🟡 NO LIVE DATA\n\n{symbol} • {tf}\n{str(e)}")

async def auto_job(context:ContextTypes.DEFAULT_TYPE):
    for chat, cfg in list(user_auto.items()):
        try:
            s=get_signal(cfg["symbol"],cfg["tf"],binary=False)
            if "error" in s: continue
            candle=s["candleTime"]
            if cfg["last_candle"] == candle: continue
            cfg["last_candle"]=candle
            await context.bot.send_message(chat_id=chat,text=fmt_signal(s,cfg["symbol"],cfg["tf"]))
        except Exception as e:
            logging.warning("auto %s: %s",chat,e)

async def run_bot():
    application=Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start",start))
    application.add_handler(CommandHandler("signal",signal_cmd))
    application.add_handler(CommandHandler("auto",auto_cmd))
    application.add_handler(CommandHandler("stop",stop_cmd))
    application.add_handler(CallbackQueryHandler(button))
    application.job_queue.run_repeating(auto_job, interval=20, first=10)
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logging.info("Telegram polling started")
    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    # Flask health endpoint in daemon thread; Telegram owns the main asyncio loop.
    threading.Thread(target=lambda: app.run(host="0.0.0.0",port=PORT,debug=False,use_reloader=False),
                     daemon=True).start()
    asyncio.run(run_bot())

if __name__=="__main__":
    main()
