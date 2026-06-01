"""
═══════════════════════════════════════════════════════════════════════════
  day_context_v14.py — Day/Regime DECISIONS for the long-premium gate
═══════════════════════════════════════════════════════════════════════════

  TWO-STRUCTURE BOUNDARY
    signals.py  = WHAT the market is  (classification — single source of truth)
    THIS FILE   = SHOULD we trade now & how big  (decisions only)

  Every classification fact (regime, efficiency_ratio, vix_bias, is_expiry_day,
  the trend/reversal strategy sets, ATR floors, VIX spike %) is IMPORTED from
  signals.py — never re-implemented here. This module only turns those facts
  into block / penalise / size decisions.

  classify_day_context(...) -> DayContext with:
    • tradeable: bool         — hard yes/no for this moment
    • size_mult: float        — 0.0..1.0 position-size multiplier
    • target_mode: str        — "pct" (normal) or "absolute" (expiry theta)
    • max_hold_min: float|None — overrides engine hold on dangerous days
    • reasons: list[str]      — full audit trail (logged per standing instruction)

  Pure logic, no network. The live bot fetches VIX/expiry/gap and passes in.
═══════════════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field
import datetime

import signals   # single source of truth for all classification

# ── Decision-only thresholds (POLICY, not classification) ───────────────────
EXPIRY_STOP       = datetime.time(13, 0)   # no new entries after 1pm on expiry
TRADE_OPEN_SETTLE = datetime.time(9, 45)   # skip opening 30m of unstable ATR
LUNCH_START       = datetime.time(12, 0)
LUNCH_END         = datetime.time(13, 0)
MIN_EFFICIENCY    = 0.30                    # chop penalty threshold (policy)
MIN_VIABLE_SIZE   = 0.25                    # below this, don't bother trading

# Classification constants come from signals.py — imported, not redefined:
#   signals.VIX_SPIKE_PCT, signals.ATR_TREND_MIN, signals.TREND_STRATEGIES,
#   signals.REGIME_BLOCK_TREND, signals.vix_bias, signals.efficiency_ratio,
#   signals.is_expiry_day, signals.is_trend_strategy, signals.GAP_FILTER_PCT


@dataclass
class DayContext:
    tradeable: bool = True
    size_mult: float = 1.0
    target_mode: str = "pct"          # "pct" or "absolute"
    max_hold_min: float | None = None
    day_type: str = "NORMAL"
    reasons: list = field(default_factory=list)

    def block(self, reason: str):
        self.tradeable = False
        self.reasons.append(f"BLOCK: {reason}")
        return self

    def penalise(self, mult: float, reason: str):
        self.size_mult *= mult
        self.reasons.append(f"size×{mult:.2f}: {reason}")
        return self


def classify_day_context(
    now_time: datetime.time,
    direction: str,                      # "bullish"->CE, "bearish"->PE
    atr_5m: float,
    recent_closes: list,                 # for efficiency ratio (trend vs chop)
    *,
    today: datetime.date | None = None,
    nearest_expiry: str | None = None,
    vix: float | None = None,
    vix_open: float | None = None,
    gap_pct: float = 0.0,                # today's open vs prev close, %
    is_event_day: bool = False,          # caller passes from an event calendar
    strong_breakout: bool = False,       # fresh FVG/BOS break = chop resolving
    regime: str = "UNKNOWN",             # from signals.classify_intraday_regime
    strategy_name: str = "",             # which signals.py detector fired
    atr_day_low: float = 0.0,            # [FIX 2] day's observed ATR range
    atr_day_high: float = 0.0,           #         for relative trend gating
) -> DayContext:
    """Turn signals.py classifications into a trade decision for this moment."""

    ctx = DayContext()

    # ── 1. SESSION-TIME GATE ───────────────────────────────────────────────
    if now_time < TRADE_OPEN_SETTLE:
        return ctx.block(f"pre-{TRADE_OPEN_SETTLE} open: ATR unstable")

    # ── 2. EXPIRY-DAY THETA REGIME (uses signals.is_expiry_day) ────────────
    expiry_today = signals.is_expiry_day(today, nearest_expiry)
    if expiry_today:
        ctx.day_type = "EXPIRY"
        ctx.reasons.append("EXPIRY DAY: theta accelerates")
        if now_time >= EXPIRY_STOP:
            return ctx.block(f"expiry day past {EXPIRY_STOP}: theta cliff")
        ctx.target_mode = "absolute"
        ctx.max_hold_min = 12.0
        ctx.penalise(0.5, "expiry-day theta risk")

    # ── 3. EVENT / IV-CRUSH GUARD (uses signals.vix_bias) ──────────────────
    if vix is not None:
        vb = signals.vix_bias(vix)
        if vb == "extreme":
            return ctx.block(f"VIX {vix:.1f} extreme: IV-crush risk")
        if is_event_day and vb == "bearish":
            ctx.penalise(0.4, f"event day + elevated VIX {vix:.1f} (crush risk)")
            ctx.reasons.append("EVENT DAY: IV crush can sink a correct call")
        if vix_open and vix_open > 0:
            spike = (vix - vix_open) / vix_open * 100
            if spike > signals.VIX_SPIKE_PCT:
                ctx.penalise(0.5, f"VIX spiked +{spike:.0f}% from open")

    # ── 4. GAP-DAY HANDLING (uses signals.GAP_FILTER_PCT) ──────────────────
    if abs(gap_pct) >= signals.GAP_FILTER_PCT:
        if ctx.day_type == "NORMAL":
            ctx.day_type = "GAP"
        ctx.reasons.append(f"gap day {gap_pct:+.2f}%")
        gap_dir = "bullish" if gap_pct > 0 else "bearish"
        if gap_dir != direction:
            ctx.penalise(0.6, f"gap {gap_dir} opposes {direction} entry")

    # ── 5. TREND vs CHOP (uses signals.efficiency_ratio) ───────────────────
    #   Chop hurts premium-buyers — BUT every breakout emerges FROM chop. So
    #   penalise size in chop; only HARD-BLOCK severe chop with no breakout.
    er = signals.efficiency_ratio(recent_closes)
    ctx.reasons.append(f"efficiency {er:.2f}")
    if er < MIN_EFFICIENCY:
        ctx.penalise(0.6, f"chop (efficiency {er:.2f}<{MIN_EFFICIENCY})")
        if er < MIN_EFFICIENCY * 0.5 and not strong_breakout:
            return ctx.block(f"severe chop (efficiency {er:.2f}) + no breakout")
        if er < MIN_EFFICIENCY * 0.5 and strong_breakout:
            ctx.reasons.append("severe chop but strong breakout overrides block")

    # ── 6. LUNCH LULL ──────────────────────────────────────────────────────
    if LUNCH_START <= now_time < LUNCH_END and not expiry_today:
        ctx.penalise(0.7, "lunch lull: weak follow-through")

    # ── 6b. REGIME GATE (uses signals.REGIME_BLOCK_TREND) ──────────────────
    is_trend_strat = signals.is_trend_strategy(strategy_name)
    if is_trend_strat and regime in signals.REGIME_BLOCK_TREND:
        return ctx.block(f"{strategy_name} trend-type but regime={regime} "
                         f"(live WR poor here)")

    # ── 6c. ATR MOMENTUM FILTER (uses signals.trend_atr_ok — relative) ─────
    if is_trend_strat:
        atr_ok, atr_why = signals.trend_atr_ok(atr_5m, atr_day_low, atr_day_high)
        if not atr_ok:
            return ctx.block(f"{strategy_name} trend-type: {atr_why}")
        ctx.reasons.append(f"trend ATR ok: {atr_why}")

    # ── 7. FINAL SIZE FLOOR ────────────────────────────────────────────────
    if ctx.size_mult < MIN_VIABLE_SIZE:
        return ctx.block(f"cumulative size {ctx.size_mult:.2f} too low to bother")

    return ctx
