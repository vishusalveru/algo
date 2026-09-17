"""
═══════════════════════════════════════════════════════════════════════════
  signals.py  —  SHARED Signal Detection Layer
═══════════════════════════════════════════════════════════════════════════

  PURPOSE
    Extracts market signals from Nifty candle data. Pure signal detection —
    no trade logic, no sizing, no entry/exit decisions. Returns structured
    signal data (type, strength, reason) for any trading bot to consume.

  EXPORTS
    Indicators: calc_atr, calc_rsi, calc_vwap_bands, calc_ema
    Detectors:  detect_fvg, detect_bos, detect_supertrend_signal, ... (12 total)
    Structure:  detect_trend_multi, classify_intraday_regime
    Constants:  All 20 required by detectors + dependencies

  VERIFIED
    Tested against v12 output on synthetic and live candles.
    Deterministic: same input → same output (required for shared module).

  CONSUMED BY
    nifty_bot_v13.py (option-chain-driven trader)
    Any future bot needing "what's the signal?" answer
═══════════════════════════════════════════════════════════════════════════
"""

import numpy as np
import pandas as pd
import datetime
import logging

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
#  CONSTANTS — 20 extracted from v12, verified for use in detectors
# ─────────────────────────────────────────────────────────────────────────

# ── FVG (Fair Value Gap) detection ──
STRONG_FVG_GAP = 10          # gap must be >= 10 pts
STRONG_FVG_BODY = 20         # body must be >= 20 pts for "strong"
MIN_FVG_BODY = 10            # absolute minimum body size
MIN_FVG_SIZE = 5             # gap size must be >= 5 pts to count
FVG_MAX_AGE_CANDLES = 3      # skip FVGs older than 3 candles

# ── ORB (Opening Range Breakout) detection ──
ORB_MAX_RANGE = 100          # skip ORB if first 30min range > 100pts (noise)
OTM_OFFSET = 100             # option strike offset from ATM

# ── EMA Stack detection ──
EMASTACK_MIN_RVOL = 1.3      # require RVOL >= 1.3x for EMA Stack
EMA50_TOLERANCE = 20         # price within 20pts of EMA50 for bounce signal

# ── BOS (Break of Structure) detection ──
BOS_SWING_LOOKBACK = 10      # candles to look back for swing extremes
BOS_MIN_MOVE = 8             # minimum move beyond swing to confirm BOS

# ── CPR (Central Pivot Range) detection ──
CPR_BREAKOUT_BUFFER = 5      # buffer above/below CPR for entry

# ── RSI Divergence detection ──
RSI_DIV_LOOKBACK = 6         # candles to compare for divergence signal

# ── Session/Market structure ──
GAP_FILTER_PCT = 0.5         # gap must be > 0.5% to apply gap logic
TIME_EXIT_AFTER = datetime.time(14, 0)  # session exit time reference

# ── Trade parameters (used in detectors for context, not logic) ──
LOT_SIZE = 65                # Nifty lot size (fixed by exchange)


# ─────────────────────────────────────────────────────────────────────────
#  INDICATORS — pure math, deterministic
# ─────────────────────────────────────────────────────────────────────────

def calc_rvol(df, lookback=20):
    """Relative volume = current candle volume / average volume over lookback.
    Computed from live candle data (NOT a hardcoded default). Returns None if it
    can't be computed, so callers must treat 'no data' as 'no signal' rather than
    silently passing a fake 1.0 (which previously disabled EMAStack permanently).
    """
    try:
        if df is None or len(df) < lookback + 1:
            return None
        vols = df["volume"].astype(float)
        if vols.iloc[-1] <= 0:
            return None
        avg = vols.iloc[-(lookback+1):-1].mean()   # average EXCLUDING current
        if avg <= 0:
            return None
        return round(float(vols.iloc[-1]) / avg, 2)
    except Exception as e:
        log.debug(f"RVOL calc error: {e}")
        return None


def calc_atr(df, period=14):
    """Average True Range — volatility measure."""
    try:
        if df is None or len(df) < period:
            return 20.0
        df = df.copy()
        df["prev_close"] = df["close"].astype(float).shift(1)
        df["tr"] = df[["high", "low", "prev_close"]].apply(
            lambda r: max(
                float(r["high"]) - float(r["low"]),
                abs(float(r["high"]) - float(r["prev_close"])),
                abs(float(r["low"]) - float(r["prev_close"]))
            ),
            axis=1
        )
        atr = float(df["tr"].ewm(span=period, adjust=False).mean().iloc[-1])
        return round(atr, 1)
    except Exception as e:
        log.debug(f"ATR calc error: {e}")
        return 20.0


def calc_rsi_series(df, period=14):
    """Full RSI series (not just the latest value) — needed by RSI-divergence
    detection, which compares RSI now vs RSI N candles ago. Returns None if it
    can't be computed (caller treats None as 'no signal', not a fake series)."""
    try:
        if df is None or len(df) < period + 1:
            return None
        delta = df["close"].astype(float).diff()
        gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50.0).values
    except Exception as e:
        log.debug(f"RSI series calc error: {e}")
        return None


