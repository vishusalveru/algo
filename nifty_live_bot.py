"""
═══════════════════════════════════════════════════════════════════════════
  nifty_live_bot.py — LIVE trading bot (REAL ORDERS), FVG + BOS, 1 lot
═══════════════════════════════════════════════════════════════════════════

  ⚠️⚠️ THIS PLACES REAL ORDERS WITH REAL MONEY. ⚠️⚠️

  Spec: LIVE_BOT_README.md. Reuses the SAME detection (signals.py) and decision
  (decision_layer / day_context / trade_engine) modules as the paper bot, so
  signals/direction are identical to paper. The ONLY difference is execution:
  real orders via live_execution.py instead of simulated fills.

  HARD RULES (all enforced below):
    • StrongFVG signal ONLY. Any other signal is skipped.
    • 1 lot per trade. Never more.
    • Per-trade SL 20% (BROKER-SIDE SL-M) + dynamic 4-20% trailing target.
    • DAILY STOP: after 2 stop-loss hits, no new entries for the day.
    • No new entries before 09:45 or after 14:30. A position opened before
      14:30 runs to its target/SL even after 14:30.
    • Expiry day: trade the NEXT expiry (roll), never the expiring contract.

  MODE: set MODE='mock' to dry-run the whole loop with NO real orders (for a
  final logic check on the VM), or MODE='live' to place real orders.

  The operator (you) can intervene manually at any time. Kill = Ctrl-C or
  `kill $(cat live_bot.pid)`; that triggers a clean shutdown (open SL-M stays
  resting at the exchange to protect any open position).
═══════════════════════════════════════════════════════════════════════════
"""

import time, logging, datetime, csv, os, json
import requests
import pandas as pd
import pytz

import signals
import config
import live_feeds_v14 as feeds
import event_calendar_v14 as events
from decision_layer_v14 import decide_entry, CapitalLadder, ReentryLockout
from trade_engine_v14 import OptionQuote, LongOptionPosition, sell_fill_from_bid
from live_execution import LiveExecutor

# ─────────────────────────────────────────────────────────────────────────
MODE = "live"          # 'live' = REAL ORDERS | 'mock' = dry-run, no orders
STRATEGY = "StrongFVG"  # legacy ref; live now trades LIVE_STRATEGIES (FVG + BOS)
LOT_QTY = 65            # 1 Nifty lot
MAX_SL_HITS_PER_DAY = 2 # daily stop: 2 stop-loss hits -> done

# ── Release 2026-10-14 ───────────────────────────────────────────────────────
# Safety fixes (broker SL-M is the authority for stops, confirmed exits/cancels,
# monitor-before-fetch, persisted day state, fail-closed startup) are ALWAYS on.
# These two change WHICH trades are taken; each is a switch so its effect can
# be measured separately. Owner decision 2026-10-14: both ON in live.
RELEASE = "2026-10-14"
LIVE_FIX = {
    "seeded_indicators": True,   # ATR/RSI/EMA on prev session + today (D2/D3)
    "fvg_require_in_gap": True,  # FVG entry must still be inside its gap
}

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("nifty_live.log"), logging.StreamHandler()])
log = logging.getLogger(__name__)

NIFTY_KEY = "NSE_INDEX|Nifty 50"
IST = pytz.timezone("Asia/Kolkata")
TRADE_OPEN = datetime.time(9, 45)   # no entries before
TRADE_END  = datetime.time(14, 30)  # no NEW entries after
SCAN_SLEEP = 30
TELEGRAM_SCAN_INTERVAL = 300
MIN_OI, MIN_QTY, MAX_SPREAD_PCT = 50000, 25, 0.08

def now_ist(): return datetime.datetime.now(IST)
def ist_time(): return now_ist().time()
def _h(): return {"Accept":"application/json","Authorization":f"Bearer {config.LIVE_TOKEN}"}

# ── Telegram (same as paper) ────────────────────────────────────────────────
def tg_on():
    return (hasattr(config,"BOT_TOKEN") and config.BOT_TOKEN
            and hasattr(config,"CHAT_ID") and config.CHAT_ID
            and "your_" not in str(config.BOT_TOKEN))
def tg(msg):
    if not tg_on(): return
    try:
        requests.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            data={"chat_id":config.CHAT_ID,"text":msg,"parse_mode":"HTML"}, timeout=5)
    except Exception as e: log.warning(f"tg fail: {e}")

# ── Data fetch (reuse paper bot's exact logic) ──────────────────────────────
def get_ltp():
    try:
        r = requests.get("https://api.upstox.com/v3/market-quote/ltp",
            params={"instrument_key":NIFTY_KEY}, headers=_h(), timeout=5)
        if r.status_code==200:
            for v in r.json().get("data",{}).values():
                if v.get("last_price"): return float(v["last_price"])
    except Exception as e: log.error(f"ltp: {e}")
    return None

def get_candles(interval=5):
    try:
        url=f"https://api.upstox.com/v3/historical-candle/intraday/{NIFTY_KEY}/minutes/{interval}"
        r=requests.get(url, headers=_h(), timeout=10)
        if r.status_code==200 and r.json().get("status")=="success":
            c=r.json()["data"]["candles"]
            if not c: return None
            df=pd.DataFrame(c, columns=["timestamp","open","high","low","close","volume","oi"])
            df["timestamp"]=pd.to_datetime(df["timestamp"]); df=df.sort_values("timestamp").reset_index(drop=True)
            for col in ["open","high","low","close","volume","oi"]:
                df[col]=pd.to_numeric(df[col],errors="coerce").fillna(0)
            return df
    except Exception as e: log.error(f"candles {interval}: {e}")
    return None

