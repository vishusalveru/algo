"""
Failure-injection harness for nifty_live_bot.py (release 2026-10-14).

Runs the REAL run() loop against a FAKE broker that keeps true order and
position state (SL-M orders trigger on the option price, cancels/sells can
fail, fills can arrive late). Signals and gate decisions are stubbed so each
scenario controls exactly when a trade happens; execution is entirely real.

    python3 live_harness.py <scenario>      # one scenario, prints PASS/FAIL
    python3 live_harness.py all             # every scenario, each in a fresh process
"""
import os, sys, json, subprocess, datetime as dt, tempfile, shutil

RELEASE = os.environ.get("RELEASE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCENARIOS = ["normal_target", "chain_none", "bid_at_stop_broker_fills_later",
             "sl_filled_between_polls", "cancel_fails_once", "sell_rejected_once",
             "sell_outcome_unknown", "entry_fills_late", "entry_partial_fill",
             "sl_place_fails_flatten", "restart_after_two_sls",
             "startup_positions_error", "startup_pending_order",
             "ltp_outage_while_open", "backstop_no_bid",
             "manual_close_stop_cancelled", "manual_close_stop_resting",
             "no_funds_rejected_after_place", "no_funds_place_error", "no_funds_restart_same_day",
             "mock_target", "mock_sl"]

if len(sys.argv) > 1 and sys.argv[1] == "all":
    ok = True
    for sc in SCENARIOS:
        r = subprocess.run([sys.executable, __file__, sc], capture_output=True, text=True)
        line = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
        print(line if r.returncode == 0 else f"FAIL  {sc}: crashed\n{r.stdout[-1500:]}\n{r.stderr[-2500:]}")
        ok &= (r.returncode == 0 and line.startswith("PASS"))
    print("\nALL SCENARIOS PASSED" if ok else "\nSOME SCENARIOS FAILED")
    sys.exit(0 if ok else 1)

SC = sys.argv[1]
work = tempfile.mkdtemp(prefix=f"lh_{SC}_")
os.chdir(work)
with open("config.py", "w") as fh:
    fh.write('LIVE_TOKEN="x"\nBOT_TOKEN="t"\nCHAT_ID="c"\n')
sys.path[:0] = [work, RELEASE]

import time as _time, pandas as pd

# ── fake clock ──────────────────────────────────────────────────────────────
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CLOCK = {"t": dt.datetime(2026, 10, 15, 9, 50, 0, tzinfo=IST).timestamp(), "cycle": 0}
MAX_CYCLES = {"default": 40}
def fake_time(): return CLOCK["t"]
def fake_sleep(s):
    CLOCK["t"] += s
    if s >= 30:                      # one bot-loop cycle
        CLOCK["cycle"] += 1
        BROKER.tick()
        SCRIPT.on_cycle(CLOCK["cycle"])
        if CLOCK["cycle"] >= SCRIPT.max_cycles:
            raise KeyboardInterrupt
_time.time = fake_time
_time.sleep = fake_sleep

