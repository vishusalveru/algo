"""
=============================================================
  stock_news_analyser.py — Multi-Source News Stock Analyser
  ─────────────────────────────────────────────────────────
  PURPOSE:
    Scrapes and analyses news from multiple sources to
    surface actionable stock signals for:
    - SHORT TERM  : 1–5 day momentum plays (intraday/swing)
    - LONG TERM   : 3–12 month fundamental catalyst plays

  SOURCES:
    National  : Moneycontrol, Economic Times, Business Standard,
                NSE announcements, BSE filings RSS
    Global    : Reuters Markets, Bloomberg (RSS), FT Markets RSS,
                Yahoo Finance RSS, Investing.com RSS

  OUTPUT:
    - Per-stock sentiment tag (BULLISH / BEARISH / NEUTRAL)
    - Horizon tag (SHORT / LONG / BOTH)
    - Catalyst tag (EARNINGS / UPGRADE / MACRO / TECHNICAL /
                    POLICY / M&A / SECTOR)
    - Confidence score (1–5)
    - Headline + source

  CALLED FROM:
    nifty_bot_v5.py at premarket and via /news Telegram command

  USAGE:
    from stock_news_analyser import run_news_scan, format_news_report
    report = run_news_scan()
    send_telegram(format_news_report(report))
=============================================================
"""

import re
import logging
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  SOURCES CONFIG
# ─────────────────────────────────────────────
SOURCES = {
    # ── National ────────────────────────────
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
        "weight" : 1.2,   # highest — official exchange data
    },
    # ── Global ──────────────────────────────
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
#  KEYWORD DICTIONARIES
# ─────────────────────────────────────────────

# Stock name → ticker mapping (expand as needed)
STOCK_ALIASES = {
    "reliance": "RELIANCE", "ril": "RELIANCE",
    "tcs": "TCS", "tata consultancy": "TCS",
    "infosys": "INFY", "infy": "INFY",
    "hdfc bank": "HDFCBANK", "hdfcbank": "HDFCBANK",
    "icici bank": "ICICIBANK", "icicibank": "ICICIBANK",
    "wipro": "WIPRO",
    "hcl tech": "HCLTECH", "hcltech": "HCLTECH",
    "bajaj finance": "BAJFINANCE",
    "bajaj auto": "BAJAJ-AUTO",
    "maruti": "MARUTI", "maruti suzuki": "MARUTI",
    "sun pharma": "SUNPHARMA", "sunpharma": "SUNPHARMA",
    "dr reddy": "DRREDDY", "drl": "DRREDDY",
    "cipla": "CIPLA",
    "divis": "DIVISLAB", "divis lab": "DIVISLAB",
    "asian paints": "ASIANPAINT",
    "nestle": "NESTLEIND",
    "hindustan unilever": "HINDUNILVR", "hul": "HINDUNILVR",
    "itc": "ITC",
    "kotak": "KOTAKBANK", "kotak bank": "KOTAKBANK",
    "axis bank": "AXISBANK",
    "sbi": "SBIN", "state bank": "SBIN",
    "power grid": "POWERGRID",
    "ntpc": "NTPC",
    "ongc": "ONGC",
    "bharti airtel": "BHARTIARTL", "airtel": "BHARTIARTL",
    "titan": "TITAN",
    "ultratech": "ULTRACEMCO", "ultratech cement": "ULTRACEMCO",
    "grasim": "GRASIM",
    "adani ports": "ADANIPORTS",
    "adani enterprises": "ADANIENT",
    "tata steel": "TATASTEEL",
    "jswsteel": "JSWSTEEL", "jsw steel": "JSWSTEEL",
    "hindalco": "HINDALCO",
    "eicher motors": "EICHERMOT",
    "hero motocorp": "HEROMOTOCO",
    "tech mahindra": "TECHM",
    "ltimindtree": "LTIM",
    "indusind bank": "INDUSINDBK",
    "m&m": "M&M", "mahindra": "M&M",
    "tata motors": "TATAMOTORS",
    "bpcl": "BPCL",
    "britannia": "BRITANNIA",
    "shree cement": "SHREECEM",
    "apollo hospitals": "APOLLOHOSP",
    "dmart": "DMART", "avenue supermarts": "DMART",
    "sbi life": "SBILIFE",
    "hdfc life": "HDFCLIFE",
    "sbi cards": "SBICARD",
    "tata consumer": "TATACONSUM",
    "tata power": "TATAPOWER",
    "coal india": "COALINDIA",
    "bse": "BSE",
}

