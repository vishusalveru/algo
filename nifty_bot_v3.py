"""
=============================================================
  Nifty 50 FVG Scalping Bot v3 — COMPLETE
  ─────────────────────────────────────────────
  PATCHES (same as SPY v3 + Nifty specific):
  #1  ORB breakout detected AT formation
  #2  Trend relaxed 3/4 candles
  #3  FVG body filter Nifty scale (15pts)
  #4  Candle cache cleared every scan
  #5  Telegram gated — no alerts outside hours
  #6  PCR refresh every 30min
  #7  RVOL filter min 1.5x
  #8  OBV direction confirmation
  #9  VWAP bands +-1SD +-2SD
  #10 Multi-timeframe trend 5m+15m+30m
  #11 Auto-bias from Upstox PCR + GIFT Nifty
  #12 Bias reminder at 9:00 AM IST
  #13 35+ scan columns
  #14 EMA 9/21/50
  #15 Daily CSV auto-send
  #16 nifty_auto_bias integrated — reversal check before every trade
  #17 Previous day High/Low/Close S&R levels
  #18 Breakaway gap detection
  #19 Strong FVG → trailing SL
  #20 Daily loss/profit limits
=============================================================
"""

import time
import logging
import datetime
import csv
import os
import threading
import requests
import numpy as np
import pandas as pd
import pytz
from bs4 import BeautifulSoup
import upstox_client
import config
from nifty_auto_bias import (
    get_combined_bias_nifty,
    pre_trade_check_nifty,
    format_bias_message_nifty,
    format_reversal_alert_nifty
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("nifty_v3.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  PARAMETERS
# ─────────────────────────────────────────────
SL_POINTS           = 10
TARGET_POINTS       = 8
TRAIL_DISTANCE      = 10
TRAIL_START         = 15
STRONG_FVG_GAP      = 20
STRONG_FVG_BODY     = 30
MIN_FVG_BODY        = 15
BREAKAWAY_GAP_OPEN  = 50
BREAKAWAY_GAP_INTRA = 30
ORB_END_TIME        = datetime.time(9, 45)
MAX_TRADES          = 15
CAPITAL_PER_TRADE   = 6500
DAILY_LOSS_LIMIT    = 3000
DAILY_PROFIT_TARGET = 2000
LOT_SIZE            = 65
OTM_OFFSET          = 100
MIN_RVOL            = 1.5
NIFTY_KEY           = "NSE_INDEX|Nifty 50"

IST = pytz.timezone("Asia/Kolkata")
def now_ist(): return datetime.datetime.now(IST)
def ist_time(): return now_ist().time()

TRADE_START  = datetime.time(9, 30)
TRADE_END    = datetime.time(14, 30)
EXPIRY_STOP  = datetime.time(13, 0)
REMINDER_TIME = datetime.time(9, 0)

# ─────────────────────────────────────────────
#  UPSTOX CLIENT
# ─────────────────────────────────────────────
def get_upstox_client():
    cfg = upstox_client.Configuration()
    cfg.access_token = config.LIVE_TOKEN
    return upstox_client.ApiClient(cfg)

def get_headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.LIVE_TOKEN}"
    }

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id": config.CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            log.warning(f"TG failed: {resp.text}")
    except Exception as e:
        log.error(f"TG error: {e}")

def tg(icon, title, lines):
    body = "\n".join([f"  {l}" for l in lines])
    send_telegram(f"{icon} <b>{title}</b>\n{body}")
    log.info(f"[TG] {title}")

def send_csv_files():
    files = [
        ("scan_log_v3.csv",  "Nifty Scan Log v3"),
        ("trade_log_v3.csv", "Nifty Trade Log v3"),
    ]
    send_telegram("📊 <b>Nifty Daily CSV Report</b>")
    sent = 0
    for fname, caption in files:
        path = f"/home/salverukrishna83/algo-trading/{fname}"
        if not os.path.exists(path): continue
        try:
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
            with open(path, "rb") as f:
                resp = requests.post(url, data={
                    "chat_id": config.CHAT_ID, "caption": caption
                }, files={"document": f}, timeout=30)
            if resp.json().get("ok"): sent+=1
        except Exception as e:
            log.error(f"File send: {e}")
    send_telegram(f"Sent {sent}/{len(files)} files")

# ─────────────────────────────────────────────
#  TELEGRAM LISTENER
# ─────────────────────────────────────────────
class TelegramListener:
    def __init__(self):
        self.bias = "neutral"
        self.last_update_id = 0
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()
        log.info("TG listener started")

    def _poll(self):
        while self._running:
            try:
                url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
                resp = requests.get(url, params={
                    "offset": self.last_update_id+1, "timeout": 30
                }, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5); continue
                for update in resp.json().get("result", []):
                    self.last_update_id = update["update_id"]
                    text = update.get("message",{}).get("text","").strip().lower()
                    if text.startswith("/bias"):
                        parts = text.split()
                        if len(parts)>=2 and parts[1] in ["bullish","bearish","neutral"]:
                            self.bias = parts[1]
                            send_telegram(f"FII/DII Bias: {self.bias.upper()}")
                    elif text == "/status":
                        send_telegram(
                            f"Nifty Bot v3\n"
                            f"Running: YES\n"
                            f"Bias: {self.bias.upper()}\n"
                            f"IST: {now_ist().strftime('%H:%M:%S')}"
                        )
                    elif text == "/report":
                        send_csv_files()
                    elif text == "/help":
                        send_telegram(
                            "Nifty Bot v3 Commands:\n"
                            "/bias bullish\n/bias bearish\n/bias neutral\n"
                            "/status\n/report"
                        )
            except Exception as e:
                log.error(f"TG poll: {e}")
                time.sleep(5)

