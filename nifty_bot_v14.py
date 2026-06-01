"""
═══════════════════════════════════════════════════════════════════════════
  nifty_bot_v14.py — Live Loop (paper trading) wiring the v14 decision chain
═══════════════════════════════════════════════════════════════════════════

  FLOW EACH CYCLE
    live_feeds  → fetch LTP, candles, VIX, expiry, gap
    signals.py  → indicators, regime, 12 detectors (DETECTION)
    decision_layer.decide_entry → day/vix/expiry/chop/regime/capital/lockout/
                  pricing/edge gates  (DECISION)
    LongOptionPosition → realistic ask-entry / bid-exit, vol-scaled target+hold
    Telegram + CSV logging of every parameter

  TWO-STRUCTURE RESPECTED
    Detection lives in signals.py. Decisions live in the gate layer. This file
    only ORCHESTRATES: fetch → classify → decide → execute → log.

  PAPER TRADING ONLY. Places no orders. Profit when LONG premium rises (CE & PE).
═══════════════════════════════════════════════════════════════════════════
"""

import time
import logging
import datetime
import csv
import os
import json
import requests
import numpy as np
import pandas as pd
import pytz

import signals
import config
import live_feeds_v14 as feeds
import event_calendar_v14 as events
from decision_layer_v14 import decide_entry, CapitalLadder, ReentryLockout
from trade_engine_v14 import OptionQuote, LongOptionPosition, sell_fill_from_bid

# ─────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    handlers=[logging.FileHandler("nifty_v14.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

NIFTY_KEY = "NSE_INDEX|Nifty 50"
IST = pytz.timezone("Asia/Kolkata")

TRADE_START = datetime.time(9, 30)
TRADE_END = datetime.time(14, 30)
HARD_CLOSE = datetime.time(15, 10)
SCAN_SLEEP = 30                 # seconds between cycles
TELEGRAM_SCAN_INTERVAL = 300    # 5-min market scan alerts
MAX_LOTS = 2
MIN_OI = 50000
MIN_QTY = 25
MAX_SPREAD_PCT = 0.08

SIGNAL_PRIORITY = [
    "StrongFVG", "BOS", "EMAStack", "VWAPBand", "VWAPCross",
    "EMA50Bounce", "EMACross", "SuperTrend", "CPR", "RSIDivergence", "ORPH_ORPL",
]
STRONG_BREAKOUT_STRATS = {"StrongFVG", "BOS"}

SCAN_COLS = ["datetime","nifty_ltp","atm","regime","rsi","atr","vix",
             "trend","efficiency","signal","decision","size","reasons"]
TRADE_COLS = ["trade_no","ts","strategy","direction","entry_nifty","entry_premium",
              "lots","sl","target","target_mode","hold_min","day_type",
              "entry_rsi","entry_atr","entry_regime","vix","strike","opt_type",
              "iv","delta","spread","confidence","exit_ts","exit_premium","duration_min",
              "exit_reason","mfe_pts","mae_pts","pts","pnl","result","reasons"]
# [FIX 1] OPEN log: written at entry so an interrupted trade is never lost.
OPEN_COLS = ["trade_no","entry_ts","strategy","direction","entry_nifty",
             "entry_premium","lots","sl","target","hold_min","strike","opt_type",
             "iv","delta","vix","size_mult","confidence"]
# Skip log: blocked signals broken out separately (per request) for easy review.
SKIP_COLS = ["datetime","signal","direction","regime","atr","vix","size","reasons"]


def now_ist():
    return datetime.datetime.now(IST)

def ist_time():
    return now_ist().time()

def _h():
    return {"Accept": "application/json",
            "Authorization": f"Bearer {config.LIVE_TOKEN}"}

# ── Telegram ───────────────────────────────────────────────────────────────
def tg_on():
    return (hasattr(config, "BOT_TOKEN") and config.BOT_TOKEN
            and hasattr(config, "CHAT_ID") and config.CHAT_ID
            and "your_" not in str(config.BOT_TOKEN))

