"""
=============================================================
  nifty_auto_bias.py — Nifty 50 Auto Bias + Trend Reversal
  ─────────────────────────────────────────────────────────
  BIAS SOURCES:
  1. PCR from Upstox option chain (25%)
  2. FII/DII bias via Telegram /bias command (40%)
  3. Moneycontrol news sentiment (15%)
  4. India VIX level (10%)
  5. SGX/GIFT Nifty pre-market direction (10%)

  TREND REVERSAL DETECTION:
  Same 8 algorithms as NYSE but calibrated for Nifty:
  1. CHoCH (Change of Character)
  2. BOS (Break of Structure)
  3. RSI Divergence
  4. VWAP Reclaim / Rejection
  5. Volume Climax
  6. EMA Cross (9/21)
  7. Double Top / Double Bottom
  8. Gap Fill Reversal + Previous day S&R

  Called before EVERY trade to confirm or reject entry
=============================================================
"""

import requests
import logging
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  SECTION 1: BIAS SOURCES — NIFTY SPECIFIC
# ═══════════════════════════════════════════════════════════

def get_live_expiries(token, instrument_key="NSE_INDEX|Nifty 50"):
    """
    Fetch all available expiry dates for an instrument directly from
    Upstox option contracts endpoint — no date guessing needed.
    Returns a list of date strings sorted nearest-first, e.g.
    ['2025-05-12', '2025-05-19', '2025-05-29', ...]
    """
    import datetime
    try:
        headers = {"Accept": "application/json",
                   "Authorization": f"Bearer {token}"}
        resp = requests.get(
            "https://api.upstox.com/v2/option/contract",
            headers=headers,
            params={"instrument_key": instrument_key},
            timeout=10
        )
        data = resp.json()
        if data.get("status") != "success" or not data.get("data"):
            log.warning(f"get_live_expiries: bad response {data.get('status')}")
            return []
        today = datetime.date.today()
        expiries = sorted(set(
            item["expiry"] for item in data["data"]
            if "expiry" in item and item["expiry"] >= today.strftime("%Y-%m-%d")
        ))
        log.info(f"Live expiries fetched: {expiries[:6]}")
        return expiries
    except Exception as e:
        log.error(f"get_live_expiries error: {e}")
        return []


def get_pcr_bias(analytics_token):
    """
    PCR from Upstox option chain using LIVE expiry dates.
    Fetches actual available expiries first — no hardcoded weekday math.
    > 1.2 → bullish (more puts = protection buying = market confident)
    < 0.8 → bearish (more calls = speculation = market nervous)
    """
    try:
        headers = {"Accept": "application/json",
                   "Authorization": f"Bearer {analytics_token}"}

        # Step 1: Get real expiry dates from the market
        expiries = get_live_expiries(analytics_token)
        if not expiries:
            log.warning("PCR: no live expiries available")
            return "neutral", None

        # Step 2: Try each expiry nearest-first, pick first with real OI
        for expiry_str in expiries[:5]:
            resp = requests.get(
                "https://api.upstox.com/v2/option/chain",
                headers=headers,
                params={"instrument_key": "NSE_INDEX|Nifty 50",
                        "expiry_date": expiry_str},
                timeout=10
            )
            data = resp.json()
            if data.get("status") != "success" or not data.get("data"):
                log.info(f"PCR: expiry {expiry_str} — no data, trying next")
                continue

            pe_oi = ce_oi = 0
            for r in data["data"]:
                pe = r.get("put_options",  {}) or {}
                ce = r.get("call_options", {}) or {}
                if pe.get("market_data"):
                    pe_oi += pe["market_data"].get("oi", 0)
                if ce.get("market_data"):
                    ce_oi += ce["market_data"].get("oi", 0)

            if ce_oi > 0 and (pe_oi + ce_oi) > 1000:
                pcr  = round(pe_oi / ce_oi, 2)
                bias = "bullish" if pcr > 1.2 else "bearish" if pcr < 0.8 else "neutral"
                log.info(f"PCR: {pcr} → {bias} (expiry:{expiry_str} PE:{pe_oi} CE:{ce_oi})")
                return bias, pcr
            else:
                log.info(f"PCR: expiry {expiry_str} — low OI (PE:{pe_oi} CE:{ce_oi}), trying next")

        log.warning("PCR: all expiries had zero/low OI")
        return "neutral", None

    except Exception as e:
        log.error(f"PCR error: {e}")
        return "neutral", None


