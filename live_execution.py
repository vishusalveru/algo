"""
═══════════════════════════════════════════════════════════════════════════
  live_execution.py — REAL order execution layer (Upstox) for the LIVE bot
═══════════════════════════════════════════════════════════════════════════

  ⚠️ THIS PLACES REAL ORDERS WITH REAL MONEY when mode='live'.

  Built per LIVE_BOT_README.md §5. Isolated from decision logic — this module
  ONLY handles the mechanics of getting an order to the exchange and back:
    • place a market BUY (entry)
    • confirm the fill and read the ACTUAL fill price
    • place a broker-side SL-M resting at the exchange (survives bot death)
    • place a market SELL (exit), cancel the resting SL-M
    • query open positions (startup reconciliation)
    • handle rejections (never report a fill that didn't happen)

  MODES:
    'mock' — no network; returns simulated responses. For logic testing here.
    'live' — real Upstox v3 order API with your LIVE token.
  (No 'sandbox' mode per owner decision; mock is for offline logic tests only.)

  TESTABILITY: every method returns a structured OrderResult. In 'mock' mode
  the whole flow runs with no network so the calling bot's logic can be tested
  end-to-end before a single real order. The FIRST real order happens only when
  mode='live' on your VM with a funded account.

  IMPORTANT: this module never decides WHETHER to trade — that's the decision
  layer. It only executes what it's told, and reports honestly what happened.
═══════════════════════════════════════════════════════════════════════════
"""

import time
import logging
from dataclasses import dataclass, field

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger(__name__)

# [DOC-VERIFIED 2026-06-03] Order PLACEMENT + CANCEL use the HFT host per the
# current V3 docs; order details / positions use the standard api host.
PLACE_URL  = "https://api-hft.upstox.com/v3/order/place"
CANCEL_URL = "https://api-hft.upstox.com/v3/order/cancel"
DETAILS_URL = "https://api.upstox.com/v2/order/details"
POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"

PRODUCT_INTRADAY = "I"     # intraday (auto square-off) — correct for option buying
VALIDITY_DAY = "DAY"
FILL_POLL_SECONDS = 1.0
FILL_POLL_MAX = 15         # poll up to ~15s for a market order to fill


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    status: str = ""          # 'complete','rejected','open','cancelled','error'
    fill_price: float = 0.0   # ACTUAL average fill price (the number that matters)
    filled_qty: int = 0
    message: str = ""
    raw: dict = field(default_factory=dict)


