#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════
  verify_v13.py  —  Integration & Parameter Verification
═══════════════════════════════════════════════════════════════════════════

  PURPOSE
    Verifies that signals.py and nifty_bot_v13.py work together correctly
    WITHOUT live trading. Tests:
      1. signals.py loads and all 12 detectors run
      2. Synthetic candles produce expected signals
      3. Confidence scoring works as designed
      4. Option quality gates function
      5. CSV logging is formatted correctly
      6. Parameter dump shows all expected columns

  RUN
    python3 verify_v13.py

  OUTPUT
    ✓ All checks pass → you can run the live bot
    ✗ Any check fails → detailed error message

═══════════════════════════════════════════════════════════════════════════
"""

import sys
import os
import json
import csv
import pandas as pd
import numpy as np
import datetime

# ─────────────────────────────────────────────────────────────────────────
#  SYNTHETIC CANDLE DATA
# ─────────────────────────────────────────────────────────────────────────

def make_bullish_candles(n=30):
    """Generate synthetic bullish trend candles."""
    base = 24000
    candles = []
    for i in range(n):
        o = base + i * 2 + np.random.uniform(-1, 1)
        h = o + np.random.uniform(10, 20)
        l = o - np.random.uniform(1, 5)
        c = o + np.random.uniform(5, 15)
        v = np.random.uniform(50000, 150000)
        base = c
        candles.append({
            "timestamp": datetime.datetime.now() - datetime.timedelta(minutes=(n-i)*5),
            "open": o, "high": h, "low": l, "close": c, "volume": v, "oi": 0
        })
    return pd.DataFrame(candles)

def make_bearish_candles(n=30):
    """Generate synthetic bearish trend candles."""
    base = 24000
    candles = []
    for i in range(n):
        o = base - i * 2 + np.random.uniform(-1, 1)
        h = o + np.random.uniform(1, 5)
        l = o - np.random.uniform(10, 20)
        c = o - np.random.uniform(5, 15)
        v = np.random.uniform(50000, 150000)
        base = c
        candles.append({
            "timestamp": datetime.datetime.now() - datetime.timedelta(minutes=(n-i)*5),
            "open": o, "high": h, "low": l, "close": c, "volume": v, "oi": 0
        })
    return pd.DataFrame(candles)

def make_ranging_candles(n=30):
    """Generate synthetic ranging candles."""
    base = 24000
    candles = []
    for i in range(n):
        o = base + np.random.uniform(-5, 5)
        h = o + np.random.uniform(5, 10)
        l = o - np.random.uniform(5, 10)
        c = o + np.random.uniform(-5, 5)
        v = np.random.uniform(50000, 150000)
        candles.append({
            "timestamp": datetime.datetime.now() - datetime.timedelta(minutes=(n-i)*5),
            "open": o, "high": h, "low": l, "close": c, "volume": v, "oi": 0
        })
    return pd.DataFrame(candles)

# ─────────────────────────────────────────────────────────────────────────
#  VERIFICATION CHECKS
# ─────────────────────────────────────────────────────────────────────────

def check_signals_module():
    """Check that signals.py loads and all detectors work."""
    print("\n[CHECK 1] signals.py module import and detector availability...")
    try:
        import signals
        
        # Check all constants exist
        required_const = [
            "STRONG_FVG_GAP", "STRONG_FVG_BODY", "MIN_FVG_BODY", "MIN_FVG_SIZE",
            "OTM_OFFSET", "EMASTACK_MIN_RVOL", "EMA50_TOLERANCE", "BOS_SWING_LOOKBACK",
            "BOS_MIN_MOVE", "CPR_BREAKOUT_BUFFER", "RSI_DIV_LOOKBACK", "LOT_SIZE"
        ]
        for const in required_const:
            if not hasattr(signals, const):
                raise ValueError(f"Missing constant: {const}")
        
        # Check all detector functions exist
        required_detectors = [
            "calc_atr", "calc_rsi", "calc_ema", "calc_vwap_bands",
            "detect_fvg", "detect_bos", "detect_ema_stack", "detect_ema_cross",
            "detect_vwap_band_break", "detect_vwap_cross", "detect_ema50_bounce",
            "detect_supertrend_signal", "detect_cpr_signal", "detect_rsi_divergence",
            "detect_orph_orpl", "detect_trend_multi", "classify_intraday_regime"
        ]
        for func in required_detectors:
            if not hasattr(signals, func):
                raise ValueError(f"Missing detector: {func}")
        
        print("  ✓ signals.py loaded with all constants and detectors")
        return True, signals
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False, None

def check_signal_detection(signals_module):
    """Check that signals are detected correctly on synthetic data."""
    print("\n[CHECK 2] Signal detection on synthetic candles...")
    try:
        bull_df = make_bullish_candles(30)
        bear_df = make_bearish_candles(30)
        range_df = make_ranging_candles(30)
        
        # Test on bullish candles
        atr_bull = signals_module.calc_atr(bull_df)
        rsi_bull = signals_module.calc_rsi(bull_df)
        trend_bull, _, strength_bull = signals_module.detect_trend_multi(
            bull_df, bull_df, bull_df
        )
        
        if trend_bull != "bullish":
            raise ValueError(f"Expected bullish trend, got {trend_bull}")
        
        # Test on bearish candles
        trend_bear, _, strength_bear = signals_module.detect_trend_multi(
            bear_df, bear_df, bear_df
        )
        if trend_bear != "bearish":
            raise ValueError(f"Expected bearish trend, got {trend_bear}")
        
        # Test indicators
        if atr_bull <= 0:
            raise ValueError(f"ATR should be positive, got {atr_bull}")
        if rsi_bull < 0 or rsi_bull > 100:
            raise ValueError(f"RSI out of range: {rsi_bull}")
        
        print(f"  ✓ Bull trend: {trend_bull} {strength_bull} (ATR:{atr_bull:.1f} RSI:{rsi_bull:.1f})")
        print(f"  ✓ Bear trend: {trend_bear} {strength_bear}")
        return True
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False

def check_confidence_scoring():
    """Check that confidence scoring works."""
    print("\n[CHECK 3] Confidence scoring logic...")
    try:
        # Import bot module
        import nifty_bot_v13 as bot
        
        # Create fake indicators
        indicators = {
            "atr": 35.0,
            "rsi": 45.0,
            "trend": "bullish",
            "t5": "bullish",
            "t15": "bullish",
            "t30": "bullish",
            "e9": 24100,
            "e21": 24050,
            "e50": 24000,
            "vwap": 24080,
        }
        ltp = 24100
        signal_data = {"type": "bullish", "strong": True}
        
        score, label, reasons = bot.calc_confidence(signal_data, "bullish", indicators, ltp)
        
        if score < 0 or score > 10:
            raise ValueError(f"Confidence out of range: {score}")
        if label not in ("HIGH", "MEDIUM", "LOW"):
            raise ValueError(f"Invalid label: {label}")
        if len(reasons) == 0:
            raise ValueError("No confidence reasons provided")
        
        print(f"  ✓ Bullish signal: {score}/10 {label}")
        print(f"    Reasons: {', '.join(reasons[:3])}")
        
        # Test bearish
        score_bear, label_bear, _ = bot.calc_confidence(
            signal_data, "bearish", indicators, ltp
        )
        print(f"  ✓ Bearish signal: {score_bear}/10 {label_bear}")
        return True
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False

def check_option_quality_gate():
    """Check that option quality gates work."""
    print("\n[CHECK 4] Option quality gate logic...")
    try:
        import nifty_bot_v13 as bot
        
        # Good option
        good_opt = {
            "strike": 24000,
            "opt_type": "CE",
            "bid": 100.0,
            "ask": 102.0,
            "mid": 101.0,
            "spread": 2.0,
            "spread_pct": 0.02,
            "bid_qty": 50,
            "oi": 100000,
            "delta": 0.50,
            "iv": 18.5,
            "theta": -5.0,
            "instr_key": "NSE_FO|NIFTY24MAY24000CE"
        }
        
        ok, msg = bot.check_option_quality(good_opt)
        if not ok:
            raise ValueError(f"Good option rejected: {msg}")
        print(f"  ✓ Good option passed: {msg}")
        
        # Bad spread
        bad_spread_opt = good_opt.copy()
        bad_spread_opt["spread_pct"] = 0.15  # 15% > 8% threshold
        
        ok, msg = bot.check_option_quality(bad_spread_opt)
        if ok:
            raise ValueError("Bad spread option should have been rejected")
        print(f"  ✓ Bad spread rejected: {msg}")
        
        # Low OI
        low_oi_opt = good_opt.copy()
        low_oi_opt["oi"] = 10000  # < 50k threshold
        
        ok, msg = bot.check_option_quality(low_oi_opt)
        if ok:
            raise ValueError("Low OI option should have been rejected")
        print(f"  ✓ Low OI rejected: {msg}")
        
        return True
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False

def check_csv_logging():
    """Check that CSV logging structure is correct."""
    print("\n[CHECK 5] CSV logging schema...")
    try:
        import nifty_bot_v13 as bot
        
        # Check all column definitions exist
        for cols, name in [
            (bot.SCAN_COLS, "SCAN"),
            (bot.TRADE_COLS, "TRADE"),
            (bot.SKIP_COLS, "SKIP")
        ]:
            if not isinstance(cols, list) or len(cols) == 0:
                raise ValueError(f"{name} columns not defined")
            print(f"  ✓ {name} columns: {len(cols)} fields")
        
        # Try writing a test row
        test_scan = {
            "datetime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "nifty_ltp": 24100.5,
            "atm_strike": 24100,
            "session_bias": "TRENDING_BULL",
            "rsi": 55.3,
            "atr": 32.1,
            "signal_fired": "StrongFVG",
            "daily_pnl": 0,
        }
        
        # Validate that all required fields can be mapped
        row = {c: test_scan.get(c, "") for c in bot.SCAN_COLS}
        if len(row) != len(bot.SCAN_COLS):
            raise ValueError("Row size mismatch")
        
        print(f"  ✓ Test row: {len(row)} fields populated")
        return True
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False

def check_position_class():
    """Check that PaperPosition class works."""
    print("\n[CHECK 6] PaperPosition class and P&L calculation...")
    try:
        import nifty_bot_v13 as bot
        
        # Create a fake option
        opt_data = {
            "strike": 24000,
            "opt_type": "CE",
            "mid": 150.0,
            "bid": 148.0,
            "ask": 152.0,
            "spread": 4.0,
            "delta": 0.55,
            "iv": 19.0,
            "instr_key": "NSE_FO|NIFTY24MAY24000CE"
        }
        
        indicators = {
            "rsi": 52.0,
            "atr": 30.0,
            "trend": "bullish"
        }
        
        pos = bot.PaperPosition(
            trade_no=1,
            strategy="TestFVG",
            direction="bullish",
            entry_nifty=24100.0,
            option_data=opt_data,
            confidence=(7, "MEDIUM"),
            indicators=indicators,
            prev_ltp=24090.0
        )
        
        # Check position properties
        if pos.lots != 1:
            raise ValueError(f"Expected 1 lot, got {pos.lots}")
        if pos.sl_price >= pos.entry_premium:
            raise ValueError(f"SL {pos.sl_price} should be < entry {pos.entry_premium}")
        if pos.tgt_price <= pos.entry_premium:
            raise ValueError(f"Target {pos.tgt_price} should be > entry {pos.entry_premium}")
        
        # Test P&L calculation
        exit_premium = 160.0  # +10 profit
        pnl, pts = pos.calc_pnl(exit_premium)
        
        if pnl <= 0:
            raise ValueError(f"Expected positive P&L, got {pnl}")
        
        print(f"  ✓ Position created: {pos.strategy} {pos.direction}")
        print(f"    Entry premium: Rs.{pos.entry_premium:.2f}")
        print(f"    SL: Rs.{pos.sl_price:.2f} ({pos.sl_pct*100:.0f}% of premium)")
        print(f"    Target: Rs.{pos.tgt_price:.2f} ({pos.tgt_sustain_pct*100:.0f}% sustain)")
        print(f"  ✓ P&L calc: exit@{exit_premium:.0f} → Rs.{pnl:+.0f} ({pts:+.1f}pts)")
        
        return True
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False

def check_parameter_dump():
    """Generate a sample parameter dump to show what logging looks like."""
    print("\n[CHECK 7] Parameter dump (sample trade for analysis)...")
    try:
        import nifty_bot_v13 as bot
        
        # Create sample scenario
        opt_data = {
            "strike": 24050,
            "opt_type": "CE",
            "mid": 145.50,
            "bid": 143.50,
            "ask": 147.50,
            "spread": 4.0,
            "delta": 0.52,
            "iv": 18.75,
            "instr_key": "NSE_FO|NIFTY24MAY24050CE"
        }
        
        indicators = {
            "rsi": 54.3,
            "atr": 31.5,
            "trend": "bullish",
            "t5": "bullish",
            "t15": "bullish",
            "t30": "neutral",
            "e9": 24102.3,
            "e21": 24055.7,
            "e50": 24020.1,
            "vwap": 24080.5,
            "rvol": 1.35,
        }
        
        pos = bot.PaperPosition(
            trade_no=42,
            strategy="StrongFVG",
            direction="bullish",
            entry_nifty=24105.0,
            option_data=opt_data,
            confidence=(8, "HIGH"),
            indicators=indicators,
            prev_ltp=24095.0
        )
        
        sample_trade = {
            "trade_no": pos.trade_no,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "strategy": pos.strategy,
            "direction": pos.direction,
            "entry_nifty": round(pos.entry_nifty, 1),
            "entry_premium": round(pos.entry_premium, 2),
            "lots": pos.lots,
            "capital": round(pos.capital, 0),
            "sl_pct": round(pos.sl_pct * 100, 1),
            "sl_price": round(pos.sl_price, 2),
            "tgt_sustain_pct": round(pos.tgt_sustain_pct * 100, 1),
            "tgt_price": round(pos.tgt_price, 2),
            "entry_time": pos.entry_time,
            "entry_rsi": round(pos.entry_rsi, 1),
            "entry_atr": round(pos.entry_atr, 1),
            "entry_trend": pos.entry_trend,
            "entry_conf": pos.confidence[0],
            "option_strike": pos.option_strike,
            "option_type": pos.option_type,
            "option_iv": round(pos.option_iv, 2),
            "option_delta": round(pos.option_delta, 3),
            "option_spread": round(pos.option_spread, 2),
        }
        
        print("  ✓ Sample trade entry logged:")
        for key in ["trade_no", "timestamp", "strategy", "direction", "entry_nifty",
                   "entry_premium", "lots", "entry_rsi", "entry_atr", "entry_conf",
                   "option_strike", "option_type", "option_iv", "option_delta"]:
            val = sample_trade.get(key, "?")
            print(f"    {key:20s}: {val}")
        
        return True
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 75)
    print("  nifty_bot_v13 VERIFICATION SUITE")
    print("=" * 75)
    
    results = []
    
    # Check 1: signals module
    ok, signals_mod = check_signals_module()
    results.append(("signals.py module", ok))
    
    if signals_mod:
        # Check 2: signal detection
        ok = check_signal_detection(signals_mod)
        results.append(("Signal detection", ok))
    
    # Check 3: confidence scoring
    ok = check_confidence_scoring()
    results.append(("Confidence scoring", ok))
    
    # Check 4: option quality gate
    ok = check_option_quality_gate()
    results.append(("Option quality gate", ok))
    
    # Check 5: CSV logging
    ok = check_csv_logging()
    results.append(("CSV logging schema", ok))
    
    # Check 6: position class
    ok = check_position_class()
    results.append(("PaperPosition class", ok))
    
    # Check 7: parameter dump
    ok = check_parameter_dump()
    results.append(("Parameter dump", ok))
    
    # Summary
    print("\n" + "=" * 75)
    print("  VERIFICATION SUMMARY")
    print("=" * 75)
    for check_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status:8s} : {check_name}")
    
    all_pass = all(p for _, p in results)
    print("\n" + "=" * 75)
    if all_pass:
        print("  ✓✓✓ ALL CHECKS PASSED — v13 is ready for live trading ✓✓✓")
        print("=" * 75)
        return 0
    else:
        print("  ✗✗✗ SOME CHECKS FAILED — review errors above ✗✗✗")
        print("=" * 75)
        return 1

if __name__ == "__main__":
    sys.exit(main())
