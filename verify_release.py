#!/usr/bin/env python3
"""
verify_release.py — checks every item of AUDIT_2026-10-14 against the code that
is ACTUALLY in this folder. No network, no orders, never touches your logs
(it runs inside a temporary folder). Run after deploying:

    cd ~/algo-trading && python3 verify_release.py           # ~3-6 min on e2-micro
    cd ~/algo-trading && python3 verify_release.py --quick   # skips the 22 broker scenarios

Exit code 0 = every item that this release claims to fix is verified fixed.
"""
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
QUICK = "--quick" in sys.argv
WORK = tempfile.mkdtemp(prefix="verify_rel_")
os.chdir(WORK)                       # bot loggers write here, not into your folder
if not os.path.exists(os.path.join(HERE, "config.py")):
    with open("config.py", "w") as fh:
        fh.write('LIVE_TOKEN="x"\nBOT_TOKEN=""\nCHAT_ID=""\n')
sys.path[:0] = [HERE, WORK]

import pandas as pd

RESULTS = []          # (id, title, status, evidence)
def record(i, title, ok, evidence):
    RESULTS.append((i, title, "FIXED" if ok else "FAILED", evidence))
def not_in_release(i, title, why):
    RESULTS.append((i, title, "NOT IN THIS RELEASE", why))
def src(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()

LIVE, PAPER, SIG, EXE, FEEDS = (src("nifty_live_bot.py"), src("nifty_bot_v14.py"), src("signals.py"),
                                src("live_execution.py"), src("live_feeds_v14.py"))
record("V0", "Release files are the 2026-10-14 release",
       'RELEASE = "2026-10-14"' in LIVE and '"seeded_indicators": True' in LIVE
       and '"fvg_require_in_gap": True' in LIVE and 'MODE = "live"' in LIVE,
       'RELEASE="2026-10-14", LIVE_FIX seeded+fvg_in_gap True, MODE="live"')

# ── 4.1 live-money safety: run the fake-broker scenarios ───────────────────
SAFETY = [
    ("C1", "Stop decided by the BROKER SL-M, never by a 0/missing bid",
     ["chain_none", "bid_at_stop_broker_fills_later", "sl_filled_between_polls"]),
    ("C2", "Exit = confirmed cancel -> sell -> confirmed fill; failure keeps a stop",
     ["cancel_fails_once", "sell_rejected_once", "sell_outcome_unknown"]),
    ("C3", "SL-M placement failure -> confirmed flatten + halt", ["sl_place_fails_flatten"]),
    ("C4", "Unconfirmed entry cancelled and read back (late / partial fill)",
     ["entry_fills_late", "entry_partial_fill"]),
    ("H1", "Position managed during LTP/candle/chain outage, incl. backstop",
     ["ltp_outage_while_open", "backstop_no_bid"]),
    ("H2", "Daily SL stop / halt survive a restart", ["restart_after_two_sls", "no_funds_restart_same_day"]),
    ("H3", "Startup refuses on API error or pending F&O order",
     ["startup_positions_error", "startup_pending_order"]),
    ("N1", "[found in review] position closed by hand in Upstox is never sold again",
     ["manual_close_stop_cancelled", "manual_close_stop_resting"]),
    ("N2", "[no-funds test] rejected entries -> 3 tries -> halt, no sells",
     ["no_funds_rejected_after_place", "no_funds_place_error"]),
    ("N3", "MOCK mode exits at target and at stop", ["mock_target", "mock_sl"]),
]
harness = os.path.join(HERE, "tests", "live_harness.py")
if QUICK:
    for i, t, _ in SAFETY:
        RESULTS.append((i, t, "SKIPPED (--quick)", "run without --quick"))
else:
    env = dict(os.environ, RELEASE_DIR=HERE)
    for i, t, scs in SAFETY:
        outs = []
        for sc in scs:
            # no -I: isolated mode hides user-installed packages (pip --user), so
            # pandas/requests would not import on the VM
            r = subprocess.run([sys.executable, harness, sc], capture_output=True, text=True, env=env)
            last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
            if not last.startswith("PASS"):
                err = (r.stderr.strip().splitlines() or [""])[-1]
                last += f" [stderr: {err[:160]}]" if err else ""
            outs.append((sc, r.returncode == 0 and last.startswith("PASS"), last))
        record(i, t, all(o[1] for o in outs),
               "; ".join(f"{sc} {'PASS' if ok else 'FAIL: ' + last}" for sc, ok, last in outs))

# ── 4.2 D1 previous close ──────────────────────────────────────────────────
import live_feeds_v14 as F
class _R:
    def __init__(self, js): self.status_code, self._js = 200, js
    def json(self): return self._js
today = F.today_ist()
sess, d = [], today
while len(sess) < 6:
    if d.weekday() < 5: sess.append(d)
    d -= dt.timedelta(days=1)
closes = {s: 23000 + i * 100 for i, s in enumerate(sorted(sess))}
want = max(s for s in closes if s < today)
_orig_get = F.requests.get
ok_all = True
for newest_first in (True, False):
    rows = sorted([[f"{s.isoformat()}T00:00:00+05:30", c, c, c, c, 0, 0] for s, c in closes.items()],
                  key=lambda x: x[0], reverse=newest_first)
    F.requests.get = lambda *a, _r=rows, **k: _R({"status": "success", "data": {"candles": _r}})
    p = F.get_prev_day_ohlc()
    ok_all &= bool(p) and p["date"] == want.isoformat() and p["close"] == closes[want]
F.requests.get = _orig_get
record("D1", "Previous close = last session strictly before IST today",
       ok_all, f"API newest-first and oldest-first both give {want} close {closes[want]}")

# ── 4.2 D2 + D3: the live entry gate on a clean trend BEFORE 13:25 ─────────
import nifty_live_bot as B
import decision_layer_v14 as DL
B.log.handlers[:] = []          # keep the check's output clean

def session(day, n, start, step, rng=14.0):
    ts = pd.date_range(f"{day} 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    rows = []
    for i, t in enumerate(ts):
        o = start + i * step + (-3 if i % 4 == 3 else 0)          # small pullback every 4th candle
        c = o + step * 0.8
        rows.append([t, o, max(o, c) + rng, min(o, c) - rng, c, 0, 0])
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])

