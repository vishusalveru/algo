import os, sys, tempfile, json, glob
"""Tests for the full-chain OI totals / bias line (paper bot, 2026-10-22). No network, no orders.
    cd ~/algo-trading && python3 tests/test_oi_totals.py
Runs in a temp folder (never touches your logs); replays your own chain_v14_2026-10-*.csv files at the end."""
HERE = os.path.dirname(os.path.abspath(__file__))
REL = os.environ.get("RELEASE_DIR", os.path.dirname(HERE))
work = tempfile.mkdtemp(); os.chdir(work)
if not os.path.exists(os.path.join(REL, "config.py")):
    open("config.py", "w").write('LIVE_TOKEN="x"\nBOT_TOKEN=""\nCHAT_ID=""\n')
sys.path[:0] = [REL, work] + ([os.environ["EXTRA_MODS"]] if os.environ.get("EXTRA_MODS") else [])
import pandas as pd
import nifty_bot_v14 as P
TG = []; P.tg = lambda m: TG.append(m)
P.log.handlers[:] = []
ok = True
def check(c, name, detail=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
def chain(strikes, ce, pe, ce_prev=None, pe_prev=None):
    out = []
    for i, k in enumerate(strikes):
        cm = {"oi": ce[i]}; pm = {"oi": pe[i]}
        if ce_prev is not None: cm["prev_oi"] = ce_prev[i]
        if pe_prev is not None: pm["prev_oi"] = pe_prev[i]
        out.append({"strike_price": float(k), "call_options": {"market_data": cm}, "put_options": {"market_data": pm}})
    return out
def reset():
    P._oi_tot.clear(); P._oi_flip.clear(); P._oi_prev_snap.clear(); P._oi_big_since_tg.clear()
    P._oi_walls.clear(); P._oi_tg_prev.clear()
K = [22900, 22950, 23000, 23050, 23100]
LOT = 65; L = lambda lakh_lots: int(lakh_lots * 1e5 * LOT)
# 1. Upstox basis
reset()
c = chain(K, [L(5)]*5, [L(6)]*5, [L(4.8)]*5, [L(5.5)]*5)
r = P.compute_oi_totals(c, 1000.0)
check(r["oi_basis"] == P.BASIS_UPSTOX, "prev_oi present -> Upstox basis", r["oi_basis"])
check(r["ce_oi_total_lots"] == 25.0 and r["pe_oi_total_lots"] == 30.0, "totals in lakh lots", f"{r['ce_oi_total_lots']} / {r['pe_oi_total_lots']}")
check(r["ce_oi_chg_lots"] == 1.0 and r["pe_oi_chg_lots"] == 2.5, "change vs prev close", f"{r['ce_oi_chg_lots']} / {r['pe_oi_chg_lots']}")
check(r["oi_bias"] == "WEAK BULL", "fresh direction -> WEAK even at 2.7%", r["oi_bias"])
r = P.compute_oi_totals(c, 1000.0 + 700)
check(r["oi_bias"] == "WEAK BULL" and abs(r["oi_net_pct"] - 2.73) < 0.01, "after 11 min: 2.7% -> WEAK", f"{r['oi_bias']} {r['oi_net_pct']}")
c2 = chain(K, [L(5)]*5, [L(6.8)]*5, [L(4.8)]*5, [L(5.5)]*5)
r = P.compute_oi_totals(c2, 1000.0 + 800)
check(r["oi_bias"] == "STRONG BULL", ">=8% held -> STRONG", f"{r['oi_bias']} {r['oi_net_pct']}")
line = P.oi_tg_line()
print("   TG line:", line.replace("\n", " / "))
check("change since Upstox prev close" in line and "CE 25.00L (+1.00L)" in line and "PE 34.00L (+6.50L)" in line and "STRONG BULL" in line, "Telegram line shows basis, totals, change, label")
# [2026-11-05] clarity additions
check("🟢" in line and "support building" in line and "Put side up 5.50L lots more" in line, "label explained in words (bull)")
check("PCR 1.36 (more puts)" in line and "lakh lots" in line, "PCR explained + unit legend")
check("Last" not in line and "was" not in line, "first line: no 5-min change / previous label yet")
P._oi_tg_prev["ts"] -= 300                  # pretend the previous alert was 5 min ago
c3 = chain(K, [L(5.2)]*5, [L(6.8)]*5, [L(4.8)]*5, [L(5.5)]*5)          # calls +1.00L, puts unchanged
P.compute_oi_totals(c3, 1000.0 + 1100)
line2 = P.oi_tg_line(); print("   TG line 2:", line2.replace("\n", " / "))
check("Last 5 min: CE +1.00L | PE +0.00L → calls ahead → bearish tilt" in line2, "5-min change shown with tilt", line2)
check("(was STRONG BULL)" in line2 or "(same as 5 min ago)" in line2, "previous label shown")
cw = chain(K, [L(5), L(5), L(5), L(9), L(5)], [L(4), L(8), L(6), L(6), L(6)], [L(4.8)]*5, [L(5.5)]*5)
P.compute_oi_totals(cw, 1000.0 + 1200)
check("resistance 23050 (CE 9.00L)" in P.oi_tg_line() and P._oi_walls["pe"] == (22950, 8.0), "biggest call/put OI strikes", str(P._oi_walls))
cw2 = chain(K, [L(9), L(5), L(5), L(6), L(5)], [L(4), L(5), L(6), L(6), L(8)], [L(4.8)]*5, [L(5.5)]*5)
for r_ in cw2: r_["underlying_spot_price"] = 23010.0
P.compute_oi_totals(cw2, 1000.0 + 1300)
check(P._oi_walls == {"ce": (23050, 6.0), "pe": (23000, 6.0)}, "walls only on the correct side of spot (ITM 22900 CE / 23100 PE ignored)", str(P._oi_walls))
reset(); P.compute_oi_totals(chain(K, [L(5)]*5, [L(5)]*5, [L(6)]*5, [L(6.5)]*5), 1.0)
lb = P.oi_tg_line()
check("🔴" in lb and "resistance building" in lb and "Call side up" in lb, "label explained in words (bear)", lb.split("\n")[2])
check(not __import__("re").search(r"<(?!/?(b|i)>)", lb), "only <b>/<i> tags (Telegram HTML safe)")
# 2. bot-start basis + persistence across restart
reset()
c = chain(K, [L(5)]*5, [L(6)]*5)
r = P.compute_oi_totals(c, 5000.0)
check(r["oi_basis"] == P.BASIS_START and r["ce_oi_chg_lots"] == 0 and os.path.exists(P._oi_baseline_file()), "no prev_oi -> bot-start basis, baseline saved", r["oi_basis_since"])
reset()                                    # simulated restart: memory gone, file stays
c = chain(K, [L(5.2)]*5, [L(5.9)]*5)
r = P.compute_oi_totals(c, 9000.0)
check(r["ce_oi_chg_lots"] == 1.0 and r["pe_oi_chg_lots"] == -0.5 and r["oi_bias"].endswith("BEAR"), "restart keeps the morning baseline", f"{r['ce_oi_chg_lots']} {r['pe_oi_chg_lots']} {r['oi_bias']}")
check("bot start" in P.oi_tg_line(), "Telegram line says 'bot start HH:MM'")
# 3. partial prev_oi -> whole-cycle fallback + switch warning
TG.clear()
c = chain(K, [L(5)]*5, [L(6)]*5, [L(4.8)]*5, [L(5.5)]*5)
P.compute_oi_totals(c, 9100.0)            # -> Upstox (switch from BOT_START)
check(any("OI basis switched" in m for m in TG), "basis switch -> Telegram warning")
TG.clear()
c = chain(K, [L(5)]*5, [L(6)]*5, [L(4.8)]*4 + [None], [L(5.5)]*5)   # 9/10 = 90% < 95%
r = P.compute_oi_totals(c, 9200.0)
check(r["oi_basis"] == P.BASIS_START and abs(r["prev_oi_coverage"] - 0.9) < 1e-9, "90% coverage -> falls back for the WHOLE line", f"{r['prev_oi_coverage']}")
check(any("OI basis switched" in m for m in TG), "switch back also warns")
# 4. big jump
reset(); P._oi_big_since_tg.clear()
c = chain(K, [L(5)]*5, [L(6)]*5, [L(4.8)]*5, [L(5.5)]*5); P.compute_oi_totals(c, 1.0)
pe = [L(6)]*5; pe[2] += 12000 * LOT
r = P.compute_oi_totals(chain(K, [L(5)]*5, pe, [L(4.8)]*5, [L(5.5)]*5), 121.0)
check(r["oi_big_jump"].startswith("23000PE +12000"), "big single-strike jump logged", r["oi_big_jump"])
check("big move: 23000PE +12.0k lots" in P.oi_tg_line(), "big jump shown once in the next Telegram line")
check("big move" not in P.oi_tg_line(), "...and not repeated")
# 5. robustness
reset()
check(P.compute_oi_totals([], 1.0)["oi_bias"] == "", "empty chain -> blanks, no crash")
check(P.compute_oi_totals([{"strike_price": "x"}, {"strike_price": 23000}], 1.0)["oi_bias"] == "", "malformed rows -> blanks, no crash")
check(P.oi_tg_line().startswith("<b>OI</b> n/a"), "no data -> 'OI n/a'")
# 6. log rows carry the columns
check(all(c in P.CHAIN_COLS for c in P.OI_TOTAL_COLS) and "oi_bias" in P.SKIP_COLS and "oi_bias" in P.SIGNALS_COLS
      and "entry_oi_bias" in P.TRADE_COLS and '"entry_regime_label","entry_vix"' in open(os.path.join(REL, "nifty_bot_v14.py")).read(),
      "chain/skip/signals/trade columns added (verify_release strings intact)")
# 7. replay real chain logs (7 strikes, no prev_oi -> bot-start basis)
C = os.environ.get("CHAIN_DIR", REL)
OFF = {"m3": -150, "m2": -100, "m1": -50, "atm": 0, "p1": 50, "p2": 100, "p3": 150}
agree = tot = 0
for f in sorted(glob.glob(os.path.join(C, "chain_v14_2026-10-*.csv"))):
    reset(); [os.remove(x) for x in glob.glob("oi_baseline_*.json")]
    d = pd.read_csv(f)
    labels = []
    for i, row in d.iterrows():
        ks = [row.atm + o for o in OFF.values()]
        ce = [row[f"s_{k}_ce"] for k in OFF]; pe = [row[f"s_{k}_pe"] for k in OFF]
        ch = [{"strike_price": float(k), "call_options": {"market_data": {"oi": a}}, "put_options": {"market_data": {"oi": b}}}
              for k, a, b in zip(ks, ce, pe) if pd.notna(a) and pd.notna(b)]
        r = P.compute_oi_totals(ch, float(i * 120))
        labels.append(r["oi_bias"])
    end = labels[-1]; move = d.nifty_ltp.iloc[-1] - d.nifty_ltp.iloc[0]
    tot += 1; agree += (("BULL" in end) == (move > 0))
    print(f"   {os.path.basename(f)[10:20]}: end label {end:14s} | Nifty {move:+.0f} | labels seen {sorted(set(labels))}")
check(tot >= 1, f"replayed {tot} October chain sessions without error")
print("\nALL OI TESTS PASSED" if ok else "\nSOME OI TESTS FAILED"); sys.exit(0 if ok else 1)