# ── fake broker (true order + position state) ──────────────────────────────
class Broker:
    def __init__(self):
        self.orders = {}; self.n = 0
        self.pos = {}                # instrument -> net qty
        self.prem = 100.0            # option price (LTP); bid = prem - 0.25
        self.fail = {}               # injected failures, consumed as used
        self.positions_error = False; self.orderbook_error = False
        self.preexisting_pending = False
    def _id(self): self.n += 1; return f"OID{self.n}"
    def place(self, p):
        if self.fail.get("reject_next_" + p["transaction_type"].lower() + "_" + p["order_type"].lower(), 0) > 0:
            self.fail["reject_next_" + p["transaction_type"].lower() + "_" + p["order_type"].lower()] -= 1
            return 200, {"status": "error", "errors": [{"message": "RMS reject (injected)"}]}
        oid = self._id()
        o = dict(id=oid, txn=p["transaction_type"], type=p["order_type"], qty=p["quantity"],
                 ins=p["instrument_token"], trig=p.get("trigger_price", 0), status="open",
                 avg=0.0, filled=0)
        self.orders[oid] = o
        if o["type"] == "SL-M":
            o["status"] = "trigger pending"
        elif o["type"] == "MARKET":
            if o["txn"] == "BUY" and self.fail.get("no_funds"):
                o["status"] = "rejected"          # RMS: insufficient funds (after placement)
                o["msg"] = self.fail.get("no_funds_msg", "")
            elif o["txn"] == "BUY" and self.fail.get("entry_late") is not None:
                o["status"] = "open"; o["late_in"] = self.fail.pop("entry_late")
            elif o["txn"] == "BUY" and self.fail.pop("entry_partial", False):
                o["status"] = "open"; o["partial"] = True
            elif o["txn"] == "SELL" and self.fail.pop("sell_stuck", False):
                o["status"] = "open"; o["stuck"] = True
            else:
                self._fill(o, self.prem + (0.3 if o["txn"] == "BUY" else -0.3))
        return 200, {"status": "success", "data": {"order_ids": [oid]}}
    def _fill(self, o, px, qty=None):
        q = o["qty"] if qty is None else qty
        o["status"] = "complete" if qty is None else o["status"]; o["avg"] = round(px, 2); o["filled"] = q
        self.pos[o["ins"]] = self.pos.get(o["ins"], 0) + (q if o["txn"] == "BUY" else -q)
    def details(self, oid):
        if self.fail.get("details_down", 0) > 0:
            self.fail["details_down"] -= 1
            return 500, {"status": "error"}
        o = self.orders.get(oid)
        if not o: return 200, {"status": "error", "errors": [{"message": "no such order"}]}
        if o.get("stuck") and self.fail.get("stuck_unreadable"):
            return 500, {"status": "error"}
        if o.get("late_in") is not None and o["status"] == "open":
            o["late_in"] -= 1                       # fills after N status reads
            if o["late_in"] <= 0: self._fill(o, self.prem + 0.3)
        return 200, {"status": "success", "data": {"status": o["status"], "average_price": o["avg"],
                     "filled_quantity": o["filled"], "pending_quantity": o["qty"] - o["filled"],
                     "status_message": o.get("msg", "")}}
    def cancel(self, oid):
        if self.fail.get("cancel_fail", 0) > 0:
            self.fail["cancel_fail"] -= 1
            return 500, {"status": "error", "errors": [{"message": "cancel failed (injected)"}]}
        o = self.orders.get(oid)
        if o and o["status"] not in ("complete", "cancelled", "rejected"):
            if o.get("partial"):
                self._fill(o, self.prem + 0.3, qty=20); o["filled"] = 20
            if o.get("stuck") and self.fail.get("stuck_unreadable"):
                return 200, {"status": "success", "data": {"order_id": oid}}
            o["status"] = "cancelled"
        return 200, {"status": "success", "data": {"order_id": oid}}
    def tick(self):
        for o in list(self.orders.values()):
            if o["type"] == "SL-M" and o["status"] == "trigger pending" and self.prem <= o["trig"]:
                self._fill(o, self.prem - 0.4)          # SL-M fills at market, some slippage
    def open_orders(self):
        return [o for o in self.orders.values() if o["status"] not in ("complete", "cancelled", "rejected")]

BROKER = Broker()

import live_execution as LE
def _post(self, url, payload): return BROKER.place(payload)
def _get(self, url, params=None):
    if url == LE.DETAILS_URL: return BROKER.details(params["order_id"])
    if url == LE.POSITIONS_URL:
        if BROKER.positions_error: return 503, {"status": "error"}
        return 200, {"status": "success", "data": [{"instrument_token": k, "quantity": v}
                                                   for k, v in BROKER.pos.items()]}
    if url == LE.ORDERBOOK_URL:
        if BROKER.orderbook_error: return 503, {"status": "error"}
        data = [{"order_id": o["id"], "status": o["status"], "order_type": o["type"],
                 "instrument_token": o["ins"]} for o in BROKER.orders.values()]
        if BROKER.preexisting_pending:
            data.append({"order_id": "OLD1", "status": "trigger pending", "order_type": "SL-M",
                         "instrument_token": "NSE_FO|99999"})
        return 200, {"status": "success", "data": data}
    raise AssertionError(url)
