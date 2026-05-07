"""
=============================================================
  Nifty 50 Scalping Bot v4 — Final Production Build
  ─────────────────────────────────────────────────
  ALL PATCHES APPLIED:
  #1  Session bias + Z-score mean reversion
  #2  FVG retest at gap EDGE not inside
  #3  EMA50 candle confirmation
  #4  Fixed CSV column shift
  #5  Nifty futures key auto-detection
  #6  VWAPCross min confidence = 3/9
  #7  PCR cache max 15 minutes
  #8  ORB direction = session bias
  #9  Previous day S&R in scan log

  VWAP IMPROVEMENTS:
  #10 ATR-based dynamic VWAP bands
  #11 2-candle VWAP cross confirmation + volume filter
  #12 Previous day VWAP with 0.5% gap filter

  ADDITIONAL:
  #13 RSI confirmation for Z-score mean reversion
  #14 Time-based exit after 2:00 PM (theta protection)
  #15 Consecutive loss → reduce capital (not hard stop)
  #16 Same strategy allowed again if HIGH confidence (7+/9)
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
        logging.FileHandler("nifty_v4.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  PARAMETERS
# ─────────────────────────────────────────────
SL_POINTS            = 10
TARGET_POINTS        = 8
TRAIL_DISTANCE       = 10
TRAIL_START          = 15
STRONG_FVG_GAP       = 10
STRONG_FVG_BODY      = 20
MIN_FVG_BODY         = 10
FVG_MAX_AGE_CANDLES  = 6       # PATCH #2: max 30min old FVG
ORB_END_TIME         = datetime.time(9, 45)
SESSION_BIAS_END     = datetime.time(10, 0)
TIME_EXIT_AFTER      = datetime.time(14, 0)   # PATCH #14
MAX_TRADES           = 15
CAPITAL_PER_TRADE    = 6500
CAPITAL_REDUCED      = 3250    # PATCH #15: 50% after 2 losses
DAILY_LOSS_LIMIT     = 3000
DAILY_PROFIT_TARGET  = 2000
LOT_SIZE             = 65
OTM_OFFSET           = 100
MIN_RVOL             = 1.0
EMA50_TOLERANCE      = 20
NIFTY_KEY            = "NSE_INDEX|Nifty 50"
ZSCORE_WINDOW        = 6
ZSCORE_THRESHOLD     = 2.0
PCR_FRESH_SECS       = 900     # 15 min
PCR_STALE_SECS       = 1800    # 30 min
GAP_FILTER_PCT       = 0.5     # PATCH #12
ATR_PERIOD           = 14      # PATCH #10
ATR_TRENDING_MIN     = 15      # min ATR to trade VWAP band
VWAP_CROSS_VOL_MIN   = 1.2     # PATCH #11: volume filter
RSI_PERIOD           = 14      # PATCH #13
RSI_OVERBOUGHT       = 65
RSI_OVERSOLD         = 35
HIGH_CONF_REENTRY    = 7       # PATCH #16: re-entry threshold

# Minimum confidence per strategy
MIN_CONF = {
    "StrongFVG"  : 6,
    "ORB+EMA"    : 5,
    "EMAStack"   : 5,
    "VWAPBand"   : 4,
    "VWAPCross"  : 3,
    "EMA50Bounce": 6,
    "EMACross"   : 4,
}
HIGH_CONF   = 7
MEDIUM_CONF = 4

IST = pytz.timezone("Asia/Kolkata")
def now_ist(): return datetime.datetime.now(IST)
def ist_time(): return now_ist().time()

TRADE_START   = datetime.time(9, 30)
TRADE_END     = datetime.time(14, 30)
EXPIRY_STOP   = datetime.time(13, 0)
REMINDER_TIME = datetime.time(9, 0)

def get_headers():
    return {"Accept":"application/json",
            "Authorization":f"Bearer {config.LIVE_TOKEN}"}

# ─────────────────────────────────────────────
#  PATCH #1: SESSION BIAS + Z-SCORE
# ─────────────────────────────────────────────
class SessionBias:
    def __init__(self):
        self.bias          = "neutral"
        self.is_set        = False
        self.price_history = []

    def update(self, ltp, df_5):
        t = ist_time()
        if not self.is_set and t >= SESSION_BIAS_END and df_5 is not None:
            sess_df = df_5[df_5["timestamp"].dt.time <= SESSION_BIAS_END]
            if not sess_df.empty:
                first_open = float(sess_df["open"].iloc[0])
                last_close = float(sess_df["close"].iloc[-1])
                high_30    = float(sess_df["high"].max())
                low_30     = float(sess_df["low"].min())
                mid_30     = (high_30 + low_30) / 2
                if last_close > mid_30 and last_close > first_open:
                    self.bias = "bullish"
                elif last_close < mid_30 and last_close < first_open:
                    self.bias = "bearish"
                else:
                    self.bias = "neutral"
                self.is_set = True
                log.info(f"Session bias: {self.bias} open:{first_open:.0f} close:{last_close:.0f}")
        self.price_history.append(ltp)
        if len(self.price_history) > ZSCORE_WINDOW * 2:
            self.price_history.pop(0)

    def get_zscore(self, ltp):
        if len(self.price_history) < ZSCORE_WINDOW: return 0.0
        w    = self.price_history[-ZSCORE_WINDOW:]
        mean = np.mean(w); std = np.std(w)
        return round((ltp - mean) / std, 2) if std > 0 else 0.0

    def trade_allowed(self, direction, ltp):
        if not self.is_set or self.bias == "neutral":
            return True, 0.0, "Session neutral — all trades allowed"
        zs = self.get_zscore(ltp)
        if self.bias == direction:
            return True, zs, f"Trend trade — session {self.bias} matches"
        # Counter-trend allowed only if Z-score extreme
        if abs(zs) >= ZSCORE_THRESHOLD:
            return True, zs, f"Mean reversion allowed — Z:{zs:+.2f} extreme"
        return False, zs, f"Counter-trend blocked — session:{self.bias} Z:{zs:+.2f}"

# ─────────────────────────────────────────────
#  PATCH #7: PCR CACHE 15-MIN
# ─────────────────────────────────────────────
class PCRCache:
    def __init__(self):
        self.val = None; self.bias = "neutral"; self.time = None

    def age(self):
        if self.time is None: return 9999
        return (datetime.datetime.now()-self.time).seconds

    def fetch(self):
        try:
            today = datetime.date.today()
            for add in [0,7,14]:
                d = (3-today.weekday())%7 + add
                if d==0: d=7
                exp = today + datetime.timedelta(days=d)
                resp = requests.get(
                    "https://api.upstox.com/v2/option/chain",
                    headers=get_headers(),
                    params={"instrument_key":NIFTY_KEY,
                            "expiry_date":exp.strftime("%Y-%m-%d")},
                    timeout=10)
                data = resp.json()
                if data["status"]!="success" or not data.get("data"): continue
                pe=ce=0
                for r in data["data"]:
                    p=r.get("put_options",{}); c=r.get("call_options",{})
                    if p and p.get("market_data"): pe+=p["market_data"].get("oi",0)
                    if c and c.get("market_data"): ce+=c["market_data"].get("oi",0)
                if ce>0:
                    pcr = round(pe/ce,2)
                    bias= "bullish" if pcr>1.2 else "bearish" if pcr<0.8 else "neutral"
                    self.val=pcr; self.bias=bias; self.time=datetime.datetime.now()
                    log.info(f"PCR:{pcr}({bias}) exp:{exp}")
                    return pcr,bias,"fresh"
            return self.val,self.bias,"stale"
        except Exception as e:
            log.error(f"PCR:{e}"); return self.val,self.bias,"error"

    def get(self):
        a = self.age()
        if a < PCR_FRESH_SECS:  return self.val,self.bias,1.0,"fresh"
        if a < PCR_STALE_SECS:  return self.val,"neutral",0.5,"stale"
        return None,"neutral",0.0,"excluded"

    def should_refresh(self): return self.age() >= PCR_FRESH_SECS

# ─────────────────────────────────────────────
#  PATCH #15: CAPITAL MANAGER
# ─────────────────────────────────────────────
class CapitalManager:
    """Reduce capital after consecutive losses, restore on win."""
    def __init__(self):
        self.consec_losses = 0
        self.reduced       = False

    def on_result(self, result):
        if result == "target":
            self.consec_losses = 0
            self.reduced       = False
        elif result in ["sl","timeout"]:
            self.consec_losses += 1
            if self.consec_losses >= 2:
                self.reduced = True

    def get_capital(self):
        if self.reduced:
            log.info(f"Capital reduced after {self.consec_losses} losses")
            return CAPITAL_REDUCED
        return CAPITAL_PER_TRADE

    def get_info(self):
        return (f"Rs.{self.get_capital():.0f} "
                f"{'(REDUCED)' if self.reduced else '(NORMAL)'}")

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id":config.CHAT_ID,"text":message,"parse_mode":"HTML"
        }, timeout=10)
        if resp.status_code != 200:
            plain = message.replace("<b>","").replace("</b>","")
            requests.post(url, data={"chat_id":config.CHAT_ID,"text":plain}, timeout=10)
    except Exception as e: log.error(f"TG:{e}")

def tg(icon, title, lines):
    body = "\n".join([f"  {l}" for l in lines])
    msg  = f"{icon} <b>{title}</b>\n{body}"
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id":config.CHAT_ID,"text":msg,"parse_mode":"HTML"
        }, timeout=10)
        if resp.status_code != 200:
            requests.post(url, data={
                "chat_id":config.CHAT_ID,
                "text":f"{icon} {title}\n{body}"
            }, timeout=10)
    except Exception as e: log.error(f"TG:{e}")
    log.info(f"[TG] {title}")

def send_csv_files():
    files=[("scan_log_v4.csv","Nifty Scan v4"),("trade_log_v4.csv","Nifty Trade v4")]
    send_telegram("Nifty v4 Daily CSVs sending...")
    sent=0
    for fname,caption in files:
        path=f"/home/salverukrishna83/algo-trading/{fname}"
        if not os.path.exists(path): continue
        try:
            url=f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
            with open(path,"rb") as f:
                resp=requests.post(url,data={"chat_id":config.CHAT_ID,"caption":caption},
                                   files={"document":f},timeout=30)
            if resp.json().get("ok"): sent+=1
        except Exception as e: log.error(f"CSV:{e}")
    send_telegram(f"Sent {sent}/{len(files)} — upload to Claude!")

# ─────────────────────────────────────────────
#  TELEGRAM LISTENER
# ─────────────────────────────────────────────
class TelegramListener:
    def __init__(self):
        self.bias="neutral"; self.last_update_id=0; self._running=False

    def start(self):
        self._running=True
        threading.Thread(target=self._poll,daemon=True).start()

    def _poll(self):
        while self._running:
            try:
                url=f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
                resp=requests.get(url,params={"offset":self.last_update_id+1,"timeout":30},timeout=35)
                if resp.status_code!=200: time.sleep(5); continue
                for update in resp.json().get("result",[]):
                    self.last_update_id=update["update_id"]
                    text=update.get("message",{}).get("text","").strip().lower()
                    if text.startswith("/bias"):
                        parts=text.split()
                        if len(parts)>=2 and parts[1] in ["bullish","bearish","neutral"]:
                            self.bias=parts[1]
                            send_telegram(f"FII/DII Bias: {self.bias.upper()}")
                    elif text=="/status":
                        send_telegram(f"Nifty Bot v4 Final\nBias:{self.bias.upper()}\nIST:{now_ist().strftime('%H:%M:%S')}")
                    elif text=="/report": send_csv_files()
                    elif text=="/help":
                        send_telegram("Commands:\n/bias bullish|bearish|neutral\n/status\n/report")
            except Exception as e: log.error(f"TG poll:{e}"); time.sleep(5)

# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_nifty_ltp():
    try:
        resp=requests.get("https://api.upstox.com/v2/market-quote/ltp",
                         headers=get_headers(),params={"instrument_key":NIFTY_KEY},timeout=5)
        data=resp.json()
        if data["status"]=="success":
            key=list(data["data"].keys())[0]
            return float(data["data"][key]["last_price"])
        return None
    except Exception as e: log.error(f"LTP:{e}"); return None

def get_candles(interval_val=5):
    try:
        url=(f"https://api.upstox.com/v3/historical-candle/intraday/"
             f"{NIFTY_KEY}/minutes/{interval_val}")
        resp=requests.get(url,headers=get_headers(),timeout=10)
        data=resp.json()
        if data["status"]!="success": return None
        candles=data["data"]["candles"]
        if not candles: return None
        df=pd.DataFrame(candles,columns=["timestamp","open","high","low","close","volume","oi"])
        df["timestamp"]=pd.to_datetime(df["timestamp"])
        df=df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open","high","low","close","volume","oi"]:
            df[col]=pd.to_numeric(df[col],errors="coerce").fillna(0)
        return df
    except Exception as e: log.error(f"Candle:{e}"); return None

def get_futures_candles(interval_val=5):
    """PATCH #5: Auto-detect futures key."""
    try:
        today=datetime.date.today()
        month=today.strftime("%b").upper()[:3]
        year=today.strftime("%y")
        for key in [f"NSE_FO|NIFTY{year}{month}FUT",f"NSE_FO|NIFTY{month}{year}FUT"]:
            try:
                url=(f"https://api.upstox.com/v3/historical-candle/intraday/"
                     f"{key}/minutes/{interval_val}")
                resp=requests.get(url,headers=get_headers(),timeout=10)
                data=resp.json()
                if data["status"]!="success": continue
                candles=data["data"]["candles"]
                if not candles: continue
                df=pd.DataFrame(candles,columns=["timestamp","open","high","low","close","volume","oi"])
                df["timestamp"]=pd.to_datetime(df["timestamp"])
                df=df.sort_values("timestamp").reset_index(drop=True)
                for col in ["volume","oi"]:
                    df[col]=pd.to_numeric(df[col],errors="coerce").fillna(0)
                log.info(f"Futures:{key} {len(df)}c")
                return df
            except: continue
        return None
    except Exception as e: log.error(f"Futures:{e}"); return None