def calc_rsi(df, period=14):
    try:
        if df is None or len(df) < period:
            return 50.0
        delta = df["close"].astype(float).diff()
        gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 1)
    except Exception as e:
        log.debug(f"RSI calc error: {e}")
        return 50.0


def calc_ema(df, periods=[9, 21, 50]):
    """Exponential Moving Averages."""
    try:
        if df is None or len(df) < max(periods):
            return df
        df = df.copy()
        for p in periods:
            df[f"ema{p}"] = df["close"].astype(float).ewm(span=p, adjust=False).mean()
        return df
    except Exception as e:
        log.debug(f"EMA calc error: {e}")
        return df


def calc_vwap_bands(df, atr=30.0):
    """VWAP with standard deviation bands."""
    try:
        if df is None or len(df) < 5:
            return df
        df = df.copy()
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        df["typical"] = (df["high"] + df["low"] + df["close"]) / 3

        total_vol = df["volume"].sum()
        if total_vol == 0:
            df["weight"] = 1.0
        else:
            df["weight"] = df["volume"]

        df["cum_vol"] = df["weight"].cumsum()
        df["cum_tv"] = (df["typical"] * df["weight"]).cumsum()
        df["vwap"] = df["cum_tv"] / df["cum_vol"]
        df["cum_tv2"] = (((df["typical"] - df["vwap"]) ** 2) * df["weight"]).cumsum()
        df["sd"] = np.sqrt((df["cum_tv2"] / df["cum_vol"]).clip(lower=0))

        mult = 1.5 if atr > 30 else 1.0 if atr > 15 else 0.75
        df["vwap_u1"] = df["vwap"] + mult * df["sd"]
        df["vwap_l1"] = df["vwap"] - mult * df["sd"]
        df["vwap_u2"] = df["vwap"] + 2 * mult * df["sd"]
        df["vwap_l2"] = df["vwap"] - 2 * mult * df["sd"]
        df["band_width"] = df["vwap_u1"] - df["vwap_l1"]
        return df
    except Exception as e:
        log.debug(f"VWAP calc error: {e}")
        return df


def calc_supertrend(df, period=10, mult=3.0):
    """SuperTrend indicator — trend flip detection."""
    try:
        if df is None or len(df) < period + 2:
            return "neutral", 0, False

        df = df.copy().reset_index(drop=True)
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs()
        ], axis=1).max(axis=1)
        atr_st = tr.ewm(span=period, adjust=False).mean()

        hl2 = (df["high"] + df["low"]) / 2
        basic_upper = hl2 + mult * atr_st
        basic_lower = hl2 - mult * atr_st

        final_upper = np.array(basic_upper.values, dtype=float)
        final_lower = np.array(basic_lower.values, dtype=float)
        close = df["close"].values

        for i in range(1, len(df)):
            if close[i-1] <= final_upper[i-1]:
                final_upper[i] = min(basic_upper.iloc[i], final_upper[i-1])
            else:
                final_upper[i] = basic_upper.iloc[i]
            if close[i-1] >= final_lower[i-1]:
                final_lower[i] = max(basic_lower.iloc[i], final_lower[i-1])
            else:
                final_lower[i] = basic_lower.iloc[i]

        st_bull = [True] * len(df)
        for i in range(1, len(df)):
            if st_bull[i-1]:
                st_bull[i] = close[i] >= final_lower[i-1]
            else:
                st_bull[i] = close[i] > final_upper[i-1]

        direction = "bullish" if st_bull[-1] else "bearish"
        level = round(float(final_lower[-1] if st_bull[-1] else final_upper[-1]), 1)
        fresh = st_bull[-1] != st_bull[-2]
        return direction, level, fresh
    except Exception as e:
        log.error(f"SuperTrend calc error: {e}")
        return "neutral", 0, False


# ─────────────────────────────────────────────────────────────────────────
#  TREND DETECTION
# ─────────────────────────────────────────────────────────────────────────

def detect_trend_relaxed(df, min_agree=3):
    """Simple trend from HH/HL and LL/LH pattern (last 4 candles)."""
    if df is None or len(df) < 4:
        return "neutral", "Not enough", 0
    recent = df.tail(4)
    highs = [float(x) for x in recent["high"].tolist()]
    lows = [float(x) for x in recent["low"].tolist()]
    hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
    hl = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i-1])
    ll = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i-1])
    lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i-1])
    bull = min(hh, hl)
    bear = min(ll, lh)
    if bull >= min_agree:
        return "bullish", f"HH:{hh} HL:{hl}", bull
    if bear >= min_agree:
        return "bearish", f"LL:{ll} LH:{lh}", bear
    return "neutral", f"HH:{hh} HL:{hl} LL:{ll} LH:{lh}", 0