def get_india_vix_bias(analytics_token):
    """
    India VIX from Upstox or NSE.
    < 13  → low fear → bullish
    13-18 → neutral
    > 18  → high fear → bearish
    > 25  → extreme fear → strongly bearish
    """
    try:
        headers = {
            "Accept"       : "application/json",
            "Authorization": f"Bearer {analytics_token}"
        }
        resp = requests.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            headers=headers,
            params={"instrument_key": "NSE_INDEX|India VIX"},
            timeout=5
        )
        data = resp.json()
        if data["status"] == "success":
            key = list(data["data"].keys())[0]
            vix = float(data["data"][key]["last_price"])
            bias = "bullish" if vix < 13 else "bearish" if vix > 18 else "neutral"
            log.info(f"India VIX: {vix:.2f} → {bias}")
            return bias, round(vix, 2)
        return "neutral", 15.0
    except Exception as e:
        log.error(f"India VIX error: {e}")
        return "neutral", 15.0


def get_gift_nifty_bias(analytics_token, prev_close):
    """
    GIFT Nifty (SGX Nifty equivalent) pre-market direction.
    Compares GIFT Nifty to previous Nifty close.
    > +0.3% → bullish
    < -0.3% → bearish
    """
    try:
        headers = {
            "Accept"       : "application/json",
            "Authorization": f"Bearer {analytics_token}"
        }
        resp = requests.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            headers=headers,
            params={"instrument_key": "NSE_INDEX|GIFT Nifty"},
            timeout=5
        )
        data = resp.json()
        if data["status"] == "success" and prev_close:
            key       = list(data["data"].keys())[0]
            gift_price = float(data["data"][key]["last_price"])
            chg_pct   = ((gift_price - prev_close) / prev_close) * 100
            bias      = "bullish" if chg_pct > 0.3 else "bearish" if chg_pct < -0.3 else "neutral"
            log.info(f"GIFT Nifty: {gift_price:.2f} | {chg_pct:+.2f}% → {bias}")
            return bias, round(chg_pct, 2), gift_price
        return "neutral", 0, 0
    except Exception as e:
        log.error(f"GIFT Nifty error: {e}")
        return "neutral", 0, 0