def get_prev_day_ohlc():
    try:
        today=datetime.date.today()
        from_dt=(today-datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        to_dt=today.strftime("%Y-%m-%d")
        url=(f"https://api.upstox.com/v3/historical-candle/{NIFTY_KEY}/days/1/{to_dt}/{from_dt}")
        resp=requests.get(url,headers=get_headers(),timeout=10)
        data=resp.json()
        if data["status"]!="success" or not data["data"]["candles"]: return None
        candles=data["data"]["candles"]
        prev=candles[-2] if len(candles)>=2 else candles[-1]
        return {"open":float(prev[1]),"high":float(prev[2]),
                "low":float(prev[3]),"close":float(prev[4])}
    except Exception as e: log.error(f"PrevOHLC:{e}"); return None

# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_atr(df, period=ATR_PERIOD):
    """PATCH #10: Average True Range."""
    if df is None or len(df) < period: return 0
    df = df.copy()
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = np.maximum(
        df["high"]-df["low"],
        np.maximum(abs(df["high"]-df["prev_close"]),
                   abs(df["low"]-df["prev_close"]))
    )
    return round(float(df["tr"].tail(period).mean()), 1)

def calc_rsi(df, period=RSI_PERIOD):
    """PATCH #13: RSI calculation."""
    if df is None or len(df) < period+1: return 50
    delta = df["close"].astype(float).diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period-1, adjust=False).mean()
    avg_l = loss.ewm(com=period-1, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, 1e-10)
    rsi   = 100 - (100/(1+rs))
    return round(float(rsi.iloc[-1]), 1)

