"""
adaptive_gate.py — volatility-scaled BOS gate (rule B, backtested 4 Nov 2026).

WHY
  Fixed ATR numbers (floor 18, auto-pass 35) meant different things in different
  markets: the typical 5-min ATR was ~21 in June, ~17 in July, ~26 in Oct–Nov.
  Rule B measures the market against its own recent past instead.

RULE B (BOS only; FVG untouched)
  1. ref ATR   = median over the previous 10 sessions of each session's median
                 5-min ATR, read from the paper bot's own scan_v14_<date>.csv logs
                 (the exact definition used in the backtest).
  2. floor     = 0.9 x ref ATR   (replaces the fixed 18 in signals.trend_atr_ok)
  3. spike     = BOS is SKIPPED when ATR >= 35 and efficiency < 0.30
                 (a volatility spike in choppy tape — the 4 Nov SL pattern).
  If the reference cannot be computed (fewer than 5 recent sessions, or logs too
  old) the floor stays at the fixed 18 — the current behaviour — and the reason
  is reported. The spike block does not need the reference and always applies.

Backtest (BOS episodes, bot-like bracket, index points):
  current : Jun 8/100%/+124  Jul 4/75%/+32  Oct-Nov 8/25%/-160
  rule B  : Jun 8/100%/+124  Jul 5/80%/+47  Oct-Nov 1/100%/+16
  floor multiple 0.85-0.95 gives identical results (stable plateau).

Pure helpers; no orders, no network. The caller decides what to do with them.
"""
import datetime
import glob
import os
import re
import statistics

import pandas as pd

import signals

REF_SESSIONS = 10          # sessions in the reference window
REF_MIN_SESSIONS = 5       # fewer than this -> no reference, keep fixed floor
REF_MAX_AGE_DAYS = 120     # ignore scan logs older than this (calendar days); backtest used no age limit
FLOOR_MULT = 0.9           # floor = FLOOR_MULT x ref
FLOOR_SANITY = (12.0, 35.0)  # guard against a corrupt reference; never binds on real data so far
FIXED_FLOOR = 18.0         # current behaviour, used as the fallback
SPIKE_ATR = 35.0           # spike block: ATR at or above this ...
SPIKE_EFF = 0.30           # ... with efficiency below this -> skip BOS
SPIKE_STRATEGIES = ("BOS",)


def session_median_atr(path):
    """Median of the ATR column of one scan log, or None."""
    try:
        s = pd.read_csv(path, usecols=["atr"], on_bad_lines="skip")
        a = pd.to_numeric(s["atr"], errors="coerce").dropna()
        a = a[a > 0]
        return float(a.median()) if len(a) else None
    except Exception:
        return None


def reference_atr(scan_dir, today, n=REF_SESSIONS):
    """(ref or None, info dict). Uses sessions strictly BEFORE `today`."""
    today = pd.Timestamp(today).date()
    oldest = today - datetime.timedelta(days=REF_MAX_AGE_DAYS)
    days = []
    for f in glob.glob(os.path.join(scan_dir, "scan_v14_*.csv")):
        m = re.search(r"scan_v14_(\d{4}-\d{2}-\d{2})\.csv$", f)
        if not m:
            continue
        d = datetime.date.fromisoformat(m.group(1))
        if oldest <= d < today:
            days.append((d, f))
    days.sort()
    meds = []
    for d, f in days[-n:]:
        v = session_median_atr(f)
        if v is not None:
            meds.append((d, v))
    info = {"sessions": len(meds), "from": str(meds[0][0]) if meds else None,
            "to": str(meds[-1][0]) if meds else None}
    if len(meds) < REF_MIN_SESSIONS:
        info["why"] = f"only {len(meds)} recent sessions (need {REF_MIN_SESSIONS})"
        return None, info
    ref = round(statistics.median(v for _, v in meds), 2)
    info["why"] = "ok"
    return ref, info


def floor_for(ref):
    """BOS ATR floor for a reference ATR (fixed 18 when ref is None)."""
    if ref is None:
        return FIXED_FLOOR
    lo, hi = FLOOR_SANITY
    return round(min(max(FLOOR_MULT * float(ref), lo), hi), 2)