def get_moneycontrol_news_bias():
    """Moneycontrol market news sentiment for Nifty."""
    try:
        resp  = requests.get(
            "https://www.moneycontrol.com/news/business/markets/",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        soup  = BeautifulSoup(resp.text, "html.parser")
        heads = []
        for tag in soup.find_all(["h2","h3"], limit=30):
            text = tag.get_text(strip=True)
            if len(text) > 30:
                tl = text.lower()
                if any(w in tl for w in ["nifty","sensex","market","rally",
                                          "fall","stock","trade","index","fii"]):
                    heads.append(text[:120])
        heads = list(dict.fromkeys(heads))[:8]
        bull  = ["rally","surge","gain","rise","bullish","positive","strong",
                 "up","buy","support","recovery","boost","high","record"]
        bear  = ["fall","drop","decline","bearish","negative","weak","down",
                 "sell","crash","pressure","drag","concern","caution"]
        score = sum(1 for h in heads for w in bull if w in h.lower()) - \
                sum(1 for h in heads for w in bear if w in h.lower())
        sent  = "bullish" if score >= 3 else "bearish" if score <= -3 else "neutral"
        log.info(f"Moneycontrol news: {sent} | score={score}")
        return sent, score, heads
    except Exception as e:
        log.error(f"Moneycontrol news error: {e}")
        return "neutral", 0, []


# ═══════════════════════════════════════════════════════════
#  SECTION 2: TREND REVERSAL — NIFTY CALIBRATED
#  Same algorithms, Nifty-specific thresholds
# ═══════════════════════════════════════════════════════════

def detect_choch_nifty(df, current_trend):
    """CHoCH for Nifty — same logic, no price adjustment needed."""
    if df is None or len(df) < 6:
        return False, None, "Not enough candles"

    recent     = df.tail(10)
    last_close = float(recent["close"].iloc[-1])

    if current_trend == "bullish":
        lows = [float(x) for x in recent["low"].tolist()[:-1]]
        if len(lows) >= 2:
            higher_low = max(lows[-3:])
            if last_close < higher_low:
                reason = (f"Bearish CHoCH: Close {last_close:.1f} < "
                          f"Higher low {higher_low:.1f} → uptrend broken!")
                log.warning(reason)
                return True, "bearish", reason

    elif current_trend == "bearish":
        highs = [float(x) for x in recent["high"].tolist()[:-1]]
        if len(highs) >= 2:
            lower_high = min(highs[-3:])
            if last_close > lower_high:
                reason = (f"Bullish CHoCH: Close {last_close:.1f} > "
                          f"Lower high {lower_high:.1f} → downtrend broken!")
                log.info(reason)
                return True, "bullish", reason

    return False, None, "No CHoCH detected"


def detect_rsi_divergence_nifty(df):
    """RSI Divergence for Nifty — same as NYSE."""
    if df is None or len(df) < 14:
        return False, None, 0, "Not enough candles"

    df    = df.copy()
    delta = df["close"].astype(float).diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=13, adjust=False).mean()
    avg_l = loss.ewm(com=13, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    recent     = df.tail(10)
    prices     = recent["close"].astype(float).tolist()
    rsis       = recent["rsi"].tolist()

    if len(prices) < 4:
        return False, None, 0, "Not enough data"

    price_diff = prices[-1] - prices[-4]
    rsi_diff   = rsis[-1]  - rsis[-4]

    # Nifty: price diff in points (50+ meaningful)
    if price_diff < -20 and rsi_diff > 2:
        strength = min(10, abs(rsi_diff))
        reason   = (f"Bullish RSI divergence: Price {price_diff:+.1f}pts "
                    f"RSI +{rsi_diff:.1f} → reversal UP likely")
        return True, "bullish", round(strength, 1), reason

    if price_diff > 20 and rsi_diff < -2:
        strength = min(10, abs(rsi_diff))
        reason   = (f"Bearish RSI divergence: Price +{price_diff:.1f}pts "
                    f"RSI {rsi_diff:.1f} → reversal DOWN likely")
        return True, "bearish", round(strength, 1), reason

    cur_rsi = round(rsis[-1], 1)
    return False, None, 0, f"No RSI divergence | RSI:{cur_rsi}"


def detect_vwap_reversal_nifty(df):
    """VWAP reversal for Nifty."""
    if df is None or len(df) < 5:
        return False, None, "Not enough candles"

    df         = df.copy()
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tv"]  = (df["typical"] * df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"] / df["cum_vol"]

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    vwap  = float(last["vwap"])
    close = float(last["close"])
    prev_close  = float(prev["close"])
    prev_vwap   = float(prev["vwap"])

    if prev_close < prev_vwap and close > vwap:
        reason = f"VWAP Reclaim: Price {close:.1f} crossed above VWAP {vwap:.1f} → bullish reversal"
        return True, "bullish", reason

    if prev_close > prev_vwap and close < vwap:
        reason = f"VWAP Rejection: Price {close:.1f} crossed below VWAP {vwap:.1f} → bearish reversal"
        return True, "bearish", reason

    return False, None, f"No VWAP reversal | VWAP:{vwap:.1f}"


def detect_volume_climax_nifty(df):
    """Volume climax for Nifty — 3x average volume threshold."""
    if df is None or len(df) < 10:
        return False, None, 0, "Not enough candles"

    avg_vol = float(df["volume"].mean())
    last    = df.iloc[-1]
    cur_vol = float(last["volume"])
    rvol    = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1
    body    = float(last["close"]) - float(last["open"])

    if rvol >= 3.0:
        if body < 0:
            reason = (f"Bullish volume climax: RVOL {rvol}x on DOWN candle "
                      f"{body:.1f}pts → sellers exhausted")
            return True, "bullish", rvol, reason
        elif body > 0:
            reason = (f"Bearish volume climax: RVOL {rvol}x on UP candle "
                      f"+{body:.1f}pts → buyers exhausted")
            return True, "bearish", rvol, reason

    return False, None, rvol, f"No climax | RVOL:{rvol}x"


