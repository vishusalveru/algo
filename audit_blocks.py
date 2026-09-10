#!/usr/bin/env python3
"""
audit_blocks.py — Weekly counterfactual audit of EVERY block type.

WHY
  Every filter in the decision chain either SAVES money (blocks trades that
  would have lost) or BLEEDS it (starves real winners). You can't tell which
  from the block reason alone — you have to ask what the market actually did
  AFTER each block. This script does that across all sessions, per block type.

METHOD (counterfactual proxy)
  For each blocked signal, look at the INDEX move in the next 20 minutes:
    favorable = points the index travelled in the signal's direction
    adverse   = points it travelled against (would have pressured the stop)
    net       = favorable - adverse
  net < 0  -> block SAVED us (trade would have gone against, on average)
  net > 0  -> block may be BLEEDING (trade would have moved our way)

  Also reports, with a ~20pt-equivalent stop model:
    clean_win = fraction reaching >=25 favorable AND never hitting -20 adverse
    stopped   = fraction hitting -20 adverse (likely stopped out first)
  A high net with high `stopped` is NOT a clean bleeder — it's a high-variance
  coin-flip the filter is buying lower variance on.

HONEST LIMITS (read before acting on any number)
  1. INDEX POINTS, not option premium. Real P&L includes theta + spread + delta.
     Slow (low-ATR) moves bleed MORE premium, so "bleeder" net is OPTIMISTIC —
     the true edge of slow-move buckets is worse than shown here.
  2. COUNTERFACTUAL, not realised. Only TICK REPLAY prices the actual option on
     each blocked entry. Treat this as a screen that says WHERE to look, not a
     P&L statement.
  3. SMALL BINS. Anything with n<15 is a hint, not a finding. Flagged with '*'.

USAGE
  python3 audit_blocks.py [dir]      # dir defaults to current
  Run weekly. Watch for any bucket FLIPPING sign (saver->bleeder or back) as
  sessions accumulate — that flip is the signal to investigate, not the
  absolute number on any single week.
"""
import glob, re, sys
import numpy as np
import pandas as pd

FAV_WIN = 25     # index pts in-direction = a clean win proxy
FAV_SOFT = 15    # softer "moved our way" proxy
ADV_STOP = 20    # index pts against = likely stop-out proxy
HORIZON_MIN = 20 # look-forward window
THIN = 15        # bins below this are flagged unreliable


def categorize(reasons: str):
    """Map a block's reason chain to a stable category. First BLOCK wins."""
    blocks = re.findall(r'BLOCK: ([^;]+)', str(reasons))
    if not blocks:
        return None
    b = blocks[0].strip()
    if 'drift' in b:                         return 'ATR-drift'
    if 'CHOPPY' in b:                        return 'regime=CHOPPY'
    if 'regime=' in b:                       return 'regime-other'
    if 'severe chop' in b:                   return 'severe-chop'
    if 'lockout' in b or 're-entry' in b:    return 'reentry-lockout'
    if 'too' in b and 'size' in b:           return 'size-too-small'
    if 'expiry' in b:                        return 'expiry-cliff'
    if 'VIX' in b:                           return 'vix-extreme'
    if 'pre-' in b:                          return 'pre-open'
    if 'needs ATR>' in b or ('ATR' in b and '<' in b):  return 'ATR-floor'
    if 'event' in b:                         return 'event-day'
    if 'gap' in b:                           return 'gap-oppose'
    return 'other'


def build(path='.'):
    rows = []
    skip_files = sorted(glob.glob(f'{path}/skip_v14_2026-*.csv'))
    if not skip_files:
        return pd.DataFrame()
    for skf in skip_files:
        date = skf.split('_')[-1].replace('.csv', '')
        scf = f'{path}/scan_v14_{date}.csv'
        try:
            sk = pd.read_csv(skf)
            sc = pd.read_csv(scf)
        except Exception:
            continue
        sk['dt'] = pd.to_datetime(sk['datetime'], errors='coerce')
        sc['dt'] = pd.to_datetime(sc['datetime'], errors='coerce')
        sc = sc.dropna(subset=['dt']).sort_values('dt').reset_index(drop=True)
        for _, b in sk.iterrows():
            c = categorize(b['reasons'])
            if c is None:
                continue
            d = str(b.get('direction', ''))
            t0 = b['dt']
            if pd.isna(t0):
                continue
            before = sc[sc['dt'] <= t0]
            if before.empty:
                continue
            p0 = before.iloc[-1]['nifty_ltp']
            win = sc[(sc['dt'] > t0) & (sc['dt'] <= t0 + pd.Timedelta(minutes=HORIZON_MIN))]
            if win.empty:
                continue
            lo, hi = win['nifty_ltp'].min(), win['nifty_ltp'].max()
            if d == 'bearish':
                fav, adv = p0 - lo, hi - p0
            elif d == 'bullish':
                fav, adv = hi - p0, p0 - lo
            else:
                continue
            rows.append({'cat': c, 'date': date, 'signal': b['signal'],
                         'direction': d, 'fav': fav, 'adv': adv, 'net': fav - adv})
    return pd.DataFrame(rows)


