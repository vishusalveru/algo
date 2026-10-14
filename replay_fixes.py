#!/usr/bin/env python3
"""
replay_fixes.py — BACKTEST of the 2026-10-14 data fixes before they go live.

For every BOS / StrongFVG signal your bots logged (signals_v14 / scan_v14,
including the June/July archives), this replays the LIVE entry gate twice
with REAL Upstox candles:

    OLD inputs: the previous close the old code actually used (candles[-2] on
                the raw 7-day list), ATR/RSI/EMA from today's candles only
    NEW inputs: the real previous session's close, ATR/RSI/EMA warmed up with
                the previous session's candles (what the new code does)

and reports every decision that FLIPS, with how each flipped signal would
have done (20-min bracket on real 1-min candles: +15 index pts before -10,
2.5 pts cost — the same proxy used in the audit).

READ-ONLY: no orders, no changes to any bot file. Needs today's token.
    cd ~/algo-trading && python3 replay_fixes.py
Output: replay_fixes_results.csv + replay_fixes_gaps.csv + printed summary.
"""
import datetime as dt
import glob
import io
import os
import re
import tarfile
import time

import pandas as pd
import requests

import signals
import live_feeds_v14 as feeds
import day_context_v14 as dc
import event_calendar_v14 as events
from trade_engine_v14 import ABS_MIN_ATR

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET, STOP, WINDOW, COST = 15.0, 10.0, 20, 2.5
KEY = feeds.NIFTY_KEY


# ── 1. logged signals + logged gaps (current folder + archive/*.tar.gz) ─────
def iter_logs(prefix):
    for f in sorted(glob.glob(os.path.join(HERE, f"{prefix}_*.csv"))):
        yield os.path.basename(f), open(f, "rb").read()
    for tf in sorted(glob.glob(os.path.join(HERE, "archive", "*.tar.gz"))):
        with tarfile.open(tf) as t:
            for m in t.getmembers():
                if os.path.basename(m.name).startswith(prefix + "_") and m.name.endswith(".csv"):
                    yield os.path.basename(m.name), t.extractfile(m).read()


def day_of(name):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return m.group(1) if m else None


def load_signals():
    rows, have_sig_days = [], set()
    for name, raw in iter_logs("signals_v14"):
        d = pd.read_csv(io.BytesIO(raw), on_bad_lines="skip")
        if {"datetime", "signal", "direction"} <= set(d.columns):
            d = d[d.signal.isin(["BOS", "StrongFVG"])]
            d["src"] = "signals"
            rows.append(d); have_sig_days.add(day_of(name))
    for name, raw in iter_logs("scan_v14"):
        if day_of(name) in have_sig_days:
            continue
        d = pd.read_csv(io.BytesIO(raw), on_bad_lines="skip")
        if {"datetime", "signal"} <= set(d.columns) and "direction" not in d.columns:
            continue
        if {"datetime", "signal", "direction"} <= set(d.columns):
            d = d[d.signal.isin(["BOS", "StrongFVG"])]
            d["src"] = "scan"; rows.append(d)
    S = pd.concat(rows, ignore_index=True)
    S["datetime"] = pd.to_datetime(S["datetime"], errors="coerce")
    S = S.dropna(subset=["datetime"]).drop_duplicates(["datetime", "signal", "direction"])
    return S.sort_values("datetime")


def load_scans_vix():
    vix = {}
    for name, raw in iter_logs("scan_v14"):
        d = pd.read_csv(io.BytesIO(raw), usecols=lambda c: c in ("datetime", "vix"), on_bad_lines="skip")
        if "vix" in d:
            d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
            d["vix"] = pd.to_numeric(d["vix"], errors="coerce")
            vix[day_of(name)] = d.dropna()
    return vix


def load_logged_gaps():
    gaps = {}
    for prefix in ("skip_v14", "trade_v14"):
        for name, raw in iter_logs(prefix):
            m = re.search(r"gap day ([+-][0-9.]+)%", raw.decode("utf-8", "ignore"))
            if m:
                gaps[day_of(name)] = float(m.group(1))
    return gaps