def detect_ema_cross_nifty(df):
    """EMA 9/21 cross for Nifty."""
    if df is None or len(df) < 21:
        return False, None, "Not enough candles"

    df        = df.copy()
    df["e9"]  = df["close"].astype(float).ewm(span=9,  adjust=False).mean()
    df["e21"] = df["close"].astype(float).ewm(span=21, adjust=False).mean()

    last = df.iloc[-1]; prev = df.iloc[-2]
    e9n  = float(last["e9"]);  e21n  = float(last["e21"])
    e9p  = float(prev["e9"]);  e21p  = float(prev["e21"])

    if e9p <= e21p and e9n > e21n:
        reason = (f"Bullish EMA cross: EMA9 {e9n:.1f} > EMA21 {e21n:.1f} → uptrend")
        return True, "bullish", reason

    if e9p >= e21p and e9n < e21n:
        reason = (f"Bearish EMA cross: EMA9 {e9n:.1f} < EMA21 {e21n:.1f} → downtrend")
        return True, "bearish", reason

    return False, None, f"No EMA cross | gap:{e9n-e21n:+.1f}pts"


def detect_double_top_bottom_nifty(df):
    """Double top/bottom for Nifty — tolerance 15 points."""
    if df is None or len(df) < 15:
        return False, None, 0, "Not enough candles"

    recent    = df.tail(20)
    highs     = [float(x) for x in recent["high"].tolist()]
    lows      = [float(x) for x in recent["low"].tolist()]
    tolerance = 15   # 15 Nifty points tolerance

    peaks = [(i, highs[i]) for i in range(1, len(highs)-1)
             if highs[i] > highs[i-1] and highs[i] > highs[i+1]]
    if len(peaks) >= 2:
        h1, h2 = peaks[-2][1], peaks[-1][1]
        if abs(h1-h2) <= tolerance and peaks[-1][0]-peaks[-2][0] >= 3:
            level  = round((h1+h2)/2, 1)
            reason = f"Double Top at {level:.1f} (peaks:{h1:.1f}&{h2:.1f}) → bearish"
            return True, "bearish", level, reason

    troughs = [(i, lows[i]) for i in range(1, len(lows)-1)
               if lows[i] < lows[i-1] and lows[i] < lows[i+1]]
    if len(troughs) >= 2:
        l1, l2 = troughs[-2][1], troughs[-1][1]
        if abs(l1-l2) <= tolerance and troughs[-1][0]-troughs[-2][0] >= 3:
            level  = round((l1+l2)/2, 1)
            reason = f"Double Bottom at {level:.1f} (troughs:{l1:.1f}&{l2:.1f}) → bullish"
            return True, "bullish", level, reason

    return False, None, 0, "No double top/bottom"


def detect_gap_fill_nifty(df, prev_close):
    """Gap fill reversal for Nifty — threshold 30 points."""
    if df is None or len(df) < 2 or not prev_close:
        return False, None, "No prev close"

    open_price = float(df["open"].iloc[0])
    last_price = float(df["close"].iloc[-1])
    gap        = open_price - prev_close

    if gap > 30 and last_price <= prev_close + (gap * 0.5):
        reason = (f"Gap fill: Bullish gap {gap:.1f}pts being filled "
                  f"→ support at {prev_close:.1f}")
        return True, "bullish", reason

    if gap < -30 and last_price >= prev_close + (gap * 0.5):
        reason = (f"Gap fill: Bearish gap {abs(gap):.1f}pts being filled "
                  f"→ resistance at {prev_close:.1f}")
        return True, "bearish", reason

    return False, None, f"No gap fill | gap:{gap:+.1f}pts"


def detect_sr_break_nifty(df, prev_ohlc=None):
    """S&R level break for Nifty using previous day OHLC."""
    if df is None or len(df) < 2:
        return False, None, 0, "Not enough candles"

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    close = float(last["close"])
    high  = float(last["high"])
    low   = float(last["low"])
    tol   = 5   # 5 Nifty points tolerance

    levels = []
    if prev_ohlc:
        levels = [
            ("Prev High",  prev_ohlc["high"],  "resistance"),
            ("Prev Low",   prev_ohlc["low"],   "support"),
            ("Prev Close", prev_ohlc["close"], "pivot"),
        ]

    for name, level, level_type in levels:
        if level_type == "resistance":
            if float(prev["close"]) < level and high > level and close < level - tol:
                reason = (f"Failed breakout at {name} {level:.1f}: "
                          f"High {high:.1f} rejected → bearish")
                return True, "bearish", level, reason
        if level_type == "support":
            if float(prev["close"]) > level and low < level and close > level + tol:
                reason = (f"Failed breakdown at {name} {level:.1f}: "
                          f"Low {low:.1f} recovered → bullish")
                return True, "bullish", level, reason

    return False, None, 0, "No S&R reversal"


# ═══════════════════════════════════════════════════════════
#  SECTION 3: REVERSAL RISK ASSESSMENT — NIFTY
# ═══════════════════════════════════════════════════════════