def report(df):
    if df.empty:
        print('No blocks found (need skip_v14_*.csv + matching scan_v14_*.csv).')
        return
    n_sessions = df['date'].nunique()
    print('=' * 78)
    print(f'BLOCK AUDIT — {len(df)} blocks across {n_sessions} sessions '
          f'(index-point counterfactual, {HORIZON_MIN}min horizon)')
    print('=' * 78)
    print('net<0 = block SAVED us | net>0 = block may BLEED | * = thin bin (n<%d)\n' % THIN)

    def agg(s):
        return pd.Series({
            'n': len(s),
            'fav': round(s['fav'].mean(), 1),
            'adv': round(s['adv'].mean(), 1),
            'net': round(s['net'].mean(), 1),
            'clean_win%': round(((s['fav'] >= FAV_WIN) & (s['adv'] < ADV_STOP)).mean() * 100),
            'stopped%': round((s['adv'] >= ADV_STOP).mean() * 100),
        })
    g = df.groupby('cat').apply(agg, include_groups=False).sort_values('net')
    # annotate thin + verdict
    out = []
    for cat, r in g.iterrows():
        thin = '*' if r['n'] < THIN else ' '
        if r['net'] <= -3:      verdict = 'SAVER'
        elif r['net'] >= 10:    verdict = 'BLEEDER?'
        elif r['net'] >= 3:     verdict = 'borderline-bleed'
        else:                   verdict = 'neutral'
        # high stopped% downgrades a bleeder to coin-flip
        if verdict.startswith('BLEED') and r['stopped%'] >= 35:
            verdict = 'coin-flip(hi-var)'
        out.append((f'{cat}{thin}', int(r['n']), r['fav'], r['adv'], r['net'],
                    int(r['clean_win%']), int(r['stopped%']), verdict))
    hdr = f"{'category':18}{'n':>5}{'fav':>6}{'adv':>6}{'net':>7}{'cleanW%':>9}{'stop%':>7}  verdict"
    print(hdr); print('-' * len(hdr))
    for cat, n, fav, adv, net, cw, st, v in out:
        print(f'{cat:18}{n:>5}{fav:>6}{adv:>6}{net:>+7}{cw:>9}{st:>7}  {v}')

    # Which strategies each BLEEDER affects (matters: only BOS/StrongFVG are live)
    print('\n' + '-' * 78)
    print('For any BLEEDER/borderline: which strategies does it block?')
    print('(only BOS + StrongFVG are LIVE; others are benched = research lead, not live leak)')
    for cat in g[g['net'] >= 3].index:
        s = df[df['cat'] == cat]
        by = s.groupby('signal')['net'].agg(['size', 'mean']).round(1)
        live = [x for x in s['signal'].unique() if x in ('BOS', 'StrongFVG')]
        tag = 'TOUCHES LIVE' if live else 'benched only'
        print(f'\n  {cat} [{tag}]:')
        for sig, row in by.iterrows():
            mark = '  <- LIVE' if sig in ('BOS', 'StrongFVG') else ''
            print(f'      {sig:14} n={int(row["size"]):3d}  net={row["mean"]:+.0f}{mark}')

    print('\n' + '=' * 78)
    print('READ THIS BEFORE CHANGING ANYTHING')
    print('=' * 78)
    print("""  • A SAVER blocking would-be losers is the filter working. Leave it.
  • A BLEEDER on a BENCHED strategy is a research lead (revive in paper?), not
    a live fix.
  • A borderline/coin-flip on a LIVE strategy (esp. regime=CHOPPY on BOS) is
    where the efficiency-aware paper flags earn their keep — let them split the
    bucket into the +EV efficient subset vs the rest. Validate in paper first.
  • Numbers are INDEX POINTS, optimistic vs real premium P&L. Only tick replay
    converts these to rupees. Watch for a bucket FLIPPING sign week-over-week —
    that flip is the real signal, not any single week's value.""")


if __name__ == '__main__':
    df = build(sys.argv[1] if len(sys.argv) > 1 else '.')
    report(df)