class _Resp:
    def __init__(self, code, js): self.status_code, self._js = code, js
    def json(self): return self._js
LE.LiveExecutor._post = _post
LE.LiveExecutor._get = _get
LE.requests.delete = lambda url, headers=None, params=None, timeout=None: _Resp(*BROKER.cancel(params["order_id"]))

# ── market data + bot stubs ─────────────────────────────────────────────────
import nifty_live_bot as B
import decision_layer_v14 as DL
TG = []
B.tg = lambda m: TG.append(m)
B.now_ist = lambda: dt.datetime.fromtimestamp(CLOCK["t"], IST)
B.ist_time = lambda: B.now_ist().time()
INS = "NSE_FO|12345"
MKT = {"ltp_ok": True, "chain_ok": True, "nifty": 23500.0}
def candles(n, start=23400.0):
    ts = pd.date_range("2026-10-15 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    px = [start + i * 2 for i in range(n)]
    return pd.DataFrame({"timestamp": ts, "open": px, "high": [p + 5 for p in px],
                         "low": [p - 5 for p in px], "close": [p + 1 for p in px], "volume": 0, "oi": 0})
B.get_ltp = lambda: MKT["nifty"] if MKT["ltp_ok"] else None
B.get_candles = lambda interval=5: candles(20) if MKT["ltp_ok"] else None
def get_chain(expiry):
    if not MKT["chain_ok"]: return None
    rows = []
    for k in (23400, 23450, 23500, 23550, 23600):
        side = {"market_data": {"bid_price": round(BROKER.prem - 0.25, 2), "ask_price": round(BROKER.prem + 0.25, 2),
                                "bid_qty": 500, "ask_qty": 500, "oi": 200000 if k == 23500 else 1000},
                "option_greeks": {"delta": 0.5, "iv": 12.0}, "instrument_key": INS if k == 23500 else f"NSE_FO|{k}"}
        rows.append({"strike_price": k, "call_options": side, "put_options": side})
    return rows
B.get_chain = get_chain
B.feeds.get_tradeable_expiry = lambda: ("2026-10-20", "2026-10-20", False)
B.feeds.get_india_vix = lambda: 12.0
B.feeds.get_prev_day_ohlc = lambda: {"open": 23300, "high": 23450, "low": 23250, "close": 23400, "date": "2026-10-14"}
B.feeds.get_session_candles = lambda d, interval=5: candles(75, 23300.0)
B.events.is_event_day = lambda d: (False, "")

SIGNAL = {"fire_at": {2}}
def detect(df5, ltp, ind, day_atr_high=0):
    return ("BOS", "bullish", "stub BOS") if CLOCK["cycle"] in SIGNAL["fire_at"] else (None, None, "")
B.detect_live_signal = detect
def decide(**kw):
    d = DL.Decision(); d.enter = True; d.size_mult = 1.0; d.reasons = ["stub: enter"]
    d.day_type = "NORMAL"; d.target_mode = "pct"
    e = kw["quote"].ask + 0.05
    d.plan = {"entry_price": e, "target_price": round(e * 1.10, 2), "sl_price": round(e * 0.8, 2),
              "hold_min": 40.0, "target_pct": 0.10}
    return d
B.decide_entry = decide

class Script:
    max_cycles = 40
    def on_cycle(self, c): pass
SCRIPT = Script()

# ── scenarios ───────────────────────────────────────────────────────────────
def price_path(points):
    """{cycle: premium} applied when that cycle starts."""
    def f(c):
        if c in points: BROKER.prem = points[c]
    return f

if SC == "normal_target":
    SCRIPT.on_cycle = price_path({5: 104, 8: 112})
elif SC == "chain_none":
    def f(c):
        MKT["chain_ok"] = not (5 <= c <= 8)          # chain down while position open
        if c == 12: BROKER.prem = 112
    SCRIPT.on_cycle = f
elif SC == "bid_at_stop_broker_fills_later":
    # bid dips to the stop (premium 80.4 > trigger 80.3 so the LTP-triggered SL-M
    # has not fired), then the premium falls through the trigger
    SCRIPT.on_cycle = price_path({5: 80.4, 9: 79.0})
elif SC == "sl_filled_between_polls":
    def f(c):
        if c == 5:
            BROKER.prem = 70.0; BROKER.tick()        # SL-M fills at 69.6 ...
            BROKER.prem = 112.0                      # ... then price rips to target
    SCRIPT.on_cycle = f
elif SC == "cancel_fails_once":
    def f(c):
        if c == 6: BROKER.prem = 112; BROKER.fail["cancel_fail"] = 7   # whole confirm window fails
    SCRIPT.on_cycle = f
elif SC == "sell_rejected_once":
    def f(c):
        if c == 6: BROKER.prem = 112; BROKER.fail["reject_next_sell_market"] = 1
    SCRIPT.on_cycle = f
elif SC == "sell_outcome_unknown":
    def f(c):
        if c == 6:
            BROKER.prem = 112; BROKER.fail["sell_stuck"] = True; BROKER.fail["stuck_unreadable"] = True
    SCRIPT.on_cycle = f
elif SC == "entry_fills_late":
    BROKER.fail["entry_late"] = 16                   # fills on the 16th status read: after the
                                                     # 15-read poll window, before the cancel lands
    SCRIPT.on_cycle = price_path({8: 112})
elif SC == "entry_partial_fill":
    BROKER.fail["entry_partial"] = True
elif SC == "sl_place_fails_flatten":
    BROKER.fail["reject_next_sell_sl-m"] = 2
elif SC == "restart_after_two_sls":
    with open(f"live_trade_2026-10-15.csv", "w") as fh:
        fh.write(",".join(B.TRADE_COLS) + "\n")
        for i in (1, 2):
            row = {c: "" for c in B.TRADE_COLS}
            row.update(trade_no=i, strategy="BOS", exit_reason="sl", pnl=-1200)
            fh.write(",".join(str(row[c]) for c in B.TRADE_COLS) + "\n")
    SIGNAL["fire_at"] = set(range(1, 30))
elif SC == "startup_positions_error":
    BROKER.positions_error = True
elif SC == "startup_pending_order":
    BROKER.preexisting_pending = True
elif SC == "ltp_outage_while_open":
    def f(c):
        MKT["ltp_ok"] = not (c >= 4)                 # index LTP + candles down from cycle 4
        if c == 7: BROKER.prem = 112                 # chain still works -> target exit must happen
    SCRIPT.on_cycle = f
elif SC == "backstop_no_bid":
    CLOCK["t"] = dt.datetime(2026, 10, 15, 14, 20, 0, tzinfo=IST).timestamp()
    B.TRADE_END = dt.time(14, 30)
    def f(c):
        if c == 4: MKT["chain_ok"] = False           # no bid from 14:22 onward
    SCRIPT.on_cycle = f
    SCRIPT.max_cycles = 140                          # runs past hold expiry
elif SC in ("manual_close_stop_cancelled", "manual_close_stop_resting"):
    def f(c):
        if c == 5:                                   # owner sells in the Upstox app
            BROKER.pos[INS] = 0
            if SC == "manual_close_stop_cancelled":
                for o in BROKER.open_orders(): o["status"] = "cancelled"
        if c == 7: BROKER.prem = 112                 # would have been a target exit
    SCRIPT.on_cycle = f
elif SC in ("no_funds_rejected_after_place", "no_funds_place_error", "no_funds_restart_same_day"):
    SIGNAL["fire_at"] = set(range(1, 60))            # signal present every cycle
    SCRIPT.max_cycles = 60
    if SC == "no_funds_place_error":
        BROKER.fail["reject_next_buy_market"] = 999
    else:
        BROKER.fail["no_funds"] = True
        BROKER.fail["no_funds_msg"] = "Insufficient funds (injected)"
    if SC == "no_funds_restart_same_day":            # earlier run today already halted
        with open("live_state_2026-10-15.json", "w") as fh:
            json.dump({"halted": True, "halt_reason": "3 unfilled entries in a row (margin/account?)"}, fh)
elif SC in ("mock_target", "mock_sl"):
    B.MODE = "mock"                                  # dry-run: executor never calls the broker
    SCRIPT.on_cycle = price_path({5: 112} if SC == "mock_target" else {5: 79.0})
else:
    sys.exit(f"unknown scenario {SC}")

B.run()

# ── checks ──────────────────────────────────────────────────────────────────
rows = []
fn = "live_trade_2026-10-15.csv"
if os.path.exists(fn):
    rows = [r for r in pd.read_csv(fn).to_dict("records") if str(r.get("exit_reason")) not in ("OPEN", "nan")]
net = BROKER.pos.get(INS, 0)
live_orders = BROKER.open_orders()
tg_all = "\n".join(TG)
problems = []
def need(cond, msg):
    if not cond: problems.append(msg)

need(net >= 0, f"broker position is SHORT ({net})")
if SC not in ("startup_positions_error", "startup_pending_order", "restart_after_two_sls",
              "sell_outcome_unknown", "entry_partial_fill"):
    need(net == 0, f"position not flat at end (net {net})")
    need(not live_orders, f"orders left live: {[(o['id'], o['type'], o['status']) for o in live_orders]}")

if SC == "normal_target":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "target" and rows[0]["order_status"] == "filled",
         f"expected 1 target exit, got {[(r['exit_reason'], r['order_status']) for r in rows]}")