def get_chain(expiry):
    try:
        r=requests.get("https://api.upstox.com/v2/option/chain",
            params={"instrument_key":NIFTY_KEY,"expiry_date":expiry}, headers=_h(), timeout=12)
        if r.status_code==200 and r.json().get("status")=="success":
            return r.json().get("data",[])
    except Exception as e: log.error(f"chain: {e}")
    return None

def build_quote(chain, atm, opt_type):
    best=None
    for row in chain or []:
        sp=row.get("strike_price")
        if sp not in (atm-100, atm, atm+100): continue
        side="call_options" if opt_type=="CE" else "put_options"
        opt=row.get(side,{}); md=opt.get("market_data",{}); gk=opt.get("option_greeks",{})
        bid,ask=md.get("bid_price",0),md.get("ask_price",0)
        if not bid or not ask: continue
        q=OptionQuote(strike=sp,opt_type=opt_type,bid=bid,ask=ask,
            bid_qty=md.get("bid_qty",0),ask_qty=md.get("ask_qty",0),oi=md.get("oi",0),
            delta=gk.get("delta",0.5 if opt_type=="CE" else -0.5),iv=gk.get("iv",0),
            instr_key=opt.get("instrument_key",""))
        if best is None or q.oi>best.oi: best=q
    return best

def current_bid(chain, strike, opt_type):
    for row in chain or []:
        if row.get("strike_price")==strike:
            side="call_options" if opt_type=="CE" else "put_options"
            return row.get(side,{}).get("market_data",{}).get("bid_price",0)
    return 0

def compute_indicators(df5, df15, df30, df_prev=None):
    """Indicators for the current cycle.

    [2026-10-14] ATR / RSI / EMA are computed on PREVIOUS SESSION + TODAY's
    candles (LIVE_FIX['seeded_indicators']). Before this, with today's candles
    only: ATR and RSI were placeholders (20.0 / 50.0) until ~10:20, and EMA50
    did not exist until ~13:25 — which made the regime classifier unable to
    say TRENDING_BULL (it saw EMA9 = EMA21 = 0), so live never took a bullish
    BOS before 13:25 (0 of 9) while bearish BOS traded freely (10 of 13).
    Detectors, the 12-candle regime window and VWAP still use TODAY only.
    If EMAs are still unavailable the regime is UNKNOWN (never computed from
    zero EMAs, which biased it bearish)."""
    ind = {}
    try:
        seed = feeds.seeded_frame(df_prev, df5) if LIVE_FIX["seeded_indicators"] else df5
        ind["hist_candles"] = len(seed)
        ind["atr"] = signals.calc_atr(seed); ind["rsi"] = signals.calc_rsi(seed)
        dfe = signals.calc_ema(seed)
        have_ema = "ema9" in dfe.columns
        if have_ema:
            ind["e9"] = float(dfe["ema9"].iloc[-1]); ind["e21"] = float(dfe["ema21"].iloc[-1])
            ind["e50"] = float(dfe["ema50"].iloc[-1]); ind["df_ema"] = dfe
        t5, _, _ = signals.detect_trend_relaxed(df5)
        trend, _, strength = signals.detect_trend_multi(df5, df15, df30,
            ind.get("e9", 0), ind.get("e21", 0), ind.get("e50", 0), 0)
        ind["t5"], ind["trend"], ind["trend_strength"] = t5, trend, strength
        if have_ema or not LIVE_FIX["seeded_indicators"]:
            ind["regime"] = signals.classify_intraday_regime(
                df5, ind.get("e9", 0), ind.get("e21", 0), ind.get("atr", 30))
        else:
            ind["regime"] = "UNKNOWN"
    except Exception as e: log.error(f"indicators: {e}")
    return ind

def detect_live_signal(df5, ltp, ind, day_atr_high=0):
    """LIVE bot trades BOS + StrongFVG.
    [2026-06-10] PRIORITY = BOS FIRST, then FVG (BOS higher quality per trade).
    BOS is a structure break (not exhaustion-filtered); FVG keeps its exhaustion
    filter. [2026-10-14] FVG must still be INSIDE its gap
    (LIVE_FIX['fvg_require_in_gap']); every FVG block is written to the live
    skip log. Returns (strategy, direction, detail) or (None, None, "")."""
    rsi = ind.get("rsi", 50)
    eff = ind.get("efficiency", None)
    atr = ind.get("atr", None)
    try:
        bos, why = signals.detect_bos(df5, ltp)
        if bos:
            return "BOS", bos.get("type"), why
    except Exception as e:
        log.error(f"bos detect: {e}")
    try:
        fvg, why = signals.detect_fvg(df5, require_in_gap=LIVE_FIX["fvg_require_in_gap"])
        if fvg:
            direction = fvg.get("type")
            block, ex = signals.fvg_exhaustion_block(direction, rsi, eff, atr, day_atr_high)
            if block:
                log.info(f"FVG {direction} BLOCKED (exhaustion): {', '.join(ex)}")
                log_live_skip("StrongFVG", direction, ind,
                              reasons=f"BLOCK: FVG exhaustion: {', '.join(ex)}; detector: {why}")
            else:
                return "StrongFVG", direction, why
    except Exception as e:
        log.error(f"fvg detect: {e}")
    return None, None, ""

LIVE_STRATEGIES = ("StrongFVG", "BOS")   # strategies the live bot may trade