def episodes(S):
    out, last = [], {}
    for r in S.itertuples():
        k = (r.signal, r.direction)
        if k in last and (r.datetime - last[k]).total_seconds() < 600:
            last[k] = r.datetime; continue
        last[k] = r.datetime
        out.append(r)
    return out


# ── 2. real candles ─────────────────────────────────────────────────────────
_cache = {}

def api(url):
    if url in _cache:
        return _cache[url]
    for attempt in range(3):
        r = requests.get(url, headers=feeds._headers(), timeout=15)
        if r.status_code == 429:
            time.sleep(2 + attempt * 2); continue
        js = r.json()
        time.sleep(0.35)
        _cache[url] = js if (r.status_code == 200 and js.get("status") == "success") else None
        return _cache[url]
    _cache[url] = None
    return None


def candles(day, unit, interval):
    js = api(f"https://api.upstox.com/v3/historical-candle/{KEY}/{unit}/{interval}/{day}/{day}")
    if not js or not js["data"]["candles"]:
        return None
    c = pd.DataFrame(js["data"]["candles"]).iloc[:, :7]
    c.columns = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
    c["timestamp"] = pd.to_datetime(c["timestamp"]).dt.tz_localize(None)
    return c.sort_values("timestamp").reset_index(drop=True)


def daily_window(day):
    d = dt.date.fromisoformat(day)
    js = api(f"https://api.upstox.com/v3/historical-candle/{KEY}/days/1/{day}/{(d - dt.timedelta(days=7)).isoformat()}")
    return js["data"]["candles"] if js else []


def prev_close_old_and_new(day):
    raw = [c for c in daily_window(day) if str(c[0])[:10] != day]   # live never saw today's daily candle
    old = float(raw[-2][4]) if len(raw) >= 2 else None              # the old code: candles[-2], raw order
    srt = sorted(raw, key=lambda c: str(c[0]))
    new = (str(srt[-1][0])[:10], float(srt[-1][4])) if srt else (None, None)
    return old, new


def frame_at(c5, c1, t):
    """5-min candles the bot saw at time t: completed buckets + the forming one."""
    done = c5[c5.timestamp + pd.Timedelta(minutes=5) <= t]
    bucket = t.floor("5min")
    part = c1[(c1.timestamp >= bucket) & (c1.timestamp + pd.Timedelta(minutes=1) <= t)]
    if len(part):
        done = pd.concat([done, pd.DataFrame([{"timestamp": bucket, "open": part.open.iloc[0],
                          "high": part.high.max(), "low": part.low.min(), "close": part.close.iloc[-1],
                          "volume": 0, "oi": 0}])], ignore_index=True)
    return done.reset_index(drop=True)


def inputs(today_df, prev_df, seeded):
    base = pd.concat([prev_df, today_df], ignore_index=True) if (seeded and prev_df is not None) else today_df
    atr = signals.calc_atr(base)
    e = signals.calc_ema(base)
    have = "ema9" in e.columns
    e9 = float(e["ema9"].iloc[-1]) if have else 0
    e21 = float(e["ema21"].iloc[-1]) if have else 0
    if seeded and not have:
        regime = "UNKNOWN"
    else:
        regime = signals.classify_intraday_regime(today_df, e9, e21, atr)
    return atr, regime, len(base)


def atr_range(c5, c1, t, prev_df, seeded):
    vals = []
    for ts in c5.timestamp[c5.timestamp + pd.Timedelta(minutes=5) <= t]:
        f = c5[c5.timestamp <= ts]
        base = pd.concat([prev_df, f], ignore_index=True) if (seeded and prev_df is not None) else f
        vals.append(signals.calc_atr(base))
    return (min(vals), max(vals)) if vals else (0.0, 0.0)