elif SC == "chain_none":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "target",
         f"chain outage must not create a phantom SL: {[(r['exit_reason'], r['pnl']) for r in rows]}")
elif SC == "bid_at_stop_broker_fills_later":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "sl" and rows[0]["order_status"] == "sl_filled",
         f"expected broker SL exit: {[(r['exit_reason'], r['order_status']) for r in rows]}")
    need(abs(rows[0]["actual_exit_fill"] - 78.6) < 0.01 if rows else False,
         f"SL exit must use the broker's real fill 78.6, got {rows[0]['actual_exit_fill'] if rows else None}")
elif SC == "sl_filled_between_polls":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "sl", f"expected SL (filled at broker), got {[r['exit_reason'] for r in rows]}")
elif SC == "cancel_fails_once":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "target", f"expected target after retry, got {[r['exit_reason'] for r in rows]}")
    need("EXIT NOT DONE" in tg_all, "operator was not alerted about the failed exit")
elif SC == "sell_rejected_once":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "target", f"expected target after retry, got {[r['exit_reason'] for r in rows]}")
    sells = [o for o in BROKER.orders.values() if o["txn"] == "SELL" and o["type"] == "SL-M"]
    need(len(sells) >= 2, "stop was not re-placed after the rejected sell")
elif SC == "sell_outcome_unknown":
    sells = [o for o in BROKER.orders.values() if o["txn"] == "SELL" and o["type"] == "MARKET"]
    need(len(sells) == 1, f"bot re-sent a SELL with unknown outcome ({len(sells)} market sells) — short risk")
    need("HALTED" in tg_all and "MANUAL ACTION" in tg_all, "operator not told to act")
    need(not rows, "must not log an exit it cannot confirm")
