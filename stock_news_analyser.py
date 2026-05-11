"""
=============================================================
  stock_news_analyser.py — Multi-Source News Stock Analyser
  ─────────────────────────────────────────────────────────
  PURPOSE:
    Scrapes and analyses news from multiple sources to
    surface actionable stock signals for Indian markets.

  SOURCES:
    Global Macro : Reuters, Yahoo Finance, Investing.com RSS
    India News   : Moneycontrol, Economic Times, Business
                   Standard, NSE announcements

  OUTPUT PER STOCK (Indian only):
    - Sentiment  : BULLISH / BEARISH / NEUTRAL
    - Horizon    : SHORT (1–5d) / LONG (3–12m)
    - Catalyst   : EARNINGS / UPGRADE / MACRO / POLICY / M&A
    - Technicals : EMA9, EMA21, RSI, VWAP, Volume (Upstox)
    - Decision   : ✅ BUY / ❌ SELL / ⏳ WAIT
    - Confidence : 1–5

  CALLED FROM:
    Standalone cron or nifty_bot_v5.py /news command

  USAGE:
    python3 stock_news_analyser.py
=============================================================
"""

import re
import logging
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  SOURCES CONFIG
# ─────────────────────────────────────────────
SOURCES = {
    # ── India ───────────────────────────────
    "moneycontrol_markets": {
        "url"    : "https://www.moneycontrol.com/news/business/markets/",
        "type"   : "html",
        "region" : "IN",
        "weight" : 1.0,
    },
    "moneycontrol_stocks": {
        "url"    : "https://www.moneycontrol.com/news/business/stocks/",
        "type"   : "html",
        "region" : "IN",
        "weight" : 1.0,
    },
    "economic_times": {
        "url"    : "https://economictimes.indiatimes.com/markets/stocks/news/rssfeeds/2146842.cms",
        "type"   : "rss",
        "region" : "IN",
        "weight" : 1.0,
    },
    "business_standard": {
        "url"    : "https://www.business-standard.com/rss/markets-106.rss",
        "type"   : "rss",
        "region" : "IN",
        "weight" : 0.9,
    },
    "nse_announcements": {
        "url"    : "https://www.nseindia.com/api/corporate-announcements?index=equities",
        "type"   : "nse_json",
        "region" : "IN",
        "weight" : 1.2,
    },
    # ── Global Macro (for macro context only) ─
    "reuters_markets": {
        "url"    : "https://feeds.reuters.com/reuters/businessNews",
        "type"   : "rss",
        "region" : "GLOBAL",
        "weight" : 1.1,
    },
    "yahoo_finance": {
        "url"    : "https://finance.yahoo.com/news/rssindex",
        "type"   : "rss",
        "region" : "GLOBAL",
        "weight" : 0.8,
    },
    "investing_com": {
        "url"    : "https://www.investing.com/rss/news.rss",
        "type"   : "rss",
        "region" : "GLOBAL",
        "weight" : 0.8,
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/rss+xml,*/*",
}

# ─────────────────────────────────────────────
#  INDIAN STOCK ALIASES → NSE ticker + Upstox key
# ─────────────────────────────────────────────
# Format: alias → (NSE_TICKER, UPSTOX_INSTRUMENT_KEY)
STOCK_ALIASES = {
    "reliance"          : ("RELIANCE",    "NSE_EQ|INE002A01018"),
    "ril"               : ("RELIANCE",    "NSE_EQ|INE002A01018"),
    "tcs"               : ("TCS",         "NSE_EQ|INE467B01029"),
    "tata consultancy"  : ("TCS",         "NSE_EQ|INE467B01029"),
    "infosys"           : ("INFY",        "NSE_EQ|INE009A01021"),
    "infy"              : ("INFY",        "NSE_EQ|INE009A01021"),
    "hdfc bank"         : ("HDFCBANK",    "NSE_EQ|INE040A01034"),
    "hdfcbank"          : ("HDFCBANK",    "NSE_EQ|INE040A01034"),
    "icici bank"        : ("ICICIBANK",   "NSE_EQ|INE090A01021"),
    "icicibank"         : ("ICICIBANK",   "NSE_EQ|INE090A01021"),
    "wipro"             : ("WIPRO",       "NSE_EQ|INE075A01022"),
    "hcl tech"          : ("HCLTECH",     "NSE_EQ|INE860A01027"),
    "hcltech"           : ("HCLTECH",     "NSE_EQ|INE860A01027"),
    "bajaj finance"     : ("BAJFINANCE",  "NSE_EQ|INE296A01024"),
    "bajaj auto"        : ("BAJAJ-AUTO",  "NSE_EQ|INE917I01010"),
    "maruti"            : ("MARUTI",      "NSE_EQ|INE585B01010"),
    "maruti suzuki"     : ("MARUTI",      "NSE_EQ|INE585B01010"),
    "sun pharma"        : ("SUNPHARMA",   "NSE_EQ|INE044A01036"),
    "sunpharma"         : ("SUNPHARMA",   "NSE_EQ|INE044A01036"),
    "dr reddy"          : ("DRREDDY",     "NSE_EQ|INE089A01023"),
    "cipla"             : ("CIPLA",       "NSE_EQ|INE059A01026"),
    "divis lab"         : ("DIVISLAB",    "NSE_EQ|INE361B01024"),
    "asian paints"      : ("ASIANPAINT",  "NSE_EQ|INE021A01026"),
    "nestle"            : ("NESTLEIND",   "NSE_EQ|INE239A01016"),
    "hindustan unilever": ("HINDUNILVR",  "NSE_EQ|INE030A01027"),
    "hul"               : ("HINDUNILVR",  "NSE_EQ|INE030A01027"),
    "itc"               : ("ITC",         "NSE_EQ|INE154A01025"),
    "kotak"             : ("KOTAKBANK",   "NSE_EQ|INE237A01028"),
    "kotak bank"        : ("KOTAKBANK",   "NSE_EQ|INE237A01028"),
    "axis bank"         : ("AXISBANK",    "NSE_EQ|INE238A01034"),
    "sbi"               : ("SBIN",        "NSE_EQ|INE062A01020"),
    "state bank"        : ("SBIN",        "NSE_EQ|INE062A01020"),
    "power grid"        : ("POWERGRID",   "NSE_EQ|INE752E01010"),
    "ntpc"              : ("NTPC",        "NSE_EQ|INE733E01010"),
    "ongc"              : ("ONGC",        "NSE_EQ|INE213A01029"),
    "bharti airtel"     : ("BHARTIARTL",  "NSE_EQ|INE397D01024"),
    "airtel"            : ("BHARTIARTL",  "NSE_EQ|INE397D01024"),
    "titan"             : ("TITAN",       "NSE_EQ|INE280A01028"),
    "ultratech"         : ("ULTRACEMCO",  "NSE_EQ|INE481G01011"),
    "ultratech cement"  : ("ULTRACEMCO",  "NSE_EQ|INE481G01011"),
    "adani ports"       : ("ADANIPORTS",  "NSE_EQ|INE742F01042"),
    "adani enterprises" : ("ADANIENT",    "NSE_EQ|INE423A01024"),
    "tata steel"        : ("TATASTEEL",   "NSE_EQ|INE081A01020"),
    "jsw steel"         : ("JSWSTEEL",    "NSE_EQ|INE019A01038"),
    "jswsteel"          : ("JSWSTEEL",    "NSE_EQ|INE019A01038"),
    "hindalco"          : ("HINDALCO",    "NSE_EQ|INE038A01020"),
    "eicher motors"     : ("EICHERMOT",   "NSE_EQ|INE066A01021"),
    "hero motocorp"     : ("HEROMOTOCO",  "NSE_EQ|INE158A01026"),
    "tech mahindra"     : ("TECHM",       "NSE_EQ|INE669C01036"),
    "ltimindtree"       : ("LTIM",        "NSE_EQ|INE214T01019"),
    "indusind bank"     : ("INDUSINDBK",  "NSE_EQ|INE095A01012"),
    "mahindra"          : ("M&M",         "NSE_EQ|INE101A01026"),
    "m&m"               : ("M&M",         "NSE_EQ|INE101A01026"),
    "tata motors"       : ("TATAMOTORS",  "NSE_EQ|INE155A01022"),
    "bpcl"              : ("BPCL",        "NSE_EQ|INE029A01011"),
    "britannia"         : ("BRITANNIA",   "NSE_EQ|INE216A01030"),
    "apollo hospitals"  : ("APOLLOHOSP",  "NSE_EQ|INE437A01024"),
    "dmart"             : ("DMART",       "NSE_EQ|INE192R01011"),
    "avenue supermarts" : ("DMART",       "NSE_EQ|INE192R01011"),
    "coal india"        : ("COALINDIA",   "NSE_EQ|INE522F01014"),
    "tata power"        : ("TATAPOWER",   "NSE_EQ|INE245A01021"),
    "tata consumer"     : ("TATACONSUM",  "NSE_EQ|INE192A01025"),
    "grasim"            : ("GRASIM",      "NSE_EQ|INE047A01021"),
}

# ticker → instrument key lookup
TICKER_TO_KEY = {v[0]: v[1] for v in STOCK_ALIASES.values()}

# ─────────────────────────────────────────────
#  KEYWORD DICTIONARIES
# ─────────────────────────────────────────────
BULL_KEYWORDS = {
    "beat"                    : ("BULLISH", "SHORT", "EARNINGS", 4),
    "beats"                   : ("BULLISH", "SHORT", "EARNINGS", 4),
    "outperform"              : ("BULLISH", "SHORT", "UPGRADE",  3),
    "upgrade"                 : ("BULLISH", "SHORT", "UPGRADE",  4),
    "buy rating"              : ("BULLISH", "SHORT", "UPGRADE",  4),
    "strong buy"              : ("BULLISH", "SHORT", "UPGRADE",  4),
    "buyback"                 : ("BULLISH", "SHORT", "M&A",      3),
    "dividend"                : ("BULLISH", "SHORT", "EARNINGS", 2),
    "bonus"                   : ("BULLISH", "SHORT", "EARNINGS", 2),
    "record high"             : ("BULLISH", "SHORT", "TECHNICAL",3),
    "52-week high"            : ("BULLISH", "SHORT", "TECHNICAL",3),
    "breakout"                : ("BULLISH", "SHORT", "TECHNICAL",3),
    "surge"                   : ("BULLISH", "SHORT", "TECHNICAL",2),
    "rally"                   : ("BULLISH", "SHORT", "TECHNICAL",2),
    "profit jumps"            : ("BULLISH", "SHORT", "EARNINGS", 4),
    "net profit rises"        : ("BULLISH", "SHORT", "EARNINGS", 4),
    "revenue rises"           : ("BULLISH", "SHORT", "EARNINGS", 3),
    "q4 results beat"         : ("BULLISH", "SHORT", "EARNINGS", 5),
    "q3 results beat"         : ("BULLISH", "SHORT", "EARNINGS", 5),
    "expansion"               : ("BULLISH", "LONG",  "SECTOR",   3),
    "acquisition"             : ("BULLISH", "LONG",  "M&A",      3),
    "merger"                  : ("BULLISH", "LONG",  "M&A",      3),
    "order win"               : ("BULLISH", "LONG",  "EARNINGS", 4),
    "order book"              : ("BULLISH", "LONG",  "EARNINGS", 3),
    "capex"                   : ("BULLISH", "LONG",  "SECTOR",   2),
    "capacity expansion"      : ("BULLISH", "LONG",  "SECTOR",   3),
    "policy support"          : ("BULLISH", "LONG",  "POLICY",   3),
    "government contract"     : ("BULLISH", "LONG",  "POLICY",   4),
    "pli"                     : ("BULLISH", "LONG",  "POLICY",   3),
    "target raised"           : ("BULLISH", "BOTH",  "UPGRADE",  4),
    "target price raised"     : ("BULLISH", "BOTH",  "UPGRADE",  4),
}

BEAR_KEYWORDS = {
    "miss"                    : ("BEARISH", "SHORT", "EARNINGS", 4),
    "misses"                  : ("BEARISH", "SHORT", "EARNINGS", 4),
    "downgrade"               : ("BEARISH", "SHORT", "UPGRADE",  4),
    "sell rating"             : ("BEARISH", "SHORT", "UPGRADE",  4),
    "underperform"            : ("BEARISH", "SHORT", "UPGRADE",  3),
    "profit falls"            : ("BEARISH", "SHORT", "EARNINGS", 4),
    "net profit drops"        : ("BEARISH", "SHORT", "EARNINGS", 4),
    "revenue declines"        : ("BEARISH", "SHORT", "EARNINGS", 3),
    "q4 results miss"         : ("BEARISH", "SHORT", "EARNINGS", 5),
    "52-week low"             : ("BEARISH", "SHORT", "TECHNICAL",3),
    "breakdown"               : ("BEARISH", "SHORT", "TECHNICAL",3),
    "crash"                   : ("BEARISH", "SHORT", "TECHNICAL",3),
    "slump"                   : ("BEARISH", "SHORT", "TECHNICAL",2),
    "debt concern"            : ("BEARISH", "SHORT", "EARNINGS", 3),
    "margin pressure"         : ("BEARISH", "SHORT", "EARNINGS", 3),
    "regulatory action"       : ("BEARISH", "LONG",  "POLICY",   4),
    "sebi notice"             : ("BEARISH", "LONG",  "POLICY",   5),
    "fraud"                   : ("BEARISH", "LONG",  "POLICY",   5),
    "investigation"           : ("BEARISH", "LONG",  "POLICY",   4),
    "plant shutdown"          : ("BEARISH", "LONG",  "SECTOR",   4),
    "losing market share"     : ("BEARISH", "LONG",  "SECTOR",   3),
    "target cut"              : ("BEARISH", "BOTH",  "UPGRADE",  4),
    "target price cut"        : ("BEARISH", "BOTH",  "UPGRADE",  4),
    "delisting"               : ("BEARISH", "LONG",  "M&A",      5),
}

GLOBAL_MACRO = {
    "fed rate"        : ("MACRO", "BOTH",  "MACRO"),
    "federal reserve" : ("MACRO", "BOTH",  "MACRO"),
    "us inflation"    : ("MACRO", "SHORT", "MACRO"),
    "us cpi"          : ("MACRO", "SHORT", "MACRO"),
    "crude oil"       : ("MACRO", "SHORT", "SECTOR"),
    "brent"           : ("MACRO", "SHORT", "SECTOR"),
    "dollar index"    : ("MACRO", "SHORT", "MACRO"),
    "rupee"           : ("MACRO", "SHORT", "MACRO"),
    "china gdp"       : ("MACRO", "LONG",  "MACRO"),
    "global recession": ("MACRO", "LONG",  "MACRO"),
    "trade war"       : ("MACRO", "LONG",  "MACRO"),
    "tariff"          : ("MACRO", "BOTH",  "POLICY"),
    "rbi rate"        : ("MACRO", "SHORT", "POLICY"),
    "rbi policy"      : ("MACRO", "SHORT", "POLICY"),
    "repo rate"       : ("MACRO", "SHORT", "POLICY"),
    "inflation data"  : ("MACRO", "SHORT", "MACRO"),
    "gdp growth"      : ("MACRO", "LONG",  "MACRO"),
    "fii buying"      : ("MACRO", "SHORT", "MACRO"),
    "fii selling"     : ("MACRO", "SHORT", "MACRO"),
    "dii buying"      : ("MACRO", "SHORT", "MACRO"),
}


# ─────────────────────────────────────────────
#  SCRAPERS
# ─────────────────────────────────────────────

def fetch_html_headlines(url, limit=40):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return []
        soup  = BeautifulSoup(resp.text, "html.parser")
        texts = []
        for tag in soup.find_all(["h1","h2","h3","h4","a"], limit=200):
            text = tag.get_text(strip=True)
            if 20 < len(text) < 200:
                texts.append(text)
        return list(dict.fromkeys(texts))[:limit]
    except Exception as e:
        log.error(f"fetch_html {url}: {e}")
        return []


def fetch_rss_headlines(url, limit=30):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return []
        root  = ET.fromstring(resp.content)
        items = root.findall(".//item")
        texts = []
        for item in items[:limit]:
            title = item.findtext("title", "").strip()
            desc  = item.findtext("description", "").strip()
            if desc:
                desc = BeautifulSoup(desc, "html.parser").get_text(strip=True)
            combined = f"{title}. {desc[:100]}" if desc else title
            if combined:
                texts.append(combined[:200])
        return texts
    except Exception as e:
        log.error(f"fetch_rss {url}: {e}")
        return []


def fetch_nse_announcements(limit=20):
    try:
        resp = requests.get(
            "https://www.nseindia.com/api/corporate-announcements",
            headers={**HEADERS, "Referer": "https://www.nseindia.com"},
            params={"index": "equities"},
            timeout=12
        )
        data  = resp.json()
        items = []
        for item in (data if isinstance(data, list) else data.get("data",[]))[:limit]:
            symbol  = item.get("symbol","")
            subject = item.get("subject","") or item.get("desc","")
            if symbol and subject:
                items.append(f"{symbol}: {subject[:150]}")
        return items
    except Exception as e:
        log.error(f"NSE announcements: {e}")
        return []


# ─────────────────────────────────────────────
#  STOCK MENTION EXTRACTOR
# ─────────────────────────────────────────────

def extract_stock_mentions(headline):
    """Returns list of (NSE_TICKER, UPSTOX_KEY) tuples found in headline."""
    hl    = headline.lower()
    found = {}
    for alias, (ticker, key) in STOCK_ALIASES.items():
        if alias in hl:
            found[ticker] = key
    # Also catch uppercase tickers directly e.g. RELIANCE, TCS
    words = re.findall(r'\b[A-Z]{2,12}\b', headline)
    for w in words:
        if w in TICKER_TO_KEY:
            found[w] = TICKER_TO_KEY[w]
    return list(found.items())   # [(ticker, key), ...]


def score_headline(headline, source_region="IN", source_weight=1.0):
    hl_lower = headline.lower()
    stocks   = extract_stock_mentions(headline)

    for kw, (sentiment, horizon, catalyst, base_conf) in BULL_KEYWORDS.items():
        if kw in hl_lower:
            conf = min(5, round(base_conf * source_weight))
            return {
                "headline" : headline[:150],
                "stocks"   : stocks,
                "sentiment": sentiment,
                "horizon"  : horizon,
                "catalyst" : catalyst,
                "conf"     : conf,
                "region"   : source_region,
            }

    for kw, (sentiment, horizon, catalyst, base_conf) in BEAR_KEYWORDS.items():
        if kw in hl_lower:
            conf = min(5, round(base_conf * source_weight))
            return {
                "headline" : headline[:150],
                "stocks"   : stocks,
                "sentiment": sentiment,
                "horizon"  : horizon,
                "catalyst" : catalyst,
                "conf"     : conf,
                "region"   : source_region,
            }

    for kw, (sentiment, horizon, catalyst) in GLOBAL_MACRO.items():
        if kw in hl_lower:
            return {
                "headline" : headline[:150],
                "stocks"   : stocks,
                "sentiment": sentiment,
                "horizon"  : horizon,
                "catalyst" : catalyst,
                "conf"     : 2,
                "region"   : source_region,
            }

    return None


# ─────────────────────────────────────────────
#  UPSTOX TECHNICALS FETCHER
# ─────────────────────────────────────────────

def get_technicals(ticker, instrument_key, live_token):
    """
    Fetch 5-min candles from Upstox for a stock and compute:
    EMA9, EMA21, RSI14, VWAP, Volume (today vs 5d avg), LTP.
    Returns dict or None on failure.
    """
    try:
        headers = {
            "Accept"       : "application/json",
            "Authorization": f"Bearer {live_token}"
        }

        # ── LTP ──────────────────────────────
        ltp_resp = requests.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            headers=headers,
            params={"instrument_key": instrument_key},
            timeout=5
        )
        ltp_data = ltp_resp.json()
        if ltp_data.get("status") != "success":
            return None
        ltp = float(list(ltp_data["data"].values())[0]["last_price"])

        # ── Intraday 5-min candles ────────────
        url = (f"https://api.upstox.com/v3/historical-candle/intraday/"
               f"{instrument_key}/minutes/5")
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("status") != "success" or not data["data"].get("candles"):
            return None

        candles = data["data"]["candles"]
        df = pd.DataFrame(candles, columns=[
            "timestamp","open","high","low","close","volume","oi"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)

        if len(df) < 22:
            return None

        # ── EMAs ─────────────────────────────
        df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        ema9  = round(df["ema9"].iloc[-1],  2)
        ema21 = round(df["ema21"].iloc[-1], 2)

        # ── RSI ──────────────────────────────
        delta  = df["close"].diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))
        rsi = round(df["rsi"].iloc[-1], 1)

        # ── VWAP ─────────────────────────────
        df["tp"]  = (df["high"] + df["low"] + df["close"]) / 3
        df["tpv"] = df["tp"] * df["volume"]
        vwap = round(df["tpv"].cumsum().iloc[-1] / df["volume"].cumsum().iloc[-1], 2)

        # ── Volume ───────────────────────────
        today_vol = int(df["volume"].sum())
        avg_vol   = int(df["volume"].mean() * len(df))   # rough proxy
        rvol      = round(today_vol / avg_vol, 2) if avg_vol > 0 else 1.0

        return {
            "ticker"   : ticker,
            "ltp"      : ltp,
            "ema9"     : ema9,
            "ema21"    : ema21,
            "rsi"      : rsi,
            "vwap"     : vwap,
            "today_vol": today_vol,
            "rvol"     : rvol,
        }

    except Exception as e:
        log.error(f"Technicals {ticker}: {e}")
        return None


# ─────────────────────────────────────────────
#  BUY / SELL / WAIT DECISION
# ─────────────────────────────────────────────

def make_decision(sentiment, tech):
    """
    Combine news sentiment + technicals → BUY / SELL / WAIT + reason.
    """
    if tech is None:
        # No technicals — decision from news only
        if sentiment == "BULLISH":
            return "⏳ WAIT", "No live data — news bullish but unconfirmed"
        elif sentiment == "BEARISH":
            return "⏳ WAIT", "No live data — news bearish but unconfirmed"
        return "⏳ WAIT", "Insufficient data"

    ltp   = tech["ltp"]
    ema9  = tech["ema9"]
    ema21 = tech["ema21"]
    rsi   = tech["rsi"]
    vwap  = tech["vwap"]
    rvol  = tech["rvol"]

    bull_tech = (ema9 > ema21) and (ltp > vwap) and (rsi < 70) and (rvol >= 1.0)
    bear_tech = (ema9 < ema21) and (ltp < vwap) and (rsi > 30) and (rvol >= 1.0)

    reasons = []
    if ema9 > ema21:  reasons.append("EMA✅")
    else:             reasons.append("EMA❌")
    if ltp > vwap:    reasons.append("AboveVWAP✅")
    else:             reasons.append("BelowVWAP❌")
    if rsi < 30:      reasons.append(f"RSI oversold({rsi})")
    elif rsi > 70:    reasons.append(f"RSI overbought({rsi})")
    else:             reasons.append(f"RSI ok({rsi})")
    if rvol >= 1.2:   reasons.append(f"HighVol({rvol}x)✅")
    elif rvol >= 1.0: reasons.append(f"Vol ok({rvol}x)")
    else:             reasons.append(f"LowVol({rvol}x)❌")

    reason_str = " | ".join(reasons)

    if sentiment == "BULLISH" and bull_tech:
        return "✅ BUY", reason_str
    elif sentiment == "BEARISH" and bear_tech:
        return "❌ SELL", reason_str
    elif sentiment == "BULLISH" and not bull_tech:
        return "⏳ WAIT", f"News bullish but tech weak: {reason_str}"
    elif sentiment == "BEARISH" and not bear_tech:
        return "⏳ WAIT", f"News bearish but tech holds: {reason_str}"
    else:
        return "⏳ WAIT", reason_str


# ─────────────────────────────────────────────
#  MAIN SCAN
# ─────────────────────────────────────────────

def run_news_scan(live_token=None):
    """
    Scrape all sources, score headlines, fetch technicals for
    Indian stocks only, generate BUY/SELL/WAIT decisions.

    Returns structured result dict.
    """
    all_headlines = []
    errors        = []

    for source_name, cfg in SOURCES.items():
        try:
            if cfg["type"] == "html":
                heads = fetch_html_headlines(cfg["url"])
            elif cfg["type"] == "rss":
                heads = fetch_rss_headlines(cfg["url"])
            elif cfg["type"] == "nse_json":
                heads = fetch_nse_announcements()
            else:
                heads = []

            for h in heads:
                all_headlines.append((h, cfg["region"], cfg["weight"], source_name))
            log.info(f"{source_name}: {len(heads)} headlines")
        except Exception as e:
            errors.append(f"{source_name}: {e}")
            log.error(f"Source {source_name} failed: {e}")

    # Deduplicate
    seen, unique_hl = set(), []
    for h, region, weight, source in all_headlines:
        key = h[:60].lower()
        if key not in seen:
            seen.add(key)
            unique_hl.append((h, region, weight, source))

    short_term, long_term, macro = [], [], []

    for h, region, weight, source in unique_hl:
        sig = score_headline(h, region, weight)
        if sig is None:
            continue
        sig["source"] = source

        # Global signals → macro context only (no stock recommendations)
        if region == "GLOBAL":
            if sig["sentiment"] == "MACRO":
                macro.append(sig)
            # Skip GLOBAL stock signals — only Indian stocks recommended
            continue

        # Indian signals
        if sig["sentiment"] == "MACRO":
            macro.append(sig)
        elif sig["horizon"] == "SHORT":
            short_term.append(sig)
        elif sig["horizon"] == "LONG":
            long_term.append(sig)
        elif sig["horizon"] == "BOTH":
            short_term.append(sig)
            long_term.append(sig)

    # Dedup per stock, top N
    def dedup_by_stock(signals, max_per_stock=2, top_n=10):
        stock_count, result = {}, []
        for s in sorted(signals, key=lambda x: -x["conf"]):
            stocks = s["stocks"] or []
            if not stocks:
                continue  # skip signals with no identified Indian stock
            for ticker, key in stocks:
                count = stock_count.get(ticker, 0)
                if count < max_per_stock:
                    stock_count[ticker] = count + 1
                    result.append(s)
                    break
            if len(result) >= top_n:
                break
        return result

    short_term = dedup_by_stock(short_term, top_n=8)
    long_term  = dedup_by_stock(long_term,  top_n=8)
    macro      = sorted(macro, key=lambda x: -x["conf"])[:5]

    # ── Fetch technicals for Indian stocks ──
    tech_cache = {}
    if live_token:
        all_tickers = {}
        for sig in short_term + long_term:
            for ticker, key in sig.get("stocks", []):
                all_tickers[ticker] = key
        for ticker, key in all_tickers.items():
            log.info(f"Fetching technicals: {ticker}")
            tech_cache[ticker] = get_technicals(ticker, key, live_token)

    # ── Attach decisions ──
    for sig in short_term + long_term:
        stocks = sig.get("stocks", [])
        if stocks:
            ticker, _ = stocks[0]
            tech      = tech_cache.get(ticker)
            decision, reason = make_decision(sig["sentiment"], tech)
            sig["decision"] = decision
            sig["tech_reason"] = reason
            sig["tech"] = tech
        else:
            sig["decision"]    = "⏳ WAIT"
            sig["tech_reason"] = "No stock identified"
            sig["tech"]        = None

    return {
        "short_term"   : short_term,
        "long_term"    : long_term,
        "macro"        : macro,
        "raw_count"    : len(all_headlines),
        "scored_count" : len(short_term) + len(long_term) + len(macro),
        "timestamp"    : datetime.now().strftime("%Y-%m-%d %H:%M IST"),
        "errors"       : errors,
    }


# ─────────────────────────────────────────────
#  TELEGRAM FORMATTER
# ─────────────────────────────────────────────

def _conf_bar(conf):
    return "⬛" * conf + "⬜" * (5 - conf)

def _sentiment_icon(sentiment):
    return "📈" if sentiment == "BULLISH" else "📉" if sentiment == "BEARISH" else "🌐"


def format_news_report(report, max_short=6, max_long=5, max_macro=4):
    lines = [
        f"📰 <b>STOCK NEWS SCAN — {report['timestamp']}</b>",
        f"  {report['raw_count']} headlines → {report['scored_count']} signals",
        "",
    ]

    # ── Global Macro ────────────────────────
    if report["macro"]:
        lines.append("🌐 <b>GLOBAL MACRO</b>")
        for s in report["macro"][:max_macro]:
            lines.append(
                f"  {_conf_bar(s['conf'])} [{s['catalyst']}] "
                f"{s['headline'][:90]}"
            )
        lines.append("")

    # ── Short Term Indian Stocks ─────────────
    if report["short_term"]:
        lines.append("⚡ <b>SHORT TERM — INDIAN STOCKS (1–5 days)</b>")
        for s in report["short_term"][:max_short]:
            tickers = ", ".join(t for t, _ in s["stocks"]) if s["stocks"] else "—"
            icon    = _sentiment_icon(s["sentiment"])
            tech    = s.get("tech")
            decision = s.get("decision", "⏳ WAIT")

            lines.append(
                f"\n  {decision} | {icon} <b>{tickers}</b> "
                f"[{s['catalyst']}] conf:{_conf_bar(s['conf'])}"
            )
            lines.append(f"  📰 {s['headline'][:85]}")

            if tech:
                lines.append(
                    f"  📊 LTP:{tech['ltp']:.0f} | EMA9:{tech['ema9']:.0f} "
                    f"EMA21:{tech['ema21']:.0f} | RSI:{tech['rsi']:.0f} "
                    f"| VWAP:{tech['vwap']:.0f} | Vol:{tech['rvol']}x"
                )
            lines.append(f"  💡 {s.get('tech_reason','')[:100]}")
        lines.append("")

    # ── Long Term Indian Stocks ──────────────
    if report["long_term"]:
        lines.append("📅 <b>LONG TERM — INDIAN STOCKS (3–12 months)</b>")
        for s in report["long_term"][:max_long]:
            tickers  = ", ".join(t for t, _ in s["stocks"]) if s["stocks"] else "—"
            icon     = _sentiment_icon(s["sentiment"])
            decision = s.get("decision", "⏳ WAIT")
            tech     = s.get("tech")

            lines.append(
                f"\n  {decision} | {icon} <b>{tickers}</b> "
                f"[{s['catalyst']}] conf:{_conf_bar(s['conf'])}"
            )
            lines.append(f"  📰 {s['headline'][:85]}")
            if tech:
                lines.append(
                    f"  📊 LTP:{tech['ltp']:.0f} | RSI:{tech['rsi']:.0f} "
                    f"| VWAP:{tech['vwap']:.0f} | Vol:{tech['rvol']}x"
                )
            lines.append(f"  💡 {s.get('tech_reason','')[:100]}")
        lines.append("")

    if not report["short_term"] and not report["long_term"] and not report["macro"]:
        lines.append("  No strong signals found this scan.")

    if report["errors"]:
        lines.append(f"  ⚠️ {len(report['errors'])} source(s) failed")

    lines.append("  Send /news to refresh anytime")
    return "\n".join(lines)


def format_news_report_short(report):
    """Compact version for embedding in daily bias message."""
    lines = ["📰 <b>Top Stock Signals:</b>"]
    for s in report["short_term"][:3]:
        tickers  = ", ".join(t for t, _ in s["stocks"]) if s["stocks"] else "Nifty"
        icon     = "📈" if s["sentiment"] == "BULLISH" else "📉"
        decision = s.get("decision", "⏳")
        lines.append(f"  {icon} {decision} ⚡ {tickers} — {s['headline'][:60]}")
    for s in report["long_term"][:2]:
        tickers  = ", ".join(t for t, _ in s["stocks"]) if s["stocks"] else "Sector"
        icon     = "📈" if s["sentiment"] == "BULLISH" else "📉"
        decision = s.get("decision", "⏳")
        lines.append(f"  {icon} {decision} 📅 {tickers} — {s['headline'][:60]}")
    if not report["short_term"] and not report["long_term"]:
        lines.append("  No strong signals today")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  STANDALONE RUN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    print("Running news scan with technicals...")
    report = run_news_scan(live_token=config.LIVE_TOKEN)
    msg    = format_news_report(report)
    print(msg)
    print(f"\nShort: {len(report['short_term'])} | "
          f"Long: {len(report['long_term'])} | "
          f"Macro: {len(report['macro'])}")

    # ── Send to Telegram ──
    try:
        # Split if message too long for Telegram (4096 char limit)
        max_len = 4000
        parts   = [msg[i:i+max_len] for i in range(0, len(msg), max_len)]
        url     = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        for part in parts:
            resp = requests.post(url, data={
                "chat_id"   : config.CHAT_ID,
                "text"      : part,
                "parse_mode": "HTML"
            }, timeout=10)
            if resp.status_code == 200:
                print(f"Telegram sent ✅ ({len(part)} chars)")
            else:
                print(f"Telegram error: {resp.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")