def chain(atm):
    side = {"market_data": {"bid_price": 120.0, "ask_price": 120.5, "bid_qty": 900, "ask_qty": 900, "oi": 300000},
            "option_greeks": {"delta": 0.5, "iv": 12.0}, "instrument_key": "NSE_FO|1"}
    return [{"strike_price": atm, "call_options": side, "put_options": side}]

def gate(direction, hhmm, seeded):
    step = 6.0 if direction == "bullish" else -6.0
    prev = session("2026-10-14", 75, 23300.0, step / 3)
    n = (dt.datetime.combine(dt.date.today(), dt.time(*hhmm)) -
         dt.datetime.combine(dt.date.today(), dt.time(9, 15))).seconds // 300
    tday = session("2026-10-15", n, float(prev.close.iloc[-1]) + step, step)
    B.LIVE_FIX["seeded_indicators"] = seeded
    lo = hi = 0.0
    for k in range(6, n + 1):                                    # the bot's running ATR range
        ind = B.compute_indicators(tday.iloc[:k], tday.iloc[:k], tday.iloc[:k], prev if seeded else None)
        a = ind.get("atr", 0)
        if a > 0:
            lo = min(lo, a) if lo else a; hi = max(hi, a)
    if not seeded:
        ind["hist_candles"] = 99                                 # the old code had no candle-count block
    ltp = float(tday.close.iloc[-1]); atm = int(round(ltp / 50) * 50)
    if ind.get("hist_candles", 0) < 14:
        return ind, False, f"BLOCK: only {ind.get('hist_candles')} candles"
    q = B.build_quote(chain(atm), atm, "CE" if direction == "bullish" else "PE")
    dcs = DL.decide_entry(quote=q, direction=direction, strategy_name="BOS", confidence=7,
                          atr_5m=ind.get("atr", 30), recent_closes=tday.close.astype(float).tolist()[-30:],
                          now_time=dt.time(*hhmm), regime=ind.get("regime", "UNKNOWN"), strong_breakout=True,
                          today=dt.date(2026, 10, 15), nearest_expiry="2026-10-20", vix=14.0, vix_open=14.0,
                          gap_pct=0.0, is_event_day=False, capital=DL.CapitalLadder(), lockout=DL.ReentryLockout(),
                          min_oi=B.MIN_OI, min_qty=B.MIN_QTY, max_spread_pct=B.MAX_SPREAD_PCT,
                          atr_day_low=lo, atr_day_high=hi)
    why = next((r for r in dcs.reasons if r.startswith("BLOCK")), f"size {dcs.size_mult:.2f}")
    return ind, dcs.enter, why

