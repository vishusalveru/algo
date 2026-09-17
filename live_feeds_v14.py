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

import config

NIFTY_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY = "NSE_INDEX|India VIX"


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
            today = datetime.date.today().strftime("%Y-%m-%d")
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
            today = datetime.date.today().strftime("%Y-%m-%d")
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
    today = datetime.date.today().strftime("%Y-%m-%d")
    nearest = expiries[0]
    if nearest == today:
        # expiry day — roll to the next contract if one exists
        if len(expiries) >= 2:
            return expiries[1], nearest, True
        return nearest, nearest, False   # no next contract available; caller blocks
    return nearest, nearest, False


def get_prev_day_ohlc():
    """Previous trading day's OHLC (for gap + CPR)."""
    try:
        today = datetime.date.today()
        frm = (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        to = today.strftime("%Y-%m-%d")
        url = (f"https://api.upstox.com/v3/historical-candle/{NIFTY_KEY}/days/1/"
               f"{to}/{frm}")
        r = requests.get(url, headers=_headers(), timeout=10)
        if r.status_code == 200 and r.json().get("status") == "success":
            candles = r.json()["data"]["candles"]
            if len(candles) >= 2:
                p = candles[-2]
                return {"open": float(p[1]), "high": float(p[2]),
                        "low": float(p[3]), "close": float(p[4])}
    except Exception:
        pass
    return None


def compute_gap_pct(today_open, prev_ohlc):
    """Gap % of today's open vs previous close. Returns 0.0 if unavailable."""
    try:
        if prev_ohlc and prev_ohlc.get("close") and today_open:
            return (today_open - prev_ohlc["close"]) / prev_ohlc["close"] * 100
    except Exception:
        pass
    return 0.0