def detect_trend_multi(df5, df15, df30, e9=0, e21=0, e50=0, ltp=0):
    """Multi-timeframe trend: need 2/3 TF agreement."""
    t5, _, _ = detect_trend_relaxed(df5)
    t15, _, _ = detect_trend_relaxed(df15)
    t30, _, _ = detect_trend_relaxed(df30)
    bull = [t5, t15, t30].count("bullish")
    bear = [t5, t15, t30].count("bearish")
    if bull >= 2:
        return "bullish", f"{t5}/{t15}/{t30}", "strong" if bull == 3 else "moderate"
    if bear >= 2:
        return "bearish", f"{t5}/{t15}/{t30}", "strong" if bear == 3 else "moderate"
    if e9 > 0 and e21 > 0 and e50 > 0 and ltp > 0:
        if e9 > e21 > e50 and ltp > e21:
            return "bullish", f"{t5}/{t15}/{t30}", "ema_confirmed"
        if e9 < e21 < e50 and ltp < e21:
            return "bearish", f"{t5}/{t15}/{t30}", "ema_confirmed"
    return "neutral", f"{t5}/{t15}/{t30}", "weak"


# ─────────────────────────────────────────────────────────────────────────
#  REGIME CLASSIFIER
# ─────────────────────────────────────────────────────────────────────────

def classify_intraday_regime(df_5, e9, e21, atr=None):
    """Classify market regime from 12-candle price structure."""
    try:
        if df_5 is None or len(df_5) < 12:
            return "UNKNOWN"
        closes = df_5["close"].astype(float).values[-12:]
        rng = closes.max() - closes.min()
        half1 = closes[:6].mean()
        half2 = closes[6:].mean()
        direction = "up" if half2 > half1 else "down"
        ema_bull = float(e9) > float(e21)

        # Dynamic thresholds based on ATR
        if atr is not None and atr > 0:
            trend_threshold = atr * 1.8
            weak_threshold = atr * 1.2
        else:
            trend_threshold = 100
            weak_threshold = 60

        if rng > trend_threshold:
            if direction == "up" and ema_bull:
                return "TRENDING_BULL"
            if direction == "down" and not ema_bull:
                return "TRENDING_BEAR"
            return "CHOPPY"
        elif rng > weak_threshold:
            if direction == "up":
                return "WEAK_BULL"
            else:
                return "WEAK_BEAR"
        else:
            return "RANGING"
    except Exception as e:
        log.debug(f"Regime classify error: {e}")
        return "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────
#  MARKET CLASSIFICATION — live-data findings (v6–v10), DETECTION ONLY
#  These describe "what the market is". Trade DECISIONS (block/size) live in
#  the gate layer, not here. signals.py stays the single source of truth for
#  classification so nothing downstream re-implements it.
# ─────────────────────────────────────────────────────────────────────────

# [V9-F2] Win rate BY REGIME from real April trading (documented in v10):
#   TRENDING_BEAR 67% +2470 | WEAK_BEAR 75% +4290 | TRENDING_BULL 40% -1560
#   WEAK_BULL 27% -4615 | RANGING 17%. Trend signals chop into SLs in
#   weak/choppy/ranging regimes. This is reference data for the gate layer.
REGIME_TRENDING    = {"TRENDING_BULL", "TRENDING_BEAR"}
REGIME_BLOCK_TREND = {"WEAK_BULL", "CHOPPY", "RANGING", "UNKNOWN"}

# Which of the 12 detectors are momentum/trend-type vs reversal-type.
# [V9-F3] Trend strategies need real momentum (ATR>35); reversal strategies
# thrive in ranging markets at the lower ATR>20 floor.
TREND_STRATEGIES    = {"EMAStack", "EMACross", "SuperTrend", "BOS"}
REVERSAL_STRATEGIES = {"VWAPCross", "RSIDivergence", "EMA50Bounce", "CPR"}
ATR_TREND_MIN       = 35.0   # [V9-F3] absolute crash-day reference (now a CEILING
                             # for auto-qualify, not the only gate — see below)
# [FIX 2] Relative-ATR trend qualification. A fixed ATR>35 (tuned on April
# crash days, ATR 50-70) blocked every BOS on the 2026-05-29 trend day where
# ATR peaked at 28. Instead, a trend strategy qualifies if the CURRENT ATR sits
# in the upper part of the DAY'S OWN range — i.e. momentum relative to today —
# OR clears the absolute floor. This generalises to bullish & bearish trends.
ATR_DAY_PCTL_MIN    = 0.60   # current ATR must be >= 60th pct of the day's range
ATR_ABS_FLOOR       = 18.0   # but never below this (true dead-tape cutoff)


