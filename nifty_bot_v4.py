"""
=============================================================
  Nifty 50 Scalping Bot v4 — All Patches Applied
  ─────────────────────────────────────────────────────────
  PATCHES FROM SESSION ANALYSIS:
  #1  Session bias + Z-Score + Mean Reversion
      First 30min = session direction
      Z > +2.0 = mean reversion short allowed even in bullish session
      Z < -2.0 = mean reversion long allowed even in bearish session
  #2  FVG retest at EDGE not inside gap
      Bullish FVG → entry at c3_low ± 5pts (gap top edge)
      Bearish FVG → entry at c1_low ± 5pts (gap bottom edge)
  #3  EMA50 candle confirmation required
      Last candle must close in trade direction before entry
  #4  Fixed CSV column shift in write_trade()
  #5  Nifty futures key auto-detection
  #6  VWAPCross min confidence = 3/9
  #7  PCR cache max 15 minutes
      15-30 min stale = neutral weight
      >30 min = excluded from confidence
  #8  ORB direction must match session bias
  #9  Previous day S&R in scan log (PDH/PDL/PDC)

  STRATEGIES (all 7 running for data collection):
  1. StrongFVG    — retest at gap EDGE
  2. ORB+EMA      — session bias aligned only
  3. EMAStack     — price > EMA9 > EMA21 > EMA50
  4. VWAPBand     — price breaks ±1SD band
  5. VWAPCross    — price crosses VWAP (min conf 3/9)
  6. EMA50Bounce  — candle confirmation required
  7. EMACross     — EMA9/21 cross with VWAP confirm

  CONFIDENCE SCORING (0-9):
  +2  Trend aligned with direction
  +2  EMA9/EMA21 aligned
  +1  Price on correct VWAP side
  +1  PCR confirms (fresh < 15min only)
  +1  RVOL > 1.5x
  +1  Pre-market bias matches
  +1  EMA50 correct side
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
SESSION_BIAS_END    = datetime.time(10, 0)   # first 30min for session bias
MAX_TRADES          = 15
CAPITAL_PER_TRADE   = 6500
DAILY_LOSS_LIMIT    = 3000
DAILY_PROFIT_TARGET = 2000
LOT_SIZE            = 65
OTM_OFFSET          = 100
MIN_RVOL            = 1.0
EMA50_TOLERANCE     = 20
NIFTY_KEY           = "NSE_INDEX|Nifty 50"
ZSCORE_WINDOW       = 6        # 30 min rolling Z-score
ZSCORE_THRESHOLD    = 2.0      # extreme = mean reversion possible
PCR_MAX_AGE_SECS    = 900      # 15 min fresh PCR
PCR_STALE_SECS      = 1800     # 30 min stale

# Confidence thresholds per strategy
MIN_CONF = {
    "StrongFVG"  : 6,
    "ORB+EMA"    : 5,
    "EMAStack"   : 5,
    "VWAPBand"   : 4,
    "VWAPCross"  : 3,   # PATCH #6: lower threshold
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
    return {
        "Accept"       : "application/json",
        "Authorization": f"Bearer {config.LIVE_TOKEN}"
    }

# ─────────────────────────────────────────────
#  PATCH #1: SESSION BIAS + Z-SCORE
# ─────────────────────────────────────────────
class SessionBias:
    """
    Detects overall session direction from first 30 minutes.
    Also calculates rolling Z-score for mean reversion detection.
    """
    def __init__(self):
        self.bias        = "neutral"  # bullish/bearish/neutral
        self.open_price  = None
        self.is_set      = False
        self.price_history = []       # rolling price buffer

    def update(self, ltp, df_5):
        """Update session bias from first 30min candles."""
        t = ist_time()
        if not self.is_set and t >= SESSION_BIAS_END:
            if df_5 is not None and len(df_5) >= 3:
                # Get first 30min candles
                sess_df = df_5[df_5["timestamp"].dt.time <= SESSION_BIAS_END]
                if not sess_df.empty:
                    first_open  = float(sess_df["open"].iloc[0])
                    last_close  = float(sess_df["close"].iloc[-1])
                    high_30     = float(sess_df["high"].max())
                    low_30      = float(sess_df["low"].min())
                    mid_30      = (high_30 + low_30) / 2
                    self.open_price = first_open
                    # Session bias rules:
                    # Close above mid = bullish tendency
                    # Close below mid = bearish tendency
                    if last_close > mid_30 and last_close > first_open:
                        self.bias = "bullish"
                    elif last_close < mid_30 and last_close < first_open:
                        self.bias = "bearish"
                    else:
                        self.bias = "neutral"
                    self.is_set = True
                    log.info(f"Session bias set: {self.bias} "
                             f"(open:{first_open:.0f} close:{last_close:.0f} "
                             f"mid:{mid_30:.0f})")
        # Update price history for Z-score
        self.price_history.append(ltp)
        if len(self.price_history) > ZSCORE_WINDOW * 2:
            self.price_history.pop(0)

    def get_zscore(self, ltp):
        """Calculate rolling Z-score of current price."""
        if len(self.price_history) < ZSCORE_WINDOW:
            return 0.0
        window = self.price_history[-ZSCORE_WINDOW:]
        mean   = np.mean(window)
        std    = np.std(window)
        if std == 0: return 0.0
        return round((ltp - mean) / std, 2)

    def is_mean_reversion_allowed(self, direction, ltp):
        """
        PATCH #1: Allow counter-trend trade only if Z-score extreme.
        Bullish session but SHORT trade:
          → Only if Z > +2.0 (price extended above mean)
        Bearish session but LONG trade:
          → Only if Z < -2.0 (price extended below mean)
        """
        zscore = self.get_zscore(ltp)
        if self.bias == "bullish" and direction == "bearish":
            return zscore > ZSCORE_THRESHOLD, zscore
        if self.bias == "bearish" and direction == "bullish":
            return zscore < -ZSCORE_THRESHOLD, zscore
        # Same direction as session bias = always allowed
        return True, zscore

    def trade_allowed(self, direction, ltp):
        """Check if trade direction is allowed given session bias and Z-score."""
        if not self.is_set or self.bias == "neutral":
            return True, 0.0, "Session bias neutral — all trades allowed"
        allowed, zscore = self.is_mean_reversion_allowed(direction, ltp)
        if self.bias == direction:
            return True, zscore, f"Trend trade — session {self.bias} matches {direction}"
        elif allowed:
            return True, zscore, f"Mean reversion allowed — Z-score {zscore:.2f} extreme"
        else:
            return False, zscore, (f"Counter-trend blocked — session {self.bias} "
                                   f"but trade {direction} — Z-score {zscore:.2f} not extreme")


# ─────────────────────────────────────────────
#  PATCH #7: PCR with 15-min cache
# ─────────────────────────────────────────────
class PCRCache:
    def __init__(self):
        self.val  = None
        self.bias = "neutral"
        self.time = None

    def age_seconds(self):
        if self.time is None: return 9999
        return (datetime.datetime.now() - self.time).seconds

    def fetch(self):
        """Fetch PCR with multi-expiry fallback."""
        try:
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
                    self.val  = pcr
                    self.bias = bias
                    self.time = datetime.datetime.now()
                    log.info(f"PCR fetched: {pcr} ({bias}) expiry:{expiry}")
                    return pcr, bias, "fresh"
            return self.val, self.bias, "stale"
        except Exception as e:
            log.error(f"PCR fetch: {e}")
            return self.val, self.bias, "error"

    def get(self):
        """
        PATCH #7: Return PCR with age-based weight.
        Returns (pcr_val, pcr_bias, weight, status)
        weight: 1.0=fresh, 0.5=stale, 0.0=excluded
        """
        age = self.age_seconds()
        if age < PCR_MAX_AGE_SECS:
            return self.val, self.bias, 1.0, "fresh"
        elif age < PCR_STALE_SECS:
            return self.val, "neutral", 0.5, "stale"
        else:
            return None, "neutral", 0.0, "excluded"

    def should_refresh(self):
        return self.age_seconds() >= PCR_MAX_AGE_SECS


# ─────────────────────────────────────────────
#  CONFIDENCE SCORER
# ─────────────────────────────────────────────
def calc_confidence(direction, trend, e9, e21, e50, ltp,
                    vwap, pcr_bias, pcr_weight, rvol, pre_bias):
    score   = 0
    reasons = []

    # +2: Trend aligned
    if trend == direction:
        score += 2
        reasons.append(f"Trend {trend} aligned +2")
    else:
        reasons.append(f"Trend {trend} mismatch +0")

    # +2: EMA9/EMA21 aligned
    if direction=="bullish" and e9>e21:
        score += 2
        reasons.append(f"EMA9({e9:.0f})>EMA21({e21:.0f}) bullish +2")
    elif direction=="bearish" and e9<e21:
        score += 2
        reasons.append(f"EMA9({e9:.0f})<EMA21({e21:.0f}) bearish +2")
    else:
        reasons.append(f"EMA mismatch +0")

    # +1: Price on correct VWAP side
    if direction=="bullish" and ltp>vwap:
        score += 1
        reasons.append(f"Price({ltp:.0f})>VWAP({vwap:.0f}) +1")
    elif direction=="bearish" and ltp<vwap:
        score += 1
        reasons.append(f"Price({ltp:.0f})<VWAP({vwap:.0f}) +1")
    else:
        reasons.append(f"Wrong VWAP side +0")

    # +1: PCR confirms (PATCH #7: weight-based)
    if pcr_weight >= 1.0:
        if (direction=="bullish" and pcr_bias=="bullish") or \
           (direction=="bearish" and pcr_bias=="bearish"):
            score += 1
            reasons.append(f"PCR {pcr_bias} fresh +1")
        elif pcr_bias=="neutral":
            reasons.append(f"PCR neutral +0")
        else:
            reasons.append(f"PCR conflicts +0")
    elif pcr_weight >= 0.5:
        reasons.append(f"PCR stale — not counted +0")
    else:
        reasons.append(f"PCR excluded (>30min old) +0")

    # +1: RVOL > 1.5x
    if rvol >= 1.5:
        score += 1
        reasons.append(f"RVOL {rvol}x strong +1")
    else:
        reasons.append(f"RVOL {rvol}x weak +0")

    # +1: Pre-market bias matches
    if pre_bias == direction or pre_bias == "neutral":
        score += 1
        reasons.append(f"Pre-bias {pre_bias} ok +1")
    else:
        reasons.append(f"Pre-bias {pre_bias} conflicts +0")

    # +1: EMA50 correct side
    if direction=="bullish" and ltp>e50:
        score += 1
        reasons.append(f"Price({ltp:.0f})>EMA50({e50:.0f}) +1")
    elif direction=="bearish" and ltp<e50:
        score += 1
        reasons.append(f"Price({ltp:.0f})<EMA50({e50:.0f}) +1")
    else:
        reasons.append(f"EMA50 wrong side +0")

    label = "HIGH" if score>=HIGH_CONF else "MEDIUM" if score>=MEDIUM_CONF else "LOW"
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
            requests.post(url, data={
                "chat_id": config.CHAT_ID,
                "text"   : f"{icon} {title}\n{body}"
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
                            f"Nifty Bot v4 Patched\n"
                            f"Bias: {self.bias.upper()}\n"
                            f"IST: {now_ist().strftime('%H:%M:%S')}")
                    elif text=="/report": send_csv_files()
                    elif text=="/help":
                        send_telegram(
                            "Commands:\n/bias bullish|bearish|neutral\n"
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
            params={"instrument_key":NIFTY_KEY}, timeout=5
        )
        data = resp.json()
        if data["status"]=="success":
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
        if data["status"]!="success": return None
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
    """PATCH #5: Auto-detect current month futures key."""
    try:
        today  = datetime.date.today()
        month  = today.strftime("%b").upper()[:3]
        year   = today.strftime("%y")
        # Try multiple key formats
        keys = [
            f"NSE_FO|NIFTY{year}{month}FUT",
            f"NSE_FO|NIFTY{month}{year}FUT",
        ]
        for fut_key in keys:
            try:
                url  = (f"https://api.upstox.com/v3/historical-candle/intraday/"
                        f"{fut_key}/minutes/{interval_val}")
                resp = requests.get(url, headers=get_headers(), timeout=10)
                data = resp.json()
                if data["status"]!="success": continue
                candles = data["data"]["candles"]
                if not candles: continue
                df = pd.DataFrame(candles, columns=[
                    "timestamp","open","high","low","close","volume","oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                for col in ["volume","oi"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
                log.info(f"Futures OK: {fut_key} {len(df)} candles")
                return df
            except: continue
        return None
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
    """PATCH #2: Returns FVG with edge levels for correct retest."""
    if df is None or len(df)<3: return None,"No candles"
    candles=df.tail(15)
    for i in range(len(candles)-1,1,-1):
        c1=candles.iloc[i-2]; c2=candles.iloc[i-1]; c3=candles.iloc[i]
        body=abs(float(c2["close"])-float(c2["open"]))
        if body<MIN_FVG_BODY: continue
        c1h=float(c1["high"]); c1l=float(c1["low"])
        c3h=float(c3["high"]); c3l=float(c3["low"])
        candle_age = len(candles)-1-i   # how many candles ago
        if c1h<c3l:
            size=round(c3l-c1h,1)
            strong=size>=STRONG_FVG_GAP and body>=STRONG_FVG_BODY
            return {
                "type"      : "bullish",
                "top"       : round(c3l,1),    # gap top = c3_low
                "bottom"    : round(c1h,1),    # gap bottom = c1_high
                "mid"       : round((c3l+c1h)/2,1),
                "edge"      : round(c3l,1),    # PATCH #2: retest edge = c3_low
                "size"      : size,
                "strong"    : strong,
                "age_candles": candle_age
            }, f"{'STRONG' if strong else 'WEAK'} Bullish FVG {size:.1f}pts age:{candle_age}c"
        if c1l>c3h:
            size=round(c1l-c3h,1)
            strong=size>=STRONG_FVG_GAP and body>=STRONG_FVG_BODY
            return {
                "type"      : "bearish",
                "top"       : round(c1l,1),
                "bottom"    : round(c3h,1),
                "mid"       : round((c1l+c3h)/2,1),
                "edge"      : round(c3h,1),    # PATCH #2: retest edge = c3_high
                "size"      : size,
                "strong"    : strong,
                "age_candles": candle_age
            }, f"{'STRONG' if strong else 'WEAK'} Bearish FVG {size:.1f}pts age:{candle_age}c"
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
        e9=float(df_ema["ema9"].iloc[-1])
        e21=float(df_ema["ema21"].iloc[-1])
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
        return None,f"No VWAP band break U1:{vu1:.0f} L1:{vl1:.0f}"
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

def detect_ema50_bounce(df_ema,ltp,t5,df_5):
    """PATCH #3: Require closed candle confirmation at EMA50."""
    try:
        e50=float(df_ema["ema50"].iloc[-1])
        dist=abs(ltp-e50)
        if dist>EMA50_TOLERANCE:
            return None,f"No EMA50 bounce E50:{e50:.0f} dist:{dist:.1f}pts"

        # PATCH #3: Check last candle direction
        last_candle = df_5.iloc[-1]
        candle_open = float(last_candle["open"])
        candle_close= float(last_candle["close"])
        candle_bull  = candle_close > candle_open
        candle_bear  = candle_close < candle_open
        candle_body  = abs(candle_close-candle_open)

        if t5=="bullish" and ltp>e50 and candle_bull and candle_body>5:
            return {"type":"bullish","e50":round(e50,1)}, \
                   f"EMA50 bounce confirmed bullish at {e50:.0f} dist {dist:.1f}pts"
        if t5=="bearish" and ltp<e50 and candle_bear and candle_body>5:
            return {"type":"bearish","e50":round(e50,1)}, \
                   f"EMA50 rejection confirmed bearish at {e50:.0f} dist {dist:.1f}pts"

        return None,f"EMA50 near {e50:.0f} but no candle confirmation"
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
                 conf_score, conf_label, session_bias, zscore, is_strong=False):
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
        self.trend_strength=trend_strength
        self.conf_score   = conf_score
        self.conf_label   = conf_label
        self.session_bias = session_bias
        self.zscore       = zscore
        self.is_strong    = is_strong
        self.entry_time   = now_ist().strftime("%H:%M:%S IST")
        self.start_time   = time.time()
        self.be_moved     = False
        self.trailing     = is_strong
        self.best_price   = entry_price
        self.sl_price     = entry_price-SL_POINTS if direction=="bullish" \
                            else entry_price+SL_POINTS
        self.tgt_price    = entry_price+TARGET_POINTS if direction=="bullish" \
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
                           [f"Nifty:{ltp:.0f} Profit:+{profit:.0f}pts SL:{new_sl:.0f}"])
            elif self.direction=="bearish" and ltp<self.best_price:
                self.best_price=ltp
                profit=self.entry_price-ltp
                if profit>=TRAIL_START:
                    new_sl=round(ltp+TRAIL_DISTANCE,1)
                    if new_sl<self.sl_price:
                        self.sl_price=new_sl
                        tg("TRAIL",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.0f} Profit:+{profit:.0f}pts SL:{new_sl:.0f}"])
            if self.direction=="bullish" and ltp<=self.sl_price: return "sl"
            if self.direction=="bearish" and ltp>=self.sl_price: return "sl"
        else:
            if not self.be_moved:
                half=(self.entry_price+self.tgt_price)/2
                if (self.direction=="bullish" and ltp>=half) or \
                   (self.direction=="bearish" and ltp<=half):
                    self.be_moved=True; self.sl_price=self.entry_price
                    tg("LOCK",f"Trade #{self.trade_no} Breakeven",
                       [f"Nifty:{ltp:.0f} SL moved to:{self.entry_price:.0f}"])
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
#  CSV LOGS — PATCH #4: Fixed column order
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime","nifty_ltp","chg_from_open","chg_pct",
    "session_bias","zscore",
    "trend_5m","trend_15m","trend_30m","trend_combined","trend_strength",
    "rvol","vwap","vwap_u1","vwap_l1","price_vs_vwap",
    "ema9","ema21","ema50","ema9_vs_ema21","price_vs_ema9","price_vs_ema50",
    "fvg_found","fvg_type","fvg_strong","fvg_size","fvg_age_candles",
    "orb_high","orb_low","orb_signal","orb_size",
    "ema_stack","ema_cross","vwap_band","vwap_cross","ema50_bounce",
    "pcr","pcr_bias","pcr_status","fii_bias","overall_bias",
    "pdh","pdl","pdc",
    "entry_condition_met","strategy_triggered",
    "trades_today","daily_pnl","reason"
]

