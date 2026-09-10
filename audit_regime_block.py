#!/usr/bin/env python3
"""
audit_regime_block.py — Does the regime=CHOPPY gate on BOS help or leak?

CONTEXT (2026-06-25)
  day_context_v14 HARD-BLOCKS any trend-type strategy (BOS, EMACross, ...) when
  signals.classify_intraday_regime() returns a regime in REGIME_BLOCK_TREND
  (CHOPPY, WEAK_BULL, WEAK_BEAR ...). On 06-25, 18 BOS fired 12:01-12:10 and ALL
  were blocked as regime=CHOPPY even though efficiency was a healthy 0.39-0.43.

  This is the SAME failure shape as the old blunt gap filter: a coarse
  categorical gate overriding a continuous signal (efficiency) that disagrees.

WHAT THIS AUDITS
  Across every skip_v14_*.csv, find BOS signals blocked by the regime gate,
  then split them by whether efficiency AGREED (chop, eff<0.30) or DISAGREED
  (eff>=0.30, i.e. the tape was actually efficient and the label was wrong).
  The DISAGREE bucket is the suspected leak — efficient BOS the gate killed.

  We CANNOT know the counterfactual P&L of a blocked trade without tick replay.
  So this is a DIAGNOSTIC, not a P&L claim: it quantifies how often the gate
  fires against efficient tape, clusters the blocks (re-fires of one setup vs
  distinct signals), and contrasts with how BOS actually performs when it DOES
  trade (from the trade logs) at comparable efficiency.

  OUTPUT drives the decision: if the gate mostly blocks genuine chop -> keep it.
  If it routinely blocks efficient tape that BOS wins on -> soften it to an
  efficiency-aware gate (block only when regime=CHOPPY AND eff<0.30), exactly
  as the gap filter was softened. Decision stays data-gated; this just measures.
"""
import glob, re, sys
import numpy as np
import pandas as pd

CHOP_EFF = 0.30   # signals MIN_EFFICIENCY — the disagreement boundary

def _regime(r):
    m = re.search(r'regime=(\w+)', str(r)); return m.group(1) if m else '?'
def _eff(r):
    m = re.search(r'efficiency ([\d.]+)', str(r))
    return float(m.group(1)) if m else np.nan

def load_skips(path='.'):
    fs = sorted(glob.glob(f'{path}/skip_v14_2026-*.csv'))
    frames = []
    for f in fs:
        try:
            d = pd.read_csv(f); d['date'] = f.split('_')[-1].replace('.csv','')
            frames.append(d)
        except Exception as e:
            print(f'  skip {f}: {e}')
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def load_trades(path='.'):
    fs = sorted(glob.glob(f'{path}/trade_v14_2026-*.csv'))
    frames = []
    for f in fs:
        try:
            d = pd.read_csv(f); d['date'] = f.split('_')[-1].replace('.csv','')
            frames.append(d)
        except Exception:
            pass
    t = pd.concat(frames, ignore_index=True)
    t = t[pd.to_numeric(t['pnl'], errors='coerce').notna()].copy()
    t['pnl'] = pd.to_numeric(t['pnl'], errors='coerce')
    t['win'] = (t['pnl'] > 0).astype(int)
    return t

def main(path='.'):
    sk = load_skips(path)
    if sk.empty:
        print('no skip files found'); return 1
    sk['is_regime_block'] = sk['reasons'].astype(str).str.contains('trend-type but regime')
    bos = sk[(sk['signal'] == 'BOS') & sk['is_regime_block']].copy()
    bos['blocked_regime'] = bos['reasons'].apply(_regime)
    bos['eff'] = bos['reasons'].apply(_eff)
    bos['dt'] = pd.to_datetime(bos['datetime'], errors='coerce')

    print('=' * 72)
    print('REGIME-BLOCK AUDIT — BOS signals killed by the regime gate')
    print('=' * 72)
    print(f'Sessions audited      : {sk["date"].nunique()}')
    print(f'BOS regime-blocks      : {len(bos)}')
    print(f'Blocking regimes       : {dict(bos["blocked_regime"].value_counts())}')

    # AGREE vs DISAGREE
    agree   = bos[bos['eff'] <  CHOP_EFF]   # gate + efficiency both say chop
    dis     = bos[bos['eff'] >= CHOP_EFF]   # efficient tape mislabeled chop
    print('\n--- Did efficiency AGREE the tape was choppy? ---')
    print(f'AGREE (eff<{CHOP_EFF}, gate justified)   : {len(agree):3d}  '
          f'(mean eff {agree["eff"].mean():.2f})')
    print(f'DISAGREE (eff>={CHOP_EFF}, SUSPECTED LEAK): {len(dis):3d}  '
          f'(mean eff {dis["eff"].mean():.2f})')
    if len(bos):
        print(f'  => gate fires against EFFICIENT tape {len(dis)/len(bos):.0%} of the time')

    # Cluster the disagree blocks: distinct setups vs re-fires (gap in minutes)
    print('\n--- DISAGREE blocks clustered (>=3min gap = new setup) ---')
    dis = dis.sort_values('dt')
    clusters, last, n = [], None, 0
    for _, r in dis.iterrows():
        if last is None or (r['dt'] - last).total_seconds() > 180:
            n += 1
        clusters.append(n); last = r['dt']
    dis = dis.assign(cluster=clusters)
    distinct = dis['cluster'].nunique() if len(dis) else 0
    print(f'{len(dis)} efficient-tape blocks = ~{distinct} DISTINCT setups '
          f'(rest are 30s re-fires of the same signal)')
    if len(dis):
        by = dis.groupby('date').agg(blocks=('eff','size'),
                                     setups=('cluster','nunique'),
                                     mean_eff=('eff','mean')).round(2)
        print(by.to_string())

    # Contrast: how does BOS perform when it DOES trade at eff>=0.30?
    print('\n--- CONTRAST: BOS that actually TRADED (from trade logs) ---')
    t = load_trades(path)
    tb = t[t['strategy'] == 'BOS']
    print(f'BOS traded: n={len(tb)}  win-rate={tb["win"].mean():.0%}  '
          f'total=Rs.{tb["pnl"].sum():+.0f}  avg=Rs.{tb["pnl"].mean():+.0f}')
    print('  (BOS is the system\'s edge; the question is whether the gate is')
    print('   throwing away efficient-tape BOS that would have shared this edge.)')

    # Verdict scaffold (decision stays with the owner + more data)
    print('\n' + '=' * 72)
    print('READ')
    print('=' * 72)
    leak_rate = len(dis)/len(bos) if len(bos) else 0
    if leak_rate >= 0.25:
        print(f'⚠  The gate blocks efficient tape {leak_rate:.0%} of the time '
              f'({distinct} distinct efficient setups killed).')
        print('   This mirrors the blunt-gap-filter leak. CANDIDATE FIX: make the')
        print('   regime gate efficiency-aware — block trend strats only when')
        print('   regime in BLOCK-set AND efficiency_ratio < MIN_EFFICIENCY.')
        print('   Validate paper-only behind a flag (like smart_gap_filter) before live.')
    else:
        print(f'✓  The gate rarely fires against efficient tape ({leak_rate:.0%}). '
              f'It is mostly catching genuine chop — leave it as is.')
    print('\nNOTE: counterfactual P&L of blocked trades needs tick replay to')
    print('confirm. This audit measures the LEAK RATE, not realised P&L.')
    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else '.'))
