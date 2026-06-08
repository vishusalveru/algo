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

def compute_indicators(df5, df15, df30):
    ind={}
    try:
        ind["atr"]=signals.calc_atr(df5); ind["rsi"]=signals.calc_rsi(df5)
        dfe=signals.calc_ema(df5)
        if "ema9" in dfe.columns:
            ind["e9"]=float(dfe["ema9"].iloc[-1]); ind["e21"]=float(dfe["ema21"].iloc[-1])
            ind["e50"]=float(dfe["ema50"].iloc[-1]); ind["df_ema"]=dfe
        t5,_,_=signals.detect_trend_relaxed(df5)
        trend,_,strength=signals.detect_trend_multi(df5,df15,df30,
            ind.get("e9",0),ind.get("e21",0),ind.get("e50",0),0)
        ind["t5"],ind["trend"],ind["trend_strength"]=t5,trend,strength
        ind["regime"]=signals.classify_intraday_regime(df5,ind.get("e9",0),ind.get("e21",0),ind.get("atr",30))
    except Exception as e: log.error(f"indicators: {e}")
    return ind

def detect_live_signal(df5, ltp, ind, day_atr_high=0):
    """LIVE bot trades StrongFVG + BOS [BOS added 2026-06-05, paper-validated].
    FVG entries pass through the exhaustion filter (block late/exhausted entries
    where 2+ signals agree). BOS is a structure-break signal that catches moves
    earlier, so it is NOT exhaustion-filtered. Returns (strategy, direction) or
    (None, None), plus logs why an FVG was blocked."""
    rsi = ind.get("rsi", 50)
    eff = ind.get("efficiency", None)
    atr = ind.get("atr", None)
    # Priority: FVG first (with exhaustion filter), then BOS.
    try:
        fvg, _ = signals.detect_fvg(df5)
        if fvg:
            direction = fvg.get("type")
            block, why = signals.fvg_exhaustion_block(direction, rsi, eff, atr, day_atr_high)
            if block:
                log.info(f"FVG {direction} BLOCKED (exhaustion): {', '.join(why)}")
            else:
                return "StrongFVG", direction
    except Exception as e:
        log.error(f"fvg detect: {e}")
    try:
        bos, _ = signals.detect_bos(df5, ltp)
        if bos:
            return "BOS", bos.get("type")
    except Exception as e:
        log.error(f"bos detect: {e}")
    return None, None

LIVE_STRATEGIES = ("StrongFVG", "BOS")   # strategies the live bot may trade

# ── Logging (same schema as paper + live execution fields) ──────────────────
TRADE_COLS=["trade_no","entry_ts","strategy","direction","entry_nifty","entry_premium",
    "actual_fill","slippage","lots","sl","sl_trigger","target","hold_min","strike","opt_type",
    "iv","delta","spread","confidence","exit_ts","exit_premium","actual_exit_fill",
    "duration_min","exit_reason","mfe_pts","mae_pts","pts","pnl","result",
    "entry_order_id","sl_order_id","exit_order_id","order_status","reasons"]

def init_logs():
    d=datetime.date.today().strftime("%Y-%m-%d")
    fn=f"live_trade_{d}.csv"
    if not os.path.exists(fn):
        csv.DictWriter(open(fn,"w",newline=""),fieldnames=TRADE_COLS).writeheader()

def log_trade(rec):
    d=datetime.date.today().strftime("%Y-%m-%d")
    w=csv.DictWriter(open(f"live_trade_{d}.csv","a",newline=""),fieldnames=TRADE_COLS)
    w.writerow({c:rec.get(c,"") for c in TRADE_COLS})

