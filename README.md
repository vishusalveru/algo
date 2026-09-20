> PART I (project inception → 2026-06-25) lives in the claude.ai project docs, not in this repo. This file begins at Part II.


---

# PART II — 2026-06-25 → 2026-09-20 (paper bot)

> Continuation of the log above. Same rules apply: these are paid-for lessons,
> don't silently revert them. Everything here is PAPER-side unless marked.
> Live-side findings for the same period are in LIVE_BOT_README.md Part II.

## The deployment gap (2026-06-25 → 2026-09-17) — READ THIS FIRST

**For roughly three months, code changes discussed in chat were never running.**
Two independent causes, both now fixed:

1. **Directory split.** Both bots run from `~/algo-trading/`, but the git repo
   was cloned into `~/algo-trading/algo/`. Every `git pull` updated files one
   directory BELOW where the bots actually read from. The pulls succeeded, the
   logs looked healthy, and nothing deployed.
2. **Branch divergence.** Local HEAD sat on `master` (7b29f16) while the remote
   was `origin/main` (1cc17c4). A pull could never reach the working tree even
   if the directory had been right.

**How it was caught:** a grep for a feature that had been "deployed" weeks
earlier returned 0. `grep -c net_gex nifty_bot_v14.py` → 0. The chat transcript
showed the same greps returning 0 on an earlier morning too, and a `git commit`
run after that had captured the OLD on-disk files — so the commit history looked
healthy while carrying stale code.

**Fixed 2026-09-20:** `algo/` subfolder removed; repo root IS `~/algo-trading/`;
branch renamed `master` → `main` and force-pushed; remote switched to SSH
(`git@github.com:vishusalveru/algo.git`) because PATs expire and would break the
pull-based deploy silently — the exact failure mode that cost three months.
`config.py` confirmed absent from `git ls-files` (token never left the VM).

**STANDING RULE — verify deploys, never assume them.** After every deploy:
```bash
grep -c net_gex nifty_bot_v14.py            # expect 8
grep -c early_kill_mfe nifty_bot_v14.py     # expect 1
grep -c "now_ist().strftime" nifty_bot_v14.py  # expect 1
python3 verify_v14.py                        # expect ALL CHECKS PASSED
python3 -c "import config; print(len(config.LIVE_TOKEN))"  # expect 313
```
And at runtime: the Telegram startup banner must show the **FLAGS + CAL** lines.
A missing banner means the old file is the one running.

---

## MFE early-kill (PAPER ONLY, built this period)

The MFE-timing instrument added 2026-06-23 finally produced enough trajectory
data to act on the −12131 "never-developed" leak recorded in Part I.

`trade_engine_v14.py`:
```python
EARLY_KILL_MARK_MIN = 5.0    # checkpoint minute
EARLY_KILL_MFE_MIN  = 3.0    # MFE-so-far below this at the mark = cut
```
Cuts only when ALL of: 5 minutes elapsed, MFE-so-far < 3.0, and the position is
NOT in profit. The not-in-profit condition is what keeps it from cutting a slow
grinder that is already green.

`early_kill_mfe=True` is passed **only** by `nifty_bot_v14.py`. Default is
`False`, so the live bot is unaffected. This is deliberate and is NOT ready to
graduate — see the live README for why (≈3 paper samples so far, and July paper
data showed 4 of 5 false-kills were StrongFVG, the slow developer).

## Smart regime gate (PAPER ONLY)

The old regime block was unconditional: trend strategies blocked outright in
WEAK_BULL / CHOPPY / RANGING / UNKNOWN. Now it blocks only when the regime is in
that set **AND** `efficiency_ratio < MIN_EFFICIENCY` — i.e. the label says chop
and the tape agrees. A mislabelled regime on a genuinely clean tape no longer
suppresses the trade. Flag: `smart_regime_gate`, paper passes `True`.

## StrongFVG afternoon cutoff (PAPER ONLY)