# PATCH #4: Fixed column order — must match write_trade() exactly
TRADE_COLS = [
    "date","trade_no","strategy",
    "entry_time","exit_time",
    "conf_score","conf_label",
    "session_bias","zscore_at_entry",
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
    """PATCH #4: Write trade with correct column mapping."""
    with open("trade_log_v4.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in TRADE_COLS}
        csv.DictWriter(f,fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats,pre_bias,pcr_cache,session_bias):
    wr=(stats["wins"]/stats["trades"]*100) if stats["trades"]>0 else 0
    pcr_v,pcr_b,_,pcr_st = pcr_cache.get()
    tg("SUMMARY","Nifty v4 DAILY SUMMARY",
       [f"Session bias : {session_bias.bias.upper()}",
        f"Pre-bias     : {pre_bias.upper()}",
        f"PCR          : {pcr_v or 'N/A'} ({pcr_b}) [{pcr_st}]",
        f"Trades       : {stats['trades']}",
        f"Wins         : {stats['wins']}",
        f"Losses       : {stats['losses']}",
        f"Win rate     : {wr:.1f}%",
        f"P&L          : Rs.{stats['pnl']:+.0f}",
        f"HIGH conf    : {stats.get('high_conf',0)} trades",
        f"MED conf     : {stats.get('med_conf',0)} trades",
        f"LOW conf     : {stats.get('low_conf',0)} trades",
        f"By strategy  : FVG:{stats.get('fvg',0)} ORB:{stats.get('orb',0)} "
        f"EMAStk:{stats.get('ema_stack',0)} VWAP:{stats.get('vwap',0)} "
        f"EMA50:{stats.get('ema50',0)} EMACx:{stats.get('ema_cross',0)}"])
    send_csv_files()

def open_trade(trade_no,strategy,direction,entry_price,
               pcr_cache,tg_listener,pre_bias,is_strong,
               signal,rvol,trend_strength,risk_level,
               conf_score,conf_label,conf_reasons,
               session_bias_obj,zscore,vwap,e9,e21,e50):
    opt    = "CE" if direction=="bullish" else "PE"
    strike,expiry = get_option_details(entry_price,opt)
    premium= round(CAPITAL_PER_TRADE/LOT_SIZE,1)
    pcr_v,pcr_b,_,_ = pcr_cache.get()
    trade  = PaperTrade(
        trade_no=trade_no,strategy=strategy,
        direction=direction,entry_price=entry_price,
        option_type=opt,strike=strike,expiry=expiry,
        premium=premium,signal=signal,pcr=pcr_v,
        fii_bias=tg_listener.bias,pre_bias=pre_bias,
        rvol=rvol,trend_strength=trend_strength,
        conf_score=conf_score,conf_label=conf_label,
        session_bias=session_bias_obj.bias,zscore=zscore,
        is_strong=is_strong
    )
    mode="Trailing" if is_strong else f"Fixed {TARGET_POINTS}pts"
    sess_info = (f"Z-score {zscore:+.2f} mean-reversion"
                 if session_bias_obj.bias!=direction else
                 f"Session {session_bias_obj.bias} aligned")
    tg("ENTRY",f"PAPER TRADE #{trade_no} — {strategy}",
       [f"Direction    : {direction.upper()}",
        f"Confidence   : {conf_label} ({conf_score}/9)",
        f"Session      : {sess_info}",
        f"Trend        : {trend_strength}",
        f"Option       : {opt} {strike} | {expiry}",
        f"Entry        : {entry_price:.0f}",
        f"SL           : {trade.sl_price:.0f} (-{SL_POINTS}pts)",
        f"Target       : {trade.tgt_price:.0f} (+{TARGET_POINTS}pts)",
        f"Exit mode    : {mode}",
        f"RVOL         : {rvol}x",
        f"VWAP         : {vwap:.0f} ({entry_price-vwap:+.0f}pts)",
        f"EMA9/21/50   : {e9:.0f}/{e21:.0f}/{e50:.0f}",
        f"Risk         : {risk_level}",
        f"Reasons      : {' | '.join(conf_reasons[:3])}",
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
    tg_listener   = TelegramListener()
    tg_listener.start()
    session_bias  = SessionBias()
    pcr_cache     = PCRCache()

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
        "Nifty Bot v4 — All Patches Applied\n"
        "NEW: Session bias + Z-score mean reversion\n"
        "NEW: FVG retest at gap EDGE\n"
        "NEW: EMA50 candle confirmation\n"
        "NEW: PCR 15-min cache\n"
        "NEW: Fixed CSV columns\n"
        f"SL:{SL_POINTS}pts TGT:{TARGET_POINTS}pts\n"
        f"Max:{MAX_TRADES} Loss:Rs.{DAILY_LOSS_LIMIT} Profit:Rs.{DAILY_PROFIT_TARGET}\n\n"
        "Send /bias bullish|bearish|neutral before 9:30AM"
    )

    while True:
        t  = ist_time()
        now= now_ist()

        if not reminder_sent and REMINDER_TIME<=t<TRADE_START:
            pcr_v,pcr_b = pcr_cache.fetch()[:2]
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
                # Pre-fetch PCR
                pcr_cache.fetch()
                send_telegram(format_bias_message_nifty(bias_report))
                premarket_done=True
            time.sleep(30); continue

        premarket_done=False

        if t>=TRADE_END:
            if not closed_summary_sent:
                send_summary(stats,pre_bias,pcr_cache,session_bias)
                closed_summary_sent=True
                stats={
                    "trades":0,"wins":0,"losses":0,"timeouts":0,
                    "skipped":0,"pnl":0.0,"consec_loss":0,
                    "fvg":0,"orb":0,"ema_stack":0,"vwap_band":0,
                    "vwap_cross":0,"ema_cross":0,"ema50":0,
                    "high_conf":0,"med_conf":0,"low_conf":0
                }
                trade_no=0; active_trade=None; last_scan=None
                pre_bias="neutral"; orb_high=None; orb_low=None
                orb_formed=False; used_signals=set()
                tg_listener.bias="neutral"; reminder_sent=False
                premarket_done=False; open_price=None
                closed_summary_sent=False; prev_df5_ema=None
                session_bias=SessionBias(); pcr_cache=PCRCache()
            time.sleep(60); continue

        closed_summary_sent=False

        if stats["trades"]>=MAX_TRADES: time.sleep(30*60); continue
        if stats["consec_loss"]>=3:
            tg("STOP","Risk Protection",
               [f"Consecutive losses: {stats['consec_loss']}","Paused for today"])
            send_summary(stats,pre_bias,pcr_cache,session_bias)
            time.sleep(16*3600); continue
        if stats["pnl"]<=-DAILY_LOSS_LIMIT:
            tg("STOP","Daily Loss Limit",[f"P&L: Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_cache,session_bias)
            time.sleep(16*3600); continue
        if stats["pnl"]>=DAILY_PROFIT_TARGET:
            tg("DONE","Daily Profit Target!",[f"P&L: Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_cache,session_bias)
            time.sleep(16*3600); continue
        if is_expiry_day() and t>=EXPIRY_STOP: time.sleep(10*60); continue

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
                pcr_v,pcr_b,_,_ = pcr_cache.get()
                tg(icon,f"TRADE #{active_trade.trade_no} {result.upper()}",
                   [f"Strategy     : {active_trade.strategy}",
                    f"Confidence   : {active_trade.conf_label} ({active_trade.conf_score}/9)",
                    f"Session bias : {active_trade.session_bias.upper()}",
                    f"Z-score entry: {active_trade.zscore:+.2f}",
                    f"Direction    : {active_trade.direction.upper()}",
                    f"Entry        : {active_trade.entry_price:.0f}",
                    f"Exit         : {ltp:.0f}",
                    f"Points       : {pts:+.1f}",
                    f"Duration     : {duration}min",
                    f"P&L          : Rs.{pnl:+.0f}",
                    f"Day P&L      : Rs.{stats['pnl']:+.0f}",
                    f"Trades today : {stats['trades']}/{MAX_TRADES}"])
                # PATCH #4: Write trade with correct column order
                write_trade({
                    "date"          : datetime.date.today(),
                    "trade_no"      : active_trade.trade_no,
                    "strategy"      : active_trade.strategy,
                    "entry_time"    : active_trade.entry_time,
                    "exit_time"     : now.strftime("%H:%M:%S"),
                    "conf_score"    : active_trade.conf_score,
                    "conf_label"    : active_trade.conf_label,
                    "session_bias"  : active_trade.session_bias,
                    "zscore_at_entry": active_trade.zscore,
                    "pre_bias"      : pre_bias,
                    "fii_bias"      : active_trade.fii_bias,
                    "pcr"           : active_trade.pcr,
                    "pcr_bias"      : pcr_b,
                    "trend_combined": active_trade.trend_strength,
                    "trend_strength": active_trade.trend_strength,
                    "rvol_at_entry" : active_trade.rvol,
                    "direction"     : active_trade.direction,
                    "is_strong"     : active_trade.is_strong,
                    "exit_mode"     : "Trail" if active_trade.trailing else "Fixed",
                    "entry_nifty"   : active_trade.entry_price,
                    "exit_nifty"    : round(ltp,1),
                    "points_moved"  : pts,
                    "option_type"   : active_trade.option_type,
                    "strike"        : active_trade.strike,
                    "expiry"        : active_trade.expiry,
                    "premium"       : active_trade.premium,
                    "lots"          : 1,
                    "capital_used"  : active_trade.premium*LOT_SIZE,
                    "sl_points"     : SL_POINTS,
                    "target_points" : TARGET_POINTS,
                    "pnl_est"       : pnl,
                    "result"        : result,
                    "be_triggered"  : active_trade.be_moved,
                    "trail_triggered": active_trade.trailing,
                    "duration_min"  : duration,
                    "consec_losses" : stats["consec_loss"],
                    "daily_pnl"     : stats["pnl"],
                    "notes"         : active_trade.signal
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
        session_bias.update(ltp, df_5)
        zscore = session_bias.get_zscore(ltp)

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
                        f"Session bias: {session_bias.bias.upper()}",
                        f"Z-score: {zscore:+.2f}"])
            except Exception as e: log.error(f"ORB: {e}")

        # PCR refresh if needed
        if pcr_cache.should_refresh():
            pcr_cache.fetch()
        pcr_v,pcr_b,pcr_weight,pcr_status = pcr_cache.get()

        # Calculate indicators
        df5_ema  = calc_ema(df_5)
        e9  = round(float(df5_ema["ema9"].iloc[-1]),1)
        e21 = round(float(df5_ema["ema21"].iloc[-1]),1)
        e50 = round(float(df5_ema["ema50"].iloc[-1]),1)
        trend,trend_r,trend_strength=detect_trend_multi(
            df_5,df_15,df_30,e9,e21,e50,ltp)
        t5,_,_ =detect_trend_relaxed(df_5)
        t15,_,_=detect_trend_relaxed(df_15)
        t30,_,_=detect_trend_relaxed(df_30)
        rvol   =calc_rvol(df_5,fut_df)
        df5_vwap=calc_vwap_bands(df_5)
        lr      =df5_vwap.iloc[-1]
        vwap    =round(float(lr["vwap"]),1)
        vu1     =round(float(lr["vwap_u1"]),1)
        vl1     =round(float(lr["vwap_l1"]),1)

        # Detect all strategies
        fvg,    fvg_r   =detect_fvg(df_5)
        orb_s,  orb_r   =detect_orb(df_5,orb_high,orb_low,ltp)
        ema_stk,ema_sk_r=detect_ema_stack(df5_ema,ltp,t5)
        ema_cx, ema_cx_r=detect_ema_cross(df5_ema,prev_df5_ema) \
                         if prev_df5_ema is not None else (None,"No prev EMA")
        vwap_bb,vwap_bb_r=detect_vwap_band_break(df5_vwap,ltp,t5)
        vwap_cx,vwap_cx_r=detect_vwap_cross(df5_vwap,ltp)
        ema50_b,ema50_r  =detect_ema50_bounce(df5_ema,ltp,t5,df_5)
        prev_df5_ema=df5_ema.copy()

        # 5-min scan log
        do_scan=(last_scan is None or (now_ist()-last_scan).seconds>=300)
        if do_scan:
            last_scan=now_ist()
            strats=[]
            if fvg and fvg.get("strong") and fvg.get("age_candles",99)<=6:
                strats.append("StrongFVG")
            if orb_s and ema_stk and orb_s["type"]==ema_stk["type"]:
                strats.append("ORB+EMA")
            if ema_stk:   strats.append("EMAStack")
            if vwap_bb:   strats.append("VWAPBand")
            if vwap_cx:   strats.append("VWAPCross")
            if ema50_b:   strats.append("EMA50Bounce")
            if ema_cx:    strats.append("EMACross")
            entry_met=len(strats)>0
            chg_open=round(ltp-open_price,1) if open_price else 0
            chg_pct =round((chg_open/open_price*100),2) if open_price else 0

            # PATCH #9: Previous day S&R
            pdh=prev_ohlc["high"] if prev_ohlc else ""
            pdl=prev_ohlc["low"]  if prev_ohlc else ""
            pdc=prev_ohlc["close"]if prev_ohlc else ""

            write_scan({
                "datetime"       :now.strftime("%Y-%m-%d %H:%M IST"),
                "nifty_ltp"      :round(ltp,1),
                "chg_from_open"  :chg_open,
                "chg_pct"        :chg_pct,
                "session_bias"   :session_bias.bias,
                "zscore"         :round(zscore,2),
                "trend_5m"       :t5,"trend_15m":t15,
                "trend_30m"      :t30,"trend_combined":trend,
                "trend_strength" :trend_strength,"rvol":rvol,
                "vwap"           :vwap,"vwap_u1":vu1,"vwap_l1":vl1,
                "price_vs_vwap"  :round(ltp-vwap,1),
                "ema9"           :e9,"ema21":e21,"ema50":e50,
                "ema9_vs_ema21"  :round(e9-e21,1),
                "price_vs_ema9"  :round(ltp-e9,1),
                "price_vs_ema50" :round(ltp-e50,1),
                "fvg_found"      :fvg is not None,
                "fvg_type"       :fvg["type"] if fvg else "",
                "fvg_strong"     :fvg["strong"] if fvg else "",
                "fvg_size"       :fvg["size"] if fvg else "",
                "fvg_age_candles":fvg["age_candles"] if fvg else "",
                "orb_high"       :orb_high or "","orb_low":orb_low or "",
                "orb_signal"     :orb_s["type"] if orb_s else "",
                "orb_size"       :orb_s["size"] if orb_s else "",
                "ema_stack"      :ema_stk["type"] if ema_stk else "",
                "ema_cross"      :ema_cx["type"] if ema_cx else "",
                "vwap_band"      :vwap_bb["type"] if vwap_bb else "",
                "vwap_cross"     :vwap_cx["type"] if vwap_cx else "",
                "ema50_bounce"   :ema50_b["type"] if ema50_b else "",
                "pcr"            :pcr_v or "",
                "pcr_bias"       :pcr_b,
                "pcr_status"     :pcr_status,
                "fii_bias"       :tg_listener.bias,
                "overall_bias"   :pre_bias,
                "pdh"            :pdh,"pdl":pdl,"pdc":pdc,
                "entry_condition_met":entry_met,
                "strategy_triggered":",".join(strats),
                "trades_today"   :stats["trades"],
                "daily_pnl"      :stats["pnl"],
                "reason"         :f"FVG:{fvg_r}|ORB:{orb_r}|VWAP:{vwap_cx_r}"
            })
            icon="OK" if entry_met else "WAIT"
            tg(icon,f"NIFTY SCAN v4 {now.strftime('%H:%M')}",
               [f"Nifty        : {ltp:.0f} ({chg_pct:+.2f}%)",
                f"Session bias : {session_bias.bias.upper()} | Z:{zscore:+.2f}",
                f"Trend        : {trend.upper()} ({trend_strength})",
                f"5m/15m/30m   : {t5}/{t15}/{t30}",
                f"RVOL         : {rvol}x",
                f"VWAP         : {vwap:.0f} ({ltp-vwap:+.0f})",
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
                f"Bias         : {pre_bias.upper()}",
                f"PDH/PDL/PDC  : {pdh:.0f}/{pdl:.0f}/{pdc:.0f}" if prev_ohlc else "PDH/PDL/PDC: N/A",
                f"Signals      : {', '.join(strats) if strats else 'NONE'}"])

        # ── STRATEGY EXECUTOR ─────────────────────────────────────
        def try_trade(strategy_name, direction, is_strong,
                      signal_text, stat_key, retest_zone=None):
            nonlocal trade_no, active_trade
            if strategy_name in used_signals: return False

            # PATCH #1: Session bias + Z-score check
            allowed,zs,reason = session_bias.trade_allowed(direction, ltp)
            if not allowed:
                log.info(f"{strategy_name} blocked: {reason}")
                stats["skipped"]+=1; return False

            # Check minimum confidence for this strategy
            conf_score,conf_label,conf_reasons = calc_confidence(
                direction,trend,e9,e21,e50,ltp,
                vwap,pcr_b,pcr_weight,rvol,pre_bias
            )
            min_conf = MIN_CONF.get(strategy_name, 4)
            if conf_score < min_conf:
                log.info(f"{strategy_name} conf {conf_score}<{min_conf} skip")
                stats["skipped"]+=1; return False

            # Pre-bias check
            if pre_bias!="neutral" and pre_bias!=direction:
                # Allow if Z-score extreme (mean reversion)
                if abs(zs) < ZSCORE_THRESHOLD:
                    stats["skipped"]+=1; return False

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
                       [f"Zone: {bottom:.0f} to {top:.0f}"])
                    stats["skipped"]+=1; return False
            else:
                ep = get_nifty_ltp()
                if ep is None: return False

            trade_no+=1
            active_trade=open_trade(
                trade_no,strategy_name,direction,ep,
                pcr_cache,tg_listener,pre_bias,is_strong,
                f"{signal_text} | Conf:{conf_label}({conf_score}/9) | "
                f"Sess:{session_bias.bias} Z:{zs:+.2f}",
                rvol,trend_strength,risk,
                conf_score,conf_label,conf_reasons,
                session_bias,zs,vwap,e9,e21,e50
            )
            used_signals.add(strategy_name)
            stats[stat_key]+=1
            if conf_label=="HIGH": stats["high_conf"]+=1
            elif conf_label=="MEDIUM": stats["med_conf"]+=1
            else: stats["low_conf"]+=1
            return True

        # ── RUN ALL 7 STRATEGIES ─────────────────────────────────

        # 1. Strong FVG — PATCH #2: retest at edge, PATCH: staleness check
        if fvg and fvg.get("strong") and fvg.get("age_candles",99)<=6 \
           and "StrongFVG" not in used_signals:
            edge = fvg["edge"]
            # PATCH #2: retest zone at gap EDGE not inside gap
            zone = (edge-5, edge+5) if fvg["type"]=="bullish" else (edge-5, edge+5)
            if try_trade("StrongFVG",fvg["type"],True,
                         f"Strong FVG {fvg['size']:.1f}pts age:{fvg['age_candles']}c",
                         "fvg", retest_zone=zone):
                time.sleep(15); continue

        # 2. ORB + EMA — PATCH #8: session bias aligned
        if orb_s and orb_formed and "ORB+EMA" not in used_signals:
            orb_dir=orb_s["type"]
            ema_ok=(e9>e21 if orb_dir=="bullish" else e9<e21)
            # PATCH #8: ORB must match session bias
            sess_ok=(session_bias.bias==orb_dir or session_bias.bias=="neutral")
            if ema_ok and sess_ok:
                level=orb_s["level"]
                if try_trade("ORB+EMA",orb_dir,False,
                             f"ORB {orb_dir} {orb_s['size']:.1f}pts EMA+Session confirmed",
                             "orb", retest_zone=(level-8,level+8)):
                    time.sleep(15); continue

        # 3. EMA Stack
        if ema_stk and "EMAStack" not in used_signals:
            if try_trade("EMAStack",ema_stk["type"],False,
                         f"EMA Stack {ema_stk['type']}",
                         "ema_stack"):
                time.sleep(15); continue

        # 4. VWAP Band Break
        if vwap_bb and "VWAPBand" not in used_signals:
            if try_trade("VWAPBand",vwap_bb["type"],False,
                         f"VWAP band break {vwap_bb['type']} at {vwap_bb['level']:.0f}",
                         "vwap_band"):
                time.sleep(15); continue

        # 5. VWAP Cross (min conf 3/9)
        if vwap_cx and "VWAPCross" not in used_signals:
            if try_trade("VWAPCross",vwap_cx["type"],False,
                         f"VWAP cross {vwap_cx['type']} at {vwap_cx['vwap']:.0f}",
                         "vwap_cross"):
                time.sleep(15); continue

        # 6. EMA50 Bounce — PATCH #3: candle confirmation built into detector
        if ema50_b and "EMA50Bounce" not in used_signals:
            if try_trade("EMA50Bounce",ema50_b["type"],False,
                         f"EMA50 bounce {ema50_b['type']} at {ema50_b['e50']:.0f}",
                         "ema50"):
                time.sleep(15); continue

        # 7. EMA Cross
        if ema_cx and "EMACross" not in used_signals:
            if try_trade("EMACross",ema_cx["type"],False,
                         f"EMA cross {ema_cx['type']} E9:{ema_cx['e9']:.0f} E21:{ema_cx['e21']:.0f}",
                         "ema_cross"):
                time.sleep(15); continue

        time.sleep(60)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Nifty Bot v4 Patched stopped")
        send_telegram("Nifty Bot v4 Patched stopped.")