def calc_vwap_bands(df, atr=0):
    """PATCH #10: Dynamic VWAP bands based on ATR."""
    df = df.copy()
    df["volume"]  = df["volume"].replace(0,1)
    df["typical"] = (df["high"]+df["low"]+df["close"])/3
    df["cum_tv"]  = (df["typical"]*df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"]/df["cum_vol"]
    df["cum_tv2"] = (((df["typical"]-df["vwap"])**2)*df["volume"]).cumsum()
    df["sd"]      = np.sqrt(df["cum_tv2"]/df["cum_vol"])
    # Dynamic multiplier based on ATR
    if atr > 30:   mult = 1.5   # trending day
    elif atr > 15: mult = 1.0   # normal day
    else:          mult = 0.75  # tight range
    df["vwap_u1"] = df["vwap"] + mult*df["sd"]
    df["vwap_l1"] = df["vwap"] - mult*df["sd"]
    df["vwap_u2"] = df["vwap"] + 2*mult*df["sd"]
    df["vwap_l2"] = df["vwap"] - 2*mult*df["sd"]
    df["band_width"] = df["vwap_u1"] - df["vwap_l1"]
    return df

def calc_ema(df, periods=[9,21,50]):
    df=df.copy()
    for p in periods:
        df[f"ema{p}"]=df["close"].astype(float).ewm(span=p,adjust=False).mean()
    return df

def calc_rvol(df, fut_df=None):
    try:
        if fut_df is not None and len(fut_df)>=5:
            vol=fut_df["volume"].astype(float)
            if vol.sum()>0 and vol.std()>0:
                avg=float(vol.mean()); cur=float(vol.iloc[-1])
                if avg>0: return round(max(0.5,min(5.0,cur/avg)),2)
        if df is not None and len(df)>=5:
            oi=df["oi"].astype(float)
            if oi.sum()>0 and oi.std()>0:
                oi_chg=oi.diff().abs().fillna(0)
                avg=float(oi_chg.mean()); cur=float(oi_chg.iloc[-1])
                if avg>0: return round(max(0.5,min(5.0,cur/avg)),2)
        return 1.2
    except: return 1.2

def detect_trend_relaxed(df, min_agree=3):
    if df is None or len(df)<4: return "neutral","Not enough",0
    recent=df.tail(4)
    highs=[float(x) for x in recent["high"].tolist()]
    lows =[float(x) for x in recent["low"].tolist()]
    hh=sum(1 for i in range(1,len(highs)) if highs[i]>highs[i-1])
    hl=sum(1 for i in range(1,len(lows))  if lows[i] >lows[i-1])
    ll=sum(1 for i in range(1,len(lows))  if lows[i] <lows[i-1])
    lh=sum(1 for i in range(1,len(highs)) if highs[i]<highs[i-1])
    bull=min(hh,hl); bear=min(ll,lh)
    if bull>=min_agree: return "bullish",f"HH:{hh} HL:{hl}",bull
    if bear>=min_agree: return "bearish",f"LL:{ll} LH:{lh}",bear
    return "neutral",f"HH:{hh} HL:{hl} LL:{ll} LH:{lh}",0

def detect_trend_multi(df5,df15,df30,e9=0,e21=0,e50=0,ltp=0):
    t5,_,_ =detect_trend_relaxed(df5)
    t15,_,_=detect_trend_relaxed(df15)
    t30,_,_=detect_trend_relaxed(df30)
    bull=[t5,t15,t30].count("bullish")
    bear=[t5,t15,t30].count("bearish")
    if bull>=2: return "bullish",f"{t5}/{t15}/{t30}","strong" if bull==3 else "moderate"
    if bear>=2: return "bearish",f"{t5}/{t15}/{t30}","strong" if bear==3 else "moderate"
    if bull==1: return "bullish",f"{t5}/{t15}/{t30}","weak"
    if bear==1: return "bearish",f"{t5}/{t15}/{t30}","weak"
    if e9>0 and e21>0 and e50>0 and ltp>0:
        if e9>e21>e50 and ltp>e21: return "bullish",f"{t5}/{t15}/{t30}","ema_confirmed"
        if e9<e21<e50 and ltp<e21: return "bearish",f"{t5}/{t15}/{t30}","ema_confirmed"
    return "neutral",f"{t5}/{t15}/{t30}","weak"

def detect_fvg(df):
    """PATCH #2: FVG with edge levels + staleness check."""
    if df is None or len(df)<3: return None,"No candles"
    candles=df.tail(15)
    for i in range(len(candles)-1,1,-1):
        c1=candles.iloc[i-2]; c2=candles.iloc[i-1]; c3=candles.iloc[i]
        body=abs(float(c2["close"])-float(c2["open"]))
        if body<MIN_FVG_BODY: continue
        c1h=float(c1["high"]); c1l=float(c1["low"])
        c3h=float(c3["high"]); c3l=float(c3["low"])
        age=len(candles)-1-i
        if c1h<c3l:
            size=round(c3l-c1h,1)
            strong=size>=STRONG_FVG_GAP and body>=STRONG_FVG_BODY
            return {"type":"bullish","top":round(c3l,1),"bottom":round(c1h,1),
                    "mid":round((c3l+c1h)/2,1),"edge":round(c3l,1),
                    "size":size,"strong":strong,"age":age}, \
                   f"{'STRONG' if strong else 'WEAK'} Bull FVG {size:.1f}pts age:{age}c"
        if c1l>c3h:
            size=round(c1l-c3h,1)
            strong=size>=STRONG_FVG_GAP and body>=STRONG_FVG_BODY
            return {"type":"bearish","top":round(c1l,1),"bottom":round(c3h,1),
                    "mid":round((c1l+c3h)/2,1),"edge":round(c3h,1),
                    "size":size,"strong":strong,"age":age}, \
                   f"{'STRONG' if strong else 'WEAK'} Bear FVG {size:.1f}pts age:{age}c"
    return None,"No FVG in last 15 candles"

def detect_orb(df,orb_high,orb_low,ltp):
    if orb_high is None or orb_low is None: return None,"ORB not formed yet"
    if ltp:
        if ltp>orb_high and round(ltp-orb_high,1)>=5:
            return {"type":"bullish","level":orb_high,"size":round(ltp-orb_high,1)}, \
                   f"ORB bullish {round(ltp-orb_high,1)}pts above {orb_high:.0f}"
        if ltp<orb_low and round(orb_low-ltp,1)>=5:
            return {"type":"bearish","level":orb_low,"size":round(orb_low-ltp,1)}, \
                   f"ORB bearish {round(orb_low-ltp,1)}pts below {orb_low:.0f}"
    return None,f"No ORB | Range {orb_low:.0f} to {orb_high:.0f}"

def detect_ema_stack(df_ema,ltp,t5):
    try:
        e9=float(df_ema["ema9"].iloc[-1]); e21=float(df_ema["ema21"].iloc[-1])
        e50=float(df_ema["ema50"].iloc[-1])
        if ltp>e9>e21>e50 and t5=="bullish":
            return {"type":"bullish","e9":round(e9,1),"e21":round(e21,1),"e50":round(e50,1)}, \
                   f"EMA Stack bull P{ltp:.0f} E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
        if ltp<e9<e21<e50 and t5=="bearish":
            return {"type":"bearish","e9":round(e9,1),"e21":round(e21,1),"e50":round(e50,1)}, \
                   f"EMA Stack bear P{ltp:.0f} E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
        return None,f"No EMA stack"
    except: return None,"EMA stack error"

def detect_ema_cross(df_ema,prev_df_ema):
    try:
        e9=float(df_ema["ema9"].iloc[-1]); e21=float(df_ema["ema21"].iloc[-1])
        pe9=float(prev_df_ema["ema9"].iloc[-1]); pe21=float(prev_df_ema["ema21"].iloc[-1])
        if pe9<=pe21 and e9>e21:
            return {"type":"bullish","e9":round(e9,1),"e21":round(e21,1)}, \
                   f"EMA9 crossed above EMA21 at {e9:.0f}"
        if pe9>=pe21 and e9<e21:
            return {"type":"bearish","e9":round(e9,1),"e21":round(e21,1)}, \
                   f"EMA9 crossed below EMA21 at {e9:.0f}"
        return None,f"No EMA cross gap {round(e9-e21,1)}pts"
    except: return None,"EMA cross error"

def detect_vwap_band_break(df_vwap,ltp,t5,atr):
    """PATCH #10: ATR filter for band break."""
    try:
        last=df_vwap.iloc[-1]; prev=df_vwap.iloc[-2]
        vu1=float(last["vwap_u1"]); vl1=float(last["vwap_l1"])
        bw =float(last.get("band_width",0))
        pltp=float(prev["close"])
        # PATCH #10: skip if ATR too low (bands too tight)
        if atr < ATR_TRENDING_MIN:
            return None,f"ATR {atr:.0f}pts too low for band break"
        # Skip band break if bands too narrow
        if bw < 30:
            return None,f"Bands too narrow {bw:.0f}pts"
        if pltp<vu1 and ltp>vu1 and t5=="bullish":
            return {"type":"bullish","level":round(vu1,1)}, \
                   f"Broke above VWAP+1SD at {vu1:.0f} ATR:{atr:.0f}"
        if pltp>vl1 and ltp<vl1 and t5=="bearish":
            return {"type":"bearish","level":round(vl1,1)}, \
                   f"Broke below VWAP-1SD at {vl1:.0f} ATR:{atr:.0f}"
        return None,f"No band break U1:{vu1:.0f} L1:{vl1:.0f} ATR:{atr:.0f}"
    except: return None,"VWAP band error"

def detect_vwap_cross(df_vwap, ltp, df_5):
    """
    PATCH #11: 2-candle confirmation + volume filter.
    Cross must:
    1. Last 2 candles both close on same side of VWAP
    2. Crossing candle volume > 1.2x average
    """
    try:
        if len(df_vwap) < 3: return None,"Not enough candles"
        last =df_vwap.iloc[-1]; prev=df_vwap.iloc[-2]; prev2=df_vwap.iloc[-3]
        vwap =float(last["vwap"])
        c1   =float(last["close"]); c2=float(prev["close"]); c3=float(prev2["close"])
        v1   =float(last["volume"]); avg_vol=float(df_5["volume"].mean())
        vol_ok=v1 > avg_vol * VWAP_CROSS_VOL_MIN

        # Bullish: last 2 candles above VWAP, prev was below
        if c3<vwap and c2>vwap and c1>vwap:
            if vol_ok:
                return {"type":"bullish","vwap":round(vwap,1)}, \
                       f"VWAP cross bull confirmed 2c at {vwap:.0f} vol:{v1:.0f}"
            return None,f"VWAP cross bull but volume low {v1:.0f}<{avg_vol*VWAP_CROSS_VOL_MIN:.0f}"

        # Bearish: last 2 candles below VWAP, prev was above
        if c3>vwap and c2<vwap and c1<vwap:
            if vol_ok:
                return {"type":"bearish","vwap":round(vwap,1)}, \
                       f"VWAP cross bear confirmed 2c at {vwap:.0f} vol:{v1:.0f}"
            return None,f"VWAP cross bear but volume low {v1:.0f}<{avg_vol*VWAP_CROSS_VOL_MIN:.0f}"

        return None,f"No VWAP cross (2-candle required) VWAP:{vwap:.0f}"
    except: return None,"VWAP cross error"

def detect_ema50_bounce(df_ema,ltp,t5,df_5):
    """PATCH #3: Candle confirmation required."""
    try:
        e50=float(df_ema["ema50"].iloc[-1]); dist=abs(ltp-e50)
        if dist>EMA50_TOLERANCE:
            return None,f"No EMA50 bounce dist:{dist:.0f}pts"
        last=df_5.iloc[-1]
        co=float(last["open"]); cc=float(last["close"])
        body=abs(cc-co)
        if t5=="bullish" and ltp>e50 and cc>co and body>5:
            return {"type":"bullish","e50":round(e50,1)}, \
                   f"EMA50 bounce bull confirmed at {e50:.0f} dist:{dist:.0f}pts"
        if t5=="bearish" and ltp<e50 and cc<co and body>5:
            return {"type":"bearish","e50":round(e50,1)}, \
                   f"EMA50 rejection bear confirmed at {e50:.0f} dist:{dist:.0f}pts"
        return None,f"EMA50 near {e50:.0f} no candle confirm"
    except: return None,"EMA50 error"

def calc_prev_vwap(prev_ohlc, open_price):
    """
    PATCH #12: Previous day VWAP.
    Only valid if today's gap < GAP_FILTER_PCT%.
    Uses simplified VWAP from prev day OHLC.
    """
    if prev_ohlc is None: return None, False
    prev_close = prev_ohlc["close"]
    if open_price > 0:
        gap_pct = abs(open_price - prev_close)/prev_close*100
        if gap_pct > GAP_FILTER_PCT:
            log.info(f"Prev VWAP invalid — gap {gap_pct:.2f}% > {GAP_FILTER_PCT}%")
            return None, False
    # Approximate prev VWAP as midpoint of prev day OHLC
    prev_vwap = round((prev_ohlc["high"]+prev_ohlc["low"]+prev_ohlc["close"])/3, 1)
    return prev_vwap, True

def is_retesting(price,bottom,top): return bottom<=price<=top

def get_option_details(nifty_price,option_type):
    atm=round(nifty_price/50)*50
    strike=atm+OTM_OFFSET if option_type=="CE" else atm-OTM_OFFSET
    today=datetime.date.today()
    days=(3-today.weekday())%7
    if days==0: days=7
    expiry=today+datetime.timedelta(days=days)
    return strike,expiry

def is_expiry_day(): return datetime.date.today().weekday()==3

# ─────────────────────────────────────────────
#  CONFIDENCE SCORER
# ─────────────────────────────────────────────
def calc_confidence(direction,trend,e9,e21,e50,ltp,
                    vwap,pcr_bias,pcr_weight,rvol,pre_bias):
    score=0; reasons=[]
    if trend==direction:
        score+=2; reasons.append(f"Trend {trend} aligned +2")
    else: reasons.append(f"Trend {trend} mismatch +0")
    if direction=="bullish" and e9>e21:
        score+=2; reasons.append(f"EMA9>EMA21 bull +2")
    elif direction=="bearish" and e9<e21:
        score+=2; reasons.append(f"EMA9<EMA21 bear +2")
    else: reasons.append(f"EMA mismatch +0")
    if direction=="bullish" and ltp>vwap:
        score+=1; reasons.append(f"Above VWAP +1")
    elif direction=="bearish" and ltp<vwap:
        score+=1; reasons.append(f"Below VWAP +1")
    else: reasons.append(f"Wrong VWAP side +0")
    if pcr_weight>=1.0:
        if (direction=="bullish" and pcr_bias=="bullish") or \
           (direction=="bearish" and pcr_bias=="bearish"):
            score+=1; reasons.append(f"PCR {pcr_bias} fresh +1")
        else: reasons.append(f"PCR {pcr_bias} no match +0")
    else: reasons.append(f"PCR {pcr_weight:.1f}w excluded +0")
    if rvol>=1.5: score+=1; reasons.append(f"RVOL {rvol}x +1")
    else: reasons.append(f"RVOL {rvol}x weak +0")
    if pre_bias==direction or pre_bias=="neutral":
        score+=1; reasons.append(f"Pre-bias {pre_bias} ok +1")
    else: reasons.append(f"Pre-bias {pre_bias} conflicts +0")
    if direction=="bullish" and ltp>e50:
        score+=1; reasons.append(f"Above EMA50 +1")
    elif direction=="bearish" and ltp<e50:
        score+=1; reasons.append(f"Below EMA50 +1")
    else: reasons.append(f"EMA50 wrong side +0")
    label="HIGH" if score>=HIGH_CONF else "MEDIUM" if score>=MEDIUM_CONF else "LOW"
    return score,label,reasons

# ─────────────────────────────────────────────
#  PAPER TRADE ENGINE
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium, signal,
                 pcr, fii_bias, pre_bias, rvol, trend_strength,
                 conf_score, conf_label, session_bias, zscore, is_strong=False):
        self.trade_no      = trade_no
        self.strategy      = strategy
        self.direction     = direction
        self.entry_price   = entry_price
        self.option_type   = option_type
        self.strike        = strike
        self.expiry        = expiry
        self.premium       = premium
        self.signal        = signal
        self.pcr           = pcr
        self.fii_bias      = fii_bias
        self.pre_bias      = pre_bias
        self.rvol          = rvol
        self.trend_strength= trend_strength
        self.conf_score    = conf_score
        self.conf_label    = conf_label
        self.session_bias  = session_bias
        self.zscore        = zscore
        self.is_strong     = is_strong
        self.entry_time    = now_ist().strftime("%H:%M:%S IST")
        self.start_time    = time.time()
        self.be_moved      = False
        self.trailing      = is_strong
        self.best_price    = entry_price
        self.sl_price      = entry_price-SL_POINTS if direction=="bullish" \
                             else entry_price+SL_POINTS
        self.tgt_price     = entry_price+TARGET_POINTS if direction=="bullish" \
                             else entry_price-TARGET_POINTS

    def check(self, ltp, t):
        # PATCH #14: Time-based exit after 2:00 PM
        if t >= TIME_EXIT_AFTER and self.duration() > 20:
            return "timeout_theta"

        if self.trailing:
            if self.direction=="bullish" and ltp>self.best_price:
                self.best_price=ltp
                if ltp-self.entry_price>=TRAIL_START:
                    new_sl=round(ltp-TRAIL_DISTANCE,1)
                    if new_sl>self.sl_price:
                        self.sl_price=new_sl
                        tg("TRAIL",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.0f} Profit:+{ltp-self.entry_price:.0f}pts SL:{new_sl:.0f}"])
            elif self.direction=="bearish" and ltp<self.best_price:
                self.best_price=ltp
                if self.entry_price-ltp>=TRAIL_START:
                    new_sl=round(ltp+TRAIL_DISTANCE,1)
                    if new_sl<self.sl_price:
                        self.sl_price=new_sl
                        tg("TRAIL",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.0f} Profit:+{self.entry_price-ltp:.0f}pts SL:{new_sl:.0f}"])
            if self.direction=="bullish" and ltp<=self.sl_price: return "sl"
            if self.direction=="bearish" and ltp>=self.sl_price: return "sl"
        else:
            if not self.be_moved:
                half=(self.entry_price+self.tgt_price)/2
                if (self.direction=="bullish" and ltp>=half) or \
                   (self.direction=="bearish" and ltp<=half):
                    self.be_moved=True; self.sl_price=self.entry_price
                    tg("LOCK",f"Trade #{self.trade_no} Breakeven",
                       [f"Nifty:{ltp:.0f} SL->{self.entry_price:.0f}"])
            if self.direction=="bullish":
                if ltp>=self.tgt_price: return "target"
                if ltp<=self.sl_price:  return "sl"
            else:
                if ltp<=self.tgt_price: return "target"
                if ltp>=self.sl_price:  return "sl"
        return None

    def duration(self): return round((time.time()-self.start_time)/60,1)
    def calc_pnl(self,exit_price):
        pts=exit_price-self.entry_price if self.direction=="bullish" \
            else self.entry_price-exit_price
        return round(pts*0.4*LOT_SIZE,0)