# Bullish / Bearish catalyst keywords with horizon hints
BULL_KEYWORDS = {
    # Short-term catalysts
    "beat": ("BULLISH", "SHORT", "EARNINGS", 4),
    "beats": ("BULLISH", "SHORT", "EARNINGS", 4),
    "outperform": ("BULLISH", "SHORT", "UPGRADE", 3),
    "upgrade": ("BULLISH", "SHORT", "UPGRADE", 4),
    "buy rating": ("BULLISH", "SHORT", "UPGRADE", 4),
    "strong buy": ("BULLISH", "SHORT", "UPGRADE", 4),
    "buyback": ("BULLISH", "SHORT", "M&A", 3),
    "dividend": ("BULLISH", "SHORT", "EARNINGS", 2),
    "bonus": ("BULLISH", "SHORT", "EARNINGS", 2),
    "record high": ("BULLISH", "SHORT", "TECHNICAL", 3),
    "52-week high": ("BULLISH", "SHORT", "TECHNICAL", 3),
    "breakout": ("BULLISH", "SHORT", "TECHNICAL", 3),
    "surge": ("BULLISH", "SHORT", "TECHNICAL", 2),
    "rally": ("BULLISH", "SHORT", "TECHNICAL", 2),
    "profit jumps": ("BULLISH", "SHORT", "EARNINGS", 4),
    "net profit rises": ("BULLISH", "SHORT", "EARNINGS", 4),
    "revenue rises": ("BULLISH", "SHORT", "EARNINGS", 3),
    "q4 results beat": ("BULLISH", "SHORT", "EARNINGS", 5),
    "q3 results beat": ("BULLISH", "SHORT", "EARNINGS", 5),
    # Long-term catalysts
    "expansion": ("BULLISH", "LONG", "SECTOR", 3),
    "acquisition": ("BULLISH", "LONG", "M&A", 3),
    "merger": ("BULLISH", "LONG", "M&A", 3),
    "order win": ("BULLISH", "LONG", "EARNINGS", 4),
    "order book": ("BULLISH", "LONG", "EARNINGS", 3),
    "capex": ("BULLISH", "LONG", "SECTOR", 2),
    "capacity expansion": ("BULLISH", "LONG", "SECTOR", 3),
    "policy support": ("BULLISH", "LONG", "POLICY", 3),
    "government contract": ("BULLISH", "LONG", "POLICY", 4),
    "fdi": ("BULLISH", "LONG", "POLICY", 2),
    "production linked incentive": ("BULLISH", "LONG", "POLICY", 3),
    "pli": ("BULLISH", "LONG", "POLICY", 3),
    "target raised": ("BULLISH", "BOTH", "UPGRADE", 4),
    "target price raised": ("BULLISH", "BOTH", "UPGRADE", 4),
}