def apply_floor(ref):
    """Set signals.ATR_ABS_FLOOR for this process. trend_atr_ok reads it at call
    time and only BOS (a trend strategy) is gated by it. Returns the floor."""
    signals.ATR_ABS_FLOOR = floor_for(ref)
    return signals.ATR_ABS_FLOOR


def spike_block(strategy, atr, efficiency):
    """(block, reason). BOS in a volatility spike on choppy tape."""
    if strategy not in SPIKE_STRATEGIES:
        return False, ""
    try:
        atr = float(atr); eff = float(efficiency)
    except (TypeError, ValueError):
        return False, ""
    if atr >= SPIKE_ATR and eff < SPIKE_EFF:
        return True, f"spike block: ATR {atr:.1f}>={SPIKE_ATR:.0f} with efficiency {eff:.2f}<{SPIKE_EFF}"
    return False, ""


def startup_line(ref, info):
    """One line for the log / Telegram start message."""
    # plain text only: Telegram HTML mode rejects bare < > & characters
    if ref is None:
        return f"Adaptive BOS: no reference ({info.get('why')}) → fixed floor {FIXED_FLOOR:.0f}"
    return (f"Adaptive BOS: ref ATR {ref:.1f} ({info['sessions']} sessions {info['from']} to {info['to']}) "
            f"→ BOS floor {floor_for(ref):.1f} | skip BOS if ATR ≥ {SPIKE_ATR:.0f} and efficiency below {SPIKE_EFF}")


# ─────────────────────────────────────────────────────────────────────────
#  BOS SHADOW SCORECARD — WARNING ONLY (owner decision 4 Nov 2026)
# ─────────────────────────────────────────────────────────────────────────
#  Early warning for the NEXT change in market structure. Uses the paper bot's
#  logs from PREVIOUS sessions only: every BOS signal the paper detector fired
#  (signals_v14_<date>.csv), deduped to one per direction per 10 min, is passed
#  through the same gate the live bot uses today (deployed day_context + rule B)
#  and scored with the bot-like bracket on the paper bot's 30-s prices
#  (scan_v14_<date>.csv): +18 target / -56 stop on the index, hold by ATR,
#  2.5 points cost. Traded or not does not matter — it measures the RULE.
#  If fewer than SCORE_MIN_WINS of the last SCORE_N allowed signals won, the
#  live bot sends a Telegram warning. It NEVER blocks or changes a trade.
#  Backtest: on the old rule it would first have warned on 23 Oct and never in
#  Jun–Jul; on rule B it never fired (rule B had already removed those trades).
SCORE_N = 5
SCORE_MIN_WINS = 3
SCORE_MAX_SESSIONS = 20      # look back at most this many paper sessions
BRACKET_TGT, BRACKET_STOP, BRACKET_COST = 18.0, 56.0, 2.5


def _hold_for(atr):
    return 15 if atr >= 35 else 25 if atr >= 25 else 40 if atr >= 18 else 55