def assess_reversal_risk_nifty(df_5, df_15, current_trend,
                                prev_close=None, prev_ohlc=None):
    """
    Run all reversal detectors before a Nifty trade.
    Returns: (risk_level, proceed, signals, summary)
    """
    signals  = []
    warnings = 0
    aborts   = 0

    # 1. CHoCH
    choch, choch_type, choch_reason = detect_choch_nifty(df_5, current_trend)
    if choch and choch_type != current_trend:
        signals.append(f"⚠️ CHoCH: {choch_reason}")
        aborts += 1

    # 2. RSI Divergence
    div, div_type, div_str, div_reason = detect_rsi_divergence_nifty(df_15)
    if div and div_type != current_trend:
        signals.append(f"⚠️ RSI Div ({div_str:.1f}): {div_reason}")
        if div_str >= 5: aborts  += 1
        else:            warnings += 1

    # 3. VWAP Reversal
    vr, vr_type, vr_reason = detect_vwap_reversal_nifty(df_5)
    if vr and vr_type != current_trend:
        signals.append(f"⚠️ VWAP: {vr_reason}")
        warnings += 1

    # 4. Volume Climax
    vc, vc_type, vc_rvol, vc_reason = detect_volume_climax_nifty(df_5)
    if vc and vc_type != current_trend:
        signals.append(f"⚠️ Vol Climax ({vc_rvol}x): {vc_reason}")
        if vc_rvol >= 5: aborts  += 1
        else:            warnings += 1

    # 5. EMA Cross
    ec, ec_type, ec_reason = detect_ema_cross_nifty(df_15)
    if ec and ec_type != current_trend:
        signals.append(f"⚠️ EMA Cross: {ec_reason}")
        warnings += 1

    # 6. Double Top/Bottom
    dbl, dbl_type, dbl_level, dbl_reason = detect_double_top_bottom_nifty(df_5)
    if dbl and dbl_type != current_trend:
        signals.append(f"⚠️ {dbl_reason}")
        aborts += 1

    # 7. Gap Fill
    gf, gf_type, gf_reason = detect_gap_fill_nifty(df_5, prev_close)
    if gf and gf_type != current_trend:
        signals.append(f"⚠️ {gf_reason}")
        warnings += 1

    # 8. S&R Break
    sr, sr_type, sr_level, sr_reason = detect_sr_break_nifty(df_5, prev_ohlc)
    if sr and sr_type != current_trend:
        signals.append(f"⚠️ {sr_reason}")
        warnings += 1

    # Risk decision
    if aborts >= 2:
        risk = "ABORT"; proceed = False
        summary = f"ABORT — {aborts} strong reversal signals vs {current_trend}"
    elif aborts == 1 or warnings >= 3:
        risk = "HIGH"; proceed = False
        summary = f"HIGH risk — skip ({aborts} aborts, {warnings} warnings)"
    elif warnings >= 2:
        risk = "MEDIUM"; proceed = True
        summary = f"MEDIUM risk — proceed with tight SL"
    elif warnings == 1:
        risk = "LOW-MEDIUM"; proceed = True
        summary = f"LOW-MEDIUM — proceed normally (1 signal)"
    else:
        risk = "LOW"; proceed = True
        summary = f"LOW risk — all clear for {current_trend}"

    log.info(f"Nifty reversal risk: {risk} | Proceed:{proceed}")
    return risk, proceed, signals, summary


# ═══════════════════════════════════════════════════════════
#  SECTION 4: COMBINED NIFTY BIAS
# ═══════════════════════════════════════════════════════════

