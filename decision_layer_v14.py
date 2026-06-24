"""
═══════════════════════════════════════════════════════════════════════════
  decision_layer_v14.py — Single Entry-Decision Pipeline
═══════════════════════════════════════════════════════════════════════════

  Ties together, in ONE place, every gate a trade must pass before entry:

    1. day_context_v14.classify_day_context(...)   — day-type / regime / VIX
    2. trade_engine_v14.evaluate_entry(...)          — pricing / liquidity / edge
    3. CapitalLadder                                  — v10 [F10] de-risk after losses
    4. ReentryLockout                                 — v10 [V6-F3] post-loss bench

  Returns a single Decision object the live bot acts on, with a full audit
  trail (logged per the standing instruction to log every parameter).

  Ported faithfully from v10's live-validated logic; adapted to long premium.
═══════════════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field

from day_context_v14 import classify_day_context
from trade_engine_v14 import OptionQuote, evaluate_entry, LongOptionPosition

# ── Capital ladder (v10 [F10]/[F18]) ───────────────────────────────────────
CAPITAL_NORMAL  = 1.0     # full size multiplier
CAPITAL_REDUCED = 0.5     # after 2 consecutive losses
LOSS_RESULTS    = {"sl", "timeout", "timeout_theta", "timeout_duration"}

# ── Re-entry lockout (v10 [V6-F3]) ──────────────────────────────────────────
HIGH_CONF_REENTRY = 9     # after a loss, need >=9/10 to re-enter
PERFECT_CONF      = 10     # 10/10 always bypasses the lockout


class CapitalLadder:
    """v10 [F10]: 2 losses -> reduced size; 2 wins -> restored."""
    def __init__(self):
        self.consec_losses = 0
        self.consec_wins = 0
        self.reduced = False

    def on_result(self, result: str, pnl: float = 0.0):
        # A "trail" exit, or any timeout/exit that ended IN PROFIT, is a win for
        # streak purposes. Only true losses (SL, or negative-P&L timeouts) count
        # against the capital ladder. [FIX 1/3 alignment]
        is_win = (result == "target") or (pnl > 0)
        is_loss = (result == "sl") or (pnl < 0)
        if is_win:
            self.consec_losses = 0
            self.consec_wins += 1
            if self.reduced and self.consec_wins >= 2:
                self.reduced = False
        elif is_loss:
            self.consec_wins = 0
            self.consec_losses += 1
            if self.consec_losses >= 2:
                self.reduced = True

    def multiplier(self) -> float:
        return CAPITAL_REDUCED if self.reduced else CAPITAL_NORMAL


class ReentryLockout:
    """v10 [V6-F3]: a strategy that just lost is benched unless conf is high."""
    def __init__(self):
        self._last = {}   # strategy -> last result

    def record(self, strategy: str, result: str):
        self._last[strategy] = result

    def allows(self, strategy: str, conf: int):
        last = self._last.get(strategy)
        if last is None:
            return True, "never traded today"
        # [FIX 1] Only a true STOP-LOSS benches a strategy. A timeout means
        # "right direction, ran out of time" — not a misfire — so it does NOT
        # trigger the lockout. This stops one unlucky timeout from sitting the
        # bot out of an entire trend (observed 2026-05-29).
        if last == "sl":
            if conf >= PERFECT_CONF:
                return True, f"perfect {conf}/10 bypass after SL"
            if conf >= HIGH_CONF_REENTRY:
                return True, f"high conf {conf}/10 re-entry after SL"
            return False, f"prev SL; conf {conf}<{HIGH_CONF_REENTRY}"
        if last in LOSS_RESULTS:   # timeout etc. — allowed, just note it
            return True, f"prev {last} (not SL) — re-entry allowed"
        return True, "last was a win"


@dataclass
class Decision:
    enter: bool = False
    size_mult: float = 0.0
    plan: dict | None = None
    quote: OptionQuote | None = None
    day_type: str = "NORMAL"
    target_mode: str = "pct"
    max_hold_min: float | None = None
    reasons: list = field(default_factory=list)


def decide_entry(
    *,
    quote: OptionQuote,
    direction: str,
    strategy_name: str,
    confidence: int,
    atr_5m: float,
    recent_closes: list,
    now_time,
    regime: str = "UNKNOWN",
    strong_breakout: bool = False,
    today=None,
    nearest_expiry: str | None = None,
    vix: float | None = None,
    vix_open: float | None = None,
    gap_pct: float = 0.0,
    is_event_day: bool = False,
    capital: CapitalLadder | None = None,
    lockout: ReentryLockout | None = None,
    min_oi: int = 50000,
    min_qty: int = 25,
    max_spread_pct: float = 0.08,
    atr_day_low: float = 0.0,
    atr_day_high: float = 0.0,
    smart_gap_filter: bool = False,   # PAPER-ONLY gap-filter test; default off = live unchanged
) -> Decision:
    """Run the full gate chain. Returns a Decision with an audit trail."""
    d = Decision()

    # 1. Re-entry lockout (cheapest check first)
    if lockout is not None:
        ok, why = lockout.allows(strategy_name, confidence)
        d.reasons.append(f"lockout: {why}")
        if not ok:
            d.reasons.append("BLOCK: re-entry lockout")
            return d

    # 2. Day context (regime, VIX, expiry, chop, gaps)
    ctx = classify_day_context(
        now_time=now_time, direction=direction, atr_5m=atr_5m,
        recent_closes=recent_closes, today=today, nearest_expiry=nearest_expiry,
        vix=vix, vix_open=vix_open, gap_pct=gap_pct, is_event_day=is_event_day,
        strong_breakout=strong_breakout, regime=regime, strategy_name=strategy_name,
        atr_day_low=atr_day_low, atr_day_high=atr_day_high,
        smart_gap_filter=smart_gap_filter,
    )
    d.reasons.extend([f"ctx: {r}" for r in ctx.reasons])
    d.day_type = ctx.day_type
    d.target_mode = ctx.target_mode
    d.max_hold_min = ctx.max_hold_min
    if not ctx.tradeable:
        d.reasons.append("BLOCK: day context")
        return d

    # 3. Engine entry evaluation (pricing / liquidity / edge / vol)
    ok, why, plan = evaluate_entry(
        quote, atr_5m=atr_5m, iv_floor=quote.iv,
        min_oi=min_oi, min_qty=min_qty, max_spread_pct=max_spread_pct,
    )
    d.reasons.append(f"engine: {why}")
    if not ok:
        d.reasons.append("BLOCK: engine entry gate")
        return d

    # If the day context shortened the hold (e.g. expiry), respect it.
    if ctx.max_hold_min is not None:
        plan["hold_min"] = min(plan["hold_min"], ctx.max_hold_min)

    # 4. Capital ladder sizing
    cap_mult = capital.multiplier() if capital is not None else 1.0
    total_mult = round(ctx.size_mult * cap_mult, 3)
    d.reasons.append(f"size: ctx {ctx.size_mult:.2f} x capital {cap_mult:.2f} = {total_mult:.2f}")

    if total_mult < 0.25:
        d.reasons.append(f"BLOCK: final size {total_mult:.2f} too small")
        return d

    d.enter = True
    d.size_mult = total_mult
    d.plan = plan
    d.quote = quote
    return d
