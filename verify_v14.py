#!/usr/bin/env python3
"""
verify_v14.py — No-network self-test of the v14 logic chain.
Run after any edit: confirms detection, gates, ladder, lockout, event calendar.
"""
import datetime, sys

def main():
    ok = True
    def check(name, cond):
        nonlocal ok
        print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
        ok = ok and cond

    print("verify_v14 — logic self-test")
    print("="*60)

    # 1. imports
    import signals, day_context_v14, decision_layer_v14, trade_engine_v14
    import event_calendar_v14
    print("\n[imports]")
    check("all modules import", True)

    # 2. signals classification
    print("\n[signals.py classification]")
    check("efficiency_ratio trend≈1", signals.efficiency_ratio([1,2,3,4,5]) > 0.9)
    check("efficiency_ratio chop low", signals.efficiency_ratio([1,3,1,3,1]) < 0.3)
    check("vix_bias extreme", signals.vix_bias(27) == "extreme")
    check("is_trend_strategy BOS", signals.is_trend_strategy("BOS"))
    check("is_trend_strategy RSIDiv false", not signals.is_trend_strategy("RSIDivergence"))

    # 3. event calendar
    print("\n[event calendar]")
    ev, _ = event_calendar_v14.is_event_day(datetime.date(2026,6,5))
    check("RBI day flagged", ev)
    nev, _ = event_calendar_v14.is_event_day(datetime.date(2026,5,26))
    check("normal day not flagged", not nev)

    # 4. decision chain
    print("\n[decision chain]")
    from decision_layer_v14 import decide_entry, CapitalLadder, ReentryLockout
    from trade_engine_v14 import OptionQuote
    q = OptionQuote(strike=24000, opt_type="PE", bid=188.85, ask=189.30,
                    bid_qty=100, ask_qty=100, oi=100000, delta=-0.502, iv=13.92)
    closes = [24000 - i*0.5 for i in range(30)]  # mild downtrend

    cap, lock = CapitalLadder(), ReentryLockout()
    d = decide_entry(quote=q, direction="bearish", strategy_name="StrongFVG",
                     confidence=7, atr_5m=22, recent_closes=closes,
                     now_time=datetime.time(11,0), regime="TRENDING_BEAR",
                     strong_breakout=True, today=datetime.date(2026,5,26),
                     capital=cap, lockout=lock)
    check("valid setup enters", d.enter)

    lock.record("StrongFVG","sl")
    d2 = decide_entry(quote=q, direction="bearish", strategy_name="StrongFVG",
                      confidence=7, atr_5m=22, recent_closes=closes,
                      now_time=datetime.time(11,1), regime="TRENDING_BEAR",
                      strong_breakout=True, today=datetime.date(2026,5,26),
                      capital=cap, lockout=lock)
    check("re-entry lockout blocks after loss", not d2.enter)

    d3 = decide_entry(quote=q, direction="bearish", strategy_name="BOS",
                      confidence=8, atr_5m=40, recent_closes=closes,
                      now_time=datetime.time(11,0), regime="WEAK_BULL",
                      strong_breakout=True, today=datetime.date(2026,5,26),
                      capital=CapitalLadder(), lockout=ReentryLockout())
    check("regime gate blocks trend strat in WEAK_BULL", not d3.enter)

    d4 = decide_entry(quote=q, direction="bearish", strategy_name="StrongFVG",
                      confidence=8, atr_5m=22, recent_closes=closes,
                      now_time=datetime.time(13,30), regime="TRENDING_BEAR",
                      strong_breakout=True, today=datetime.date(2026,5,26),
                      nearest_expiry="2026-05-26",
                      capital=CapitalLadder(), lockout=ReentryLockout())
    check("expiry-day cliff blocks afternoon", not d4.enter)

    # 5. capital ladder
    print("\n[capital ladder]")
    c = CapitalLadder()
    c.on_result("sl"); c.on_result("sl")
    check("2 losses -> reduced size", c.multiplier() == 0.5)
    c.on_result("target"); c.on_result("target")
    check("2 wins -> restored size", c.multiplier() == 1.0)

    print("\n" + "="*60)
    print("ALL CHECKS PASSED ✓" if ok else "SOME CHECKS FAILED ✗")
    print("="*60)
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
