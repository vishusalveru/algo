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
# [2026-06-02] Minimum absolute premium. Options cheaper than this are
# pathological: tiny SL in rupee terms (whipsaws out), bid vanishes as they
# decay, and a single tick is a large % of premium. The expiry-roll handles the
# main cause, but this is a cheap backstop for any near-worthless option.
MIN_ABS_PREMIUM = 20.0

# You enter mid-swing, not at the extreme of a range. Realistically you
# capture only a fraction of the full expected range in your favour.
CAPTURE_FRAC = 0.45

# [TRAIL FIX] Trailing stop only ARMS once the trade is at least this fraction
# of the way to target (a meaningful profit, not a 1-tick blip). Once armed it
# gives back at most (1-TRAIL_GIVEBACK) of the peak gain, floored at breakeven.
TRAIL_ARM_FRAC = 0.50    # arm only after +50% of the way to target
TRAIL_GIVEBACK = 0.60    # trail at entry + 60% of the gain (give back 40%)

# IV richness guard: if current IV is this multiple above the baseline,
# treat premium as expensive and require a bigger edge.
IV_RICH_MULT = 1.35
# Typical Nifty ATM IV baseline (%). Used as the reference when no live rolling
# IV floor is available. ~13-15% is normal; >baseline*IV_RICH_MULT (~18-20%) is
# rich. Tunable from logged IV data later.
NIFTY_IV_BASELINE = 14.0


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

# [THETA FIX 2026-06-02] Premium decay estimate. Theta isn't linear and
# accelerates near expiry, but a flat per-minute drag is a reasonable first
# approximation and far better than ignoring decay entirely (the old model did).
# ~1.5%/hour = 0.00025/min of premium. Tunable from logged data later.
THETA_PER_MIN = 0.00025


def expected_premium_move(atr_5m: float, delta: float, hold_min: float,
                          entry_premium: float = 0.0) -> float:
    """Expected CAPTUREABLE option-premium move (rupees) over `hold_min`,
    NET OF THETA DECAY.

    Nifty expected range over the hold ≈ atr_5m * sqrt(hold/5).
    You enter mid-swing, capturing only CAPTURE_FRAC of it. Premium move ≈
    |delta| * captureable_nifty_move. Then SUBTRACT theta decay over the hold —
    a slow move that arrives after decay isn't really captureable.
    [reviewer + internal audit agreed: ignoring theta over-stated the move]
    """
    if atr_5m <= 0 or hold_min <= 0:
        return 0.0
    nifty_range = atr_5m * (hold_min / 5.0) ** 0.5
    captureable = nifty_range * CAPTURE_FRAC
    gross = abs(delta) * captureable
    theta_decay = entry_premium * THETA_PER_MIN * hold_min if entry_premium > 0 else 0.0
    return max(0.0, gross - theta_decay)