# ─────────────────────────────────────────────
#  MARKET DATA — Upstox Live API
# ─────────────────────────────────────────────
def get_nifty_ltp():
    try:
        resp = requests.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            headers=get_headers(),
            params={"instrument_key": NIFTY_KEY},
            timeout=5
        )
        data = resp.json()
        if data["status"] == "success":
            key = list(data["data"].keys())[0]
            return float(data["data"][key]["last_price"])
        return None
    except Exception as e:
        log.error(f"LTP: {e}")
        return None

def get_candles(interval_val=5):
    try:
        url  = (f"https://api.upstox.com/v3/historical-candle/intraday/"
                f"{NIFTY_KEY}/minutes/{interval_val}")
        resp = requests.get(url, headers=get_headers(), timeout=10)
        data = resp.json()
        if data["status"] != "success": return None
        candles = data["data"]["candles"]
        if not candles: return None
        df = pd.DataFrame(candles, columns=[
            "timestamp","open","high","low","close","volume","oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        log.info(f"Fresh {len(df)} candles [{interval_val}min]")
        return df
    except Exception as e:
        log.error(f"Candle: {e}")
        return None

def get_prev_day_ohlc():
    try:
        today   = datetime.date.today()
        from_dt = (today - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        to_dt   = today.strftime("%Y-%m-%d")
        url     = (f"https://api.upstox.com/v3/historical-candle/"
                   f"{NIFTY_KEY}/days/1/{to_dt}/{from_dt}")
        resp    = requests.get(url, headers=get_headers(), timeout=10)
        data    = resp.json()
        if data["status"] != "success" or not data["data"]["candles"]:
            return None
        candles = data["data"]["candles"]
        prev    = candles[-2] if len(candles)>=2 else candles[-1]
        return {
            "open": float(prev[1]),"high": float(prev[2]),
            "low":  float(prev[3]),"close":float(prev[4])
        }
    except Exception as e:
        log.error(f"Prev OHLC: {e}")
        return None

# PCR with 30min cache
_pcr = {"val":None,"bias":"neutral","time":None}

def get_pcr():
    global _pcr
    try:
        now = datetime.datetime.now()
        if _pcr["time"] and (now-_pcr["time"]).seconds < 1800:
            return _pcr["val"], _pcr["bias"]
        today       = datetime.date.today()
        days_to_thu = (3 - today.weekday()) % 7
        if days_to_thu == 0: days_to_thu = 7
        expiry = today + datetime.timedelta(days=days_to_thu)
        resp   = requests.get(
            "https://api.upstox.com/v2/option/chain",
            headers=get_headers(),
            params={"instrument_key":NIFTY_KEY,
                    "expiry_date":expiry.strftime("%Y-%m-%d")},
            timeout=10
        )
        data = resp.json()
        if data["status"]!="success" or not data.get("data"):
            return None, "neutral"
        pe_oi=ce_oi=0
        for r in data["data"]:
            pe = r.get("put_options",{})
            ce = r.get("call_options",{})
            if pe and pe.get("market_data"): pe_oi+=pe["market_data"].get("oi",0)
            if ce and ce.get("market_data"): ce_oi+=ce["market_data"].get("oi",0)
        if ce_oi==0: return None,"neutral"
        pcr  = round(pe_oi/ce_oi,2)
        bias = "bullish" if pcr>1.2 else "bearish" if pcr<0.8 else "neutral"
        _pcr = {"val":pcr,"bias":bias,"time":now}
        log.info(f"PCR: {pcr} → {bias}")
        return pcr, bias
    except Exception as e:
        log.error(f"PCR: {e}")
        return None,"neutral"

# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_vwap_bands(df):
    df = df.copy()
    df["typical"] = (df["high"]+df["low"]+df["close"])/3
    df["cum_tv"]  = (df["typical"]*df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"]/df["cum_vol"]
    df["cum_tv2"] = (((df["typical"]-df["vwap"])**2)*df["volume"]).cumsum()
    df["sd"]      = np.sqrt(df["cum_tv2"]/df["cum_vol"])
    df["vwap_u1"] = df["vwap"]+df["sd"]
    df["vwap_l1"] = df["vwap"]-df["sd"]
    df["vwap_u2"] = df["vwap"]+2*df["sd"]
    df["vwap_l2"] = df["vwap"]-2*df["sd"]
    return df

def calc_ema(df, periods=[9,21,50]):
    df = df.copy()
    for p in periods:
        df[f"ema{p}"] = df["close"].astype(float).ewm(span=p,adjust=False).mean()
    return df

def calc_rvol(df):
    if df is None or len(df)<5: return 1.0
    avg = float(df["volume"].mean())
    cur = float(df["volume"].iloc[-1])
    return round(cur/avg,2) if avg>0 else 1.0

def calc_obv(df):
    if df is None or len(df)<3: return "neutral"
    obv=[0]
    for i in range(1,len(df)):
        if float(df["close"].iloc[i])>float(df["close"].iloc[i-1]):
            obv.append(obv[-1]+float(df["volume"].iloc[i]))
        elif float(df["close"].iloc[i])<float(df["close"].iloc[i-1]):
            obv.append(obv[-1]-float(df["volume"].iloc[i]))
        else: obv.append(obv[-1])
    if obv[-1]>obv[-2]>obv[-3]: return "bullish"
    if obv[-1]<obv[-2]<obv[-3]: return "bearish"
    return "neutral"

def calc_cumulative_delta(df):
    if df is None or len(df)<1: return 0
    delta=0
    for _,row in df.iterrows():
        body = float(row["close"])-float(row["open"])
        vol  = float(row["volume"])
        delta += vol if body>0 else -vol if body<0 else 0
    return round(delta,0)

def detect_trend_relaxed(df, min_agree=3):
    if df is None or len(df)<4: return "neutral","Not enough",0
    recent = df.tail(4)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]
    hh = sum(1 for i in range(1,len(highs)) if highs[i]>highs[i-1])
    hl = sum(1 for i in range(1,len(lows))  if lows[i] >lows[i-1])
    ll = sum(1 for i in range(1,len(lows))  if lows[i] <lows[i-1])
    lh = sum(1 for i in range(1,len(highs)) if highs[i]<highs[i-1])
    bull=min(hh,hl); bear=min(ll,lh)
    if bull>=min_agree: return "bullish",f"HH:{hh}/3 HL:{hl}/3",bull
    if bear>=min_agree: return "bearish",f"LL:{ll}/3 LH:{lh}/3",bear
    return "neutral",f"HH:{hh} HL:{hl} LL:{ll} LH:{lh}",0

def detect_trend_multi(df5,df15,df30):
    t5,_,_ = detect_trend_relaxed(df5)
    t15,_,_= detect_trend_relaxed(df15)
    t30,_,_= detect_trend_relaxed(df30)
    bull=[t5,t15,t30].count("bullish")
    bear=[t5,t15,t30].count("bearish")
    if bull>=2: return "bullish",f"{t5}/{t15}/{t30}","strong" if bull==3 else "moderate"
    if bear>=2: return "bearish",f"{t5}/{t15}/{t30}","strong" if bear==3 else "moderate"
    return "neutral",f"{t5}/{t15}/{t30}","weak"

def detect_bos(df, trend):
    if df is None or len(df)<6: return False,0
    recent=df.tail(10); lc=float(recent["close"].iloc[-1])
    if trend=="bullish":
        sh=float(recent["high"].iloc[:-1].max())
        if lc>sh: return True,sh
    elif trend=="bearish":
        sl=float(recent["low"].iloc[:-1].min())
        if lc<sl: return True,sl
    return False,0

def detect_fvg(df):
    if df is None or len(df)<3: return None,"Not enough candles"
    candles=df.tail(15)
    for i in range(len(candles)-1,1,-1):
        c1=candles.iloc[i-2]; c2=candles.iloc[i-1]; c3=candles.iloc[i]
        body=abs(float(c2["close"])-float(c2["open"]))
        if body<MIN_FVG_BODY: continue
        c1h=float(c1["high"]); c1l=float(c1["low"])
        c3h=float(c3["high"]); c3l=float(c3["low"])
        if c1h<c3l:
            size=round(c3l-c1h,2)
            strong=size>STRONG_FVG_GAP and body>STRONG_FVG_BODY
            return {"type":"bullish","top":round(c3l,2),"bottom":round(c1h,2),
                    "mid":round((c3l+c1h)/2,2),"size":size,"strong":strong}, \
                   f"{'STRONG' if strong else 'WEAK'} Bullish FVG {size:.1f}pts"
        if c1l>c3h:
            size=round(c1l-c3h,2)
            strong=size>STRONG_FVG_GAP and body>STRONG_FVG_BODY
            return {"type":"bearish","top":round(c1l,2),"bottom":round(c3h,2),
                    "mid":round((c1l+c3h)/2,2),"size":size,"strong":strong}, \
                   f"{'STRONG' if strong else 'WEAK'} Bearish FVG {size:.1f}pts"
    return None,"No FVG in last 15 candles"

def detect_breakaway_gap(df, prev_close):
    if df is None or len(df)<2: return None,"Not enough candles"
    first_open=float(df["open"].iloc[0])
    if prev_close:
        gap=abs(first_open-prev_close)
        if gap>=BREAKAWAY_GAP_OPEN:
            direction="bullish" if first_open>prev_close else "bearish"
            return {"type":direction,"gap_type":"gap_open","size":round(gap,1),
                    "level":round(prev_close,1),"strong":True}, \
                   f"Gap open {direction} {gap:.1f}pts"
    candles=df.tail(10)
    for i in range(len(candles)-1,0,-1):
        curr=candles.iloc[i]; prev=candles.iloc[i-1]
        body=abs(float(curr["close"])-float(curr["open"]))
        if body<BREAKAWAY_GAP_INTRA: continue
        if float(curr["low"])>float(prev["high"]):
            return {"type":"bullish","gap_type":"intraday","size":round(body,1),
                    "level":float(prev["high"]),"strong":True}, \
                   f"Intraday breakaway bullish {body:.1f}pts"
        if float(curr["high"])<float(prev["low"]):
            return {"type":"bearish","gap_type":"intraday","size":round(body,1),
                    "level":float(prev["low"]),"strong":True}, \
                   f"Intraday breakaway bearish {body:.1f}pts"
    return None,"No breakaway gap"

def detect_orb(df, orb_high, orb_low, current_price=None):
    if orb_high is None or orb_low is None: return None,"ORB not formed yet"
    price=current_price
    if price is None and df is not None and len(df)>0:
        price=float(df.iloc[-1]["close"])
    if price:
        if price>orb_high and (price-orb_high)>=5:
            return {"type":"bullish","level":orb_high,"size":round(price-orb_high,1)}, \
                   f"ORB bullish {price:.1f} > {orb_high:.1f}"
        if price<orb_low and (orb_low-price)>=5:
            return {"type":"bearish","level":orb_low,"size":round(orb_low-price,1)}, \
                   f"ORB bearish {price:.1f} < {orb_low:.1f}"
    return None,f"No ORB | Range:{orb_low:.1f}-{orb_high:.1f}"

def detect_vwap_rejection(df5, trend, rvol):
    if df5 is None or len(df5)<5: return None,"Not enough candles"
    if trend=="neutral": return None,"Trend neutral"
    if rvol<MIN_RVOL: return None,f"RVOL {rvol} < {MIN_RVOL}"
    df5 = calc_vwap_bands(df5)
    last=df5.iloc[-1]; prev=df5.iloc[-2]
    vwap=float(last["vwap"]); vl1=float(last["vwap_l1"]); vu1=float(last["vwap_u1"])
    close=float(last["close"]); low=float(last["low"]); high=float(last["high"])
    vol=float(last["volume"]); avg_vol=float(df5["volume"].mean())
    surge=vol>avg_vol*MIN_RVOL
    if trend=="bullish":
        if (vl1<=low<=vwap or float(prev["low"])<=vwap) and close>vwap and surge:
            return {"type":"bullish","vwap":round(vwap,2),"vwap_l1":round(vl1,2),
                    "vwap_u1":round(vu1,2),"volume":round(vol,0),
                    "avg_volume":round(avg_vol,0),"rvol":rvol}, \
                   f"VWAP bullish {vwap:.1f} RVOL:{rvol}"
    if trend=="bearish":
        if (vwap<=high<=vu1 or float(prev["high"])>=vwap) and close<vwap and surge:
            return {"type":"bearish","vwap":round(vwap,2),"vwap_l1":round(vl1,2),
                    "vwap_u1":round(vu1,2),"volume":round(vol,0),
                    "avg_volume":round(avg_vol,0),"rvol":rvol}, \
                   f"VWAP bearish {vwap:.1f} RVOL:{rvol}"
    return None,f"No VWAP | {vwap:.1f} surge:{surge}"

def is_retesting(price,bottom,top): return bottom<=price<=top

def get_option_details(nifty_price, option_type):
    atm    = round(nifty_price/50)*50
    strike = atm+OTM_OFFSET if option_type=="CE" else atm-OTM_OFFSET
    today  = datetime.date.today()
    days   = (3-today.weekday())%7
    if days==0: days=7
    expiry = today+datetime.timedelta(days=days)
    return strike, expiry

def is_expiry_day(): return datetime.date.today().weekday()==3

# ─────────────────────────────────────────────
#  PAPER TRADE ENGINE
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium, signal,
                 pcr, fii_bias, pre_bias, rvol, obv,
                 trend_strength, is_strong=False):
        self.trade_no     = trade_no
        self.strategy     = strategy
        self.direction    = direction
        self.entry_price  = entry_price
        self.option_type  = option_type
        self.strike       = strike
        self.expiry       = expiry
        self.premium      = premium
        self.signal       = signal
        self.pcr          = pcr
        self.fii_bias     = fii_bias
        self.pre_bias     = pre_bias
        self.rvol         = rvol
        self.obv          = obv
        self.trend_strength = trend_strength
        self.is_strong    = is_strong
        self.entry_time   = now_ist().strftime("%H:%M:%S IST")
        self.start_time   = time.time()
        self.be_moved     = False
        self.trailing     = is_strong
        self.best_price   = entry_price
        self.sl_price     = entry_price-SL_POINTS if direction=="bullish" else entry_price+SL_POINTS
        self.tgt_price    = entry_price+TARGET_POINTS if direction=="bullish" else entry_price-TARGET_POINTS

    def check(self, ltp):
        if self.trailing:
            if self.direction=="bullish" and ltp>self.best_price:
                self.best_price=ltp
                profit=ltp-self.entry_price
                if profit>=TRAIL_START:
                    new_sl=round(ltp-TRAIL_DISTANCE,2)
                    if new_sl>self.sl_price:
                        self.sl_price=new_sl
                        tg("📈",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.1f}",f"Profit:+{profit:.1f}pts",f"SL:{new_sl:.1f}"])
            elif self.direction=="bearish" and ltp<self.best_price:
                self.best_price=ltp
                profit=self.entry_price-ltp
                if profit>=TRAIL_START:
                    new_sl=round(ltp+TRAIL_DISTANCE,2)
                    if new_sl<self.sl_price:
                        self.sl_price=new_sl
                        tg("📉",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.1f}",f"Profit:+{profit:.1f}pts",f"SL:{new_sl:.1f}"])
            if self.direction=="bullish" and ltp<=self.sl_price: return "sl"
            if self.direction=="bearish" and ltp>=self.sl_price: return "sl"
        else:
            if not self.be_moved:
                half=(self.entry_price+self.tgt_price)/2
                if (self.direction=="bullish" and ltp>=half) or \
                   (self.direction=="bearish" and ltp<=half):
                    self.be_moved=True; self.sl_price=self.entry_price
                    tg("LOCK",f"Trade #{self.trade_no} Breakeven",
                       [f"Nifty:{ltp:.1f}",f"SL:{self.entry_price:.1f}"])
            if self.direction=="bullish":
                if ltp>=self.tgt_price: return "target"
                if ltp<=self.sl_price:  return "sl"
            else:
                if ltp<=self.tgt_price: return "target"
                if ltp>=self.sl_price:  return "sl"
        return None

    def duration(self): return round((time.time()-self.start_time)/60,1)
    def calc_pnl(self, exit_price):
        pts = exit_price-self.entry_price if self.direction=="bullish" else self.entry_price-exit_price
        return round(pts*0.4*LOT_SIZE,0)

# ─────────────────────────────────────────────
#  CSV LOGS 35+ columns
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime","nifty_ltp","nifty_change_from_open","nifty_change_pct",
    "trend_5m","trend_15m","trend_30m","trend_combined","trend_strength",
    "rvol","obv_direction","cumulative_delta","volume_current","volume_avg",
    "vwap","vwap_upper1","vwap_lower1","vwap_upper2","vwap_lower2","price_vs_vwap",
    "ema9","ema21","ema50","price_vs_ema9","price_vs_ema21","price_vs_ema50",
    "fvg_found","fvg_type","fvg_strong","fvg_size",
    "bos_confirmed","breakaway_found","breakaway_type",
    "orb_high","orb_low","orb_signal","orb_breakout_size",
    "vwap_signal","vwap_rvol",
    "pcr","pcr_bias","fii_bias","overall_bias",
    "reversal_risk","reversal_signals",
    "prev_high","prev_low","prev_close",
    "entry_condition_met","strategy_triggered",
    "trades_today","daily_pnl","consec_losses","reason"
]

TRADE_COLS = [
    "date","trade_no","strategy","entry_time","exit_time",
    "pre_bias","fii_bias","pcr",
    "trend_combined","trend_strength",
    "rvol_at_entry","obv_at_entry",
    "reversal_risk","reversal_signals",
    "direction","is_strong","exit_mode",
    "entry_nifty","exit_nifty","points_moved",
    "option_type","strike","expiry",
    "premium","lots","capital_used",
    "sl_points","target_points","pnl_est","result",
    "be_triggered","trail_triggered",
    "duration_min","consec_losses","daily_pnl","notes"
]

def init_logs():
    for fname,cols in [("scan_log_v3.csv",SCAN_COLS),
                        ("trade_log_v3.csv",TRADE_COLS)]:
        if not os.path.exists(fname):
            with open(fname,"w",newline="") as f:
                csv.DictWriter(f,fieldnames=cols).writeheader()
    log.info("Nifty v3 logs initialised")

def write_scan(rec):
    with open("scan_log_v3.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in SCAN_COLS}
        csv.DictWriter(f,fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open("trade_log_v3.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in TRADE_COLS}
        csv.DictWriter(f,fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats, pre_bias, pcr):
    wr=(stats["wins"]/stats["trades"]*100) if stats["trades"]>0 else 0
    tg("SUMMARY","Nifty v3 DAILY SUMMARY",
       [f"Pre-bias   : {pre_bias.upper()}",
        f"PCR        : {pcr or 'N/A'}",
        f"Trades     : {stats['trades']}",
        f"Wins       : {stats['wins']}",
        f"Losses     : {stats['losses']}",
        f"Win rate   : {wr:.1f}%",
        f"P&L        : Rs.{stats['pnl']:+.0f}",
        f"FVG trades : {stats.get('fvg_trades',0)}",
        f"ORB trades : {stats.get('orb_trades',0)}",
        f"VWAP trades: {stats.get('vwap_trades',0)}",
        f"Strong trail:{stats.get('strong_trades',0)}"])
    send_csv_files()

def open_trade(trade_no, strategy, direction, entry_price,
               pcr_val, tg_listener, pre_bias, is_strong,
               signal, rvol, obv, trend_strength, trend, risk_level):
    opt    = "CE" if direction=="bullish" else "PE"
    strike,expiry = get_option_details(entry_price,opt)
    premium = round(CAPITAL_PER_TRADE/LOT_SIZE,1)
    trade   = PaperTrade(
        trade_no=trade_no,strategy=strategy,
        direction=direction,entry_price=entry_price,
        option_type=opt,strike=strike,expiry=expiry,
        premium=premium,signal=signal,pcr=pcr_val,
        fii_bias=tg_listener.bias,pre_bias=pre_bias,
        rvol=rvol,obv=obv,trend_strength=trend_strength,
        is_strong=is_strong
    )
    mode = "Trailing" if is_strong else f"Fixed {TARGET_POINTS}pts"
    tg("ENTRY",f"NIFTY PAPER TRADE #{trade_no} — {strategy}",
       [f"Direction    : {direction.upper()}",
        f"Trend        : {trend} ({trend_strength})",
        f"Option       : {opt} {strike} | {expiry}",
        f"Entry        : {entry_price:.1f}",
        f"SL           : {trade.sl_price:.1f} (-{SL_POINTS}pts)",
        f"Exit mode    : {mode}",
        f"RVOL         : {rvol}x",
        f"OBV          : {obv}",
        f"Reversal risk: {risk_level}",
        f"Capital      : Rs.{premium*LOT_SIZE:.0f}",
        f"NOTE         : PAPER TRADE"])
    return trade

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    init_logs()
    tg_listener = TelegramListener()
    tg_listener.start()

    stats = {
        "trades":0,"wins":0,"losses":0,"timeouts":0,
        "skipped":0,"pnl":0.0,"consec_loss":0,
        "fvg_trades":0,"orb_trades":0,"vwap_trades":0,"strong_trades":0
    }

    trade_no     = 0
    active_trade = None
    last_scan    = None
    pre_bias     = "neutral"
    pcr_val      = None
    pcr_bias     = "neutral"
    premarket_done = False
    reminder_sent  = False
    orb_high     = None
    orb_low      = None
    orb_formed   = False
    prev_ohlc    = None
    used_signals = set()
    open_price   = None
    closed_summary_sent = False

    send_telegram(
        f"Nifty Scalping Bot v3 Started\n"
        f"Patches: 20 applied\n"
        f"Mode: PAPER TRADING\n"
        f"SL:{SL_POINTS}pts TGT:{TARGET_POINTS}pts Trail:{TRAIL_DISTANCE}pts\n"
        f"RVOL min:{MIN_RVOL}x LOT:{LOT_SIZE}\n"
        f"Max:{MAX_TRADES} Loss:Rs.{DAILY_LOSS_LIMIT} Profit:Rs.{DAILY_PROFIT_TARGET}\n"
        f"nifty_auto_bias: integrated\n\n"
        f"/bias bullish|bearish|neutral\n/status\n/report"
    )

    while True:
        t = ist_time()
        now = now_ist()

        # PATCH #12: 9AM reminder
        if not reminder_sent and t>=REMINDER_TIME and t<TRADE_START:
            pcr_v,pcr_b = get_pcr()
            send_telegram(
                f"Nifty Pre-market starts soon!\n"
                f"PCR: {pcr_v or 'N/A'} ({pcr_b})\n"
                f"Check FII/DII on NSE and send:\n"
                f"/bias bullish|bearish|neutral\n"
                f"Auto-bias used if not sent."
            )
            reminder_sent=True

        # PATCH #5: Outside hours — no alerts
        if t<TRADE_START:
            if not premarket_done and t>=REMINDER_TIME:
                prev_ohlc = get_prev_day_ohlc()
                final_bias, bias_report = get_combined_bias_nifty(
                    config.LIVE_TOKEN,
                    prev_ohlc["close"] if prev_ohlc else None,
                    tg_listener.bias
                )
                pre_bias = final_bias
                pcr_val  = bias_report["pcr_val"]
                pcr_bias = bias_report["pcr_bias"]
                send_telegram(format_bias_message_nifty(bias_report))
                premarket_done=True
            time.sleep(30); continue

        premarket_done=False

        if t>=TRADE_END:
            if not closed_summary_sent:
                send_summary(stats, pre_bias, pcr_val)
                send_telegram("Nifty market closed. Bot sleeping.")
                closed_summary_sent=True
                stats={"trades":0,"wins":0,"losses":0,"timeouts":0,
                       "skipped":0,"pnl":0.0,"consec_loss":0,
                       "fvg_trades":0,"orb_trades":0,"vwap_trades":0,"strong_trades":0}
                trade_no=0; active_trade=None; last_scan=None
                pre_bias="neutral"; pcr_val=None
                orb_high=None; orb_low=None; orb_formed=False
                used_signals=set(); tg_listener.bias="neutral"
                reminder_sent=False; premarket_done=False
                open_price=None; closed_summary_sent=False
            time.sleep(60); continue

        closed_summary_sent=False

        # GUARDS
        if stats["trades"]>=MAX_TRADES: time.sleep(30*60); continue
        if stats["consec_loss"]>=3:
            tg("STOP","Risk Protection",[f"Losses:{stats['consec_loss']}","Paused"])
            send_summary(stats,pre_bias,pcr_val); time.sleep(16*3600); continue
        if stats["pnl"]<=-DAILY_LOSS_LIMIT:
            tg("STOP","Loss Limit",[f"P&L:Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_val); time.sleep(16*3600); continue
        if stats["pnl"]>=DAILY_PROFIT_TARGET:
            tg("DONE","Profit Target!",[f"P&L:Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_val); time.sleep(16*3600); continue
        if is_expiry_day() and t>=EXPIRY_STOP: time.sleep(10*60); continue

        # MONITOR ACTIVE TRADE
        if active_trade is not None:
            ltp=get_nifty_ltp(); result=None
            if ltp: result=active_trade.check(ltp)
            if t>=TRADE_END: result="timeout"; ltp=ltp or active_trade.entry_price
            if result:
                exit_time=now.strftime("%H:%M:%S IST")
                duration=active_trade.duration()
                pnl=active_trade.calc_pnl(ltp)
                pts=round(ltp-active_trade.entry_price,2) if active_trade.direction=="bullish" \
                    else round(active_trade.entry_price-ltp,2)
                if result=="target": icon="WIN"; stats["wins"]+=1; stats["consec_loss"]=0
                elif result=="sl":   icon="LOSS"; stats["losses"]+=1; stats["consec_loss"]+=1
                else:                icon="TIME"; stats["timeouts"]+=1; stats["consec_loss"]=0
                stats["trades"]+=1; stats["pnl"]+=pnl
                exit_mode="Trail" if active_trade.trailing else "Fixed"
                tg(icon,f"NIFTY TRADE #{active_trade.trade_no} {result.upper()}",
                   [f"Strategy : {active_trade.strategy}",
                    f"Entry    : {active_trade.entry_price:.1f}",
                    f"Exit     : {ltp:.1f}",
                    f"Points   : {pts:+.1f}",
                    f"Duration : {duration}min",
                    f"P&L      : Rs.{pnl:+.0f}",
                    f"Day P&L  : Rs.{stats['pnl']:+.0f}",
                    f"Trades   : {stats['trades']}/{MAX_TRADES}"])
                write_trade({
                    "date":datetime.date.today(),"trade_no":active_trade.trade_no,
                    "strategy":active_trade.strategy,
                    "entry_time":active_trade.entry_time,"exit_time":exit_time,
                    "pre_bias":pre_bias,"fii_bias":active_trade.fii_bias,
                    "pcr":active_trade.pcr,
                    "trend_combined":active_trade.trend_strength,
                    "trend_strength":active_trade.trend_strength,
                    "rvol_at_entry":active_trade.rvol,"obv_at_entry":active_trade.obv,
                    "direction":active_trade.direction,"is_strong":active_trade.is_strong,
                    "exit_mode":exit_mode,"entry_nifty":active_trade.entry_price,
                    "exit_nifty":round(ltp,2),"points_moved":pts,
                    "option_type":active_trade.option_type,"strike":active_trade.strike,
                    "expiry":active_trade.expiry,"premium":active_trade.premium,
                    "lots":1,"capital_used":active_trade.premium*LOT_SIZE,
                    "sl_points":SL_POINTS,"target_points":TARGET_POINTS,
                    "pnl_est":pnl,"result":result,
                    "be_triggered":active_trade.be_moved,
                    "trail_triggered":active_trade.trailing,
                    "duration_min":duration,"consec_losses":stats["consec_loss"],
                    "daily_pnl":stats["pnl"],"notes":active_trade.signal
                })
                active_trade=None; time.sleep(2*60)
            else: time.sleep(15)
            continue

        # FETCH FRESH DATA
        ltp   = get_nifty_ltp()
        df_5  = get_candles(5)
        df_15 = get_candles(15)
        df_30 = get_candles(30)
        if ltp is None or df_5 is None: time.sleep(15); continue
        if open_price is None: open_price=ltp

        # ORB FORMATION
        if not orb_formed and t>=ORB_END_TIME:
            try:
                orb_df=df_5[df_5["timestamp"].dt.time<=ORB_END_TIME]
                if not orb_df.empty:
                    orb_high=float(orb_df["high"].max())
                    orb_low =float(orb_df["low"].min())
                    orb_formed=True
                    tg("ORB","Nifty ORB Range Formed",
                       [f"High:{orb_high:.1f}",f"Low:{orb_low:.1f}",
                        f"Size:{orb_high-orb_low:.1f}pts",f"Nifty:{ltp:.1f}"])
            except Exception as e: log.error(f"ORB: {e}")

        # PCR refresh
        pcr_val,pcr_bias = get_pcr()

        # INDICATORS
        trend,trend_reason,trend_strength = detect_trend_multi(df_5,df_15,df_30)
        t5,_,_  = detect_trend_relaxed(df_5)
        t15,_,_ = detect_trend_relaxed(df_15)
        t30,_,_ = detect_trend_relaxed(df_30)
        rvol    = calc_rvol(df_5)
        obv     = calc_obv(df_5)
        cum_d   = calc_cumulative_delta(df_5)
        df5v    = calc_vwap_bands(df_5)
        lr      = df5v.iloc[-1]
        vwap    = round(float(lr["vwap"]),2)
        vu1     = round(float(lr["vwap_u1"]),2)
        vl1     = round(float(lr["vwap_l1"]),2)
        vu2     = round(float(lr["vwap_u2"]),2)
        vl2     = round(float(lr["vwap_l2"]),2)
        df5e    = calc_ema(df_5)
        ema9    = round(float(df5e["ema9"].iloc[-1]),2)
        ema21   = round(float(df5e["ema21"].iloc[-1]),2)
        ema50   = round(float(df5e["ema50"].iloc[-1]),2)

        # DETECTORS
        fvg,    fvg_r  = detect_fvg(df_5)
        bos,    bos_lv = detect_bos(df_5,trend)
        bgap,   bgap_r = detect_breakaway_gap(df_5,prev_ohlc["close"] if prev_ohlc else None)
        orb_s,  orb_r  = detect_orb(df_5,orb_high,orb_low,ltp)
        vwap_s, vwap_r = detect_vwap_rejection(df_5,trend,rvol)

        # 5-MIN SCAN LOG
        do_scan=(last_scan is None or (now_ist()-last_scan).seconds>=300)
        if do_scan:
            last_scan=now_ist()
            strats=[]
            if fvg and bos: strats.append("FVG+BOS")
            if bgap:        strats.append("Breakaway")
            if orb_s:       strats.append("ORB")
            if vwap_s:      strats.append("VWAP")
            entry_met=len(strats)>0 and trend!="neutral" and rvol>=MIN_RVOL
            chg_open=round(ltp-open_price,2) if open_price else 0
            chg_pct =round((chg_open/open_price*100),2) if open_price else 0
            write_scan({
                "datetime":now.strftime("%Y-%m-%d %H:%M IST"),
                "nifty_ltp":round(ltp,2),"nifty_change_from_open":chg_open,
                "nifty_change_pct":chg_pct,"trend_5m":t5,"trend_15m":t15,
                "trend_30m":t30,"trend_combined":trend,"trend_strength":trend_strength,
                "rvol":rvol,"obv_direction":obv,"cumulative_delta":cum_d,
                "volume_current":round(float(df_5["volume"].iloc[-1]),0),
                "volume_avg":round(float(df_5["volume"].mean()),0),
                "vwap":vwap,"vwap_upper1":vu1,"vwap_lower1":vl1,
                "vwap_upper2":vu2,"vwap_lower2":vl2,"price_vs_vwap":round(ltp-vwap,2),
                "ema9":ema9,"ema21":ema21,"ema50":ema50,
                "price_vs_ema9":round(ltp-ema9,2),"price_vs_ema21":round(ltp-ema21,2),
                "price_vs_ema50":round(ltp-ema50,2),
                "fvg_found":fvg is not None,"fvg_type":fvg["type"] if fvg else "",
                "fvg_strong":fvg["strong"] if fvg else "","fvg_size":fvg["size"] if fvg else "",
                "bos_confirmed":bos,"breakaway_found":bgap is not None,
                "breakaway_type":bgap["gap_type"] if bgap else "",
                "orb_high":orb_high or "","orb_low":orb_low or "",
                "orb_signal":orb_s["type"] if orb_s else "",
                "orb_breakout_size":orb_s["size"] if orb_s else "",
                "vwap_signal":vwap_s["type"] if vwap_s else "",
                "vwap_rvol":vwap_s["rvol"] if vwap_s else "",
                "pcr":pcr_val or "","pcr_bias":pcr_bias,
                "fii_bias":tg_listener.bias,"overall_bias":pre_bias,
                "prev_high":prev_ohlc["high"] if prev_ohlc else "",
                "prev_low":prev_ohlc["low"] if prev_ohlc else "",
                "prev_close":prev_ohlc["close"] if prev_ohlc else "",
                "entry_condition_met":entry_met,
                "strategy_triggered":",".join(strats),
                "trades_today":stats["trades"],"daily_pnl":stats["pnl"],
                "consec_losses":stats["consec_loss"],
                "reason":f"FVG:{fvg_r}|ORB:{orb_r}|VWAP:{vwap_r}"
            })
            ci="OK" if entry_met else "WAIT"
            tg(ci,f"NIFTY SCAN {now.strftime('%H:%M')} IST",
               [f"Nifty     : {ltp:.1f} ({chg_pct:+.2f}%)",
                f"Trend     : {trend.upper()} ({trend_strength})",
                f"5m/15m/30m: {t5}/{t15}/{t30}",
                f"RVOL      : {rvol}x {'OK' if rvol>=MIN_RVOL else 'LOW'}",
                f"OBV       : {obv}",
                f"VWAP      : {vwap:.1f} ({ltp-vwap:+.1f})",
                f"EMA9/21   : {ema9:.1f}/{ema21:.1f}",
                f"FVG       : {fvg_r[:40] if fvg else 'NONE'}",
                f"BOS       : {'YES' if bos else 'NO'}",
                f"ORB       : {orb_r[:40]}",
                f"VWAP sig  : {vwap_r[:40]}",
                f"PCR       : {pcr_val or 'N/A'} ({pcr_bias})",
                f"Bias      : {pre_bias.upper()}",
                f"Signals   : {', '.join(strats) if strats else 'NONE'}"])

        if rvol<MIN_RVOL: time.sleep(60); continue

        # ── STRATEGY 1: FVG + BOS ─────────────────────────────
        if fvg and bos and trend!="neutral" and "FVG" not in used_signals:
            if fvg["type"]==trend and (pre_bias=="neutral" or pre_bias==trend):
                if obv==trend or obv=="neutral":
                    proceed,risk,summary,rev_signals = pre_trade_check_nifty(
                        df_5,df_15,trend,pre_bias,
                        prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                    )
                    send_telegram(format_reversal_alert_nifty(
                        risk,proceed,rev_signals,summary,"FVG+BOS",trend))
                    if not proceed:
                        stats["skipped"]+=1; time.sleep(60); continue
                    is_strong=fvg["strong"]
                    retest_ok=False; ep=None; sw=time.time()
                    while time.time()-sw<10*60:
                        cur=get_nifty_ltp()
                        if cur and is_retesting(cur,fvg["bottom"],fvg["top"]):
                            retest_ok=True; ep=cur; break
                        time.sleep(15)
                    if retest_ok:
                        trade_no+=1
                        active_trade=open_trade(
                            trade_no,"FVG+BOS",trend,ep,pcr_val,
                            tg_listener,pre_bias,is_strong,
                            f"FVG {fvg['size']:.1f}pts BOS@{bos_lv:.1f}",
                            rvol,obv,trend_strength,trend,risk
                        )
                        used_signals.add("FVG"); stats["fvg_trades"]+=1
                        if is_strong: stats["strong_trades"]+=1
                        time.sleep(15); continue
                    else: stats["skipped"]+=1

        # ── STRATEGY 1B: BREAKAWAY GAP ────────────────────────
        if bgap and trend!="neutral" and "BGAP" not in used_signals:
            if bgap["type"]==trend and (pre_bias=="neutral" or pre_bias==trend):
                proceed,risk,summary,rev_signals = pre_trade_check_nifty(
                    df_5,df_15,trend,pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_signals,summary,"BreakawayGap",trend))
                if not proceed:
                    stats["skipped"]+=1; time.sleep(60); continue
                level=bgap["level"]; retest_ok=False; ep=None; sw=time.time()
                while time.time()-sw<10*60:
                    cur=get_nifty_ltp()
                    if cur and is_retesting(cur,level-5,level+5):
                        retest_ok=True; ep=cur; break
                    time.sleep(15)
                if retest_ok:
                    trade_no+=1
                    active_trade=open_trade(
                        trade_no,"BreakawayGap",trend,ep,pcr_val,
                        tg_listener,pre_bias,True,
                        f"Bgap {bgap['gap_type']} {bgap['size']:.1f}pts",
                        rvol,obv,trend_strength,trend,risk
                    )
                    used_signals.add("BGAP"); stats["fvg_trades"]+=1; stats["strong_trades"]+=1
                    time.sleep(15); continue
                else: stats["skipped"]+=1

        # ── STRATEGY 2: ORB ───────────────────────────────────
        if orb_s and orb_formed and "ORB" not in used_signals:
            if pre_bias=="neutral" or pre_bias==orb_s["type"]:
                proceed,risk,summary,rev_signals = pre_trade_check_nifty(
                    df_5,df_15,orb_s["type"],pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_signals,summary,"ORB",orb_s["type"]))
                if not proceed:
                    stats["skipped"]+=1; time.sleep(60); continue
                level=orb_s["level"]; retest_ok=False; ep=None; sw=time.time()
                while time.time()-sw<10*60:
                    cur=get_nifty_ltp()
                    if cur and is_retesting(cur,level-5,level+5):
                        retest_ok=True; ep=cur; break
                    time.sleep(15)
                if retest_ok:
                    trade_no+=1
                    active_trade=open_trade(
                        trade_no,"ORB",orb_s["type"],ep,pcr_val,
                        tg_listener,pre_bias,False,
                        f"ORB {orb_s['type']} {orb_s['size']:.1f}pts",
                        rvol,obv,trend_strength,trend,risk
                    )
                    used_signals.add("ORB"); stats["orb_trades"]+=1
                    time.sleep(15); continue
                else: stats["skipped"]+=1

        # ── STRATEGY 3: VWAP ──────────────────────────────────
        if vwap_s and "VWAP" not in used_signals:
            if pre_bias=="neutral" or pre_bias==vwap_s["type"]:
                proceed,risk,summary,rev_signals = pre_trade_check_nifty(
                    df_5,df_15,vwap_s["type"],pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_signals,summary,"VWAP",vwap_s["type"]))
                if not proceed:
                    stats["skipped"]+=1; time.sleep(60); continue
                cur=get_nifty_ltp()
                if cur:
                    trade_no+=1
                    active_trade=open_trade(
                        trade_no,"VWAP",vwap_s["type"],cur,pcr_val,
                        tg_listener,pre_bias,False,
                        f"VWAP {vwap_s['type']} RVOL:{rvol}",
                        rvol,obv,trend_strength,trend,risk
                    )
                    used_signals.add("VWAP"); stats["vwap_trades"]+=1
                    time.sleep(15); continue

        time.sleep(60)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Nifty Bot v3 stopped")
        send_telegram("Nifty Bot v3 stopped.")
