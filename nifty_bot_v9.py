"""
=============================================================
  Nifty 50 Scalping Bot v9 — Regime-Aware + Quality Filter Build
  ─────────────────────────────────────────────────────────
  INHERITS : All v5-v8 fixes.

  V9 CHANGES (April 2026 backtest — 20 days, 41 trades proved):

  [V9-F1]  ORB+EMA DISABLED
           April backtest: 14 trades, WR 29%, -Rs.5,330
           Fires on false breakouts on ranging days. Every
           other strategy combined: WR 52%, +Rs.6,280.
           ORB+EMA alone was destroying overall performance.
           Will be re-enabled after further data collection.

  [V9-F2]  REGIME GATE — block WEAK_BULL and CHOPPY
           April backtest by regime:
             TRENDING_BEAR: WR 67%, +Rs.2,470
             WEAK_BEAR    : WR 75%, +Rs.4,290
             TRENDING_BULL: WR 40%, -Rs.1,560
             WEAK_BULL    : WR 27%, -Rs.4,615  ← blocked
             RANGING      : WR 17%, +Rs.365
           WEAK_BULL/CHOPPY regimes = slow drift, no momentum.
           Signals fire but price chops → consecutive SLs.
           Regime is detected from 12-candle price structure:
             Range >100pts + EMA aligned   = TRENDING
             Range 60-100pts               = WEAK/CHOPPY
             Range <60pts                  = RANGING
           Gate: block TREND strategies in WEAK_BULL/CHOPPY.
           Allow REVERSAL strategies (VWAPCross, RSIDivergence)
           in any regime — they thrive in ranging markets.

  [V9-F3]  ATR MOMENTUM FILTER — trend strategies need ATR>35
           Low ATR on a "bullish" day = slow drift, not momentum.
           April crash days (ATR 50-70) → WR 100%.
           April consolidation days (ATR 25-35) → WR 29%.
           Trend strategies (EMAStack, BOS, EMACross, SuperTrend)
           now require ATR > 35 to enter.
           Reversal strategies keep ATR > 20 (existing gate).

  [V9-F4]  LOG FILES renamed v9_DATE.csv

  STRATEGIES (11 — ORB+EMA disabled):
  Group A — TREND    : EMAStack, EMACross, SuperTrend, BOS
  Group B — STRUCTURE: StrongFVG, EMA50Bounce, VWAPBand, ORPH_ORPL
  Group C — REVERSAL : RSIDivergence, VWAPCross, CPR

  REGIME GATE:
  • TRENDING_BULL/BEAR: all strategies active
  • WEAK_BULL/WEAK_BEAR/CHOPPY: Group A blocked, B+C allowed
  • RANGING: Group A blocked, B+C allowed
  • UNKNOWN: all blocked (insufficient data)

  V8 CORE CHANGES
  ───────────────
  [V8-F1]  MULTI-TRADE EXECUTION
           v7 blocked ALL other strategies the moment one
           trade opened. v8 runs concurrent trades per
           strategy group:
           • Group A (Trend)    : EMAStack, EMACross,
                                  SuperTrend, BOS, ORB+EMA
           • Group B (Structure): StrongFVG, EMA50Bounce,
                                  VWAPBand, ORPH_ORPL
           • Group C (Reversal) : RSIDivergence, VWAPCross,
                                  CPR
           One active trade per group allowed at a time.
           Max 2 concurrent trades total (capital protection).
           Each group has its own capital allocation.

  [V8-F2]  CRASH-PROOF ARCHITECTURE
           • All state (trades, P&L, cooldowns) written to
             bot_state.json after EVERY update.
           • On restart: active trades restored from state.
           • Uncaught exceptions caught in outer try/except
             with Telegram alert + clean file flush.
           • Each API call wrapped in retry_api() with 3
             retries + exponential backoff.
           • CSV writes are atomic (write to .tmp then rename).

  [V8-F3]  CORRECTED PER-STRATEGY VALIDATIONS
           Fixed 3 issues found in v7 backtest:
           a) RSI confidence penalty removed for BOS/FVG/BOS:
              RSI>60 was deducting -1 from conf score even for
              momentum strategies. BOS at RSI 78 scored 0/10
              because: trend(+2) - RSI penalty(-1) - PCR(-1)
              - RVOL(-1) = 0. Now RSI penalty only applies to
              mean-reversion strategies (EMA50Bounce, CPR,
              RSIDivergence, VWAPCross).
           b) Conf=0 pre-block: RSI/session checks were
              firing BEFORE conf calculation → skip log showed
              conf=0 for valid signals. Fixed: conf calculated
              first, then all gates applied.
           c) Session bias deadlock: when auto_bias=bearish
              and session=bullish, session_bias_over_auto
              strategies were still failing the pre_bias gate
              because effective_bias wasn't propagated into
              the bias_mismatch check. Fixed.

  [V8-F4]  OPTIMISED TRADE STRUCTURE
           • Dynamic SL/Target per strategy type:
             - Trend strategies  : SL=12, TGT=20 (1:1.67)
             - Structure strats  : SL=10, TGT=18 (1:1.8)
             - Reversal strats   : SL=8,  TGT=16 (1:2.0)
           • Breakeven move: 50% of target (was fixed)
           • Trailing: starts at 60% of target, not fixed 12pts
           • Hard timeout: 25min (was 30min) — more theta safe

  [V8-F5]  BACKTEST VALIDATED
           Replay of May 11 + May 13 through v8 rules:
           May 11: 0 trades (correctly avoids bad day) ₹0
           May 13: 3 trades, WR 33%, Net -₹910
           (vs v5: -₹1752 / v6: ₹0 / v7 raw: -₹8515)

  [V8-F6]  SIGNAL QUALITY GATE (new)
           All strategies must pass a minimum signal quality
           score before entering. Combines: ATR adequacy,
           RVOL adequacy, time of day, candle body size.
           Prevents entries in dead market conditions.

  [V8-F7]  STRATEGY GROUP CAPITAL SPLIT
           Group A: Rs.4000 (trend — higher allocation)
           Group B: Rs.3500 (structure)
           Group C: Rs.3000 (reversal — smaller, counter)
           After 2 losses in a group → group halved.
           After 2 wins in reduced → group restored.

  [V8-F8]  ENHANCED SKIP LOG
           Skip log now records: which gate blocked, what the
           conf score would have been, what the outcome was
           (trade taken by another strategy on same candle).
           Enables post-session analysis of what was blocked
           and what traded simultaneously.

  STRATEGY GROUPS:
  ────────────────
  Group A — TREND (one trade at a time):
    1. EMAStack      4. BOS
    2. EMACross      5. ORB+EMA
    3. SuperTrend

  Group B — STRUCTURE (one trade at a time):
    6.  StrongFVG    8.  VWAPBand
    7.  EMA50Bounce  9.  ORPH_ORPL

  Group C — REVERSAL (one trade at a time):
    10. RSIDivergence  12. CPR
    11. VWAPCross

  MAX CONCURRENT: 2 groups active at once.

  DATA   : Upstox Live API (paper trading, no orders placed)
  LOGS   : scan/trade/skip_log_v8_DATE.csv
  STATE  : bot_state.json (crash-safe)
  ALERTS : Telegram /status /report /bias /news /help
=============================================================
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
import config
# [V9-FIX2] Option chain for liquidity + delta
try:
    from upstox_option_chain import OptionChain, check_option_quality
    _OPTION_CHAIN_AVAILABLE = True
except ImportError:
    print('WARNING: upstox_option_chain.py not found — option quality gate disabled')
    _OPTION_CHAIN_AVAILABLE = False
    OptionChain = None
    def check_option_quality(*a, **kw): return True, 'Module N/A', {}
from nifty_auto_bias import (
    get_combined_bias_nifty,
    pre_trade_check_nifty,
    format_bias_message_nifty,
    format_reversal_alert_nifty
)

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("nifty_v9.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  PARAMETERS
# ─────────────────────────────────────────────
# [F1] RR fixed to 1:1.5 — must keep or better
# ── Base RR (used when ATR dynamic SL is unavailable) ──────
SL_POINTS            = 10
TARGET_POINTS        = 15     # 1:1.5 RR minimum
TRAIL_DISTANCE       = 10
TRAIL_START          = 12     # start trailing after 12pts profit

# [V6-F11] Dynamic ATR-based SL/Target multipliers
ATR_SL_MULT          = 0.8    # SL = 0.8 x ATR  (min SL_POINTS)
ATR_TGT_MULT         = 1.2    # TGT = 1.2 x ATR (min TARGET_POINTS, always ≥1.5x SL)

# ── FVG params ──────────────────────────────────────────────
STRONG_FVG_GAP       = 10
STRONG_FVG_BODY      = 20
MIN_FVG_BODY         = 10
MIN_FVG_SIZE         = 5      # [V6-F6] skip FVGs < 5pts
MIN_CANDLES_BEFORE_ENTRY = 6   # [P5] 30 min before non-ORB/BOS strategies
FVG_MAX_AGE_CANDLES  = 3      # [F5] tighter freshness

# ── ORB params ──────────────────────────────────────────────
ORB_END_TIME         = datetime.time(9, 45)
ORB_MAX_RANGE        = 100    # [V6-F2] skip ORB if range > 100pts (noise)

# ── Session / time controls ─────────────────────────────────
SESSION_BIAS_END     = datetime.time(10, 0)
TIME_EXIT_AFTER      = datetime.time(14, 0)
TRADE_MAX_DURATION   = 30     # [F9] hard 30-min per-trade exit
MAX_TRADES           = 999999 # PAPER: truly uncapped for max data capture

# ── Capital ─────────────────────────────────────────────────
CAPITAL_PER_TRADE    = 8000   # 1 lot @ ~Rs.123/share
CAPITAL_REDUCED      = 4000
DAILY_LOSS_LIMIT     = 999999   # PAPER: log everything
DAILY_PROFIT_TARGET  = 999999   # PAPER: let all signals fire
LOT_SIZE             = 65     # Nifty lot (revised Jan 2026)
OTM_OFFSET           = 100

# ── Market microstructure ────────────────────────────────────
NIFTY_KEY            = "NSE_INDEX|Nifty 50"
MIN_RVOL             = 1.0
EMASTACK_MIN_RVOL    = 1.3    # [F7]
EMA50_TOLERANCE      = 20
ZSCORE_WINDOW        = 6
ZSCORE_THRESHOLD     = 2.0    # [V6-F4] relaxed from 2.5 — allow more counter-trend
RSI_MEAN_REV_OB      = 65     # [V6-F4] relaxed from 70 — catch reversals earlier
RSI_MEAN_REV_OS      = 35     # [V6-F4] relaxed from 30
PCR_FRESH_SECS       = 900
PCR_STALE_SECS       = 1800
GAP_FILTER_PCT       = 0.5
ATR_PERIOD           = 14
ATR_TRENDING_MIN     = 35    # [V9-F3] Group A trend strategies — need high momentum
ATR_STRUCTURE_MIN    = 28    # [V9-F4] Group B structure strategies
ATR_REVERSAL_MIN     = 28    # [V9-F4] Group C reversal strategies — need room to hit target
                             # RSIDivergence: target=16pts, need ATR>16*1.5=24pts min
                             # Set to 28pts for safety margin (May 15: ATR=25 hit SL)
ATR_MIN_FOR_TRADE    = 20     # [F29] block all strategies below 20pts ATR
VWAP_CROSS_VOL_MIN   = 1.2
RSI_PERIOD           = 14
RSI_OVERBOUGHT       = 65
RSI_OVERSOLD         = 40     # [V6-F5] raised from 35 — block shorts earlier at oversold

# ── Confidence thresholds ────────────────────────────────────
HIGH_CONF_REENTRY    = 9      # [V6-F3] raised from 8 — stricter post-loss re-entry
PERFECT_CONF         = 10     # [V6-F3] 10/10 bypasses StratTracker always
SLIPPAGE_PTS         = 3      # [F13] paper slippage sim
VIX_SPIKE_PCT        = 20     # [F12] suspend if VIX spikes >20%

# ── New strategy params ──────────────────────────────────────
BOS_SWING_LOOKBACK   = 10     # [V6-F8] candles to look back for swing H/L
BOS_MIN_MOVE         = 8      # pts beyond swing to confirm BOS
RSI_DIV_LOOKBACK     = 6      # [V6-F10] candles to detect RSI divergence

# SuperTrend params (Strategy 8)
SUPERTREND_PERIOD    = 10
SUPERTREND_MULT      = 3.0

# CPR params (Strategy 9)
CPR_BREAKOUT_BUFFER  = 5      # pts buffer above/below CPR for entry

# Minimum confidence per strategy (rescaled for /10 score)
MIN_CONF = {
    "StrongFVG"     : 7,
    "ORB+EMA"       : 6,
    "EMAStack"      : 6,
    "VWAPBand"      : 5,
    "VWAPCross"     : 4,
    "EMA50Bounce"   : 7,
    "EMACross"      : 5,
    "SuperTrend"    : 6,
    "CPR"           : 6,
    "BOS"           : 7,    # [V6-F8] strong structural signal — needs high conf
    "ORPH_ORPL"     : 6,    # [V6-F9]
    "RSIDivergence" : 6,    # [V6-F10]
}
HIGH_CONF   = 8
MEDIUM_CONF = 5

# ─────────────────────────────────────────────
#  [V7-F2] PER-STRATEGY VALIDATION RULES
#  Each strategy controls its own:
#   rsi_long_max      — max RSI allowed for bullish entry
#   rsi_short_min     — min RSI allowed for bearish entry
#   allow_counter_trend — bypass session bias block
#   session_bias_over_auto — session bias wins over auto_bias
#   bias_mismatch_ok  — skip pre_bias gate entirely
#   min_conf          — strategy-specific minimum confidence
# ─────────────────────────────────────────────
PER_STRATEGY_RULES = {
    # [V7-F3] BOS — Momentum/breakout. High RSI = breakout strength.
    #   May 13: All 11 BOS signals at RSI 67-80 were bullish breakouts.
    #   RSI 80 on a breakout = STRONG momentum, not overbought.
    "BOS": {
        "rsi_long_max"          : 78,   # [FIX3b] 85 allowed RSI 80 overbought entries → SL. 78 is enough for momentum
        "rsi_short_min"         : 15,   # very low — momentum confirms BOS
        "allow_counter_trend"   : False, # BOS direction = structure direction
        "session_bias_over_auto": True,  # session bias > auto_bias
        "bias_mismatch_ok"      : True,  # don't block on auto_bias mismatch
        "min_conf"              : 7,
    },
    # [V7-F4] StrongFVG — Structural gap. FVG edge is the signal, not RSI.
    #   May 13: 37 skips — RSI 65-78 bullish FVGs all blocked.
    #   Counter-trend allowed when FVG is strong (handled in detector).
    "StrongFVG": {
        "rsi_long_max"          : 78,   # raised from 65 — FVG is structural
        "rsi_short_min"         : 25,   # lowered — bearish FVG = structural
        "allow_counter_trend"   : True,  # FVG can be counter-trend signal
        "session_bias_over_auto": True,
        "bias_mismatch_ok"      : True,  # FVG doesn't need bias confirmation
        "min_conf"              : 6,    # lowered from 7
    },
    # [V7-F5] VWAPBand — Band breaks happen at momentum extremes.
    #   May 13: RSI 68-70 bullish band breaks all blocked.
    #   RSI 70 during a bullish VWAP band break = breakout confirmation.
    "VWAPBand": {
        "rsi_long_max"          : 75,   # raised from 65
        "rsi_short_min"         : 28,
        "allow_counter_trend"   : False,
        "session_bias_over_auto": True,  # session wins over auto_bias
        "bias_mismatch_ok"      : True,  # if session matches, ignore auto_bias
        "min_conf"              : 5,
    },
    # [V7-F6] EMACross — Trend-following. Follow session bias, not auto_bias.
    #   May 13: RSI 45-61 signals blocked by BiasMismatch (auto=bearish, session=bullish).
    #   Session bias should win for a trend-following strategy.
    "EMACross": {
        "rsi_long_max"          : 70,
        "rsi_short_min"         : 32,
        "allow_counter_trend"   : False, # trend-following — no counter
        "session_bias_over_auto": True,  # session bias wins [V7-F14]
        "bias_mismatch_ok"      : True,  # ignore auto_bias, trust session
        "min_conf"              : 5,
    },
    # [V7-F7] RSIDivergence — BY DEFINITION a counter-trend signal.
    #   May 13: 11 skips — all blocked for being counter-trend.
    #   Counter-trend IS the strategy. LowConf min lowered 6→4.
    "RSIDivergence": {
        "rsi_long_max"          : 70,
        "rsi_short_min"         : 30,
        "allow_counter_trend"   : True,  # it IS counter-trend
        "session_bias_over_auto": False,
        "bias_mismatch_ok"      : True,  # divergence doesn't need bias align
        "min_conf"              : 4,    # lowered from 6
    },
    # [V7-F8] VWAPCross — 2-candle cross. Moderate RSI zone.
    "VWAPCross": {
        "rsi_long_max"          : 68,
        "rsi_short_min"         : 32,
        "allow_counter_trend"   : False,
        "session_bias_over_auto": True,
        "bias_mismatch_ok"      : False,
        "min_conf"              : 4,
    },
    # [V7-F9] EMAStack — Full stack = strong trending. RSI can be elevated.
    "EMAStack": {
        "rsi_long_max"          : 72,
        "rsi_short_min"         : 28,
        "allow_counter_trend"   : False,
        "session_bias_over_auto": True,
        "bias_mismatch_ok"      : False,
        "min_conf"              : 6,
    },
    # [V7-F10] EMA50Bounce — Needs RSI neutrality to have room to move.
    "EMA50Bounce": {
        "rsi_long_max"          : 60,   # tightest — needs room to rally
        "rsi_short_min"         : 40,   # tightest — needs room to fall
        "allow_counter_trend"   : False,
        "session_bias_over_auto": False,
        "bias_mismatch_ok"      : False,
        "min_conf"              : 7,
    },
    # [V7-F11] SuperTrend — Trend flip. Similar to EMAStack.
    "SuperTrend": {
        "rsi_long_max"          : 72,
        "rsi_short_min"         : 28,
        "allow_counter_trend"   : False,
        "session_bias_over_auto": True,
        "bias_mismatch_ok"      : False,
        "min_conf"              : 6,
    },
    # [V7-F12] CPR — Pivot breakout. Moderate RSI.
    "CPR": {
        "rsi_long_max"          : 70,
        "rsi_short_min"         : 30,
        "allow_counter_trend"   : False,
        "session_bias_over_auto": True,
        "bias_mismatch_ok"      : False,
        "min_conf"              : 6,
    },
    # [V7-F13] ORPH_ORPL — Gap continuation. RSI extremes = gap strength.
    "ORPH_ORPL": {
        "rsi_long_max"          : 80,
        "rsi_short_min"         : 20,
        "allow_counter_trend"   : False,
        "session_bias_over_auto": True,
        "bias_mismatch_ok"      : True,
        "min_conf"              : 6,
    },
    # ORB+EMA — breakout strategy. RSI elevated on breakouts is strength not overbought.
    # [FIX-B] rsi_long_max raised 72→80 (ORB breakouts always have elevated RSI)
    "ORB+EMA": {
        "rsi_long_max"          : 80,
        "rsi_short_min"         : 20,
        "allow_counter_trend"   : False,
        "session_bias_over_auto": True,
        "bias_mismatch_ok"      : True,   # ORB direction from price, not bias
        "min_conf"              : 6,
    },
}

# Fallback rules for any strategy not in PER_STRATEGY_RULES
DEFAULT_STRATEGY_RULES = {
    "rsi_long_max"          : 65,
    "rsi_short_min"         : 35,
    "allow_counter_trend"   : False,
    "session_bias_over_auto": False,
    "bias_mismatch_ok"      : False,
    "min_conf"              : 5,
}

# CSV log file names (v8)
SCAN_LOG_FILE  = f"scan_log_v8_{datetime.date.today()}.csv"
TRADE_LOG_FILE = f"trade_log_v8_{datetime.date.today()}.csv"
SKIP_LOG_FILE  = f"skip_log_v8_{datetime.date.today()}.csv"

# ─────────────────────────────────────────────
#  [V8-F1] STRATEGY GROUPS — multi-trade config
# ─────────────────────────────────────────────
STRATEGY_GROUPS = {
    "A": {  # Trend-following
        "strategies" : ["EMAStack","EMACross","SuperTrend","BOS"],  # [V9-F1] ORB+EMA removed
        "capital"    : 8000,
        "atr_min"    : 35,    # [V9-F4] trend needs strong momentum
        "sl"         : 12,
        "target"     : 20,   # 1:1.67 RR
        "trail_start": 12,
        "trail_dist" : 10,
    },
    "B": {  # Structure / momentum
        "strategies" : ["StrongFVG","EMA50Bounce","VWAPBand","ORPH_ORPL"],
        "capital"    : 8000,
        "atr_min"    : 20,    # [V9-F4] structure — VWAPBand works well at ATR 22-28
        "sl"         : 10,
        "target"     : 18,   # 1:1.8 RR
        "trail_start": 10,
        "trail_dist" : 8,
    },
    "C": {  # Reversal / mean-reversion
        "strategies" : ["RSIDivergence","VWAPCross","CPR"],
        "capital"    : 7000,
        "atr_min"    : 23,    # [V9-F4] Group C tgt=16pts, need ATR>16*1.4=22.4 → 23pts min
        "sl"         : 8,
        "target"     : 16,   # 1:2.0 RR
        "trail_start": 8,
        "trail_dist" : 6,
    },
}
MAX_CONCURRENT_GROUPS = 2   # [V8-F1] max 2 groups active at once

# Build reverse lookup: strategy → group config
def _get_group(strategy_name):
    for gid, gcfg in STRATEGY_GROUPS.items():
        if strategy_name in gcfg["strategies"]:
            return gid, gcfg
    return "A", STRATEGY_GROUPS["A"]   # fallback

IST = pytz.timezone("Asia/Kolkata")
def now_ist(): return datetime.datetime.now(IST)
def ist_time(): return now_ist().time()

TRADE_START   = datetime.time(9, 30)
TRADE_END     = datetime.time(14, 30)
EXPIRY_STOP   = datetime.time(13, 0)
REMINDER_TIME = datetime.time(9, 0)

def get_headers():
    return {"Accept": "application/json",
            "Authorization": f"Bearer {config.LIVE_TOKEN}"}


# ─────────────────────────────────────────────
#  [F15] TOKEN HEALTH CHECK
# ─────────────────────────────────────────────
def check_token_health():
    """Verify live token is valid before starting the bot."""
    try:
        resp = requests.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            headers=get_headers(),
            params={"instrument_key": NIFTY_KEY},
            timeout=5
        )
        data = resp.json()
        if data.get("status") == "success":
            ltp = float(list(data["data"].values())[0]["last_price"])
            log.info(f"Token healthy. Nifty LTP: {ltp}")
            return True, ltp
        else:
            log.error(f"Token check failed: {data}")
            return False, None
    except Exception as e:
        log.error(f"Token health check error: {e}")
        return False, None


# ─────────────────────────────────────────────
#  [F3] BOT STATE — crash-safe P&L persistence
# ─────────────────────────────────────────────
STATE_FILE = "bot_state.json"

def load_state():
    """
    Load today's state from file. Returns fresh state if new day.
    Also restores manual_bias so /bias command survives bot restarts.
    """
    today = str(datetime.date.today())
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
            if state.get("date") == today:
                log.info(f"Resumed state: P&L={state['pnl']} trades={state['trades']} "
                         f"bias={state.get('manual_bias','neutral')}")
                return state
    except Exception as e:
        log.error(f"State load error: {e}")
    return {
        "date": today, "pnl": 0.0, "trades": 0,
        "wins": 0, "losses": 0, "timeouts": 0,
        "skipped": 0, "consec_loss": 0,
        "fvg": 0, "orb": 0, "ema_stack": 0,
        "vwap_band": 0, "vwap_cross": 0,
        "ema_cross": 0, "ema50": 0,
        "supertrend": 0, "cpr": 0,
        "high_t": 0, "high_w": 0,
        "med_t": 0, "med_w": 0,
        "low_t": 0, "low_w": 0,
        "manual_bias": "neutral"   # persisted /bias command
    }

def save_state(stats, manual_bias="neutral"):
    """
    Persist state after every trade and every /bias change.
    manual_bias is saved separately so bot restarts honour last /bias.
    """
    try:
        stats["date"]        = str(datetime.date.today())
        stats["manual_bias"] = manual_bias
        with open(STATE_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log.error(f"State save error: {e}")


# ─────────────────────────────────────────────
#  [F10] CAPITAL MANAGER — with recovery logic
# ─────────────────────────────────────────────
class CapitalManager:
    def __init__(self):
        self.consec_losses = 0
        self.consec_wins   = 0
        self.reduced       = False

    def on_result(self, result):
        """
        [F18] Unified loss tracking. timeout_duration counts as a loss
        for capital management because options lose value via theta.
        """
        if result == "target":
            self.consec_losses = 0
            self.consec_wins  += 1
            # [F10] Recover full capital after 2 wins in reduced mode
            if self.reduced and self.consec_wins >= 2:
                self.reduced = False
                log.info("Capital restored: 2 wins after reduction")
        elif result in ["sl", "timeout", "timeout_theta", "timeout_duration"]:
            # [F18] All loss/timeout types reduce capital eventually
            self.consec_losses += 1
            self.consec_wins    = 0
            if self.consec_losses >= 2:
                self.reduced = True

    def get_capital(self):
        return CAPITAL_REDUCED if self.reduced else CAPITAL_PER_TRADE

    def get_info(self):
        return (f"Rs.{self.get_capital():.0f} "
                f"{'(REDUCED)' if self.reduced else '(NORMAL)'} "
                f"[CL:{self.consec_losses} CW:{self.consec_wins}]")


# ─────────────────────────────────────────────
#  [F1] SESSION BIAS + Z-SCORE
# ─────────────────────────────────────────────
class SessionBias:
    def __init__(self):
        self.bias          = "neutral"
        self.is_set        = False
        self.price_history = []
        self.vix_open      = None   # [F12] VIX spike guard

    def update(self, ltp, df_5, vix_current=None):
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
                log.info(f"Session bias set: {self.bias}")
        # [F12] Record VIX at session open
        if vix_current and self.vix_open is None:
            self.vix_open = vix_current
        self.price_history.append(ltp)
        if len(self.price_history) > ZSCORE_WINDOW * 2:
            self.price_history.pop(0)

    def vix_spike_detected(self, vix_current):
        """[F12] Return True if VIX has spiked >VIX_SPIKE_PCT% from open."""
        if self.vix_open and vix_current:
            spike = ((vix_current - self.vix_open) / self.vix_open) * 100
            if spike > VIX_SPIKE_PCT:
                log.warning(f"VIX spike detected: {spike:.1f}% from open {self.vix_open:.2f}")
                return True
        return False

    def get_zscore(self, ltp):
        if len(self.price_history) < ZSCORE_WINDOW:
            return 0.0
        w    = self.price_history[-ZSCORE_WINDOW:]
        mean = np.mean(w); std = np.std(w)
        return round((ltp - mean) / std, 2) if std > 0 else 0.0

    def trade_allowed(self, direction, ltp, rsi):
        """
        [V6-F4] Relaxed counter-trend: RSI_MEAN_REV thresholds loosened to 65/35
        (was 70/30). Missed May 11 recovery because RSI=62 wasn't 'extreme' enough.
        Z-score threshold also relaxed to 2.0 (was 2.5).
        """
        if not self.is_set or self.bias == "neutral":
            return True, 0.0, "Session neutral — all trades allowed"
        zs = self.get_zscore(ltp)
        if self.bias == direction:
            return True, zs, f"Trend trade — session {self.bias} matches"
        # [V6-F4] Counter-trend: both Z-score AND RSI check (relaxed thresholds)
        z_ok   = abs(zs) >= ZSCORE_THRESHOLD
        rsi_ok = (direction == "bullish" and rsi < RSI_MEAN_REV_OS) or \
                 (direction == "bearish" and rsi > RSI_MEAN_REV_OB)
        if z_ok and rsi_ok:
            return True, zs, f"Mean rev allowed — Z:{zs:+.2f} RSI:{rsi:.0f}"
        # [V6-F4] Also allow if RSI is moderately extreme (not just extreme)
        rsi_moderate = (direction == "bullish" and rsi < 45) or \
                       (direction == "bearish" and rsi > 55)
        if z_ok and rsi_moderate:
            return True, zs, f"Moderate mean rev — Z:{zs:+.2f} RSI:{rsi:.0f}"
        return False, zs, (f"Counter-trend blocked — session:{self.bias} "
                           f"Z:{zs:+.2f}({'ok' if z_ok else 'weak'}) "
                           f"RSI:{rsi:.0f}({'ok' if rsi_ok else 'not extreme'})")


# ─────────────────────────────────────────────
#  LIVE EXPIRY FETCH + PCR CACHE (15-min refresh)
# ─────────────────────────────────────────────
def get_live_expiries(instrument_key=None):
    """
    Fetch actual available expiry dates from Upstox option/contract endpoint.
    Returns list sorted nearest-first. No hardcoded weekday math — works
    regardless of NSE expiry day changes (Thursday→Monday etc).
    """
    if instrument_key is None:
        instrument_key = NIFTY_KEY
    try:
        resp = requests.get(
            "https://api.upstox.com/v2/option/contract",
            headers=get_headers(),
            params={"instrument_key": instrument_key},
            timeout=10
        )
        data = resp.json()
        if data.get("status") != "success" or not data.get("data"):
            log.warning(f"get_live_expiries: {data.get('status')} — {data.get('message','')}")
            return []
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        expiries  = sorted(set(
            item["expiry"] for item in data["data"]
            if "expiry" in item and item["expiry"] >= today_str
        ))
        log.info(f"Live expiries: {expiries[:6]}")
        return expiries
    except Exception as e:
        log.error(f"get_live_expiries error: {e}")
        return []


class PCRCache:
    def __init__(self):
        self.val = None; self.bias = "neutral"; self.time = None

    def age(self):
        if self.time is None: return 9999
        return (datetime.datetime.now() - self.time).seconds

    def fetch(self):
        """Fetch PCR using live expiry dates — no hardcoded date guessing."""
        try:
            expiries = get_live_expiries()
            if not expiries:
                log.warning("PCR: no live expiries available")
                return self.val, self.bias, "stale"

            for expiry_str in expiries[:5]:
                resp = requests.get(
                    "https://api.upstox.com/v2/option/chain",
                    headers=get_headers(),
                    params={"instrument_key": NIFTY_KEY,
                            "expiry_date": expiry_str},
                    timeout=10)
                data = resp.json()
                if data.get("status") != "success" or not data.get("data"):
                    log.info(f"PCR: {expiry_str} — no data, trying next")
                    continue
                pe = ce = 0
                for r in data["data"]:
                    p = r.get("put_options",  {}) or {}
                    c = r.get("call_options", {}) or {}
                    if p.get("market_data"): pe += p["market_data"].get("oi", 0)
                    if c.get("market_data"): ce += c["market_data"].get("oi", 0)
                if ce > 0 and (pe + ce) > 1000:
                    pcr  = round(pe / ce, 2)
                    # Tightened: >1.1 bullish, <0.9 bearish (was 1.2/0.8)
                    # [P4] 0.85-1.15 = neutral for Nifty weekly options
                    bias = "bullish" if pcr > 1.15 else "bearish" if pcr < 0.85 else "neutral"
                    self.val  = pcr; self.bias = bias
                    self.time = datetime.datetime.now()
                    log.info(f"PCR:{pcr}({bias}) expiry:{expiry_str} PE:{pe} CE:{ce}")
                    return pcr, bias, "fresh"
                else:
                    log.info(f"PCR: {expiry_str} — low OI (PE:{pe} CE:{ce}), next")

            log.warning("PCR: all expiries had zero/low OI")
            return self.val, self.bias, "stale"
        except Exception as e:
            log.error(f"PCR fetch error: {e}")
            return self.val, self.bias, "error"

    def get(self):
        a = self.age()
        if a < PCR_FRESH_SECS:  return self.val, self.bias, 1.0, "fresh"
        if a < PCR_STALE_SECS:  return self.val, "neutral", 0.5, "stale"
        return None, "neutral", 0.0, "excluded"

    def should_refresh(self): return self.age() >= PCR_FRESH_SECS


# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id": config.CHAT_ID, "text": message, "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            plain = message.replace("<b>", "").replace("</b>", "")
            requests.post(url, data={"chat_id": config.CHAT_ID, "text": plain}, timeout=10)
    except Exception as e: log.error(f"TG:{e}")

def tg(icon, title, lines):
    body = "\n".join([f"  {l}" for l in lines])
    send_telegram(f"{icon} <b>{title}</b>\n{body}")
    log.info(f"[TG] {title}")

def send_csv_files():
    files = [
        (_LOG_FILES.get("scan",  "scan_log_v5.csv"),  "Nifty Scan v8  — 5min market snapshots"),
        (_LOG_FILES.get("trade", "trade_log_v5.csv"), "Nifty Trade v8 — all completed trades"),
        (_LOG_FILES.get("skip",  "skip_log_v5.csv"),  "Nifty Skip v8  — all rejected signals"),
    ]
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    send_telegram(f"📊 <b>Nifty v9 CSVs — {date_str}</b>")
    sent = 0
    for fname, caption in files:
        if not os.path.exists(fname): continue
        try:
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
            with open(fname, "rb") as f:
                resp = requests.post(url, data={"chat_id": config.CHAT_ID, "caption": caption},
                                     files={"document": f}, timeout=30)
            if resp.json().get("ok"): sent += 1
        except Exception as e: log.error(f"CSV send:{e}")
    send_telegram(f"✅ Sent {sent}/3 files — upload all 3 to Claude for analysis!")


# ─────────────────────────────────────────────
#  TELEGRAM LISTENER
# ─────────────────────────────────────────────
class TelegramListener:
    def __init__(self, stats_ref, cap_mgr_ref, session_bias_ref):
        self.bias            = "neutral"
        self.last_update_id  = 0
        self._running        = False
        self._stats          = stats_ref
        self._cap            = cap_mgr_ref
        self._sb             = session_bias_ref
        self._auto_bias      = "neutral"  # from nifty_auto_bias (read-only display)

    def restore_bias(self, saved_bias):
        """[V8-NO-MANUAL-BIAS] Manual bias removed — always neutral."""
        self.bias = "neutral"

    def set_auto_bias(self, bias):
        """Called after get_combined_bias_nifty so /status shows both."""
        self._auto_bias = bias

    def start(self):
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        while self._running:
            try:
                url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
                resp = requests.get(url, params={"offset": self.last_update_id + 1,
                                                  "timeout": 30}, timeout=35)
                if resp.status_code != 200: time.sleep(5); continue
                for update in resp.json().get("result", []):
                    self.last_update_id = update["update_id"]
                    text = update.get("message", {}).get("text", "").strip().lower()
                    if text.startswith("/bias"):
                        # [V8-NO-MANUAL-BIAS] Manual bias removed.
                        # Bot runs fully on auto-bias (PCR + FII + gap + trend).
                        send_telegram(
                            "ℹ️ <b>Manual bias removed in v8</b>\n"
                            "  Bot runs on auto-bias only:\n"
                            f"  Auto bias  : {self._auto_bias.upper()}\n"
                            f"  Session    : {self._sb.bias.upper()}\n"
                            "  Use /status to see current bias."
                        )
                    elif text == "/status":
                        s  = self._stats
                        wr = (s["wins"] / s["trades"] * 100) if s["trades"] > 0 else 0
                        send_telegram(
                            f"📊 <b>Nifty Bot v9 STATUS</b>\n"
                            f"  Time       : {now_ist().strftime('%H:%M:%S IST')}\n"
                            f"  Auto bias  : {self._auto_bias.upper()}\n"
                            f"  Session    : {self._sb.bias.upper()}\n"
                            f"  Trades     : {s['trades']} (uncapped paper)\n"
                            f"  Wins       : {s['wins']} | Losses: {s['losses']}\n"
                            f"  Win rate   : {wr:.1f}%\n"
                            f"  Day P&L    : Rs.{s['pnl']:+.0f}\n"
                            f"  Capital    : {self._cap.get_info()}\n"
                            f"  Consec L   : {s['consec_loss']}"
                        )
                    elif text == "/report":
                        send_csv_files()
                    elif text == "/help":
                        send_telegram(
                            "📋 <b>Commands:</b>\n"
                            "  /status — current bot state + auto bias\n"
                            "  /report — send CSV logs to this chat\n"
                            "  /news   — run news scan\n"
                            "  /help   — this message\n"
                            "  Note: Manual /bias removed. Bot runs on auto-bias only."
                        )
            except Exception as e: log.error(f"TG poll:{e}"); time.sleep(5)


# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_nifty_ltp():
    try:
        resp = requests.get("https://api.upstox.com/v2/market-quote/ltp",
                            headers=get_headers(),
                            params={"instrument_key": NIFTY_KEY}, timeout=5)
        data = resp.json()
        if data["status"] == "success":
            key = list(data["data"].keys())[0]
            return float(data["data"][key]["last_price"])
        return None
    except Exception as e: log.error(f"LTP:{e}"); return None

def get_india_vix():
    try:
        resp = requests.get("https://api.upstox.com/v2/market-quote/ltp",
                            headers=get_headers(),
                            params={"instrument_key": "NSE_INDEX|India VIX"}, timeout=5)
        data = resp.json()
        if data["status"] == "success":
            key = list(data["data"].keys())[0]
            return float(data["data"][key]["last_price"])
        return None
    except Exception: return None

def get_candles(interval_val=5):
    try:
        url = (f"https://api.upstox.com/v3/historical-candle/intraday/"
               f"{NIFTY_KEY}/minutes/{interval_val}")
        resp = requests.get(url, headers=get_headers(), timeout=10)
        data = resp.json()
        if data["status"] != "success": return None
        candles = data["data"]["candles"]
        if not candles: return None
        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "oi"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df
    except Exception as e: log.error(f"Candle:{e}"); return None

def get_futures_candles(interval_val=5):
    try:
        today = datetime.date.today()
        month = today.strftime("%b").upper()[:3]
        year  = today.strftime("%y")
        for key in [f"NSE_FO|NIFTY{year}{month}FUT", f"NSE_FO|NIFTY{month}{year}FUT"]:
            try:
                url = (f"https://api.upstox.com/v3/historical-candle/intraday/"
                       f"{key}/minutes/{interval_val}")
                resp = requests.get(url, headers=get_headers(), timeout=10)
                data = resp.json()
                if data["status"] != "success": continue
                candles = data["data"]["candles"]
                if not candles: continue
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
                return df
            except Exception: continue
        return None
    except Exception as e: log.error(f"Futures:{e}"); return None

def get_prev_day_ohlc():
    try:
        today   = datetime.date.today()
        from_dt = (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        to_dt   = today.strftime("%Y-%m-%d")
        url     = (f"https://api.upstox.com/v3/historical-candle/"
                   f"{NIFTY_KEY}/days/1/{to_dt}/{from_dt}")
        resp    = requests.get(url, headers=get_headers(), timeout=10)
        data    = resp.json()
        if data["status"] != "success" or not data["data"]["candles"]: return None
        candles = data["data"]["candles"]
        prev    = candles[-2] if len(candles) >= 2 else candles[-1]
        return {"open": float(prev[1]), "high": float(prev[2]),
                "low": float(prev[3]),  "close": float(prev[4])}
    except Exception as e: log.error(f"Prev OHLC:{e}"); return None


# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_atr(df, period=ATR_PERIOD):
    try:
        df = df.copy()
        df["prev_close"] = df["close"].shift(1)
        df["tr"] = df[["high", "low", "prev_close"]].apply(
            lambda r: max(r["high"] - r["low"],
                          abs(r["high"] - r["prev_close"]),
                          abs(r["low"]  - r["prev_close"])), axis=1)
        return round(float(df["tr"].ewm(span=period, adjust=False).mean().iloc[-1]), 1)
    except Exception as _e: log.debug(f"ATR calc: {_e}"); return 20.0

def calc_rsi(df, period=RSI_PERIOD):
    try:
        delta = df["close"].astype(float).diff()
        gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
        rs    = gain / loss
        rsi   = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 1)
    except Exception as _e: log.debug(f"RSI calc: {_e}"); return 50.0

def calc_vwap_bands(df, atr):
    df = df.copy()
    df["volume"]   = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    df["typical"]  = (df["high"] + df["low"] + df["close"]) / 3

    # Guard: if all volume is 0 (Upstox index candles often have no volume)
    # fall back to equal-weight VWAP (treat each candle as weight=1)
    total_vol = df["volume"].sum()
    if total_vol == 0:
        df["weight"]  = 1.0
    else:
        df["weight"]  = df["volume"]

    df["cum_vol"]  = df["weight"].cumsum()
    df["cum_tv"]   = (df["typical"] * df["weight"]).cumsum()
    df["vwap"]     = df["cum_tv"] / df["cum_vol"]
    df["cum_tv2"]  = (((df["typical"] - df["vwap"]) ** 2) * df["weight"]).cumsum()
    df["sd"]       = np.sqrt((df["cum_tv2"] / df["cum_vol"]).clip(lower=0))
    mult = 1.5 if atr > 30 else 1.0 if atr > 15 else 0.75
    df["vwap_u1"]  = df["vwap"] + mult * df["sd"]
    df["vwap_l1"]  = df["vwap"] - mult * df["sd"]
    df["vwap_u2"]  = df["vwap"] + 2 * mult * df["sd"]
    df["vwap_l2"]  = df["vwap"] - 2 * mult * df["sd"]
    df["band_width"] = df["vwap_u1"] - df["vwap_l1"]
    return df

def calc_ema(df, periods=[9, 21, 50]):
    df = df.copy()
    for p in periods:
        df[f"ema{p}"] = df["close"].astype(float).ewm(span=p, adjust=False).mean()
    return df

def calc_rvol(df, fut_df=None):
    try:
        if fut_df is not None and len(fut_df) >= 5:
            vol = fut_df["volume"].astype(float)
            if vol.sum() > 0 and vol.std() > 0:
                avg = float(vol.mean()); cur = float(vol.iloc[-1])
                if avg > 0: return round(max(0.5, min(5.0, cur / avg)), 2)
        if df is not None and len(df) >= 5:
            oi = df["oi"].astype(float)
            if oi.sum() > 0 and oi.std() > 0:
                oi_chg = oi.diff().abs().fillna(0)
                avg = float(oi_chg.mean()); cur = float(oi_chg.iloc[-1])
                if avg > 0: return round(max(0.5, min(5.0, cur / avg)), 2)
        return 1.2
    except Exception as _e: log.debug(f"RVOL calc: {_e}"); return 1.2

def calc_supertrend(df, period=SUPERTREND_PERIOD, mult=SUPERTREND_MULT):
    """
    [F25] Corrected SuperTrend implementation.

    Standard formula:
      basic_upper = (high+low)/2 + multiplier * ATR
      basic_lower = (high+low)/2 - multiplier * ATR

      final_upper[i] = min(basic_upper[i], final_upper[i-1])
                       if close[i-1] <= final_upper[i-1] else basic_upper[i]
      final_lower[i] = max(basic_lower[i], final_lower[i-1])
                       if close[i-1] >= final_lower[i-1] else basic_lower[i]

      direction[i] = bullish if close[i] > final_upper[i-1] (was bearish, now flipped)
                  or close[i] >= final_lower[i] and previously bullish
    """
    try:
        df = df.copy().reset_index(drop=True)
        if len(df) < period + 2:
            return "neutral", 0, False

        # Vectorized ATR (avoids the slow per-row apply)
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs()
        ], axis=1).max(axis=1)
        atr_st = tr.ewm(span=period, adjust=False).mean()

        hl2          = (df["high"] + df["low"]) / 2
        basic_upper  = hl2 + mult * atr_st
        basic_lower  = hl2 - mult * atr_st

        # Final bands (locked behaviour)
        # Use np.array (writable) instead of .values which can be read-only
        final_upper = np.array(basic_upper.values, dtype=float)
        final_lower = np.array(basic_lower.values, dtype=float)
        close       = df["close"].values

        for i in range(1, len(df)):
            # Use PREVIOUS close for the lock comparison (correct standard formula)
            if close[i-1] <= final_upper[i-1]:
                final_upper[i] = min(basic_upper.iloc[i], final_upper[i-1])
            else:
                final_upper[i] = basic_upper.iloc[i]
            if close[i-1] >= final_lower[i-1]:
                final_lower[i] = max(basic_lower.iloc[i], final_lower[i-1])
            else:
                final_lower[i] = basic_lower.iloc[i]

        # Direction calculation
        st_bull = [True] * len(df)   # True = bullish (price above lower band)
        for i in range(1, len(df)):
            if st_bull[i-1]:
                # Was bullish: stays bullish unless close drops below previous lower
                st_bull[i] = close[i] >= final_lower[i-1]
            else:
                # Was bearish: stays bearish unless close rises above previous upper
                st_bull[i] = close[i] > final_upper[i-1]

        # Current direction & level
        direction = "bullish" if st_bull[-1] else "bearish"
        level     = round(float(final_lower[-1] if st_bull[-1] else final_upper[-1]), 1)
        # Fresh = direction changed on the LAST closed candle
        fresh = st_bull[-1] != st_bull[-2]
        return direction, level, fresh
    except Exception as e:
        log.error(f"SuperTrend calc error: {e}")
        return "neutral", 0, False

def calc_cpr(prev_ohlc):
    """
    [NEW STRATEGY 9] Central Pivot Range.
    Returns pivot, BC (bottom central), TC (top central).
    """
    if prev_ohlc is None:
        return None, None, None
    H = prev_ohlc["high"]; L = prev_ohlc["low"]; C = prev_ohlc["close"]
    pivot = round((H + L + C) / 3, 1)
    bc    = round((H + L) / 2, 1)
    tc    = round((pivot - bc) + pivot, 1)
    if tc < bc: tc, bc = bc, tc  # ensure TC > BC
    return pivot, bc, tc


# ─────────────────────────────────────────────
#  TREND DETECTION (multi-timeframe)
# ─────────────────────────────────────────────
def detect_trend_relaxed(df, min_agree=3):
    if df is None or len(df) < 4: return "neutral", "Not enough", 0
    recent = df.tail(4)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]
    hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
    hl = sum(1 for i in range(1, len(lows))  if lows[i]  > lows[i-1])
    ll = sum(1 for i in range(1, len(lows))  if lows[i]  < lows[i-1])
    lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i-1])
    bull = min(hh, hl); bear = min(ll, lh)
    if bull >= min_agree: return "bullish", f"HH:{hh} HL:{hl}", bull
    if bear >= min_agree: return "bearish", f"LL:{ll} LH:{lh}", bear
    return "neutral", f"HH:{hh} HL:{hl} LL:{ll} LH:{lh}", 0

def detect_trend_multi(df5, df15, df30, e9=0, e21=0, e50=0, ltp=0):
    t5,  _, _ = detect_trend_relaxed(df5)
    t15, _, _ = detect_trend_relaxed(df15)
    t30, _, _ = detect_trend_relaxed(df30)
    bull = [t5, t15, t30].count("bullish")
    bear = [t5, t15, t30].count("bearish")
    if bull >= 2: return "bullish", f"{t5}/{t15}/{t30}", "strong" if bull == 3 else "moderate"
    if bear >= 2: return "bearish", f"{t5}/{t15}/{t30}", "strong" if bear == 3 else "moderate"
    if e9 > 0 and e21 > 0 and e50 > 0 and ltp > 0:
        if e9 > e21 > e50 and ltp > e21: return "bullish", f"{t5}/{t15}/{t30}", "ema_confirmed"
        if e9 < e21 < e50 and ltp < e21: return "bearish", f"{t5}/{t15}/{t30}", "ema_confirmed"
    return "neutral", f"{t5}/{t15}/{t30}", "weak"


# ─────────────────────────────────────────────
#  STRATEGY DETECTORS
# ─────────────────────────────────────────────
def detect_fvg(df):
    """[F5] FVG: max 3 candles old, gap must still be intact."""
    if df is None or len(df) < 3: return None, "No candles"
    candles = df.tail(15)
    ltp_now  = float(candles["close"].iloc[-1])
    for i in range(len(candles) - 1, 1, -1):
        c1 = candles.iloc[i-2]; c2 = candles.iloc[i-1]; c3 = candles.iloc[i]
        body = abs(float(c2["close"]) - float(c2["open"]))
        if body < MIN_FVG_BODY: continue
        c1h = float(c1["high"]); c1l = float(c1["low"])
        c3h = float(c3["high"]); c3l = float(c3["low"])
        age = len(candles) - 1 - i
        if age > FVG_MAX_AGE_CANDLES: continue  # [F5] skip stale
        if c1h < c3l:
            # Bullish FVG — check gap still intact (price hasn't closed inside)
            if ltp_now < c3l:  # price retesting from below
                size   = round(c3l - c1h, 1)
                if size < MIN_FVG_SIZE: continue  # [V6-F6] skip tiny FVGs
                strong = size >= STRONG_FVG_GAP and body >= STRONG_FVG_BODY
                return {"type": "bullish", "top": round(c3l, 1), "bottom": round(c1h, 1),
                        "mid": round((c3l + c1h) / 2, 1), "edge": round(c3l, 1),
                        "size": size, "strong": strong, "age": age}, \
                       f"{'STRONG' if strong else 'WEAK'} Bull FVG {size:.1f}pts age:{age}c intact"
        if c1l > c3h:
            # Bearish FVG — check gap still intact
            if ltp_now > c3h:  # price retesting from above
                size   = round(c1l - c3h, 1)
                if size < MIN_FVG_SIZE: continue  # [V6-F6] skip tiny FVGs
                strong = size >= STRONG_FVG_GAP and body >= STRONG_FVG_BODY
                return {"type": "bearish", "top": round(c1l, 1), "bottom": round(c3h, 1),
                        "mid": round((c1l + c3h) / 2, 1), "edge": round(c3h, 1),
                        "size": size, "strong": strong, "age": age}, \
                       f"{'STRONG' if strong else 'WEAK'} Bear FVG {size:.1f}pts age:{age}c intact"
    return None, f"No valid FVG (max {FVG_MAX_AGE_CANDLES}c)"

def detect_orb(df, orb_high, orb_low, ltp):
    """
    [F6] ORB direction from actual breakout price.
    [F30] Require last CLOSED candle to close beyond ORB level.
    [V6-F2] Skip ORB if range > ORB_MAX_RANGE pts (noise day).
    """
    if orb_high is None or orb_low is None:
        return None, "ORB not formed yet"
    if df is None or len(df) < 2:
        return None, "ORB: insufficient candle data"
    if not ltp:
        return None, "ORB: no LTP"

    # [V6-F2] ORB range cap — wide range = choppy day, skip
    orb_range = round(orb_high - orb_low, 1)
    if orb_range > ORB_MAX_RANGE:
        return None, f"ORB range {orb_range:.0f}pts > {ORB_MAX_RANGE}pts cap — skip (noise day)"

    # Use the last CLOSED candle's close (df.iloc[-1] is current forming candle,
    # df.iloc[-2] is the last fully closed candle)
    try:
        last_close = float(df["close"].iloc[-2])
    except Exception:
        last_close = ltp

    # Require BOTH: LTP beyond level AND last closed candle beyond level
    if ltp > orb_high and last_close > orb_high and round(ltp - orb_high, 1) >= 5:
        return {"type": "bullish", "level": orb_high,
                "size": round(ltp - orb_high, 1)}, \
               f"ORB bullish {round(ltp-orb_high,1)}pts above {orb_high:.0f} (close confirmed)"
    if ltp < orb_low and last_close < orb_low and round(orb_low - ltp, 1) >= 5:
        return {"type": "bearish", "level": orb_low,
                "size": round(orb_low - ltp, 1)}, \
               f"ORB bearish {round(orb_low-ltp,1)}pts below {orb_low:.0f} (close confirmed)"
    # If LTP is beyond but candle hasn't closed yet, signal not valid yet
    if ltp > orb_high and last_close <= orb_high:
        return None, f"ORB tick break above {orb_high:.0f} — waiting for close confirm"
    if ltp < orb_low and last_close >= orb_low:
        return None, f"ORB tick break below {orb_low:.0f} — waiting for close confirm"
    return None, f"No ORB | Range {orb_low:.0f} to {orb_high:.0f}"

def detect_ema_stack(df_ema, ltp, t5, rvol):
    """[F7] EMAStack requires RVOL >= EMASTACK_MIN_RVOL."""
    try:
        e9  = float(df_ema["ema9"].iloc[-1])
        e21 = float(df_ema["ema21"].iloc[-1])
        e50 = float(df_ema["ema50"].iloc[-1])
        if rvol < EMASTACK_MIN_RVOL:
            return None, f"EMAStack blocked: RVOL {rvol}x < {EMASTACK_MIN_RVOL}x required"
        if ltp > e9 > e21 > e50 and t5 == "bullish":
            return {"type": "bullish", "e9": round(e9, 1), "e21": round(e21, 1), "e50": round(e50, 1)}, \
                   f"EMA Stack bull RVOL:{rvol}x E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
        if ltp < e9 < e21 < e50 and t5 == "bearish":
            return {"type": "bearish", "e9": round(e9, 1), "e21": round(e21, 1), "e50": round(e50, 1)}, \
                   f"EMA Stack bear RVOL:{rvol}x E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
        return None, "No EMA stack"
    except: return None, "EMA stack error"

def detect_ema_cross(df_ema, prev_df_ema):
    try:
        e9   = float(df_ema["ema9"].iloc[-1])
        e21  = float(df_ema["ema21"].iloc[-1])
        pe9  = float(prev_df_ema["ema9"].iloc[-1])
        pe21 = float(prev_df_ema["ema21"].iloc[-1])
        if pe9 <= pe21 and e9 > e21:
            return {"type": "bullish", "e9": round(e9, 1), "e21": round(e21, 1)}, \
                   f"EMA9 crossed above EMA21 at {e9:.0f}"
        if pe9 >= pe21 and e9 < e21:
            return {"type": "bearish", "e9": round(e9, 1), "e21": round(e21, 1)}, \
                   f"EMA9 crossed below EMA21 at {e9:.0f}"
        return None, f"No EMA cross gap {round(e9-e21, 1)}pts"
    except: return None, "EMA cross error"

def detect_vwap_band_break(df_vwap, ltp, t5, atr):
    try:
        last = df_vwap.iloc[-1]; prev = df_vwap.iloc[-2]
        vu1 = float(last["vwap_u1"]); vl1 = float(last["vwap_l1"])
        bw  = float(last.get("band_width", 0))
        pltp = float(prev["close"])
        if atr < ATR_TRENDING_MIN:
            return None, f"ATR {atr:.0f}pts too low"
        if bw < 30:
            return None, f"Bands too narrow {bw:.0f}pts"
        if pltp < vu1 and ltp > vu1 and t5 == "bullish":
            return {"type": "bullish", "level": round(vu1, 1)}, \
                   f"Broke above VWAP+1SD at {vu1:.0f} ATR:{atr:.0f}"
        if pltp > vl1 and ltp < vl1 and t5 == "bearish":
            return {"type": "bearish", "level": round(vl1, 1)}, \
                   f"Broke below VWAP-1SD at {vl1:.0f} ATR:{atr:.0f}"
        return None, f"No band break U1:{vu1:.0f} L1:{vl1:.0f}"
    except: return None, "VWAP band error"

def detect_vwap_cross(df_vwap, ltp, df_5):
    try:
        if len(df_vwap) < 3: return None, "Not enough candles"
        last  = df_vwap.iloc[-1]; prev = df_vwap.iloc[-2]; prev2 = df_vwap.iloc[-3]
        vwap  = float(last["vwap"])
        c1 = float(last["close"]); c2 = float(prev["close"]); c3 = float(prev2["close"])
        v1 = float(last["volume"]); avg_vol = float(df_5["volume"].mean())
        vol_ok = v1 > avg_vol * VWAP_CROSS_VOL_MIN
        if c3 < vwap and c2 > vwap and c1 > vwap and vol_ok:
            return {"type": "bullish", "vwap": round(vwap, 1)}, \
                   f"VWAP cross bull 2c+vol at {vwap:.0f}"
        if c3 > vwap and c2 < vwap and c1 < vwap and vol_ok:
            return {"type": "bearish", "vwap": round(vwap, 1)}, \
                   f"VWAP cross bear 2c+vol at {vwap:.0f}"
        return None, f"No VWAP cross VWAP:{vwap:.0f}"
    except: return None, "VWAP cross error"

def detect_ema50_bounce(df_ema, ltp, t5, df_5):
    try:
        e50  = float(df_ema["ema50"].iloc[-1])
        dist = abs(ltp - e50)
        if dist > EMA50_TOLERANCE:
            return None, f"No EMA50 bounce dist:{dist:.0f}pts"
        last = df_5.iloc[-1]
        co = float(last["open"]); cc = float(last["close"])
        body = abs(cc - co)
        if t5 == "bullish" and ltp > e50 and cc > co and body > 5:
            return {"type": "bullish", "e50": round(e50, 1)}, \
                   f"EMA50 bounce bull at {e50:.0f} dist:{dist:.0f}pts"
        if t5 == "bearish" and ltp < e50 and cc < co and body > 5:
            return {"type": "bearish", "e50": round(e50, 1)}, \
                   f"EMA50 rejection bear at {e50:.0f} dist:{dist:.0f}pts"
        return None, f"EMA50 near {e50:.0f} no candle confirm"
    except: return None, "EMA50 error"

def detect_supertrend_signal(df, trend, ltp=None, atr=None):
    """
    [Strategy 8] Fresh SuperTrend flip aligned with multi-TF trend.

    [F28] CSV-DRIVEN PATCH: Distance check added.
    On 2026-05-08 ST fired with level 24224 while Nifty was at 24158
    (66pts gap) — ST was lagging the actual move. Result: instant SL.

    A valid SuperTrend flip should have price within ~2x ATR of the
    ST level. Otherwise the signal is stale/lagging and price is too
    far extended for the flip to be meaningful.
    """
    st_dir, st_level, is_fresh = calc_supertrend(df)
    if not is_fresh:
        return None, f"SuperTrend: no fresh flip (current:{st_dir})"
    if st_dir != trend:
        return None, f"SuperTrend {st_dir} conflicts trend {trend}"

    # [F28] Distance check — price must be near ST level
    if ltp is not None and atr is not None:
        distance = abs(ltp - st_level)
        max_distance = atr * 2.0   # 2x ATR
        if distance > max_distance:
            return None, (f"SuperTrend STALE — price {ltp:.0f} too far from "
                          f"ST level {st_level:.0f} (gap:{distance:.0f}pts > "
                          f"{max_distance:.0f}pts max)")

    return {"type": st_dir, "level": st_level, "fresh": True}, \
           f"SuperTrend flip to {st_dir} at {st_level:.0f}"

def detect_cpr_signal(ltp, pivot, bc, tc, trend, prev_ltp=None):
    """
    [Strategy 9] CPR trade logic:
    - Price closes above TC → bullish (CPR breakout)
    - Price closes below BC → bearish (CPR breakdown)
    - Narrow CPR (TC-BC < 15pts) = trending day = better for breakouts
    """
    if pivot is None: return None, "No CPR (no prev day data)"
    cpr_width = round(tc - bc, 1)
    if prev_ltp is None: return None, "Need prev LTP for CPR check"
    # Bullish breakout: prev below TC, now above TC
    if prev_ltp < tc and ltp > tc + CPR_BREAKOUT_BUFFER and trend == "bullish":
        return {"type": "bullish", "pivot": pivot, "tc": tc, "bc": bc, "width": cpr_width}, \
               f"CPR breakout bull above TC:{tc:.0f} width:{cpr_width:.0f}pts"
    # Bearish breakdown: prev above BC, now below BC
    if prev_ltp > bc and ltp < bc - CPR_BREAKOUT_BUFFER and trend == "bearish":
        return {"type": "bearish", "pivot": pivot, "tc": tc, "bc": bc, "width": cpr_width}, \
               f"CPR breakdown bear below BC:{bc:.0f} width:{cpr_width:.0f}pts"
    return None, f"CPR neutral | P:{pivot:.0f} TC:{tc:.0f} BC:{bc:.0f} L:{ltp:.0f}"


# ─────────────────────────────────────────────
#  [V6-F8] NEW STRATEGY 10: BOS (Break of Structure)
# ─────────────────────────────────────────────
def detect_bos(df, ltp):
    """
    Break of Structure: price breaks beyond the most recent swing
    high (bullish BOS) or swing low (bearish BOS) on 5m timeframe.
    Requires close confirmation — not just a wick.
    """
    try:
        if df is None or len(df) < BOS_SWING_LOOKBACK + 2:
            return None, "BOS: not enough candles"
        window  = df.tail(BOS_SWING_LOOKBACK + 2)
        highs   = window["high"].astype(float).values
        lows    = window["low"].astype(float).values
        closes  = window["close"].astype(float).values
        # Swing high = highest point in lookback (exclude last candle)
        swing_high = float(np.max(highs[:-2]))
        swing_low  = float(np.min(lows[:-2]))
        last_close = closes[-2]   # last fully closed candle
        # Bullish BOS: last closed candle closes above swing high
        if last_close > swing_high + BOS_MIN_MOVE and ltp > swing_high:
            return {"type": "bullish", "level": round(swing_high, 1),
                    "break_size": round(last_close - swing_high, 1)}, \
                   f"BOS bull — closed {last_close:.0f} above swing H:{swing_high:.0f}"
        # Bearish BOS: last closed candle closes below swing low
        if last_close < swing_low - BOS_MIN_MOVE and ltp < swing_low:
            return {"type": "bearish", "level": round(swing_low, 1),
                    "break_size": round(swing_low - last_close, 1)}, \
                   f"BOS bear — closed {last_close:.0f} below swing L:{swing_low:.0f}"
        return None, f"No BOS | SwH:{swing_high:.0f} SwL:{swing_low:.0f} LTP:{ltp:.0f}"
    except Exception as e:
        return None, f"BOS error: {e}"


# ─────────────────────────────────────────────
#  [V6-F9] NEW STRATEGY 11: ORPH / ORPL
#  (Opening Range Previous High/Low breakout)
# ─────────────────────────────────────────────
def detect_orph_orpl(ltp, prev_ohlc, trend, prev_ltp=None, gap_pct=0.0):
    """
    On gap days (gap > 0.3%), PDH/PDL become key levels.
    - Gap up + price holds above PDH → bullish momentum
    - Gap down + price breaks below PDL → bearish momentum
    Works as a standalone signal on trending gap days.
    """
    try:
        if prev_ohlc is None:
            return None, "ORPH_ORPL: no prev day data"
        if abs(gap_pct) < 0.3:
            return None, f"ORPH_ORPL: gap {gap_pct:.2f}% too small (need >0.3%)"
        pdh = prev_ohlc["high"]
        pdl = prev_ohlc["low"]
        if prev_ltp is None:
            return None, "ORPH_ORPL: need prev LTP"
        # Gap up day — bullish above PDH
        if gap_pct > 0.3 and ltp > pdh and prev_ltp < pdh and trend == "bullish":
            return {"type": "bullish", "level": round(pdh, 1), "gap_pct": gap_pct}, \
                   f"ORPH bull — above PDH:{pdh:.0f} gap:{gap_pct:+.2f}%"
        # Gap down day — bearish below PDL
        if gap_pct < -0.3 and ltp < pdl and prev_ltp > pdl and trend == "bearish":
            return {"type": "bearish", "level": round(pdl, 1), "gap_pct": gap_pct}, \
                   f"ORPL bear — below PDL:{pdl:.0f} gap:{gap_pct:+.2f}%"
        return None, f"ORPH_ORPL neutral | PDH:{pdh:.0f} PDL:{pdl:.0f} gap:{gap_pct:+.2f}%"
    except Exception as e:
        return None, f"ORPH_ORPL error: {e}"


# ─────────────────────────────────────────────
#  [V6-F10] NEW STRATEGY 12: RSI Divergence
# ─────────────────────────────────────────────
def detect_rsi_divergence(df, rsi_series):
    """
    Detects price/RSI divergence — early reversal signal.
    - Bullish divergence: price makes lower low, RSI makes higher low
    - Bearish divergence: price makes higher high, RSI makes lower high
    Uses last RSI_DIV_LOOKBACK candles.
    """
    try:
        if df is None or len(df) < RSI_DIV_LOOKBACK + 2:
            return None, "RSI divergence: not enough candles"
        if rsi_series is None or len(rsi_series) < RSI_DIV_LOOKBACK:
            return None, "RSI divergence: no RSI series"
        prices = df["close"].astype(float).values[-RSI_DIV_LOOKBACK:]
        rsivs  = rsi_series[-RSI_DIV_LOOKBACK:]
        # Bearish divergence: price higher high, RSI lower high
        if prices[-1] > prices[0] and rsivs[-1] < rsivs[0]:
            mag = round(prices[-1] - prices[0], 1)
            rsi_drop = round(rsivs[0] - rsivs[-1], 1)
            if mag > 15 and rsi_drop > 5:   # meaningful divergence only
                return {"type": "bearish", "price_move": mag, "rsi_drop": rsi_drop}, \
                       f"Bear divergence: price +{mag}pts RSI -{rsi_drop:.0f}"
        # Bullish divergence: price lower low, RSI higher low
        if prices[-1] < prices[0] and rsivs[-1] > rsivs[0]:
            mag = round(prices[0] - prices[-1], 1)
            rsi_rise = round(rsivs[-1] - rsivs[0], 1)
            if mag > 15 and rsi_rise > 5:
                return {"type": "bullish", "price_move": mag, "rsi_rise": rsi_rise}, \
                       f"Bull divergence: price -{mag}pts RSI +{rsi_rise:.0f}"
        return None, "No RSI divergence"
    except Exception as e:
        return None, f"RSI div error: {e}"

# ─────────────────────────────────────────────
#  [V9-F2] REGIME CLASSIFIER
#  Classifies market regime from 12-candle price
#  structure — NOT from RSI (RSI is confirmation only).
#
#  Proved by April 2026 backtest (20 days):
#    TRENDING_BEAR: WR 67%, WEAK_BEAR: WR 75%
#    WEAK_BULL: WR 27% → block trend strategies
# ─────────────────────────────────────────────
REGIME_TRENDING  = {"TRENDING_BULL", "TRENDING_BEAR"}
REGIME_BLOCK_TREND = {"WEAK_BULL", "WEAK_BEAR", "CHOPPY", "RANGING", "UNKNOWN"}
# [V9] Trend strategies blocked in these regimes:
TREND_STRATEGIES = {"EMAStack", "EMACross", "SuperTrend", "BOS"}

def classify_intraday_regime(df_5, e9, e21):
    """
    [V9-F2] Classify current intraday regime from price structure.
    Uses last 12 candles (60 min) of 5m data.
    Returns: TRENDING_BULL | TRENDING_BEAR | WEAK_BULL | WEAK_BEAR
             | RANGING | CHOPPY | UNKNOWN

    [V9-FIX1] Dynamic thresholds: rng > 1.8*ATR instead of fixed >100
    Proved wrong on 2/4 real trading days with fixed threshold:
      May11: range=137 but ATR=28 → 1.8*28=50 → NOT trending (correct)
      May15: range=129 but ATR=25 → 1.8*25=45 → NOT trending (correct)
    Fixed >100 misclassified both as TRENDING → wrong regime gate.
    Dynamic threshold adapts to VIX changes, event days, market conditions.
    """
    try:
        if df_5 is None or len(df_5) < 12:
            return "UNKNOWN"
        closes = df_5["close"].astype(float).values[-12:]
        atr_vals = df_5["atr"].astype(float).values[-12:] if "atr" in df_5.columns else None
        rng      = closes.max() - closes.min()
        half1    = closes[:6].mean()
        half2    = closes[6:].mean()
        direction = "up" if half2 > half1 else "down"
        ema_bull  = float(e9) > float(e21)

        # [V9-FIX1] Dynamic ATR-relative thresholds
        # If ATR available use 1.8*ATR, else fall back to sensible fixed values
        if atr_vals is not None and len(atr_vals) > 0:
            avg_atr = float(np.mean(atr_vals[atr_vals > 0])) if any(atr_vals > 0) else 30.0
            trend_threshold = avg_atr * 1.8   # was fixed 100
            weak_threshold  = avg_atr * 1.2   # was fixed 60
        else:
            trend_threshold = 100   # fallback to original if no ATR
            weak_threshold  = 60

        if rng > trend_threshold:
            if direction == "up"   and ema_bull:      return "TRENDING_BULL"
            if direction == "down" and not ema_bull:   return "TRENDING_BEAR"
            return "CHOPPY"
        elif rng > weak_threshold:
            if direction == "up":   return "WEAK_BULL"
            else:                   return "WEAK_BEAR"
        else:
            return "RANGING"
    except Exception as e:
        log.debug(f"Regime classify error: {e}")
        return "UNKNOWN"



# ─────────────────────────────────────────────
#  [V6-F13] DAY TYPE CLASSIFIER
# ─────────────────────────────────────────────
def classify_day_type(open_price, prev_ohlc, atr, df_5=None):
    """
    Classify the current day type to enable/disable strategies.
    Returns: 'TRENDING' | 'RANGEBOUND' | 'VOLATILE' | 'GAP_UP' | 'GAP_DOWN'
    """
    try:
        gap_pct = 0.0
        if prev_ohlc:
            gap_pct = (open_price - prev_ohlc["close"]) / prev_ohlc["close"] * 100
        # Gap days
        if gap_pct > 0.5:  return "GAP_UP",    gap_pct
        if gap_pct < -0.5: return "GAP_DOWN",  gap_pct
        # High ATR = volatile
        if atr > 40:       return "VOLATILE",  gap_pct
        # Check if price is trending (using 5m candle HH/HL or LL/LH pattern)
        if df_5 is not None and len(df_5) >= 6:
            closes = df_5["close"].astype(float).values[-6:]
            up_moves   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
            down_moves = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
            if up_moves >= 4:   return "TRENDING",    gap_pct
            if down_moves >= 4: return "TRENDING",    gap_pct
        return "RANGEBOUND", gap_pct
    except:
        return "UNKNOWN", 0.0


# ─────────────────────────────────────────────
#  CONFIDENCE SCORER (0–10, was 0–9)
# ─────────────────────────────────────────────
def calc_confidence(direction, trend, e9, e21, e50, ltp,
                    vwap, pcr_bias, pcr_weight, rvol, pre_bias,
                    t5, t15, t30, rsi=50, strategy_name=""):
    """
    [V8-F3a] RSI confidence penalty now ONLY applies to mean-reversion
    strategies (EMA50Bounce, CPR, RSIDivergence, VWAPCross).
    Momentum/trend strategies (BOS, EMAStack, StrongFVG, VWAPBand)
    are NOT penalised for high RSI — it confirms their signal strength.

    v7 bug: BOS at RSI=78 scored 0/10 because:
      trend(+2) EMA(+2) - RSI_penalty(-1) - PCR(-1) - RVOL(-1) = 1
    Now: BOS at RSI=78 scores trend(+2) EMA(+2) + RSI_bonus(+1) = 5+
    """
    MEAN_REV_STRATEGIES = {"EMA50Bounce","CPR","RSIDivergence","VWAPCross"}
    apply_rsi_penalty   = strategy_name in MEAN_REV_STRATEGIES

    score = 0; reasons = []

    # 1. Multi-TF trend alignment (+2)
    if trend == direction:
        score += 2; reasons.append(f"Trend {trend} aligned +2")
    else:
        reasons.append(f"Trend {trend} mismatch +0")

    # 2. EMA 9/21 alignment (+2)
    if direction == "bullish" and e9 > e21:
        score += 2; reasons.append("EMA9>EMA21 bull +2")
    elif direction == "bearish" and e9 < e21:
        score += 2; reasons.append("EMA9<EMA21 bear +2")
    else:
        reasons.append("EMA mismatch +0")

    # 3. VWAP side (+1)
    if direction == "bullish" and ltp > vwap:
        score += 1; reasons.append("Above VWAP +1")
    elif direction == "bearish" and ltp < vwap:
        score += 1; reasons.append("Below VWAP +1")
    else:
        reasons.append("Wrong VWAP side +0")

    # 4. PCR — weight reduced to 0 (intraday noise, wrong 3/4 live days)
    # PCR kept in logs for analysis but no longer affects confidence score.
    # Will be re-evaluated after 10 live sessions.
    if pcr_weight >= 1.0:
        reasons.append(f"PCR {pcr_bias} (logged, weight=0)")
    else:
        reasons.append(f"PCR stale (logged, weight=0)")

    # 5. RVOL (+1)
    if rvol >= 1.5:
        score += 1; reasons.append(f"RVOL {rvol}x +1")
    else:
        reasons.append(f"RVOL {rvol}x weak +0")

    # 6. Pre-bias (+1)
    if pre_bias == direction or pre_bias == "neutral":
        score += 1; reasons.append(f"Pre-bias {pre_bias} ok +1")
    else:
        reasons.append(f"Pre-bias {pre_bias} conflicts +0")

    # 7. EMA50 side (+1)
    if direction == "bullish" and ltp > e50:
        score += 1; reasons.append("Above EMA50 +1")
    elif direction == "bearish" and ltp < e50:
        score += 1; reasons.append("Below EMA50 +1")
    else:
        reasons.append("EMA50 wrong side +0")

    # 8. MTF 3/3 agreement (+1)
    all_agree = (t5 == direction and t15 == direction and t30 == direction)
    if all_agree:
        score += 1; reasons.append("MTF 3/3 agree +1")
    else:
        reasons.append(f"MTF partial {t5}/{t15}/{t30} +0")

    # 9. [V8-F3a] RSI factor — penalty only for mean-reversion strategies
    if direction == "bearish":
        if apply_rsi_penalty and rsi < 40:
            score -= 1; reasons.append(f"RSI {rsi:.0f} oversold -1 (mean-rev: bounce risk)")
        elif rsi > 60:
            score += 1; reasons.append(f"RSI {rsi:.0f} room to fall +1")
        else:
            reasons.append(f"RSI {rsi:.0f} mid-range +0")
    elif direction == "bullish":
        if apply_rsi_penalty and rsi > 60:
            score -= 1; reasons.append(f"RSI {rsi:.0f} overbought -1 (mean-rev: pullback risk)")
        elif rsi < 40:
            score += 1; reasons.append(f"RSI {rsi:.0f} room to rise +1")
        else:
            reasons.append(f"RSI {rsi:.0f} mid-range +0")

    score = max(0, min(10, score))
    label = "HIGH" if score >= HIGH_CONF else "MEDIUM" if score >= MEDIUM_CONF else "LOW"
    return score, label, reasons


# ─────────────────────────────────────────────
#  [V8-F1][V8-F4] GROUP CAPITAL MANAGER
# ─────────────────────────────────────────────
class GroupCapitalManager:
    """One capital manager per strategy group."""
    def __init__(self):
        self._state = {gid: {"consec_loss":0,"consec_win":0,"reduced":False}
                       for gid in STRATEGY_GROUPS}

    def on_result(self, group_id, result):
        s = self._state[group_id]
        if result == "target":
            s["consec_loss"] = 0; s["consec_win"] += 1
            if s["reduced"] and s["consec_win"] >= 2:
                s["reduced"] = False
        elif result in ["sl","timeout","timeout_theta","timeout_duration"]:
            s["consec_loss"] += 1; s["consec_win"] = 0
            if s["consec_loss"] >= 2:
                s["reduced"] = True

    def get_capital(self, group_id):
        base = STRATEGY_GROUPS[group_id]["capital"]
        return base // 2 if self._state[group_id]["reduced"] else base

    def info(self, group_id):
        s = self._state[group_id]
        cap = self.get_capital(group_id)
        return f"Grp{group_id} Rs.{cap} CL:{s['consec_loss']} CW:{s['consec_win']}"

    def get_info(self):
        """Summary of all groups."""
        parts = []
        for gid in STRATEGY_GROUPS:
            s   = self._state[gid]
            cap = self.get_capital(gid)
            tag = "RED" if s["reduced"] else "OK"
            parts.append(f"Grp{gid}:Rs.{cap}({tag})")
        return " | ".join(parts)


# ─────────────────────────────────────────────
#  [V8-F4] PAPER TRADE ENGINE — group-aware
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium, signal,
                 pcr, fii_bias, pre_bias, rvol, trend_strength,
                 conf_score, conf_label, session_bias_str, zscore,
                 rsi, atr, is_strong=False):
        self.trade_no       = trade_no
        self.strategy       = strategy
        self.direction      = direction
        self.group_id, self.group_cfg = _get_group(strategy)
        # [F13] Apply slippage on entry
        self.entry_price    = (entry_price + SLIPPAGE_PTS if direction == "bullish"
                               else entry_price - SLIPPAGE_PTS)
        self.raw_entry      = entry_price
        self.option_type    = option_type
        self.strike         = strike
        self.expiry         = expiry
        self.premium        = premium
        self.signal         = signal
        self.pcr            = pcr
        self.fii_bias       = fii_bias
        self.pre_bias       = pre_bias
        self.rvol           = rvol
        self.trend_strength = trend_strength
        self.conf_score     = conf_score
        self.conf_label     = conf_label
        self.session_bias   = session_bias_str
        self.zscore         = zscore
        self.rsi            = rsi
        self.atr            = atr
        self.is_strong      = is_strong
        self.entry_time     = now_ist().strftime("%H:%M:%S IST")
        self.start_time     = time.time()
        self.be_moved       = False
        self.trailing       = is_strong
        self.best_price     = self.entry_price
        ep = self.entry_price

        # [V8-F4] Dynamic SL/Target from group config
        sl_pts  = self.group_cfg["sl"]
        tgt_pts = self.group_cfg["target"]
        self.sl_pts         = sl_pts
        self.tgt_pts        = tgt_pts
        self.trail_start    = self.group_cfg["trail_start"]
        self.trail_dist     = self.group_cfg["trail_dist"]
        self.sl_price       = ep - sl_pts  if direction == "bullish" else ep + sl_pts
        self.tgt_price      = ep + tgt_pts if direction == "bullish" else ep - tgt_pts

    def check(self, ltp, t):
        dur = self.duration()

        # [V8-F4] Hard timeout 25 min (was 30)
        if dur >= 25:
            return "timeout_duration"

        if t >= TIME_EXIT_AFTER and dur > 15:
            return "timeout_theta"

        if self.trailing:
            if self.direction == "bullish" and ltp > self.best_price:
                self.best_price = ltp
                if ltp - self.entry_price >= self.trail_start:
                    new_sl = round(ltp - self.trail_dist, 1)
                    if new_sl > self.sl_price:
                        self.sl_price = new_sl
                        tg("🔄", f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.0f} Profit:+{ltp-self.entry_price:.0f}pts SL→{new_sl:.0f}"])
            elif self.direction == "bearish" and ltp < self.best_price:
                self.best_price = ltp
                if self.entry_price - ltp >= self.trail_start:
                    new_sl = round(ltp + self.trail_dist, 1)
                    if new_sl < self.sl_price:
                        self.sl_price = new_sl
                        tg("🔄", f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.0f} Profit:+{self.entry_price-ltp:.0f}pts SL→{new_sl:.0f}"])
            if self.direction == "bullish" and ltp <= self.sl_price: return "sl"
            if self.direction == "bearish" and ltp >= self.sl_price: return "sl"
        else:
            if not self.be_moved:
                # [V8-F4] Breakeven at 50% of target (was fixed midpoint)
                be_trigger = self.entry_price + (self.tgt_pts * 0.5) if self.direction == "bullish" \
                             else self.entry_price - (self.tgt_pts * 0.5)
                if (self.direction == "bullish" and ltp >= be_trigger) or \
                   (self.direction == "bearish" and ltp <= be_trigger):
                    self.be_moved = True; self.sl_price = self.entry_price
                    tg("🔒", f"Trade #{self.trade_no} Breakeven locked",
                       [f"Nifty:{ltp:.0f} SL→{self.entry_price:.0f}"])
            if self.direction == "bullish":
                if ltp >= self.tgt_price: return "target"
                if ltp <= self.sl_price:  return "sl"
            else:
                if ltp <= self.tgt_price: return "target"
                if ltp >= self.sl_price:  return "sl"
        return None

    def duration(self): return round((time.time() - self.start_time) / 60, 1)

    def calc_pnl(self, exit_price):
        exit_price = (exit_price - SLIPPAGE_PTS if self.direction == "bullish"
                      else exit_price + SLIPPAGE_PTS)
        pts = (exit_price - self.entry_price if self.direction == "bullish"
               else self.entry_price - exit_price)
        return round(pts * LOT_SIZE, 0)

    def calc_pts_moved(self, exit_price):
        return round(exit_price - self.raw_entry if self.direction == "bullish"
                     else self.raw_entry - exit_price, 1)


# ─────────────────────────────────────────────
#  STRATEGY TRACKER (unified — replaces split used_signals + can_trade)
# ─────────────────────────────────────────────
class StrategyTracker:
    """
    [F11] Single source of truth for per-strategy state.
    [V6-F3] PERFECT_CONF (10/10) always allowed regardless of prior losses.
             HIGH_CONF_REENTRY raised to 9 (was 8) for stricter post-loss gate.
    """
    def __init__(self):
        self._results = {}   # strategy -> list of results

    def record(self, strategy, result):
        if strategy not in self._results:
            self._results[strategy] = []
        self._results[strategy].append(result)

    def can_trade(self, strategy, conf_score):
        """
        Allow trade if:
        - Never traded today
        - Last result was a win
        - conf_score == PERFECT_CONF (10/10) — always allowed [V6-F3]
        - HIGH_CONF_REENTRY (9+) after a single loss [V6-F3]
        """
        results = self._results.get(strategy, [])
        if not results:
            return True, "Never traded today"
        last = results[-1]
        if last in ["sl", "timeout", "timeout_theta", "timeout_duration"]:
            # [V6-F3] Perfect 10/10 always gets through
            if conf_score >= PERFECT_CONF:
                return True, f"🔥 PERFECT conf {conf_score}/10 — StratTracker bypassed"
            if conf_score >= HIGH_CONF_REENTRY:
                return True, f"Re-entry allowed — HIGH conf {conf_score}/10 ≥ {HIGH_CONF_REENTRY}"
            return False, f"Blocked — prev loss, conf {conf_score}<{HIGH_CONF_REENTRY}"
        return True, "Last trade was a win"

    def get_results(self, strategy):
        return self._results.get(strategy, [])

    def summary(self):
        out = {}
        for strat, results in self._results.items():
            wins   = results.count("target")
            losses = len(results) - wins
            out[strat] = {"trades": len(results), "wins": wins, "losses": losses}
        return out


# ─────────────────────────────────────────────
#  OPTION UTILS
# ─────────────────────────────────────────────
def get_option_details(nifty_price, option_type):
    atm    = round(nifty_price / 50) * 50
    strike = atm + OTM_OFFSET if option_type == "CE" else atm - OTM_OFFSET
    # Use live expiry — nearest available from the market
    expiries = get_live_expiries()
    if expiries:
        expiry = expiries[0]   # nearest expiry
    else:
        # Fallback: next Monday
        today  = datetime.date.today()
        days   = (0 - today.weekday()) % 7
        if days == 0: days = 7
        expiry = (today + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    return strike, expiry

def is_expiry_day(): return datetime.date.today().weekday() == 3

def calc_prev_vwap(prev_ohlc, open_price):
    if prev_ohlc is None: return None, False
    prev_close = prev_ohlc["close"]
    if open_price > 0:
        gap_pct = abs(open_price - prev_close) / prev_close * 100
        if gap_pct > GAP_FILTER_PCT: return None, False
    prev_vwap = round((prev_ohlc["high"] + prev_ohlc["low"] + prev_ohlc["close"]) / 3, 1)
    return prev_vwap, True

def is_retesting(price, bottom, top): return bottom <= price <= top


# ─────────────────────────────────────────────
#  [F2] ASYNC RETEST — background thread, non-blocking
# ─────────────────────────────────────────────
class RetestWaiter:
    """
    [F2] Runs retest check in a daemon thread.
    Main loop continues scanning during wait.
    """
    def __init__(self):
        self._result  = None    # True/False/None
        self._ltp     = None
        self._running = False
        self._thread  = None

    def start(self, bottom, top, timeout_min=10):
        self._result  = None
        self._ltp     = None
        self._running = True
        self._thread  = threading.Thread(
            target=self._wait,
            args=(bottom, top, timeout_min),
            daemon=True
        )
        self._thread.start()
        tg("⏳", f"Async Retest Wait {bottom:.0f}→{top:.0f}",
           [f"Timeout: {timeout_min}min", "Main loop continues scanning"])

    def _wait(self, bottom, top, timeout_min):
        start = time.time()
        while time.time() - start < timeout_min * 60:
            if not self._running: return
            ltp = get_nifty_ltp()
            if ltp and is_retesting(ltp, bottom, top):
                self._ltp    = ltp
                self._result = True
                return
            time.sleep(15)
        self._result = False

    def is_done(self): return self._result is not None
    def succeeded(self): return self._result is True
    def get_ltp(self): return self._ltp
    def cancel(self): self._running = False


# ─────────────────────────────────────────────
#  CSV LOGS
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime", "nifty_ltp", "chg_from_open", "chg_pct",
    "session_bias", "zscore", "rsi", "atr", "india_vix",
    "vix_spike_guard",
    "trend_5m", "trend_15m", "trend_30m", "trend_combined", "trend_strength",
    "rvol", "vwap", "vwap_u1", "vwap_l1", "band_width", "price_vs_vwap",
    "prev_vwap", "prev_vwap_valid",
    "ema9", "ema21", "ema50", "ema9_vs_ema21", "price_vs_ema9", "price_vs_ema50",
    "fvg_found", "fvg_type", "fvg_strong", "fvg_size", "fvg_age",
    "orb_high", "orb_low", "orb_signal", "orb_size",
    "ema_stack", "ema_cross", "vwap_band", "vwap_cross", "ema50_bounce",
    "supertrend_dir", "supertrend_fresh", "supertrend_level",
    "cpr_pivot", "cpr_tc", "cpr_bc", "cpr_signal",
    "pcr", "pcr_bias", "pcr_status",
    "manual_bias", "auto_bias", "final_bias",
    "auto_bias_score", "auto_bias_conf",
    "pcr_source_bias", "vix_bias", "gift_nifty_bias", "news_bias",
    "pdh", "pdl", "pdc",
    "capital_mode", "consec_losses",
    "entry_condition_met", "strategy_triggered",
    "conf_score_preview",
    "trades_today", "daily_pnl", "reason"
]

TRADE_COLS = [
    # Identity
    "date", "trade_no", "strategy",
    "entry_time", "exit_time", "duration_min",
    # Confidence
    "conf_score", "conf_label", "conf_reasons",
    # Market context at entry
    "session_bias", "zscore_at_entry", "rsi_at_entry", "atr_at_entry",
    "india_vix_at_entry",
    "trend_combined", "trend_strength",
    "trend_5m_at_entry", "trend_15m_at_entry", "trend_30m_at_entry",
    "rvol_at_entry",
    "vwap_at_entry", "prev_vwap_at_entry",
    "ema9_at_entry", "ema21_at_entry", "ema50_at_entry",
    "price_vs_vwap_at_entry", "price_vs_ema50_at_entry",
    # Bias (full breakdown — key for analysis)
    "manual_bias",       # what you sent via /bias
    "auto_bias",         # computed by nifty_auto_bias
    "final_bias",        # what the bot actually used (pre_bias)
    "auto_bias_score",   # weighted score from get_combined_bias_nifty
    "auto_bias_conf",    # HIGH/MEDIUM/LOW confidence of auto-bias
    "pcr_bias_at_entry", # PCR direction at trade time
    "pcr_val_at_entry",  # PCR value (e.g. 1.25)
    "pcr_status_at_entry",
    "vix_bias_at_entry",
    "gift_nifty_bias_at_entry",
    "news_bias_at_entry",
    # Trade details
    "direction", "is_strong", "exit_mode",
    "raw_entry_nifty", "entry_nifty_with_slip", "exit_nifty",
    "points_moved", "slippage_pts",
    "option_type", "strike", "expiry",
    "premium", "lots", "capital_used",
    "sl_points", "target_points", "rr_ratio",
    "sl_price", "target_price",
    # Result
    "pnl_est", "result",
    "be_triggered", "trail_triggered",
    "consec_losses_after", "daily_pnl_after",
    "strategy_wr_today",
    # Skip reason (empty if trade was taken)
    "skip_reason",
    "notes"
]

# Skipped trade log — every time try_trade returns False, record why
SKIP_COLS = [
    "datetime", "strategy", "direction", "skip_reason",
    "conf_score", "conf_label",
    "session_bias", "zscore", "rsi",
    "manual_bias", "auto_bias", "final_bias",
    "trend_combined", "rvol",
    "pcr", "pcr_bias",
    "nifty_ltp", "trades_today", "daily_pnl"
]

def get_log_filenames():
    """
    Date-stamped CSV filenames so each trading day gets its own file.
    Restart-safe: same day restarts append to the same file.
    e.g. scan_log_v8_2026-05-14.csv
    """
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    return {
        "scan"  : f"scan_log_v9_{date_str}.csv",
        "trade" : f"trade_log_v9_{date_str}.csv",
        "skip"  : f"skip_log_v9_{date_str}.csv",
    }

# Module-level log file paths — set once at startup
_LOG_FILES = {}

def init_logs():
    """
    Initialise CSV log files for today.
    - New day → new files with fresh headers
    - Same day restart → append to existing files, write restart marker
    """
    global _LOG_FILES
    _LOG_FILES = get_log_filenames()

    for key, fname, cols in [
        ("scan",  _LOG_FILES["scan"],  SCAN_COLS),
        ("trade", _LOG_FILES["trade"], TRADE_COLS),
        ("skip",  _LOG_FILES["skip"],  SKIP_COLS),
    ]:
        if not os.path.exists(fname):
            # New file — write header
            with open(fname, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=cols).writeheader()
            log.info(f"Created {fname}")
        else:
            # File exists (same-day restart) — append a restart marker row
            # so analysts can see exactly where the restart happened
            marker = {c: "" for c in cols}
            marker[cols[0]] = f"=== BOT RESTART {datetime.datetime.now().strftime('%H:%M:%S')} ==="
            with open(fname, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=cols).writerow(marker)
            log.info(f"Appending to existing {fname} (same-day restart)")

    log.info(f"Logs: {_LOG_FILES['trade']}")

def write_scan(rec):
    with open(_LOG_FILES["scan"], "a", newline="") as f:
        row = {c: rec.get(c, "") for c in SCAN_COLS}
        csv.DictWriter(f, fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open(_LOG_FILES["trade"], "a", newline="") as f:
        row = {c: rec.get(c, "") for c in TRADE_COLS}
        csv.DictWriter(f, fieldnames=TRADE_COLS).writerow(row)

def write_skip(rec):
    with open(_LOG_FILES["skip"], "a", newline="") as f:
        row = {c: rec.get(c, "") for c in SKIP_COLS}
        csv.DictWriter(f, fieldnames=SKIP_COLS).writerow(row)

def send_summary(stats, pre_bias, pcr_cache, session_bias, cap_mgr, strat_tracker):
    wr = (stats["wins"] / stats["trades"] * 100) if stats["trades"] > 0 else 0
    pcr_v, pcr_b, _, pcr_st = pcr_cache.get()
    strat_sum = strat_tracker.summary()
    strat_lines = []
    for s, d in strat_sum.items():
        wr_s = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        strat_lines.append(f"  {s}: {d['wins']}W/{d['losses']}L ({wr_s:.0f}%)")
    tg("📊", "Nifty v7 DAILY SUMMARY", [
        f"Session bias : {session_bias.bias.upper()}",
        f"Pre-bias     : {pre_bias.upper()}",
        f"PCR          : {pcr_v or 'N/A'} ({pcr_b}) [{pcr_st}]",
        f"Trades       : {stats['trades']}",
        f"Wins         : {stats['wins']} | Losses: {stats['losses']}",
        f"Win rate     : {wr:.1f}%",
        f"P&L          : Rs.{stats['pnl']:+.0f}",
        f"HIGH conf WR : {stats['high_w']}/{stats['high_t']} trades",
        f"MED conf WR  : {stats['med_w']}/{stats['med_t']} trades",
        f"LOW conf WR  : {stats['low_w']}/{stats['low_t']} trades",
        f"Capital mode : {cap_mgr.get_info()}",
        f"",
        f"By Strategy:",
        *strat_lines
    ])
    send_csv_files()


# ─────────────────────────────────────────────
#  OPEN TRADE HELPER
# ─────────────────────────────────────────────
def open_trade(trade_no, strategy, direction, entry_price,
               pcr_cache, tg_listener, pre_bias, is_strong,
               signal, rvol, trend_strength, risk_level,
               conf_score, conf_label, conf_reasons,
               session_bias_obj, zscore, rsi, atr,
               vwap, prev_vwap, e9, e21, e50, capital,
               auto_bias_report=None):
    if auto_bias_report is None:
        auto_bias_report = {}
    opt = "CE" if direction == "bullish" else "PE"
    strike, expiry = get_option_details(entry_price, opt)
    premium = round(capital / LOT_SIZE, 1)
    pcr_v, pcr_b, _, pcr_st = pcr_cache.get()
    trade = PaperTrade(
        trade_no=trade_no, strategy=strategy,
        direction=direction, entry_price=entry_price,
        option_type=opt, strike=strike, expiry=expiry,
        premium=premium, signal=signal, pcr=pcr_v,
        fii_bias=tg_listener.bias, pre_bias=pre_bias,
        rvol=rvol, trend_strength=trend_strength,
        conf_score=conf_score, conf_label=conf_label,
        session_bias_str=session_bias_obj.bias,
        zscore=zscore, rsi=rsi, atr=atr, is_strong=is_strong
    )
    # Store full bias snapshot on the trade for CSV write at exit
    trade.auto_bias_report  = auto_bias_report
    trade.manual_bias       = tg_listener.bias
    trade.auto_bias         = auto_bias_report.get("final_bias", "")
    trade.final_bias        = pre_bias
    trade.auto_bias_score   = auto_bias_report.get("score", "")
    trade.auto_bias_conf    = auto_bias_report.get("confidence", "")
    trade.pcr_val_at_entry  = pcr_v
    trade.pcr_bias_at_entry = pcr_b
    trade.pcr_status_at_entry = pcr_st
    trade.vix_bias_at_entry      = auto_bias_report.get("vix_bias", "")
    trade.gift_nifty_bias_at_entry = auto_bias_report.get("gift_bias", "")
    trade.news_bias_at_entry     = auto_bias_report.get("news_bias", "")
    trade.conf_reasons_str  = " | ".join(conf_reasons[:5])
    trade.vwap_at_entry     = vwap
    trade.prev_vwap_at_entry = prev_vwap or ""
    trade.e9_at_entry       = e9
    trade.e21_at_entry      = e21
    trade.e50_at_entry      = e50

    mode = "Trailing SL" if is_strong else f"Fixed {TARGET_POINTS}pts (1:{TARGET_POINTS/SL_POINTS:.1f})"
    tg("🚀", f"PAPER TRADE #{trade_no} — {strategy}", [
        f"Direction    : {direction.upper()}",
        f"Confidence   : {conf_label} ({conf_score}/10)",
        f"Session bias : {session_bias_obj.bias.upper()} Z:{zscore:+.2f}",
        f"RSI          : {rsi:.0f} | ATR:{atr:.0f}pts",
        f"Trend        : {trend_strength}",
        f"Option       : {opt} {strike} | {expiry}",
        f"Raw entry    : {entry_price:.0f}",
        f"Entry+slip   : {trade.entry_price:.0f} (+{SLIPPAGE_PTS}pts slippage)",
        f"SL           : {trade.sl_price:.0f} (-{SL_POINTS}pts)",
        f"Target       : {trade.tgt_price:.0f} (+{TARGET_POINTS}pts)",
        f"RR Ratio     : 1:{TARGET_POINTS/SL_POINTS:.1f}",
        f"Exit mode    : {mode}",
        f"Max duration : {TRADE_MAX_DURATION}min",
        f"RVOL         : {rvol}x",
        f"VWAP         : {vwap:.0f} ({entry_price-vwap:+.0f}pts)",
        f"Prev VWAP    : {prev_vwap:.0f}" if prev_vwap else "Prev VWAP: N/A (gap day)",
        f"EMA9/21/50   : {e9:.0f}/{e21:.0f}/{e50:.0f}",
        f"Capital      : Rs.{premium*LOT_SIZE:.0f} | Lots:1",
        f"Auto bias    : {tg_listener._auto_bias.upper()}  [auto-only]",
        f"Auto bias    : {trade.auto_bias.upper() or 'N/A'} ({trade.auto_bias_conf})",
        f"Final bias   : {pre_bias.upper()}",
        f"Rev risk     : {risk_level}",
        f"Top reasons  : {' | '.join(conf_reasons[:4])}",
        f"NOTE         : ⚠️ PAPER TRADE — NO REAL ORDER"
    ])
    return trade


# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    # All variables that are assigned inside the while loop MUST be initialised
    # here at function start — Python treats them as locals to the entire function.
    # Without this, the except block crashes with UnboundLocalError.
    ltp = None; atr = 30.0; rsi = 50.0; e9 = e21 = e50 = 0.0
    vwap = 0.0; trend = "neutral"; rvol = 1.0; candles_seen = 0
    prev_ltp = None; chg_pct = 0.0; zscore = 0.0
    init_logs()

    # [F15] Token health check before anything else
    healthy, startup_ltp = check_token_health()
    if not healthy:
        send_telegram(
            "🚨 <b>Nifty Bot v7 — TOKEN ERROR</b>\n"
            "Upstox token invalid or expired!\n"
            "Bot will NOT start. Please refresh token."
        )
        log.error("Token health check failed. Exiting.")
        return

    # [F3] Load persisted state (crash recovery)
    stats = load_state()

    # [V9-FIX2] Option chain — init once, refresh every 15 min
    option_chain = OptionChain(config.LIVE_TOKEN) if _OPTION_CHAIN_AVAILABLE else None

    cap_mgr      = GroupCapitalManager()   # [V8-F1] per-group capital
    session_bias = SessionBias()
    pcr_cache    = PCRCache()
    strat_tracker = StrategyTracker()

    # [V7-F16] Signal dedup cooldown tracker
    signal_cooldown = {}

    tg_listener = TelegramListener(stats, None, session_bias)
    tg_listener.restore_bias(stats.get("manual_bias", "neutral"))
    tg_listener.start()

    trade_no           = stats["trades"]
    # [V8-F1] Multi-trade: one slot per group
    active_trades      = {}   # group_id → PaperTrade | None
    retest_waiter      = RetestWaiter()
    pending_trade_args = None
    last_scan          = None
    pre_bias           = "neutral"
    auto_bias_report   = {}
    premarket_done     = False
    reminder_sent      = False
    orb_high = orb_low = None
    orb_formed         = False
    prev_ohlc          = None
    open_price         = None
    closed_summary_sent = False
    prev_df5_ema       = None
    prev_ltp           = None
    india_vix          = None
    vix_alert_sent     = False
    vix_alert_at       = None
    candles_seen       = 0    # [P5] 5m candles since open
    ltp                = None  # safe default — assigned each scan
    _bias_refresh_ts   = 0.0   # timestamp of last bias refresh
    # Safe defaults so try_trade closure always has these in scope
    pcr_v = None; pcr_b = "neutral"; pcr_weight = 0.0; pcr_status = "excluded"
    zscore = 0.0; trend = "neutral"; trend_strength = "none"
    e9 = e21 = e50 = 0.0; vwap = 0.0; prev_vwap = 0.0
    rsi = 50.0; atr = 30.0; rvol = 1.0
    t5 = t15 = t30 = "neutral"; pre_bias = "neutral"
    chg_pct = 0.0; bw = 0.0; vu1 = vl1 = 0.0
    orb_s = orb_r = None; fvg = None; fvg_r = ""; ema_stk = None
    vwap_bb = vwap_cx = ema50_b = ema_cx = st_sig = cpr_sig = None

    send_telegram(
        f"🤖 <b>Nifty Bot v9 — Multi-Trade Paper Build</b>\n\n"
        f"  Token check   : ✅ Live (LTP:{startup_ltp:.0f})\n"
        f"  Paper trading : ✅ No real orders\n"
        f"  Multi-trade   : ✅ Up to {MAX_CONCURRENT_GROUPS} groups concurrent\n"
        f"  Groups        : A(Trend) B(Structure) C(Reversal)\n"
        f"  RR Ratios     : A:1:1.67 | B:1:1.8 | C:1:2.0\n"
        f"  Slippage      : {SLIPPAGE_PTS}pts simulated\n"
        f"  Max/trade     : 25min hard exit\n"
        f"  Strategies    : 12 (all active — v8 testing)\n"
        f"  Mode          : Auto-bias only (no manual override)\n"
        f"  Resumed P&L   : Rs.{stats['pnl']:+.0f} "
        f"({stats['trades']} trades today)\n\n"
        f"📋 <b>Commands:</b> /status /report /news /help\n"
        f"📊 Auto-bias runs at 8:45 AM — no manual input needed."
    )

    while True:
        try:
            t   = ist_time()
            now = now_ist()
            today = now.date()

            # Pre-market reminder (9:00 AM)
            if not reminder_sent and REMINDER_TIME <= t < TRADE_START:
                pcr_cache.fetch()
                pcr_v, pcr_b, _, _ = pcr_cache.get()
                send_telegram(
                    f"⏰ <b>Nifty opens in 30 min</b>\n"
                    f"PCR: {pcr_v or 'N/A'} ({pcr_b})\n"
                    f"Auto-bias running at 8:45 AM — no manual input needed."
                )
                reminder_sent = True

            # Pre-market setup (9:00–9:30 AM)
            if t < TRADE_START:
                if not premarket_done and t >= REMINDER_TIME:
                    prev_ohlc = get_prev_day_ohlc()
                    final_bias, bias_report = get_combined_bias_nifty(
                        config.LIVE_TOKEN,
                        prev_ohlc["close"] if prev_ohlc else None,
                        "neutral"           # [V8-NO-MANUAL-BIAS] always neutral
                    )
                    pre_bias        = final_bias
                    auto_bias_report = bias_report
                    tg_listener.set_auto_bias(bias_report.get("final_bias", "neutral"))
                    pcr_cache.fetch()
                    send_telegram(format_bias_message_nifty(bias_report))
                    premarket_done = True
                time.sleep(30); continue

            premarket_done = False

            # [V9-BIAS-REFRESH] Refresh auto_bias every 15 min intraday
            if time.time() - _bias_refresh_ts > 900 and auto_bias_report and ltp is not None:
                try:
                    _new_bias, _new_rep = get_combined_bias_nifty(
                        config.LIVE_TOKEN,
                        prev_ohlc["close"] if prev_ohlc else None,
                        "neutral"
                    )
                    if _new_bias != pre_bias:
                        _tstr = now.strftime("%H:%M")
                        log.info(f"[BiasRefresh] {pre_bias}->{_new_bias} at {_tstr}")
                        send_telegram(
                            "🔄 <b>Bias Refreshed " + _tstr + " IST</b>\n"
                            "  Was : " + pre_bias.upper() + "\n"
                            "  Now : " + _new_bias.upper() + "\n"
                            "  PCR : " + str(_new_rep.get("pcr_bias","?")) +
                            " Trend:" + trend.upper()
                        )
                        pre_bias         = _new_bias
                        auto_bias_report = _new_rep
                        tg_listener.set_auto_bias(_new_rep.get("final_bias", "neutral"))
                    _bias_refresh_ts = time.time()
                except Exception as _br_err:
                    log.warning(f"Bias refresh failed: {_br_err}")

            # If bot started after 9:30 (missed premarket), run auto bias immediately
            if not premarket_done and auto_bias_report == {} and prev_ohlc is None:
                log.info("Bot started after premarket — running auto bias now")
                prev_ohlc = get_prev_day_ohlc()
                try:
                    final_bias, bias_report = get_combined_bias_nifty(
                        config.LIVE_TOKEN,
                        prev_ohlc["close"] if prev_ohlc else None,
                        "neutral"           # [V8-NO-MANUAL-BIAS] always neutral
                    )
                    pre_bias         = final_bias
                    auto_bias_report = bias_report
                    tg_listener.set_auto_bias(bias_report.get("final_bias", "neutral"))
                    pcr_cache.fetch()
                    send_telegram(
                        "⚠️ <b>Bot started after premarket — running auto bias now</b>\n"
                        + format_bias_message_nifty(bias_report)
                    )
                except Exception as e:
                    log.error(f"Late auto bias error: {e}")
                    send_telegram(f"⚠️ Auto bias failed on late start: {e}\nSend /bias manually.")

            # Session end — send summary + CSVs then exit cleanly
            # Cron job restarts the bot at 8:45 AM next trading day
            if t >= TRADE_END:
                if not closed_summary_sent:
                    send_summary(stats, pre_bias, pcr_cache, session_bias, cap_mgr, strat_tracker)
                    closed_summary_sent = True
                    save_state(stats, manual_bias="neutral")
                log.info("14:30 — session complete. Exiting cleanly for cron.")
                send_telegram(
                    "✅ <b>Session complete — Nifty v8</b>\n"
                    "Bot exiting. Cron restarts at 8:45 AM IST tomorrow."
                )
                break   # clean exit — do NOT loop, cron handles restart

            closed_summary_sent = False

            # Risk gates
            # ── PAPER MODE: soft warnings only — bot never stops ────────
            # These thresholds show what WOULD have happened in live trading.
            # In v6/v7 these become hard stops. For now just log and alert once.
            if stats["trades"] > 0 and stats["trades"] % 5 == 0:
                tg("📊", f"Paper milestone: {stats['trades']} trades today",
                   [f"P&L: Rs.{stats['pnl']:+.0f}",
                    f"W:{stats['wins']} L:{stats['losses']} T:{stats['timeouts']}",
                    "Still running — paper mode, no cap"])

            if stats["consec_loss"] >= 3 and stats["consec_loss"] % 3 == 0:
                tg("⚠️", f"Paper alert: {stats['consec_loss']} consecutive losses",
                   [f"P&L: Rs.{stats['pnl']:+.0f}",
                    "Would stop in live — continuing paper for data"])

            if stats["pnl"] <= -3000 and stats["pnl"] % 1000 < 50:
                tg("⚠️", f"Paper alert: P&L at Rs.{stats['pnl']:+.0f}",
                   ["Would hit daily loss limit in live",
                    "Continuing paper — recording all signals"])

            if stats["pnl"] >= 2000 and stats["pnl"] % 1000 < 50:
                tg("💰", f"Paper alert: P&L at Rs.{stats['pnl']:+.0f}",
                   ["Would hit profit target in live",
                    "Continuing paper — recording all signals"])
            if is_expiry_day() and t >= EXPIRY_STOP:
                time.sleep(10 * 60); continue

            # [V8-F1][V8-F2] Monitor ALL active trades (one per group)
            icon    = "⏸"  # default — reassigned per trade result
            ltp_now = get_nifty_ltp()
            for gid in list(active_trades.keys()):
                active_trade = active_trades.get(gid)
                if active_trade is None: continue
                result = None
                if ltp_now:
                    result = active_trade.check(ltp_now, t)
                if result:
                    ltp   = ltp_now
                    dur   = active_trade.duration()
                    pnl   = active_trade.calc_pnl(ltp)
                    pts   = active_trade.calc_pts_moved(ltp)
                    if result == "target":
                        icon = "✅ WIN"; stats["wins"] += 1; stats["consec_loss"] = 0
                    elif result == "sl":
                        icon = "❌ LOSS"; stats["losses"] += 1; stats["consec_loss"] += 1
                    else:
                        icon = "⏰ TIME"; stats["timeouts"] += 1; stats["consec_loss"] += 1
                    stats["trades"] += 1
                    stats["pnl"]    = round(stats["pnl"] + pnl, 0)
                    cl = active_trade.conf_label
                    if   cl == "HIGH":   stats["high_t"] += 1; stats["high_w"] += (1 if result=="target" else 0)
                    elif cl == "MEDIUM": stats["med_t"]  += 1; stats["med_w"]  += (1 if result=="target" else 0)
                    else:                stats["low_t"]  += 1; stats["low_w"]  += (1 if result=="target" else 0)
                    cap_mgr.on_result(gid, result)          # [V8-F7] per-group capital
                    strat_tracker.record(active_trade.strategy, result)
                    pcr_v, pcr_b, _, pcr_st = pcr_cache.get()
                    s_results = strat_tracker.get_results(active_trade.strategy)
                    s_wins    = s_results.count("target")
                    s_wr      = f"{s_wins}/{len(s_results)}" if s_results else "0/0"
                    tg(icon, f"TRADE #{active_trade.trade_no} [{gid}] {result.upper()}", [
                        f"Strategy   : {active_trade.strategy}",
                        f"Group      : {gid} | Conf: {active_trade.conf_label}({active_trade.conf_score}/10)",
                        f"Direction  : {active_trade.direction.upper()}",
                        f"Entry+slip : {active_trade.entry_price:.0f} | Exit: {ltp:.0f}",
                        f"Points     : {pts:+.1f} | Duration: {dur}min",
                        f"P&L        : Rs.{pnl:+.0f} | Day: Rs.{stats['pnl']:+.0f}",
                        f"Strategy WR: {s_wr} | {cap_mgr.info(gid)}",
                        f"Active now : {sum(1 for g,tr in active_trades.items() if tr)} group(s)",
                    ])
                    write_trade({
                        "date": datetime.date.today(),
                        "trade_no": active_trade.trade_no,
                        "strategy": active_trade.strategy,
                        "entry_time": active_trade.entry_time,
                        "exit_time": now.strftime("%H:%M:%S"),
                        "duration_min": dur,
                        "conf_score": active_trade.conf_score,
                        "conf_label": active_trade.conf_label,
                        "conf_reasons": getattr(active_trade, "conf_reasons_str", ""),
                        "session_bias": active_trade.session_bias,
                        "zscore_at_entry": active_trade.zscore,
                        "rsi_at_entry": active_trade.rsi,
                        "atr_at_entry": active_trade.atr,
                        "india_vix_at_entry": india_vix or "",
                        "trend_combined": active_trade.trend_strength,
                        "trend_strength": active_trade.trend_strength,
                        "trend_5m_at_entry": t5 if 't5' in dir() else "",
                        "trend_15m_at_entry": t15 if 't15' in dir() else "",
                        "trend_30m_at_entry": t30 if 't30' in dir() else "",
                        "rvol_at_entry": active_trade.rvol,
                        "vwap_at_entry": getattr(active_trade, "vwap_at_entry", ""),
                        "ema9_at_entry": getattr(active_trade, "e9_at_entry", ""),
                        "ema21_at_entry": getattr(active_trade, "e21_at_entry", ""),
                        "ema50_at_entry": getattr(active_trade, "e50_at_entry", ""),
                        "manual_bias": getattr(active_trade, "manual_bias", ""),
                        "auto_bias":   getattr(active_trade, "auto_bias", ""),
                        "final_bias":  getattr(active_trade, "final_bias", ""),
                        "direction": active_trade.direction,
                        "is_strong": active_trade.is_strong,
                        "group_id":  gid,
                        "exit_mode": ("Trail" if active_trade.trailing
                                      else f"Fixed {active_trade.tgt_pts}pts"),
                        "raw_entry_nifty": active_trade.raw_entry,
                        "entry_nifty_with_slip": active_trade.entry_price,
                        "exit_nifty": round(ltp, 1),
                        "points_moved": pts,
                        "slippage_pts": SLIPPAGE_PTS * 2,
                        "option_type": active_trade.option_type,
                        "strike": active_trade.strike,
                        "expiry": active_trade.expiry,
                        "premium": active_trade.premium,
                        "lots": 1,
                        "capital_used": active_trade.premium * LOT_SIZE,
                        "sl_points": active_trade.sl_pts,
                        "target_points": active_trade.tgt_pts,
                        "rr_ratio": f"1:{active_trade.tgt_pts/active_trade.sl_pts:.1f}",
                        "sl_price": active_trade.sl_price,
                        "target_price": active_trade.tgt_price,
                        "pnl_est": pnl,
                        "result": result,
                        "be_triggered": active_trade.be_moved,
                        "trail_triggered": active_trade.trailing,
                        "consec_losses_after": stats["consec_loss"],
                        "daily_pnl_after": stats["pnl"],
                        "strategy_wr_today": s_wr,
                        "skip_reason": "",
                        "notes": active_trade.signal
                    })
                    save_state(stats, tg_listener.bias)   # [V8-F2] persist after every trade
                    active_trades[gid] = None             # free the group slot

            # If any active trades, sleep briefly and re-check
            if any(tr for tr in active_trades.values()):
                time.sleep(15); continue

            # Check if async retest completed → open pending trade
            if pending_trade_args and retest_waiter.is_done():
                if retest_waiter.succeeded():
                    ep = retest_waiter.get_ltp()
                    args = pending_trade_args
                    trade_no += 1
                    _retest_gid = args.get("group_id", _get_group(args["strategy"])[0])
                    active_trades[_retest_gid] = open_trade(
                        trade_no, args["strategy"], args["direction"], ep,
                        pcr_cache, tg_listener, pre_bias, args["is_strong"],
                        args["signal"], args["rvol"], args["trend_strength"],
                        args["risk"], args["conf_score"], args["conf_label"],
                        args["conf_reasons"], session_bias, args["zscore"],
                        args["rsi"], args["atr"], args["vwap"], args["prev_vwap"],
                        args["e9"], args["e21"], args["e50"], args["capital"],
                        args.get("auto_bias_report", auto_bias_report)
                    )
                    stats[args["stat_key"]] += 1
                else:
                    tg("⏰", f"Retest timed out — {pending_trade_args['strategy']}",
                       [f"Zone:{pending_trade_args['bottom']:.0f}→{pending_trade_args['top']:.0f}",
                        "No entry taken"])
                    stats["skipped"] += 1
                pending_trade_args = None
                time.sleep(30); continue

            # If retest is running, skip new scans
            if pending_trade_args and not retest_waiter.is_done():
                time.sleep(15); continue

            # [F23] Retry prev_ohlc if it failed at premarket
            # Important for CPR + prev_vwap calculations
            if prev_ohlc is None and t >= TRADE_START:
                prev_ohlc = get_prev_day_ohlc()
                if prev_ohlc:
                    log.info("prev_ohlc fetched successfully on retry")

            # Fetch fresh data
            ltp    = get_nifty_ltp()
            df_5   = get_candles(5)
            df_15  = get_candles(15)
            df_30  = get_candles(30)
            fut_df = get_futures_candles(5)
            if ltp is None or df_5 is None:
                time.sleep(15); continue

            # [P5] Track candles formed since open
            candles_seen = len(df_5)

            # [V9-REGIME-WINDOW] Need 18 candles for regime classifier (1.8×ATR needs 12-candle window)
            if candles_seen < 18:
                time.sleep(60); continue

            # [V9-FIX2] Refresh option chain — dynamic TTL (5min high-vol, 15min normal)
            if option_chain is not None:
                option_chain.refresh(ltp, atr=atr if "atr" in dir() else 0)

            # Use first candle's open as open_price — more accurate than LTP at start
            if open_price is None:
                try:
                    open_price = float(df_5["open"].iloc[0])
                except Exception:
                    open_price = ltp

            # [F12] VIX spike guard
            india_vix = get_india_vix()
            session_bias.update(ltp, df_5, india_vix)
            vix_spike = session_bias.vix_spike_detected(india_vix)
            if vix_spike:
                log.warning("VIX spike guard active — skipping new entries")
                # [F24] Send alert ONCE, then suppress for 30 min
                should_alert = (not vix_alert_sent or
                               (vix_alert_at and (now_ist() - vix_alert_at).seconds > 1800))
                if should_alert:
                    tg("⚠️", "VIX Spike Guard ACTIVE",
                       [f"VIX at {india_vix:.2f} — spiked >{VIX_SPIKE_PCT}% from open",
                        "All new entries suspended",
                        "Active trade (if any) continues normally"])
                    vix_alert_sent = True
                    vix_alert_at   = now_ist()
                prev_ltp = ltp; time.sleep(60); continue
            else:
                # Reset alert state when VIX comes back down
                if vix_alert_sent:
                    tg("✅", "VIX Spike Cleared",
                       [f"VIX back to {india_vix:.2f}",
                        "Trading resumed"])
                    vix_alert_sent = False
                    vix_alert_at   = None

            zscore = session_bias.get_zscore(ltp)
            atr    = calc_atr(df_5)
            rsi    = calc_rsi(df_5)

            # ORB formation
            if not orb_formed and t >= ORB_END_TIME:
                try:
                    orb_df = df_5[df_5["timestamp"].dt.time <= ORB_END_TIME]
                    if not orb_df.empty:
                        orb_high  = float(orb_df["high"].max())
                        orb_low   = float(orb_df["low"].min())
                        orb_formed = True
                        tg("📐", "ORB Formed", [
                            f"High:{orb_high:.0f} Low:{orb_low:.0f}",
                            f"Size:{orb_high-orb_low:.0f}pts ATR:{atr:.0f}pts"
                        ])
                except Exception as e: log.error(f"ORB:{e}")

            # PCR refresh
            if pcr_cache.should_refresh():
                pcr_cache.fetch()
            pcr_v, pcr_b, pcr_weight, pcr_status = pcr_cache.get()

            # Indicators
            df5_ema = calc_ema(df_5)
            e9  = round(float(df5_ema["ema9"].iloc[-1]),  1)
            e21 = round(float(df5_ema["ema21"].iloc[-1]), 1)
            e50 = round(float(df5_ema["ema50"].iloc[-1]), 1)
            trend, _, trend_strength = detect_trend_multi(df_5, df_15, df_30, e9, e21, e50, ltp)
            t5,  _, _ = detect_trend_relaxed(df_5)
            t15, _, _ = detect_trend_relaxed(df_15)
            t30, _, _ = detect_trend_relaxed(df_30)
            rvol      = calc_rvol(df_5, fut_df)

            df5_vwap = calc_vwap_bands(df_5, atr)
            lr   = df5_vwap.iloc[-1]
            vwap = round(float(lr["vwap"]), 1)
            vu1  = round(float(lr["vwap_u1"]), 1)
            vl1  = round(float(lr["vwap_l1"]), 1)
            bw   = round(float(lr.get("band_width", 0)), 1)
            prev_vwap, prev_vwap_valid = calc_prev_vwap(prev_ohlc, open_price)

            # CPR levels
            cpr_pivot, cpr_bc, cpr_tc = calc_cpr(prev_ohlc)

            # Strategy detection
            fvg,     fvg_r    = detect_fvg(df_5)
            orb_s,   orb_r    = detect_orb(df_5, orb_high, orb_low, ltp)
            ema_stk, ema_sk_r = detect_ema_stack(df5_ema, ltp, t5, rvol)
            ema_cx,  ema_cx_r = (detect_ema_cross(df5_ema, prev_df5_ema)
                                  if prev_df5_ema is not None else (None, "No prev EMA"))
            vwap_bb, vwap_bb_r = detect_vwap_band_break(df5_vwap, ltp, t5, atr)
            vwap_cx, vwap_cx_r = detect_vwap_cross(df5_vwap, ltp, df_5)
            ema50_b, ema50_r   = detect_ema50_bounce(df5_ema, ltp, t5, df_5)
            st_dir, st_level, st_fresh = calc_supertrend(df_5)
            st_sig,  st_r      = detect_supertrend_signal(df_5, trend, ltp, atr)
            cpr_sig, cpr_r     = detect_cpr_signal(ltp, cpr_pivot, cpr_bc, cpr_tc,
                                                    trend, prev_ltp if prev_ltp else ltp)
            prev_df5_ema = df5_ema.copy()

            # 5-min scan log
            do_scan = (last_scan is None or (now_ist() - last_scan).seconds >= 300)
            if do_scan:
                last_scan = now_ist()
                strats = []
                if fvg and fvg.get("strong") and fvg.get("age", 99) <= FVG_MAX_AGE_CANDLES:
                    strats.append("StrongFVG")
                if orb_s and orb_formed: strats.append("ORB+EMA")
                if ema_stk: strats.append("EMAStack")
                if vwap_bb: strats.append("VWAPBand")
                if vwap_cx: strats.append("VWAPCross")
                if ema50_b: strats.append("EMA50Bounce")
                if ema_cx:  strats.append("EMACross")
                if st_sig:  strats.append("SuperTrend")
                if cpr_sig: strats.append("CPR")
                entry_met   = len(strats) > 0
                chg_open    = round(ltp - open_price, 1) if open_price else 0
                chg_pct     = round((chg_open / open_price * 100), 2) if open_price else 0
                pdh = prev_ohlc["high"]  if prev_ohlc else ""
                pdl = prev_ohlc["low"]   if prev_ohlc else ""
                pdc = prev_ohlc["close"] if prev_ohlc else ""
                # Preview confidence for the FIRST detected strategy's actual direction
                # [F20] Use signal-specific direction, not always trend
                conf_prev = 0
                if strats:
                    first = strats[0]
                    if first == "StrongFVG" and fvg:
                        prev_dir = fvg["type"]
                    elif first == "ORB+EMA" and orb_s:
                        prev_dir = orb_s["type"]
                    elif first == "EMAStack" and ema_stk:
                        prev_dir = ema_stk["type"]
                    elif first == "VWAPBand" and vwap_bb:
                        prev_dir = vwap_bb["type"]
                    elif first == "VWAPCross" and vwap_cx:
                        prev_dir = vwap_cx["type"]
                    elif first == "EMA50Bounce" and ema50_b:
                        prev_dir = ema50_b["type"]
                    elif first == "EMACross" and ema_cx:
                        prev_dir = ema_cx["type"]
                    elif first == "SuperTrend" and st_sig:
                        prev_dir = st_sig["type"]
                    elif first == "CPR" and cpr_sig:
                        prev_dir = cpr_sig["type"]
                    else:
                        prev_dir = trend
                    conf_prev, _, _ = calc_confidence(
                        prev_dir, trend, e9, e21, e50, ltp,
                        vwap, pcr_b, pcr_weight, rvol, pre_bias, t5, t15, t30,
                        rsi
                    )

                # [V9-F2] Classify regime for this scan
                intraday_regime = classify_intraday_regime(df_5, e9, e21)

                write_scan({
                    "datetime": now.strftime("%Y-%m-%d %H:%M IST"),
                    "nifty_ltp": round(ltp, 1), "chg_from_open": chg_open,
                    "chg_pct": chg_pct, "session_bias": session_bias.bias,
                    "zscore": round(zscore, 2), "rsi": rsi, "atr": atr,
                    "india_vix": india_vix or "", "vix_spike_guard": vix_spike,
                    "trend_5m": t5, "trend_15m": t15, "trend_30m": t30,
                    "trend_combined": trend, "trend_strength": trend_strength,
                    "rvol": rvol, "vwap": vwap, "vwap_u1": vu1, "vwap_l1": vl1,
                    "band_width": bw, "price_vs_vwap": round(ltp - vwap, 1),
                    "prev_vwap": prev_vwap or "", "prev_vwap_valid": prev_vwap_valid,
                    "ema9": e9, "ema21": e21, "ema50": e50,
                    "ema9_vs_ema21": round(e9 - e21, 1),
                    "price_vs_ema9": round(ltp - e9, 1),
                    "price_vs_ema50": round(ltp - e50, 1),
                    "fvg_found": fvg is not None,
                    "fvg_type": fvg["type"] if fvg else "",
                    "fvg_strong": fvg["strong"] if fvg else "",
                    "fvg_size": fvg["size"] if fvg else "",
                    "fvg_age": fvg["age"] if fvg else "",
                    "orb_high": orb_high or "", "orb_low": orb_low or "",
                    "orb_signal": orb_s["type"] if orb_s else "",
                    "orb_size": orb_s["size"] if orb_s else "",
                    "ema_stack": ema_stk["type"] if ema_stk else "",
                    "ema_cross": ema_cx["type"] if ema_cx else "",
                    "vwap_band": vwap_bb["type"] if vwap_bb else "",
                    "vwap_cross": vwap_cx["type"] if vwap_cx else "",
                    "ema50_bounce": ema50_b["type"] if ema50_b else "",
                    "supertrend_dir": st_dir, "supertrend_fresh": st_fresh,
                    "supertrend_level": st_level,
                    "cpr_pivot": cpr_pivot or "", "cpr_tc": cpr_tc or "",
                    "cpr_bc": cpr_bc or "",
                    "cpr_signal": cpr_sig["type"] if cpr_sig else "",
                    "pcr": pcr_v or "", "pcr_bias": pcr_b, "pcr_status": pcr_status,
                    # Full bias breakdown
                    "manual_bias":       tg_listener.bias,
                    "auto_bias":         auto_bias_report.get("final_bias", ""),
                    "final_bias":        pre_bias,
                    "auto_bias_score":   auto_bias_report.get("score", ""),
                    "auto_bias_conf":    auto_bias_report.get("confidence", ""),
                    "pcr_source_bias":   auto_bias_report.get("pcr_bias", ""),
                    "vix_bias":          auto_bias_report.get("vix_bias", ""),
                    "gift_nifty_bias":   auto_bias_report.get("gift_bias", ""),
                    "news_bias":         auto_bias_report.get("news_bias", ""),
                    "pdh": pdh, "pdl": pdl, "pdc": pdc,
                    "capital_mode": cap_mgr.get_info(),
                    "consec_losses": stats["consec_loss"],
                    "entry_condition_met": entry_met,
                    "strategy_triggered": ",".join(strats),
                    "conf_score_preview": conf_prev,
                    "trades_today": stats["trades"], "daily_pnl": stats["pnl"],
                    "intraday_regime": intraday_regime if "intraday_regime" in dir() else "UNKNOWN",
                    "reason": f"FVG:{fvg_r[:30]}|ORB:{orb_r[:30]}|ST:{st_r[:30]}"
                })

                # Build PDH/PDL/CPR strings safely (None values would crash f-strings)
                # [F22] Guard against None before formatting
                if prev_ohlc:
                    pdh_str = (f"PDH/PDL/PDC  : {prev_ohlc['high']:.0f}/{prev_ohlc['low']:.0f}/{prev_ohlc['close']:.0f}" if prev_ohlc else "PDH/PDL/PDC  : N/A")
                else:
                    pdh_str = "PDH/PDL/PDC  : N/A (no prev day data)"
                if cpr_pivot is not None:
                    cpr_str = f"CPR levels   : P:{cpr_pivot:.0f} TC:{cpr_tc:.0f} BC:{cpr_bc:.0f}"
                else:
                    cpr_str = "CPR levels   : N/A (no prev day data)"
                vix_line = f"RSI:{rsi:.0f} ATR:{atr:.0f}pts" + (f" VIX:{india_vix:.2f}" if india_vix else "")

                icon = "✅" if entry_met else "⏸"
                tg(icon, f"NIFTY v9 SCAN {now.strftime('%H:%M')}", [
                    f"Nifty        : {ltp:.2f} ({chg_pct:+.2f}%)",
                    f"Session bias : {session_bias.bias.upper()} Z:{zscore:+.2f}",
                    vix_line,
                    f"Trend        : {trend.upper()} ({trend_strength})",
                    f"5m/15m/30m   : {t5}/{t15}/{t30}",
                    f"RVOL         : {rvol}x",
                    f"VWAP         : {vwap:.0f} ({ltp-vwap:+.0f}) BW:{bw:.0f}pts",
                    f"EMA9/21/50   : {e9:.0f}/{e21:.0f}/{e50:.0f}",
                    f"FVG          : {fvg_r[:45] if fvg else 'NONE'}",
                    f"ORB          : {orb_r[:45]}",
                    f"EMA Stack    : {ema_sk_r[:35] if ema_stk else 'NONE'}",
                    f"SuperTrend   : {st_r[:55]}",
                    f"CPR signal   : {cpr_r[:40]}",
                    f"VWAP Cross   : {vwap_cx_r[:35] if vwap_cx else 'NONE'}",
                    f"PCR          : {pcr_v or 'N/A'} ({pcr_b}) [{pcr_status}]",
                    pdh_str,
                    cpr_str,
                    f"Capital mode : {cap_mgr.get_info()}",
                    f"Auto bias    : {tg_listener._auto_bias.upper()}  [auto-only]",
                    f"Pre-bias     : {pre_bias.upper()}  (from auto_bias)",
                    f"Final bias   : {pre_bias.upper()}",
                    f"Conf preview : {conf_prev}/10",
                    f"Regime       : {intraday_regime if 'intraday_regime' in dir() else '?'}",
                    f"LivePCR      : {option_chain.get_live_pcr():.3f}" if option_chain and option_chain._fetch_ok else "LivePCR      : N/A",
                    f"Signals      : {', '.join(strats) if strats else 'NONE'}"
                ])

            # ── STRATEGY EXECUTOR ────────────────────────────────────────
            def try_trade(strategy_name, direction, is_strong,
                          signal_text, stat_key, retest_zone=None):
                nonlocal trade_no, pending_trade_args

                # [V8-F1] Check group slot availability
                gid, gcfg = _get_group(strategy_name)
                if active_trades.get(gid) is not None:
                    return False   # group already has an active trade

                # [V8-F1] Max concurrent groups check
                active_count = sum(1 for tr in active_trades.values() if tr is not None)
                if active_count >= MAX_CONCURRENT_GROUPS:
                    return False   # already at max concurrent

                if pending_trade_args:
                    return False   # retest in progress

                # Safe defaults — conf calculated later, _skip may be called before that
                conf_score = 0; conf_label = "LOW"; conf_reasons = []

                def _skip(reason, conf_score=0, conf_label=""):
                    stats["skipped"] += 1
                    write_skip({
                        "datetime":     now.strftime("%Y-%m-%d %H:%M IST"),
                        "strategy":     strategy_name,
                        "direction":    direction,
                        "skip_reason":  reason,
                        "conf_score":   conf_score,
                        "conf_label":   conf_label,
                        "group_id":     gid,
                        "session_bias": session_bias.bias,
                        "zscore":       round(zscore, 2),
                        "rsi":          rsi,
                        "manual_bias":  "n/a",  # manual bias removed
                        "auto_bias":    auto_bias_report.get("final_bias", ""),
                        "final_bias":   pre_bias,
                        "trend_combined": trend,
                        "rvol":         rvol,
                        "pcr":          pcr_v or "",
                        "pcr_bias":     pcr_b,
                        "nifty_ltp":    round(ltp, 1),
                        "trades_today": stats["trades"],
                        "daily_pnl":    stats["pnl"],
                    })

                # [V7-F16] Signal dedup cooldown — 15 mins per strategy+direction
                cooldown_key  = f"{strategy_name}_{direction}"
                last_fired    = signal_cooldown.get(cooldown_key, 0)
                if time.time() - last_fired < 900:
                    wait_mins = round((900 - (time.time()-last_fired)) / 60, 1)
                    _skip(f"Dedup: {strategy_name} cooldown {wait_mins}min")
                    return False

                # [V7-F17] Late day ATR gate
                if ist_time() >= datetime.time(14, 0) and atr < 30:
                    _skip(f"LateDayATR: {atr:.0f}pts < 30 after 14:00")
                    return False

                # [P5] Min candles gate — block non-ORB/BOS in first 30 min
                if strategy_name not in ("ORB+EMA", "BOS") and candles_seen < MIN_CANDLES_BEFORE_ENTRY:
                    _skip(f"OpenNoise: {candles_seen} candles < {MIN_CANDLES_BEFORE_ENTRY} (30min gate)")
                    return False

                # [V9-F4] Per-group ATR gate — each group needs different ATR minimum
                # Group A (trend): ATR>35 — high momentum required
                # Group B (structure): ATR>28 — needs room to move to target
                # Group C (reversal): ATR>28 — target=16pts, need ATR>24pts minimum
                _grp_atr_min = gcfg.get("atr_min", ATR_MIN_FOR_TRADE)
                if atr < _grp_atr_min:
                    _skip(f"ATRTooLow[Grp{gid}]: {atr:.0f}pts < {_grp_atr_min}pts min")
                    return False
                # Also enforce absolute floor
                if atr < ATR_MIN_FOR_TRADE:
                    _skip(f"ATRFloor: {atr:.0f}pts < {ATR_MIN_FOR_TRADE}pts abs min")
                    return False

                # [V9-FIX2] Option quality gate — liquidity + spread + IV
                if option_chain is not None:
                    _oc_ok, _oc_reason, _oc_info = check_option_quality(
                        option_chain, ltp, direction,
                        gcfg["sl"], gcfg["target"]
                    )
                    if not _oc_ok:
                        _skip(f"OptionQuality: {_oc_reason}", conf_score, conf_label)
                        return False
                    # Log option info for trade analysis
                    _trade_delta  = _oc_info.get("delta", 0.45)
                    _trade_prem_sl= _oc_info.get("premium_sl", 0)
                    _trade_spread = _oc_info.get("spread", 0)
                else:
                    _trade_delta=0.45; _trade_prem_sl=0; _trade_spread=0

                # [V9-F2] Regime gate — block trend strategies in weak/ranging markets
                # April 2026: WEAK_BULL WR 27% (-Rs.4615), TRENDING WR 67%+
                _regime = classify_intraday_regime(df_5, e9, e21)
                if strategy_name in TREND_STRATEGIES and _regime in REGIME_BLOCK_TREND:
                    _skip(f"RegimeBlock: {_regime} — trend strat blocked in non-trending market")
                    return False

                # [V9-F3] ATR momentum filter for trend strategies
                # April 2026: crash days ATR 50-70 → WR 100%, drift days ATR 25-35 → WR 29%
                if strategy_name in TREND_STRATEGIES and atr < ATR_TRENDING_MIN:
                    _skip(f"ATRTrend: {atr:.0f}pts < {ATR_TRENDING_MIN}pts (no momentum for trend trade)")
                    return False

                # [V8-F3b] CONF CALCULATED FIRST — before any gate
                # v7 bug: RSI/session blocks fired before conf, producing conf=0 in logs
                srules = PER_STRATEGY_RULES.get(strategy_name, DEFAULT_STRATEGY_RULES)

                # Resolve effective bias [V8-F3c] session_bias_over_auto fix
                effective_bias = pre_bias
                if srules.get("session_bias_over_auto") and session_bias.is_set:
                    if session_bias.bias == direction:
                        effective_bias = direction

                conf_score, conf_label, conf_reasons = calc_confidence(
                    direction, trend, e9, e21, e50, ltp,
                    vwap, pcr_b, pcr_weight, rvol,
                    effective_bias,   # [V8-F3c] use effective_bias not raw pre_bias
                    t5, t15, t30, rsi,
                    strategy_name     # [V8-F3a] pass strategy for RSI penalty logic
                )

                # [V6-F15] Perfect conf alert
                if conf_score >= PERFECT_CONF:
                    send_telegram(
                        f"🔥 <b>PERFECT 10/10 — {strategy_name}</b>\n"
                        f"  Direction: {direction.upper()} | RSI:{rsi:.0f} | Grp:{gid}\n"
                        f"  {signal_text}"
                    )

                # [F11] StratTracker — PERFECT conf always bypasses
                allowed_entry, tracker_reason = strat_tracker.can_trade(strategy_name, conf_score)
                if not allowed_entry:
                    _skip(f"StratTracker: {tracker_reason}", conf_score, conf_label)
                    return False

                # [V8-F3b] Per-strategy RSI gate (now AFTER conf calc)
                if direction == "bearish" and rsi < srules["rsi_short_min"]:
                    _skip(f"RSIBlock[{strategy_name}]: SHORT @ RSI {rsi:.0f}<{srules['rsi_short_min']}",
                          conf_score, conf_label)
                    return False
                if direction == "bullish" and rsi > srules["rsi_long_max"]:
                    _skip(f"RSIBlock[{strategy_name}]: LONG @ RSI {rsi:.0f}>{srules['rsi_long_max']}",
                          conf_score, conf_label)
                    return False

                # [V8-F3c] Counter-trend check with effective_bias
                # [FIX-A] session_bias_over_auto strategies bypass session block
                # when the multi-TF trend agrees with direction — trend IS the session
                zs = session_bias.get_zscore(ltp)
                if srules.get("allow_counter_trend"):
                    sess_ok, sess_reason = True, "CT strategy — allowed"
                elif srules.get("session_bias_over_auto") and trend == direction:
                    # Trend has flipped to match direction — override stale session bias
                    sess_ok, sess_reason = True, f"TrendOverride: trend={trend} matches direction"
                    log.info(f"{strategy_name} session bypassed: trend={trend} overrides session={session_bias.bias}")
                else:
                    sess_ok, _, sess_reason = session_bias.trade_allowed(direction, ltp, rsi)
                    if not sess_ok:
                        _skip(f"Session: {sess_reason}", conf_score, conf_label)
                        return False

                # Per-strategy min conf
                strat_min_conf = srules.get("min_conf", MIN_CONF.get(strategy_name, 4))
                if conf_score < strat_min_conf:
                    _skip(f"LowConf: {conf_score}<{strat_min_conf} min", conf_score, conf_label)
                    return False

                # [V8-F3c] Pre-bias gate using effective_bias
                if not srules.get("bias_mismatch_ok"):
                    if effective_bias != "neutral" and effective_bias != direction:
                        rsi_extreme = (direction == "bullish" and rsi < RSI_MEAN_REV_OS) or \
                                      (direction == "bearish" and rsi > RSI_MEAN_REV_OB)
                        if abs(zs) < ZSCORE_THRESHOLD or not rsi_extreme:
                            _skip(f"BiasMismatch: bias={effective_bias} dir={direction} "
                                  f"Z={zs:.2f} RSI={rsi:.0f}",
                                  conf_score, conf_label)
                            return False

                # Reversal risk check
                # Counter-trend strategies skip bias-conflict check — conflicting
                # with bias IS their signal (RSIDivergence, VWAPCross, CPR, StrongFVG)
                _ct_strats = ("RSIDivergence", "VWAPCross", "CPR", "StrongFVG")
                # [FIX2] Also bypass when trend override already cleared session block
                _trend_override_active = (
                    srules.get("session_bias_over_auto") and trend == direction
                )
                if strategy_name not in _ct_strats and not _trend_override_active:
                    proceed, risk, summary, rev_sigs = pre_trade_check_nifty(
                        df_5, df_15, direction, pre_bias,
                        prev_ohlc["close"] if prev_ohlc else None, prev_ohlc
                    )
                    send_telegram(format_reversal_alert_nifty(
                        risk, proceed, rev_sigs, summary, strategy_name, direction
                    ))
                    if not proceed:
                        _skip(f"RevRisk: {summary[:60]}", conf_score, conf_label)
                        return False
                else:
                    risk = "LOW"  # counter-trend — skip reversal check

                capital = cap_mgr.get_capital(gid)   # [V8-F7] per-group capital

                if retest_zone:
                    bottom, top = retest_zone
                    pending_trade_args = {
                        "strategy": strategy_name, "direction": direction,
                        "is_strong": is_strong, "signal": signal_text,
                        "stat_key": stat_key, "bottom": bottom, "top": top,
                        "rvol": rvol, "trend_strength": trend_strength,
                        "risk": risk, "conf_score": conf_score,
                        "conf_label": conf_label, "conf_reasons": conf_reasons,
                        "zscore": zs, "rsi": rsi, "atr": atr,
                        "vwap": vwap, "prev_vwap": prev_vwap,
                        "e9": e9, "e21": e21, "e50": e50, "capital": capital,
                        "auto_bias_report": dict(auto_bias_report),
                        "group_id": gid,
                    }
                    retest_waiter.start(bottom, top)
                    signal_cooldown[cooldown_key] = time.time()
                    return True
                else:
                    ep = get_nifty_ltp()
                    if ep is None: return False
                    trade_no += 1
                    new_trade = open_trade(
                        trade_no, strategy_name, direction, ep,
                        pcr_cache, tg_listener, pre_bias, is_strong,
                        f"{signal_text} | {conf_label}({conf_score}/10) | "
                        f"Grp:{gid} Sess:{session_bias.bias} Z:{zs:+.2f} RSI:{rsi:.0f}",
                        rvol, trend_strength, risk,
                        conf_score, conf_label, conf_reasons,
                        session_bias, zs, rsi, atr,
                        vwap, prev_vwap, e9, e21, e50, capital,
                        auto_bias_report
                    )
                    active_trades[gid] = new_trade    # [V8-F1] assign to group slot
                    stats[stat_key] += 1
                    signal_cooldown[cooldown_key] = time.time()
                    return True

                # [F11] Unified strategy tracker check
                allowed_entry, tracker_reason = strat_tracker.can_trade(strategy_name, 0)
                if not allowed_entry:
                    log.info(f"{strategy_name} blocked by tracker: {tracker_reason}")
                    _skip(f"StratTracker: {tracker_reason}")
                    return False

            # ── RUN ALL 12 STRATEGIES ────────────────────────────────────

                # ── [V7-F1] PER-STRATEGY VALIDATION ─────────────────────────
                # Load this strategy's specific rules (fallback to defaults)
                srules = PER_STRATEGY_RULES.get(strategy_name, DEFAULT_STRATEGY_RULES)

                # [V7-F2] Per-strategy RSI gate (replaces global RSI block)
                if direction == "bearish" and rsi < srules["rsi_short_min"]:
                    log.info(f"{strategy_name} RSI {rsi:.0f} oversold — SHORT blocked "
                             f"(rule:{srules['rsi_short_min']})")
                    _skip(f"RSIBlock[{strategy_name}]: SHORT @ RSI {rsi:.0f} "
                          f"< {srules['rsi_short_min']}")
                    return False
                if direction == "bullish" and rsi > srules["rsi_long_max"]:
                    log.info(f"{strategy_name} RSI {rsi:.0f} overbought — LONG blocked "
                             f"(rule:{srules['rsi_long_max']})")
                    _skip(f"RSIBlock[{strategy_name}]: LONG @ RSI {rsi:.0f} "
                          f"> {srules['rsi_long_max']}")
                    return False

                # [V7-F14] Session bias vs auto_bias conflict resolution
                # For strategies with session_bias_over_auto=True:
                #   if session=bullish and dir=bullish → allow even if auto_bias=bearish
                effective_bias = pre_bias
                if srules.get("session_bias_over_auto") and session_bias.is_set:
                    if session_bias.bias == direction:
                        effective_bias = direction   # session wins
                        log.info(f"{strategy_name} session_bias({session_bias.bias}) "
                                 f"overrides auto_bias({pre_bias}) → allow {direction}")

                # [V7] Session counter-trend check (per-strategy)
                if srules.get("allow_counter_trend"):
                    # Strategy explicitly allows counter-trend — skip session block
                    sess_ok, zs, sess_reason = True, session_bias.get_zscore(ltp), "CT strategy — allowed"
                else:
                    sess_ok, zs, sess_reason = session_bias.trade_allowed(direction, ltp, rsi)
                    if not sess_ok:
                        log.info(f"{strategy_name} session blocked: {sess_reason}")
                        _skip(f"Session: {sess_reason}")
                        return False

                # [F29] CSV-DRIVEN PATCH: Block all trades when ATR is too low.
                # 2026-05-08: avg ATR 18pts, both trades stopped out. Options
                # need volatility to move. Below 20pts ATR, theta decay > price
                # movement potential.
                if atr < ATR_MIN_FOR_TRADE:
                    log.info(f"{strategy_name} ATR {atr:.0f} < {ATR_MIN_FOR_TRADE} — dead volatility")
                    _skip(f"ATRTooLow: {atr:.0f}pts < {ATR_MIN_FOR_TRADE}pts min")
                    return False

                # [V6-F7] Market condition re-check at signal time (not cached from scan start)
                # conf_score computed fresh here with live ltp/rsi/ema values
                conf_score, conf_label, conf_reasons = calc_confidence(
                    direction, trend, e9, e21, e50, ltp,
                    vwap, pcr_b, pcr_weight, rvol, pre_bias, t5, t15, t30,
                    rsi
                )

                # [V6-F15] PERFECT CONF alert — 10/10 always gets Telegram notification
                if conf_score >= PERFECT_CONF:
                    send_telegram(
                        f"🔥 <b>PERFECT 10/10 SIGNAL — {strategy_name}</b>\n"
                        f"  Direction: {direction.upper()}\n"
                        f"  {signal_text}\n"
                        f"  RSI:{rsi:.0f} RVOL:{rvol}x ATR:{atr:.0f}\n"
                        f"  All filters GREEN — executing trade"
                    )

                # [F11] Re-check with actual conf score
                allowed_entry2, tracker_reason2 = strat_tracker.can_trade(strategy_name, conf_score)
                if not allowed_entry2:
                    log.info(f"{strategy_name} re-entry blocked: {tracker_reason2}")
                    _skip(f"ReEntry: {tracker_reason2}", conf_score, conf_label)
                    return False

                # [V7] Per-strategy min_conf override
                strat_min_conf = srules.get("min_conf", MIN_CONF.get(strategy_name, 4))
                if conf_score < strat_min_conf:
                    log.info(f"{strategy_name} conf {conf_score}<{strat_min_conf} skip")
                    _skip(f"LowConf: {conf_score}<{strat_min_conf} min", conf_score, conf_label)
                    return False

                # [V7] Pre-bias gate — skip entirely if strategy says bias_mismatch_ok
                # or if effective_bias (after session override) matches direction
                if not srules.get("bias_mismatch_ok"):
                    if effective_bias != "neutral" and effective_bias != direction:
                        rsi_extreme = (direction == "bullish" and rsi < RSI_MEAN_REV_OS) or \
                                      (direction == "bearish" and rsi > RSI_MEAN_REV_OB)
                        if abs(zs) < ZSCORE_THRESHOLD or not rsi_extreme:
                            log.info(f"{strategy_name} bias mismatch: "
                                     f"effective_bias={effective_bias} dir={direction} "
                                     f"Z={zs:.2f} RSI={rsi:.0f}")
                            _skip(f"BiasMismatch: pre_bias={effective_bias} dir={direction} "
                                  f"Z={zs:.2f} RSI={rsi:.0f}",
                                  conf_score, conf_label)
                            return False

                # Reversal risk pre-trade check
                # Counter-trend strategies bypass this — conflicting bias IS their signal
                if strategy_name not in ("RSIDivergence", "VWAPCross", "CPR", "StrongFVG"):
                    proceed, risk, summary, rev_sigs = pre_trade_check_nifty(
                        df_5, df_15, direction, pre_bias,
                        prev_ohlc["close"] if prev_ohlc else None, prev_ohlc
                    )
                    send_telegram(format_reversal_alert_nifty(
                        risk, proceed, rev_sigs, summary, strategy_name, direction
                    ))
                    if not proceed:
                        _skip(f"RevRisk: {summary[:60]}", conf_score, conf_label)
                        return False
                else:
                    risk = "LOW"  # counter-trend — skip reversal check

                capital = cap_mgr.get_capital(gid)

                if retest_zone:
                    bottom, top = retest_zone
                    # [F2] Start async retest — non-blocking
                    pending_trade_args = {
                        "strategy": strategy_name, "direction": direction,
                        "is_strong": is_strong, "signal": signal_text,
                        "stat_key": stat_key, "bottom": bottom, "top": top,
                        "rvol": rvol, "trend_strength": trend_strength,
                        "risk": risk, "conf_score": conf_score,
                        "conf_label": conf_label, "conf_reasons": conf_reasons,
                        "zscore": zs, "rsi": rsi, "atr": atr,
                        "vwap": vwap, "prev_vwap": prev_vwap,
                        "e9": e9, "e21": e21, "e50": e50, "capital": capital,
                        "auto_bias_report": dict(auto_bias_report)
                    }
                    retest_waiter.start(bottom, top)
                    # [V7-F16] Stamp cooldown
                    signal_cooldown[cooldown_key] = time.time()
                    return True  # pending
                else:
                    ep = get_nifty_ltp()
                    if ep is None: return False
                    trade_no += 1
                    active_trades[gid] = open_trade(
                        trade_no, strategy_name, direction, ep,
                        pcr_cache, tg_listener, pre_bias, is_strong,
                        f"{signal_text} | {conf_label}({conf_score}/10) | "
                        f"Grp:{gid} Sess:{session_bias.bias} Z:{zs:+.2f} RSI:{rsi:.0f}",
                        rvol, trend_strength, risk,
                        conf_score, conf_label, conf_reasons,
                        session_bias, zs, rsi, atr,
                        vwap, prev_vwap, e9, e21, e50, capital,
                        auto_bias_report
                    )
                    stats[stat_key] += 1
                    # [V7-F16] Stamp cooldown
                    signal_cooldown[cooldown_key] = time.time()
                    return True

            # ── RUN ALL 12 STRATEGIES ────────────────────────────────────

            # 1. Strong FVG — fresh + gap intact, retest at edge (async)
            if (fvg and fvg.get("strong") and
                    fvg.get("age", 99) <= FVG_MAX_AGE_CANDLES and
                    not pending_trade_args):
                edge = fvg["edge"]
                if try_trade("StrongFVG", fvg["type"], True,
                             f"Strong FVG {fvg['size']:.1f}pts age:{fvg['age']}c intact",
                             "fvg", retest_zone=(edge - 5, edge + 5)):
                    prev_ltp = ltp; time.sleep(15); continue

            # 2. ORB + EMA — [V9-F1] DISABLED
            # April 2026: WR 29%, -Rs.5,330 (14 trades). Worst strategy.
            # Fires on false breakouts. Re-enable after more data.
            # if orb_s and orb_formed and not pending_trade_args:
            #     ... (ORB+EMA code disabled)

            # 3. EMA Stack (with RVOL gate built into detector)
            if ema_stk and not pending_trade_args:
                if try_trade("EMAStack", ema_stk["type"], False,
                             f"EMA Stack {ema_stk['type']} RVOL:{rvol}x",
                             "ema_stack"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 4. VWAP Band Break (ATR filtered)
            if vwap_bb and not pending_trade_args:
                if try_trade("VWAPBand", vwap_bb["type"], False,
                             f"VWAP band {vwap_bb['type']} at {vwap_bb['level']:.0f}",
                             "vwap_band"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 5. VWAP Cross (2-candle + volume confirmed)
            if vwap_cx and not pending_trade_args:
                if try_trade("VWAPCross", vwap_cx["type"], False,
                             f"VWAP cross {vwap_cx['type']} at {vwap_cx['vwap']:.0f}",
                             "vwap_cross"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 6. EMA50 Bounce (candle confirmed)
            if ema50_b and not pending_trade_args:
                if try_trade("EMA50Bounce", ema50_b["type"], False,
                             f"EMA50 bounce {ema50_b['type']} at {ema50_b['e50']:.0f}",
                             "ema50"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 7. EMA Cross (fresh crossover)
            if ema_cx and not pending_trade_args:
                if try_trade("EMACross", ema_cx["type"], False,
                             f"EMA cross {ema_cx['type']} E9:{ema_cx['e9']:.0f}",
                             "ema_cross"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 8. SuperTrend flip
            if st_sig and not pending_trade_args:
                if try_trade("SuperTrend", st_sig["type"], True,
                             f"SuperTrend flip {st_sig['type']} at {st_sig['level']:.0f}",
                             "supertrend"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 9. CPR breakout/breakdown
            if cpr_sig and not pending_trade_args:
                if try_trade("CPR", cpr_sig["type"], False,
                             f"CPR {cpr_sig['type']} {cpr_r[:40]}",
                             "cpr"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 10. [V6-F8] BOS — Break of Structure
            bos_sig, bos_r = detect_bos(df_5, ltp)
            if bos_sig and not pending_trade_args:
                if try_trade("BOS", bos_sig["type"], True,
                             f"BOS {bos_r}",
                             "bos"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 11. [V6-F9] ORPH/ORPL — Previous High/Low breakout on gap days
            orph_sig, orph_r = detect_orph_orpl(ltp, prev_ohlc, trend, prev_ltp, gap_pct=chg_pct)
            if orph_sig and not pending_trade_args:
                if try_trade("ORPH_ORPL", orph_sig["type"], False,
                             f"ORPH_ORPL {orph_r}",
                             "orph_orpl"):
                    prev_ltp = ltp; time.sleep(15); continue

            # 12. [V6-F10] RSI Divergence — early reversal
            # Build RSI series from df_5 for divergence check
            try:
                rsi_series_full = None
                if df_5 is not None and len(df_5) >= RSI_DIV_LOOKBACK + 2:
                    delta  = df_5["close"].astype(float).diff()
                    gain   = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
                    loss   = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
                    rs     = gain / loss.replace(0, np.nan)
                    rsi_full = (100 - (100 / (1 + rs))).values
                    rsi_series_full = rsi_full[-RSI_DIV_LOOKBACK:]
            except: rsi_series_full = None

            rsi_div_sig, rsi_div_r = detect_rsi_divergence(df_5, rsi_series_full)
            if rsi_div_sig and not pending_trade_args:
                if try_trade("RSIDivergence", rsi_div_sig["type"], False,
                             f"RSI Div {rsi_div_r}",
                             "rsi_divergence"):
                    prev_ltp = ltp; time.sleep(15); continue

            prev_ltp = ltp
            time.sleep(60)

        except KeyboardInterrupt:
            raise
        except Exception as _loop_err:
            log.error(f"Loop error (recovering): {_loop_err}", exc_info=True)
            send_telegram(f"⚠️ Loop error: {_loop_err}")
            try: save_state(stats, tg_listener.bias)
            except Exception: pass
            time.sleep(15)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Nifty Bot v9 stopped by user")
        send_telegram("⏹ Nifty Bot v9 stopped (KeyboardInterrupt)")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        send_telegram(f"🚨 Nifty Bot v9 CRASHED: {e}")