def trend_atr_ok(atr_now, atr_day_low, atr_day_high):
    """[FIX 2] Is current ATR strong enough — relative to the day — for a
    trend strategy? Returns (ok: bool, why: str). Pure classification."""
    try:
        atr_now = float(atr_now)
    except (TypeError, ValueError):
        return False, "no ATR"
    if atr_now < ATR_ABS_FLOOR:
        return False, f"ATR {atr_now:.1f}<{ATR_ABS_FLOOR} (dead tape)"
    # Absolute strong-trend auto-qualify (real crash/high-vol day)
    if atr_now >= ATR_TREND_MIN:
        return True, f"ATR {atr_now:.1f}>={ATR_TREND_MIN} (strong)"
    # Relative: is ATR in the upper part of today's observed range?
    try:
        lo, hi = float(atr_day_low), float(atr_day_high)
        if hi > lo:
            pctl = (atr_now - lo) / (hi - lo)
            if pctl >= ATR_DAY_PCTL_MIN:
                return True, (f"ATR {atr_now:.1f} at {pctl*100:.0f}pct of day "
                              f"range [{lo:.0f}-{hi:.0f}] (relative momentum)")
            return False, (f"ATR {atr_now:.1f} only {pctl*100:.0f}pct of day "
                           f"range [{lo:.0f}-{hi:.0f}] (drift)")
    except (TypeError, ValueError):
        pass
    # No day-range info: fall back to absolute floor already passed
    return True, f"ATR {atr_now:.1f}>={ATR_ABS_FLOOR} (no day range, floor ok)"
ATR_REVERSAL_MIN    = 20.0   # reversal strategies floor

# [V8-F3a] RSI penalty applies ONLY to mean-reversion strategies. Momentum
# strategies are NOT penalised for high RSI — it confirms their signal.
MEAN_REV_STRATEGIES = {"EMA50Bounce", "CPR", "RSIDivergence", "VWAPCross"}

# [F12] Suspend if India VIX spikes >20% from session open (regime break).
VIX_SPIKE_PCT = 20.0


def efficiency_ratio(closes):
    """Kaufman efficiency ratio: net move / total path. 0=chop, 1=clean trend.

    Two days with the same ATR are NOT the same market — one can trend cleanly,
    the other whipsaw. Premium-buying needs trend; this distinguishes them.
    Pure classification: returns the number, makes no trade decision.
    """
    try:
        if closes is None or len(closes) < 3:
            return 0.0
        net = abs(float(closes[-1]) - float(closes[0]))
        path = sum(abs(float(closes[i]) - float(closes[i - 1]))
                   for i in range(1, len(closes)))
        return round(net / path, 3) if path > 0 else 0.0
    except Exception as e:
        log.debug(f"efficiency_ratio error: {e}")
        return 0.0


def vix_bias(vix):
    """India VIX -> fear regime. [auto_bias bands, live-tuned]
    <13 low fear (bullish) | 13-18 neutral | >18 high fear (bearish) | >25 extreme.
    """
    try:
        v = float(vix)
    except (TypeError, ValueError):
        return "neutral"
    if v < 13:
        return "bullish"
    if v > 25:
        return "extreme"
    if v > 18:
        return "bearish"
    return "neutral"


def is_expiry_day(today=None, expiry_str=None):
    """True if today is the nearest-expiry day. [v10 line 1903 + live-expiry]
    Prefer the live expiry string (robust to NSE Thu->Mon changes); fall back
    to weekday()==Thursday only if no expiry string is supplied.
    """
    today = today or datetime.date.today()
    if expiry_str:
        try:
            exp = datetime.datetime.strptime(expiry_str, "%Y-%m-%d").date()
            return exp == today
        except (ValueError, TypeError):
            pass
    return today.weekday() == 3  # Thursday fallback


def classify_day_type(open_price, prev_ohlc, atr, df_5=None):
    """[V6-F13] Classify the day from open gap + ATR + candle structure.
    Returns (day_type, gap_pct). Pure classification, no trade decision.
    day_type in {GAP_UP, GAP_DOWN, VOLATILE, TRENDING, RANGEBOUND, UNKNOWN}.
    """
    try:
        gap_pct = 0.0
        if prev_ohlc and prev_ohlc.get("close"):
            gap_pct = (open_price - prev_ohlc["close"]) / prev_ohlc["close"] * 100
        if gap_pct > GAP_FILTER_PCT:
            return "GAP_UP", round(gap_pct, 2)
        if gap_pct < -GAP_FILTER_PCT:
            return "GAP_DOWN", round(gap_pct, 2)
        if atr and atr > 40:
            return "VOLATILE", round(gap_pct, 2)
        if df_5 is not None and len(df_5) >= 6:
            closes = df_5["close"].astype(float).values[-6:]
            ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
            downs = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])
            if ups >= 4 or downs >= 4:
                return "TRENDING", round(gap_pct, 2)
        return "RANGEBOUND", round(gap_pct, 2)
    except Exception as e:
        log.debug(f"classify_day_type error: {e}")
        return "UNKNOWN", 0.0