# ─────────────────────────────────────────────
#  CSV LOGS — PATCH #4: Fixed columns
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime","nifty_ltp","chg_from_open","chg_pct",
    "session_bias","zscore","rsi","atr",
    "trend_5m","trend_15m","trend_30m","trend_combined","trend_strength",
    "rvol","vwap","vwap_u1","vwap_l1","band_width","price_vs_vwap",
    "prev_vwap","prev_vwap_valid",
    "ema9","ema21","ema50","ema9_vs_ema21","price_vs_ema9","price_vs_ema50",
    "fvg_found","fvg_type","fvg_strong","fvg_size","fvg_age",
    "orb_high","orb_low","orb_signal","orb_size",
    "ema_stack","ema_cross","vwap_band","vwap_cross","ema50_bounce",
    "pcr","pcr_bias","pcr_status","fii_bias","overall_bias",
    "pdh","pdl","pdc",
    "capital_mode","consec_losses",
    "entry_condition_met","strategy_triggered",
    "trades_today","daily_pnl","reason"
]

TRADE_COLS = [
    "date","trade_no","strategy",
    "entry_time","exit_time",
    "conf_score","conf_label",
    "session_bias","zscore_at_entry","rsi_at_entry",
    "pre_bias","fii_bias","pcr","pcr_bias","pcr_status",
    "trend_combined","trend_strength","rvol_at_entry","atr_at_entry",
    "direction","is_strong","exit_mode",
    "entry_nifty","exit_nifty","points_moved",
    "option_type","strike","expiry",
    "premium","lots","capital_used",
    "sl_points","target_points","pnl_est","result",
    "be_triggered","trail_triggered",
    "duration_min","consec_losses","daily_pnl",
    "vwap_at_entry","prev_vwap_at_entry",
    "ema9_at_entry","ema21_at_entry","ema50_at_entry",
    "notes"
]