elif SC == "entry_fills_late":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "target", f"late fill not adopted/managed: {[r['exit_reason'] for r in rows]}")
elif SC == "entry_partial_fill":
    need(net == 0, f"partial fill not flattened (net {net})")
    need("PARTIAL" in tg_all and "HALTED" in tg_all, "partial fill not halted/alerted")
    need(not live_orders, f"orders left live: {[(o['id'], o['status']) for o in live_orders]}")
elif SC == "sl_place_fails_flatten":
    need(any(r["exit_reason"] == "no_stop_flatten" for r in rows), "unprotected position not flattened+logged")
    need("HALTED" in tg_all, "no halt after failing to place a stop")
elif SC == "restart_after_two_sls":
    need(not [o for o in BROKER.orders.values() if o["txn"] == "BUY"], "bot traded after restart despite 2 SL hits today")
    need("SL hits 2" in tg_all, "startup banner did not report the restored SL count")
elif SC == "startup_positions_error":
    need(not BROKER.orders and "NOT STARTED" in tg_all, "bot started although positions could not be read")
elif SC == "startup_pending_order":
    need(not [o for o in BROKER.orders.values() if o["id"] != "OLD1"] and "NOT STARTED" in tg_all,
         "bot started next to a pending F&O order")
elif SC == "ltp_outage_while_open":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "target", f"position not managed during LTP outage: {[r['exit_reason'] for r in rows]}")
elif SC in ("manual_close_stop_cancelled", "manual_close_stop_resting"):
    need(not [o for o in BROKER.orders.values() if o["txn"] == "SELL" and o["type"] == "MARKET"],
         "bot sold a position the owner had already closed (would be SHORT)")
    need(len(rows) == 1 and rows[0]["exit_reason"] == "external", f"expected 1 external row: {[r['exit_reason'] for r in rows]}")
    need("closed outside the bot" in tg_all, "owner not told")