# ── Live skip log [2026-10-14] — LOGGING ONLY ───────────────────────────────
# Live used to discard the reasons whenever a signal was blocked. This writes
# live_skip_<date>.csv (same fields as paper's skip log). A logging failure
# only warns; it can never stop or alter trading.
SKIP_COLS = ["datetime", "signal", "direction", "ltp", "regime", "atr",
             "efficiency", "rsi", "vix", "reasons"]

def log_live_skip(signal, direction, ind, ltp=None, vix=None, reasons=""):
    try:
        now = now_ist()
        fn = f"live_skip_{now:%Y-%m-%d}.csv"
        new = not os.path.exists(fn)
        if not isinstance(reasons, str):
            reasons = "; ".join(str(r) for r in reasons)
        with open(fn, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=SKIP_COLS)
            if new:
                w.writeheader()
            w.writerow({"datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "signal": signal, "direction": direction,
                        "ltp": "" if ltp is None else ltp,
                        "regime": ind.get("regime", ""), "atr": ind.get("atr", ""),
                        "efficiency": ind.get("efficiency", ""), "rsi": ind.get("rsi", ""),
                        "vix": "" if vix is None else vix, "reasons": reasons})
    except Exception as e:
        log.warning(f"live skip log failed: {e}")

# ── Trade log (same schema as before) ───────────────────────────────────────
TRADE_COLS=["trade_no","entry_ts","strategy","direction","entry_nifty","entry_premium",
    "actual_fill","slippage","lots","sl","sl_trigger","target","hold_min","strike","opt_type",
    "iv","delta","spread","confidence","exit_ts","exit_premium","actual_exit_fill",
    "duration_min","exit_reason","mfe_pts","mae_pts","pts","pnl","result",
    "entry_order_id","sl_order_id","exit_order_id","order_status","reasons",
    # execution-measurement fields (2026-06-17, desk-review priority):
    "exit_slippage","total_slippage","stop_efficiency"]

def _day(): return now_ist().strftime("%Y-%m-%d")      # [2026-10-14] IST date (VM is UTC)
def trade_file(): return f"live_trade_{_day()}.csv"
def state_file(): return f"live_state_{_day()}.json"

def init_logs():
    fn = trade_file()
    if not os.path.exists(fn):
        with open(fn, "w", newline="") as fh:
            csv.DictWriter(fh, fieldnames=TRADE_COLS).writeheader()

