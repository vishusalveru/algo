"""
═══════════════════════════════════════════════════════════════════════════
  trade_engine_v14.py — Corrected, Volatility-Aware Long-Options Engine
═══════════════════════════════════════════════════════════════════════════

  WHAT THIS FIXES (vs v13)
    1. LONG-ONLY semantics are correct for BOTH CE and PE.
         You are LONG premium. You profit when PREMIUM RISES — period.
         CE and PE use the SAME exit test. (The earlier feedback that flipped
         PE comparators was WRONG; this module does NOT do that.)
    2. REALISTIC FILLS
         Entry  = ASK + 1 tick impact   (you pay up to buy)
         Exit   = BID - 1 tick impact   (you sell down to exit)
         No flat point-based slippage constant. Spread IS the cost.
    3. ASK-SIDE LIQUIDITY gate (need sellers to buy from), plus bid-side
       for the exit later.
    4. VOLATILITY AWARENESS (the core request):
         a. Entry gate: skip if expected move over the planned hold is too
            small to clear spread + target. No edge => no trade.
         b. Dynamic target: target premium % derived from EXPECTED MOVE
            (ATR-based), not a fixed 10% guess.
         c. Dynamic time stop: low ATR => allow more time; high ATR => less.
         d. IV sanity: avoid buying when IV is extremely rich relative to the
            recent range (you'd bleed on IV mean-reversion + theta).

  This module is pure logic: deterministic, unit-testable, no network calls.
  The live bot imports these functions/classes.

  TICK = 0.05 for NSE option premia.
═══════════════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass
import time

TICK = 0.05
LOT_SIZE = 65  # Nifty lot size (reference)

# ── Volatility / target configuration ──────────────────────────────────────
# Delta of the option we trade (ATM ≈ 0.5). Premium move ≈ delta * nifty_move.
# Expected nifty move over a hold of H minutes ≈ ATR_5m * sqrt(H / 5).
# (ATR here is the 5-minute ATR in index points.)

MIN_EDGE_RATIO = 1.8     # target gain must be >= 1.8x round-trip cost
MIN_TARGET_PCT = 0.04    # never target less than 4% (not worth the risk)
MAX_TARGET_PCT = 0.20    # cap target at 20% (greed control)
SL_PCT = 0.20            # hard stop: 20% of entry premium
ABS_MIN_ATR = 12.0       # below this 5m-ATR, market is too dead to buy premium

# You enter mid-swing, not at the extreme of a range. Realistically you
# capture only a fraction of the full expected range in your favour.
CAPTURE_FRAC = 0.45

# IV richness guard: if current IV is this multiple above its recent floor,
# treat premium as expensive and require a bigger edge.
IV_RICH_MULT = 1.35


@dataclass
class OptionQuote:
    strike: float
    opt_type: str        # "CE" or "PE"
    bid: float
    ask: float
    bid_qty: int
    ask_qty: int
    oi: int
    delta: float         # signed from API; we store abs for sizing
    iv: float            # percentage, e.g. 13.9
    instr_key: str = ""

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if (self.bid and self.ask) else 0.0

    @property
    def spread_pct(self) -> float:
        return self.spread / self.ask if self.ask > 0 else 999.0


# ── Realistic fill helpers ─────────────────────────────────────────────────

def buy_fill(quote: OptionQuote) -> float:
    """Price you actually pay to BUY: ask plus one tick of impact."""
    return round(quote.ask + TICK, 2)

def sell_fill_from_bid(bid: float) -> float:
    """Price you actually receive to SELL: bid minus one tick of impact."""
    return round(max(0.0, bid - TICK), 2)


# ── Volatility model ───────────────────────────────────────────────────────

def expected_premium_move(atr_5m: float, delta: float, hold_min: float) -> float:
    """Expected CAPTUREABLE option-premium move (rupees) over `hold_min`.

    Nifty expected range over the hold ≈ atr_5m * sqrt(hold/5).
    But you enter mid-swing, not at the extreme — you realistically capture
    only a fraction of the range in your favor. CAPTURE_FRAC models this.
    Premium move ≈ |delta| * captureable_nifty_move.
    """
    if atr_5m <= 0 or hold_min <= 0:
        return 0.0
    nifty_range = atr_5m * (hold_min / 5.0) ** 0.5
    captureable = nifty_range * CAPTURE_FRAC
    return abs(delta) * captureable


def dynamic_target_pct(entry_premium: float, atr_5m: float, delta: float,
                       hold_min: float) -> float:
    """Target % of premium derived from the EXPECTED move, not a fixed guess.

    We aim for ~70% of the expected premium move (leave room to actually fill),
    bounded by [MIN_TARGET_PCT, MAX_TARGET_PCT].
    """
    if entry_premium <= 0:
        return MIN_TARGET_PCT
    exp_move = expected_premium_move(atr_5m, delta, hold_min)
    raw_pct = 0.70 * exp_move / entry_premium
    return max(MIN_TARGET_PCT, min(MAX_TARGET_PCT, raw_pct))


def dynamic_hold_minutes(atr_5m: float) -> float:
    """How long we're willing to hold, scaled to volatility.

    Dead tape => give the move more time. Fast tape => take it quickly.
    """
    if atr_5m >= 35:
        return 15.0
    if atr_5m >= 25:
        return 25.0
    if atr_5m >= 18:
        return 40.0
    return 55.0  # very low vol: needs lots of time (often we won't trade at all)


# ── Entry gate (the volatility-aware decision) ──────────────────────────────

def evaluate_entry(quote: OptionQuote, atr_5m: float, iv_floor: float,
                   min_oi: int, min_qty: int, max_spread_pct: float):
    """Decide whether this option is worth BUYING right now.

    Returns (ok: bool, reason: str, plan: dict|None).
    plan contains entry_price, target_price, sl_price, hold_min, target_pct.
    """
    # 1. Both sides must exist
    if quote.bid <= 0 or quote.ask <= 0:
        return False, "no two-sided quote", None

    # 2. Liquidity: need sellers (ask side) to buy, buyers (bid side) to exit
    if quote.ask_qty < min_qty:
        return False, f"ask_qty {quote.ask_qty}<{min_qty}", None
    if quote.bid_qty < min_qty:
        return False, f"bid_qty {quote.bid_qty}<{min_qty}", None
    if quote.oi < min_oi:
        return False, f"oi {quote.oi}<{min_oi}", None

    # 3. Spread gate
    if quote.spread_pct > max_spread_pct:
        return False, f"spread {quote.spread_pct*100:.1f}%>{max_spread_pct*100:.0f}%", None

    # 4. Volatility floor — too dead to buy premium
    if atr_5m < ABS_MIN_ATR:
        return False, f"ATR {atr_5m:.1f}<{ABS_MIN_ATR} (dead tape)", None

    # 5. Build the plan using realistic entry fill
    entry = buy_fill(quote)                 # pay the ask + tick
    hold = dynamic_hold_minutes(atr_5m)
    tgt_pct = dynamic_target_pct(entry, atr_5m, quote.delta, hold)
    target = round(entry * (1 + tgt_pct), 2)
    sl = round(entry * (1 - SL_PCT), 2)

    # 6. Edge test — two conditions must BOTH hold:
    #    (a) ACHIEVABILITY: the premium gain we need to hit target must be
    #        reachable within the expected move over the hold. If the target
    #        gain exceeds the expected move, we're hoping, not trading.
    #    (b) COST COVERAGE: that same target gain must clear the round-trip
    #        cost by MIN_EDGE_RATIO. A target smaller than the spread is a
    #        guaranteed loser.
    round_trip_cost = quote.spread + 2 * TICK
    if round_trip_cost <= 0:
        return False, "degenerate cost", None

    target_gain = target - entry                      # rupees we aim to capture
    exp_move = expected_premium_move(atr_5m, quote.delta, hold)

    # (a) achievability: need expected move to at least reach the target gain
    if exp_move < target_gain:
        return False, (f"unreachable: expMove Rs.{exp_move:.1f} < "
                       f"targetGain Rs.{target_gain:.1f}"), None

    # (a2) TIME REALISM: a target that needs most of the hold to arrive is
    #      fragile — theta and noise erode it. Require the target gain to be
    #      reachable within the FIRST THIRD of the hold (a "fast enough" move).
    early_move = expected_premium_move(atr_5m, quote.delta, hold / 3.0)
    if early_move < target_gain * 0.8:
        return False, (f"too slow: earlyMove Rs.{early_move:.1f} < "
                       f"0.8*targetGain Rs.{target_gain*0.8:.1f} "
                       f"(ATR {atr_5m:.1f} can't move premium fast enough)"), None

    # (b) cost coverage: the gain we capture must beat the round-trip cost
    edge_ratio = target_gain / round_trip_cost
    if edge_ratio < MIN_EDGE_RATIO:
        return False, (f"edge {edge_ratio:.2f}<{MIN_EDGE_RATIO} "
                       f"(targetGain {target_gain:.1f} vs cost {round_trip_cost:.2f})"), None

    # 7. IV richness guard
    if iv_floor > 0 and quote.iv > iv_floor * IV_RICH_MULT:
        # premium is rich; demand even more edge
        if edge_ratio < MIN_EDGE_RATIO * 1.4:
            return False, (f"IV rich ({quote.iv:.1f} vs floor {iv_floor:.1f}) "
                           f"and edge {edge_ratio:.2f} insufficient"), None

    plan = {
        "entry_price": entry,
        "target_price": target,
        "sl_price": sl,
        "target_pct": round(tgt_pct, 4),
        "hold_min": hold,
        "exp_move": round(exp_move, 2),
        "round_trip_cost": round(round_trip_cost, 2),
        "edge_ratio": round(edge_ratio, 2),
    }
    return True, "OK", plan


# ── Position ────────────────────────────────────────────────────────────────

class LongOptionPosition:
    """A LONG option (CE or PE). Profit when premium RISES. Same for both."""

    def __init__(self, trade_no, strategy, direction, entry_nifty,
                 quote: OptionQuote, plan: dict, lots: int,
                 indicators: dict, start_ts: float | None = None):
        self.trade_no = trade_no
        self.strategy = strategy
        self.direction = direction          # "bullish"->CE, "bearish"->PE
        self.entry_nifty = entry_nifty
        self.quote = quote
        self.option_type = quote.opt_type
        self.option_strike = quote.strike
        self.option_iv = quote.iv
        self.option_delta = abs(quote.delta)
        self.option_spread = quote.spread

        self.entry_premium = plan["entry_price"]   # realistic ASK+tick fill
        self.target_price = plan["target_price"]
        self.sl_price = plan["sl_price"]
        self.target_pct = plan["target_pct"]
        self.hold_min = plan["hold_min"]
        self.plan = plan

        self.lots = lots
        self.capital = self.entry_premium * lots * LOT_SIZE

        self.entry_rsi = indicators.get("rsi", 50)
        self.entry_atr = indicators.get("atr", 0)
        self.entry_trend = indicators.get("trend", "neutral")
        self.start_ts = start_ts if start_ts is not None else time.time()

    def check_exit(self, current_bid: float, now_ts: float | None = None):
        """LONG-OPTION exit test — identical for CE and PE.

        We can only SELL at the bid. Profit when premium (bid) RISES to target.
        Returns (reason|None, sell_price, dur_min).
        """
        now_ts = now_ts if now_ts is not None else time.time()
        dur_min = (now_ts - self.start_ts) / 60.0
        sell_price = sell_fill_from_bid(current_bid)

        # Time stop (volatility-scaled hold)
        if dur_min >= self.hold_min:
            return "timeout", sell_price, dur_min

        # Long option: premium UP = profit (CE and PE alike)
        if sell_price >= self.target_price:
            return "target", sell_price, dur_min
        if sell_price <= self.sl_price:
            return "sl", sell_price, dur_min

        return None, sell_price, dur_min

    def pnl(self, sell_price: float):
        """P&L in rupees. Long option: (sell - buy) * lots * lotsize."""
        pts = sell_price - self.entry_premium
        return round(pts * self.lots * LOT_SIZE, 0), round(pts, 2)