BEAR_KEYWORDS = {
    # Short-term catalysts
    "miss": ("BEARISH", "SHORT", "EARNINGS", 4),
    "misses": ("BEARISH", "SHORT", "EARNINGS", 4),
    "downgrade": ("BEARISH", "SHORT", "UPGRADE", 4),
    "sell rating": ("BEARISH", "SHORT", "UPGRADE", 4),
    "underperform": ("BEARISH", "SHORT", "UPGRADE", 3),
    "profit falls": ("BEARISH", "SHORT", "EARNINGS", 4),
    "net profit drops": ("BEARISH", "SHORT", "EARNINGS", 4),
    "revenue declines": ("BEARISH", "SHORT", "EARNINGS", 3),
    "q4 results miss": ("BEARISH", "SHORT", "EARNINGS", 5),
    "52-week low": ("BEARISH", "SHORT", "TECHNICAL", 3),
    "breakdown": ("BEARISH", "SHORT", "TECHNICAL", 3),
    "fall": ("BEARISH", "SHORT", "TECHNICAL", 1),
    "decline": ("BEARISH", "SHORT", "TECHNICAL", 1),
    "drop": ("BEARISH", "SHORT", "TECHNICAL", 1),
    "crash": ("BEARISH", "SHORT", "TECHNICAL", 3),
    "slump": ("BEARISH", "SHORT", "TECHNICAL", 2),
    "debt concern": ("BEARISH", "SHORT", "EARNINGS", 3),
    "margin pressure": ("BEARISH", "SHORT", "EARNINGS", 3),
    "price hike warning": ("BEARISH", "SHORT", "EARNINGS", 3),
    # Long-term catalysts
    "regulatory action": ("BEARISH", "LONG", "POLICY", 4),
    "sebi notice": ("BEARISH", "LONG", "POLICY", 5),
    "fraud": ("BEARISH", "LONG", "POLICY", 5),
    "investigation": ("BEARISH", "LONG", "POLICY", 4),
    "plant shutdown": ("BEARISH", "LONG", "SECTOR", 4),
    "losing market share": ("BEARISH", "LONG", "SECTOR", 3),
    "target cut": ("BEARISH", "BOTH", "UPGRADE", 4),
    "target price cut": ("BEARISH", "BOTH", "UPGRADE", 4),
    "delisting": ("BEARISH", "LONG", "M&A", 5),
}

# Global macro keywords → Nifty + sector impact
GLOBAL_MACRO = {
    "fed rate": ("MACRO", "BOTH", "MACRO"),
    "federal reserve": ("MACRO", "BOTH", "MACRO"),
    "us inflation": ("MACRO", "SHORT", "MACRO"),
    "us cpi": ("MACRO", "SHORT", "MACRO"),
    "crude oil": ("MACRO", "SHORT", "SECTOR"),
    "brent": ("MACRO", "SHORT", "SECTOR"),
    "dollar index": ("MACRO", "SHORT", "MACRO"),
    "rupee": ("MACRO", "SHORT", "MACRO"),
    "china gdp": ("MACRO", "LONG", "MACRO"),
    "global recession": ("MACRO", "LONG", "MACRO"),
    "trade war": ("MACRO", "LONG", "MACRO"),
    "tariff": ("MACRO", "BOTH", "POLICY"),
    "rbi rate": ("MACRO", "SHORT", "POLICY"),
    "rbi policy": ("MACRO", "SHORT", "POLICY"),
    "repo rate": ("MACRO", "SHORT", "POLICY"),
    "inflation data": ("MACRO", "SHORT", "MACRO"),
    "iip data": ("MACRO", "SHORT", "MACRO"),
    "gdp growth": ("MACRO", "LONG", "MACRO"),
    "fii buying": ("MACRO", "SHORT", "MACRO"),
    "fii selling": ("MACRO", "SHORT", "MACRO"),
    "dii buying": ("MACRO", "SHORT", "MACRO"),
}


# ─────────────────────────────────────────────
#  SCRAPERS
# ─────────────────────────────────────────────

