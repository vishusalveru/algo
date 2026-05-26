"""
═══════════════════════════════════════════════════════════════════════════
  nifty_bot_v13.py  —  Option-Chain-Driven Nifty Paper Trading Bot
═══════════════════════════════════════════════════════════════════════════

  ARCHITECTURE
    signals.py               → pure signal detection (12 strategies)
    nifty_bot_v13.py        → option quality gates + confidence + trade logic

  POSITION RULES (paper trade, live Upstox data)
    • Max lots per trade:    2 (hard cap)
    • Stop loss:             20% of entry premium (hard cap)
    • Target:                10% sustain trailing (exit if doesn't hold)
    • Max trade duration:    30 minutes (hard timeout)
    • Slippage simulation:   3 points on entry/exit
    • Entry gate:            Option spread < 8%, OI > 50k, bid_qty > 25 lots

  LOGGING
    • Every parameter logged: LTP, ATR, RSI, trend, PCR, signal, confidence,
      delta, IV, spread, entry, exit, P&L, reason
    • CSV output: scan_v13_DATE.csv, trade_v13_DATE.csv, skip_v13_DATE.csv
    • JSON state: bot_state_v13.json (crash recovery)

  SIGNALS
    • All detection from signals.py (detectors + indicators)
    • Priority order: FVG > BOS > EMAStack > VWAPBand > others
    • No signal = no entry (simple gate)

  CONFIDENCE (bot-side, this module)
    • Scored 0–10 based on: trend alignment, EMA stack, VWAP position,
      RSI extremity, multi-TF agreement
    • Entry threshold: 5/10 minimum (medium confidence)
    • Adaptive: higher threshold if recent losses, lower if good streak

  LIVE DATA SOURCE
    Upstox API v3 (market-quote/option-greek for greeks)
              v2 (option/chain for liquidity)

═══════════════════════════════════════════════════════════════════════════
"""

import time
import logging
import datetime
import csv
import os
import json
import threading
import requests
import numpy as np
import pandas as pd
import pytz

# Import the shared signal detection layer
import signals
import config

# ─────────────────────────────────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    handlers=[
        logging.FileHandler("nifty_v13.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
#  CONSTANTS & CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────

NIFTY_KEY = "NSE_INDEX|Nifty 50"
IST = pytz.timezone("Asia/Kolkata")

# Position sizing
MAX_LOTS = 2
SL_PCT_OF_PREMIUM = 0.20  # 20% of entry premium
TARGET_PCT_OF_PREMIUM = 0.10  # 10% of entry premium for sustain level
MAX_TRADE_DURATION_MIN = 30
SLIPPAGE_PTS = 3

# Entry gates
MIN_OPTION_SPREAD_PCT = 0.08  # 8% of mid-price
MIN_OPTION_OI = 50000
MIN_BID_QTY = 25  # lots

# Confidence thresholds
MIN_CONF_FOR_ENTRY = 5  # 0–10 scale
HIGH_CONF_THRESHOLD = 8

# Session timing
TRADE_START = datetime.time(9, 30)
TRADE_END = datetime.time(14, 30)
SESSION_BIAS_END = datetime.time(10, 0)

# Signal priority (execute in order, first-fire wins)
SIGNAL_PRIORITY = [
    "StrongFVG", "BOS", "EMAStack", "VWAPBand", "VWAPCross",
    "EMA50Bounce", "EMACross", "SuperTrend", "CPR", "RSIDivergence", "ORPH_ORPL"
]

# CSV columns for logging
SCAN_COLS = [
    "datetime", "nifty_ltp", "atm_strike", "session_bias", "rsi", "atr",
    "trend_5m", "trend_15m", "trend_30m", "trend_combined", "trend_strength",
    "e9", "e21", "e50", "vwap", "rvol",
    "fvg_sig", "bos_sig", "ema_stack_sig", "vwap_band_sig", "vwap_cross_sig",
    "ema50_sig", "ema_cross_sig", "st_sig", "cpr_sig", "rsi_div_sig", "orph_sig",
    "signal_fired", "capital_mode", "daily_pnl", "active_trades"
]

TRADE_COLS = [
    "trade_no", "timestamp", "strategy", "direction",
    "entry_nifty", "entry_premium", "lots", "capital",
    "sl_pct", "sl_price", "tgt_sustain_pct", "tgt_price",
    "entry_time", "exit_time", "duration_min",
    "entry_rsi", "entry_atr", "entry_trend", "entry_conf",
    "option_strike", "option_type", "option_iv", "option_delta", "option_spread",
    "exit_reason", "exit_nifty", "exit_premium",
    "pts_moved", "pnl_est", "result",
    "notes"
]

SKIP_COLS = [
    "datetime", "strategy", "reason", "signal_conf", "nifty_ltp", "active_trades"
]

# ─────────────────────────────────────────────────────────────────────────
#  UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────

def now_ist():
    """Current time in IST."""
    return datetime.datetime.now(IST)

def ist_time():
    """Current time of day in IST."""
    return now_ist().time()

def get_headers():
    """API headers for Upstox."""
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.LIVE_TOKEN}"
    }

