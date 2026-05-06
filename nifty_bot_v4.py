"""
=============================================================
  Nifty 50 Scalping Bot v4 — Final with Confidence Scoring
  ─────────────────────────────────────────────────────────
  STRATEGIES (all 6 run in paper mode for data collection):
  1. StrongFVG    — Strong FVG retest
  2. ORB+EMA      — ORB breakout + EMA confirm + retest
  3. EMAStack     — Price>EMA9>EMA21>EMA50
  4. VWAPBand     — Price breaks +-1SD VWAP band
  5. VWAPCross    — Price crosses VWAP (75% WR from backtest)
  6. EMA50Bounce  — Bounce off EMA50

  CONFIDENCE SCORING (0-9):
  +2  Trend aligned with trade direction
  +2  EMA9/EMA21 aligned with direction
  +1  Price on correct side of VWAP
  +1  PCR confirms direction
  +1  RVOL > 1.5x (strong volume)
  +1  Pre-market bias matches
  +1  EMA50 on correct side
  Score 7+  = HIGH confidence
  Score 4-6 = MEDIUM confidence
  Score 0-3 = LOW confidence (still trades — learning data)

  LEARNING CYCLE:
  Run → Collect CSV → Backtest → Fix → Repeat every week
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
SL_POINTS           = 10
TARGET_POINTS       = 8
TRAIL_DISTANCE      = 10
TRAIL_START         = 15
STRONG_FVG_GAP      = 10
STRONG_FVG_BODY     = 20
MIN_FVG_BODY        = 10
ORB_END_TIME        = datetime.time(9, 45)
MAX_TRADES          = 15
CAPITAL_PER_TRADE   = 6500
DAILY_LOSS_LIMIT    = 3000
DAILY_PROFIT_TARGET = 2000
LOT_SIZE            = 65
OTM_OFFSET          = 100
MIN_RVOL            = 1.0
EMA50_TOLERANCE     = 20
NIFTY_KEY           = "NSE_INDEX|Nifty 50"

# Confidence thresholds
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
    return {
        "Accept"       : "application/json",
        "Authorization": f"Bearer {config.LIVE_TOKEN}"
    }

# ─────────────────────────────────────────────
#  CONFIDENCE SCORER
# ─────────────────────────────────────────────
def calc_confidence(direction, trend, e9, e21, e50, ltp,
                    vwap, pcr_bias, rvol, pre_bias, pcr_val):
    """
    Score 0-9 based on indicator agreement.
    Higher = more indicators confirm the trade.
    """
    score  = 0
    reasons = []

    # +2: Trend aligned
    if trend == direction:
        score += 2
        reasons.append("Trend aligned +2")
    else:
        reasons.append("Trend mismatch -0")

    # +2: EMA9/EMA21 aligned
    if direction=="bullish" and e9>e21:
        score += 2
        reasons.append("EMA9>EMA21 bullish +2")
    elif direction=="bearish" and e9<e21:
        score += 2
        reasons.append("EMA9<EMA21 bearish +2")
    else:
        reasons.append("EMA mismatch -0")

    # +1: Price on correct VWAP side
    if direction=="bullish" and ltp>vwap:
        score += 1
        reasons.append("Price above VWAP +1")
    elif direction=="bearish" and ltp<vwap:
        score += 1
        reasons.append("Price below VWAP +1")
    else:
        reasons.append("Wrong VWAP side -0")

    # +1: PCR confirms
    if (direction=="bullish" and pcr_bias=="bullish") or \
       (direction=="bearish" and pcr_bias=="bearish"):
        score += 1
        reasons.append(f"PCR confirms {pcr_bias} +1")
    elif pcr_val is None:
        reasons.append("PCR N/A -0")
    else:
        reasons.append("PCR neutral -0")

    # +1: RVOL > 1.5x
    if rvol >= 1.5:
        score += 1
        reasons.append(f"RVOL {rvol}x strong +1")
    else:
        reasons.append(f"RVOL {rvol}x weak -0")

    # +1: Pre-market bias matches
    if pre_bias == direction or pre_bias == "neutral":
        score += 1
        reasons.append(f"Pre-bias {pre_bias} ok +1")
    else:
        reasons.append(f"Pre-bias {pre_bias} conflicts -0")

    # +1: EMA50 on correct side
    if direction=="bullish" and ltp>e50:
        score += 1
        reasons.append("Price above EMA50 +1")
    elif direction=="bearish" and ltp<e50:
        score += 1
        reasons.append("Price below EMA50 +1")
    else:
        reasons.append("EMA50 wrong side -0")

    # Confidence label
    if score >= HIGH_CONF:
        label = "HIGH"
    elif score >= MEDIUM_CONF:
        label = "MEDIUM"
    else:
        label = "LOW"

    return score, label, reasons

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id"   : config.CHAT_ID,
            "text"      : message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            # Retry plain text
            plain = message.replace("<b>","").replace("</b>","") \
                           .replace("<i>","").replace("</i>","")
            requests.post(url, data={
                "chat_id": config.CHAT_ID,
                "text"   : plain
            }, timeout=10)
    except Exception as e:
        log.error(f"TG: {e}")

def tg(icon, title, lines):
    body = "\n".join([f"  {l}" for l in lines])
    msg  = f"{icon} <b>{title}</b>\n{body}"
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id"   : config.CHAT_ID,
            "text"      : msg,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            plain = f"{icon} {title}\n{body}"
            requests.post(url, data={
                "chat_id": config.CHAT_ID,
                "text"   : plain
            }, timeout=10)
    except Exception as e:
        log.error(f"TG: {e}")
    log.info(f"[TG] {title}")

def send_csv_files():
    files = [("scan_log_v4.csv","Nifty Scan v4"),
             ("trade_log_v4.csv","Nifty Trade v4")]
    send_telegram("Nifty v4 Daily CSVs sending...")
    sent = 0
    for fname, caption in files:
        path = f"/home/salverukrishna83/algo-trading/{fname}"
        if not os.path.exists(path): continue
        try:
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
            with open(path,"rb") as f:
                resp = requests.post(url, data={
                    "chat_id":config.CHAT_ID,"caption":caption
                }, files={"document":f}, timeout=30)
            if resp.json().get("ok"): sent+=1
        except Exception as e: log.error(f"CSV: {e}")
    send_telegram(f"Sent {sent}/{len(files)} — upload to Claude for analysis!")

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

    def _poll(self):
        while self._running:
            try:
                url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
                resp = requests.get(url, params={
                    "offset":self.last_update_id+1,"timeout":30
                }, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5); continue
                for update in resp.json().get("result",[]):
                    self.last_update_id = update["update_id"]
                    text = update.get("message",{}).get("text","").strip().lower()
                    if text.startswith("/bias"):
                        parts = text.split()
                        if len(parts)>=2 and parts[1] in ["bullish","bearish","neutral"]:
                            self.bias = parts[1]
                            send_telegram(f"FII/DII Bias: {self.bias.upper()}")
                    elif text=="/status":
                        send_telegram(
                            f"Nifty Bot v4 Running\n"
                            f"Bias: {self.bias.upper()}\n"
                            f"IST: {now_ist().strftime('%H:%M:%S')}")
                    elif text=="/report": send_csv_files()
                    elif text=="/help":
                        send_telegram(
                            "Commands:\n"
                            "/bias bullish|bearish|neutral\n"
                            "/status\n/report")
            except Exception as e:
                log.error(f"TG poll: {e}"); time.sleep(5)

# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_nifty_ltp():
    try:
        resp = requests.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            headers=get_headers(),
            params={"instrument_key": NIFTY_KEY}, timeout=5
        )
        data = resp.json()
        if data["status"] == "success":
            key = list(data["data"].keys())[0]
            return float(data["data"][key]["last_price"])
        return None
    except Exception as e:
        log.error(f"LTP: {e}"); return None

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
        for col in ["open","high","low","close","volume","oi"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df
    except Exception as e:
        log.error(f"Candle: {e}"); return None

def get_futures_candles(interval_val=5):
    try:
        today   = datetime.date.today()
        month   = today.strftime("%b").upper()
        year    = today.strftime("%y")
        fut_key = f"NSE_FO|NIFTY{year}{month}FUT"
        url     = (f"https://api.upstox.com/v3/historical-candle/intraday/"
                   f"{fut_key}/minutes/{interval_val}")
        resp    = requests.get(url, headers=get_headers(), timeout=10)
        data    = resp.json()
        if data["status"] != "success": return None
        candles = data["data"]["candles"]
        if not candles: return None
        df = pd.DataFrame(candles, columns=[
            "timestamp","open","high","low","close","volume","oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["volume","oi"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df
    except Exception as e:
        log.error(f"Futures: {e}"); return None

def get_prev_day_ohlc():
    try:
        today   = datetime.date.today()
        from_dt = (today-datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        to_dt   = today.strftime("%Y-%m-%d")
        url     = (f"https://api.upstox.com/v3/historical-candle/"
                   f"{NIFTY_KEY}/days/1/{to_dt}/{from_dt}")
        resp    = requests.get(url, headers=get_headers(), timeout=10)
        data    = resp.json()
        if data["status"]!="success" or not data["data"]["candles"]:
            return None
        candles = data["data"]["candles"]
        prev    = candles[-2] if len(candles)>=2 else candles[-1]
        return {"open":float(prev[1]),"high":float(prev[2]),
                "low":float(prev[3]),"close":float(prev[4])}
    except Exception as e:
        log.error(f"Prev OHLC: {e}"); return None

_pcr = {"val":None,"bias":"neutral","time":None}

def get_pcr():
    global _pcr
    try:
        now = datetime.datetime.now()
        if _pcr["time"] and (now-_pcr["time"]).seconds < 1800:
            return _pcr["val"], _pcr["bias"]
        today = datetime.date.today()
        for add_days in [0, 7, 14]:
            days_to_thu = (3-today.weekday())%7 + add_days
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
                continue
            pe_oi=ce_oi=0
            for r in data["data"]:
                pe=r.get("put_options",{})
                ce=r.get("call_options",{})
                if pe and pe.get("market_data"):
                    pe_oi+=pe["market_data"].get("oi",0)
                if ce and ce.get("market_data"):
                    ce_oi+=ce["market_data"].get("oi",0)
            if ce_oi>0:
                pcr  = round(pe_oi/ce_oi,2)
                bias = "bullish" if pcr>1.2 else "bearish" if pcr<0.8 else "neutral"
                _pcr = {"val":pcr,"bias":bias,"time":now}
                return pcr, bias
        return None,"neutral"
    except Exception as e:
        log.error(f"PCR: {e}"); return None,"neutral"

# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_vwap_bands(df):
    df = df.copy()
    df["volume"]  = df["volume"].replace(0,1)
    df["typical"] = (df["high"]+df["low"]+df["close"])/3
    df["cum_tv"]  = (df["typical"]*df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"]/df["cum_vol"]
    df["cum_tv2"] = (((df["typical"]-df["vwap"])**2)*df["volume"]).cumsum()
    df["sd"]      = np.sqrt(df["cum_tv2"]/df["cum_vol"])
    df["vwap_u1"] = df["vwap"]+df["sd"]
    df["vwap_l1"] = df["vwap"]-df["sd"]
    return df

def calc_ema(df, periods=[9,21,50]):
    df = df.copy()
    for p in periods:
        df[f"ema{p}"] = df["close"].astype(float).ewm(span=p,adjust=False).mean()
    return df

def calc_rvol(df, fut_df=None):
    try:
        if fut_df is not None and len(fut_df)>=5:
            vol = fut_df["volume"].astype(float)
            if vol.sum()>0 and vol.std()>0:
                avg=float(vol.mean()); cur=float(vol.iloc[-1])
                if avg>0: return round(max(0.5,min(5.0,cur/avg)),2)
        if df is not None and len(df)>=5:
            oi = df["oi"].astype(float)
            if oi.sum()>0 and oi.std()>0:
                oi_chg = oi.diff().abs().fillna(0)
                avg=float(oi_chg.mean()); cur=float(oi_chg.iloc[-1])
                if avg>0: return round(max(0.5,min(5.0,cur/avg)),2)
        return 1.2
    except: return 1.2

def detect_trend_relaxed(df, min_agree=3):
    if df is None or len(df)<4: return "neutral","Not enough",0
    recent = df.tail(4)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]
    hh=sum(1 for i in range(1,len(highs)) if highs[i]>highs[i-1])
    hl=sum(1 for i in range(1,len(lows))  if lows[i] >lows[i-1])
    ll=sum(1 for i in range(1,len(lows))  if lows[i] <lows[i-1])
    lh=sum(1 for i in range(1,len(highs)) if highs[i]<highs[i-1])
    bull=min(hh,hl); bear=min(ll,lh)
    if bull>=min_agree: return "bullish",f"HH:{hh} HL:{hl}",bull
    if bear>=min_agree: return "bearish",f"LL:{ll} LH:{lh}",bear
    return "neutral",f"HH:{hh} HL:{hl} LL:{ll} LH:{lh}",0

def detect_trend_multi(df5,df15,df30,e9=0,e21=0,e50=0,ltp=0):
    t5,_,_ = detect_trend_relaxed(df5)
    t15,_,_= detect_trend_relaxed(df15)
    t30,_,_= detect_trend_relaxed(df30)
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
    if df is None or len(df)<3: return None,"No candles"
    candles=df.tail(15)
    for i in range(len(candles)-1,1,-1):
        c1=candles.iloc[i-2]; c2=candles.iloc[i-1]; c3=candles.iloc[i]
        body=abs(float(c2["close"])-float(c2["open"]))
        if body<MIN_FVG_BODY: continue
        c1h=float(c1["high"]); c1l=float(c1["low"])
        c3h=float(c3["high"]); c3l=float(c3["low"])
        if c1h<c3l:
            size=round(c3l-c1h,1)
            strong=size>=STRONG_FVG_GAP and body>=STRONG_FVG_BODY
            return {"type":"bullish","top":round(c3l,1),"bottom":round(c1h,1),
                    "mid":round((c3l+c1h)/2,1),"size":size,"strong":strong}, \
                   f"{'STRONG' if strong else 'WEAK'} Bullish FVG {size:.1f}pts"
        if c1l>c3h:
            size=round(c1l-c3h,1)
            strong=size>=STRONG_FVG_GAP and body>=STRONG_FVG_BODY
            return {"type":"bearish","top":round(c1l,1),"bottom":round(c3h,1),
                    "mid":round((c1l+c3h)/2,1),"size":size,"strong":strong}, \
                   f"{'STRONG' if strong else 'WEAK'} Bearish FVG {size:.1f}pts"
    return None,"No FVG in last 15 candles"

def detect_orb(df,orb_high,orb_low,ltp):
    if orb_high is None or orb_low is None: return None,"ORB not formed yet"
    if ltp:
        diff_h=round(ltp-orb_high,1); diff_l=round(orb_low-ltp,1)
        if ltp>orb_high and diff_h>=5:
            return {"type":"bullish","level":orb_high,"size":diff_h}, \
                   f"ORB bullish breakout {diff_h}pts above {orb_high:.0f}"
        if ltp<orb_low and diff_l>=5:
            return {"type":"bearish","level":orb_low,"size":diff_l}, \
                   f"ORB bearish breakdown {diff_l}pts below {orb_low:.0f}"
    return None,f"No ORB | Range {orb_low:.0f} to {orb_high:.0f}"

def detect_ema_stack(df_ema,ltp,t5):
    try:
        e9=float(df_ema["ema9"].iloc[-1]); e21=float(df_ema["ema21"].iloc[-1])
        e50=float(df_ema["ema50"].iloc[-1])
        if ltp>e9>e21>e50 and t5=="bullish":
            return {"type":"bullish","e9":round(e9,1),"e21":round(e21,1),"e50":round(e50,1)}, \
                   f"EMA Stack bullish P{ltp:.0f} E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
        if ltp<e9<e21<e50 and t5=="bearish":
            return {"type":"bearish","e9":round(e9,1),"e21":round(e21,1),"e50":round(e50,1)}, \
                   f"EMA Stack bearish P{ltp:.0f} E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
        return None,f"No EMA stack E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
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

def detect_vwap_band_break(df_vwap,ltp,t5):
    try:
        last=df_vwap.iloc[-1]; prev=df_vwap.iloc[-2]
        vu1=float(last["vwap_u1"]); vl1=float(last["vwap_l1"])
        pltp=float(prev["close"])
        if pltp<vu1 and ltp>vu1 and t5=="bullish":
            return {"type":"bullish","level":round(vu1,1)}, \
                   f"Broke above VWAP plus1SD at {vu1:.0f}"
        if pltp>vl1 and ltp<vl1 and t5=="bearish":
            return {"type":"bearish","level":round(vl1,1)}, \
                   f"Broke below VWAP minus1SD at {vl1:.0f}"
        return None,f"No VWAP band break"
    except: return None,"VWAP band error"

def detect_vwap_cross(df_vwap,ltp):
    try:
        last=df_vwap.iloc[-1]; prev=df_vwap.iloc[-2]
        vwap=float(last["vwap"]); pvwap=float(prev["vwap"])
        pltp=float(prev["close"])
        if pltp<pvwap and ltp>vwap:
            return {"type":"bullish","vwap":round(vwap,1)}, \
                   f"VWAP reclaim bullish at {vwap:.0f}"
        if pltp>pvwap and ltp<vwap:
            return {"type":"bearish","vwap":round(vwap,1)}, \
                   f"VWAP rejection bearish at {vwap:.0f}"
        return None,f"No VWAP cross VWAP:{vwap:.0f}"
    except: return None,"VWAP cross error"

def detect_ema50_bounce(df_ema,ltp,t5):
    try:
        e50=float(df_ema["ema50"].iloc[-1]); dist=abs(ltp-e50)
        if dist<=EMA50_TOLERANCE:
            if t5=="bullish" and ltp>e50:
                return {"type":"bullish","e50":round(e50,1)}, \
                       f"EMA50 bounce support at {e50:.0f} dist {dist:.1f}pts"
            if t5=="bearish" and ltp<e50:
                return {"type":"bearish","e50":round(e50,1)}, \
                       f"EMA50 rejection at {e50:.0f} dist {dist:.1f}pts"
        return None,f"No EMA50 bounce E50:{e50:.0f} dist:{dist:.1f}pts"
    except: return None,"EMA50 error"

def is_retesting(price,bottom,top): return bottom<=price<=top

def get_option_details(nifty_price,option_type):
    atm    = round(nifty_price/50)*50
    strike = atm+OTM_OFFSET if option_type=="CE" else atm-OTM_OFFSET
    today  = datetime.date.today()
    days   = (3-today.weekday())%7
    if days==0: days=7
    expiry = today+datetime.timedelta(days=days)
    return strike,expiry

def is_expiry_day(): return datetime.date.today().weekday()==3

# ─────────────────────────────────────────────
#  PAPER TRADE ENGINE
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium, signal,
                 pcr, fii_bias, pre_bias, rvol, trend_strength,
                 conf_score, conf_label, is_strong=False):
        self.trade_no    = trade_no
        self.strategy    = strategy
        self.direction   = direction
        self.entry_price = entry_price
        self.option_type = option_type
        self.strike      = strike
        self.expiry      = expiry
        self.premium     = premium
        self.signal      = signal
        self.pcr         = pcr
        self.fii_bias    = fii_bias
        self.pre_bias    = pre_bias
        self.rvol        = rvol
        self.trend_strength = trend_strength
        self.conf_score  = conf_score
        self.conf_label  = conf_label
        self.is_strong   = is_strong
        self.entry_time  = now_ist().strftime("%H:%M:%S IST")
        self.start_time  = time.time()
        self.be_moved    = False
        self.trailing    = is_strong
        self.best_price  = entry_price
        self.sl_price    = entry_price-SL_POINTS if direction=="bullish" \
                           else entry_price+SL_POINTS
        self.tgt_price   = entry_price+TARGET_POINTS if direction=="bullish" \
                           else entry_price-TARGET_POINTS

    def check(self,ltp):
        if self.trailing:
            if self.direction=="bullish" and ltp>self.best_price:
                self.best_price=ltp
                profit=ltp-self.entry_price
                if profit>=TRAIL_START:
                    new_sl=round(ltp-TRAIL_DISTANCE,1)
                    if new_sl>self.sl_price:
                        self.sl_price=new_sl
                        tg("TRAIL",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty: {ltp:.0f}",f"Profit: +{profit:.0f}pts",
                            f"New SL: {new_sl:.0f}"])
            elif self.direction=="bearish" and ltp<self.best_price:
                self.best_price=ltp
                profit=self.entry_price-ltp
                if profit>=TRAIL_START:
                    new_sl=round(ltp+TRAIL_DISTANCE,1)
                    if new_sl<self.sl_price:
                        self.sl_price=new_sl
                        tg("TRAIL",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty: {ltp:.0f}",f"Profit: +{profit:.0f}pts",
                            f"New SL: {new_sl:.0f}"])
            if self.direction=="bullish" and ltp<=self.sl_price: return "sl"
            if self.direction=="bearish" and ltp>=self.sl_price: return "sl"
        else:
            if not self.be_moved:
                half=(self.entry_price+self.tgt_price)/2
                if (self.direction=="bullish" and ltp>=half) or \
                   (self.direction=="bearish" and ltp<=half):
                    self.be_moved=True; self.sl_price=self.entry_price
                    tg("LOCK",f"Trade #{self.trade_no} Breakeven",
                       [f"Nifty: {ltp:.0f}",
                        f"SL moved to entry: {self.entry_price:.0f}"])
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
#  CSV LOGS — includes confidence data
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime","nifty_ltp","chg_from_open","chg_pct",
    "trend_5m","trend_15m","trend_30m","trend_combined","trend_strength",
    "rvol","vwap","vwap_u1","vwap_l1","price_vs_vwap",
    "ema9","ema21","ema50","ema9_vs_ema21","price_vs_ema9","price_vs_ema50",
    "fvg_found","fvg_type","fvg_strong","fvg_size",
    "orb_high","orb_low","orb_signal","orb_size",
    "ema_stack","ema_cross","vwap_band","vwap_cross","ema50_bounce",
    "pcr","pcr_bias","fii_bias","overall_bias",
    "entry_condition_met","strategy_triggered",
    "trades_today","daily_pnl","reason"
]

TRADE_COLS = [
    "date","trade_no","strategy","entry_time","exit_time",
    "conf_score","conf_label","conf_reasons",
    "pre_bias","fii_bias","pcr","pcr_bias",
    "trend_combined","trend_strength","rvol_at_entry",
    "direction","is_strong","exit_mode",
    "entry_nifty","exit_nifty","points_moved",
    "option_type","strike","expiry",
    "premium","lots","capital_used",
    "sl_points","target_points","pnl_est","result",
    "be_triggered","trail_triggered",
    "duration_min","consec_losses","daily_pnl",
    "vwap_at_entry","ema9_at_entry","ema21_at_entry","ema50_at_entry",
    "notes"
]

def init_logs():
    for fname,cols in [("scan_log_v4.csv",SCAN_COLS),
                        ("trade_log_v4.csv",TRADE_COLS)]:
        if not os.path.exists(fname):
            with open(fname,"w",newline="") as f:
                csv.DictWriter(f,fieldnames=cols).writeheader()
    log.info("Nifty v4 logs initialised")

def write_scan(rec):
    with open("scan_log_v4.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in SCAN_COLS}
        csv.DictWriter(f,fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open("trade_log_v4.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in TRADE_COLS}
        csv.DictWriter(f,fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats,pre_bias,pcr):
    wr=(stats["wins"]/stats["trades"]*100) if stats["trades"]>0 else 0
    tg("SUMMARY","Nifty v4 DAILY SUMMARY",
       [f"Pre-bias   : {pre_bias.upper()}",
        f"PCR        : {pcr or 'N/A'}",
        f"Trades     : {stats['trades']}",
        f"Wins       : {stats['wins']}",
        f"Losses     : {stats['losses']}",
        f"Win rate   : {wr:.1f}%",
        f"P&L        : Rs.{stats['pnl']:+.0f}",
        f"HIGH conf  : {stats.get('high_conf',0)} trades",
        f"MED conf   : {stats.get('med_conf',0)} trades",
        f"LOW conf   : {stats.get('low_conf',0)} trades",
        f"By strategy:",
        f"  VWAPCross:{stats.get('vwap_cross',0)} "
        f"ORB:{stats.get('orb',0)} "
        f"FVG:{stats.get('fvg',0)}",
        f"  EMAStack:{stats.get('ema_stack',0)} "
        f"EMACross:{stats.get('ema_cross',0)} "
        f"EMA50:{stats.get('ema50',0)}"])
    send_csv_files()

def open_trade(trade_no, strategy, direction, entry_price,
               pcr_val, pcr_bias, tg_listener, pre_bias,
               is_strong, signal, rvol, trend_strength,
               risk_level, conf_score, conf_label, conf_reasons,
               vwap, e9, e21, e50):
    opt    = "CE" if direction=="bullish" else "PE"
    strike,expiry = get_option_details(entry_price,opt)
    premium= round(CAPITAL_PER_TRADE/LOT_SIZE,1)
    trade  = PaperTrade(
        trade_no=trade_no,strategy=strategy,
        direction=direction,entry_price=entry_price,
        option_type=opt,strike=strike,expiry=expiry,
        premium=premium,signal=signal,pcr=pcr_val,
        fii_bias=tg_listener.bias,pre_bias=pre_bias,
        rvol=rvol,trend_strength=trend_strength,
        conf_score=conf_score,conf_label=conf_label,
        is_strong=is_strong
    )
    conf_icon = "HIGH" if conf_label=="HIGH" else \
                "MED"  if conf_label=="MEDIUM" else "LOW"
    mode = "Trailing" if is_strong else f"Fixed {TARGET_POINTS}pts"
    tg("ENTRY",f"PAPER TRADE #{trade_no} — {strategy}",
       [f"Direction    : {direction.upper()}",
        f"Confidence   : {conf_icon} ({conf_score}/9)",
        f"Trend        : {trend_strength}",
        f"Option       : {opt} {strike} | {expiry}",
        f"Entry Nifty  : {entry_price:.0f}",
        f"SL           : {trade.sl_price:.0f} (-{SL_POINTS}pts)",
        f"Target       : {trade.tgt_price:.0f} (+{TARGET_POINTS}pts)",
        f"Exit mode    : {mode}",
        f"RVOL         : {rvol}x",
        f"VWAP         : {vwap:.0f} ({entry_price-vwap:+.0f}pts)",
        f"EMA9/21/50   : {e9:.0f}/{e21:.0f}/{e50:.0f}",
        f"Rev risk     : {risk_level}",
        f"Signal       : {signal}",
        f"Capital      : Rs.{premium*LOT_SIZE:.0f}",
        f"NOTE         : PAPER TRADE"])
    return trade

def wait_for_retest(bottom,top,timeout_min=10):
    start=time.time()
    tg("WAIT",f"Waiting retest {bottom:.0f} to {top:.0f}",
       [f"Timeout: {timeout_min} min"])
    while time.time()-start < timeout_min*60:
        ltp=get_nifty_ltp()
        if ltp and is_retesting(ltp,bottom,top):
            return True,ltp
        time.sleep(15)
    return False,None

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    init_logs()
    tg_listener=TelegramListener()
    tg_listener.start()

    stats={
        "trades":0,"wins":0,"losses":0,"timeouts":0,
        "skipped":0,"pnl":0.0,"consec_loss":0,
        "fvg":0,"orb":0,"ema_stack":0,"vwap_band":0,
        "vwap_cross":0,"ema_cross":0,"ema50":0,
        "high_conf":0,"med_conf":0,"low_conf":0
    }

    trade_no      = 0
    active_trade  = None
    last_scan     = None
    pre_bias      = "neutral"
    pcr_val       = None
    pcr_bias      = "neutral"
    premarket_done= False
    reminder_sent = False
    orb_high      = None
    orb_low       = None
    orb_formed    = False
    prev_ohlc     = None
    used_signals  = set()
    open_price    = None
    closed_summary_sent = False
    prev_df5_ema  = None

    send_telegram(
        "Nifty Scalping Bot v4 Final\n"
        "All 6 strategies running for data collection\n"
        "Confidence scoring: HIGH(7+) MED(4-6) LOW(0-3)\n"
        f"SL:{SL_POINTS}pts TGT:{TARGET_POINTS}pts\n"
        f"Max:{MAX_TRADES} Loss:Rs.{DAILY_LOSS_LIMIT} Profit:Rs.{DAILY_PROFIT_TARGET}\n\n"
        "Send /bias bullish|bearish|neutral before 9:30AM"
    )

    while True:
        t  = ist_time()
        now= now_ist()

        if not reminder_sent and REMINDER_TIME<=t<TRADE_START:
            pcr_v,pcr_b=get_pcr()
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
                    tg_listener.bias
                )
                pre_bias=final_bias
                pcr_val=bias_report["pcr_val"]
                pcr_bias=bias_report["pcr_bias"]
                send_telegram(format_bias_message_nifty(bias_report))
                premarket_done=True
            time.sleep(30); continue

        premarket_done=False

        if t>=TRADE_END:
            if not closed_summary_sent:
                send_summary(stats,pre_bias,pcr_val)
                closed_summary_sent=True
                stats={
                    "trades":0,"wins":0,"losses":0,"timeouts":0,
                    "skipped":0,"pnl":0.0,"consec_loss":0,
                    "fvg":0,"orb":0,"ema_stack":0,"vwap_band":0,
                    "vwap_cross":0,"ema_cross":0,"ema50":0,
                    "high_conf":0,"med_conf":0,"low_conf":0
                }
                trade_no=0; active_trade=None; last_scan=None
                pre_bias="neutral"; pcr_val=None; pcr_bias="neutral"
                orb_high=None; orb_low=None; orb_formed=False
                used_signals=set(); tg_listener.bias="neutral"
                reminder_sent=False; premarket_done=False
                open_price=None; closed_summary_sent=False
                prev_df5_ema=None
            time.sleep(60); continue

        closed_summary_sent=False

        if stats["trades"]>=MAX_TRADES: time.sleep(30*60); continue
        if stats["consec_loss"]>=3:
            tg("STOP","Risk Protection",
               [f"Consecutive losses: {stats['consec_loss']}",
                "Bot paused for today"])
            send_summary(stats,pre_bias,pcr_val)
            time.sleep(16*3600); continue
        if stats["pnl"]<=-DAILY_LOSS_LIMIT:
            tg("STOP","Daily Loss Limit",
               [f"P&L: Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_val)
            time.sleep(16*3600); continue
        if stats["pnl"]>=DAILY_PROFIT_TARGET:
            tg("DONE","Daily Profit Target!",
               [f"P&L: Rs.{stats['pnl']:+.0f}","Protecting gains!"])
            send_summary(stats,pre_bias,pcr_val)
            time.sleep(16*3600); continue
        if is_expiry_day() and t>=EXPIRY_STOP:
            time.sleep(10*60); continue

        # Monitor active trade
        if active_trade is not None:
            ltp=get_nifty_ltp(); result=None
            if ltp: result=active_trade.check(ltp)
            if t>=TRADE_END: result="timeout"; ltp=ltp or active_trade.entry_price
            if result:
                duration=active_trade.duration()
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
                if active_trade.conf_label=="HIGH": stats["high_conf"]+=1
                elif active_trade.conf_label=="MEDIUM": stats["med_conf"]+=1
                else: stats["low_conf"]+=1
                tg(icon,f"TRADE #{active_trade.trade_no} {result.upper()}",
                   [f"Strategy     : {active_trade.strategy}",
                    f"Confidence   : {active_trade.conf_label} ({active_trade.conf_score}/9)",
                    f"Direction    : {active_trade.direction.upper()}",
                    f"Entry        : {active_trade.entry_price:.0f}",
                    f"Exit         : {ltp:.0f}",
                    f"Points       : {pts:+.1f}",
                    f"Duration     : {duration}min",
                    f"P&L          : Rs.{pnl:+.0f}",
                    f"Day P&L      : Rs.{stats['pnl']:+.0f}",
                    f"Trades today : {stats['trades']}/{MAX_TRADES}"])
                write_trade({
                    "date":datetime.date.today(),
                    "trade_no":active_trade.trade_no,
                    "strategy":active_trade.strategy,
                    "entry_time":active_trade.entry_time,
                    "exit_time":now.strftime("%H:%M:%S"),
                    "conf_score":active_trade.conf_score,
                    "conf_label":active_trade.conf_label,
                    "conf_reasons":active_trade.signal,
                    "pre_bias":pre_bias,
                    "fii_bias":active_trade.fii_bias,
                    "pcr":active_trade.pcr,
                    "pcr_bias":pcr_bias,
                    "trend_combined":active_trade.trend_strength,
                    "trend_strength":active_trade.trend_strength,
                    "rvol_at_entry":active_trade.rvol,
                    "direction":active_trade.direction,
                    "is_strong":active_trade.is_strong,
                    "exit_mode":"Trail" if active_trade.trailing else "Fixed",
                    "entry_nifty":active_trade.entry_price,
                    "exit_nifty":round(ltp,1),
                    "points_moved":pts,
                    "option_type":active_trade.option_type,
                    "strike":active_trade.strike,
                    "expiry":active_trade.expiry,
                    "premium":active_trade.premium,
                    "lots":1,
                    "capital_used":active_trade.premium*LOT_SIZE,
                    "sl_points":SL_POINTS,
                    "target_points":TARGET_POINTS,
                    "pnl_est":pnl,"result":result,
                    "be_triggered":active_trade.be_moved,
                    "trail_triggered":active_trade.trailing,
                    "duration_min":duration,
                    "consec_losses":stats["consec_loss"],
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

        # ORB formation
        if not orb_formed and t>=ORB_END_TIME:
            try:
                orb_df=df_5[df_5["timestamp"].dt.time<=ORB_END_TIME]
                if not orb_df.empty:
                    orb_high=float(orb_df["high"].max())
                    orb_low =float(orb_df["low"].min())
                    orb_formed=True
                    tg("ORB","Nifty ORB Formed",
                       [f"High: {orb_high:.0f}",
                        f"Low: {orb_low:.0f}",
                        f"Size: {orb_high-orb_low:.0f}pts",
                        f"Nifty now: {ltp:.0f}",
                        f"vs High: {ltp-orb_high:+.0f}pts"])
            except Exception as e: log.error(f"ORB: {e}")

        # PCR refresh
        pcr_val,pcr_bias=get_pcr()

        # Indicators
        df5_ema  = calc_ema(df_5)
        e9  = round(float(df5_ema["ema9"].iloc[-1]),1)
        e21 = round(float(df5_ema["ema21"].iloc[-1]),1)
        e50 = round(float(df5_ema["ema50"].iloc[-1]),1)
        trend,trend_r,trend_strength = detect_trend_multi(
            df_5,df_15,df_30,e9,e21,e50,ltp)
        t5,_,_ = detect_trend_relaxed(df_5)
        t15,_,_= detect_trend_relaxed(df_15)
        t30,_,_= detect_trend_relaxed(df_30)
        rvol   = calc_rvol(df_5,fut_df)
        df5_vwap = calc_vwap_bands(df_5)
        lr       = df5_vwap.iloc[-1]
        vwap     = round(float(lr["vwap"]),1)
        vu1      = round(float(lr["vwap_u1"]),1)
        vl1      = round(float(lr["vwap_l1"]),1)

        # All strategies
        fvg,    fvg_r   = detect_fvg(df_5)
        orb_s,  orb_r   = detect_orb(df_5,orb_high,orb_low,ltp)
        ema_stk,ema_sk_r= detect_ema_stack(df5_ema,ltp,t5)
        ema_cx, ema_cx_r= detect_ema_cross(df5_ema,prev_df5_ema) \
                          if prev_df5_ema is not None else (None,"No prev EMA")
        vwap_bb,vwap_bb_r=detect_vwap_band_break(df5_vwap,ltp,t5)
        vwap_cx,vwap_cx_r=detect_vwap_cross(df5_vwap,ltp)
        ema50_b,ema50_r = detect_ema50_bounce(df5_ema,ltp,t5)
        prev_df5_ema=df5_ema.copy()

        # 5-min scan
        do_scan=(last_scan is None or (now_ist()-last_scan).seconds>=300)
        if do_scan:
            last_scan=now_ist()
            strats=[]
            if fvg and fvg.get("strong"):               strats.append("StrongFVG")
            if orb_s and ema_stk and \
               orb_s["type"]==ema_stk["type"]:          strats.append("ORB+EMA")
            if ema_stk:                                  strats.append("EMAStack")
            if vwap_bb:                                  strats.append("VWAPBand")
            if vwap_cx:                                  strats.append("VWAPCross")
            if ema50_b:                                  strats.append("EMA50Bounce")
            if ema_cx:                                   strats.append("EMACross")
            entry_met=len(strats)>0
            chg_open=round(ltp-open_price,1) if open_price else 0
            chg_pct =round((chg_open/open_price*100),2) if open_price else 0
            write_scan({
                "datetime":now.strftime("%Y-%m-%d %H:%M IST"),
                "nifty_ltp":round(ltp,1),"chg_from_open":chg_open,
                "chg_pct":chg_pct,"trend_5m":t5,"trend_15m":t15,
                "trend_30m":t30,"trend_combined":trend,
                "trend_strength":trend_strength,"rvol":rvol,
                "vwap":vwap,"vwap_u1":vu1,"vwap_l1":vl1,
                "price_vs_vwap":round(ltp-vwap,1),
                "ema9":e9,"ema21":e21,"ema50":e50,
                "ema9_vs_ema21":round(e9-e21,1),
                "price_vs_ema9":round(ltp-e9,1),
                "price_vs_ema50":round(ltp-e50,1),
                "fvg_found":fvg is not None,
                "fvg_type":fvg["type"] if fvg else "",
                "fvg_strong":fvg["strong"] if fvg else "",
                "fvg_size":fvg["size"] if fvg else "",
                "orb_high":orb_high or "","orb_low":orb_low or "",
                "orb_signal":orb_s["type"] if orb_s else "",
                "orb_size":orb_s["size"] if orb_s else "",
                "ema_stack":ema_stk["type"] if ema_stk else "",
                "ema_cross":ema_cx["type"] if ema_cx else "",
                "vwap_band":vwap_bb["type"] if vwap_bb else "",
                "vwap_cross":vwap_cx["type"] if vwap_cx else "",
                "ema50_bounce":ema50_b["type"] if ema50_b else "",
                "pcr":pcr_val or "","pcr_bias":pcr_bias,
                "fii_bias":tg_listener.bias,"overall_bias":pre_bias,
                "entry_condition_met":entry_met,
                "strategy_triggered":",".join(strats),
                "trades_today":stats["trades"],"daily_pnl":stats["pnl"],
                "reason":f"FVG:{fvg_r}|ORB:{orb_r}|VWAP:{vwap_cx_r}"
            })
            icon="OK" if entry_met else "WAIT"
            tg(icon,f"NIFTY SCAN v4 {now.strftime('%H:%M')}",
               [f"Nifty      : {ltp:.0f} ({chg_pct:+.2f}%)",
                f"Trend      : {trend.upper()} ({trend_strength})",
                f"5m/15m/30m : {t5}/{t15}/{t30}",
                f"RVOL       : {rvol}x",
                f"VWAP       : {vwap:.0f} ({ltp-vwap:+.0f})",
                f"EMA9/21/50 : {e9:.0f}/{e21:.0f}/{e50:.0f}",
                f"EMA9-EMA21 : {e9-e21:+.0f}pts",
                f"FVG        : {fvg_r[:40] if fvg else 'NONE'}",
                f"ORB        : {orb_r[:40]}",
                f"EMA Stack  : {ema_sk_r[:40] if ema_stk else 'NONE'}",
                f"EMA Cross  : {ema_cx_r[:40] if ema_cx else 'NONE'}",
                f"VWAP Band  : {vwap_bb_r[:40] if vwap_bb else 'NONE'}",
                f"VWAP Cross : {vwap_cx_r[:40] if vwap_cx else 'NONE'}",
                f"EMA50 Bnce : {ema50_r[:40] if ema50_b else 'NONE'}",
                f"PCR        : {pcr_val or 'N/A'} ({pcr_bias})",
                f"Bias       : {pre_bias.upper()}",
                f"Signals    : {', '.join(strats) if strats else 'NONE'}"])

        # Helper to execute any strategy
        def execute_strategy(strategy_name, direction, is_strong,
                             signal_text, stat_key, retest_zone=None):
            nonlocal trade_no, active_trade
            if strategy_name in used_signals: return False
            if pre_bias!="neutral" and pre_bias!=direction:
                stats["skipped"]+=1; return False

            # Calculate confidence score
            conf_score,conf_label,conf_reasons = calc_confidence(
                direction,trend,e9,e21,e50,ltp,
                vwap,pcr_bias,rvol,pre_bias,pcr_val
            )

            # Reversal check
            proceed,risk,summary,rev_sigs = pre_trade_check_nifty(
                df_5,df_15,direction,pre_bias,
                prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
            )
            send_telegram(format_reversal_alert_nifty(
                risk,proceed,rev_sigs,summary,strategy_name,direction))

            if not proceed:
                stats["skipped"]+=1; return False

            # Entry — with or without retest
            if retest_zone:
                bottom,top = retest_zone
                retest_ok,ep = wait_for_retest(bottom,top)
                if not retest_ok:
                    tg("TIME",f"{strategy_name} retest timeout",
                       [f"Zone: {bottom:.0f} to {top:.0f}",
                        "Discarded — fresh scan"])
                    stats["skipped"]+=1; return False
            else:
                ep = get_nifty_ltp()
                if ep is None: return False

            trade_no+=1
            active_trade = open_trade(
                trade_no,strategy_name,direction,ep,
                pcr_val,pcr_bias,tg_listener,pre_bias,
                is_strong,
                f"{signal_text} | Conf:{conf_label}({conf_score}/9) | "
                f"{' | '.join(conf_reasons[:3])}",
                rvol,trend_strength,risk,
                conf_score,conf_label,conf_reasons,
                vwap,e9,e21,e50
            )
            used_signals.add(strategy_name)
            stats[stat_key]+=1
            return True

        # ── RUN ALL 6 STRATEGIES ─────────────────────────────

        # 1. Strong FVG
        if fvg and fvg.get("strong") and "StrongFVG" not in used_signals:
            if fvg["type"]==trend:
                if execute_strategy(
                    "StrongFVG", fvg["type"], True,
                    f"Strong FVG {fvg['size']:.1f}pts {fvg['type']}",
                    "fvg",
                    retest_zone=(fvg["bottom"],fvg["top"])
                ): time.sleep(15); continue

        # 2. ORB + EMA
        if orb_s and orb_formed and "ORB+EMA" not in used_signals:
            orb_dir=orb_s["type"]
            ema_ok=(e9>e21 if orb_dir=="bullish" else e9<e21)
            if ema_ok:
                level=orb_s["level"]
                if execute_strategy(
                    "ORB+EMA", orb_dir, False,
                    f"ORB {orb_dir} {orb_s['size']:.1f}pts EMA confirmed",
                    "orb",
                    retest_zone=(level-8,level+8)
                ): time.sleep(15); continue

        # 3. EMA Stack
        if ema_stk and "EMAStack" not in used_signals:
            if execute_strategy(
                "EMAStack", ema_stk["type"], False,
                f"EMA Stack {ema_stk['type']} E9{ema_stk['e9']:.0f} E21{ema_stk['e21']:.0f}",
                "ema_stack"
            ): time.sleep(15); continue

        # 4. VWAP Band Break
        if vwap_bb and "VWAPBand" not in used_signals:
            if execute_strategy(
                "VWAPBand", vwap_bb["type"], False,
                f"VWAP band break {vwap_bb['type']} at {vwap_bb['level']:.0f}",
                "vwap_band"
            ): time.sleep(15); continue

        # 5. VWAP Cross (best backtest: 75% WR)
        if vwap_cx and "VWAPCross" not in used_signals:
            if execute_strategy(
                "VWAPCross", vwap_cx["type"], False,
                f"VWAP cross {vwap_cx['type']} at {vwap_cx['vwap']:.0f}",
                "vwap_cross"
            ): time.sleep(15); continue

        # 6. EMA50 Bounce
        if ema50_b and "EMA50Bounce" not in used_signals:
            if execute_strategy(
                "EMA50Bounce", ema50_b["type"], False,
                f"EMA50 bounce {ema50_b['type']} at {ema50_b['e50']:.0f}",
                "ema50"
            ): time.sleep(15); continue

        # 7. EMA Cross (bonus — learning data)
        if ema_cx and "EMACross" not in used_signals:
            if execute_strategy(
                "EMACross", ema_cx["type"], False,
                f"EMA cross {ema_cx['type']} E9{ema_cx['e9']:.0f} E21{ema_cx['e21']:.0f}",
                "ema_cross"
            ): time.sleep(15); continue

        time.sleep(60)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Nifty Bot v4 Final stopped")
        send_telegram("Nifty Bot v4 Final stopped.")