def fetch_html_headlines(url, limit=40):
    """Scrape headlines from an HTML news page."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return []
        soup  = BeautifulSoup(resp.text, "html.parser")
        texts = []
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "a"], limit=200):
            text = tag.get_text(strip=True)
            if 20 < len(text) < 200:
                texts.append(text)
        return list(dict.fromkeys(texts))[:limit]
    except Exception as e:
        log.error(f"fetch_html_headlines {url}: {e}")
        return []


def fetch_rss_headlines(url, limit=30):
    """Fetch headlines from an RSS feed."""
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
            # Strip HTML tags from description
            if desc:
                desc = BeautifulSoup(desc, "html.parser").get_text(strip=True)
            combined = f"{title}. {desc[:100]}" if desc else title
            if combined:
                texts.append(combined[:200])
        return texts
    except Exception as e:
        log.error(f"fetch_rss_headlines {url}: {e}")
        return []


def fetch_nse_announcements(limit=20):
    """Fetch corporate announcements directly from NSE API."""
    try:
        resp = requests.get(
            "https://www.nseindia.com/api/corporate-announcements",
            headers={**HEADERS, "Referer": "https://www.nseindia.com"},
            params={"index": "equities"},
            timeout=12
        )
        data = resp.json()
        items = []
        for item in (data if isinstance(data, list) else data.get("data", []))[:limit]:
            symbol  = item.get("symbol", "")
            subject = item.get("subject", "") or item.get("desc", "")
            if symbol and subject:
                items.append(f"{symbol}: {subject[:150]}")
        return items
    except Exception as e:
        log.error(f"NSE announcements error: {e}")
        return []


# ─────────────────────────────────────────────
#  STOCK MENTION EXTRACTOR
# ─────────────────────────────────────────────

def extract_stock_mentions(headline):
    """
    Find any known stock names/tickers in a headline.
    Returns list of canonical ticker strings.
    """
    hl = headline.lower()
    found = set()
    for alias, ticker in STOCK_ALIASES.items():
        if alias in hl:
            found.add(ticker)
    # Also catch uppercase tickers like RELIANCE, TCS directly
    words = re.findall(r'\b[A-Z]{2,12}\b', headline)
    for w in words:
        if w in STOCK_ALIASES.values():
            found.add(w)
    return list(found)


def score_headline(headline, source_region="IN", source_weight=1.0):
    """
    Analyse a headline and return a signal dict or None.

    Returns:
    {
        "headline"  : str,
        "stocks"    : [ticker, ...],
        "sentiment" : "BULLISH" | "BEARISH" | "MACRO",
        "horizon"   : "SHORT" | "LONG" | "BOTH",
        "catalyst"  : "EARNINGS" | "UPGRADE" | "MACRO" | etc.,
        "conf"      : 1–5,
        "region"    : "IN" | "GLOBAL",
        "source"    : str,
    }
    """
    hl_lower = headline.lower()
    stocks   = extract_stock_mentions(headline)

    # Check bull keywords
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

    # Check bear keywords
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

    # Check global macro
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
#  MAIN SCAN
# ─────────────────────────────────────────────

def run_news_scan():
    """
    Scrape all sources, score headlines, deduplicate, and return
    structured results grouped by horizon.

    Returns dict:
    {
        "short_term" : [signal, ...],   # sorted by conf desc
        "long_term"  : [signal, ...],
        "macro"      : [signal, ...],
        "raw_count"  : int,
        "scored_count": int,
        "timestamp"  : str,
        "errors"     : [str, ...],
    }
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
            log.info(f"News source {source_name}: {len(heads)} headlines")
        except Exception as e:
            errors.append(f"{source_name}: {e}")
            log.error(f"News source {source_name} failed: {e}")

    # Deduplicate by first 60 chars
    seen      = set()
    unique_hl = []
    for h, region, weight, source in all_headlines:
        key = h[:60].lower()
        if key not in seen:
            seen.add(key)
            unique_hl.append((h, region, weight, source))

    # Score each headline
    short_term = []
    long_term  = []
    macro      = []

    for h, region, weight, source in unique_hl:
        sig = score_headline(h, region, weight)
        if sig is None:
            continue
        sig["source"] = source
        if sig["sentiment"] == "MACRO":
            macro.append(sig)
        elif sig["horizon"] == "SHORT":
            short_term.append(sig)
        elif sig["horizon"] == "LONG":
            long_term.append(sig)
        elif sig["horizon"] == "BOTH":
            short_term.append(sig)
            long_term.append(sig)

    # Sort by confidence desc, then dedupe per stock
    def dedup_by_stock(signals, max_per_stock=2, top_n=10):
        stock_count = {}
        result = []
        for s in sorted(signals, key=lambda x: -x["conf"]):
            stocks = s["stocks"] or ["_macro"]
            for ticker in (stocks or ["_macro"]):
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
    """Visual confidence bar: ⬛⬛⬛⬜⬜ for 3/5."""
    return "⬛" * conf + "⬜" * (5 - conf)