def init_logs():
    for fname,cols in [("scan_log_v4.csv",SCAN_COLS),("trade_log_v4.csv",TRADE_COLS)]:
        if not os.path.exists(fname):
            with open(fname,"w",newline="") as f:
                csv.DictWriter(f,fieldnames=cols).writeheader()
    log.info("Nifty v4 Final logs initialised")

def write_scan(rec):
    with open("scan_log_v4.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in SCAN_COLS}
        csv.DictWriter(f,fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open("trade_log_v4.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in TRADE_COLS}
        csv.DictWriter(f,fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats,pre_bias,pcr_cache,session_bias,cap_mgr):
    wr=(stats["wins"]/stats["trades"]*100) if stats["trades"]>0 else 0
    pcr_v,pcr_b,_,pcr_st=pcr_cache.get()
    tg("SUMMARY","Nifty v4 DAILY SUMMARY",
       [f"Session bias : {session_bias.bias.upper()}",
        f"Pre-bias     : {pre_bias.upper()}",
        f"PCR          : {pcr_v or 'N/A'} ({pcr_b}) [{pcr_st}]",
        f"Trades       : {stats['trades']}",
        f"Wins         : {stats['wins']}",
        f"Losses       : {stats['losses']}",
        f"Win rate     : {wr:.1f}%",
        f"P&L          : Rs.{stats['pnl']:+.0f}",
        f"HIGH conf WR : {stats['high_w']}/{stats['high_t']} trades",
        f"MED conf WR  : {stats['med_w']}/{stats['med_t']} trades",
        f"LOW conf WR  : {stats['low_w']}/{stats['low_t']} trades",
        f"Capital mode : {cap_mgr.get_info()}",
        f"By strategy  :",
        f"  FVG:{stats.get('fvg',0)} ORB:{stats.get('orb',0)}",
        f"  EMAStk:{stats.get('ema_stack',0)} EMACx:{stats.get('ema_cross',0)}",
        f"  VWAPCx:{stats.get('vwap_cross',0)} VWAPBand:{stats.get('vwap_band',0)}",
        f"  EMA50:{stats.get('ema50',0)}"])
    send_csv_files()

def open_trade(trade_no,strategy,direction,entry_price,
               pcr_cache,tg_listener,pre_bias,is_strong,
               signal,rvol,trend_strength,risk_level,
               conf_score,conf_label,conf_reasons,
               session_bias_obj,zscore,rsi,atr,
               vwap,prev_vwap,e9,e21,e50,capital):
    opt=("CE" if direction=="bullish" else "PE")
    strike,expiry=get_option_details(entry_price,opt)
    premium=round(capital/LOT_SIZE,1)
    pcr_v,pcr_b,_,pcr_st=pcr_cache.get()
    trade=PaperTrade(
        trade_no=trade_no,strategy=strategy,
        direction=direction,entry_price=entry_price,
        option_type=opt,strike=strike,expiry=expiry,
        premium=premium,signal=signal,pcr=pcr_v,
        fii_bias=tg_listener.bias,pre_bias=pre_bias,
        rvol=rvol,trend_strength=trend_strength,
        conf_score=conf_score,conf_label=conf_label,
        session_bias=session_bias_obj.bias,
        zscore=zscore,is_strong=is_strong
    )
    mode="Trailing" if is_strong else f"Fixed {TARGET_POINTS}pts"
    sess_info=(f"Z:{zscore:+.2f} mean-rev" if session_bias_obj.bias!=direction
               else f"Session {session_bias_obj.bias}")
    tg("ENTRY",f"PAPER TRADE #{trade_no} — {strategy}",
       [f"Direction    : {direction.upper()}",
        f"Confidence   : {conf_label} ({conf_score}/9)",
        f"Session      : {sess_info}",
        f"RSI          : {rsi:.0f} | ATR:{atr:.0f}pts",
        f"Trend        : {trend_strength}",
        f"Option       : {opt} {strike} | {expiry}",
        f"Entry        : {entry_price:.0f}",
        f"SL           : {trade.sl_price:.0f} (-{SL_POINTS}pts)",
        f"Target       : {trade.tgt_price:.0f} (+{TARGET_POINTS}pts)",
        f"Exit mode    : {mode}",
        f"RVOL         : {rvol}x",
        f"VWAP         : {vwap:.0f} ({entry_price-vwap:+.0f}pts)",
        f"Prev VWAP    : {prev_vwap:.0f}" if prev_vwap else "Prev VWAP: N/A (gap day)",
        f"EMA9/21/50   : {e9:.0f}/{e21:.0f}/{e50:.0f}",
        f"Capital      : Rs.{premium*LOT_SIZE:.0f}",
        f"Rev risk     : {risk_level}",
        f"Conf reasons : {' | '.join(conf_reasons[:3])}",
        f"NOTE         : PAPER TRADE"])
    return trade

def wait_for_retest(bottom,top,timeout_min=10):
    start=time.time()
    tg("WAIT",f"Waiting retest {bottom:.0f} to {top:.0f}",
       [f"Timeout: {timeout_min} min"])
    while time.time()-start < timeout_min*60:
        ltp=get_nifty_ltp()
        if ltp and is_retesting(ltp,bottom,top): return True,ltp
        time.sleep(15)
    return False,None

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    init_logs()
    tg_listener  = TelegramListener(); tg_listener.start()
    session_bias = SessionBias()
    pcr_cache    = PCRCache()
    cap_mgr      = CapitalManager()   # PATCH #15

    stats={
        "trades":0,"wins":0,"losses":0,"timeouts":0,
        "skipped":0,"pnl":0.0,"consec_loss":0,
        "fvg":0,"orb":0,"ema_stack":0,"vwap_band":0,
        "vwap_cross":0,"ema_cross":0,"ema50":0,
        "high_t":0,"high_w":0,"med_t":0,"med_w":0,"low_t":0,"low_w":0
    }

    trade_no       = 0
    active_trade   = None
    last_scan      = None
    pre_bias       = "neutral"
    premarket_done = False
    reminder_sent  = False
    orb_high=orb_low=None; orb_formed=False
    prev_ohlc      = None
    # PATCH #16: track per-strategy last result
    strategy_results = {}  # strategy -> list of results
    open_price     = None
    closed_summary_sent = False
    prev_df5_ema   = None

    send_telegram(
        "Nifty Bot v4 — Final Production Build\n"
        "All 16 patches applied\n"
        "NEW: ATR dynamic VWAP bands\n"
        "NEW: 2-candle VWAP cross confirmation\n"
        "NEW: Prev day VWAP with gap filter\n"
        "NEW: RSI + time exit + capital reduction\n"
        "NEW: HIGH conf re-entry allowed\n"
        f"SL:{SL_POINTS}pts TGT:{TARGET_POINTS}pts\n"
        f"Max:{MAX_TRADES} Loss:Rs.{DAILY_LOSS_LIMIT}\n\n"
        "Send /bias bullish|bearish|neutral before 9:30AM"
    )

    while True:
        t  = ist_time()
        now= now_ist()

        if not reminder_sent and REMINDER_TIME<=t<TRADE_START:
            pcr_cache.fetch()
            pcr_v,pcr_b,_,_=pcr_cache.get()
            send_telegram(
                f"Nifty opens in 30 min!\n"
                f"PCR: {pcr_v or 'N/A'} ({pcr_b})\n"
                f"Send /bias bullish|bearish|neutral"
            )
            reminder_sent=True

        if t<TRADE_START:
            if not premarket_done and t>=REMINDER_TIME:
                prev_ohlc=get_prev_day_ohlc()
                final_bias,bias_report=get_combined_bias_nifty(
                    config.LIVE_TOKEN,
                    prev_ohlc["close"] if prev_ohlc else None,
                    tg_listener.bias)
                pre_bias=final_bias
                pcr_cache.fetch()
                send_telegram(format_bias_message_nifty(bias_report))
                premarket_done=True
            time.sleep(30); continue

        premarket_done=False

        if t>=TRADE_END:
            if not closed_summary_sent:
                send_summary(stats,pre_bias,pcr_cache,session_bias,cap_mgr)
                closed_summary_sent=True
                stats={
                    "trades":0,"wins":0,"losses":0,"timeouts":0,
                    "skipped":0,"pnl":0.0,"consec_loss":0,
                    "fvg":0,"orb":0,"ema_stack":0,"vwap_band":0,
                    "vwap_cross":0,"ema_cross":0,"ema50":0,
                    "high_t":0,"high_w":0,"med_t":0,"med_w":0,"low_t":0,"low_w":0
                }
                trade_no=0; active_trade=None; last_scan=None
                pre_bias="neutral"; orb_high=orb_low=None; orb_formed=False
                strategy_results={}; tg_listener.bias="neutral"
                reminder_sent=False; premarket_done=False
                open_price=None; closed_summary_sent=False
                prev_df5_ema=None
                session_bias=SessionBias(); pcr_cache=PCRCache(); cap_mgr=CapitalManager()
            time.sleep(60); continue

        closed_summary_sent=False

        if stats["trades"]>=MAX_TRADES: time.sleep(30*60); continue
        if stats["consec_loss"]>=3:
            tg("STOP","Risk Protection",
               [f"Consecutive losses: {stats['consec_loss']}","Paused"])
            send_summary(stats,pre_bias,pcr_cache,session_bias,cap_mgr)
            time.sleep(16*3600); continue
        if stats["pnl"]<=-DAILY_LOSS_LIMIT:
            tg("STOP","Daily Loss Limit",[f"P&L:Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_cache,session_bias,cap_mgr)
            time.sleep(16*3600); continue
        if stats["pnl"]>=DAILY_PROFIT_TARGET:
            tg("DONE","Daily Profit Target!",[f"P&L:Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_cache,session_bias,cap_mgr)
            time.sleep(16*3600); continue
        if is_expiry_day() and t>=EXPIRY_STOP: time.sleep(10*60); continue

        # Monitor active trade
        if active_trade is not None:
            ltp=get_nifty_ltp(); result=None
            if ltp: result=active_trade.check(ltp,t)
            if t>=TRADE_END or result=="timeout_theta":
                result="timeout"; ltp=ltp or active_trade.entry_price
            if result:
                dur=active_trade.duration()
                pnl=active_trade.calc_pnl(ltp)
                pts=round(ltp-active_trade.entry_price,1) if active_trade.direction=="bullish" \
                    else round(active_trade.entry_price-ltp,1)
                if result=="target":
                    icon="WIN"; stats["wins"]+=1; stats["consec_loss"]=0
                elif result=="sl":
                    icon="LOSS"; stats["losses"]+=1; stats["consec_loss"]+=1
                else:
                    icon="TIME"; stats["timeouts"]+=1; stats["consec_loss"]=0
                stats["trades"]+=1; stats["pnl"]+=pnl
                # Confidence tracking
                cl=active_trade.conf_label
                if cl=="HIGH":   stats["high_t"]+=1; stats["high_w"]+=(1 if result=="target" else 0)
                elif cl=="MEDIUM":stats["med_t"]+=1; stats["med_w"]+=(1 if result=="target" else 0)
                else:            stats["low_t"]+=1;  stats["low_w"]+=(1 if result=="target" else 0)
                # PATCH #15: update capital manager
                cap_mgr.on_result(result)
                # PATCH #16: track strategy result
                strat=active_trade.strategy
                if strat not in strategy_results: strategy_results[strat]=[]
                strategy_results[strat].append(result)
                pcr_v,pcr_b,_,pcr_st=pcr_cache.get()
                tg(icon,f"TRADE #{active_trade.trade_no} {result.upper()}",
                   [f"Strategy     : {active_trade.strategy}",
                    f"Confidence   : {active_trade.conf_label} ({active_trade.conf_score}/9)",
                    f"Session bias : {active_trade.session_bias.upper()}",
                    f"Direction    : {active_trade.direction.upper()}",
                    f"Entry        : {active_trade.entry_price:.0f}",
                    f"Exit         : {ltp:.0f}",
                    f"Points       : {pts:+.1f}",
                    f"Duration     : {dur}min",
                    f"P&L          : Rs.{pnl:+.0f}",
                    f"Day P&L      : Rs.{stats['pnl']:+.0f}",
                    f"Capital next : {cap_mgr.get_info()}",
                    f"Trades today : {stats['trades']}/{MAX_TRADES}"])
                write_trade({
                    "date":datetime.date.today(),"trade_no":active_trade.trade_no,
                    "strategy":active_trade.strategy,
                    "entry_time":active_trade.entry_time,"exit_time":now.strftime("%H:%M:%S"),
                    "conf_score":active_trade.conf_score,"conf_label":active_trade.conf_label,
                    "session_bias":active_trade.session_bias,"zscore_at_entry":active_trade.zscore,
                    "pre_bias":pre_bias,"fii_bias":active_trade.fii_bias,
                    "pcr":active_trade.pcr,"pcr_bias":pcr_b,"pcr_status":pcr_st,
                    "trend_combined":active_trade.trend_strength,
                    "trend_strength":active_trade.trend_strength,
                    "rvol_at_entry":active_trade.rvol,
                    "direction":active_trade.direction,"is_strong":active_trade.is_strong,
                    "exit_mode":"Trail" if active_trade.trailing else "Fixed",
                    "entry_nifty":active_trade.entry_price,"exit_nifty":round(ltp,1),
                    "points_moved":pts,"option_type":active_trade.option_type,
                    "strike":active_trade.strike,"expiry":active_trade.expiry,
                    "premium":active_trade.premium,"lots":1,
                    "capital_used":active_trade.premium*LOT_SIZE,
                    "sl_points":SL_POINTS,"target_points":TARGET_POINTS,
                    "pnl_est":pnl,"result":result,
                    "be_triggered":active_trade.be_moved,
                    "trail_triggered":active_trade.trailing,
                    "duration_min":dur,"consec_losses":stats["consec_loss"],
                    "daily_pnl":stats["pnl"],
                    "notes":active_trade.signal
                })
                active_trade=None; time.sleep(2*60)
            else: time.sleep(15)
            continue

        # Fetch fresh data
        ltp    = get_nifty_ltp()
        df_5   = get_candles(5)
        df_15  = get_candles(15)
        df_30  = get_candles(30)
        fut_df = get_futures_candles(5)
        if ltp is None or df_5 is None: time.sleep(15); continue
        if open_price is None: open_price=ltp

        # Update session bias + Z-score
        session_bias.update(ltp,df_5)
        zscore = session_bias.get_zscore(ltp)

        # ATR + RSI
        atr = calc_atr(df_5)
        rsi = calc_rsi(df_5)

        # ORB formation
        if not orb_formed and t>=ORB_END_TIME:
            try:
                orb_df=df_5[df_5["timestamp"].dt.time<=ORB_END_TIME]
                if not orb_df.empty:
                    orb_high=float(orb_df["high"].max())
                    orb_low =float(orb_df["low"].min())
                    orb_formed=True
                    tg("ORB","Nifty ORB Formed",
                       [f"High:{orb_high:.0f} Low:{orb_low:.0f}",
                        f"Size:{orb_high-orb_low:.0f}pts",
                        f"ATR:{atr:.0f}pts RSI:{rsi:.0f}",
                        f"Session:{session_bias.bias.upper()} Z:{zscore:+.2f}"])
            except Exception as e: log.error(f"ORB:{e}")

        # PCR refresh
        if pcr_cache.should_refresh(): pcr_cache.fetch()
        pcr_v,pcr_b,pcr_weight,pcr_status=pcr_cache.get()

        # Indicators
        df5_ema  = calc_ema(df_5)
        e9=round(float(df5_ema["ema9"].iloc[-1]),1)
        e21=round(float(df5_ema["ema21"].iloc[-1]),1)
        e50=round(float(df5_ema["ema50"].iloc[-1]),1)
        trend,_,trend_strength=detect_trend_multi(df_5,df_15,df_30,e9,e21,e50,ltp)
        t5,_,_ =detect_trend_relaxed(df_5)
        t15,_,_=detect_trend_relaxed(df_15)
        t30,_,_=detect_trend_relaxed(df_30)
        rvol   =calc_rvol(df_5,fut_df)

        # VWAP with dynamic ATR bands
        df5_vwap=calc_vwap_bands(df_5,atr)
        lr=df5_vwap.iloc[-1]
        vwap=round(float(lr["vwap"]),1)
        vu1=round(float(lr["vwap_u1"]),1)
        vl1=round(float(lr["vwap_l1"]),1)
        bw=round(float(lr.get("band_width",0)),1)

        # PATCH #12: Previous day VWAP
        prev_vwap,prev_vwap_valid=calc_prev_vwap(prev_ohlc,open_price)

        # Detect strategies
        fvg,    fvg_r   =detect_fvg(df_5)
        orb_s,  orb_r   =detect_orb(df_5,orb_high,orb_low,ltp)
        ema_stk,ema_sk_r=detect_ema_stack(df5_ema,ltp,t5)
        ema_cx, ema_cx_r=detect_ema_cross(df5_ema,prev_df5_ema) \
                         if prev_df5_ema is not None else (None,"No prev EMA")
        vwap_bb,vwap_bb_r=detect_vwap_band_break(df5_vwap,ltp,t5,atr)
        vwap_cx,vwap_cx_r=detect_vwap_cross(df5_vwap,ltp,df_5)
        ema50_b,ema50_r  =detect_ema50_bounce(df5_ema,ltp,t5,df_5)
        prev_df5_ema=df5_ema.copy()

        # 5-min scan log
        do_scan=(last_scan is None or (now_ist()-last_scan).seconds>=300)
        if do_scan:
            last_scan=now_ist()
            strats=[]
            if fvg and fvg.get("strong") and fvg.get("age",99)<=FVG_MAX_AGE_CANDLES:
                strats.append("StrongFVG")
            if orb_s and orb_formed and ema_stk and orb_s["type"]==ema_stk["type"]:
                strats.append("ORB+EMA")
            if ema_stk:   strats.append("EMAStack")
            if vwap_bb:   strats.append("VWAPBand")
            if vwap_cx:   strats.append("VWAPCross")
            if ema50_b:   strats.append("EMA50Bounce")
            if ema_cx:    strats.append("EMACross")
            entry_met=len(strats)>0
            chg_open=round(ltp-open_price,1) if open_price else 0
            chg_pct =round((chg_open/open_price*100),2) if open_price else 0
            pdh=prev_ohlc["high"] if prev_ohlc else ""
            pdl=prev_ohlc["low"]  if prev_ohlc else ""
            pdc=prev_ohlc["close"]if prev_ohlc else ""
            write_scan({
                "datetime":now.strftime("%Y-%m-%d %H:%M IST"),
                "nifty_ltp":round(ltp,1),"chg_from_open":chg_open,
                "chg_pct":chg_pct,"session_bias":session_bias.bias,
                "zscore":round(zscore,2),"rsi":rsi,"atr":atr,
                "trend_5m":t5,"trend_15m":t15,"trend_30m":t30,
                "trend_combined":trend,"trend_strength":trend_strength,
                "rvol":rvol,"vwap":vwap,"vwap_u1":vu1,"vwap_l1":vl1,
                "band_width":bw,"price_vs_vwap":round(ltp-vwap,1),
                "prev_vwap":prev_vwap or "","prev_vwap_valid":prev_vwap_valid,
                "ema9":e9,"ema21":e21,"ema50":e50,
                "ema9_vs_ema21":round(e9-e21,1),
                "price_vs_ema9":round(ltp-e9,1),
                "price_vs_ema50":round(ltp-e50,1),
                "fvg_found":fvg is not None,
                "fvg_type":fvg["type"] if fvg else "",
                "fvg_strong":fvg["strong"] if fvg else "",
                "fvg_size":fvg["size"] if fvg else "",
                "fvg_age":fvg["age"] if fvg else "",
                "orb_high":orb_high or "","orb_low":orb_low or "",
                "orb_signal":orb_s["type"] if orb_s else "",
                "orb_size":orb_s["size"] if orb_s else "",
                "ema_stack":ema_stk["type"] if ema_stk else "",
                "ema_cross":ema_cx["type"] if ema_cx else "",
                "vwap_band":vwap_bb["type"] if vwap_bb else "",
                "vwap_cross":vwap_cx["type"] if vwap_cx else "",
                "ema50_bounce":ema50_b["type"] if ema50_b else "",
                "pcr":pcr_v or "","pcr_bias":pcr_b,"pcr_status":pcr_status,
                "fii_bias":tg_listener.bias,"overall_bias":pre_bias,
                "pdh":pdh,"pdl":pdl,"pdc":pdc,
                "capital_mode":cap_mgr.get_info(),
                "consec_losses":stats["consec_loss"],
                "entry_condition_met":entry_met,
                "strategy_triggered":",".join(strats),
                "trades_today":stats["trades"],"daily_pnl":stats["pnl"],
                "reason":f"FVG:{fvg_r}|ORB:{orb_r}|VWAP:{vwap_cx_r}"
            })
            icon="OK" if entry_met else "WAIT"
            tg(icon,f"NIFTY SCAN v4 {now.strftime('%H:%M')}",
               [f"Nifty        : {ltp:.0f} ({chg_pct:+.2f}%)",
                f"Session bias : {session_bias.bias.upper()} Z:{zscore:+.2f}",
                f"RSI          : {rsi:.0f} | ATR:{atr:.0f}pts",
                f"Trend        : {trend.upper()} ({trend_strength})",
                f"5m/15m/30m   : {t5}/{t15}/{t30}",
                f"RVOL         : {rvol}x",
                f"VWAP         : {vwap:.0f} ({ltp-vwap:+.0f}) BW:{bw:.0f}pts",
                f"Prev VWAP    : {prev_vwap:.0f} {'valid' if prev_vwap_valid else 'gap-day'}" if prev_vwap else "Prev VWAP: N/A",
                f"EMA9/21/50   : {e9:.0f}/{e21:.0f}/{e50:.0f}",
                f"EMA9-EMA21   : {e9-e21:+.0f}pts",
                f"FVG          : {fvg_r[:40] if fvg else 'NONE'}",
                f"ORB          : {orb_r[:40]}",
                f"EMA Stack    : {ema_sk_r[:40] if ema_stk else 'NONE'}",
                f"EMA Cross    : {ema_cx_r[:40] if ema_cx else 'NONE'}",
                f"VWAP Band    : {vwap_bb_r[:40] if vwap_bb else 'NONE'}",
                f"VWAP Cross   : {vwap_cx_r[:40] if vwap_cx else 'NONE'}",
                f"EMA50 Bnce   : {ema50_r[:40] if ema50_b else 'NONE'}",
                f"PCR          : {pcr_v or 'N/A'} ({pcr_b}) [{pcr_status}]",
                f"PDH/PDL/PDC  : {pdh:.0f}/{pdl:.0f}/{pdc:.0f}" if prev_ohlc else "PDH/PDL: N/A",
                f"Capital mode : {cap_mgr.get_info()}",
                f"Bias         : {pre_bias.upper()}",
                f"Signals      : {', '.join(strats) if strats else 'NONE'}"])

        # ── STRATEGY EXECUTOR ─────────────────────────────────────
        def can_trade(strategy_name, conf_score):
            """
            PATCH #16: Allow re-entry if HIGH confidence.
            Strategy with previous loss can re-enter only if conf >= HIGH_CONF_REENTRY.
            """
            results = strategy_results.get(strategy_name,[])
            if not results: return True  # never traded today
            last_result = results[-1]
            if last_result in ["sl","timeout"]:
                if conf_score >= HIGH_CONF_REENTRY:
                    log.info(f"{strategy_name} re-entry allowed — HIGH conf {conf_score}/9")
                    return True
                log.info(f"{strategy_name} blocked — prev loss, conf {conf_score}<{HIGH_CONF_REENTRY}")
                return False
            return True  # last trade was a win — always allow

        def try_trade(strategy_name, direction, is_strong,
                      signal_text, stat_key, retest_zone=None):
            nonlocal trade_no, active_trade

            # PATCH #1: Session bias + Z-score check
            allowed,zs,reason=session_bias.trade_allowed(direction,ltp)
            if not allowed:
                log.info(f"{strategy_name} session blocked: {reason}")
                stats["skipped"]+=1; return False

            # PATCH #13: RSI confirmation for mean reversion
            if session_bias.bias!=direction:  # counter-trend = mean reversion
                if direction=="bullish" and rsi>RSI_OVERSOLD+10:
                    log.info(f"{strategy_name} mean rev long blocked — RSI {rsi:.0f} not oversold")
                    stats["skipped"]+=1; return False
                if direction=="bearish" and rsi<RSI_OVERBOUGHT-10:
                    log.info(f"{strategy_name} mean rev short blocked — RSI {rsi:.0f} not overbought")
                    stats["skipped"]+=1; return False

            # Confidence score
            conf_score,conf_label,conf_reasons=calc_confidence(
                direction,trend,e9,e21,e50,ltp,vwap,pcr_b,pcr_weight,rvol,pre_bias)

            # PATCH #16: Re-entry check
            if not can_trade(strategy_name, conf_score):
                stats["skipped"]+=1; return False

            # Minimum confidence check
            min_conf=MIN_CONF.get(strategy_name,4)
            if conf_score<min_conf:
                log.info(f"{strategy_name} conf {conf_score}<{min_conf} skip")
                stats["skipped"]+=1; return False

            # Pre-bias check (allow if Z-score extreme)
            if pre_bias!="neutral" and pre_bias!=direction:
                if abs(zs)<ZSCORE_THRESHOLD:
                    stats["skipped"]+=1; return False

            # Reversal risk check
            proceed,risk,summary,rev_sigs=pre_trade_check_nifty(
                df_5,df_15,direction,pre_bias,
                prev_ohlc["close"] if prev_ohlc else None,prev_ohlc)
            send_telegram(format_reversal_alert_nifty(
                risk,proceed,rev_sigs,summary,strategy_name,direction))
            if not proceed:
                stats["skipped"]+=1; return False

            # Entry
            capital=cap_mgr.get_capital()  # PATCH #15
            if retest_zone:
                bottom,top=retest_zone
                retest_ok,ep=wait_for_retest(bottom,top)
                if not retest_ok:
                    tg("TIME",f"{strategy_name} retest timeout",
                       [f"Zone:{bottom:.0f} to {top:.0f}"])
                    stats["skipped"]+=1; return False
            else:
                ep=get_nifty_ltp()
                if ep is None: return False

            trade_no+=1
            active_trade=open_trade(
                trade_no,strategy_name,direction,ep,
                pcr_cache,tg_listener,pre_bias,is_strong,
                f"{signal_text} | Conf:{conf_label}({conf_score}/9) | "
                f"Sess:{session_bias.bias} Z:{zs:+.2f} RSI:{rsi:.0f}",
                rvol,trend_strength,risk,
                conf_score,conf_label,conf_reasons,
                session_bias,zs,rsi,atr,
                vwap,prev_vwap,e9,e21,e50,capital
            )
            stats[stat_key]+=1
            return True

        # ── RUN ALL 7 STRATEGIES ─────────────────────────────────

        # 1. Strong FVG — fresh only, retest at edge
        if fvg and fvg.get("strong") and fvg.get("age",99)<=FVG_MAX_AGE_CANDLES:
            edge=fvg["edge"]
            if try_trade("StrongFVG",fvg["type"],True,
                         f"Strong FVG {fvg['size']:.1f}pts age:{fvg['age']}c",
                         "fvg",retest_zone=(edge-5,edge+5)):
                time.sleep(15); continue

        # 2. ORB + EMA — session bias aligned
        if orb_s and orb_formed:
            orb_dir=orb_s["type"]
            ema_ok=(e9>e21 if orb_dir=="bullish" else e9<e21)
            sess_ok=(session_bias.bias==orb_dir or session_bias.bias=="neutral")
            if ema_ok and sess_ok:
                level=orb_s["level"]
                if try_trade("ORB+EMA",orb_dir,False,
                             f"ORB {orb_dir} {orb_s['size']:.1f}pts EMA+Sess",
                             "orb",retest_zone=(level-8,level+8)):
                    time.sleep(15); continue

        # 3. EMA Stack
        if ema_stk:
            if try_trade("EMAStack",ema_stk["type"],False,
                         f"EMA Stack {ema_stk['type']}",
                         "ema_stack"):
                time.sleep(15); continue

        # 4. VWAP Band Break (ATR filtered)
        if vwap_bb:
            if try_trade("VWAPBand",vwap_bb["type"],False,
                         f"VWAP band {vwap_bb['type']} at {vwap_bb['level']:.0f} ATR:{atr:.0f}",
                         "vwap_band"):
                time.sleep(15); continue

        # 5. VWAP Cross (2-candle confirmed)
        if vwap_cx:
            if try_trade("VWAPCross",vwap_cx["type"],False,
                         f"VWAP cross {vwap_cx['type']} 2c+vol at {vwap_cx['vwap']:.0f}",
                         "vwap_cross"):
                time.sleep(15); continue

        # 6. EMA50 Bounce (candle confirmed)
        if ema50_b:
            if try_trade("EMA50Bounce",ema50_b["type"],False,
                         f"EMA50 bounce {ema50_b['type']} at {ema50_b['e50']:.0f}",
                         "ema50"):
                time.sleep(15); continue

        # 7. EMA Cross
        if ema_cx:
            if try_trade("EMACross",ema_cx["type"],False,
                         f"EMA cross {ema_cx['type']} E9:{ema_cx['e9']:.0f}",
                         "ema_cross"):
                time.sleep(15); continue

        time.sleep(60)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Nifty Bot v4 Final stopped")
        send_telegram("Nifty Bot v4 Final stopped.")