def outcome(c1, t, direction):
    sign = 1 if direction == "bullish" else -1
    before = c1[c1.timestamp + pd.Timedelta(minutes=1) <= t]
    after = c1[(c1.timestamp >= t.floor("1min")) & (c1.timestamp <= t + pd.Timedelta(minutes=WINDOW))]
    if before.empty or len(after) < 5:
        return None
    px = before.close.iloc[-1]
    for r in after.itertuples():
        if sign * (r.low - px if sign > 0 else r.high - px) <= -STOP:
            return -STOP - COST
        if sign * (r.high - px if sign > 0 else r.low - px) >= TARGET:
            return TARGET - COST
    return sign * (after.close.iloc[-1] - px) - COST


def verdict(t, sig, direction, atr, regime, closes, gap, vix, vix_open, day, lo, hi, hist):
    if hist < 14:
        return False, "only %d candles (placeholder ATR/RSI)" % hist
    # live passes the TRADEABLE (rolled) expiry, so expiry-day rules never fire
    # in live (HANDOFF 7e); a far date reproduces that exactly.
    lo, hi = (min(lo, atr) if lo else atr), max(hi, atr)   # live adds the current ATR first
    ctx = dc.classify_day_context(t.time(), direction, atr, closes, today=dt.date.fromisoformat(day),
                                  nearest_expiry="2099-12-31", vix=vix, vix_open=vix_open, gap_pct=gap,
                                  is_event_day=events.is_event_day(dt.date.fromisoformat(day))[0],
                                  strong_breakout=True, regime=regime, strategy_name=sig,
                                  atr_day_low=lo, atr_day_high=hi)
    if not ctx.tradeable:
        why = [r for r in ctx.reasons if r.startswith("BLOCK")]
        return False, why[0] if why else "blocked"
    if atr < ABS_MIN_ATR:
        return False, f"engine ATR {atr:.1f}<{ABS_MIN_ATR}"
    return True, f"size {ctx.size_mult:.2f}"