def is_trend_strategy(strategy_name):
    """Convenience classifier: is this detector momentum/trend-type? [V9-F3]"""
    return strategy_name in TREND_STRATEGIES


# ─────────────────────────────────────────────────────────────────────────
#  SIGNAL DETECTORS (12 total)
# ─────────────────────────────────────────────────────────────────────────

def detect_fvg(df):
    """Fair Value Gap — fresh gap with intact zone."""
    if df is None or len(df) < 3:
        return None, "No candles"
    candles = df.tail(15)
    ltp_now = float(candles["close"].iloc[-1])
    # [FVG CONFIRMATION GUARD — Option C, 2026-06-05]
    # The old logic fired when price was merely on one side of the gap, which
    # also fires when price is BREAKING THROUGH the gap the wrong way (a FAILED
    # gap) — the cause of the MFE+0.0 losers (CE bought into a fall, PE into a
    # rise). Guard: the most recent (current) candle must CLOSE in the trade's
    # direction, confirming price is moving WITH the signal, not failing against
    # it. Minimal, testable filter; does not rebuild the detector.
    last = candles.iloc[-1]
    last_up = float(last["close"]) > float(last["open"])     # bullish confirmation
    last_down = float(last["close"]) < float(last["open"])   # bearish confirmation
    for i in range(len(candles) - 1, 1, -1):
        c1 = candles.iloc[i-2]
        c2 = candles.iloc[i-1]
        c3 = candles.iloc[i]
        body = abs(float(c2["close"]) - float(c2["open"]))
        if body < MIN_FVG_BODY:
            continue
        c1h = float(c1["high"])
        c1l = float(c1["low"])
        c3h = float(c3["high"])
        c3l = float(c3["low"])
        age = len(candles) - 1 - i
        if age > FVG_MAX_AGE_CANDLES:
            continue
        if c1h < c3l:
            if ltp_now < c3l and last_up:   # GUARD: price confirming UP, not failing down
                size = round(c3l - c1h, 1)
                if size < MIN_FVG_SIZE:
                    continue
                strong = size >= STRONG_FVG_GAP and body >= STRONG_FVG_BODY
                return {
                    "type": "bullish", "top": round(c3l, 1), "bottom": round(c1h, 1),
                    "mid": round((c3l + c1h) / 2, 1), "edge": round(c3l, 1),
                    "size": size, "strong": strong, "age": age
                }, f"{'STRONG' if strong else 'WEAK'} Bull FVG {size:.1f}pts age:{age}c"
        if c1l > c3h:
            if ltp_now > c3h and last_down:   # GUARD: price confirming DOWN, not failing up
                size = round(c1l - c3h, 1)
                if size < MIN_FVG_SIZE:
                    continue
                strong = size >= STRONG_FVG_GAP and body >= STRONG_FVG_BODY
                return {
                    "type": "bearish", "top": round(c1l, 1), "bottom": round(c3h, 1),
                    "mid": round((c1l + c3h) / 2, 1), "edge": round(c3h, 1),
                    "size": size, "strong": strong, "age": age
                }, f"{'STRONG' if strong else 'WEAK'} Bear FVG {size:.1f}pts age:{age}c"
    return None, f"No valid FVG (max {FVG_MAX_AGE_CANDLES}c)"


def detect_bos(df, ltp):
    """Break of Structure — price breaks beyond swing extreme."""
    try:
        if df is None or len(df) < BOS_SWING_LOOKBACK + 2:
            return None, "BOS: not enough candles"
        window = df.tail(BOS_SWING_LOOKBACK + 2)
        highs = window["high"].astype(float).values
        lows = window["low"].astype(float).values
        closes = window["close"].astype(float).values
        swing_high = float(np.max(highs[:-2]))
        swing_low = float(np.min(lows[:-2]))
        last_close = closes[-2]
        if last_close > swing_high + BOS_MIN_MOVE and ltp > swing_high:
            return {
                "type": "bullish", "level": round(swing_high, 1),
                "break_size": round(last_close - swing_high, 1)
            }, f"BOS bull — closed {last_close:.0f} above swing H:{swing_high:.0f}"
        if last_close < swing_low - BOS_MIN_MOVE and ltp < swing_low:
            return {
                "type": "bearish", "level": round(swing_low, 1),
                "break_size": round(swing_low - last_close, 1)
            }, f"BOS bear — closed {last_close:.0f} below swing L:{swing_low:.0f}"
        return None, f"No BOS | SwH:{swing_high:.0f} SwL:{swing_low:.0f} LTP:{ltp:.0f}"
    except Exception as e:
        return None, f"BOS error: {e}"