`day_context_v14.py`:
```python
FVG_AFTERNOON_STOP = datetime.time(14, 0)
```
Late-session StrongFVG went **1 win / 12 trades, −Rs.11k across 6 days**. The
mechanism is exhaustion plus accelerating theta — an afternoon gap is usually the
end of a move, not the start. Flag: `fvg_afternoon_cutoff`, paper passes `True`.
BOS is untouched by this cutoff.

## Block auditing — tooling + the weekly ritual

`audit_blocks.py` — counterfactual audit of EVERY block type. For each blocked
signal it measures the index-point move over the following 20 minutes
(favourable vs adverse) and nets it out, so a gate can be judged as saver or leak
rather than argued about. `audit_regime_block.py` drills into the regime gate
specifically. Run weekly.

**What the audit settled (and closed):**

| Gate | Verdict |
|---|---|
| ATR-drift filter | **KEEP AS-IS.** 183 blocks, net −3 index points. It is a net saver. |
| Making ATR-drift efficiency-aware | **REJECTED.** The eff 0.30–0.40 band nets −23 points. Adding efficiency awareness would have HURT. |
| Cooldown-after-win | KEEP (Part I finding holds) |
| Gap-opposition (blunt) | superseded in paper by `smart_gap_filter`; still blunt in live by design |

## Hypotheses the data REJECTED this period — do not re-litigate

Recorded so these are not re-proposed in a future session:

- **Delta does not explain timeouts.** Timeout delta 0.482 vs non-timeout 0.483,
  **p = 0.978**. There is no delta signal in the timeout bucket at all.
- **"Avoid delta < 0.35" (from the Options Greeks Decoder doc) does not apply
  here.** Our sub-0.35-delta trades went **5/5, +Rs.1,456**. The rule is written
  for a different holding period and cost structure.
- **"Avoid buying at IV Rank ≥ 70" does not replicate.** High-IV trades made
  MORE, not less: **+325 vs +26 average, p = 0.138.** Not significant either way,
  but certainly not the stated direction.

General lesson, now twice-proven: **a rule from an options textbook is a
hypothesis about our data, not a finding.** Test it on our trades before adopting
it. Three of three external rules failed to replicate this period.

## rvol — a dead end, and a mistake worth recording

`rvol` logged 0/N NaN across **every** session. Root cause: **`NSE_INDEX|Nifty 50`
has no traded volume.** The index is a computed number; there is nothing to
measure volume on. `calc_rvol` was never going to work on it.

**My error, recorded honestly:** I built a "fix" that fetched Nifty FUTURES
volume as a proxy via `get_nifty_future_key()` / `get_future_candles()`. Those
functions query `/v2/option/contract`, which returns **option** contracts only —
no FUT row ever matches, so the function returns `None` silently with no log
line. It was dead code from the moment it was written, and it failed in exactly
the invisible way this project has been burned by before.

**Superseded by:** per-strike option volume from the chain (below). The dead
futures path is still in the file and is **pending removal**.

## Chain instrumentation — OI, volume, gamma/GEX

`snapshot_chain_oi(chain, atm, ltp, now_ts)` in `nifty_bot_v14.py` captures
ATM±3 every cycle, rebuilds support/resistance every 15 minutes, and labels an
`oi_state`. Written to `chain_v14_<date>.csv`.

Columns added this period:
- **Per-strike volume** `v_*_ce` / `v_*_pe`, plus `atm_vol_total` and
  `atm_vol_z` (rolling z-score) — the real replacement for the dead index rvol.