def main():
    S = load_signals(); vix_by_day = load_scans_vix(); logged_gap = load_logged_gaps()
    days = sorted({d.strftime("%Y-%m-%d") for d in S.datetime})
    print(f"{len(S)} logged BOS/FVG signal rows over {len(days)} sessions")
    out, gap_rows, prev_cache = [], [], {}
    for day in days:
        c5, c1 = candles(day, "minutes", 5), candles(day, "minutes", 1)
        if c5 is None or c1 is None:
            print(f"  {day}: no candles from API — skipped"); continue
        old_close, (pdate, new_close) = prev_close_old_and_new(day)
        if pdate not in prev_cache:
            prev_cache[pdate] = candles(pdate, "minutes", 5) if pdate else None
        prev5 = prev_cache[pdate]
        open_px = float(c5.open.iloc[0])
        g_old = (open_px - old_close) / old_close * 100 if old_close else 0.0
        g_new = (open_px - new_close) / new_close * 100 if new_close else 0.0
        gap_rows.append(dict(day=day, open=open_px, old_prev_close=old_close, true_prev_close=new_close,
                             true_prev_date=pdate, old_gap_pct=round(g_old, 2), true_gap_pct=round(g_new, 2),
                             logged_gap_pct=logged_gap.get(day)))
        v = vix_by_day.get(day)
        vix_open = float(v.vix.iloc[0]) if v is not None and len(v) else None
        for r in episodes(S[S.datetime.dt.strftime("%Y-%m-%d") == day]):
            t = r.datetime
            today_df = frame_at(c5, c1, t)
            if len(today_df) < 6:
                continue
            closes = today_df.close.astype(float).tolist()[-30:]
            vix = float(v[v.datetime <= t].vix.iloc[-1]) if v is not None and len(v[v.datetime <= t]) else vix_open
            atr_o, reg_o, n_o = inputs(today_df, prev5, False)
            atr_n, reg_n, n_n = inputs(today_df, prev5, True)
            lo_o, hi_o = atr_range(c5, c1, t, prev5, False)
            lo_n, hi_n = atr_range(c5, c1, t, prev5, True)
            ok_o, why_o = verdict(t, r.signal, r.direction, atr_o, reg_o, closes, g_old, vix, vix_open, day, lo_o, hi_o, max(n_o, 14))
            ok_n, why_n = verdict(t, r.signal, r.direction, atr_n, reg_n, closes, g_new, vix, vix_open, day, lo_n, hi_n, n_n)
            out.append(dict(day=day, time=t.strftime("%H:%M"), signal=r.signal, direction=r.direction,
                            logged_regime=getattr(r, "regime", ""), old_regime=reg_o, new_regime=reg_n,
                            old_atr=round(atr_o, 1), new_atr=round(atr_n, 1),
                            old_gap=round(g_old, 2), new_gap=round(g_new, 2),
                            prev_candles=0 if prev5 is None else len(prev5),
                            old_ok=ok_o, new_ok=ok_n, old_why=why_o, new_why=why_n,
                            pts=outcome(c1, t, r.direction)))
    R = pd.DataFrame(out); G = pd.DataFrame(gap_rows)
    if R.empty:
        print("no signals could be replayed (token expired? run after today's login)"); return
    miss = R[R.prev_candles == 0]
    if len(miss):
        print(f"WARNING: {len(miss)} signals had no previous-session candles from the API; "
              "their NEW side is what live does without warm-up (blocked), not the fix itself")
    R.to_csv(os.path.join(HERE, "replay_fixes_results.csv"), index=False)
    G.to_csv(os.path.join(HERE, "replay_fixes_gaps.csv"), index=False)

    print("\n=== VALIDATION — trust the comparison only if the OLD side matches what was logged")
    lg = G.dropna(subset=["logged_gap_pct"])
    if len(lg):
        m = (lg.old_gap_pct - lg.logged_gap_pct).abs() <= 0.15
        print(f"old gap reproduces the logged gap on {m.sum()}/{len(lg)} days "
              f"({m.mean()*100:.0f}%)  [true gap matches logged on "
              f"{((lg.true_gap_pct - lg.logged_gap_pct).abs() <= 0.15).sum()}/{len(lg)}]")
    if "logged_regime" in R and R.logged_regime.notna().any():
        rr = R[R.logged_regime.astype(str).str.len() > 0]
        print(f"old regime reproduces the logged regime on {(rr.old_regime == rr.logged_regime).mean()*100:.0f}% "
              f"of {len(rr)} signals")

    print("\n=== GAP: days whose gap classification changes (|gap| >= 0.5% applies the gap rules)")
    G["old_gapday"] = G.old_gap_pct.abs() >= 0.5; G["new_gapday"] = G.true_gap_pct.abs() >= 0.5
    ch = G[(G.old_gapday != G.new_gapday) | ((G.old_gap_pct * G.true_gap_pct) < 0) & G.old_gapday]
    print(f"{len(ch)} of {len(G)} sessions change" + ("" if ch.empty else ":"))
    if len(ch):
        print(ch[["day", "old_gap_pct", "true_gap_pct", "true_prev_date"]].to_string(index=False))

    print("\n=== ENTRY DECISIONS (live gate), old inputs vs fixed inputs")
    R["change"] = R.apply(lambda x: "both allowed" if x.old_ok and x.new_ok else "both blocked" if not x.old_ok and not x.new_ok
                          else "NEWLY ALLOWED" if x.new_ok else "NEWLY BLOCKED", axis=1)
    def s(g):
        p = g.pts.dropna()
        return pd.Series({"signals": len(g), "with_outcome": len(p),
                          "win_%": round((p > 0).mean() * 100) if len(p) else None,
                          "net_pts": round(p.sum(), 1)})
    print(R.groupby(["change"]).apply(s).to_string())
    print("\nby strategy / direction:")
    print(R.groupby(["change", "signal", "direction"]).apply(s).to_string())
    flips = R[R.change.isin(["NEWLY ALLOWED", "NEWLY BLOCKED"])]
    if len(flips):
        print("\nevery flipped signal:")
        print(flips[["day", "time", "signal", "direction", "old_regime", "new_regime", "old_gap", "new_gap",
                     "old_atr", "new_atr", "old_why", "new_why", "pts"]].to_string(index=False))
    print("\nwrote replay_fixes_results.csv and replay_fixes_gaps.csv")


if __name__ == "__main__":
    main()