def dynamic_target_pct(entry_premium: float, atr_5m: float, delta: float,
                       hold_min: float) -> float:
    """Target % of premium derived from the EXPECTED move, not a fixed guess.

    We aim for ~70% of the expected premium move (leave room to actually fill),
    bounded by [MIN_TARGET_PCT, MAX_TARGET_PCT].
    """
    if entry_premium <= 0:
        return MIN_TARGET_PCT
    exp_move = expected_premium_move(atr_5m, delta, hold_min, entry_premium)
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

    # 5b. Minimum absolute premium — reject near-worthless options (see const).
    if entry < MIN_ABS_PREMIUM:
        return False, f"premium Rs.{entry:.1f}<{MIN_ABS_PREMIUM} (too cheap/worthless)", None

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
    exp_move = expected_premium_move(atr_5m, quote.delta, hold, entry)

    # (a) achievability: need expected move to at least reach the target gain
    if exp_move < target_gain:
        return False, (f"unreachable: expMove Rs.{exp_move:.1f} < "
                       f"targetGain Rs.{target_gain:.1f}"), None

    # (a2) TIME REALISM: a target that needs most of the hold to arrive is
    #      fragile — theta and noise erode it. Require the target gain to be
    #      reachable within the FIRST THIRD of the hold (a "fast enough" move).
    early_move = expected_premium_move(atr_5m, quote.delta, hold / 3.0, entry)
    if early_move < target_gain * 0.8:
        return False, (f"too slow: earlyMove Rs.{early_move:.1f} < "
                       f"0.8*targetGain Rs.{target_gain*0.8:.1f} "
                       f"(ATR {atr_5m:.1f} can't move premium fast enough)"), None

    # (b) cost coverage: the gain we capture must beat the round-trip cost
    edge_ratio = target_gain / round_trip_cost
    if edge_ratio < MIN_EDGE_RATIO:
        return False, (f"edge {edge_ratio:.2f}<{MIN_EDGE_RATIO} "
                       f"(targetGain {target_gain:.1f} vs cost {round_trip_cost:.2f})"), None

    # 7. IV richness guard.
    # [IV GATE FIX 2026-06-02] Previously compared quote.iv against iv_floor,
    # but the bot passed iv_floor = quote.iv, making the test quote.iv >
    # quote.iv*1.35 — ALWAYS FALSE (dead code). Now compare against a FIXED
    # Nifty IV baseline so the gate actually fires when premium is rich. If a
    # real rolling IV floor is supplied (>0 and different from quote.iv), use
    # the larger of it and the baseline.
    baseline = NIFTY_IV_BASELINE
    if iv_floor > 0 and abs(iv_floor - quote.iv) > 1e-6:
        baseline = max(baseline, iv_floor)
    if quote.iv > baseline * IV_RICH_MULT:
        # premium is rich (IV well above baseline) -> demand more edge
        if edge_ratio < MIN_EDGE_RATIO * 1.4:
            return False, (f"IV rich ({quote.iv:.1f} > {baseline*IV_RICH_MULT:.1f}) "
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

        # MFE/MAE tracking: best (favorable) and worst (adverse) SELLABLE price
        # seen during the hold, in premium points relative to entry. Updated
        # every check_exit cycle. Lets us tell "signal was right but exit
        # mis-set" apart from "signal was wrong".
        self.mfe_pts = 0.0   # max favorable excursion (premium rose this much)
        self.mae_pts = 0.0   # max adverse excursion (premium fell this much, <=0)
        # confidence the trade entered with (set by the bot after construction)
        self.confidence = indicators.get("confidence", 0)

    def _update_excursion(self, sell_price):
        """Track best/worst sellable price seen, in points vs entry."""
        delta = sell_price - self.entry_premium
        if delta > self.mfe_pts:
            self.mfe_pts = round(delta, 2)
        if delta < self.mae_pts:
            self.mae_pts = round(delta, 2)

    def check_exit(self, current_bid, now_ts=None, trend_still_agrees=True):
        """LONG-OPTION exit test — identical for CE and PE.

        We can only SELL at the bid. Profit when premium (bid) RISES to target.
        Returns (reason|None, sell_price, dur_min).

        [FIX 3] Timeout handling: a trade that is directionally right but slow
        should not be killed flat while the move continues (observed 2026-05-29:
        a PE timed out, then Nifty fell another 162pts). So at timeout, if the
        position is IN PROFIT and the trend still agrees, EXTEND the hold once
        and ratchet a trailing stop up to lock the gain. Losers still exit at
        timeout as before.
        """
        now_ts = now_ts if now_ts is not None else time.time()
        dur_min = (now_ts - self.start_ts) / 60.0
        sell_price = sell_fill_from_bid(current_bid)
        self._update_excursion(sell_price)   # track MFE/MAE every cycle

        # [TRAIL FIX 2026-06-01] Only ARM the trailing stop once the trade has a
        # MEANINGFUL profit — at least TRAIL_ARM_FRAC of the way to target. The
        # old version armed on the first tick above entry (+0.05!), so normal
        # bid/ask flicker tripped it instantly, converting noise into 1-2min
        # losing exits (observed 3x on 2026-06-01). Once armed, the trail never
        # sits below BREAKEVEN, so a trailed exit can't realise a loss.
        gain = sell_price - self.entry_premium
        target_gain = self.target_price - self.entry_premium
        if target_gain > 0 and gain >= TRAIL_ARM_FRAC * target_gain:
            # trail at entry + TRAIL_GIVEBACK of the gain, floored at breakeven
            trail = self.entry_premium + TRAIL_GIVEBACK * gain
            trail = max(trail, self.entry_premium)   # never lock in a loss
            self.trail_stop = max(getattr(self, "trail_stop", 0.0), round(trail, 2))

        # Target hit — take it.
        if sell_price >= self.target_price:
            return "target", sell_price, dur_min
        # Hard stop.
        if sell_price <= self.sl_price:
            return "sl", sell_price, dur_min
        # Trailing stop — only if ARMED (trail_stop set at/above breakeven).
        ts = getattr(self, "trail_stop", 0.0)
        if ts >= self.entry_premium and ts > 0 and sell_price <= ts:
            return "trail", sell_price, dur_min

        # Time stop — with the [FIX 3] extension.
        if dur_min >= self.hold_min:
            in_profit = sell_price > self.entry_premium
            extended = getattr(self, "_extended", False)
            if in_profit and trend_still_agrees and not extended:
                # extend once, by half the original hold, and keep trailing.
                self._extended = True
                self.hold_min = self.hold_min + self.hold_min * 0.5
                return None, sell_price, dur_min   # stay in, now trailing
            return "timeout", sell_price, dur_min

        return None, sell_price, dur_min

    def pnl(self, sell_price: float):
        """P&L in rupees. Long option: (sell - buy) * lots * lotsize."""
        pts = sell_price - self.entry_premium
        return round(pts * self.lots * LOT_SIZE, 0), round(pts, 2)