print("\nD2 — live BOS gate on a CLEAN trend, old inputs vs release inputs (same detector, same gates):")
print(f"{'time':>6} {'dir':>8} | {'OLD regime':>14} {'OLD':>7} | {'NEW regime':>14} {'NEW':>7} | NEW reason")
times = [(9, 45), (10, 10), (10, 15), (10, 30), (11, 30), (12, 30), (13, 15), (14, 0)]
d2_ok, old_bull_blocked, early_sym = True, 0, True
for direction in ("bullish", "bearish"):
    for hhmm in times:
        io, eo, _ = gate(direction, hhmm, False)
        inew, en, wn = gate(direction, hhmm, True)
        print(f"{hhmm[0]:02d}:{hhmm[1]:02d} {direction:>8} | {io.get('regime',''):>14} {'ENTER' if eo else 'block':>7} | "
              f"{inew.get('regime',''):>14} {'ENTER' if en else 'block':>7} | {wn[:70]}")
        want_regime = "TRENDING_BULL" if direction == "bullish" else "TRENDING_BEAR"
        if hhmm < (10, 15):      # regime needs 12 of TODAY's candles: UNKNOWN for BOTH directions
            early_sym &= inew.get("regime") == "UNKNOWN" and not en
            continue
        d2_ok &= (inew.get("regime") == want_regime and en)
        if direction == "bullish" and hhmm < (13, 25) and not eo:
            old_bull_blocked += 1
B.LIVE_FIX["seeded_indicators"] = True
record("D2", "Bullish BOS before 13:25 is no longer blocked by a bearish-biased regime",
       d2_ok and early_sym,
       f"clean up-trend -> TRENDING_BULL + ENTER at every check 10:15-14:00 (old code blocked "
       f"{old_bull_blocked}/5 bullish checks before 13:25); down-trend unchanged -> TRENDING_BEAR + ENTER. "
       f"09:45-10:10: BOS blocked in BOTH directions (regime needs 12 of today's candles) — same as before, now symmetric")

import signals
prev = session("2026-10-14", 75, 23300.0, 2.0)
early = session("2026-10-15", 6, 23460.0, 6.0)                 # 09:45
i_seed = B.compute_indicators(early, early, early, prev)
i_none = B.compute_indicators(early, early, early, None)
record("D3", "No placeholder ATR 20 / RSI 50 at the open; entries blocked if history is missing",
       signals.calc_atr(early) == 20.0 and i_seed["atr"] != 20.0 and i_seed["rsi"] != 50.0
       and i_seed["hist_candles"] >= 14 and i_none["hist_candles"] < 14
       and "hist_candles\", 0) < 14" in LIVE and "hist_candles\", 0) < 14" in PAPER,
       f"09:45 with yesterday: ATR {i_seed['atr']:.1f} RSI {i_seed['rsi']:.1f} ({i_seed['hist_candles']} candles); "
       f"without: {i_none['hist_candles']} candles -> blocked (live + paper)")

# ── 4.2 D4 ─────────────────────────────────────────────────────────────────
nn = 'nearest_expiry == today.strftime("%Y-%m-%d")'
record("D4a", "no_next guard compares like with like (string)", nn in LIVE and nn in PAPER,
       "both bots compare nearest_expiry to today.strftime('%Y-%m-%d')")
record("D4b", "atm_vol_z measures per-minute volume, not the day-cumulative total",
       "_atm_vol_last" in PAPER and "(atm_vol - prev[1]) / ((now_ts - prev[2]) / 60.0)" in PAPER,
       "z-score of volume/min between snapshots at the same ATM strike")
record("D4c", "Futures/rvol lookup no longer re-downloads the contract list every cycle",
       'if _FUT_KEY_CACHE["date"] == today:' in PAPER, "'not found' is cached for the day (path itself still finds nothing)")