- **Per-strike gamma** `g_*_ce` / `g_*_pe`, plus `net_gex` and `gex_regime`
  (labelled against the session's own median, not an absolute threshold).

**OPEN QUESTION — resolve on the first full session:** is the chain `volume`
field per-interval or day-cumulative? If `v_atm_ce` reads in the tens of millions
and only ever rises, it is cumulative and `atm_vol_z` must z-score the
**inter-snapshot diff**, not the raw value. Reference point from 09-11 postval
(23350 CE): cumulative **99,613,605** at 11:17 vs that-minute **4,430,010**.
`atm_vol_z` means nothing until this is settled.

**First suggestive chain reading (n=1, 09-18):** `pe_below_open` grew 247K →
14.86M while `ce_above_open` collapsed 2.71M → 743K, and the index closed +62
near the high. That is the first session where chain OI arguably LED price rather
than confirmed it. One session. Not a finding — a thing to watch for.

## `fetch_postval_candles` — end-of-session validation data

Pulls 1-minute candles for every instrument actually traded that session, written
to `postval_*` files. This is what lets a trade be re-examined at 1-minute
resolution after the fact without any tick-capture infrastructure.

## Three real bugs found in the full code audit (2026-09) — all mine

1. **Chain log wrote UTC while every other log writes IST.** A 5:30 offset made
   the chain data un-joinable to the scan log — the single thing it existed for.
   Fixed to `now_ist().strftime(...)`.
2. **`fetch_postval_candles` was nested inside `if tg_on():`.** With Telegram
   off, the 1-minute candles would never be fetched — and they are unrecoverable
   once the session ends. Moved outside the Telegram branch.
3. **State never reset between days.** `state['date']` was written but never
   compared. SESSION DONE reported all-time P&L as if it were the day's, and
   `traded_keys` grew unbounded. `load_state()` now resets `wins` / `losses` /
   `pnl` / `strat_pnl` / `traded_keys` on a new date while preserving the
   cumulative `trades` counter.

Pattern across all three: **each failed silently.** None raised, none logged.
That is the project's recurring failure mode — see also the rvol dead code above
and the three-month deploy gap. Prefer a loud failure to a clean-looking one.

## Event calendar rebuilt

Impact-date semantics were wrong. Corrected:
- **RBI** → impacts the **same** India session.
- **FOMC / US CPI** → announce after the India close, so they key to the **NEXT**
  India session.

Populated through 2026-12-15. `calendar_health()` added and surfaced in the
Telegram startup banner (the **CAL** line), so a stale calendar is visible at
startup instead of discovered mid-session.

## Telegram

Startup: **FLAGS + CAL** banner (this is the deploy-verification signal).
Entry: confidence, efficiency, ATR, regime, VIX, OI state.
Exit: MFE / MAE + running day P&L.
Session end: per-strategy split.

## Session notes

- **09-18 — dead tape, correctly flat.** ATR mean 13.2 (below the 18.0 trend
  floor), VIX **11.67 — the lowest on record in this dataset**, efficiency 0.132,
  73-point range. 26 signals, all blocked on dead tape. Zero trades in BOTH
  books. This is the gates working, not a miss.

## `MAX_LOTS = 2` — paper is NOT a true shadow

Paper runs `MAX_LOTS = 2`; live is hardcoded to 1 lot. **Paper P&L is inflated on
any full-size trade, and paper-vs-live P&L is not directly comparable without
normalising to 1 lot.** Every live-vs-paper figure quoted in these READMEs has
been 1-lot normalised where it says so. Set `MAX_LOTS = 1` if paper should be a
true shadow. Deferred, not forgotten.

## Pending (paper side)

- [ ] Settle the chain `volume` cumulative-vs-per-interval question (blocks
      `atm_vol_z` being meaningful)
- [ ] Remove the dead futures/rvol path
- [ ] Duplicate-signal debounce — justified by the 07-17 EMACross chase
      (−Rs.5,818: target hit in 33s, the same signal re-fired 32s later at a 16%
      higher premium). This is the Part I "no duplicate-signal debounce"
      limitation finally producing a costed example.
- [ ] BOS efficiency-floor at ATR ≥ 26 — hypothesis held at n=57, not yet built
- [ ] OI-fitness join script — needs accumulated `chain_v14` sessions first
- [ ] Strategy-aware early-kill + delta-normalised threshold (`mfe_pts/delta < 6`)
- [ ] `live_feeds_v14.get_india_vix()` still calls the deprecated
      `v2/market-quote/ltp`. Migrate when v2 is actually disabled (owner decision)