def log_trade(rec):
    with open(trade_file(), "a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=TRADE_COLS).writerow({c: rec.get(c, "") for c in TRADE_COLS})

# ── Day state survives restarts [2026-10-14] ────────────────────────────────
# The daily SL stop, re-entry lockout and capital ladder were memory-only: a
# restart after 2 SL hits reset them and the bot traded again. Now rebuilt at
# startup from today's trade CSV (the source of truth) + a small halt file.
def load_day_state(cap, lock):
    st = {"trade_no": 0, "sl_hits": 0, "day_pnl": 0.0, "halted": False, "halt_reason": ""}
    fn = trade_file()
    if os.path.exists(fn):
        with open(fn, newline="") as fh:
            rows = list(csv.DictReader(fh))
        nums = [int(float(r["trade_no"])) for r in rows if (r.get("trade_no") or "").strip()]
        st["trade_no"] = max(nums) if nums else 0
        for r in rows:
            reason = (r.get("exit_reason") or "").strip()
            if reason in ("", "OPEN"):
                continue
            pnl = float(r.get("pnl") or 0)
            st["day_pnl"] += pnl
            if reason == "sl":
                st["sl_hits"] += 1
            cap.on_result(reason, pnl); lock.record(r.get("strategy", ""), reason)
    if os.path.exists(state_file()):
        try:
            with open(state_file()) as fh:
                js = json.load(fh)
            st["halted"] = bool(js.get("halted")); st["halt_reason"] = js.get("halt_reason", "")
        except Exception as e:
            log.warning(f"state file unreadable: {e}")
    return st

def save_halt(reason):
    try:
        with open(state_file(), "w") as fh:
            json.dump({"halted": True, "halt_reason": reason, "at": now_ist().isoformat()}, fh)
    except Exception as e:
        log.warning(f"could not persist halt: {e}")

def _throttled(key, every_s=300, _last={}):
    """True at most once per `every_s` for a given key (alert de-spam)."""
    now = time.time()
    if now - _last.get(key, 0) >= every_s:
        _last[key] = now
        return True
    return False

# ── Main loop ────────────────────────────────────────────────────────────────
def run():
    log.info("="*60)
    banner = "🔴 LIVE MODE — REAL ORDERS" if MODE=="live" else "🟡 MOCK MODE — no real orders"
    log.info(f"NIFTY LIVE BOT {RELEASE} — {banner} — FVG + BOS, 1 LOT | fixes {LIVE_FIX}")
    log.info("="*60)
    init_logs()

    execu = LiveExecutor(token=getattr(config,"LIVE_TOKEN",""), mode=MODE)
    cap, lock = CapitalLadder(), ReentryLockout()
    pos = None
    pos_instr = None        # instrument_key of the open position
    sl_order_id = None      # resting broker SL-M order id ("" / None = no stop)
    entry_order_id = None
    consec_rejects = 0      # consecutive order rejections (e.g. IP block) -> pause
    REJECT_PAUSE_AT = 3     # after this many in a row, stop trying for the day
    last_tg = time.time() - TELEGRAM_SCAN_INTERVAL
    day_atr_low = day_atr_high = 0.0
    df_prev = None          # previous session's 5-min candles (indicator warm-up)
    last_seed_try = 0.0

    # ── Startup reconciliation — never start 'assuming flat' [hardened 2026-10-14]
    open_positions = execu.get_open_positions()
    pending = execu.get_pending_orders()
    pending_fo = [o for o in (pending or []) if str(o.get("instrument_token", "")).startswith("NSE_FO")]
    refuse = None
    if open_positions is None:
        refuse = "could not read positions from the broker (API error) — not starting blind"
    elif open_positions:
        refuse = f"broker shows {len(open_positions)} OPEN position(s)"
    elif pending is None:
        refuse = "could not read the order book (API error) — not starting blind"
    elif pending_fo:
        refuse = ("open F&O order(s) at the broker: " +
                  ", ".join(f"{o.get('order_id')} {o.get('order_type')} {o.get('status')}"
                            for o in pending_fo[:5]))
    if refuse:
        log.warning(f"STARTUP REFUSED: {refuse}")
        tg(f"⚠️ <b>LIVE BOT NOT STARTED</b>\n{refuse}.\nHandle it in Upstox, then restart.")
        return

    day = load_day_state(cap, lock)
    trade_no, sl_hits_today, day_pnl = day["trade_no"], day["sl_hits"], day["day_pnl"]
    halted, halt_reason = day["halted"], day["halt_reason"]

    expiry, nearest_expiry, rolled = feeds.get_tradeable_expiry()
    vix_open = feeds.get_india_vix()
    prev_ohlc = feeds.get_prev_day_ohlc()

    # [STARTUP FEED GUARD] dead feeds = expired token / blocked access -> refuse.
    test_ltp = get_ltp()
    if expiry is None or vix_open is None or test_ltp is None:
        msg = (f"🛑 <b>LIVE BOT CANNOT START — DEAD FEEDS</b>\n"
               f"Expiry={expiry} VIX={vix_open} LTP={test_ltp}\n"
               f"Almost certainly an EXPIRED/INVALID TOKEN (refresh it in "
               f"config.py) or blocked access. Bot is NOT running.")
        log.error(msg.replace("<b>","").replace("</b>",""))
        tg(msg)
        return

    today = now_ist().date()
    ev_today, _ = events.is_event_day(today)
    # [FIX 2026-10-14] nearest_expiry is a 'YYYY-MM-DD' string; it was compared to
    # a date object, so this guard could never fire.
    no_next = (rolled is False and nearest_expiry == today.strftime("%Y-%m-%d")
               and expiry == nearest_expiry)

    prev_txt = (f"prev close {prev_ohlc['close']:.0f} ({prev_ohlc.get('date','?')})"
                if prev_ohlc else "prev close UNAVAILABLE (gap gate off)")
    log.info(f"startup: expiry {expiry} rolled={rolled} | {prev_txt} | restored day state "
             f"trades={trade_no} sl_hits={sl_hits_today} pnl={day_pnl:+.0f} halted={halted}")
    roll_txt = f"\n⚠️ EXPIRY: trading NEXT expiry {expiry}" if rolled else ""
    if no_next: roll_txt = "\n🛑 EXPIRY + no next contract — NOT trading."
    halt_txt = f"\n⛔ HALTED today: {halt_reason}" if halted else ""
    tg(f"{('🔴 LIVE' if MODE=='live' else '🟡 MOCK')} <b>FVG+BOS BOT STARTED</b> ({RELEASE})\n"
       f"Expiry {expiry} | VIX {vix_open} | {prev_txt}\n"
       f"1 lot | 20% SL (broker) | daily stop {MAX_SL_HITS_PER_DAY} SLs\n"
       f"FIX: broker-SL authority, confirmed exits, seeded={LIVE_FIX['seeded_indicators']}, "
       f"fvg_in_gap={LIVE_FIX['fvg_require_in_gap']}\n"
       f"Today so far: trades {trade_no} | SL hits {sl_hits_today} | P&L Rs.{day_pnl:+.0f}"
       f"{roll_txt}{halt_txt}")

    def halt(reason):
        nonlocal halted, halt_reason
        if not halted:
            halted, halt_reason = True, reason
            save_halt(reason)
            log.error(f"HALT — no new entries today: {reason}")
            tg(f"⛔ <b>LIVE ENTRIES HALTED</b>\n{reason}")

    while True:
        try:
            t = ist_time(); now = now_ist(); today = now.date()

            # session end: stop ONLY when flat (open trade runs to its exit)
            if t >= TRADE_END and pos is None:
                log.info("Past 14:30 and flat. Session done.")
                tg(f"📊 <b>LIVE SESSION DONE</b>\nTrades {trade_no} | SL hits {sl_hits_today} | "
                   f"Day P&L Rs.{day_pnl:+.0f}")
                break
            if t < TRADE_OPEN and pos is None:
                time.sleep(SCAN_SLEEP); continue

            # indicator warm-up history: retry every 5 min until it loads
            if (LIVE_FIX["seeded_indicators"] and df_prev is None and prev_ohlc
                    and prev_ohlc.get("date") and time.time() - last_seed_try > 300):
                last_seed_try = time.time()
                df_prev = feeds.get_session_candles(prev_ohlc["date"], 5)
                log.info(f"indicator warm-up: {prev_ohlc['date']} 5-min candles = "
                         f"{0 if df_prev is None else len(df_prev)}")

            ltp = get_ltp()
            df5, df15, df30 = get_candles(5), get_candles(15), get_candles(30)
            data_ok = ltp is not None and df5 is not None and len(df5) >= 6
            ind = {}
            vix = None
            if data_ok:
                if df15 is None or len(df15) < 3: df15 = df5
                if df30 is None or len(df30) < 3: df30 = df5
                ind = compute_indicators(df5, df15, df30, df_prev)
                vix = feeds.get_india_vix()
                atm = int(round(ltp/50)*50)
                closes = df5["close"].astype(float).tolist()[-30:]
                gap_pct = feeds.compute_gap_pct(df5["open"].iloc[0] if len(df5) else ltp, prev_ohlc)
                _atr = ind.get("atr", 0)
                if _atr and _atr > 0:
                    day_atr_low = min(day_atr_low, _atr) if day_atr_low else _atr
                    day_atr_high = max(day_atr_high, _atr)
                if time.time() - last_tg >= TELEGRAM_SCAN_INTERVAL:
                    openln = ""
                    if pos is not None:
                        cb = current_bid(get_chain(expiry), pos.option_strike, pos.option_type)
                        if cb: openln = f"\n🎯 OPEN {pos.option_type}{pos.option_strike} now {cb:.1f} (entry {pos.entry_premium:.1f})"
                    tg(f"{'🔴' if MODE=='live' else '🟡'} {now:%H:%M} N{ltp:.0f} ATR{_atr:.0f} "
                       f"VIX{vix} | trades {trade_no} SLhits {sl_hits_today} PnL Rs.{day_pnl:+.0f}"
                       f"{' ⛔HALTED' if halted else ''}{openln}")
                    last_tg = time.time()

            # ── MONITOR OPEN POSITION [rebuilt 2026-10-14] ──
            # Runs BEFORE the data-ok check: an LTP/candle outage must never
            # leave a real position unmanaged (incl. the 15:25 backstop).
            if pos is not None:
                if getattr(pos, "manual_only", False):
                    if _throttled("manual", 600):
                        tg(f"🚨 <b>POSITION NEEDS MANUAL ACTION</b> #{pos.trade_no} "
                           f"{pos.option_type}{pos.option_strike} — bot is not sending orders for it.")
                    time.sleep(SCAN_SLEEP); continue
                dur = (time.time() - pos.start_ts) / 60.0
                reason = None; sell = 0.0
                exit_kind = None; exit_fill = 0.0; exit_oid = ""
                external = False    # position closed outside the bot (Upstox app)
                # 1) The BROKER's SL-M decides whether the stop was hit.
                if sl_order_id:
                    st = execu.order_status(sl_order_id)
                    if st.ok and st.status == "complete":
                        reason, exit_kind = "sl", "sl_filled"
                        exit_fill, exit_oid = (st.fill_price or pos.sl_price), sl_order_id
                    elif st.ok and st.status in ("cancelled", "rejected"):
                        held = execu.held_qty(pos_instr)
                        if held is not None and held < LOT_QTY:
                            external = True     # stop cancelled + position gone/reduced by hand
                        elif held is None and MODE == "live":
                            log.warning(f"broker stop {sl_order_id} is {st.status}; positions "
                                        f"unreadable — not re-placing blind, retry next cycle")
                        else:
                            r = execu.place_stop_loss(pos_instr, LOT_QTY, pos.sl_price, tag="sl-replace")
                            log.warning(f"broker stop {sl_order_id} is {st.status}; re-placed: {r.ok}")
                            tg(f"⚠️ <b>Broker stop was {st.status}</b> #{pos.trade_no} — "
                               f"{'re-placed' if r.ok else 'RE-PLACE FAILED, bot exits on bid'}")
                            sl_order_id = r.order_id if r.ok else None
                # 2) Bot-side exit rules (target / trail / timeout / backstop).
                if exit_kind is None and not external:
                    chain = get_chain(expiry)
                    bid = current_bid(chain, pos.option_strike, pos.option_type) if chain else 0
                    if not chain or not bid or bid <= 0:
                        # No price = no decision (was: bid 0 -> phantom "sl").
                        if _throttled("nobid", 120):
                            log.warning(f"no bid for open position (chain {'ok' if chain else 'NONE'}) "
                                        f"— skipping exit check this cycle")
                        # A market SELL does not need our bid: time-based exits
                        # still happen (hold expired, or the 15:25 backstop).
                        if dur >= pos.hold_min or t >= datetime.time(15, 25):
                            reason = "timeout"; sell = 0.0
                            log.warning(f"time exit WITHOUT a bid (dur {dur:.0f}m, {t}) — market sell")
                    else:
                        trend_ok = bool(ind) and ind.get("trend") == pos.direction
                        reason, sell, dur = pos.check_exit(bid, trend_still_agrees=trend_ok)
                        # A position opened before 14:30 runs to its OWN exit.
                        if reason is None and dur >= pos.hold_min:
                            reason = "timeout"; sell = sell_fill_from_bid(bid)
                        if reason is None and t >= datetime.time(15, 25):
                            reason = "timeout"; sell = sell_fill_from_bid(bid)
                            log.warning(f"HARD BACKSTOP force-exit at {t} (near close)")
                        if reason == "sl" and sl_order_id and MODE == "live":
                            # bid touched the stop but the broker SL-M (LTP-triggered)
                            # has not filled: the broker owns this exit. Wait.
                            if _throttled("slwait", 120):
                                log.info(f"bid {bid} at/below stop {pos.sl_price}; waiting for broker SL-M")
                            reason = None
                    if reason:
                        # never sell what the broker no longer shows we hold
                        held = execu.held_qty(pos_instr)
                        if held is not None and held < LOT_QTY:
                            external = True; reason = None
                    if reason:
                        out = execu.exit_position(pos_instr, LOT_QTY, sl_order_id or "",
                                                  sl_trigger=pos.sl_price, expected_fill=sell,
                                                  tag=f"{pos.strategy.lower()}-exit")
                        if out.kind == "filled":
                            exit_kind, exit_fill, exit_oid = "filled", out.fill_price, out.order_id
                        elif out.kind == "sl_filled":
                            reason, exit_kind = "sl", "sl_filled"
                            exit_fill, exit_oid = (out.fill_price or pos.sl_price), out.order_id
                        elif out.kind == "unknown":
                            pos.manual_only = True
                            halt(f"exit SELL {out.order_id} outcome unknown for #{pos.trade_no} — CHECK BROKER")
                        else:
                            sl_order_id = out.stop_order_id or None
                            log.error(f"EXIT not done ({reason}): {out.message} — retrying next cycle")
                            if _throttled("exitfail", 120):
                                tg(f"❌ <b>EXIT NOT DONE</b> #{pos.trade_no} ({reason})\n{out.message}\n"
                                   f"Position still open; {'stop resting' if sl_order_id else 'NO STOP'} — retrying.")
                            reason = None
                if external:
                    # [2026-10-14] closed/reduced by hand: never send another sell.
                    held = execu.held_qty(pos_instr)
                    if held == 0:
                        if sl_order_id:
                            execu.cancel_and_confirm(sl_order_id)   # drop our resting stop
                        log_trade({"trade_no": pos.trade_no, "entry_ts": getattr(pos, "entry_ts_str", ""),
                                   "strategy": pos.strategy, "direction": pos.direction,
                                   "actual_fill": round(getattr(pos, "actual_fill", pos.entry_premium), 2),
                                   "lots": 1, "strike": pos.option_strike, "opt_type": pos.option_type,
                                   "exit_ts": now.strftime("%H:%M:%S"), "duration_min": round(dur, 1),
                                   "exit_reason": "external", "result": "unknown",
                                   "entry_order_id": entry_order_id or "", "sl_order_id": sl_order_id or "",
                                   "order_status": "closed_outside_bot"})
                        halt(f"#{pos.trade_no} was closed outside the bot (Upstox app) — "
                             f"P&L not recorded by the bot; check the broker")
                        pos = None; pos_instr = None; sl_order_id = None; entry_order_id = None
                    elif held is not None:
                        pos.manual_only = True
                        halt(f"#{pos.trade_no}: broker shows qty {held} (expected {LOT_QTY}) — "
                             f"changed outside the bot. CHECK BROKER")
                    # held None (positions unreadable): decide again next cycle
                    time.sleep(SCAN_SLEEP); continue
                if exit_kind:
                    intended = pos.sl_price if exit_kind == "sl_filled" else sell
                    actual_exit = exit_fill if exit_fill > 0 else intended
                    if intended <= 0:              # exit without a bid: no intended price
                        intended = actual_exit
                    pnl_pts = actual_exit - pos.entry_premium
                    pnl = round(pnl_pts*LOT_QTY, 0)
                    day_pnl += pnl
                    if reason=="target" or (reason in ("trail","timeout") and pnl>0): result="win"
                    elif reason=="sl" or pnl<0: result="loss"
                    else: result="timeout"
                    if reason=="sl": sl_hits_today += 1   # DAILY STOP counter
                    cap.on_result(reason, pnl); lock.record(pos.strategy, reason)
                    log_trade({"trade_no":pos.trade_no,"entry_ts":getattr(pos,"entry_ts_str",""),
                        "strategy":pos.strategy,"direction":pos.direction,
                        "entry_nifty":round(pos.entry_nifty,1),
                        "entry_premium":round(getattr(pos,"computed_entry",pos.entry_premium),2),
                        "actual_fill":round(getattr(pos,"actual_fill",pos.entry_premium),2),
                        "slippage":round(getattr(pos,"actual_fill",pos.entry_premium)
                                         - getattr(pos,"computed_entry",pos.entry_premium),2),
                        "lots":1,"sl":round(pos.sl_price,2),"sl_trigger":round(pos.sl_price,1),
                        "target":round(pos.target_price,2),"hold_min":pos.hold_min,
                        "strike":pos.option_strike,"opt_type":pos.option_type,
                        "iv":round(pos.option_iv,2),"delta":round(pos.option_delta,3),
                        "spread":round(pos.option_spread,2),"confidence":getattr(pos,"confidence",0),
                        "exit_ts":now.strftime("%H:%M:%S"),"exit_premium":round(intended,2),
                        "actual_exit_fill":round(actual_exit,2),"duration_min":round(dur,1),
                        "exit_reason":reason,"mfe_pts":pos.mfe_pts,"mae_pts":pos.mae_pts,
                        "pts":round(pnl_pts,2),"pnl":pnl,"result":result,
                        "entry_order_id":entry_order_id or "","sl_order_id":sl_order_id or "",
                        "exit_order_id":exit_oid,"order_status":exit_kind,
                        "reasons":"; ".join(str(r) for r in pos.plan.get("reasons",[])),
                        # exit slippage = REAL fill vs intended exit (SL: vs the trigger)
                        "exit_slippage":round(actual_exit - intended, 2),
                        "total_slippage":round((getattr(pos,"actual_fill",pos.entry_premium)
                                               - getattr(pos,"computed_entry",pos.entry_premium))
                                               + (actual_exit - intended), 2),
                        "stop_efficiency":(round(abs(pnl_pts)/abs(pos.entry_premium-pos.sl_price),2)
                                           if pnl<0 and pos.entry_premium!=pos.sl_price else "")})
                    e = "✅" if pnl >= 0 else "❌"
                    tg(f"{e} <b>LIVE EXIT #{pos.trade_no} {reason.upper()}</b> ({pos.strategy})\n"
                       f"{pos.option_type}{pos.option_strike} {pos.entry_premium:.1f}→{actual_exit:.1f} "
                       f"= Rs.{pnl:+.0f}\nSL hits today: {sl_hits_today}")
                    log.info(f"EXIT #{pos.trade_no} {result} {exit_kind} Rs.{pnl:+.0f} (sl_hits={sl_hits_today})")
                    pos=None; pos_instr=None; sl_order_id=None; entry_order_id=None
                time.sleep(SCAN_SLEEP); continue

            if not data_ok:
                time.sleep(SCAN_SLEEP); continue

            # ── DAILY STOP / HALT: no new entries ──
            if sl_hits_today >= MAX_SL_HITS_PER_DAY or halted:
                if _throttled("dailystop", 900):
                    log.info(f"No new entries today: {'halted — '+halt_reason if halted else f'{sl_hits_today} SL hits'}")
                time.sleep(SCAN_SLEEP); continue

            # ── LOOK FOR ENTRY (FVG + BOS, session-gated) ──
            if t >= TRADE_END or t < TRADE_OPEN: time.sleep(SCAN_SLEEP); continue
            if no_next: time.sleep(SCAN_SLEEP); continue

            ind["efficiency"] = signals.efficiency_ratio(closes)
            sig_name, direction, sig_detail = detect_live_signal(df5, ltp, ind, day_atr_high)
            if direction not in ("bullish","bearish"):
                time.sleep(SCAN_SLEEP); continue

            # [2026-10-14] never enter on placeholder ATR/RSI (fewer than 14 candles)
            if ind.get("hist_candles", 0) < 14:
                log_live_skip(sig_name, direction, ind, ltp, vix,
                              f"BLOCK: only {ind.get('hist_candles', 0)} candles — ATR/RSI would be placeholders")
                time.sleep(SCAN_SLEEP); continue

            opt_type = "CE" if direction=="bullish" else "PE"
            chain = get_chain(expiry)
            quote = build_quote(chain, atm, opt_type)
            if quote is None or not quote.instr_key:
                log_live_skip(sig_name, direction, ind, ltp, vix, "BLOCK: no tradeable quote (chain/strike unavailable)")
                time.sleep(SCAN_SLEEP); continue

            conf = 7  # both StrongFVG and BOS are 'strong' signals in this gate
            decision = decide_entry(quote=quote, direction=direction, strategy_name=sig_name,
                confidence=conf, atr_5m=ind.get("atr",30), recent_closes=closes,
                now_time=t, regime=ind.get("regime","UNKNOWN"),
                strong_breakout=(sig_name in LIVE_STRATEGIES),
                today=today, nearest_expiry=expiry, vix=vix, vix_open=vix_open,
                gap_pct=gap_pct, is_event_day=ev_today, capital=cap, lockout=lock,
                min_oi=MIN_OI, min_qty=MIN_QTY, max_spread_pct=MAX_SPREAD_PCT,
                atr_day_low=day_atr_low, atr_day_high=day_atr_high)

            if not decision.enter:
                log_live_skip(sig_name, direction, ind, ltp, vix,
                              list(decision.reasons) + ([f"detector: {sig_detail}"] if sig_detail else []))
                time.sleep(SCAN_SLEEP); continue

            # ── PLACE REAL ENTRY ──
            trade_no += 1
            plan = decision.plan
            log.info(f"ENTRY SIGNAL #{trade_no} {sig_name} {direction} {opt_type}{quote.strike} "
                     f"computed entry {plan['entry_price']:.2f} | {sig_detail}")
            entry = execu.place_entry(quote.instr_key, LOT_QTY, tag=sig_name.lower())
            if not entry.ok:
                consec_rejects += 1
                log.error(f"ENTRY REJECTED ({consec_rejects}): {entry.message}")
                if consec_rejects >= REJECT_PAUSE_AT:
                    halt(f"{consec_rejects} order rejections in a row (account/IP/margin?). "
                         f"Last: {entry.message[:80]}")
                else:
                    tg(f"❌ <b>ENTRY REJECTED</b> #{trade_no}: {entry.message[:60]}")
                trade_no -= 1   # didn't actually enter
                time.sleep(SCAN_SLEEP); continue
            entry_order_id = entry.order_id
            fill = execu.confirm_fill(entry.order_id, expected_fill=plan["entry_price"])
            if not fill.ok:
                # [2026-10-14] cancel it and read back the FINAL state — an
                # unconfirmed order used to be left live and could fill later
                # with no stop and no tracking.
                late = execu.resolve_unconfirmed_entry(entry.order_id)
                if late.status == "complete":
                    fill = late                       # it did fill: adopt it below
                    log.warning(f"entry #{trade_no} filled late ({late.fill_price}) — adopting")
                elif late.filled_qty > 0:
                    fl = execu.flatten(quote.instr_key, late.filled_qty, tag="partial-flatten")
                    halt(f"entry #{trade_no} PARTIAL fill {late.filled_qty} — "
                         f"{'flattened' if fl.ok else 'FLATTEN FAILED, CHECK BROKER'}")
                    trade_no -= 1; time.sleep(SCAN_SLEEP); continue
                elif late.status in ("cancelled", "rejected"):
                    consec_rejects += 1
                    log.error(f"ENTRY NOT FILLED ({consec_rejects}): {fill.message} -> {late.status} "
                              f"[broker: {late.message or 'no reason given'}]")
                    if consec_rejects >= REJECT_PAUSE_AT:
                        halt(f"{consec_rejects} unfilled entries in a row (margin/account?). "
                         f"Last broker reason: {(late.message or 'none')[:80]}")
                    else:
                        tg(f"⚠️ <b>ENTRY NOT FILLED</b> #{trade_no} — {late.status}: {(late.message or '')[:80]}")
                    trade_no -= 1; time.sleep(SCAN_SLEEP); continue
                else:
                    halt(f"entry order {entry.order_id} state UNKNOWN after cancel — CHECK BROKER")
                    trade_no -= 1; time.sleep(SCAN_SLEEP); continue
            consec_rejects = 0   # cleared only after a CONFIRMED fill (not mere placement)

            actual_fill = fill.fill_price if fill.fill_price>0 else plan["entry_price"]
            sl_trigger = round(actual_fill*0.80, 1)   # SL from the ACTUAL fill (20% below)
            sl = execu.place_stop_loss(quote.instr_key, LOT_QTY, sl_trigger, tag="sl")
            if not sl.ok:
                sl = execu.place_stop_loss(quote.instr_key, LOT_QTY, sl_trigger, tag="sl")  # one retry
            if sl.ok:
                sl_order_id = sl.order_id
            else:
                # [2026-10-14] flatten WITH confirmation (was fire-and-forget)
                log.error(f"SL-M PLACEMENT FAILED twice: {sl.message}. Flattening.")
                fl = execu.flatten(quote.instr_key, LOT_QTY, tag="no-sl-flatten")
                if fl.ok:
                    halt(f"#{trade_no}: broker stop could not be placed; position flattened at {fl.fill_price}")
                    log_trade({"trade_no":trade_no,"entry_ts":now.strftime("%Y-%m-%d %H:%M:%S"),
                        "strategy":sig_name,"direction":direction,"entry_nifty":round(ltp,1),
                        "entry_premium":round(plan["entry_price"],2),"actual_fill":round(actual_fill,2),
                        "lots":1,"strike":quote.strike,"opt_type":opt_type,
                        "exit_ts":now_ist().strftime("%H:%M:%S"),"actual_exit_fill":round(fl.fill_price,2),
                        "exit_reason":"no_stop_flatten",
                        "pnl":round((fl.fill_price-actual_fill)*LOT_QTY,0),
                        "entry_order_id":entry_order_id,"exit_order_id":fl.order_id,
                        "order_status":"flattened"})
                    day_pnl += round((fl.fill_price-actual_fill)*LOT_QTY,0)
                    time.sleep(SCAN_SLEEP); continue
                # could not flatten either: manage it WITHOUT a broker stop
                # (bid-based SL exits apply when sl_order_id is empty) and halt.
                sl_order_id = None
                halt(f"#{trade_no}: NO broker stop and flatten failed — bot managing on bid. CHECK BROKER")

            plan2 = dict(plan)
            plan2["entry_price"]=actual_fill
            plan2["target_price"]=round(actual_fill*(1+plan["target_pct"]),2)
            plan2["sl_price"]=sl_trigger
            plan2["reasons"]=list(decision.reasons) + ([f"detector: {sig_detail}"] if sig_detail else [])
            plan2["day_type"]=decision.day_type
            plan2["target_mode"]=decision.target_mode
            pos = LongOptionPosition(trade_no, sig_name, direction, ltp, quote, plan2, 1, ind)
            pos.confidence=conf; pos.actual_fill=actual_fill
            pos.computed_entry=plan["entry_price"]   # keep the pre-fill estimate
            pos.entry_ts_str=now.strftime("%Y-%m-%d %H:%M:%S")
            pos_instr = quote.instr_key

            # crash-safe OPEN row the instant the real order fills
            log_trade({"trade_no":trade_no,"entry_ts":pos.entry_ts_str,
                "strategy":sig_name,"direction":direction,
                "entry_nifty":round(ltp,1),"entry_premium":round(plan["entry_price"],2),
                "actual_fill":round(actual_fill,2),
                "slippage":round(actual_fill-plan["entry_price"],2),"lots":1,
                "sl":round(sl_trigger,2),"sl_trigger":round(sl_trigger,1),
                "target":round(plan2["target_price"],2),"hold_min":pos.hold_min,
                "strike":quote.strike,"opt_type":opt_type,
                "iv":round(quote.iv,2),"delta":round(quote.delta,3),
                "confidence":conf,"exit_reason":"OPEN",
                "entry_order_id":entry_order_id or "","sl_order_id":sl_order_id or "",
                "order_status":"filled-open"})

            tg(f"{'🔴' if MODE=='live' else '🟡'} <b>LIVE ENTRY #{trade_no} {sig_name}</b>\n"
               f"{direction.upper()} {opt_type}{quote.strike}\nfill Rs.{actual_fill:.2f} "
               f"(est {plan['entry_price']:.2f}) slip {actual_fill-plan['entry_price']:+.2f}\n"
               f"SL(broker) {sl_trigger if sl_order_id else 'NONE'} Tgt {plan2['target_price']:.1f}")
            log.info(f"ENTERED #{trade_no} fill {actual_fill} SL-M@{sl_trigger} (oid {sl_order_id})")
            time.sleep(SCAN_SLEEP)

        except KeyboardInterrupt:
            log.info("Manual stop (Ctrl-C).")
            tg(f"⏹ <b>LIVE BOT STOPPED manually</b>\nOpen position (if any) still has "
               f"its broker SL-M resting. Day P&L Rs.{day_pnl:+.0f}")
            break
        except Exception as e:
            log.error(f"loop error: {e}", exc_info=True)
            time.sleep(SCAN_SLEEP)

if __name__ == "__main__":
    run()
