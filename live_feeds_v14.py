"""
═══════════════════════════════════════════════════════════════════════════
  live_feeds_v14.py — Fetch VIX / expiry / gap / prev-OHLC from Upstox
═══════════════════════════════════════════════════════════════════════════

  These are the LIVE inputs the day-context gate needs but cannot compute
  itself. All fetch patterns are ported from the proven v10 bot.

  Network calls live HERE only (the gate/engine stay pure & testable).
  Every function fails soft: returns None on error so the bot degrades
  gracefully (e.g. if VIX fetch fails, the VIX gates simply don't fire).
═══════════════════════════════════════════════════════════════════════════
"""

import datetime
import requests
import pandas as pd

import config

NIFTY_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY = "NSE_INDEX|India VIX"


# [FIX 2026-10-14] The VM clock is UTC; trading dates are IST. Use IST "today"
# everywhere a date decides something (expiry roll, previous session).
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def today_ist():
    return datetime.datetime.now(IST).date()


def _headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.LIVE_TOKEN}",
    }


def get_india_vix():
    """Current India VIX level. [ported from v10 get_india_vix]"""
    try:
        r = requests.get("https://api.upstox.com/v2/market-quote/ltp",
                         headers=_headers(),
                         params={"instrument_key": VIX_KEY}, timeout=5)
        data = r.json()
        if data.get("status") == "success" and data.get("data"):
            key = list(data["data"].keys())[0]
            return float(data["data"][key]["last_price"])
    except Exception:
        pass
    return None


def get_nearest_expiry():
    """Nearest Nifty option expiry as 'YYYY-MM-DD'. [ported from v10]"""
    try:
        r = requests.get("https://api.upstox.com/v2/option/contract",
                         headers=_headers(),
                         params={"instrument_key": NIFTY_KEY}, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            today = today_ist().strftime("%Y-%m-%d")
            expiries = sorted({d.get("expiry", "") for d in data
                               if d.get("expiry", "") >= today})
            for exp in expiries:
                if exp >= today:
                    return exp
    except Exception:
        pass
    return None


def get_all_expiries():
    """Sorted list of all upcoming Nifty expiries ['YYYY-MM-DD', ...]."""
    try:
        r = requests.get("https://api.upstox.com/v2/option/contract",
                         headers=_headers(),
                         params={"instrument_key": NIFTY_KEY}, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            today = today_ist().strftime("%Y-%m-%d")
            return sorted({d.get("expiry", "") for d in data
                           if d.get("expiry", "") >= today})
    except Exception:
        pass
    return []


def get_tradeable_expiry():
    """The expiry we should actually TRADE.

    On a normal day: the nearest expiry.
    On EXPIRY DAY ITSELF: the nearest expiry decays to ~zero intraday (no time
    value, vanishing bid). So we ROLL to the NEXT expiry and trade that instead
    — those contracts still have real premium and a stable bid.
    Returns (tradeable_expiry, nearest_expiry, rolled: bool).
    """
    expiries = get_all_expiries()
    if not expiries:
        return None, None, False
    today = today_ist().strftime("%Y-%m-%d")
    nearest = expiries[0]
    if nearest == today:
        # expiry day — roll to the next contract if one exists
        if len(expiries) >= 2:
            return expiries[1], nearest, True
        return nearest, nearest, False   # no next contract available; caller blocks
    return nearest, nearest, False


def _daily_candles(days_back=12):
    """Daily Nifty candles as [(date, open, high, low, close)], OLDEST FIRST.
    The API's order is not relied on — rows are sorted by their own timestamp."""
    today = today_ist()
    frm = (today - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
    to = today.strftime("%Y-%m-%d")
    url = (f"https://api.upstox.com/v3/historical-candle/{NIFTY_KEY}/days/1/"
           f"{to}/{frm}")
    r = requests.get(url, headers=_headers(), timeout=10)
    if r.status_code != 200 or r.json().get("status") != "success":
        return []
    rows = []
    for c in r.json()["data"]["candles"]:
        d = datetime.date.fromisoformat(str(c[0])[:10])
        rows.append((d, float(c[1]), float(c[2]), float(c[3]), float(c[4])))
    return sorted(rows)


def get_prev_day_ohlc():
    """Previous trading SESSION's OHLC (for gap + CPR).

    [FIX 2026-10-14] Was `candles[-2]` on the raw API list. The API returns
    newest-first, so that picked a day near the START of the 7-day window
    (usually ~4 sessions back): logged gaps matched the real previous close on
    1 of 33 days. Now: sort by date, take the last session strictly before
    today (IST). Returns a 'date' key so the bot can log what it used."""
    try:
        today = today_ist()
        prev = [row for row in _daily_candles() if row[0] < today]
        if prev:
            d, o, h, l, c = prev[-1]
            return {"open": o, "high": h, "low": l, "close": c,
                    "date": d.isoformat()}
    except Exception:
        pass
    return None


def get_session_candles(date_str, interval=5):
    """[ADDED 2026-10-14] Intraday candles of a PAST session (oldest first), same
    columns as the bots' get_candles(). Used to warm up ATR/RSI/EMA so they are
    real from the first minute instead of placeholders (ATR 20 / RSI 50 until
    ~10:20) and EMAs that only exist after ~13:25. None on any failure."""
    try:
        url = (f"https://api.upstox.com/v3/historical-candle/{NIFTY_KEY}/minutes/"
               f"{interval}/{date_str}/{date_str}")
        r = requests.get(url, headers=_headers(), timeout=10)
        if r.status_code == 200 and r.json().get("status") == "success":
            c = r.json()["data"]["candles"]
            if not c:
                return None
            df = pd.DataFrame(c, columns=["timestamp", "open", "high", "low",
                                          "close", "volume", "oi"][:len(c[0])])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            for col in ["open", "high", "low", "close", "volume", "oi"]:
                if col in df:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            return df
    except Exception:
        pass
    return None


def seeded_frame(prev_df, today_df):
    """[ADDED 2026-10-14] Previous session + today's candles, for INDICATORS ONLY
    (ATR, RSI, EMA). Detectors (BOS, FVG, regime window, VWAP) keep using
    today's candles. Returns today_df unchanged if there is no history."""
    if prev_df is None or len(prev_df) == 0:
        return today_df
    if today_df is None or len(today_df) == 0:
        return prev_df
    cols = [c for c in today_df.columns if c in prev_df.columns]
    return pd.concat([prev_df[cols], today_df[cols]], ignore_index=True)


def compute_gap_pct(today_open, prev_ohlc):
    """Gap % of today's open vs previous close. Returns 0.0 if unavailable."""
    try:
        if prev_ohlc and prev_ohlc.get("close") and today_open:
            return (today_open - prev_ohlc["close"]) / prev_ohlc["close"] * 100
    except Exception:
        pass
    return 0.0
