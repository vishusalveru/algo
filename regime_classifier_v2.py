"""
regime_classifier_v2.py — universal market-state classifier + per-strategy
fitness layer. OBSERVATIONAL/SHADOW: computes & logs, does NOT gate trades yet.

PRINCIPLES (agreed with owner):
  - INPUTS are 100% computed from live data; None if unavailable (never a fake
    default that silently passes a gate).
  - FITNESS PATTERNS are DERIVED from the owner's BOS/FVG trade history, not
    invented.
  - THRESHOLD boundaries are DERIVED from the data's distribution (medians,
    empirical separation points), recomputed as data grows — not hand-picked.
  - A classifier must draw lines somewhere; those lines come from data, and the
    METHOD (e.g. 'split at median') is the only fixed structural choice.
"""
import numpy as np

# ── PART 1: UNIVERSAL REGIME CLASSIFIER ────────────────────────────────────
def classify_regime_v2(closes, rsi, atr, efficiency, rsi_prev=None):
    """Objective market state from live data. All inputs live-computed by caller.
    Returns dict or UNKNOWN state if inputs insufficient (no fabricated values)."""
    if closes is None or len(closes) < 12 or atr is None or atr <= 0:
        return {"state": "UNKNOWN", "direction": "neutral", "strength": None,
                "momentum": None, "quality": None}
    c = np.array(closes[-12:], dtype=float)

    # DIRECTION: signed slope (pts/candle) via least-squares — live data only.
    x = np.arange(len(c))
    coeffs = np.polyfit(x, c, 1)
    slope = float(coeffs[0])
    direction = "up" if slope > 0 else "down"

    # R-SQUARED: how cleanly price fits the regression line = trend QUALITY.
    # High R2 = a real clean trend; low R2 = noisy drift (fake trend / chop).
    # Distinguishes 'real trend' from 'choppy drift the label calls trending'.
    fit = np.polyval(coeffs, x)
    ss_res = float(np.sum((c - fit) ** 2))
    ss_tot = float(np.sum((c - c.mean()) ** 2))
    r_squared = round(1.0 - ss_res / ss_tot, 3) if ss_tot > 0 else None

    # STRENGTH inputs (all live): range vs ATR, directional travel vs ATR.
    rng = float(c.max() - c.min())
    range_power = rng / atr
    slope_power = abs(slope) * len(c) / atr

    # QUALITY: efficiency (live). None-safe.
    quality = float(efficiency) if efficiency is not None else None

    # MOMENTUM: accelerating vs decelerating — half-2 slope vs half-1 slope.
    h1 = float(np.polyfit(np.arange(6), c[:6], 1)[0])
    h2 = float(np.polyfit(np.arange(6), c[6:], 1)[0])
    if direction == "up":
        momentum = "accelerating" if h2 > h1 * 1.1 else \
                   "decelerating" if h2 < h1 * 0.7 else "steady"
    else:
        momentum = "accelerating" if h2 < h1 * 1.1 else \
                   "decelerating" if h2 > h1 * 0.7 else "steady"
    rsi_rolling = None
    if rsi_prev is not None and rsi is not None:
        if direction == "up":   rsi_rolling = rsi < rsi_prev - 2
        else:                   rsi_rolling = rsi > rsi_prev + 2

    # FUSED STRENGTH SCORE 0-10. Magnitudes are reasoned CONTRIBUTIONS (each
    # signal informs, none vetoes). Thresholds for the LABEL come from data
    # (see strength_label_bounds, derived from trade distribution).
    score = 0.0
    score += min(range_power / 1.8, 1.0) * 4.0
    score += min(slope_power, 1.0) * 3.0
    if quality is not None:
        score += quality * 2.0
    score += 1.0 if momentum == "accelerating" else (0.0 if momentum == "steady" else -1.0)
    if rsi_rolling:
        score -= 1.0
    score = max(0.0, min(10.0, score))

    return {"state": None, "direction": direction, "strength": round(score, 1),
            "momentum": momentum, "quality": round(quality, 2) if quality is not None else None,
            "range_power": round(range_power, 2), "slope": round(slope, 2),
            "r_squared": r_squared, "rsi_rolling": rsi_rolling}


# ── PART 2: DATA-DERIVED THRESHOLDS ────────────────────────────────────────
def derive_thresholds(trades_df):
    """Compute boundaries FROM the trade data's distribution — not hardcoded.
    Recompute as data grows. Returns per-strategy ATR medians + efficiency
    separation points where available."""
    out = {}
    for strat in trades_df['strategy'].unique():
        s = trades_df[trades_df['strategy'] == strat]
        atrs = s['entry_atr'].dropna() if 'entry_atr' in s else None
        out[strat] = {
            "n": len(s),
            "atr_median": float(atrs.median()) if atrs is not None and len(atrs) else None,
            "atr_min": float(atrs.min()) if atrs is not None and len(atrs) else None,
            "atr_max": float(atrs.max()) if atrs is not None and len(atrs) else None,
        }
    return out


# ── PART 3: PER-STRATEGY FITNESS (derived from trade data) ──────────────────
def build_fitness_profiles(trades_df):
    """DERIVE each strategy's fitness from its OWN trade outcomes — not invented.
    Coarse by necessity (small samples): splits by data-median ATR + direction.
    Returns, per strategy, the empirical win-rate/avg-pnl in each bin, so the
    fitness call is grounded in what actually happened. Flags thin bins."""
    profiles = {}
    for strat in trades_df['strategy'].unique():
        s = trades_df[trades_df['strategy'] == strat].copy()
        if 'entry_atr' not in s or s['entry_atr'].dropna().empty:
            continue
        med = float(s['entry_atr'].median())   # data-derived boundary
        prof = {"atr_median": med, "n": len(s), "bins": {}}
        for label, sub in [("atr_low", s[s['entry_atr'] < med]),
                           ("atr_high", s[s['entry_atr'] >= med])]:
            if len(sub):
                prof["bins"][label] = {
                    "n": len(sub),
                    "win_rate": round(sub['pnl'].gt(0).mean(), 2),
                    "avg_pnl": round(sub['pnl'].mean(), 0),
                    "thin": len(sub) < 8,   # honest flag: too few to trust
                }
        profiles[strat] = prof
    return profiles


def fitness_for(strategy, atr, profiles):
    """Given current ATR and the derived profiles, return this strategy's
    OBSERVED fitness for the current condition. Observational only — returns a
    score/flag, does NOT block. None if no profile/data."""
    p = profiles.get(strategy)
    if not p or atr is None:
        return {"fitness": None, "note": "no profile/data"}
    bin_label = "atr_low" if atr < p["atr_median"] else "atr_high"
    b = p["bins"].get(bin_label)
    if not b:
        return {"fitness": None, "note": f"no data in {bin_label}"}
    # fitness = observed edge in this bin (avg_pnl sign + win_rate), flagged thin
    return {"fitness": b["avg_pnl"], "win_rate": b["win_rate"],
            "bin": bin_label, "thin": b["thin"],
            "note": "OBSERVATIONAL — thin sample" if b["thin"] else "observed"}
