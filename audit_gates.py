#!/usr/bin/env python3
"""
audit_gates.py — after the session: for every BLOCKED signal, which gate stopped
it, what price did next, and was the block right?

    cd ~/algo-trading && python3 audit_gates.py              # today
    cd ~/algo-trading && python3 audit_gates.py 2026-10-14   # a past day

Reads (read-only):
    live_skip_<date>.csv   live blocks  (needs the 2026-10-14 logging patch)
    skip_v14_<date>.csv    paper blocks
    scan_v14_<date>.csv    paper scan log = the 30-second Nifty price series

Method: repeats of the same signal+direction within 10 minutes count as ONE
episode. For each episode, look at Nifty over the next 20 minutes:
    best   = furthest it went in the signal's favour (index points)
    worst  = furthest it went against
    verdict: MISSED WINNER if it reached +15 before -10, else BLOCK OK.
This is a PROXY in index points, not option P&L. On June/July data it was
stricter than the real bot (60% vs 80% win rate on trades actually taken),
so treat a borderline "BLOCK OK" with some caution.
"""
import os
import re
import sys
from datetime import date

import pandas as pd

TARGET, STOP, WINDOW_MIN, DEDUPE_MIN = 15.0, 10.0, 20, 10
GENERIC = {"day context", "engine entry gate"}


def gate_of(reasons: str) -> str:
    """The most specific BLOCK reason in a reasons string."""
    r = str(reasons)
    blocks = [b.strip() for b in re.findall(r"BLOCK: ([^;]+)", r)]
    specific = [b for b in blocks if b.lower() not in GENERIC]
    if specific:
        g = specific[0]
    elif "BLOCK: engine entry gate" in r:
        m = re.search(r"engine: ([^;]+)", r)
        g = "engine: " + (m.group(1).strip() if m else "?")
    elif blocks:
        g = blocks[0]
    else:
        g = "unknown"
    g = re.sub(r"\d+\.\d+", "#", g)          # group "ATR 11.5<18.0" and "ATR 12.8<18.0"
    return g[:70]


def episodes(skip: pd.DataFrame) -> pd.DataFrame:
    skip = skip.sort_values("datetime")
    out, last = [], {}
    for r in skip.itertuples():
        key = (r.signal, r.direction)
        if key in last and (r.datetime - last[key]).total_seconds() < DEDUPE_MIN * 60:
            last[key] = r.datetime
            continue
        last[key] = r.datetime
        out.append(dict(datetime=r.datetime, signal=r.signal, direction=r.direction,
                        gate=gate_of(r.reasons), atr=getattr(r, "atr", "")))
    return pd.DataFrame(out)


def outcome(px: pd.DataFrame, t, direction: str):
    sign = 1 if direction == "bullish" else -1
    before = px[px.datetime <= t]
    after = px[(px.datetime > t) & (px.datetime <= t + pd.Timedelta(minutes=WINDOW_MIN))]
    if before.empty or len(after) < 10:
        return None
    entry = before.nifty_ltp.iloc[-1]
    best = worst = 0.0
    for v in after.nifty_ltp:
        move = sign * (v - entry)
        best, worst = max(best, move), min(worst, move)
        if move >= TARGET:
            return entry, best, worst, "MISSED WINNER"
        if move <= -STOP:
            return entry, best, worst, "BLOCK OK"
    return entry, best, worst, ("MISSED WINNER" if sign * (after.nifty_ltp.iloc[-1] - entry) > 2.5
                                else "BLOCK OK")


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    scan_f = f"scan_v14_{day}.csv"
    if not os.path.exists(scan_f):
        sys.exit(f"{scan_f} not found — the paper scan log supplies the price series.")
    px = pd.read_csv(scan_f, usecols=["datetime", "nifty_ltp"], parse_dates=["datetime"]).dropna()

    for book, f in (("LIVE", f"live_skip_{day}.csv"), ("PAPER", f"skip_v14_{day}.csv")):
        print("=" * 92)
        if not os.path.exists(f):
            print(f"{book}: {f} not found" + ("  (live skip logging not deployed yet?)" if book == "LIVE" else ""))
            continue
        sk = pd.read_csv(f, parse_dates=["datetime"], on_bad_lines="skip")
        if sk.empty:
            print(f"{book}: no blocked signals on {day}")
            continue
        ep = episodes(sk)
        rows = []
        for e in ep.itertuples():
            o = outcome(px, e.datetime, e.direction)
            if o:
                rows.append(dict(time=e.datetime.strftime("%H:%M"), signal=e.signal,
                                 dir=e.direction[:4], gate=e.gate, nifty=round(o[0], 1),
                                 best=round(o[1], 1), worst=round(o[2], 1), verdict=o[3]))
        R = pd.DataFrame(rows)
        print(f"{book} {day}: {len(sk)} blocked scans -> {len(ep)} episodes "
              f"({len(R)} with 20 min of price data after)")
        if R.empty:
            continue
        print(R.to_string(index=False))
        s = R.groupby("gate").agg(episodes=("verdict", "size"),
                                  missed_winners=("verdict", lambda x: (x == "MISSED WINNER").sum()))
        s["blocks_ok"] = s.episodes - s.missed_winners
        print("\nBy gate:")
        print(s.sort_values("episodes", ascending=False).to_string())
    print("=" * 92)
    print("Proxy: +15 index pts before -10 within 20 min = MISSED WINNER. Index points, "
          "not option P&L; stricter than the real bot.")


if __name__ == "__main__":
    main()