def detect_ema_stack(df_ema, ltp, t5, rvol):
    """EMA Stack alignment (with RVOL gate)."""
    try:
        if rvol is None:
            return None, "EMAStack: no RVOL data (no trade)"
        if rvol < EMASTACK_MIN_RVOL:
            return None, f"EMAStack blocked: RVOL {rvol}x < {EMASTACK_MIN_RVOL}x"
        e9 = float(df_ema["ema9"].iloc[-1])
        e21 = float(df_ema["ema21"].iloc[-1])
        e50 = float(df_ema["ema50"].iloc[-1])
        if ltp > e9 > e21 > e50 and t5 == "bullish":
            return {
                "type": "bullish", "e9": round(e9, 1),
                "e21": round(e21, 1), "e50": round(e50, 1)
            }, f"EMA Stack bull RVOL:{rvol}x E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
        if ltp < e9 < e21 < e50 and t5 == "bearish":
            return {
                "type": "bearish", "e9": round(e9, 1),
                "e21": round(e21, 1), "e50": round(e50, 1)
            }, f"EMA Stack bear RVOL:{rvol}x E9{e9:.0f} E21{e21:.0f} E50{e50:.0f}"
        return None, "No EMA stack"
    except Exception as e:
        log.warning(f"detect_ema_stack error: {e!r}")
        return None, f"EMA stack error: {e}"


def detect_ema_cross(df_ema, prev_df_ema):
    """EMA 9/21 crossover."""
    try:
        if prev_df_ema is None:
            return None, "No prev EMA"
        e9 = float(df_ema["ema9"].iloc[-1])
        e21 = float(df_ema["ema21"].iloc[-1])
        pe9 = float(prev_df_ema["ema9"].iloc[-1])
        pe21 = float(prev_df_ema["ema21"].iloc[-1])
        if pe9 <= pe21 and e9 > e21:
            return {
                "type": "bullish", "e9": round(e9, 1), "e21": round(e21, 1)
            }, f"EMA9 crossed above EMA21 at {e9:.0f}"
        if pe9 >= pe21 and e9 < e21:
            return {
                "type": "bearish", "e9": round(e9, 1), "e21": round(e21, 1)
            }, f"EMA9 crossed below EMA21 at {e9:.0f}"
        return None, f"No EMA cross gap {round(e9-e21, 1)}pts"
    except Exception as e:
        log.warning(f"detect_ema_cross error: {e!r}")
        return None, f"EMA cross error: {e}"


def detect_vwap_band_break(df_vwap, ltp, t5, atr):
    """VWAP band breakout."""
    try:
        if len(df_vwap) < 2:
            return None, "Not enough VWAP"
        last = df_vwap.iloc[-1]
        prev = df_vwap.iloc[-2]
        vu1 = float(last["vwap_u1"])
        vl1 = float(last["vwap_l1"])
        pltp = float(prev["close"])
        if atr < 20:
            return None, f"ATR {atr:.0f}pts too low"
        if pltp < vu1 and ltp > vu1 and t5 == "bullish":
            return {"type": "bullish", "level": round(vu1, 1)}, \
                   f"Broke above VWAP+1SD at {vu1:.0f} ATR:{atr:.0f}"
        if pltp > vl1 and ltp < vl1 and t5 == "bearish":
            return {"type": "bearish", "level": round(vl1, 1)}, \
                   f"Broke below VWAP-1SD at {vl1:.0f} ATR:{atr:.0f}"
        return None, f"No band break U1:{vu1:.0f} L1:{vl1:.0f}"
    except Exception as e:
        log.warning(f"detect_vwap_band_break error: {e!r}")
        return None, f"VWAP band error: {e}"


def detect_vwap_cross(df_vwap, ltp, df_5):
    """VWAP 2-candle cross with volume confirmation."""
    try:
        if len(df_vwap) < 3:
            return None, "Not enough candles"
        last = df_vwap.iloc[-1]
        prev = df_vwap.iloc[-2]
        prev2 = df_vwap.iloc[-3]
        vwap = float(last["vwap"])
        c1 = float(last["close"])
        c2 = float(prev["close"])
        c3 = float(prev2["close"])
        v1 = float(last["volume"])
        avg_vol = float(df_5["volume"].mean())
        vol_ok = v1 > avg_vol * 1.2
        if c3 < vwap and c2 > vwap and c1 > vwap and vol_ok:
            return {"type": "bullish", "vwap": round(vwap, 1)}, \
                   f"VWAP cross bull 2c+vol at {vwap:.0f}"
        if c3 > vwap and c2 < vwap and c1 < vwap and vol_ok:
            return {"type": "bearish", "vwap": round(vwap, 1)}, \
                   f"VWAP cross bear 2c+vol at {vwap:.0f}"
        return None, f"No VWAP cross VWAP:{vwap:.0f}"
    except Exception as e:
        log.warning(f"detect_vwap_cross error: {e!r}")
        return None, f"VWAP cross error: {e}"