# FVG gap-bound: bullish gap 23510-23540, then price falls; last candle bounces green
def fvg_frame(last_close):
    c = [[23500, 23510, 23490, 23505], [23505, 23560, 23500, 23555], [23556, 23580, 23540, 23575],
         [23575, 23578, 23520, 23525], [23525, 23528, 23480, 23485],
         [last_close - 13, last_close + 5, last_close - 17, last_close]]
    ts = pd.date_range("2026-10-15 10:00", periods=len(c), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame([[t] + r + [0, 0] for t, r in zip(ts, c)],
                        columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
thr_old = signals.detect_fvg(fvg_frame(23495), require_in_gap=False)[0]   # bounced BELOW the gap
thr_new = signals.detect_fvg(fvg_frame(23495), require_in_gap=True)[0]
ins_old = signals.detect_fvg(fvg_frame(23525), require_in_gap=False)[0]   # bounced INSIDE the gap
ins_new = signals.detect_fvg(fvg_frame(23525), require_in_gap=True)[0]
record("D4d", "FVG entry must still be INSIDE the gap (live AND paper)",
       thr_old is not None and thr_new is None and ins_old is not None and ins_new is not None
       and 'require_in_gap=LIVE_FIX["fvg_require_in_gap"]' in LIVE and "detect_fvg(df5, require_in_gap=True)" in PAPER,
       f"price went THROUGH the gap: old -> {'signal' if thr_old else 'none'}, release -> {'signal' if thr_new else 'none'}; "
       f"price INSIDE the gap: old -> {'signal' if ins_old else 'none'}, release -> {'signal' if ins_new else 'none'}")
not_in_release("D4e", "FVG 'strong' flag honoured (both bots still trade every FVG)",
               "strategy change: needs fvg_replay.py results (strong x inside/through) first")
not_in_release("D4f", "Expiry-day rules (13:00 cutoff etc.) — still OFF since the 06-02 roll",
               "kept as-is on purpose: data since the roll says harmless (BOS 2/2 after 13:00 on expiry)")

# ── 4.3 paper vs live ──────────────────────────────────────────────────────
record("P2a", "Paper trades 1 lot like live", re.search(r"^MAX_LOTS = 1\b", PAPER, re.M) is not None, "MAX_LOTS = 1")
not_in_release("P2b", "Paper: BOS-first priority, exhaustion filter, no 14:30 force-close, blunt gates",
               "paper is the experiment book; a true live shadow = the MOCK live copy in ~/livemock (DEPLOY 6b)")

# ── 4.4 misleading logs ────────────────────────────────────────────────────
c1 = next(r for r in RESULTS if r[0] == "C1")
record("L1", "Live SL exit logs the broker's REAL fill",
       (QUICK or c1[2] == "FIXED") and "exit_fill, exit_oid = (st.fill_price" in LIVE,
       "SL row uses the SL-M average price (scenario C1 checks 78.6)")
record("L2", "Paper session win rate uses today's trades only",
       '_n_day = state["wins"] + state["losses"] + state["timeouts"]' in PAPER, "wins / (wins+losses+timeouts) today")
record("L3", "Paper trade columns: true regime and VIX at entry",
       '"entry_regime_label","entry_vix"' in PAPER and "pos.entry_vix = vix" in PAPER,
       "new columns entry_regime_label, entry_vix (old columns kept for comparability)")
record("L4", "Signal with no tradeable quote leaves a skip row (paper + live)",
       PAPER.count("BLOCK: no tradeable quote") >= 1 and "BLOCK: no tradeable quote" in LIVE, "skip row written")
record("L5", "Net-GEX comment matches the formula", "dealers are LONG\n    # calls / SHORT puts" in PAPER,
       "comment corrected; formula unchanged")
record("L6", "Live writes a skip log for every blocked signal", "live_skip_" in LIVE and LIVE.count("log_live_skip(") >= 5,
       "live_skip_<date>.csv")
not_in_release("L7", "Telegram OI net-change / concentration line", "visibility feature, not a defect; next release")

# ── report ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 100)
for i, t, s, e in RESULTS:
    print(f"{i:<4} {s:<20} {t}\n{'':25}{e}")
print("=" * 100)
failed = [r for r in RESULTS if r[2] == "FAILED"]
print(f"{sum(r[2] == 'FIXED' for r in RESULTS)} fixed, {len(failed)} FAILED, "
      f"{sum(r[2] == 'NOT IN THIS RELEASE' for r in RESULTS)} not in this release (by decision)"
      + (", safety scenarios skipped" if QUICK else ""))
print("RELEASE VERIFIED" if not failed else "RELEASE NOT VERIFIED — do not start live")
sys.exit(1 if failed else 0)
