"""
=============================================================
  Nifty 50 Scalping Bot v2
  ─────────────────────────────────────────────
  STRATEGIES   : 1) BOS + FVG/Breakaway Gap (Strong→Trail, Weak→Fixed)
                 2) ORB Breakout (9:15–9:45 AM)
                 3) VWAP Rejection with Volume
  MARKET DATA  : Upstox Live API
  ORDERS       : Paper trades (internal engine)
  SL / TARGET  : 10pts SL | 8pts target (fixed) or Trail 10pts (strong)
  DAILY LIMITS : Loss ₹3,000 | Profit ₹2,000
  MAX TRADES   : 15/day
  CAPITAL      : ₹6,500/trade (65 lots × ₹100 avg)
  LOGS         : scan_log_v2.csv | trade_log_v2.csv
  ALERTS       : Telegram (every action with reason)
=============================================================
"""

import time
import logging
import datetime
import csv
import os
import threading
import requests
import pandas as pd
from bs4 import BeautifulSoup
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("bot_v2.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  SCALPING PARAMETERS
# ─────────────────────────────────────────────
SL_POINTS            = 10
TARGET_POINTS        = 8
TRAIL_DISTANCE       = 10     # trail SL this many points behind price
TRAIL_START          = 15     # start trailing after 15pts profit
STRONG_FVG_GAP       = 20     # FVG gap > 20pts = strong
STRONG_FVG_BODY      = 30     # impulse body > 30pts = strong
BREAKAWAY_GAP_OPEN   = 50     # gap open > 50pts from prev close
BREAKAWAY_GAP_INTRA  = 30     # intraday gap candle body > 30pts
ORB_START            = datetime.time(9, 15)
ORB_END              = datetime.time(9, 45)
MAX_TRADES           = 15
CAPITAL_PER_TRADE    = 6500
DAILY_LOSS_LIMIT     = 3000
DAILY_PROFIT_TARGET  = 2000
LOT_SIZE             = 65
OTM_OFFSET           = 100
TRADE_START          = datetime.time(9, 30)
TRADE_END            = datetime.time(14, 30)
EXPIRY_STOP          = datetime.time(13, 0)
NIFTY_KEY            = "NSE_INDEX|Nifty 50"


# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id"   : config.CHAT_ID,
            "text"      : message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            log.warning(f"Telegram failed: {resp.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def tg(icon, title, lines):
    body = "\n".join([f"  {l}" for l in lines])
    send_telegram(f"{icon} <b>{title}</b>\n{body}")
    log.info(f"[TG] {title}")


# ─────────────────────────────────────────────
#  TELEGRAM LISTENER — /bias command
# ─────────────────────────────────────────────
class TelegramListener:
    def __init__(self):
        self.bias           = "neutral"
        self.last_update_id = 0
        self._running       = False

    def start(self):
        self._running = True
        t = threading.Thread(target=self._poll, daemon=True)
        t.start()
        log.info("📱 Telegram listener started")

    def _poll(self):
        while self._running:
            try:
                url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
                resp = requests.get(url, params={
                    "offset" : self.last_update_id + 1,
                    "timeout": 30
                }, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5); continue
                for update in resp.json().get("result", []):
                    self.last_update_id = update["update_id"]
                    text = update.get("message", {}).get("text", "").strip().lower()
                    if text.startswith("/bias"):
                        parts = text.split()
                        if len(parts) >= 2 and parts[1] in ["bullish","bearish","neutral"]:
                            self.bias = parts[1]
                            send_telegram(f"✅ <b>Bias: {self.bias.upper()}</b>")
                    elif text == "/status":
                        send_telegram(
                            f"🤖 <b>Bot v2 Status</b>\n"
                            f"  Running  : ✅\n"
                            f"  FII Bias : {self.bias.upper()}\n"
                            f"  Time     : {datetime.datetime.now().strftime('%H:%M:%S')}"
                        )
                    elif text == "/help":
                        send_telegram(
                            "📋 <b>Commands</b>\n"
                            "  /bias bullish\n"
                            "  /bias bearish\n"
                            "  /bias neutral\n"
                            "  /status"
                        )
            except Exception as e:
                log.error(f"TG poll error: {e}")
                time.sleep(5)


# ─────────────────────────────────────────────
#  UPSTOX MARKET DATA
# ─────────────────────────────────────────────
def get_headers():
    return {
        "Accept"       : "application/json",
        "Authorization": f"Bearer {config.LIVE_TOKEN}"
    }

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
        log.error(f"LTP error: {e}")
        return None

def get_candles(interval_val=5):
    try:
        url  = (f"https://api.upstox.com/v3/historical-candle/intraday/"
                f"{NIFTY_KEY}/minutes/{interval_val}")
        resp = requests.get(url, headers=get_headers(), timeout=10)
        data = resp.json()
        if data["status"] != "success":
            return None
        candles = data["data"]["candles"]
        if not candles:
            return None
        df = pd.DataFrame(candles, columns=[
            "timestamp","open","high","low","close","volume","oi"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        log.error(f"Candle error: {e}")
        return None

def get_prev_day_ohlc():
    """Get previous day OHLC for breakaway gap and ORB."""
    try:
        today    = datetime.date.today()
        from_dt  = (today - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        to_dt    = today.strftime("%Y-%m-%d")
        url      = (f"https://api.upstox.com/v3/historical-candle/"
                    f"{NIFTY_KEY}/days/1/{to_dt}/{from_dt}")
        resp     = requests.get(url, headers=get_headers(), timeout=10)
        data     = resp.json()
        if data["status"] != "success" or not data["data"]["candles"]:
            return None
        candles  = data["data"]["candles"]
        prev     = candles[-2] if len(candles) >= 2 else candles[-1]
        return {
            "open" : float(prev[1]),
            "high" : float(prev[2]),
            "low"  : float(prev[3]),
            "close": float(prev[4])
        }
    except Exception as e:
        log.error(f"Prev OHLC error: {e}")
        return None

def get_pcr():
    try:
        today       = datetime.date.today()
        days_to_thu = (3 - today.weekday()) % 7
        if days_to_thu == 0: days_to_thu = 7
        expiry = today + datetime.timedelta(days=days_to_thu)
        resp   = requests.get(
            "https://api.upstox.com/v2/option/chain",
            headers=get_headers(),
            params={"instrument_key": NIFTY_KEY,
                    "expiry_date": expiry.strftime("%Y-%m-%d")},
            timeout=10
        )
        data = resp.json()
        if data["status"] != "success" or not data.get("data"):
            return None, "neutral"
        pe_oi = ce_oi = 0
        for r in data["data"]:
            pe = r.get("put_options",  {})
            ce = r.get("call_options", {})
            if pe and pe.get("market_data"): pe_oi += pe["market_data"].get("oi", 0)
            if ce and ce.get("market_data"): ce_oi += ce["market_data"].get("oi", 0)
        if ce_oi == 0: return None, "neutral"
        pcr  = round(pe_oi / ce_oi, 2)
        bias = "bullish" if pcr > 1.2 else "bearish" if pcr < 0.8 else "neutral"
        return pcr, bias
    except Exception as e:
        log.error(f"PCR error: {e}")
        return None, "neutral"

def fetch_news_sentiment():
    try:
        resp  = requests.get(
            "https://www.moneycontrol.com/news/business/markets/",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        soup  = BeautifulSoup(resp.text, "html.parser")
        heads = []
        for tag in soup.find_all(["h2","h3"], limit=30):
            text = tag.get_text(strip=True)
            if len(text) > 30 and any(w in text.lower() for w in
               ["nifty","sensex","market","rally","fall","stock"]):
                heads.append(text[:120])
        heads = list(dict.fromkeys(heads))[:8]
        bull  = ["rally","surge","gain","rise","bullish","positive","strong","up","boost"]
        bear  = ["fall","drop","decline","bearish","negative","weak","down","crash","pressure"]
        score = sum(1 for h in heads for w in bull if w in h.lower()) - \
                sum(1 for h in heads for w in bear if w in h.lower())
        sent  = "bullish" if score >= 3 else "bearish" if score <= -3 else "neutral"
        return heads, sent, score
    except Exception as e:
        log.error(f"News error: {e}")
        return [], "neutral", 0

def compute_bias(fii, pcr, news):
    m = {"bullish":1,"neutral":0,"bearish":-1}
    s = m.get(fii,0)*0.4 + m.get(pcr,0)*0.4 + m.get(news,0)*0.2
    return "bullish" if s >= 0.4 else "bearish" if s <= -0.4 else "neutral"


# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_vwap(df):
    """Calculate VWAP from DataFrame."""
    df = df.copy()
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tv"]  = (df["typical"] * df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"] / df["cum_vol"]
    return df

def calc_ema(df, period, col="close"):
    df[f"ema{period}"] = df[col].ewm(span=period, adjust=False).mean()
    return df

def detect_trend(df):
    if df is None or len(df) < 4:
        return "neutral", "Not enough candles"
    recent = df.tail(4)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]
    hh = all(highs[i] > highs[i-1] for i in range(1, len(highs)))
    hl = all(lows[i]  > lows[i-1]  for i in range(1, len(lows)))
    ll = all(lows[i]  < lows[i-1]  for i in range(1, len(lows)))
    lh = all(highs[i] < highs[i-1] for i in range(1, len(highs)))
    if hh and hl: return "bullish", f"HH+HL | H:{[round(h,1) for h in highs]}"
    if ll and lh: return "bearish", f"LL+LH | L:{[round(l,1) for l in lows]}"
    return "neutral", f"No structure | H:{[round(h,1) for h in highs]}"

def detect_bos(df, trend):
    """
    Break of Structure:
    Bullish BOS: price closes above last swing high
    Bearish BOS: price closes below last swing low
    """
    if df is None or len(df) < 6:
        return False, 0
    recent     = df.tail(10)
    last_close = float(recent["close"].iloc[-1])
    if trend == "bullish":
        swing_high = float(recent["high"].iloc[:-1].max())
        if last_close > swing_high:
            return True, swing_high
    elif trend == "bearish":
        swing_low = float(recent["low"].iloc[:-1].min())
        if last_close < swing_low:
            return True, swing_low
    return False, 0

def detect_fvg(df):
    """Detect FVG and classify as strong or weak."""
    if df is None or len(df) < 3:
        return None, "Not enough candles"
    candles = df.tail(15)
    for i in range(len(candles)-1, 1, -1):
        c1   = candles.iloc[i-2]
        c2   = candles.iloc[i-1]
        c3   = candles.iloc[i]
        body = abs(float(c2["close"]) - float(c2["open"]))
        if body < 15: continue
        c1h  = float(c1["high"]); c1l = float(c1["low"])
        c3h  = float(c3["high"]); c3l = float(c3["low"])

        if c1h < c3l:
            size   = round(c3l - c1h, 2)
            strong = size > STRONG_FVG_GAP and body > STRONG_FVG_BODY
            fvg    = {"type":"bullish","top":round(c3l,2),
                      "bottom":round(c1h,2),"mid":round((c3l+c1h)/2,2),
                      "size":size,"strong":strong,
                      "time":str(c3["timestamp"])}
            return fvg, f"{'STRONG' if strong else 'WEAK'} Bullish FVG | Gap:{size}pts | Body:{body:.1f}pts"

        if c1l > c3h:
            size   = round(c1l - c3h, 2)
            strong = size > STRONG_FVG_GAP and body > STRONG_FVG_BODY
            fvg    = {"type":"bearish","top":round(c1l,2),
                      "bottom":round(c3h,2),"mid":round((c1l+c3h)/2,2),
                      "size":size,"strong":strong,
                      "time":str(c3["timestamp"])}
            return fvg, f"{'STRONG' if strong else 'WEAK'} Bearish FVG | Gap:{size}pts | Body:{body:.1f}pts"
    return None, "No FVG in last 15 candles"

def detect_breakaway_gap(df, prev_close):
    """
    Detects breakaway gap:
    Type 1: Gap open > 50pts from prev close
    Type 2: Intraday candle body > 30pts with no overlap to prev candle
    """
    if df is None or len(df) < 2:
        return None, "Not enough candles"

    # Type 1: Gap open
    first_open = float(df["open"].iloc[0])
    if prev_close:
        gap = abs(first_open - prev_close)
        if gap >= BREAKAWAY_GAP_OPEN:
            direction = "bullish" if first_open > prev_close else "bearish"
            return {
                "type"     : direction,
                "gap_type" : "gap_open",
                "size"     : round(gap, 2),
                "level"    : round(prev_close, 2),
                "strong"   : True
            }, f"Gap open {direction} | {gap:.1f}pts from prev close {prev_close:.1f}"

    # Type 2: Intraday breakaway candle
    candles = df.tail(10)
    for i in range(len(candles)-1, 0, -1):
        curr = candles.iloc[i]
        prev = candles.iloc[i-1]
        body = abs(float(curr["close"]) - float(curr["open"]))
        if body < BREAKAWAY_GAP_INTRA:
            continue
        # No overlap check
        if float(curr["low"]) > float(prev["high"]):
            return {
                "type"     : "bullish",
                "gap_type" : "intraday",
                "size"     : round(body, 2),
                "level"    : float(prev["high"]),
                "strong"   : True
            }, f"Intraday breakaway bullish | Body:{body:.1f}pts"
        if float(curr["high"]) < float(prev["low"]):
            return {
                "type"     : "bearish",
                "gap_type" : "intraday",
                "size"     : round(body, 2),
                "level"    : float(prev["low"]),
                "strong"   : True
            }, f"Intraday breakaway bearish | Body:{body:.1f}pts"

    return None, "No breakaway gap detected"

def detect_orb(df, orb_high, orb_low):
    """
    ORB: Check if price has broken above/below the 9:15-9:45 range.
    Returns signal dict or None.
    """
    if orb_high is None or orb_low is None:
        return None, "ORB range not formed yet"
    if df is None or len(df) < 1:
        return None, "No candles"

    last  = df.iloc[-1]
    close = float(last["close"])
    body  = abs(float(last["close"]) - float(last["open"]))

    if close > orb_high and body >= 20:
        return {
            "type"     : "bullish",
            "level"    : orb_high,
            "size"     : round(close - orb_high, 2)
        }, f"ORB breakout bullish | Close:{close:.1f} > High:{orb_high:.1f}"

    if close < orb_low and body >= 20:
        return {
            "type"     : "bearish",
            "level"    : orb_low,
            "size"     : round(orb_low - close, 2)
        }, f"ORB breakdown bearish | Close:{close:.1f} < Low:{orb_low:.1f}"

    return None, f"No ORB breakout | Range:{orb_low:.1f}-{orb_high:.1f} | Price:{close:.1f}"

def detect_vwap_rejection(df_5, df_15):
    """
    VWAP rejection with volume:
    - 15min confirms trend
    - 5min price bounces off VWAP with volume > avg volume
    """
    if df_5 is None or len(df_5) < 5:
        return None, "Not enough 5min candles"
    if df_15 is None or len(df_15) < 4:
        return None, "Not enough 15min candles"

    trend_15, _ = detect_trend(df_15)
    if trend_15 == "neutral":
        return None, "15min trend neutral — no VWAP trade"

    df_5  = calc_vwap(df_5)
    avg_vol = float(df_5["volume"].mean())
    last    = df_5.iloc[-1]
    prev    = df_5.iloc[-2]

    vwap      = float(last["vwap"])
    close     = float(last["close"])
    low       = float(last["low"])
    high      = float(last["high"])
    volume    = float(last["volume"])
    vol_surge = volume > avg_vol * 1.5   # 50% above average

    # Bullish rejection: price dips to VWAP and bounces up with volume
    if trend_15 == "bullish":
        touched_vwap = low <= vwap <= high or float(prev["low"]) <= vwap
        bounced      = close > vwap
        if touched_vwap and bounced and vol_surge:
            return {
                "type"      : "bullish",
                "vwap"      : round(vwap, 2),
                "volume"    : round(volume, 0),
                "avg_volume": round(avg_vol, 0)
            }, f"VWAP bullish bounce | VWAP:{vwap:.1f} | Vol:{volume:.0f} vs Avg:{avg_vol:.0f}"

    # Bearish rejection: price rises to VWAP and rejects with volume
    if trend_15 == "bearish":
        touched_vwap = low <= vwap <= high or float(prev["high"]) >= vwap
        rejected     = close < vwap
        if touched_vwap and rejected and vol_surge:
            return {
                "type"      : "bearish",
                "vwap"      : round(vwap, 2),
                "volume"    : round(volume, 0),
                "avg_volume": round(avg_vol, 0)
            }, f"VWAP bearish rejection | VWAP:{vwap:.1f} | Vol:{volume:.0f} vs Avg:{avg_vol:.0f}"

    return None, f"No VWAP rejection | VWAP:{vwap:.1f} | Close:{close:.1f} | Vol surge:{vol_surge}"

def is_retesting(price, level_bottom, level_top):
    return level_bottom <= price <= level_top

def get_option_details(nifty_price, option_type):
    atm         = round(nifty_price / 50) * 50
    strike      = atm + OTM_OFFSET if option_type == "CE" else atm - OTM_OFFSET
    today       = datetime.date.today()
    days_to_thu = (3 - today.weekday()) % 7
    if days_to_thu == 0: days_to_thu = 7
    expiry = today + datetime.timedelta(days=days_to_thu)
    return strike, expiry

def is_expiry_day():
    return datetime.date.today().weekday() == 3


# ─────────────────────────────────────────────
#  PAPER TRADE ENGINE
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium,
                 signal, pcr, fii_bias, pre_bias, is_strong=False):
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
        self.is_strong   = is_strong
        self.entry_time  = datetime.datetime.now().strftime("%H:%M:%S")
        self.start_time  = time.time()
        self.be_moved    = False
        self.trailing    = is_strong   # strong FVG → trailing SL

        self.sl_price  = (entry_price - SL_POINTS if direction == "bullish"
                          else entry_price + SL_POINTS)
        self.tgt_price = (entry_price + TARGET_POINTS if direction == "bullish"
                          else entry_price - TARGET_POINTS)
        self.best_price = entry_price

        mode = "TRAILING SL" if is_strong else "FIXED TARGET"
        log.info(f"📝 Trade #{trade_no} | {strategy} | {direction} | "
                 f"{option_type}{strike} | {mode} | "
                 f"Entry:{entry_price:.2f} SL:{self.sl_price:.2f}")

    def check(self, ltp):
        """Returns 'target', 'sl', or None."""
        if self.trailing:
            # Update best price
            if self.direction == "bullish" and ltp > self.best_price:
                self.best_price = ltp
                profit_pts = ltp - self.entry_price
                # Start trailing after TRAIL_START points
                if profit_pts >= TRAIL_START:
                    new_sl = round(ltp - TRAIL_DISTANCE, 2)
                    if new_sl > self.sl_price:
                        old_sl       = self.sl_price
                        self.sl_price = new_sl
                        log.info(f"Trail SL updated: {old_sl:.2f} → {new_sl:.2f}")
                        tg("📈", f"Trade #{self.trade_no} — Trailing SL Updated",
                           [f"Current Nifty : {ltp:.2f}",
                            f"Profit so far : +{profit_pts:.1f}pts",
                            f"New Trail SL  : {new_sl:.2f}",
                            f"Strategy      : Riding the momentum 🚀"])

            elif self.direction == "bearish" and ltp < self.best_price:
                self.best_price = ltp
                profit_pts = self.entry_price - ltp
                if profit_pts >= TRAIL_START:
                    new_sl = round(ltp + TRAIL_DISTANCE, 2)
                    if new_sl < self.sl_price:
                        old_sl        = self.sl_price
                        self.sl_price = new_sl
                        log.info(f"Trail SL updated: {old_sl:.2f} → {new_sl:.2f}")
                        tg("📉", f"Trade #{self.trade_no} — Trailing SL Updated",
                           [f"Current Nifty : {ltp:.2f}",
                            f"Profit so far : +{profit_pts:.1f}pts",
                            f"New Trail SL  : {new_sl:.2f}",
                            f"Strategy      : Riding the momentum 🚀"])

            # Check SL
            if self.direction == "bullish" and ltp <= self.sl_price: return "sl"
            if self.direction == "bearish" and ltp >= self.sl_price: return "sl"

        else:
            # Fixed target mode
            # Move SL to breakeven halfway to target
            if not self.be_moved:
                half = (self.entry_price + self.tgt_price) / 2
                cond = (ltp >= half if self.direction == "bullish" else ltp <= half)
                if cond:
                    self.be_moved = True
                    self.sl_price = self.entry_price
                    tg("🔒", f"Trade #{self.trade_no} — SL to Breakeven",
                       [f"Price  : {ltp:.2f}",
                        f"New SL : {self.entry_price:.2f}"])

            if self.direction == "bullish":
                if ltp >= self.tgt_price: return "target"
                if ltp <= self.sl_price:  return "sl"
            else:
                if ltp <= self.tgt_price: return "target"
                if ltp >= self.sl_price:  return "sl"

        return None

    def duration(self):
        return round((time.time() - self.start_time) / 60, 1)

    def calc_pnl(self, exit_price):
        pts = (exit_price - self.entry_price if self.direction == "bullish"
               else self.entry_price - exit_price)
        return round(pts * 0.4 * LOT_SIZE, 0)


# ─────────────────────────────────────────────
#  CSV LOGS
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime","nifty_ltp","trend_15m",
    "fvg_found","fvg_type","fvg_strong","fvg_size",
    "bos_confirmed","breakaway_found","breakaway_type",
    "orb_high","orb_low","orb_signal",
    "vwap_signal","vwap_level",
    "pcr","pcr_bias","fii_bias","overall_bias",
    "entry_condition_met","strategy_triggered","reason"
]

TRADE_COLS = [
    "date","trade_no","strategy","entry_time","exit_time",
    "pre_bias","fii_bias","pcr",
    "direction","fvg_strong","exit_mode",
    "entry_nifty","exit_nifty","points_moved",
    "option_type","strike","expiry",
    "premium","capital_used","pnl_est",
    "result","be_triggered","trail_triggered",
    "duration_min","consec_losses",
    "daily_pnl","notes"
]

def init_logs():
    for fname, cols in [("scan_log_v2.csv", SCAN_COLS),
                         ("trade_log_v2.csv", TRADE_COLS)]:
        if not os.path.exists(fname):
            with open(fname, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=cols).writeheader()
    log.info("📋 v2 logs initialised")

def write_scan(rec):
    with open("scan_log_v2.csv", "a", newline="") as f:
        row = {c: rec.get(c, "") for c in SCAN_COLS}
        csv.DictWriter(f, fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open("trade_log_v2.csv", "a", newline="") as f:
        row = {c: rec.get(c, "") for c in TRADE_COLS}
        csv.DictWriter(f, fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats, pre_bias, pcr):
    wr = (stats["wins"]/stats["trades"]*100) if stats["trades"] > 0 else 0
    tg("📊", "DAILY SUMMARY v2",
       [f"Pre-bias     : {pre_bias.upper()}",
        f"PCR          : {pcr or 'N/A'}",
        f"Trades       : {stats['trades']}",
        f"Wins ✅      : {stats['wins']}",
        f"Losses ❌    : {stats['losses']}",
        f"Timeouts ⏰  : {stats['timeouts']}",
        f"Skipped ⏭    : {stats['skipped']}",
        f"Win rate     : {wr:.1f}%",
        f"Total P&L    : ₹{stats['pnl']:+.0f}",
        f"FVG trades   : {stats.get('fvg_trades',0)}",
        f"ORB trades   : {stats.get('orb_trades',0)}",
        f"VWAP trades  : {stats.get('vwap_trades',0)}",
        f"Strong FVG   : {stats.get('strong_trades',0)} (trailing SL)"])


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

    trade_no       = 0
    active_trade   = None
    last_scan_time = None
    pre_bias       = "neutral"
    pcr_val        = None
    pcr_bias       = "neutral"
    news_sent      = "neutral"
    premarket_done = False
    orb_high       = None
    orb_low        = None
    orb_formed     = False
    prev_ohlc      = None
    used_signals   = set()   # track which signals fired today

    send_telegram(
        f"🤖 <b>Scalping Bot v2 Started</b>\n"
        f"  Mode        : PAPER TRADING\n"
        f"  Strategies  : FVG+BOS | ORB | VWAP\n"
        f"  SL / TGT    : {SL_POINTS}pts / {TARGET_POINTS}pts (fixed)\n"
        f"  Strong FVG  : Trailing SL {TRAIL_DISTANCE}pts\n"
        f"  Capital     : ₹{CAPITAL_PER_TRADE}/trade\n"
        f"  Max trades  : {MAX_TRADES}/day\n"
        f"  Loss limit  : ₹{DAILY_LOSS_LIMIT}\n"
        f"  Profit tgt  : ₹{DAILY_PROFIT_TARGET}\n\n"
        f"📱 Send /bias bullish|bearish|neutral before 9:30 AM"
    )

    while True:
        now     = datetime.datetime.now()
        t       = now.time()

        # ── PRE-MARKET ──────────────────────────
        if t < TRADE_START:
            if not premarket_done and t >= datetime.time(9, 0):
                prev_ohlc            = get_prev_day_ohlc()
                pcr_val, pcr_bias    = get_pcr()
                heads, news_sent, sc = fetch_news_sentiment()
                fii_bias             = tg_listener.bias
                pre_bias             = compute_bias(fii_bias, pcr_bias, news_sent)
                icon = "📈" if pre_bias=="bullish" else "📉" if pre_bias=="bearish" else "➡️"
                tg(icon, f"PRE-MARKET BIAS: {pre_bias.upper()}",
                   [f"FII bias  : {fii_bias.upper()}",
                    f"PCR       : {pcr_val or 'N/A'} → {pcr_bias.upper()}",
                    f"News      : {news_sent.upper()} (score={sc})",
                    f"Prev close: {prev_ohlc['close'] if prev_ohlc else 'N/A'}",
                    f"Prev high : {prev_ohlc['high'] if prev_ohlc else 'N/A'}",
                    f"Prev low  : {prev_ohlc['low'] if prev_ohlc else 'N/A'}",
                    f"Bias      : {pre_bias.upper()}"])
                premarket_done = True
            time.sleep(30)
            continue

        premarket_done = False

        # ── END OF DAY ───────────────────────────
        if t >= TRADE_END:
            send_summary(stats, pre_bias, pcr_val)
            send_telegram("💤 <b>Market closed. Bot sleeping.</b>")
            stats = {"trades":0,"wins":0,"losses":0,"timeouts":0,
                     "skipped":0,"pnl":0.0,"consec_loss":0,
                     "fvg_trades":0,"orb_trades":0,"vwap_trades":0,"strong_trades":0}
            trade_no=0; active_trade=None; last_scan_time=None
            pre_bias="neutral"; pcr_val=None; orb_high=None
            orb_low=None; orb_formed=False; used_signals=set()
            tg_listener.bias="neutral"
            time.sleep(16*3600)
            continue

        # ── MONITOR ACTIVE TRADE ─────────────────
        if active_trade is not None:
            ltp    = get_nifty_ltp()
            result = None
            if ltp: result = active_trade.check(ltp)
            if t >= TRADE_END: result = "timeout"; ltp = ltp or active_trade.entry_price
            if result:
                exit_time = now.strftime("%H:%M:%S")
                duration  = active_trade.duration()
                pnl       = active_trade.calc_pnl(ltp)
                pts_moved = round(ltp - active_trade.entry_price, 2) \
                            if active_trade.direction == "bullish" \
                            else round(active_trade.entry_price - ltp, 2)

                if result == "target":
                    icon="✅"; stats["wins"]+=1; stats["consec_loss"]=0
                elif result == "sl":
                    icon="❌"; stats["losses"]+=1; stats["consec_loss"]+=1
                else:
                    icon="⏰"; stats["timeouts"]+=1; stats["consec_loss"]=0

                stats["trades"] += 1
                stats["pnl"]    += pnl

                exit_mode = "Trailing SL" if active_trade.trailing else "Fixed Target"
                tg(icon, f"TRADE #{active_trade.trade_no} — {result.upper()}",
                   [f"Strategy   : {active_trade.strategy}",
                    f"Exit mode  : {exit_mode}",
                    f"Direction  : {active_trade.direction.upper()}",
                    f"Option     : {active_trade.option_type} {active_trade.strike}",
                    f"Entry      : {active_trade.entry_price:.2f}",
                    f"Exit       : {ltp:.2f}",
                    f"Points     : {pts_moved:+.1f}",
                    f"Duration   : {duration} min",
                    f"P&L        : ₹{pnl:+.0f}",
                    f"Today P&L  : ₹{stats['pnl']:+.0f}",
                    f"Trades     : {stats['trades']}/{MAX_TRADES}"])

                write_trade({
                    "date":datetime.date.today(),
                    "trade_no":active_trade.trade_no,
                    "strategy":active_trade.strategy,
                    "entry_time":active_trade.entry_time,
                    "exit_time":exit_time,
                    "pre_bias":pre_bias,
                    "fii_bias":active_trade.fii_bias,
                    "pcr":active_trade.pcr,
                    "direction":active_trade.direction,
                    "fvg_strong":active_trade.is_strong,
                    "exit_mode":exit_mode,
                    "entry_nifty":active_trade.entry_price,
                    "exit_nifty":round(ltp,2),
                    "points_moved":pts_moved,
                    "option_type":active_trade.option_type,
                    "strike":active_trade.strike,
                    "expiry":active_trade.expiry,
                    "premium":active_trade.premium,
                    "capital_used":active_trade.premium*LOT_SIZE,
                    "pnl_est":pnl,
                    "result":result,
                    "be_triggered":active_trade.be_moved,
                    "trail_triggered":active_trade.trailing,
                    "duration_min":duration,
                    "consec_losses":stats["consec_loss"],
                    "daily_pnl":stats["pnl"],
                    "notes":f"Signal:{active_trade.signal}"
                })
                active_trade=None
                time.sleep(2*60)
            else:
                time.sleep(15)
            continue

        # ── GUARDS ───────────────────────────────
        if stats["trades"] >= MAX_TRADES:
            time.sleep(30*60); continue

        if stats["consec_loss"] >= 3:
            tg("🛑","Risk Protection",
               [f"Consecutive losses: {stats['consec_loss']}",
                f"Action: Paused for today"])
            send_summary(stats, pre_bias, pcr_val)
            time.sleep(16*3600); continue

        if stats["pnl"] <= -DAILY_LOSS_LIMIT:
            tg("🛑","Daily Loss Limit Hit",
               [f"P&L    : ₹{stats['pnl']:+.0f}",
                f"Limit  : -₹{DAILY_LOSS_LIMIT}",
                f"Action : Stopped for today"])
            send_summary(stats, pre_bias, pcr_val)
            time.sleep(16*3600); continue

        if stats["pnl"] >= DAILY_PROFIT_TARGET:
            tg("🎯","Daily Profit Target Hit!",
               [f"P&L    : ₹{stats['pnl']:+.0f}",
                f"Target : ₹{DAILY_PROFIT_TARGET}",
                f"Action : Protecting gains, stopped for today"])
            send_summary(stats, pre_bias, pcr_val)
            time.sleep(16*3600); continue

        if is_expiry_day() and t >= EXPIRY_STOP:
            time.sleep(10*60); continue

        # ── FETCH DATA ───────────────────────────
        ltp   = get_nifty_ltp()
        df_5  = get_candles(interval_val=5)
        df_15 = get_candles(interval_val=15)

        if ltp is None or df_5 is None:
            time.sleep(15); continue

        trend, trend_reason = detect_trend(df_15)

        # ── ORB RANGE FORMATION ──────────────────
        if not orb_formed and t >= ORB_END:
            orb_df   = df_5[df_5["timestamp"].dt.time <= ORB_END]
            if not orb_df.empty:
                orb_high = float(orb_df["high"].max())
                orb_low  = float(orb_df["low"].min())
                orb_formed = True
                tg("📐","ORB Range Formed",
                   [f"Range high : {orb_high:.2f}",
                    f"Range low  : {orb_low:.2f}",
                    f"Range size : {orb_high-orb_low:.1f} pts",
                    f"Strategy   : Watching for breakout..."])

        # ── REFRESH PCR every 30 min ─────────────
        if last_scan_time is None or (now-last_scan_time).seconds >= 1800:
            new_pcr, new_bias = get_pcr()
            if new_pcr: pcr_val=new_pcr; pcr_bias=new_bias

        # ── RUN ALL STRATEGY DETECTORS ───────────
        fvg,  fvg_reason  = detect_fvg(df_5)
        bos,  bos_level   = detect_bos(df_5, trend)
        bgap, bgap_reason = detect_breakaway_gap(
            df_5, prev_ohlc["close"] if prev_ohlc else None)
        orb_sig, orb_reason = detect_orb(df_5, orb_high, orb_low)
        vwap_sig, vwap_reason = detect_vwap_rejection(df_5, df_15)

        # ── 5-MIN SCAN LOG ───────────────────────
        do_scan = (last_scan_time is None or (now-last_scan_time).seconds >= 300)
        if do_scan:
            last_scan_time = now
            strats = []
            if fvg and bos:            strats.append("FVG+BOS")
            if bgap:                   strats.append("BreakawayGap")
            if orb_sig:                strats.append("ORB")
            if vwap_sig:               strats.append("VWAP")
            entry_met = len(strats) > 0 and trend != "neutral"

            write_scan({
                "datetime":now.strftime("%Y-%m-%d %H:%M"),
                "nifty_ltp":round(ltp,2),
                "trend_15m":trend,
                "fvg_found":fvg is not None,
                "fvg_type":fvg["type"] if fvg else "",
                "fvg_strong":fvg["strong"] if fvg else "",
                "fvg_size":fvg["size"] if fvg else "",
                "bos_confirmed":bos,
                "breakaway_found":bgap is not None,
                "breakaway_type":bgap["gap_type"] if bgap else "",
                "orb_high":orb_high or "",
                "orb_low":orb_low or "",
                "orb_signal":orb_sig["type"] if orb_sig else "",
                "vwap_signal":vwap_sig["type"] if vwap_sig else "",
                "vwap_level":vwap_sig["vwap"] if vwap_sig else "",
                "pcr":pcr_val or "","pcr_bias":pcr_bias,
                "fii_bias":tg_listener.bias,"overall_bias":pre_bias,
                "entry_condition_met":entry_met,
                "strategy_triggered":",".join(strats),
                "reason":f"FVG:{fvg_reason} | ORB:{orb_reason} | VWAP:{vwap_reason}"
            })

            cond_icon = "✅" if entry_met else "⏸️"
            tg(cond_icon, f"5-MIN SCAN @ {now.strftime('%H:%M')}",
               [f"Nifty      : {ltp:.2f}",
                f"Trend(15m) : {trend.upper()}",
                f"FVG        : {fvg_reason[:50] if fvg else 'NONE'}",
                f"BOS        : {'✅ Confirmed' if bos else '❌ Not confirmed'}",
                f"Breakaway  : {bgap_reason[:50] if bgap else 'NONE'}",
                f"ORB        : {orb_reason[:50]}",
                f"VWAP       : {vwap_reason[:50]}",
                f"PCR        : {pcr_val or 'N/A'} ({pcr_bias})",
                f"Bias       : {pre_bias.upper()}",
                f"Signals    : {', '.join(strats) if strats else 'NONE'}"])

        # ── STRATEGY 1: FVG + BOS ────────────────
        if fvg and bos and trend != "neutral" and "FVG" not in used_signals:
            if fvg["type"] == trend:
                if pre_bias == "neutral" or pre_bias == trend:
                    is_strong = fvg["strong"]
                    mode      = "STRONG (Trailing SL)" if is_strong else "WEAK (Fixed 8pts)"
                    tg("👀", f"FVG+BOS Setup — {mode}",
                       [f"Trend    : {trend.upper()}",
                        f"FVG      : {fvg['type'].upper()} | {fvg['bottom']}-{fvg['top']}",
                        f"Gap size : {fvg['size']}pts",
                        f"BOS at   : {bos_level:.2f}",
                        f"Mode     : {mode}",
                        f"Waiting  : Retest of FVG zone"])

                    retest_ok   = False
                    entry_price = None
                    start_wait  = time.time()
                    while time.time() - start_wait < 10*60:
                        cur = get_nifty_ltp()
                        if cur and is_retesting(cur, fvg["bottom"], fvg["top"]):
                            retest_ok=True; entry_price=cur; break
                        time.sleep(15)

                    if retest_ok:
                        trade_no += 1
                        opt_type  = "CE" if trend=="bullish" else "PE"
                        strike, expiry = get_option_details(entry_price, opt_type)
                        premium   = round(CAPITAL_PER_TRADE / LOT_SIZE, 1)
                        active_trade = PaperTrade(
                            trade_no=trade_no, strategy="FVG+BOS",
                            direction=trend, entry_price=entry_price,
                            option_type=opt_type, strike=strike,
                            expiry=expiry, premium=premium,
                            signal=f"FVG {fvg['size']}pts BOS@{bos_level:.1f}",
                            pcr=pcr_val, fii_bias=tg_listener.bias,
                            pre_bias=pre_bias, is_strong=is_strong
                        )
                        used_signals.add("FVG")
                        stats["fvg_trades"] += 1
                        if is_strong: stats["strong_trades"] += 1
                        tg("🚀", f"PAPER TRADE #{trade_no} — FVG+BOS",
                           [f"Type      : {mode}",
                            f"Direction : {trend.upper()}",
                            f"Option    : {opt_type} {strike}",
                            f"Entry     : {entry_price:.2f}",
                            f"SL        : {active_trade.sl_price:.2f}",
                            f"Target    : {'Trailing' if is_strong else str(active_trade.tgt_price)+'pts'}",
                            f"Capital   : ₹{premium*LOT_SIZE:.0f}",
                            f"NOTE      : PAPER TRADE ⚠️"])
                        time.sleep(15)
                        continue
                    else:
                        tg("⏰","FVG Retest Timeout",
                           [f"Zone  : {fvg['bottom']}-{fvg['top']}",
                            f"Action: FVG discarded"])
                        stats["skipped"] += 1

        # ── STRATEGY 1B: BREAKAWAY GAP ───────────
        if bgap and trend != "neutral" and "BGAP" not in used_signals:
            if bgap["type"] == trend:
                if pre_bias == "neutral" or pre_bias == trend:
                    tg("💥","Breakaway Gap Setup",
                       [f"Type      : {bgap['gap_type']}",
                        f"Direction : {bgap['type'].upper()}",
                        f"Gap size  : {bgap['size']}pts",
                        f"Level     : {bgap['level']:.2f}",
                        f"Mode      : STRONG — Trailing SL"])

                    retest_ok   = False
                    entry_price = None
                    start_wait  = time.time()
                    level       = bgap["level"]
                    while time.time() - start_wait < 10*60:
                        cur = get_nifty_ltp()
                        if cur and is_retesting(cur, level-5, level+5):
                            retest_ok=True; entry_price=cur; break
                        time.sleep(15)

                    if retest_ok:
                        trade_no += 1
                        opt_type  = "CE" if trend=="bullish" else "PE"
                        strike, expiry = get_option_details(entry_price, opt_type)
                        premium   = round(CAPITAL_PER_TRADE / LOT_SIZE, 1)
                        active_trade = PaperTrade(
                            trade_no=trade_no, strategy="BreakawayGap",
                            direction=trend, entry_price=entry_price,
                            option_type=opt_type, strike=strike,
                            expiry=expiry, premium=premium,
                            signal=f"Bgap {bgap['gap_type']} {bgap['size']}pts",
                            pcr=pcr_val, fii_bias=tg_listener.bias,
                            pre_bias=pre_bias, is_strong=True
                        )
                        used_signals.add("BGAP")
                        stats["fvg_trades"] += 1
                        stats["strong_trades"] += 1
                        tg("🚀", f"PAPER TRADE #{trade_no} — Breakaway Gap",
                           [f"Direction : {trend.upper()}",
                            f"Option    : {opt_type} {strike}",
                            f"Entry     : {entry_price:.2f}",
                            f"Mode      : Trailing SL {TRAIL_DISTANCE}pts",
                            f"NOTE      : PAPER TRADE ⚠️"])
                        time.sleep(15)
                        continue
                    else:
                        stats["skipped"] += 1

        # ── STRATEGY 2: ORB ──────────────────────
        if orb_sig and orb_formed and "ORB" not in used_signals:
            if pre_bias == "neutral" or pre_bias == orb_sig["type"]:
                tg("📐","ORB Breakout Setup",
                   [f"Direction : {orb_sig['type'].upper()}",
                    f"Level     : {orb_sig['level']:.2f}",
                    f"Breakout  : {orb_sig['size']:.1f}pts",
                    f"Mode      : Fixed {TARGET_POINTS}pts target",
                    f"Waiting   : Retest of ORB level"])

                retest_ok   = False
                entry_price = None
                level       = orb_sig["level"]
                start_wait  = time.time()
                while time.time() - start_wait < 10*60:
                    cur = get_nifty_ltp()
                    if cur and is_retesting(cur, level-8, level+8):
                        retest_ok=True; entry_price=cur; break
                    time.sleep(15)

                if retest_ok:
                    trade_no += 1
                    opt_type  = "CE" if orb_sig["type"]=="bullish" else "PE"
                    strike, expiry = get_option_details(entry_price, opt_type)
                    premium   = round(CAPITAL_PER_TRADE / LOT_SIZE, 1)
                    active_trade = PaperTrade(
                        trade_no=trade_no, strategy="ORB",
                        direction=orb_sig["type"], entry_price=entry_price,
                        option_type=opt_type, strike=strike,
                        expiry=expiry, premium=premium,
                        signal=f"ORB {orb_sig['type']} {orb_sig['size']:.1f}pts",
                        pcr=pcr_val, fii_bias=tg_listener.bias,
                        pre_bias=pre_bias, is_strong=False
                    )
                    used_signals.add("ORB")
                    stats["orb_trades"] += 1
                    tg("🚀", f"PAPER TRADE #{trade_no} — ORB",
                       [f"Direction : {orb_sig['type'].upper()}",
                        f"Option    : {opt_type} {strike}",
                        f"Entry     : {entry_price:.2f}",
                        f"SL        : {active_trade.sl_price:.2f}",
                        f"Target    : {active_trade.tgt_price:.2f}",
                        f"NOTE      : PAPER TRADE ⚠️"])
                    time.sleep(15)
                    continue
                else:
                    stats["skipped"] += 1

        # ── STRATEGY 3: VWAP ─────────────────────
        if vwap_sig and "VWAP" not in used_signals:
            if pre_bias == "neutral" or pre_bias == vwap_sig["type"]:
                tg("〰️","VWAP Rejection Setup",
                   [f"Direction : {vwap_sig['type'].upper()}",
                    f"VWAP      : {vwap_sig['vwap']:.2f}",
                    f"Volume    : {vwap_sig['volume']:.0f} (avg:{vwap_sig['avg_volume']:.0f})",
                    f"Mode      : Fixed {TARGET_POINTS}pts target"])

                cur = get_nifty_ltp()
                if cur:
                    trade_no += 1
                    opt_type  = "CE" if vwap_sig["type"]=="bullish" else "PE"
                    strike, expiry = get_option_details(cur, opt_type)
                    premium   = round(CAPITAL_PER_TRADE / LOT_SIZE, 1)
                    active_trade = PaperTrade(
                        trade_no=trade_no, strategy="VWAP",
                        direction=vwap_sig["type"], entry_price=cur,
                        option_type=opt_type, strike=strike,
                        expiry=expiry, premium=premium,
                        signal=f"VWAP {vwap_sig['type']} @ {vwap_sig['vwap']:.1f}",
                        pcr=pcr_val, fii_bias=tg_listener.bias,
                        pre_bias=pre_bias, is_strong=False
                    )
                    used_signals.add("VWAP")
                    stats["vwap_trades"] += 1
                    tg("🚀", f"PAPER TRADE #{trade_no} — VWAP",
                       [f"Direction : {vwap_sig['type'].upper()}",
                        f"Option    : {opt_type} {strike}",
                        f"Entry     : {cur:.2f}",
                        f"VWAP      : {vwap_sig['vwap']:.2f}",
                        f"SL        : {active_trade.sl_price:.2f}",
                        f"Target    : {active_trade.tgt_price:.2f}",
                        f"NOTE      : PAPER TRADE ⚠️"])
                    time.sleep(15)
                    continue

        time.sleep(60)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Bot v2 stopped manually")
        send_telegram("🛑 <b>Scalping Bot v2 stopped manually.</b>")
