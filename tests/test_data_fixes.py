"""Unit tests for the 2026-10-14 data fixes (no network). Run:
    python3 -I test_data_fixes.py
"""
import os, sys, tempfile, datetime as dt
work = tempfile.mkdtemp(); _here = os.path.abspath(__file__); os.chdir(work)
open("config.py", "w").write('LIVE_TOKEN="x"\nBOT_TOKEN=""\nCHAT_ID=""\n')
RELEASE = os.environ.get("RELEASE_DIR", os.path.dirname(os.path.dirname(_here)))
sys.path[:0] = [work, RELEASE]
import pandas as pd
import live_feeds_v14 as F
import signals

ok = True
def check(cond, name, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")

# ── D1: previous close ──────────────────────────────────────────────────────
class R:
    def __init__(self, js): self.status_code, self._js = 200, js
    def json(self): return self._js
today = F.today_ist()
days = []
d = today
while len(days) < 6:                      # 6 sessions incl. today, skipping weekends
    if d.weekday() < 5: days.append(d)
    d -= dt.timedelta(days=1)
closes = {day: 23000 + i * 100 for i, day in enumerate(sorted(days))}
def rows(include_today, newest_first):
    r = [[f"{day.isoformat()}T00:00:00+05:30", c - 50, c + 80, c - 90, c, 0, 0]
         for day, c in closes.items() if include_today or day != today]
    r.sort(key=lambda x: x[0], reverse=newest_first)
    return r
expected_prev = max(day for day in closes if day < today)
for inc in (False, True):
    for newest in (True, False):
        F.requests.get = lambda url, headers=None, timeout=None, params=None, _r=rows(inc, newest): \
            R({"status": "success", "data": {"candles": _r}})
        p = F.get_prev_day_ohlc()
        check(p and p["date"] == expected_prev.isoformat() and p["close"] == closes[expected_prev],
              f"prev close = last session before today (today in list={inc}, newest-first={newest})",
              f"got {p and p['date']} {p and p['close']}")
F.requests.get = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
check(F.get_prev_day_ohlc() is None, "prev close: API failure -> None (gap gate off, not a wrong number)")

# ── seeded frame ────────────────────────────────────────────────────────────
def frame(n, start, step, t0="2026-10-15 09:15"):
    ts = pd.date_range(t0, periods=n, freq="5min", tz="Asia/Kolkata")
    px = [start + i * step for i in range(n)]
    return pd.DataFrame({"timestamp": ts, "open": px, "high": [p + 4 for p in px],
                         "low": [p - 4 for p in px], "close": [p + (2 if step >= 0 else -2) for p in px],
                         "volume": 0, "oi": 0})
prev = frame(75, 23000, 1, "2026-10-14 09:15")
today6 = frame(6, 23100, 3)
seed = F.seeded_frame(prev, today6)
check(len(seed) == 81 and seed["close"].iloc[-1] == today6["close"].iloc[-1], "seeded frame = prev + today, ends at today's last candle")
check(F.seeded_frame(None, today6) is today6, "seeded frame falls back to today when there is no history")

# ── D3: no placeholders at 09:45 with history ───────────────────────────────
check(signals.calc_atr(today6) == 20.0 and signals.calc_rsi(today6) == 50.0,
      "baseline: today-only at 09:45 gives the placeholders ATR 20.0 / RSI 50.0")
a, r = signals.calc_atr(seed), signals.calc_rsi(seed)
check(a != 20.0 and r != 50.0, "seeded at 09:45: ATR/RSI are computed, not placeholders", f"ATR {a} RSI {r}")

# ── D2: regime symmetry before 13:25 ────────────────────────────────────────
import importlib
sys.modules.pop("nifty_live_bot", None)
B = importlib.import_module("nifty_live_bot")
up_today, dn_today = frame(30, 23000, 6), frame(30, 23400, -6)      # ~11:45, clean trends
up_prev, dn_prev = frame(75, 22800, 3, "2026-10-14 09:15"), frame(75, 23600, -3, "2026-10-14 09:15")
B.LIVE_FIX["seeded_indicators"] = False
old_up = B.compute_indicators(up_today, up_today, up_today)["regime"]
old_dn = B.compute_indicators(dn_today, dn_today, dn_today)["regime"]
check(old_up != "TRENDING_BULL" and old_dn == "TRENDING_BEAR",
      "baseline (old behaviour): clean uptrend at 11:45 is NOT TRENDING_BULL, downtrend IS TRENDING_BEAR",
      f"up={old_up} down={old_dn}")
B.LIVE_FIX["seeded_indicators"] = True
new_up = B.compute_indicators(up_today, up_today, up_today, up_prev)["regime"]
new_dn = B.compute_indicators(dn_today, dn_today, dn_today, dn_prev)["regime"]
check(new_up == "TRENDING_BULL" and new_dn == "TRENDING_BEAR",
      "seeded: uptrend -> TRENDING_BULL, downtrend -> TRENDING_BEAR (symmetric)", f"up={new_up} down={new_dn}")
no_hist = B.compute_indicators(up_today, up_today, up_today, None)
check(no_hist["regime"] == "UNKNOWN", "no history and no EMAs -> regime UNKNOWN (never from zero EMAs)",
      f"got {no_hist['regime']}")
check(B.compute_indicators(today6, today6, today6, None)["hist_candles"] < 14,
      "no history at 09:45 -> hist_candles < 14 (entries blocked, not placeholder-driven)")

print("\nALL DATA-FIX TESTS PASSED" if ok else "\nSOME DATA-FIX TESTS FAILED")
sys.exit(0 if ok else 1)