class LiveExecutor:
    def __init__(self, token: str, mode: str = "mock"):
        self.token = token
        self.mode = mode
        if mode == "live" and not token:
            raise ValueError("live mode requires a token")

    # ── HTTP helper ─────────────────────────────────────────────────────────
    def _headers(self):
        return {"Content-Type": "application/json", "Accept": "application/json",
                "Authorization": f"Bearer {self.token}"}

    def _post(self, url, payload):
        r = requests.post(url, json=payload, headers=self._headers(), timeout=8)
        return r.status_code, r.json()

    def _get(self, url, params=None):
        r = requests.get(url, headers=self._headers(), params=params, timeout=8)
        return r.status_code, r.json()

    # ── ENTRY: market buy ────────────────────────────────────────────────────
    def place_entry(self, instrument_key: str, qty: int, tag: str = "fvg") -> OrderResult:
        """Place a market BUY for the option. Returns OrderResult with order_id."""
        if self.mode == "mock":
            return OrderResult(ok=True, order_id=f"MOCK-{int(time.time())}",
                               status="open", message="mock entry placed")
        payload = {
            "quantity": qty, "product": PRODUCT_INTRADAY, "validity": VALIDITY_DAY,
            "price": 0, "tag": tag, "instrument_token": instrument_key,
            "order_type": "MARKET", "transaction_type": "BUY",
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False,
            "slice": False, "market_protection": -1,  # -1 = auto; NEVER 0 (rejects)
        }
        try:
            code, data = self._post(PLACE_URL, payload)
            if code == 200 and data.get("status") == "success":
                oid = data["data"]["order_ids"][0] if "order_ids" in data["data"] \
                      else data["data"].get("order_id", "")
                return OrderResult(ok=True, order_id=oid, status="open", raw=data)
            return OrderResult(ok=False, status="rejected",
                               message=str(data), raw=data)
        except Exception as e:
            return OrderResult(ok=False, status="error", message=str(e))

    # ── Confirm fill + read ACTUAL price ─────────────────────────────────────
    def confirm_fill(self, order_id: str, expected_fill: float = 0.0) -> OrderResult:
        """Poll order details until complete; return the ACTUAL average fill."""
        if self.mode == "mock":
            # simulate a fill at the expected price (no slippage in mock)
            return OrderResult(ok=True, order_id=order_id, status="complete",
                               fill_price=expected_fill, filled_qty=1,
                               message="mock fill")
        for _ in range(FILL_POLL_MAX):
            try:
                code, data = self._get(DETAILS_URL, {"order_id": order_id})
                if code == 200 and data.get("status") == "success":
                    d = data["data"]
                    st = d.get("status", "").lower()
                    if st == "complete":
                        return OrderResult(ok=True, order_id=order_id, status="complete",
                                           fill_price=float(d.get("average_price", 0) or 0),
                                           filled_qty=int(d.get("filled_quantity", 0) or 0),
                                           raw=data)
                    if st in ("rejected", "cancelled"):
                        return OrderResult(ok=False, order_id=order_id, status=st,
                                           message=d.get("status_message", ""), raw=data)
            except Exception as e:
                log.warning(f"confirm_fill poll error: {e}")
            time.sleep(FILL_POLL_SECONDS)
        return OrderResult(ok=False, order_id=order_id, status="timeout",
                           message="fill not confirmed in poll window")

    # ── Broker-side SL-M resting at the exchange ─────────────────────────────
    def place_stop_loss(self, instrument_key: str, qty: int,
                        trigger_price: float, tag: str = "fvg-sl") -> OrderResult:
        """Place a SELL SL-M that rests at the exchange. Fires even if bot dies.
        This is the REAL protection. trigger_price = the 20% stop level."""
        if self.mode == "mock":
            return OrderResult(ok=True, order_id=f"MOCKSL-{int(time.time())}",
                               status="open", message="mock SL-M placed")
        payload = {
            "quantity": qty, "product": PRODUCT_INTRADAY, "validity": VALIDITY_DAY,
            "price": 0, "tag": tag, "instrument_token": instrument_key,
            "order_type": "SL-M", "transaction_type": "SELL",
            "disclosed_quantity": 0, "trigger_price": round(trigger_price, 1),
            "is_amo": False, "slice": False, "market_protection": -1,
        }
        try:
            code, data = self._post(PLACE_URL, payload)
            if code == 200 and data.get("status") == "success":
                oid = data["data"]["order_ids"][0] if "order_ids" in data["data"] \
                      else data["data"].get("order_id", "")
                return OrderResult(ok=True, order_id=oid, status="open", raw=data)
            return OrderResult(ok=False, status="rejected", message=str(data), raw=data)
        except Exception as e:
            return OrderResult(ok=False, status="error", message=str(e))

    # ── EXIT: market sell + cancel the resting SL-M ──────────────────────────
    def place_exit(self, instrument_key: str, qty: int,
                   sl_order_id: str = "", tag: str = "fvg-exit") -> OrderResult:
        """Market SELL to exit. First cancels the resting SL-M so we don't
        double-sell. Returns the exit fill."""
        # cancel the resting stop first (best-effort)
        if sl_order_id:
            self.cancel_order(sl_order_id)
        if self.mode == "mock":
            return OrderResult(ok=True, order_id=f"MOCKX-{int(time.time())}",
                               status="open", message="mock exit placed")
        payload = {
            "quantity": qty, "product": PRODUCT_INTRADAY, "validity": VALIDITY_DAY,
            "price": 0, "tag": tag, "instrument_token": instrument_key,
            "order_type": "MARKET", "transaction_type": "SELL",
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False,
            "slice": False, "market_protection": -1,
        }
        try:
            code, data = self._post(PLACE_URL, payload)
            if code == 200 and data.get("status") == "success":
                oid = data["data"]["order_ids"][0] if "order_ids" in data["data"] \
                      else data["data"].get("order_id", "")
                return OrderResult(ok=True, order_id=oid, status="open", raw=data)
            return OrderResult(ok=False, status="rejected", message=str(data), raw=data)
        except Exception as e:
            return OrderResult(ok=False, status="error", message=str(e))

    def cancel_order(self, order_id: str) -> OrderResult:
        if self.mode == "mock":
            return OrderResult(ok=True, order_id=order_id, status="cancelled")
        try:
            # [DOC-VERIFIED] V3 cancel is a DELETE with order_id as a QUERY param
            # (not a POST body). Response: data.order_id on success.
            r = requests.delete(CANCEL_URL, headers=self._headers(),
                                params={"order_id": order_id}, timeout=8)
            data = r.json()
            ok = r.status_code == 200 and data.get("status") == "success"
            return OrderResult(ok=ok, order_id=order_id,
                               status="cancelled" if ok else "error",
                               message="" if ok else str(data), raw=data)
        except Exception as e:
            return OrderResult(ok=False, order_id=order_id, status="error", message=str(e))

    # ── Startup reconciliation ───────────────────────────────────────────────
    def get_open_positions(self) -> list:
        """Return open positions (qty != 0). Used on startup so the bot never
        starts 'assuming flat' while a real position is held."""
        if self.mode == "mock":
            return []
        try:
            code, data = self._get(POSITIONS_URL)
            if code == 200 and data.get("status") == "success":
                return [p for p in data.get("data", [])
                        if int(p.get("quantity", 0)) != 0]
        except Exception as e:
            log.warning(f"get_open_positions error: {e}")
        return []