def get_combined_bias_nifty(analytics_token, prev_close, user_bias="neutral"):
    """
    Combine all Nifty bias sources.
    Called once at pre-market (9:00 AM IST).
    """
    score_map = {"bullish": 1, "neutral": 0, "bearish": -1}

    pcr_bias,   pcr_val             = get_pcr_bias(analytics_token)
    vix_bias,   vix_val             = get_india_vix_bias(analytics_token)
    gift_bias,  gift_chg, gift_price = get_gift_nifty_bias(analytics_token, prev_close)
    news_bias,  news_score, heads    = get_moneycontrol_news_bias()

    score = (
        score_map.get(user_bias,  0) * 0.40 +   # FII/DII manual (highest)
        score_map.get(pcr_bias,   0) * 0.25 +   # PCR from Upstox
        score_map.get(news_bias,  0) * 0.15 +   # Moneycontrol news
        score_map.get(vix_bias,   0) * 0.10 +   # India VIX
        score_map.get(gift_bias,  0) * 0.10     # GIFT Nifty
    )

    final_bias = "bullish" if score >= 0.25 else "bearish" if score <= -0.25 else "neutral"
    conf       = "HIGH" if abs(score) > 0.5 else "MEDIUM" if abs(score) > 0.25 else "LOW"

    report = {
        "final_bias" : final_bias,
        "confidence" : conf,
        "score"      : round(score, 3),
        "user_bias"  : user_bias,
        "pcr_bias"   : pcr_bias,
        "pcr_val"    : pcr_val,
        "vix_bias"   : vix_bias,
        "vix_val"    : vix_val,
        "gift_bias"  : gift_bias,
        "gift_chg"   : gift_chg,
        "gift_price" : gift_price,
        "news_bias"  : news_bias,
        "news_score" : news_score,
        "headlines"  : heads,
    }

    log.info(f"Nifty combined bias: {final_bias} ({conf}) score={score:.3f}")
    return final_bias, report


# ═══════════════════════════════════════════════════════════
#  SECTION 5: PRE-TRADE CHECK — NIFTY
# ═══════════════════════════════════════════════════════════

def pre_trade_check_nifty(df_5, df_15, direction, pre_bias,
                           prev_close=None, prev_ohlc=None):
    """
    Complete pre-trade validation for Nifty.
    Called before EVERY trade entry.

    Returns: (proceed, risk_level, reason, reversal_signals)
    """
    # Check 1: Bias alignment
    if pre_bias != "neutral" and pre_bias != direction:
        return (False, "HIGH",
                f"Direction {direction} conflicts with pre-market bias {pre_bias}",
                [])

    # Check 2: Reversal risk
    risk, proceed, signals, summary = assess_reversal_risk_nifty(
        df_5, df_15, direction, prev_close, prev_ohlc
    )

    return proceed, risk, summary, signals


# ═══════════════════════════════════════════════════════════
#  SECTION 6: TELEGRAM FORMATTING
# ═══════════════════════════════════════════════════════════

def format_bias_message_nifty(report):
    """Format Nifty bias report for Telegram."""
    bias  = report["final_bias"]
    score = report["score"]
    conf  = report["confidence"]
    icon  = "📈" if bias == "bullish" else "📉" if bias == "bearish" else "➡️"

    lines = [
        f"{icon} <b>NIFTY AUTO BIAS: {bias.upper()} ({conf})</b>",
        f"  Combined score   : {score:+.3f}",
        f"",
        f"  <b>Sources (weighted):</b>",
        f"  FII/DII /bias    : {report['user_bias'].upper()} (40%)",
        f"  PCR              : {report['pcr_bias'].upper()} "
        f"({report['pcr_val'] or 'N/A'}) (25%)",
        f"  Moneycontrol news: {report['news_bias'].upper()} "
        f"(score={report['news_score']}) (15%)",
        f"  India VIX        : {report['vix_bias'].upper()} "
        f"(VIX={report['vix_val']:.1f}) (10%)",
        f"  GIFT Nifty       : {report['gift_bias'].upper()} "
        f"({report['gift_chg']:+.2f}%) (10%)",
        f"",
        f"  Decision: Buy {'CE' if bias=='bullish' else 'PE' if bias=='bearish' else 'CE or PE'} today",
        f"  Override: /bias bullish|bearish|neutral",
    ]

    if report["headlines"]:
        lines += [f"", f"  <b>Headlines:</b>"]
        for h in report["headlines"][:3]:
            lines.append(f"  • {h[:80]}")

    return "\n".join(lines)


def format_reversal_alert_nifty(risk, proceed, signals, summary, strategy, direction):
    """Format Nifty reversal risk alert for Telegram."""
    icon  = "✅" if proceed else "🛑"
    lines = [
        f"{icon} <b>PRE-TRADE CHECK — {strategy} {direction.upper()}</b>",
        f"  Risk     : {risk}",
        f"  Decision : {'PROCEED ✅' if proceed else 'SKIP TRADE ❌'}",
        f"  Summary  : {summary}",
    ]
    if signals:
        lines += [f"", f"  <b>Reversal signals:</b>"]
        for s in signals:
            lines.append(f"  {s[:100]}")
    return "\n".join(lines)
