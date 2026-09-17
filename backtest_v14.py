#!/usr/bin/env python3
"""
backtest_v14.py — Replay the real 2026-05-26 session through the corrected
volatility-aware engine and compare against v13's logged results.

We reconstruct bid/ask from the logged mid + spread, then test:
  - Does the volatility entry gate REJECT these low-vol trades? (it should)
  - If forced through, what is the REALISTIC P&L with proper fills?
  - How does dynamic target/hold differ from the fixed 10%/30min?
"""

import pandas as pd
from trade_engine_v14 import (
    OptionQuote, evaluate_entry, LongOptionPosition,
    dynamic_target_pct, dynamic_hold_minutes, expected_premium_move,
    buy_fill, sell_fill_from_bid, ABS_MIN_ATR, MIN_EDGE_RATIO,
)

TRADES = "/mnt/user-data/uploads/trade_v13_2026-05-26.csv"

MIN_OI = 50000
MIN_QTY = 25
MAX_SPREAD_PCT = 0.08


def reconstruct_quote(row):
    """Rebuild an OptionQuote from logged mid (entry_premium) + spread."""
    mid = row["entry_premium"]
    spread = row["option_spread"]
    half = spread / 2
    bid = round(mid - half, 2)
    ask = round(mid + half, 2)
    delta = row["option_delta"]
    return OptionQuote(
        strike=row["option_strike"],
        opt_type=row["option_type"],
        bid=bid, ask=ask,
        bid_qty=100, ask_qty=100,   # assume adequate (we have OI but not qty logged)
        oi=100000,                  # these were liquid ATM strikes
        delta=delta, iv=row["option_iv"],
    )


def main():
    df = pd.read_csv(TRADES)
    print("=" * 74)
    print("BACKTEST v14 — replaying real session through corrected engine")
    print("=" * 74)

    v13_total = df["pnl_est"].sum()
    print(f"\nv13 logged total P&L: Rs.{v13_total:+.0f} (4 'wins', all actually losers)\n")

    gated_out = 0
    forced_pnl = 0.0
    print("-" * 74)
    for _, row in df.iterrows():
        q = reconstruct_quote(row)
        atr = row["entry_atr"]

        ok, reason, plan = evaluate_entry(
            q, atr_5m=atr, iv_floor=q.iv,  # treat current IV as its own floor (neutral)
            min_oi=MIN_OI, min_qty=MIN_QTY, max_spread_pct=MAX_SPREAD_PCT,
        )

        tno = int(row["trade_no"])
        print(f"Trade #{tno} ({q.opt_type}, ATR {atr:.1f}):")
        if not ok:
            gated_out += 1
            print(f"  ENTRY GATE: ❌ REJECTED — {reason}")
            print(f"  => Correct call. This trade should NOT have been taken.\n")
            continue

        # If the gate passed, simulate with realistic fills using the logged
        # exit mid as the prevailing mid at exit, converted to a bid.
        print(f"  ENTRY GATE: ✅ {reason}")
        print(f"    plan: target {plan['target_pct']*100:.1f}%  hold {plan['hold_min']:.0f}m  "
              f"edge {plan['edge_ratio']:.2f}  expMove Rs.{plan['exp_move']:.1f}")
        exit_mid = row["exit_premium"]
        exit_bid = exit_mid - q.spread / 2
        sell = sell_fill_from_bid(exit_bid)
        pts = sell - plan["entry_price"]
        pnl = pts * 65
        forced_pnl += pnl
        print(f"    realistic: buy Rs.{plan['entry_price']:.2f} sell Rs.{sell:.2f} "
              f"=> Rs.{pnl:+.0f}\n")

    print("=" * 74)
    print("RESULT")
    print("=" * 74)
    print(f"Trades rejected by volatility gate: {gated_out}/4")
    if gated_out == 4:
        print("""
✅ The corrected engine REJECTS all 4 trades.

   Reason: 5m-ATR was ~18 and the ATM premium was ~Rs.190 with a tiny
   delta-driven expected move. Over any realistic hold, the expected
   premium move did not clear the round-trip cost by the required margin.

   This is the correct professional decision: in that regime, buying
   ATM premium has no edge. v13 took the trades anyway and (after honest
   accounting) bled. v14 simply does not trade.

   The fix is not 'make these trades win' — they were un-winnable.
   The fix is 'do not take no-edge trades', which v14 enforces.
""")
    else:
        print(f"Forced realistic P&L on passed trades: Rs.{forced_pnl:+.0f}")

    # Show what WOULD make the gate open: a higher-vol scenario
    print("-" * 74)
    print("SANITY: same option in a HIGHER-vol regime (ATR 30):")
    q = reconstruct_quote(df.iloc[0])
    ok, reason, plan = evaluate_entry(q, 30.0, q.iv, MIN_OI, MIN_QTY, MAX_SPREAD_PCT)
    if ok:
        print(f"  ✅ ACCEPTED — target {plan['target_pct']*100:.1f}%  "
              f"hold {plan['hold_min']:.0f}m  edge {plan['edge_ratio']:.2f}")
        print("  => In a live tape, the same setup becomes tradeable. Gate works both ways.")
    else:
        print(f"  ❌ {reason}")


if __name__ == "__main__":
    main()