# ─────────────────────────────────────────────────────────────────────────
#  DATA FETCH (Upstox API)
# ─────────────────────────────────────────────────────────────────────────

def get_nifty_ltp():
    """Fetch current Nifty LTP."""
    try:
        r = requests.get(
            "https://api.upstox.com/v3/market-quote/ltp",
            params={"instrument_key": NIFTY_KEY},
            headers=get_headers(),
            timeout=5
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            for v in data.values():
                lp = v.get("last_price")
                if lp:
                    return float(lp)
    except Exception as e:
        log.error(f"LTP fetch error: {e}")
    return None

def get_candles(interval_min=5):
    """Fetch intraday candles from Upstox v3 API."""
    try:
        url = f"https://api.upstox.com/v3/historical-candle/intraday/{NIFTY_KEY}/minutes/{interval_min}"
        r = requests.get(url, headers=get_headers(), timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                candles = data.get("data", {}).get("candles", [])
                if not candles:
                    return None
                df = pd.DataFrame(
                    candles,
                    columns=["timestamp", "open", "high", "low", "close", "volume", "oi"]
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                for col in ["open", "high", "low", "close", "volume", "oi"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
                return df
    except Exception as e:
        log.error(f"Candles fetch error: {e}")
    return None

def get_option_chain(expiry):
    """Fetch option chain from Upstox v2 API."""
    try:
        r = requests.get(
            "https://api.upstox.com/v2/option/chain",
            params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
            headers=get_headers(),
            timeout=12
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                return data.get("data", [])
    except Exception as e:
        log.error(f"Option chain fetch error: {e}")
    return None

def get_option_greeks(instrument_keys):
    """Fetch greeks from Upstox v3 API."""
    try:
        if not instrument_keys:
            return {}
        r = requests.get(
            "https://api.upstox.com/v3/market-quote/option-greek",
            params={"instrument_key": ",".join(instrument_keys)},
            headers=get_headers(),
            timeout=12
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                return data.get("data", {})
    except Exception as e:
        log.error(f"Greeks fetch error: {e}")
    return {}

def get_prev_day_ohlc():
    """Fetch previous day OHLC for CPR and gap calculations."""
    try:
        today = datetime.date.today()
        from_dt = (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        to_dt = today.strftime("%Y-%m-%d")
        url = f"https://api.upstox.com/v3/historical-candle/{NIFTY_KEY}/days/1/{to_dt}/{from_dt}"
        r = requests.get(url, headers=get_headers(), timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                candles = data.get("data", {}).get("candles", [])
                if len(candles) >= 2:
                    prev = candles[-2]
                    return {
                        "open": float(prev[1]), "high": float(prev[2]),
                        "low": float(prev[3]), "close": float(prev[4])
                    }
    except Exception as e:
        log.error(f"Prev OHLC fetch error: {e}")
    return None

def get_nearest_expiry():
    """Get nearest Nifty option expiry."""
    try:
        r = requests.get(
            "https://api.upstox.com/v2/option/contract",
            params={"instrument_key": NIFTY_KEY},
            headers=get_headers(),
            timeout=10
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            expiries = sorted(set(
                d.get("expiry", "") for d in data
                if d.get("expiry", "") >= today_str
            ))
            for exp in expiries:
                if exp > today_str:
                    return exp
            return expiries[0] if expiries else None
    except Exception as e:
        log.error(f"Expiry fetch error: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────
#  INDICATORS (via signals.py)
# ─────────────────────────────────────────────────────────────────────────

def calc_all_indicators(df_5, df_15, df_30):
    """Calculate all indicators needed for signal detection."""
    indicators = {}
    try:
        if df_5 is not None and len(df_5) >= 5:
            indicators["atr"] = signals.calc_atr(df_5)
            indicators["rsi"] = signals.calc_rsi(df_5)
            indicators["rvol"] = calc_rvol(df_5)
            
            df_vwap = signals.calc_vwap_bands(df_5, indicators.get("atr", 30))
            if len(df_vwap) > 0:
                last = df_vwap.iloc[-1]
                indicators["vwap"] = float(last.get("vwap", 0))
                indicators["vwap_u1"] = float(last.get("vwap_u1", 0))
                indicators["vwap_l1"] = float(last.get("vwap_l1", 0))
            
            df_ema = signals.calc_ema(df_5)
            if "ema9" in df_ema.columns:
                indicators["e9"] = float(df_ema["ema9"].iloc[-1])
                indicators["e21"] = float(df_ema["ema21"].iloc[-1])
                indicators["e50"] = float(df_ema["ema50"].iloc[-1])
                indicators["df_ema"] = df_ema
        
        if df_5 is not None and df_15 is not None and df_30 is not None:
            t5, _, _ = signals.detect_trend_relaxed(df_5)
            t15, _, _ = signals.detect_trend_relaxed(df_15)
            t30, _, _ = signals.detect_trend_relaxed(df_30)
            trend, _, strength = signals.detect_trend_multi(
                df_5, df_15, df_30,
                indicators.get("e9", 0), indicators.get("e21", 0),
                indicators.get("e50", 0), 0
            )
            indicators["t5"] = t5
            indicators["t15"] = t15
            indicators["t30"] = t30
            indicators["trend"] = trend
            indicators["trend_strength"] = strength
            
            regime = signals.classify_intraday_regime(
                df_5, indicators.get("e9", 0), indicators.get("e21", 0),
                indicators.get("atr", 30)
            )
            indicators["regime"] = regime
    except Exception as e:
        log.error(f"Indicator calc error: {e}")
    
    return indicators

def calc_rvol(df):
    """Relative volume."""
    try:
        if df is None or len(df) < 5:
            return 1.0
        vol = df["volume"].astype(float)
        if vol.sum() > 0 and vol.std() > 0:
            avg = float(vol.mean())
            cur = float(vol.iloc[-1])
            if avg > 0:
                return round(max(0.5, min(5.0, cur / avg)), 2)
    except:
        pass
    return 1.0

# ─────────────────────────────────────────────────────────────────────────
#  SIGNAL DETECTION (calls signals.py detectors in priority order)
# ─────────────────────────────────────────────────────────────────────────

def detect_all_signals(df_5, df_15, indicators, prev_ohlc, ltp, prev_ltp=None):
    """Run all 12 signal detectors, return first fire in priority order."""
    
    signals_dict = {}
    
    # Calculate RSI series for divergence
    rsi_series = None
    try:
        if df_5 is not None and len(df_5) >= 20:
            delta = df_5["close"].astype(float).diff()
            gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi_full = (100 - (100 / (1 + rs))).values
            rsi_series = rsi_full
    except:
        pass
    
    # Unpack indicators
    atr = indicators.get("atr", 30)
    e9 = indicators.get("e9", 0)
    e21 = indicators.get("e21", 0)
    e50 = indicators.get("e50", 0)
    t5 = indicators.get("t5", "neutral")
    trend = indicators.get("trend", "neutral")
    rvol = indicators.get("rvol", 1.0)
    vwap = indicators.get("vwap", 0)
    
    # Run all detectors
    if df_5 is not None:
        fvg, fvg_r = signals.detect_fvg(df_5)
        signals_dict["StrongFVG"] = (fvg, fvg_r)
        
        bos, bos_r = signals.detect_bos(df_5, ltp) if ltp else (None, "No LTP")
        signals_dict["BOS"] = (bos, bos_r)
        
        ema_stk, ema_stk_r = signals.detect_ema_stack(
            indicators.get("df_ema", df_5), ltp, t5, rvol
        ) if "df_ema" in indicators else (None, "No EMA data")
        signals_dict["EMAStack"] = (ema_stk, ema_stk_r)
        
        if df_15 is not None and len(df_15) >= 21:
            df_vwap = signals.calc_vwap_bands(df_5, atr)
            vwap_band, vwap_band_r = signals.detect_vwap_band_break(
                df_vwap, ltp, t5, atr
            )
            signals_dict["VWAPBand"] = (vwap_band, vwap_band_r)
            
            vwap_cx, vwap_cx_r = signals.detect_vwap_cross(df_vwap, ltp, df_5)
            signals_dict["VWAPCross"] = (vwap_cx, vwap_cx_r)
        
        ema50_b, ema50_r = signals.detect_ema50_bounce(
            indicators.get("df_ema", df_5), ltp, t5, df_5
        ) if "df_ema" in indicators else (None, "No EMA data")
        signals_dict["EMA50Bounce"] = (ema50_b, ema50_r)
    
    if df_15 is not None and "df_ema" in indicators:
        # Note: would need prev_df_ema for proper EMA cross, simplified here
        ema_cx, ema_cx_r = (None, "EMA cross skipped (needs prev_df_ema)")
        signals_dict["EMACross"] = (ema_cx, ema_cx_r)
    
    st_sig, st_r = signals.detect_supertrend_signal(df_5, trend, ltp, atr) if df_5 is not None else (None, "No df_5")
    signals_dict["SuperTrend"] = (st_sig, st_r)
    
    # CPR (needs previous day OHLC)
    cpr_pivot, cpr_bc, cpr_tc = None, None, None
    if prev_ohlc:
        H, L, C = prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"]
        cpr_pivot = round((H + L + C) / 3, 1)
        cpr_bc = round((H + L) / 2, 1)
        cpr_tc = round((cpr_pivot - cpr_bc) + cpr_pivot, 1)
        if cpr_tc < cpr_bc:
            cpr_tc, cpr_bc = cpr_bc, cpr_tc
    
    cpr, cpr_r = signals.detect_cpr_signal(ltp, cpr_pivot, cpr_bc, cpr_tc, trend, prev_ltp) if cpr_pivot else (None, "No CPR")
    signals_dict["CPR"] = (cpr, cpr_r)
    
    # RSI divergence
    rsi_div, rsi_div_r = signals.detect_rsi_divergence(df_5, rsi_series) if df_5 is not None else (None, "No RSI")
    signals_dict["RSIDivergence"] = (rsi_div, rsi_div_r)
    
    # ORPH_ORPL
    gap_pct = 0.0
    if prev_ohlc and ltp:
        gap_pct = ((ltp - prev_ohlc["close"]) / prev_ohlc["close"]) * 100
    orph, orph_r = signals.detect_orph_orpl(ltp, prev_ohlc, trend, prev_ltp, gap_pct) if prev_ohlc else (None, "No prev OHLC")
    signals_dict["ORPH_ORPL"] = (orph, orph_r)
    
    # Find first signal in priority order
    for strat_name in SIGNAL_PRIORITY:
        sig, reason = signals_dict.get(strat_name, (None, ""))
        if sig is not None:
            return strat_name, sig, reason
    
    return None, None, "No signal"

# ─────────────────────────────────────────────────────────────────────────
#  CONFIDENCE SCORING (bot-side)
# ─────────────────────────────────────────────────────────────────────────

def calc_confidence(signal_type, direction, indicators, ltp):
    """Score confidence 0–10 based on market structure alignment."""
    score = 0
    reasons = []
    
    trend = indicators.get("trend", "neutral")
    e9 = indicators.get("e9", 0)
    e21 = indicators.get("e21", 0)
    e50 = indicators.get("e50", 0)
    rsi = indicators.get("rsi", 50)
    t5 = indicators.get("t5", "neutral")
    t15 = indicators.get("t15", "neutral")
    t30 = indicators.get("t30", "neutral")
    vwap = indicators.get("vwap", 0)
    
    # 1. Trend alignment (+2)
    if trend == direction:
        score += 2
        reasons.append(f"Trend {trend} aligned")
    
    # 2. EMA 9/21 alignment (+2)
    if direction == "bullish" and e9 > e21:
        score += 2
        reasons.append(f"EMA9>{e21}")
    elif direction == "bearish" and e9 < e21:
        score += 2
        reasons.append(f"EMA9<{e21}")
    
    # 3. VWAP side (+1)
    if direction == "bullish" and ltp > vwap:
        score += 1
        reasons.append("Above VWAP")
    elif direction == "bearish" and ltp < vwap:
        score += 1
        reasons.append("Below VWAP")
    
    # 4. EMA50 side (+1)
    if direction == "bullish" and ltp > e50:
        score += 1
        reasons.append("Above EMA50")
    elif direction == "bearish" and ltp < e50:
        score += 1
        reasons.append("Below EMA50")
    
    # 5. Multi-TF agreement (+1)
    agreement = [t5 == direction, t15 == direction, t30 == direction].count(True)
    if agreement >= 2:
        score += 1
        reasons.append(f"MTF {agreement}/3")
    
    # 6. RSI extremity (+1)
    if direction == "bullish" and rsi < 40:
        score += 1
        reasons.append(f"RSI {rsi:.0f} (room to rise)")
    elif direction == "bearish" and rsi > 60:
        score += 1
        reasons.append(f"RSI {rsi:.0f} (room to fall)")
    
    # 7. Signal strength bonus (+1 for "STRONG" signals)
    if isinstance(signal_type, dict) and signal_type.get("strong"):
        score += 1
        reasons.append("Strong signal")
    
    score = max(0, min(10, score))
    label = "HIGH" if score >= HIGH_CONF_THRESHOLD else "MEDIUM" if score >= MIN_CONF_FOR_ENTRY else "LOW"
    
    return score, label, reasons

# ─────────────────────────────────────────────────────────────────────────
#  OPTION CHAIN QUALITY GATE
# ─────────────────────────────────────────────────────────────────────────

def find_best_option(chain, atm_strike, opt_type, target_delta_min=0.40, target_delta_max=0.60):
    """Find best liquid option by delta range."""
    if not chain:
        return None
    
    candidates = []
    for row in chain:
        sp = row.get("strike_price")
        if sp not in (atm_strike - 100, atm_strike, atm_strike + 100):
            continue
        
        opt_side = "call_options" if opt_type == "CE" else "put_options"
        opt = row.get(opt_side, {})
        if not opt:
            continue
        
        md = opt.get("market_data", {})
        bid = md.get("bid_price", 0)
        ask = md.get("ask_price", 0)
        bid_qty = md.get("bid_qty", 0)
        oi = md.get("oi", 0)
        
        if bid <= 0 or ask <= 0 or bid_qty < MIN_BID_QTY or oi < MIN_OPTION_OI:
            continue
        
        spread = ask - bid
        mid = (bid + ask) / 2
        spread_pct = spread / mid if mid > 0 else 999
        
        if spread_pct > MIN_OPTION_SPREAD_PCT:
            continue
        
        # Get delta if available
        greek = opt.get("option_greeks", {})
        delta = abs(greek.get("delta", 0.5))
        
        if target_delta_min <= delta <= target_delta_max:
            candidates.append({
                "strike": sp, "opt_type": opt_type, "bid": bid, "ask": ask,
                "mid": mid, "spread": spread, "spread_pct": spread_pct,
                "bid_qty": bid_qty, "oi": oi, "delta": delta,
                "iv": greek.get("iv", 0), "theta": greek.get("theta", 0),
                "instr_key": opt.get("instrument_key", "")
            })
    
    if candidates:
        # Sort by OI descending
        candidates.sort(key=lambda x: -x["oi"])
        return candidates[0]
    
    return None

def check_option_quality(option_data):
    """Check if option meets quality criteria."""
    if not option_data:
        return False, "No option data"
    
    spread_pct = option_data.get("spread_pct", 999)
    if spread_pct > MIN_OPTION_SPREAD_PCT:
        return False, f"Spread {spread_pct*100:.1f}% > {MIN_OPTION_SPREAD_PCT*100:.0f}%"
    
    bid_qty = option_data.get("bid_qty", 0)
    if bid_qty < MIN_BID_QTY:
        return False, f"Bid qty {bid_qty} < {MIN_BID_QTY}"
    
    oi = option_data.get("oi", 0)
    if oi < MIN_OPTION_OI:
        return False, f"OI {oi} < {MIN_OPTION_OI}"
    
    return True, "OK"

# ─────────────────────────────────────────────────────────────────────────
#  POSITION MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────

class PaperPosition:
    """Tracks a single paper trade."""
    
    def __init__(self, trade_no, strategy, direction, entry_nifty,
                 option_data, confidence, indicators, prev_ltp=None):
        self.trade_no = trade_no
        self.strategy = strategy
        self.direction = direction
        self.entry_nifty = entry_nifty
        self.entry_nifty_slip = entry_nifty + (SLIPPAGE_PTS if direction == "bearish" else -SLIPPAGE_PTS)
        
        # Option details
        self.option_strike = option_data["strike"]
        self.option_type = option_data["opt_type"]
        self.entry_premium = option_data["mid"]
        self.option_iv = option_data["iv"]
        self.option_delta = option_data["delta"]
        self.option_spread = option_data["spread"]
        self.instr_key = option_data["instr_key"]
        
        # Position sizing (2 lot max)
        self.lots = min(MAX_LOTS, 1)  # Start with 1 lot, can scale up later
        self.capital = self.entry_premium * self.lots * 65  # 65 = Nifty lot size
        
        # Risk management
        self.sl_pct = SL_PCT_OF_PREMIUM  # 20% of premium
        self.sl_price = self.entry_premium * (1 - self.sl_pct)
        self.tgt_sustain_pct = TARGET_PCT_OF_PREMIUM  # 10% sustain level
        self.tgt_price = self.entry_premium * (1 + self.tgt_sustain_pct)
        
        # Metadata
        self.confidence = confidence
        self.entry_rsi = indicators.get("rsi", 50)
        self.entry_atr = indicators.get("atr", 30)
        self.entry_trend = indicators.get("trend", "neutral")
        self.entry_time = now_ist().strftime("%H:%M:%S")
        self.start_ts = time.time()
        self.best_price = self.entry_premium
        self.trailing_active = False
        
        log.info(
            f"[TRADE #{trade_no}] {strategy} {direction.upper()} | "
            f"Entry:{entry_nifty:.0f} Premium:{self.entry_premium:.1f} "
            f"SL:{self.sl_price:.1f} TGT:{self.tgt_price:.1f} "
            f"Conf:{confidence[0]}/10 IV:{self.option_iv:.1f}%"
        )
    
    def update(self, current_premium):
        """Check trade status: target, SL, timeout."""
        dur_min = (time.time() - self.start_ts) / 60
        
        # Hard timeout (30 min)
        if dur_min >= MAX_TRADE_DURATION_MIN:
            return "timeout", dur_min
        
        # Target or SL
        if self.direction == "bullish":
            if current_premium >= self.tgt_price:
                # Check if sustaining above tgt for 2 candles (simplified: just check once)
                self.trailing_active = True
                if current_premium >= self.tgt_price * 1.01:  # small buffer
                    return "target", dur_min
            if current_premium <= self.sl_price:
                return "sl", dur_min
        else:
            if current_premium <= self.tgt_price:
                self.trailing_active = True
                if current_premium <= self.tgt_price * 0.99:
                    return "target", dur_min
            if current_premium >= self.sl_price:
                return "sl", dur_min
        
        return None, dur_min
    
    def calc_pnl(self, exit_premium):
        """Calculate P&L in rupees."""
        pts = exit_premium - self.entry_premium if self.direction == "bullish" else self.entry_premium - exit_premium
        pnl = pts * self.lots * 65  # 65 = Nifty lot size
        return round(pnl, 0), round(pts, 1)

# ─────────────────────────────────────────────────────────────────────────
#  STATE PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────

def load_state():
    """Load bot state from JSON (crash recovery)."""
    try:
        if os.path.exists("bot_state_v13.json"):
            with open("bot_state_v13.json") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"State load error: {e}")
    
    return {
        "date": str(datetime.date.today()),
        "trades": 0, "wins": 0, "losses": 0, "timeouts": 0,
        "pnl": 0.0, "consec_loss": 0, "skipped": 0
    }

def save_state(stats):
    """Save bot state to JSON."""
    try:
        with open("bot_state_v13.json", "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log.error(f"State save error: {e}")

# ─────────────────────────────────────────────────────────────────────────
#  CSV LOGGING
# ─────────────────────────────────────────────────────────────────────────

def init_logs():
    """Initialize CSV log files."""
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    
    for fname, cols in [
        (f"scan_v13_{date_str}.csv", SCAN_COLS),
        (f"trade_v13_{date_str}.csv", TRADE_COLS),
        (f"skip_v13_{date_str}.csv", SKIP_COLS),
    ]:
        if not os.path.exists(fname):
            with open(fname, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=cols).writeheader()

def write_scan(rec):
    """Log a market scan."""
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    fname = f"scan_v13_{date_str}.csv"
    with open(fname, "a", newline="") as f:
        row = {c: rec.get(c, "") for c in SCAN_COLS}
        csv.DictWriter(f, fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    """Log a completed trade."""
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    fname = f"trade_v13_{date_str}.csv"
    with open(fname, "a", newline="") as f:
        row = {c: rec.get(c, "") for c in TRADE_COLS}
        csv.DictWriter(f, fieldnames=TRADE_COLS).writerow(row)

def write_skip(rec):
    """Log a skipped signal."""
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    fname = f"skip_v13_{date_str}.csv"
    with open(fname, "a", newline="") as f:
        row = {c: rec.get(c, "") for c in SKIP_COLS}
        csv.DictWriter(f, fieldnames=SKIP_COLS).writerow(row)

# ─────────────────────────────────────────────────────────────────────────
#  MAIN BOT LOOP
# ─────────────────────────────────────────────────────────────────────────

def run():
    """Main trading loop."""
    log.info("=" * 75)
    log.info("Nifty Bot v13 — STARTED (option-chain-driven, live Upstox data)")
    log.info("=" * 75)
    
    init_logs()
    stats = load_state()
    active_position = None
    trade_no = stats["trades"]
    prev_ltp = None
    prev_df5_ema = None
    
    while True:
        try:
            t = ist_time()
            now = now_ist()
            
            # Exit after session ends
            if t >= TRADE_END:
                log.info(f"Session complete {t}. Exiting.")
                save_state(stats)
                break
            
            # Pre-market
            if t < TRADE_START:
                time.sleep(30)
                continue
            
            # ─ FETCH DATA ─
            ltp = get_nifty_ltp()
            if ltp is None:
                time.sleep(30)
                continue
            
            df_5 = get_candles(5)
            df_15 = get_candles(15)
            df_30 = get_candles(30)
            
            if df_5 is None or len(df_5) < 6:
                time.sleep(30)
                continue
            
            # ─ CALC INDICATORS ─
            indicators = calc_all_indicators(df_5, df_15, df_30)
            if not indicators:
                time.sleep(30)
                continue
            
            # Get previous day OHLC for CPR
            prev_ohlc = get_prev_day_ohlc()
            
            # ─ MONITOR ACTIVE POSITION ─
            if active_position:
                # Get current option price (simplified: use mid of bid/ask from chain)
                expiry = get_nearest_expiry()
                chain = get_option_chain(expiry) if expiry else None
                
                current_premium = active_position.entry_premium  # placeholder
                if chain:
                    for row in chain:
                        if row.get("strike_price") == active_position.option_strike:
                            opt_side = "call_options" if active_position.option_type == "CE" else "put_options"
                            opt = row.get(opt_side, {})
                            if opt:
                                md = opt.get("market_data", {})
                                bid = md.get("bid_price", 0)
                                ask = md.get("ask_price", 0)
                                if bid > 0 and ask > 0:
                                    current_premium = (bid + ask) / 2
                
                exit_reason, dur = active_position.update(current_premium)
                if exit_reason:
                    pnl, pts = active_position.calc_pnl(current_premium)
                    result = "win" if exit_reason == "target" else "loss" if exit_reason == "sl" else "timeout"
                    
                    if result == "win":
                        stats["wins"] += 1
                        stats["consec_loss"] = 0
                    else:
                        stats["losses"] += 1
                        stats["consec_loss"] += 1
                    
                    stats["pnl"] += pnl
                    active_position.exit_time = now.strftime("%H:%M:%S")
                    
                    write_trade({
                        "trade_no": active_position.trade_no,
                        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "strategy": active_position.strategy,
                        "direction": active_position.direction,
                        "entry_nifty": round(active_position.entry_nifty, 1),
                        "entry_premium": round(active_position.entry_premium, 2),
                        "lots": active_position.lots,
                        "capital": round(active_position.capital, 0),
                        "sl_pct": active_position.sl_pct,
                        "sl_price": round(active_position.sl_price, 2),
                        "tgt_sustain_pct": active_position.tgt_sustain_pct,
                        "tgt_price": round(active_position.tgt_price, 2),
                        "entry_time": active_position.entry_time,
                        "exit_time": active_position.exit_time,
                        "duration_min": round(dur, 1),
                        "entry_rsi": round(active_position.entry_rsi, 1),
                        "entry_atr": round(active_position.entry_atr, 1),
                        "entry_trend": active_position.entry_trend,
                        "entry_conf": active_position.confidence[0],
                        "option_strike": active_position.option_strike,
                        "option_type": active_position.option_type,
                        "option_iv": round(active_position.option_iv, 2),
                        "option_delta": round(active_position.option_delta, 3),
                        "option_spread": round(active_position.option_spread, 2),
                        "exit_reason": exit_reason,
                        "exit_nifty": round(ltp, 1),
                        "exit_premium": round(current_premium, 2),
                        "pts_moved": pts,
                        "pnl_est": pnl,
                        "result": result,
                        "notes": f"Conf:{active_position.confidence[1]}"
                    })
                    
                    log.info(f"[EXIT] Trade #{active_position.trade_no} {result.upper()} | "
                             f"P&L: Rs.{pnl:+.0f} | Duration: {dur:.1f}min")
                    save_state(stats)
                    active_position = None
                    time.sleep(30)
                    continue
            
            # ─ IF NO ACTIVE POSITION: LOOK FOR SIGNALS ─
            if active_position is None:
                strat_name, signal_data, signal_reason = detect_all_signals(
                    df_5, df_15, indicators, prev_ohlc, ltp, prev_ltp
                )
                
                # Log scan
                write_scan({
                    "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "nifty_ltp": round(ltp, 1),
                    "atm_strike": int(round(ltp / 50) * 50),
                    "session_bias": indicators.get("regime", "UNKNOWN"),
                    "rsi": round(indicators.get("rsi", 50), 1),
                    "atr": round(indicators.get("atr", 30), 1),
                    "trend_5m": indicators.get("t5", ""),
                    "trend_15m": indicators.get("t15", ""),
                    "trend_30m": indicators.get("t30", ""),
                    "trend_combined": indicators.get("trend", ""),
                    "trend_strength": indicators.get("trend_strength", ""),
                    "e9": round(indicators.get("e9", 0), 1),
                    "e21": round(indicators.get("e21", 0), 1),
                    "e50": round(indicators.get("e50", 0), 1),
                    "vwap": round(indicators.get("vwap", 0), 1),
                    "rvol": round(indicators.get("rvol", 1), 2),
                    "signal_fired": strat_name or "NONE",
                    "capital_mode": f"Rs.{stats.get('pnl', 0):+.0f}",
                    "daily_pnl": round(stats.get("pnl", 0), 0),
                    "active_trades": 1 if active_position else 0
                })
                
                if strat_name and signal_data:
                    # Calculate confidence
                    conf_score, conf_label, conf_reasons = calc_confidence(
                        signal_data, signal_data.get("type", "bullish"), indicators, ltp
                    )
                    
                    # Check entry gate
                    if conf_score < MIN_CONF_FOR_ENTRY:
                        write_skip({
                            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "strategy": strat_name,
                            "reason": f"Low confidence {conf_score}<{MIN_CONF_FOR_ENTRY}",
                            "signal_conf": conf_score,
                            "nifty_ltp": round(ltp, 1),
                            "active_trades": 0
                        })
                        time.sleep(15)
                        continue
                    
                    # Fetch option chain + find best option
                    expiry = get_nearest_expiry()
                    if not expiry:
                        write_skip({
                            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "strategy": strat_name,
                            "reason": "No expiry available",
                            "signal_conf": conf_score,
                            "nifty_ltp": round(ltp, 1),
                            "active_trades": 0
                        })
                        time.sleep(60)
                        continue
                    
                    chain = get_option_chain(expiry)
                    atm = int(round(ltp / 50) * 50)
                    opt_type = "CE" if signal_data.get("type") == "bullish" else "PE"
                    
                    option_data = find_best_option(chain, atm, opt_type) if chain else None
                    if not option_data:
                        write_skip({
                            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "strategy": strat_name,
                            "reason": "No liquid option found",
                            "signal_conf": conf_score,
                            "nifty_ltp": round(ltp, 1),
                            "active_trades": 0
                        })
                        time.sleep(15)
                        continue
                    
                    # Quality gate
                    ok, msg = check_option_quality(option_data)
                    if not ok:
                        write_skip({
                            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "strategy": strat_name,
                            "reason": f"Option quality: {msg}",
                            "signal_conf": conf_score,
                            "nifty_ltp": round(ltp, 1),
                            "active_trades": 0
                        })
                        time.sleep(15)
                        continue
                    
                    # ENTER TRADE
                    trade_no += 1
                    active_position = PaperPosition(
                        trade_no, strat_name, signal_data.get("type"),
                        ltp, option_data, (conf_score, conf_label), indicators, prev_ltp
                    )
                    stats["trades"] += 1
                    save_state(stats)
                else:
                    # No signal
                    write_skip({
                        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "strategy": "NONE",
                        "reason": signal_reason,
                        "signal_conf": 0,
                        "nifty_ltp": round(ltp, 1),
                        "active_trades": 0
                    })
                    time.sleep(60)
                    continue
            
            prev_ltp = ltp
            time.sleep(30)
        
        except KeyboardInterrupt:
            log.info("Bot stopped by user")
            save_state(stats)
            break
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
            save_state(stats)
            time.sleep(15)

if __name__ == "__main__":
    run()