def _horizon_icon(horizon):
    return "⚡" if horizon == "SHORT" else "📅" if horizon == "LONG" else "⚡📅"

def _sentiment_icon(sentiment):
    return "📈" if sentiment == "BULLISH" else "📉" if sentiment == "BEARISH" else "🌐"

def format_news_report(report, max_short=5, max_long=5, max_macro=3):
    """Format the scan result into a Telegram-ready message."""
    lines = [
        f"📰 <b>STOCK NEWS SCAN — {report['timestamp']}</b>",
        f"  Sources: {report['raw_count']} headlines → {report['scored_count']} signals",
        "",
    ]

    # ── Macro / Global ──────────────────────
    if report["macro"]:
        lines.append("🌐 <b>GLOBAL MACRO</b>")
        for s in report["macro"][:max_macro]:
            lines.append(
                f"  {_conf_bar(s['conf'])} [{s['catalyst']}] "
                f"{s['headline'][:90]}"
            )
        lines.append("")

    # ── Short Term ──────────────────────────
    if report["short_term"]:
        lines.append("⚡ <b>SHORT TERM SIGNALS (1–5 days)</b>")
        for s in report["short_term"][:max_short]:
            tickers = ", ".join(s["stocks"]) if s["stocks"] else "—"
            icon    = _sentiment_icon(s["sentiment"])
            lines.append(
                f"  {icon} <b>{tickers}</b> [{s['catalyst']}] conf:{_conf_bar(s['conf'])}"
            )
            lines.append(f"    {s['headline'][:90]}")
        lines.append("")

    # ── Long Term ───────────────────────────
    if report["long_term"]:
        lines.append("📅 <b>LONG TERM SIGNALS (3–12 months)</b>")
        for s in report["long_term"][:max_long]:
            tickers = ", ".join(s["stocks"]) if s["stocks"] else "—"
            icon    = _sentiment_icon(s["sentiment"])
            lines.append(
                f"  {icon} <b>{tickers}</b> [{s['catalyst']}] conf:{_conf_bar(s['conf'])}"
            )
            lines.append(f"    {s['headline'][:90]}")
        lines.append("")

    if not report["short_term"] and not report["long_term"] and not report["macro"]:
        lines.append("  No strong signals found this scan.")

    if report["errors"]:
        lines.append(f"  ⚠️ {len(report['errors'])} source(s) failed (check log)")

    lines.append("  Send /news to refresh anytime")
    return "\n".join(lines)


def format_news_report_short(report):
    """
    Compact version for embedding in daily bias message —
    top 3 short + top 2 long only.
    """
    lines = ["📰 <b>Top Stock Signals:</b>"]
    for s in report["short_term"][:3]:
        tickers = ", ".join(s["stocks"]) if s["stocks"] else "Nifty"
        icon    = "📈" if s["sentiment"] == "BULLISH" else "📉"
        lines.append(f"  {icon} ⚡ {tickers} — {s['headline'][:60]}")
    for s in report["long_term"][:2]:
        tickers = ", ".join(s["stocks"]) if s["stocks"] else "Sector"
        icon    = "📈" if s["sentiment"] == "BULLISH" else "📉"
        lines.append(f"  {icon} 📅 {tickers} — {s['headline'][:60]}")
    if not report["short_term"] and not report["long_term"]:
        lines.append("  No strong signals today")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  STANDALONE RUN (for testing)
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    print("Running news scan...")
    report = run_news_scan()
    print(format_news_report(report))
    print(f"\nShort: {len(report['short_term'])} | Long: {len(report['long_term'])} | Macro: {len(report['macro'])}")