# ── Main loop ────────────────────────────────────────────────────────────────
def run():
    log.info("="*60)
    banner = "🔴 LIVE MODE — REAL ORDERS" if MODE=="live" else "🟡 MOCK MODE — no real orders"
    log.info(f"NIFTY LIVE BOT — {banner} — FVG + BOS, 1 LOT")
    log.info("="*60)
    init_logs()

    execu = LiveExecutor(token=getattr(config,"LIVE_TOKEN",""), mode=MODE)
    cap, lock = CapitalLadder(), ReentryLockout()
    pos = None
    pos_instr = None        # instrument_key of the open position
    sl_order_id = None      # resting broker SL-M order id
    entry_order_id = None
    trade_no = 0
    sl_hits_today = 0       # DAILY STOP counter (true SL exits only)
    day_pnl = 0.0
    consec_rejects = 0      # consecutive order rejections (e.g. IP block) -> pause
    REJECT_PAUSE_AT = 3     # after this many in a row, stop trying for the day
    last_tg = time.time() - TELEGRAM_SCAN_INTERVAL
    day_atr_low = day_atr_high = 0.0

    # Startup reconciliation — never start 'assuming flat'
    open_positions = execu.get_open_positions()
    if open_positions:
        log.warning(f"STARTUP: broker shows {len(open_positions)} open position(s)!")
        tg(f"⚠️ <b>LIVE BOT STARTUP</b>\nBroker shows {len(open_positions)} OPEN "
           f"position(s). Bot will NOT auto-manage these — handle manually, then restart.")
        # Safe default: do not trade while an unmanaged position exists.
        log.warning("Refusing to trade until the existing position is cleared. Exiting.")
        return

    expiry, nearest_expiry, rolled = feeds.get_tradeable_expiry()
    vix_open = feeds.get_india_vix()
    prev_ohlc = feeds.get_prev_day_ohlc()

    # [STARTUP FEED GUARD] If the live feeds come back empty, the token is
    # almost certainly expired/invalid (UDAPI100050) or access is blocked. Do
    # NOT pretend to be running — refuse loudly so it can't sit there looking
    # live while unable to trade or (worse) act on missing data.
    test_ltp = get_ltp()
    if expiry is None or vix_open is None or test_ltp is None:
        msg = (f"🛑 <b>LIVE BOT CANNOT START — DEAD FEEDS</b>\n"
               f"Expiry={expiry} VIX={vix_open} LTP={test_ltp}\n"
               f"Almost certainly an EXPIRED/INVALID TOKEN (refresh it in "
               f"config.py) or blocked access. Bot is NOT running.")
        log.error(msg.replace("<b>","").replace("</b>",""))
        tg(msg)
        return

    today = datetime.date.today()
    ev_today, _ = events.is_event_day(today)
    no_next = (rolled is False and nearest_expiry==today and expiry==nearest_expiry)

    roll_txt = f"\n⚠️ EXPIRY: trading NEXT expiry {expiry}" if rolled else ""
    if no_next: roll_txt = "\n🛑 EXPIRY + no next contract — NOT trading."
    tg(f"{('🔴 LIVE' if MODE=='live' else '🟡 MOCK')} <b>FVG+BOS BOT STARTED</b>\n"
       f"Expiry {expiry} | VIX {vix_open}\n1 lot | 20% SL (broker) | daily stop {MAX_SL_HITS_PER_DAY} SLs{roll_txt}")

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

            ltp = get_ltp()
            df5,df15,df30 = get_candles(5),get_candles(15),get_candles(30)
            if ltp is None or df5 is None or len(df5)<6:
                time.sleep(SCAN_SLEEP); continue
            if df15 is None or len(df15)<3: df15=df5
            if df30 is None or len(df30)<3: df30=df5

            ind = compute_indicators(df5,df15,df30)
            vix = feeds.get_india_vix()
            atm = int(round(ltp/50)*50)
            closes = df5["close"].astype(float).tolist()[-30:]
            gap_pct = feeds.compute_gap_pct(df5["open"].iloc[0] if len(df5) else ltp, prev_ohlc)
            _atr=ind.get("atr",0)
            if _atr>0:
                day_atr_low=min(day_atr_low,_atr) if day_atr_low else _atr
                day_atr_high=max(day_atr_high,_atr)

            # ── periodic status ──
            if time.time()-last_tg>=TELEGRAM_SCAN_INTERVAL:
                openln=""
                if pos is not None:
                    cb=current_bid(get_chain(expiry),pos.option_strike,pos.option_type)
                    if cb: openln=f"\n🎯 OPEN {pos.option_type}{pos.option_strike} now {cb:.1f} (entry {pos.entry_premium:.1f})"
                tg(f"{'🔴' if MODE=='live' else '🟡'} {now:%H:%M} N{ltp:.0f} ATR{_atr:.0f} "
                   f"VIX{vix} | trades {trade_no} SLhits {sl_hits_today} PnL Rs.{day_pnl:+.0f}{openln}")
                last_tg=time.time()

            # ── MONITOR OPEN POSITION ──
            if pos is not None:
                chain = get_chain(expiry)
                bid = current_bid(chain, pos.option_strike, pos.option_type)
                if bid<=0: bid=0.0
                trend_ok = (ind.get("trend")==pos.direction)
                reason, sell, dur = pos.check_exit(bid, trend_still_agrees=trend_ok)
                if reason is None and (dur>=pos.hold_min or t>=TRADE_END):
                    reason="timeout"; sell=sell_fill_from_bid(bid)

                if reason:
                    # EXIT: if SL already fired at broker, the position is gone;
                    # otherwise place a market sell and cancel the resting SL-M.
                    actual_exit = sell
                    exit_oid = ""
                    if reason != "sl":
                        ex = execu.place_exit(pos_instr, LOT_QTY, sl_order_id=sl_order_id or "")
                        if ex.ok:
                            exit_oid = ex.order_id
                            fill = execu.confirm_fill(ex.order_id, expected_fill=sell)
                            if fill.ok and fill.fill_price>0: actual_exit=fill.fill_price
                        else:
                            log.error(f"EXIT order failed: {ex.message}. MANUAL ACTION MAY BE NEEDED.")
                            tg(f"❌ <b>EXIT ORDER FAILED</b> #{pos.trade_no} — check broker manually!")
                    else:
                        # SL-M fired at the exchange; sell price ~ trigger
                        exit_oid = sl_order_id or "broker-sl"

                    pnl_pts = actual_exit - pos.entry_premium
                    pnl = round(pnl_pts*LOT_QTY,0)
                    day_pnl += pnl
                    if reason=="target" or (reason in ("trail","timeout") and pnl>0): result="win"
                    elif reason=="sl" or pnl<0: result="loss"
                    else: result="timeout"
                    if reason=="sl": sl_hits_today += 1   # DAILY STOP counter
                    cap.on_result(reason,pnl); lock.record(pos.strategy,reason)

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
                        "exit_ts":now.strftime("%H:%M:%S"),"exit_premium":round(sell,2),
                        "actual_exit_fill":round(actual_exit,2),"duration_min":round(dur,1),
                        "exit_reason":reason,"mfe_pts":pos.mfe_pts,"mae_pts":pos.mae_pts,
                        "pts":round(pnl_pts,2),"pnl":pnl,"result":result,
                        "entry_order_id":entry_order_id or "","sl_order_id":sl_order_id or "",
                        "exit_order_id":exit_oid,"order_status":"filled",
                        "reasons":"; ".join(pos.plan.get("reasons",[]))})

                    e="✅" if pnl>=0 else "❌"
                    tg(f"{e} <b>LIVE EXIT #{pos.trade_no} {reason.upper()}</b>\n"
                       f"{pos.option_type}{pos.option_strike} {pos.entry_premium:.1f}→{actual_exit:.1f} "
                       f"= Rs.{pnl:+.0f}\nSL hits today: {sl_hits_today}")
                    log.info(f"EXIT #{pos.trade_no} {result} Rs.{pnl:+.0f} (sl_hits={sl_hits_today})")
                    pos=None; pos_instr=None; sl_order_id=None; entry_order_id=None
                time.sleep(SCAN_SLEEP); continue

            # ── DAILY STOP: 2 SL hits -> no new entries ──
            if sl_hits_today >= MAX_SL_HITS_PER_DAY:
                if time.time()-last_tg < SCAN_SLEEP:  # avoid spam; log occasionally
                    log.info(f"Daily stop active ({sl_hits_today} SL hits). No new entries.")
                time.sleep(SCAN_SLEEP); continue

            # ── LOOK FOR ENTRY (FVG + BOS, session-gated) ──
            if t >= TRADE_END or t < TRADE_OPEN: time.sleep(SCAN_SLEEP); continue
            if no_next: time.sleep(SCAN_SLEEP); continue

            # make efficiency available to the exhaustion filter
            ind["efficiency"] = signals.efficiency_ratio(closes)
            sig_name, direction = detect_live_signal(df5, ltp, ind, day_atr_high)
            if direction not in ("bullish","bearish"):
                time.sleep(SCAN_SLEEP); continue

            opt_type = "CE" if direction=="bullish" else "PE"
            chain = get_chain(expiry)
            quote = build_quote(chain, atm, opt_type)
            if quote is None or not quote.instr_key:
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
                time.sleep(SCAN_SLEEP); continue

            # ── PLACE REAL ENTRY ──
            trade_no += 1
            plan = decision.plan
            log.info(f"ENTRY SIGNAL #{trade_no} FVG {direction} {opt_type}{quote.strike} "
                     f"computed entry {plan['entry_price']:.2f}")
            entry = execu.place_entry(quote.instr_key, LOT_QTY, tag="fvg")
            if not entry.ok:
                consec_rejects += 1
                log.error(f"ENTRY REJECTED ({consec_rejects}): {entry.message}")
                # If rejections pile up it's almost always an ACCOUNT-level block
                # (e.g. UDAPI1154 IP whitelisting not propagated, margin, token),
                # not a per-trade issue. Stop retrying every signal — one clear
                # alert, then no new entries for the day.
                if consec_rejects >= REJECT_PAUSE_AT:
                    log.error("Too many consecutive rejections — pausing entries for the day.")
                    tg(f"🛑 <b>ENTRIES PAUSED</b> — {consec_rejects} rejections in a row.\n"
                       f"Likely an account/IP/margin block, not a trade issue.\n"
                       f"Last: {entry.message[:80]}\nFix it, then restart the bot.")
                    sl_hits_today = MAX_SL_HITS_PER_DAY  # reuse the no-new-entries gate
                else:
                    tg(f"❌ <b>ENTRY REJECTED</b> #{trade_no}: {entry.message[:60]}")
                trade_no -= 1   # didn't actually enter
                time.sleep(SCAN_SLEEP); continue
            consec_rejects = 0   # a successful placement clears the streak
            entry_order_id = entry.order_id
            fill = execu.confirm_fill(entry.order_id, expected_fill=plan["entry_price"])
            if not fill.ok:
                log.error(f"ENTRY FILL NOT CONFIRMED: {fill.message}. CHECK BROKER MANUALLY.")
                tg(f"⚠️ <b>FILL UNCONFIRMED</b> #{trade_no} — check broker manually!")
                trade_no -= 1
                time.sleep(SCAN_SLEEP); continue

            actual_fill = fill.fill_price if fill.fill_price>0 else plan["entry_price"]
            # SL trigger from the ACTUAL fill (20% below)
            sl_trigger = round(actual_fill*0.80, 1)
            sl = execu.place_stop_loss(quote.instr_key, LOT_QTY, sl_trigger, tag="fvg-sl")
            if sl.ok:
                sl_order_id = sl.order_id
            else:
                # could not rest a broker stop — too risky to hold unprotected
                log.error(f"SL-M PLACEMENT FAILED: {sl.message}. EXITING POSITION IMMEDIATELY.")
                tg(f"❌ <b>SL FAILED #{trade_no}</b> — flattening, holding unprotected is unsafe")
                execu.place_exit(quote.instr_key, LOT_QTY)
                trade_no -= 1
                time.sleep(SCAN_SLEEP); continue

            # build the position object on the ACTUAL fill, start monitoring
            plan2 = dict(plan)
            plan2["entry_price"]=actual_fill
            plan2["target_price"]=round(actual_fill*(1+plan["target_pct"]),2)
            plan2["sl_price"]=sl_trigger
            plan2["reasons"]=decision.reasons; plan2["day_type"]=decision.day_type
            plan2["target_mode"]=decision.target_mode
            pos = LongOptionPosition(trade_no, sig_name, direction, ltp, quote, plan2, 1, ind)
            pos.confidence=conf; pos.actual_fill=actual_fill
            pos.computed_entry=plan["entry_price"]   # keep the pre-fill estimate
            pos.entry_ts_str=now.strftime("%Y-%m-%d %H:%M:%S")
            pos_instr = quote.instr_key

            # [CRASH-SAFE LOG FIX] Write an OPEN row to the live CSV the INSTANT
            # the real order fills — before monitoring. If the bot dies before
            # exit, the real position is still on record (mirrors the paper bot).
            # The exit will append a separate completed-trade row.
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

            tg(f"{'🔴' if MODE=='live' else '🟡'} <b>LIVE ENTRY #{trade_no} FVG</b>\n"
               f"{direction.upper()} {opt_type}{quote.strike}\nfill Rs.{actual_fill:.2f} "
               f"(est {plan['entry_price']:.2f}) slip {actual_fill-plan['entry_price']:+.2f}\n"
               f"SL(broker) {sl_trigger} Tgt {plan2['target_price']:.1f}")
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
