#!/usr/bin/env python3
"""
fvg_replay.py — replay the REAL detect_fvg at every historical FVG trade entry,
using real Upstox 1-minute Nifty candles, to measure:

    * STRONG vs WEAK gaps     (detector's own `strong` flag, which both bots ignore)
    * entry INSIDE the gap vs AFTER price went THROUGH it (the missing lower bound)

READ-ONLY. Places no orders, changes no files except its own output CSV.

Run on the VM (needs today's token in config.py):
    cd ~/algo-trading && python3 fvg_replay.py

Input : consolidated_trades_*.csv   (built by consolidate_trades.py)
Output: fvg_replay_results.csv      + a printed summary
"""

import glob
import os
import sys
import time

import pandas as pd
import requests

import signals                      # the bot's own detector — exact code, not a copy
import live_feeds_v14 as feeds      # reuse NIFTY_KEY + auth headers

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fvg_replay_results.csv")


# ── 1. load FVG trades ─────────────────────────────────────────────────────
def load_trades() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(HERE, "consolidated_trades_*.csv")))
    if not files:
        sys.exit("No consolidated_trades_*.csv found. Run: python3 consolidate_trades.py")
    df = pd.read_csv(files[-1], low_memory=False)
    print(f"trades file: {os.path.basename(files[-1])}  ({len(df)} rows)")
    # paper logs 'ts', live logs 'entry_ts'
    ts = pd.Series(pd.NaT, index=df.index)
    for col in ("entry_ts", "ts"):
        if col in df:
            ts = ts.fillna(pd.to_datetime(df[col], errors="coerce"))
    df["entry_time"] = ts
    df = df[df["strategy"].astype(str).str.contains("FVG", na=False)]
    df = df.dropna(subset=["entry_time"])
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    df["lots"] = pd.to_numeric(df.get("lots"), errors="coerce").fillna(1).clip(lower=1)
    df["pnl_1lot"] = df["pnl"] / df["lots"]
    df["mfe_pts"] = pd.to_numeric(df.get("mfe_pts"), errors="coerce")
    df = df.dropna(subset=["pnl"])
    print(f"FVG trades with an entry time and P&L: {len(df)}")
    return df


# ── 2. fetch real 1-minute Nifty candles for one day ───────────────────────
_cache = {}

def minute_candles(day: str) -> pd.DataFrame | None:
    if day in _cache:
        return _cache[day]
    # same URL form as the bot's own working get_prev_day_ohlc() call
    url = f"https://api.upstox.com/v3/historical-candle/{feeds.NIFTY_KEY}/minutes/1/{day}/{day}"
    try:
        r = requests.get(url, headers=feeds._headers(), timeout=15)
        js = r.json()
    except Exception as e:
        print(f"  {day}: request failed ({e})")
        _cache[day] = None
        return None
    if r.status_code != 200 or js.get("status") != "success":
        print(f"  {day}: HTTP {r.status_code} {str(js)[:160]}")
        _cache[day] = None
        return None
    rows = js["data"]["candles"]           # [ts, open, high, low, close, vol, oi]
    if not rows:
        print(f"  {day}: no candles returned")
        _cache[day] = None
        return None
    c = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "oi"][:len(rows[0])])
    c["ts"] = pd.to_datetime(c["ts"]).dt.tz_localize(None)
    c = c.sort_values("ts").reset_index(drop=True)
    _cache[day] = c
    time.sleep(0.4)                        # be gentle with the API
    return c


# ── 3. rebuild the 5-min frame the bot saw at time t (incl. forming candle) ─
def five_min_at(c1: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    # 1-min candles fully closed before t (a candle starting at 14:15 closes 14:16)
    done = c1[c1["ts"] + pd.Timedelta(minutes=1) <= t]
    if done.empty:
        return pd.DataFrame()
    b = done.set_index("ts").resample("5min", origin="start_day", offset="15min")
    f = pd.DataFrame({"open": b["open"].first(), "high": b["high"].max(),
                      "low": b["low"].min(), "close": b["close"].last()}).dropna()
    return f.reset_index(drop=True)


def gap_position(fvg: dict, ltp: float) -> str:
    top, bottom = fvg["top"], fvg["bottom"]
    lo, hi = min(top, bottom), max(top, bottom)
    if lo <= ltp <= hi:
        return "inside"
    if fvg["type"] == "bullish":
        return "through" if ltp < lo else "above"     # through = fell below the gap
    return "through" if ltp > hi else "below"         # through = rose above the gap


# ── 4. replay every trade ──────────────────────────────────────────────────
def main() -> None:
    trades = load_trades()
    out = []
    for r in trades.itertuples():
        t = pd.Timestamp(r.entry_time)
        c1 = minute_candles(t.strftime("%Y-%m-%d"))
        if c1 is None:
            continue
        # the bot scans every ~30s, so look in a small window around the logged entry
        found = None
        for lag in (0, 30, 60, 90):
            f5 = five_min_at(c1, t - pd.Timedelta(seconds=lag))
            if len(f5) < 6:
                continue
            fvg, _ = signals.detect_fvg(f5)
            if fvg:
                found = (fvg, float(f5["close"].iloc[-1]), lag)
                break
        rec = dict(book=getattr(r, "book", ""), entry_time=t, direction=r.direction,
                   exit_reason=r.exit_reason, mfe_pts=r.mfe_pts, pnl_1lot=r.pnl_1lot)
        if found:
            fvg, ltp, lag = found
            rec.update(replay="match" if fvg["type"] == r.direction else "dir_mismatch",
                       strength="strong" if fvg["strong"] else "weak",
                       gap_size=fvg["size"], age=fvg["age"],
                       position=gap_position(fvg, ltp), lag_s=lag)
        else:
            rec.update(replay="not_found")
        out.append(rec)

    R = pd.DataFrame(out)
    R.to_csv(OUT, index=False)
    print(f"\nwrote {OUT}")

    # ── validation first: only trust this if the replay reproduces the bot ──
    print("\nREPLAY VALIDATION (does the real detector re-find each trade's FVG?)")
    print(R["replay"].value_counts().to_string())
    M = R[R["replay"] == "match"].copy()
    rate = len(M) / max(len(R), 1) * 100
    print(f"match rate: {rate:.0f}%  " +
          ("-> OK to read the results" if rate >= 70 else
           "-> TOO LOW, do not trust the split below; send me the CSV"))
    if M.empty:
        return

    # dedupe: the same signal traded by paper AND live counts once (prefer live)
    M["key"] = M["entry_time"].dt.strftime("%m-%d %H:%M") + M["direction"]
    M = M.sort_values("book").drop_duplicates("key", keep="first")

    def summary(g):
        return pd.Series({"trades": len(g), "win_%": round((g.pnl_1lot > 0).mean() * 100),
                          "pnl_1lot": round(g.pnl_1lot.sum()),
                          "avg": round(g.pnl_1lot.mean()),
                          "mfe<1": int((g.mfe_pts < 1).sum())})

    for col in ["strength", "position"]:
        print(f"\nBY {col.upper()}  (unique signals, 1-lot P&L)")
        print(M.groupby(col).apply(summary).to_string())
    print("\nSTRENGTH x DIRECTION")
    print(M.groupby(["strength", "direction"]).apply(summary).to_string())
    print("\nSTRENGTH x POSITION")
    print(M.groupby(["strength", "position"]).apply(summary).to_string())


if __name__ == "__main__":
    main()