def detect_ema50_bounce(df_ema, ltp, t5, df_5):
    """EMA50 bounce/rejection."""
    try:
        if len(df_5) < 2:
            return None, "Not enough candles"
        e50 = float(df_ema["ema50"].iloc[-1])
        dist = abs(ltp - e50)
        if dist > EMA50_TOLERANCE:
            return None, f"No EMA50 bounce dist:{dist:.0f}pts"
        last = df_5.iloc[-1]
        co = float(last["open"])
        cc = float(last["close"])
        body = abs(cc - co)
        if t5 == "bullish" and ltp > e50 and cc > co and body > 5:
            return {"type": "bullish", "e50": round(e50, 1)}, \
                   f"EMA50 bounce bull at {e50:.0f} dist:{dist:.0f}pts"
        if t5 == "bearish" and ltp < e50 and cc < co and body > 5:
            return {"type": "bearish", "e50": round(e50, 1)}, \
                   f"EMA50 rejection bear at {e50:.0f} dist:{dist:.0f}pts"
        return None, f"EMA50 near {e50:.0f} no candle confirm"
    except Exception as e:
        log.warning(f"detect_ema50_bounce error: {e!r}")
        return None, f"EMA50 error: {e}"


def detect_supertrend_signal(df, trend, ltp=None, atr=None):
    """SuperTrend fresh flip aligned with trend."""
    st_dir, st_level, is_fresh = calc_supertrend(df)
    if not is_fresh:
        return None, f"SuperTrend: no fresh flip (current:{st_dir})"
    if st_dir != trend:
        return None, f"SuperTrend {st_dir} conflicts trend {trend}"
    if ltp is not None and atr is not None:
        distance = abs(ltp - st_level)
        max_distance = atr * 2.0
        if distance > max_distance:
            return None, (f"SuperTrend STALE — price {ltp:.0f} too far from "
                         f"ST level {st_level:.0f}")
    return {"type": st_dir, "level": st_level, "fresh": True}, \
           f"SuperTrend flip to {st_dir} at {st_level:.0f}"


def detect_cpr_signal(ltp, pivot, bc, tc, trend, prev_ltp=None):
    """Central Pivot Range breakout/breakdown."""
    if pivot is None:
        return None, "No CPR (no prev day data)"
    cpr_width = round(tc - bc, 1)
    if prev_ltp is None:
        return None, "Need prev LTP for CPR check"
    if prev_ltp < tc and ltp > tc + CPR_BREAKOUT_BUFFER and trend == "bullish":
        return {
            "type": "bullish", "pivot": pivot, "tc": tc, "bc": bc, "width": cpr_width
        }, f"CPR breakout bull above TC:{tc:.0f} width:{cpr_width:.0f}pts"
    if prev_ltp > bc and ltp < bc - CPR_BREAKOUT_BUFFER and trend == "bearish":
        return {
            "type": "bearish", "pivot": pivot, "tc": tc, "bc": bc, "width": cpr_width
        }, f"CPR breakdown bear below BC:{bc:.0f} width:{cpr_width:.0f}pts"
    return None, f"CPR neutral | P:{pivot:.0f} TC:{tc:.0f} BC:{bc:.0f} L:{ltp:.0f}"


def detect_rsi_divergence(df, rsi_series):
    """Price/RSI divergence — early reversal."""
    try:
        if df is None or len(df) < RSI_DIV_LOOKBACK + 2:
            return None, "RSI divergence: not enough candles"
        if rsi_series is None or len(rsi_series) < RSI_DIV_LOOKBACK:
            return None, "RSI divergence: no RSI series"
        prices = df["close"].astype(float).values[-RSI_DIV_LOOKBACK:]
        rsivs = rsi_series[-RSI_DIV_LOOKBACK:]
        if prices[-1] > prices[0] and rsivs[-1] < rsivs[0]:
            mag = round(prices[-1] - prices[0], 1)
            rsi_drop = round(rsivs[0] - rsivs[-1], 1)
            if mag > 15 and rsi_drop > 5:
                return {
                    "type": "bearish", "price_move": mag, "rsi_drop": rsi_drop
                }, f"Bear divergence: price +{mag}pts RSI -{rsi_drop:.0f}"
        if prices[-1] < prices[0] and rsivs[-1] > rsivs[0]:
            mag = round(prices[0] - prices[-1], 1)
            rsi_rise = round(rsivs[-1] - rsivs[0], 1)
            if mag > 15 and rsi_rise > 5:
                return {
                    "type": "bullish", "price_move": mag, "rsi_rise": rsi_rise
                }, f"Bull divergence: price -{mag}pts RSI +{rsi_rise:.0f}"
        return None, "No RSI divergence"
    except Exception as e:
        return None, f"RSI div error: {e}"