def tg(msg):
    if not tg_on():
        return
    try:
        requests.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                      data={"chat_id": config.CHAT_ID, "text": msg,
                            "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        log.warning(f"tg fail: {e}")

def tg_document(path, caption=""):
    """Send a file to Telegram (used for end-of-day log delivery)."""
    if not tg_on() or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument",
                data={"chat_id": config.CHAT_ID, "caption": caption},
                files={"document": (os.path.basename(path), f)}, timeout=30)
        if r.status_code == 200:
            return True
        log.warning(f"tg_document {os.path.basename(path)}: HTTP {r.status_code} {r.text[:100]}")
    except Exception as e:
        log.warning(f"tg_document fail {path}: {e}")
    return False

def send_session_files():
    """[REQUEST] At session end, send every same-day log file to Telegram."""
    if not tg_on():
        return
    d = datetime.date.today().strftime("%Y-%m-%d")
    # all v14 files dated today, in a sensible order
    candidates = [f"trade_v14_{d}.csv", f"skip_v14_{d}.csv",
                  f"open_v14_{d}.csv", f"scan_v14_{d}.csv", "nifty_v14.log"]
    sent = 0
    for fn in candidates:
        if os.path.exists(fn):
            rows = ""
            try:
                if fn.endswith(".csv"):
                    n = sum(1 for _ in open(fn)) - 1   # minus header
                    rows = f" ({max(0,n)} rows)"
            except Exception:
                pass
            if tg_document(fn, caption=f"📎 {fn}{rows}"):
                sent += 1
    tg(f"📂 Sent {sent} log file(s) for {d}.")

# ── Data fetch ───────────────────────────────────────────────────────────────
def get_ltp():
    try:
        r = requests.get("https://api.upstox.com/v3/market-quote/ltp",
                         params={"instrument_key": NIFTY_KEY}, headers=_h(), timeout=5)
        if r.status_code == 200:
            for v in r.json().get("data", {}).values():
                if v.get("last_price"):
                    return float(v["last_price"])
    except Exception as e:
        log.error(f"ltp: {e}")
    return None

