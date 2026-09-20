> PART I (project inception → 2026-06-25) lives in the claude.ai project docs, not in this repo. This file begins at Part II.


---

# PART II — 2026-06-25 → 2026-09-20 (live bot)

> Continuation of the log above. Paper-side findings for the same period are in
> README.md Part II. **The live bot's trading logic was NOT changed this period.**
> Everything below is measurement and analysis; the only code that touched the
> live path was logging and shared bug fixes.

## Live aggregate — 41 trades, 19 sessions

**+Rs.8,648 total, 68% win rate, 1 lot throughout.**

| Strategy | Trades | WR | Net P&L | Trades with MFE < 1 |
|---|---|---|---|---|
| **BOS** | 19 | **79%** | **+10,062** | **0** |
| StrongFVG | 22 | 59% | **−1,414** | 6 |

**BOS is the system's edge and it is not close.** It carries the entire live
record and has never once taken a trade that failed to develop. This is now
confirmed across both books and both directions.

## THE LEAK — 6 trades, all the same shape, −Rs.5,789

All 6 live trades with **MFE < 1** were **StrongFVG**, and **all 6 lost**. They
total **−Rs.5,789**.

**Excluding those 6, live StrongFVG is 16 trades, 81% WR, +Rs.4,375.**

So StrongFVG is not a broken strategy — it is a good strategy with one specific
failure mode that eats its entire profit and then some. The failure mode is the
one identified back in Part I (2026-06-11): **wrong from entry, never goes our
way, rides to SL or timeout.**

**The three most recent live losses are all this exact profile:**

| Date | P&L | Note |
|---|---|---|
| 07-17 | −1,680 | |
| 09-11 | −1,411 | |
| 09-17 | −962 | **MFE 0.0**, ran **25.3 minutes** to timeout |

The 09-17 trade is the clearest case in the dataset: the premium never moved in
our favour by a single point, and the bot held it for twenty-five minutes.

## Why early-kill is still PAPER-ONLY (deliberate, not an oversight)

`early_kill_mfe` cuts exactly this profile at the 5-minute mark. It is running in
paper and it is NOT in live. **Do not graduate it yet.**

Two reasons, both data:
1. **Only ~3 paper early-kill samples exist.** That is not a sample, it is an
   anecdote.
2. **July paper data showed 4 of 5 false-kills were StrongFVG** — the slow
   developer. StrongFVG is simultaneously the strategy the rule is aimed at AND
   the strategy most likely to be cut unfairly by it. That tension has to be
   resolved with data before real money is exposed to it.

**Graduation condition (write it down now, decide it calm):** early-kill goes
live only when paper has enough early-kill fires to show that the trades it cuts
would have lost, AND that the winners it cuts are few enough and small enough to
be worth it. Candidate refinement to test first: make the threshold
**strategy-aware** and **delta-normalised** (`mfe_pts / delta < 6`), so a
low-delta option is not judged by the same absolute point threshold as a
high-delta one.

Until then, **live has no protection against the exact trade that is costing it
money.** That is a known, accepted, temporary exposure — accepted because the
alternative (shipping an unvalidated cut rule onto the one strategy it is most
likely to misfire on) is worse.

## Strong-breakout override — candidate for BOS-only restriction

Across both books: **35 trades, net −Rs.62** (1-lot normalised). Live alone:
**−Rs.1,304.** The **4 worst are all StrongFVG with MFE ≈ 0** — the same profile
as the leak above.

The override is roughly break-even overall and clearly negative on the FVG side.
**Candidate change: restrict the override to BOS only.** Not built. Needs the
same treatment as everything else — count the trades it would have changed, both
directions, before touching the live path.

## Duplicate-signal debounce — now has a costed example

**07-17, EMACross: −Rs.5,818.** Target was hit in 33 seconds. The same signal
re-fired 32 seconds later and the bot re-entered at a **16% higher premium**,
chasing its own move. The Part I limitation "no duplicate-signal debounce — same
setup can re-fire minutes apart" now has a price tag attached.

## API / SDK status (checked 2026-09)

- Order place + cancel: **v3 on the HFT host** — correct, unchanged.
- Candles + LTP: **v3** — correct.
- Option chain + contract: **v2** — still functioning.
- **`live_feeds_v14.get_india_vix()` still calls the deprecated
  `v2/market-quote/ltp`.** Owner decision (2026-09): migrate when v2 is actually
  disabled, not before. Recorded so it is not a surprise when it breaks.

## Graduation criteria — status against §10

§10 left the numbers blank. Filling them in against the actual record:

| Criterion | Target | Actual | Status |
|---|---|---|---|
| Minimum live trades | ≥ 20, mixed day types | 41 across 19 sessions | **met** |
| Win rate after real costs | ≥ 58–60% | 68% overall; BOS 79% | **met** |
| Measured slippage acceptable | live not far below paper | entry slip clean (Part I avg +0.17) | **met** |
| Loss quality — no single catastrophic loss | — | worst −1,680; no outlier carrying the record | **met** |
| **Not one fluke trade carrying the record** | — | BOS +10,062 across 19 trades, 79% WR | **met for BOS** |

**Honest read: BOS clears the §10 bar. StrongFVG does not** — it is net negative
live and its profit is entirely hostage to one unmitigated failure mode.

**This does NOT mean scale BOS now.** The §10 bar was written for the
FVG-to-2-lots question. Before any size increase, the paper-vs-live `MAX_LOTS`
mismatch (paper 2, live 1 — see README.md Part II) has to be resolved so the two
books are actually comparable, and the desk-review condition from Part I (50+
live trades + known slippage/latency + upgraded confidence model) still stands at
41 trades. **Stay at 1 lot.**

The live-side decision that IS supported by this data, and should be considered
before any sizing question: **fix or gate the StrongFVG MFE<1 failure mode.**
That is worth +Rs.5,789 on the existing record without adding a single lot.

## Pre-flight checklist — addition

Add to §9, before each session:
```
[ ] Telegram startup banner shows the FLAGS + CAL lines
    (this is the proof the DEPLOYED file is the one running — see
     README.md Part II, "The deployment gap")
```