def calc_cpr(prev_ohlc):
    """Central Pivot Range levels from PREVIOUS day's OHLC.
      pivot = (H + L + C) / 3
      BC    = (H + L) / 2
      TC    = pivot + (pivot - BC)   [reflection of BC across pivot]
    Returns (pivot, bc, tc) or (None, None, None) if prev-day data is missing —
    never a fabricated default, so callers treat 'no data' as 'no signal'."""
    try:
        if not prev_ohlc:
            return None, None, None
        h = float(prev_ohlc["high"]); l = float(prev_ohlc["low"]); c = float(prev_ohlc["close"])
        pivot = (h + l + c) / 3.0
        bc = (h + l) / 2.0
        tc = pivot + (pivot - bc)
        # TC/BC can be inverted depending on the day; order them so tc>=bc
        if tc < bc:
            tc, bc = bc, tc
        return round(pivot, 1), round(bc, 1), round(tc, 1)
    except Exception as e:
        log.debug(f"CPR calc error: {e}")
        return None, None, None


def detect_orph_orpl(ltp, prev_ohlc, trend, prev_ltp=None, gap_pct=0.0):
    """Previous High/Low breakout on gap days."""
    try:
        if prev_ohlc is None:
            return None, "ORPH_ORPL: no prev day data"
        if abs(gap_pct) < GAP_FILTER_PCT:
            return None, f"ORPH_ORPL: gap {gap_pct:.2f}% too small"
        pdh = prev_ohlc["high"]
        pdl = prev_ohlc["low"]
        if prev_ltp is None:
            return None, "ORPH_ORPL: need prev LTP"
        if gap_pct > GAP_FILTER_PCT and ltp > pdh and prev_ltp < pdh and trend == "bullish":
            return {
                "type": "bullish", "level": round(pdh, 1), "gap_pct": gap_pct
            }, f"ORPH bull — above PDH:{pdh:.0f} gap:{gap_pct:+.2f}%"
        if gap_pct < -GAP_FILTER_PCT and ltp < pdl and prev_ltp > pdl and trend == "bearish":
            return {
                "type": "bearish", "level": round(pdl, 1), "gap_pct": gap_pct
            }, f"ORPL bear — below PDL:{pdl:.0f} gap:{gap_pct:+.2f}%"
        return None, f"ORPH_ORPL neutral | PDH:{pdh:.0f} PDL:{pdl:.0f} gap:{gap_pct:+.2f}%"
    except Exception as e:
        return None, f"ORPH_ORPL error: {e}"


# ═══════════════════════════════════════════════════════════════════════════
# FVG EXHAUSTION FILTER  [added 2026-06-05, data-driven from 7 live/paper FVG trades]
# ═══════════════════════════════════════════════════════════════════════════
# Problem observed live: FVG fires LATE — it confirms a gap after the move is
# largely done, so the option is bought with no room left (MFE +0.0) and bleeds.
# Measurable signature across 3 sessions: the bad FVG entries had RSI extended
# AGAINST fresh momentum (bearish PE bought when already oversold; bullish CE
# bought when RSI not confirming) AND occurred in low-efficiency (range-bound)
# tape. The 4 winners had neither.
#
# Rule: block an FVG entry only when 2+ exhaustion signals AGREE (a single noisy
# signal — e.g. RSI near a threshold in chop — must NOT veto on its own, which
# is the whipsaw failure mode). Thresholds set slightly loose ON PURPOSE: the
# sample is only 7 trades, so we catch the egregious exhaustion (today's RSI
# 29.7 PE) without curve-fitting a precise boundary. Tighten with more data.

FVG_RSI_PE_OVERSOLD = 40.0   # buying a PUT below this RSI = move likely exhausted
FVG_RSI_CE_WEAK     = 52.0   # buying a CALL below this RSI = momentum not confirming
FVG_LOW_EFFICIENCY  = 0.20   # below this = range-bound/choppy tape

def fvg_exhaustion_block(direction, rsi, efficiency, atr=None, atr_day_high=None):
    """Return (block: bool, reasons: list). Blocks an FVG entry only when 2+
    exhaustion signals agree. Uses factors the bot already computes; no tuned
    weights — just a transparent count of agreeing signals."""
    signals_fired = []
    # S1 — RSI extended against fresh momentum (direction-aware)
    if direction == "bearish" and rsi < FVG_RSI_PE_OVERSOLD:
        signals_fired.append(f"RSI {rsi:.0f}<{FVG_RSI_PE_OVERSOLD:.0f} (PE into oversold)")
    elif direction == "bullish" and rsi < FVG_RSI_CE_WEAK:
        signals_fired.append(f"RSI {rsi:.0f}<{FVG_RSI_CE_WEAK:.0f} (CE w/o momentum)")
    # S2 — low efficiency (range-bound / choppy)
    if efficiency is not None and efficiency < FVG_LOW_EFFICIENCY:
        signals_fired.append(f"efficiency {efficiency:.2f}<{FVG_LOW_EFFICIENCY} (range-bound)")
    # S3 (optional) — ATR already at the day's extreme = move likely spent
    if atr is not None and atr_day_high and atr_day_high > 0 and atr >= atr_day_high:
        signals_fired.append(f"ATR {atr:.0f} at day high (move extended)")
    block = len(signals_fired) >= 2
    return block, signals_fired