def get_candles(interval=5):
    try:
        url = f"https://api.upstox.com/v3/historical-candle/intraday/{NIFTY_KEY}/minutes/{interval}"
        r = requests.get(url, headers=_h(), timeout=10)
        if r.status_code == 200 and r.json().get("status") == "success":
            c = r.json()["data"]["candles"]
            if not c:
                return None
            df = pd.DataFrame(c, columns=["timestamp","open","high","low","close","volume","oi"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            for col in ["open","high","low","close","volume","oi"]:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            return df
    except Exception as e:
        log.error(f"candles {interval}: {e}")
    return None

def get_chain(expiry):
    try:
        r = requests.get("https://api.upstox.com/v2/option/chain",
                         params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
                         headers=_h(), timeout=12)
        if r.status_code == 200 and r.json().get("status") == "success":
            return r.json().get("data", [])
    except Exception as e:
        log.error(f"chain: {e}")
    return None

# ── Build an OptionQuote from the live chain at the chosen strike ───────────
def build_quote(chain, atm, opt_type):
    """Pick the most liquid of ATM, ATM±100 for the given side."""
    best = None
    for row in chain or []:
        sp = row.get("strike_price")
        if sp not in (atm - 100, atm, atm + 100):
            continue
        side = "call_options" if opt_type == "CE" else "put_options"
        opt = row.get(side, {})
        if not opt:
            continue
        md = opt.get("market_data", {})
        gk = opt.get("option_greeks", {})
        bid, ask = md.get("bid_price", 0), md.get("ask_price", 0)
        if not bid or not ask:
            continue
        q = OptionQuote(
            strike=sp, opt_type=opt_type, bid=bid, ask=ask,
            bid_qty=md.get("bid_qty", 0), ask_qty=md.get("ask_qty", 0),
            oi=md.get("oi", 0), delta=gk.get("delta", 0.5 if opt_type=="CE" else -0.5),
            iv=gk.get("iv", 0), instr_key=opt.get("instrument_key", ""),
        )
        if best is None or q.oi > best.oi:
            best = q
    return best

def current_bid(chain, strike, opt_type):
    for row in chain or []:
        if row.get("strike_price") == strike:
            side = "call_options" if opt_type == "CE" else "put_options"
            md = row.get(side, {}).get("market_data", {})
            return md.get("bid_price", 0)
    return 0

# ── Indicators + signal detection (orchestrate signals.py) ──────────────────
def compute_indicators(df5, df15, df30):
    ind = {}
    try:
        ind["atr"] = signals.calc_atr(df5)
        ind["rsi"] = signals.calc_rsi(df5)
        dfe = signals.calc_ema(df5)
        if "ema9" in dfe.columns:
            ind["e9"] = float(dfe["ema9"].iloc[-1])
            ind["e21"] = float(dfe["ema21"].iloc[-1])
            ind["e50"] = float(dfe["ema50"].iloc[-1])
            ind["df_ema"] = dfe
        vb = signals.calc_vwap_bands(df5, ind.get("atr", 30))
        if len(vb):
            ind["vwap"] = float(vb.iloc[-1].get("vwap", 0))
        t5, _, _ = signals.detect_trend_relaxed(df5)
        trend, _, strength = signals.detect_trend_multi(
            df5, df15, df30, ind.get("e9",0), ind.get("e21",0), ind.get("e50",0), 0)
        ind["t5"], ind["trend"], ind["trend_strength"] = t5, trend, strength
        ind["regime"] = signals.classify_intraday_regime(
            df5, ind.get("e9",0), ind.get("e21",0), ind.get("atr",30))
    except Exception as e:
        log.error(f"indicators: {e}")
    return ind

def detect_signal(df5, df15, ind, ltp, prev_ohlc):
    """Run detectors in priority order; return (name, direction, strong)."""
    atr = ind.get("atr", 30); t5 = ind.get("t5","neutral")
    trend = ind.get("trend","neutral")
    try:
        fvg, _ = signals.detect_fvg(df5)
        if fvg:
            return "StrongFVG", fvg.get("type"), True
        bos, _ = signals.detect_bos(df5, ltp)
        if bos:
            return "BOS", bos.get("type"), True
        if "df_ema" in ind:
            stk, _ = signals.detect_ema_stack(ind["df_ema"], ltp, t5, ind.get("rvol",1.0))
            if stk:
                return "EMAStack", stk.get("type"), False
        st, _ = signals.detect_supertrend_signal(df5, trend, ltp, atr)
        if st:
            return "SuperTrend", st.get("type"), False
        rdiv, _ = signals.detect_rsi_divergence(df5, None)
        if rdiv:
            return "RSIDivergence", rdiv.get("type"), False
    except Exception as e:
        log.error(f"detect: {e}")
    return None, None, False

# ── State + logging ──────────────────────────────────────────────────────────
def load_state():
    if os.path.exists("state_v14.json"):
        try:
            return json.load(open("state_v14.json"))
        except Exception:
            pass
    return {"date": str(datetime.date.today()), "trades":0, "wins":0,
            "losses":0, "timeouts":0, "pnl":0.0}

def save_state(s):
    try:
        json.dump(s, open("state_v14.json","w"), indent=2)
    except Exception as e:
        log.error(f"state save: {e}")

def init_logs():
    d = datetime.date.today().strftime("%Y-%m-%d")
    for fn, cols in [(f"scan_v14_{d}.csv", SCAN_COLS), (f"trade_v14_{d}.csv", TRADE_COLS),
                     (f"open_v14_{d}.csv", OPEN_COLS), (f"skip_v14_{d}.csv", SKIP_COLS)]:
        if not os.path.exists(fn):
            csv.DictWriter(open(fn,"w",newline=""), fieldnames=cols).writeheader()

def log_scan(rec):
    d = datetime.date.today().strftime("%Y-%m-%d")
    w = csv.DictWriter(open(f"scan_v14_{d}.csv","a",newline=""), fieldnames=SCAN_COLS)
    w.writerow({c: rec.get(c,"") for c in SCAN_COLS})

def log_trade(rec):
    d = datetime.date.today().strftime("%Y-%m-%d")
    w = csv.DictWriter(open(f"trade_v14_{d}.csv","a",newline=""), fieldnames=TRADE_COLS)
    w.writerow({c: rec.get(c,"") for c in TRADE_COLS})

def log_open(pos, decision, vix):
    """[FIX 1] Write an OPEN row at entry so a crash/restart can't lose a trade."""
    d = datetime.date.today().strftime("%Y-%m-%d")
    rec = {"trade_no":pos.trade_no, "entry_ts":getattr(pos,"entry_ts_str",""),
           "strategy":pos.strategy, "direction":pos.direction,
           "entry_nifty":round(pos.entry_nifty,1), "entry_premium":round(pos.entry_premium,2),
           "lots":pos.lots, "sl":round(pos.sl_price,2), "target":round(pos.target_price,2),
           "hold_min":pos.hold_min, "strike":pos.option_strike, "opt_type":pos.option_type,
           "iv":round(pos.option_iv,2), "delta":round(pos.option_delta,3),
           "vix":vix, "size_mult":round(decision.size_mult,2),
           "confidence":getattr(pos,"confidence",0)}
    w = csv.DictWriter(open(f"open_v14_{d}.csv","a",newline=""), fieldnames=OPEN_COLS)
    w.writerow({c: rec.get(c,"") for c in OPEN_COLS})

def log_skip(rec):
    """Blocked signals, broken out separately for easy review (per request)."""
    d = datetime.date.today().strftime("%Y-%m-%d")
    w = csv.DictWriter(open(f"skip_v14_{d}.csv","a",newline=""), fieldnames=SKIP_COLS)
    w.writerow({c: rec.get(c,"") for c in SKIP_COLS})

# ── Main loop ────────────────────────────────────────────────────────────────
def run():
    log.info("="*70)
    log.info("Nifty Bot v14 — STARTED (v14 decision chain, paper trading)")
    log.info("="*70)
    init_logs()
    state = load_state()
    cap = CapitalLadder()
    lock = ReentryLockout()
    pos = None
    trade_no = state["trades"]
    last_tg_scan = time.time() - TELEGRAM_SCAN_INTERVAL
    day_atr_low = 0.0
    day_atr_high = 0.0
    sess_open = None
    sess_high = None
    sess_low = None

    # Session-open context (fetched once)
    expiry = feeds.get_nearest_expiry()
    vix_open = feeds.get_india_vix()
    prev_ohlc = feeds.get_prev_day_ohlc()
    today = datetime.date.today()
    ev_today, ev_label = events.is_event_day(today)

    if tg_on():
        up = events.upcoming_events(today, 7)
        ev_txt = f"\nEvent today: {ev_label}" if ev_today else ""
        up_txt = ("\nUpcoming: " + ", ".join(f"{l}(+{d}d)" for _,l,d in up)) if up else ""
        tg(f"🤖 <b>NIFTY BOT v14 STARTED</b>\nExpiry: {expiry} | VIX open: {vix_open}{ev_txt}{up_txt}")

    while True:
        try:
            t = ist_time()
            now = now_ist()
            if t >= TRADE_END and pos is None:
                log.info("Session end. Exiting.")
                if tg_on():
                    wr = state["wins"]/state["trades"]*100 if state["trades"] else 0
                    tg(f"📊 <b>SESSION DONE</b>\nTrades {state['trades']} | "
                       f"W{state['wins']} L{state['losses']} T{state['timeouts']} | "
                       f"WR {wr:.0f}%\nP&L Rs.{state['pnl']:+.0f}")
                    send_session_files()   # [REQUEST] deliver today's logs
                save_state(state)
                break
            if t < TRADE_START:
                time.sleep(SCAN_SLEEP); continue

            ltp = get_ltp()
            df5, df15, df30 = get_candles(5), get_candles(15), get_candles(30)
            if ltp is None or df5 is None or len(df5) < 6:
                time.sleep(SCAN_SLEEP); continue

            ind = compute_indicators(df5, df15, df30)
            vix = feeds.get_india_vix()
            atm = int(round(ltp/50)*50)
            closes = df5["close"].astype(float).tolist()[-30:]
            gap_pct = feeds.compute_gap_pct(df5["open"].iloc[0] if len(df5) else ltp, prev_ohlc)

            # [FIX 2] track the day's observed ATR range for relative trend gating
            _atr_now = ind.get("atr", 0)
            if _atr_now > 0:
                day_atr_low = min(day_atr_low, _atr_now) if day_atr_low else _atr_now
                day_atr_high = max(day_atr_high, _atr_now)

            # track session open / high / low for richer scan context
            if sess_open is None:
                sess_open = ltp
            sess_high = ltp if sess_high is None else max(sess_high, ltp)
            sess_low = ltp if sess_low is None else min(sess_low, ltp)

            # 5-min Telegram scan — RICH version
            if time.time() - last_tg_scan >= TELEGRAM_SCAN_INTERVAL:
                trend = ind.get("trend", "neutral")
                emoji = "📈" if trend=="bullish" else "📉" if trend=="bearish" else "➡️"
                e9, e21, e50 = ind.get("e9",0), ind.get("e21",0), ind.get("e50",0)
                eff = signals.efficiency_ratio(closes)
                day_lo = sess_low if sess_low else ltp
                day_hi = sess_high if sess_high else ltp
                day_chg = ltp - sess_open if sess_open else 0
                chg_pct = (day_chg/sess_open*100) if sess_open else 0
                vol_lbl = ("HIGH" if ind.get("atr",0)>=25 else
                           "MOD" if ind.get("atr",0)>=18 else "LOW")
                chop_lbl = ("TREND" if eff>=0.4 else "MIXED" if eff>=0.25 else "CHOP")
                pos_line = ""
                if pos is not None:
                    cur_bid = current_bid(get_chain(expiry), pos.option_strike, pos.option_type)
                    upnl = (sell_fill_from_bid(cur_bid) - pos.entry_premium) * pos.lots * 65 if cur_bid else 0
                    pos_line = (f"\n🎯 <b>OPEN #{pos.trade_no}</b> {pos.option_type}{pos.option_strike} "
                                f"{pos.strategy}\n   entry {pos.entry_premium:.1f} → now {cur_bid:.1f} "
                                f"| uP&L Rs.{upnl:+.0f}")
                msg = (
                    f"{emoji} <b>SCAN {now:%H:%M}</b>  ({chop_lbl}/{vol_lbl} vol)\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"<b>Nifty</b> {ltp:.0f}  ({day_chg:+.0f} / {chg_pct:+.2f}%)\n"
                    f"   day {day_lo:.0f}–{day_hi:.0f}\n"
                    f"<b>Trend</b> {trend} ({ind.get('trend_strength','?')}) | <b>regime</b> {ind.get('regime')}\n"
                    f"<b>ATR</b> {ind.get('atr',0):.1f} (day {day_atr_low:.0f}–{day_atr_high:.0f}) | "
                    f"<b>eff</b> {eff:.2f}\n"
                    f"<b>RSI</b> {ind.get('rsi',0):.0f} | <b>VIX</b> {vix}\n"
                    f"<b>EMA</b> 9:{e9:.0f} 21:{e21:.0f} 50:{e50:.0f}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"Trades {state['trades']} (W{state['wins']} L{state['losses']} T{state['timeouts']}) "
                    f"| P&L Rs.{state['pnl']:+.0f}"
                    f"{pos_line}"
                )
                tg(msg)
                last_tg_scan = time.time()

            # ── MONITOR OPEN POSITION ──
            if pos is not None:
                chain = get_chain(expiry)
                bid = current_bid(chain, pos.option_strike, pos.option_type)
                if bid > 0:
                    # [FIX 3] does the live trend still agree with the position?
                    trend_ok = (ind.get("trend") == pos.direction)
                    reason, sell, dur = pos.check_exit(bid, trend_still_agrees=trend_ok)
                    if reason:
                        pnl, pts = pos.pnl(sell)
                        # "trail" = trailing-stop exit (a managed win/scratch).
                        # Classify by P&L sign so a profitable timeout/trail
                        # counts as a win, a negative one as a loss.
                        if reason == "target" or (reason in ("trail","timeout") and pnl > 0):
                            result = "win"
                        elif reason == "sl" or pnl < 0:
                            result = "loss"
                        else:
                            result = "timeout"
                        state["wins" if result=="win" else "losses" if result=="loss" else "timeouts"] += 1
                        state["pnl"] += pnl
                        cap.on_result(reason, pnl); lock.record(pos.strategy, reason)
                        log_trade({
                            "trade_no":pos.trade_no, "ts":getattr(pos,"entry_ts_str",now.strftime("%Y-%m-%d %H:%M:%S")),
                            "strategy":pos.strategy, "direction":pos.direction,
                            "entry_nifty":round(pos.entry_nifty,1), "entry_premium":round(pos.entry_premium,2),
                            "lots":pos.lots, "sl":round(pos.sl_price,2), "target":round(pos.target_price,2),
                            "target_mode":pos.plan.get("target_mode","pct"), "hold_min":pos.hold_min,
                            "day_type":pos.plan.get("day_type",""), "entry_rsi":round(pos.entry_rsi,1),
                            "entry_atr":round(pos.entry_atr,1), "entry_regime":pos.entry_trend,
                            "vix":vix, "strike":pos.option_strike, "opt_type":pos.option_type,
                            "iv":round(pos.option_iv,2), "delta":round(pos.option_delta,3),
                            "spread":round(pos.option_spread,2),
                            "confidence":getattr(pos,"confidence",0),
                            "exit_ts":now.strftime("%H:%M:%S"),
                            "exit_premium":round(sell,2), "duration_min":round(dur,1),
                            "exit_reason":reason,
                            "mfe_pts":pos.mfe_pts, "mae_pts":pos.mae_pts,
                            "pts":pts, "pnl":pnl, "result":result,
                            "reasons":"; ".join(pos.plan.get("reasons",[])),
                        })
                        if tg_on():
                            e = "✅" if pnl>=0 else "❌"
                            tg(f"{e} <b>EXIT #{pos.trade_no} {reason.upper()}</b>\n"
                               f"{pos.strategy} {pos.direction}\nRs.{pos.entry_premium:.2f}→{sell:.2f} "
                               f"= Rs.{pnl:+.0f} ({pts:+.1f})\n{dur:.1f}min")
                        log.info(f"EXIT #{pos.trade_no} {result} Rs.{pnl:+.0f}")
                        save_state(state)
                        pos = None
                time.sleep(SCAN_SLEEP); continue

            # ── LOOK FOR ENTRY ──
            # [FIX 3] Hard entry cutoff: no NEW entries after TRADE_END (14:30).
            # Existing positions are still monitored above; this only gates entries.
            sig_name, direction, strong = detect_signal(df5, df15, ind, ltp, prev_ohlc)
            decision = None
            if t >= TRADE_END:
                if sig_name:
                    log.info(f"Signal {sig_name} after {TRADE_END} cutoff — entry skipped")
            elif sig_name and direction in ("bullish","bearish"):
                opt_type = "CE" if direction=="bullish" else "PE"
                chain = get_chain(expiry)
                quote = build_quote(chain, atm, opt_type)
                if quote:
                    # confidence: simple structural score (bot-side, not signals)
                    conf = 7 if strong else 5
                    decision = decide_entry(
                        quote=quote, direction=direction, strategy_name=sig_name,
                        confidence=conf, atr_5m=ind.get("atr",30), recent_closes=closes,
                        now_time=t, regime=ind.get("regime","UNKNOWN"),
                        strong_breakout=(sig_name in STRONG_BREAKOUT_STRATS),
                        today=today, nearest_expiry=expiry, vix=vix, vix_open=vix_open,
                        gap_pct=gap_pct, is_event_day=ev_today,
                        capital=cap, lockout=lock,
                        min_oi=MIN_OI, min_qty=MIN_QTY, max_spread_pct=MAX_SPREAD_PCT,
                        atr_day_low=day_atr_low, atr_day_high=day_atr_high,
                    )
                    if not decision.enter:
                        # Blocked signal -> dedicated skip log (per request).
                        log_skip({"datetime":now.strftime("%Y-%m-%d %H:%M:%S"),
                                  "signal":sig_name, "direction":direction,
                                  "regime":ind.get("regime",""), "atr":round(ind.get("atr",0),1),
                                  "vix":vix, "size":round(decision.size_mult,2),
                                  "reasons":"; ".join(decision.reasons)})
                    if decision.enter:
                        trade_no += 1
                        lots = max(1, min(MAX_LOTS, round(MAX_LOTS * decision.size_mult)))
                        decision.plan["reasons"] = decision.reasons
                        decision.plan["day_type"] = decision.day_type
                        decision.plan["target_mode"] = decision.target_mode
                        pos = LongOptionPosition(
                            trade_no, sig_name, direction, ltp, quote,
                            decision.plan, lots, ind)
                        pos.confidence = conf   # record entry confidence for analysis
                        # [FIX 2] stamp the real ENTRY time on the position so it
                        # is never overwritten by the exit time later.
                        pos.entry_ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
                        state["trades"] += 1
                        # [FIX 1] write an OPEN row immediately so a restart or
                        # crash mid-trade can never make the position vanish.
                        log_open(pos, decision, vix)
                        if tg_on():
                            e = "🟢" if direction=="bullish" else "🔴"
                            tg(f"{e} <b>ENTRY #{trade_no} {sig_name}</b>\n{direction.upper()} "
                               f"{opt_type} {quote.strike}\nBuy Rs.{pos.entry_premium:.2f} "
                               f"SL {pos.sl_price:.2f} Tgt {pos.target_price:.2f}\n"
                               f"size {decision.size_mult:.2f}({lots}lot) hold {pos.hold_min:.0f}m "
                               f"VIX {vix}")
                        log.info(f"ENTRY #{trade_no} {sig_name} {direction} {opt_type}{quote.strike} "
                                 f"@{pos.entry_premium:.2f} size{decision.size_mult:.2f}")
                        save_state(state)

            # Log every scan with the decision audit trail
            log_scan({
                "datetime":now.strftime("%Y-%m-%d %H:%M:%S"), "nifty_ltp":round(ltp,1),
                "atm":atm, "regime":ind.get("regime",""), "rsi":round(ind.get("rsi",0),1),
                "atr":round(ind.get("atr",0),1), "vix":vix, "trend":ind.get("trend",""),
                "efficiency":signals.efficiency_ratio(closes),
                "signal":sig_name or "NONE",
                "decision":"ENTER" if (decision and decision.enter) else ("BLOCK" if decision else "no-signal"),
                "size":round(decision.size_mult,2) if decision else "",
                "reasons":"; ".join(decision.reasons) if decision else "",
            })

            time.sleep(SCAN_SLEEP)

        except KeyboardInterrupt:
            log.info("Stopped by user"); save_state(state)
            tg("⏹ <b>Bot stopped</b>"); break
        except Exception as e:
            log.error(f"loop: {e}", exc_info=True)
            save_state(state); time.sleep(SCAN_SLEEP)

if __name__ == "__main__":
    run()