def _session_episodes(log_dir, day, dc):
    """Scored BOS episodes of one past paper session under rule B."""
    sf = os.path.join(log_dir, f"scan_v14_{day}.csv")
    gf = os.path.join(log_dir, f"signals_v14_{day}.csv")
    if not (os.path.exists(sf) and os.path.exists(gf)):
        return []
    sig = pd.read_csv(gf, on_bad_lines="skip")
    if not {"datetime", "signal", "direction"} <= set(sig.columns):
        return []
    sig["datetime"] = pd.to_datetime(sig.datetime, errors="coerce")
    sig = sig[(sig.signal == "BOS") & sig.direction.isin(["bullish", "bearish"])].dropna(subset=["datetime"])
    if sig.empty:
        return []
    sc = pd.read_csv(sf, on_bad_lines="skip")
    sc["datetime"] = pd.to_datetime(sc.datetime, errors="coerce")
    for c in ("nifty_ltp", "atr", "vix", "efficiency"):
        sc[c] = pd.to_numeric(sc[c], errors="coerce") if c in sc else float("nan")
    sc = sc.dropna(subset=["datetime", "nifty_ltp"]).sort_values("datetime")
    gap = 0.0
    kp = os.path.join(log_dir, f"skip_v14_{day}.csv")
    if os.path.exists(kp):
        m = re.search(r"gap day ([+-][0-9.]+)%", open(kp, errors="ignore").read())
        if m:
            gap = float(m.group(1))
    ref, _ = reference_atr(log_dir, day)
    out, last = [], {}
    for r in sig.sort_values("datetime").itertuples():
        k = r.direction
        if k in last and (r.datetime - last[k]).total_seconds() < 600:
            last[k] = r.datetime
            continue
        last[k] = r.datetime
        b = sc[sc.datetime <= r.datetime]
        if b.empty or (r.datetime - b.datetime.iloc[-1]).total_seconds() > 180:
            continue
        x = b.iloc[-1]
        atr = float(x.atr) if x.atr == x.atr else 0.0
        eff = float(x.efficiency) if x.efficiency == x.efficiency else 0.0
        a_hist = b.atr.dropna()
        lo, hi = (float(a_hist.min()), float(a_hist.max())) if len(a_hist) else (0.0, 0.0)
        c5 = b.set_index("datetime").nifty_ltp.resample("5min").last().dropna().tolist()[-30:]
        vix = float(x.vix) if x.vix == x.vix else None
        vix_open = float(sc.vix.dropna().iloc[0]) if sc.vix.notna().any() else None
        signals.ATR_ABS_FLOOR = floor_for(ref)
        ctx = dc.classify_day_context(r.datetime.time(), r.direction, atr, c5, today=r.datetime.date(),
                                      nearest_expiry="2099-12-31", vix=vix, vix_open=vix_open, gap_pct=gap,
                                      is_event_day=False, strong_breakout=True, regime=str(x.regime),
                                      strategy_name="BOS", atr_day_low=lo, atr_day_high=hi)
        if not (bool(ctx.tradeable) and atr >= 12.0 and not spike_block("BOS", atr, eff)[0]):
            continue
        sg = 1 if r.direction == "bullish" else -1
        px = float(x.nifty_ltp)
        a = sc[(sc.datetime > r.datetime) & (sc.datetime <= r.datetime + pd.Timedelta(minutes=_hold_for(atr)))]
        if a.empty:
            continue
        pts = round(sg * (a.nifty_ltp.iloc[-1] - px) - BRACKET_COST, 1)
        for v in a.nifty_ltp:
            mv = sg * (v - px)
            if mv <= -BRACKET_STOP:
                pts = -BRACKET_STOP - BRACKET_COST; break
            if mv >= BRACKET_TGT:
                pts = BRACKET_TGT - BRACKET_COST; break
        out.append({"t": r.datetime.strftime("%d-%b %H:%M"), "dir": r.direction, "atr": round(atr, 1),
                    "eff": round(eff, 2), "pts": pts})
    return out


def bos_scorecard(log_dir, today, n=SCORE_N):
    """(result dict or None, error text). Never raises. Restores the floor it touched."""
    import day_context_v14 as dc
    saved = signals.ATR_ABS_FLOOR
    try:
        today = pd.Timestamp(today).date()
        days = []
        for f in glob.glob(os.path.join(log_dir, "scan_v14_*.csv")):
            m = re.search(r"scan_v14_(\d{4}-\d{2}-\d{2})\.csv$", f)
            if m and datetime.date.fromisoformat(m.group(1)) < today:
                days.append(m.group(1))
        eps = []
        for d in sorted(days, reverse=True)[:SCORE_MAX_SESSIONS]:
            eps = _session_episodes(log_dir, d, dc) + eps
            if len(eps) >= n:
                break
        last = eps[-n:]
        wins = sum(1 for e in last if e["pts"] > 0)
        return {"n": len(last), "wins": wins, "net": round(sum(e["pts"] for e in last), 1),
                "warn": len(last) >= n and wins < SCORE_MIN_WINS, "episodes": last}, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        signals.ATR_ABS_FLOOR = saved


def scorecard_text(res, err=""):
    """Telegram text for the start message."""
    if res is None:
        return f"BOS scorecard: unavailable ({err[:80]})"
    if res["n"] == 0:
        return "BOS scorecard: no allowed BOS signals in recent paper logs yet"
    head = (f"BOS scorecard (last {res['n']} BOS the live rule would take, paper logs): "
            f"{res['wins']} won / {res['n'] - res['wins']} lost, {res['net']:+.0f} index pts")
    if res["warn"]:
        head = ("⚠️ " + head + f"\n⚠️ Below {SCORE_MIN_WINS}/{SCORE_N} — BOS may not be working in this market. "
                "WARNING ONLY: the bot still trades BOS. Your call.")
    return head