elif SC in ("manual_close_stop_cancelled", "manual_close_stop_resting"):
    need(not [o for o in BROKER.orders.values() if o["txn"] == "SELL" and o["type"] == "MARKET"],
         "bot sold a position the owner had already closed (would be SHORT)")
    need(len(rows) == 1 and rows[0]["exit_reason"] == "external", f"expected 1 external row: {[r['exit_reason'] for r in rows]}")
    need("closed outside the bot" in tg_all, "owner not told")
elif SC in ("no_funds_rejected_after_place", "no_funds_place_error"):
    buys = [o for o in BROKER.orders.values() if o["txn"] == "BUY"]
    tries = len(buys) if SC == "no_funds_rejected_after_place" else 999 - BROKER.fail["reject_next_buy_market"]
    need(tries == 3, f"expected exactly 3 entry attempts then halt, got {tries}")
    need(not [o for o in BROKER.orders.values() if o["txn"] == "SELL"], "a SELL/SL-M was sent with no position")
    need("LIVE ENTRIES HALTED" in tg_all, "no halt alert")
    if SC == "no_funds_rejected_after_place":
        need("Insufficient funds" in tg_all, "broker rejection reason not shown to the owner")
    need(not rows, f"trade rows logged for unfilled entries: {rows}")
    need(os.path.exists("live_state_2026-10-15.json"), "halt not persisted for a restart")
elif SC == "no_funds_restart_same_day":
    need(not BROKER.orders, f"bot placed {len(BROKER.orders)} orders after restarting into a halted day")
    need("HALTED today" in tg_all, "startup banner did not show the halt")
elif SC in ("mock_target", "mock_sl"):
    want = "target" if SC == "mock_target" else "sl"
    need(not BROKER.orders, "mock mode sent orders to the broker")
    need(len(rows) == 1 and rows[0]["exit_reason"] == want, f"mock expected 1 {want} exit: {[r['exit_reason'] for r in rows]}")
elif SC == "backstop_no_bid":
    need(len(rows) == 1 and rows[0]["exit_reason"] == "timeout", f"15:25 backstop did not exit without a bid: {[r['exit_reason'] for r in rows]}")

print(("PASS  " if not problems else "FAIL  ") + SC + ("" if not problems else ": " + " | ".join(problems)))
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if not problems else 1)
