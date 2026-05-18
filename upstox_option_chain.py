"""
=============================================================
  upstox_option_chain.py — Upstox Live Option Chain Module
  ─────────────────────────────────────────────────────────
  PURPOSE:
    Fetch live option chain data from Upstox API for Nifty 50.
    Provides:
      • ATM/OTM strike selection
      • Real bid/ask spread check (liquidity gate)
      • OI-based live PCR (replaces pre-market PCR)
      • Delta per strike (converts index SL/TGT to premium)
      • IV for position sizing
      • Option instrument key for actual order placement

  SOLVES (from professional review):
    1. Index SL/TGT ≠ option premium behaviour
       → Use actual delta to convert: premium_sl = index_sl × delta
    2. No liquidity filter
       → bid/ask spread check: skip if spread > MAX_SPREAD
    3. PCR from options data intraday (more accurate than PCR api)
       → live PCR = Σ PE_OI / Σ CE_OI across all strikes

  USAGE (in nifty_bot_v9.py):
    from upstox_option_chain import OptionChain
    oc = OptionChain(live_token)
    oc.refresh(nifty_ltp)          # call every 15 min
    strike = oc.get_atm_strike(nifty_ltp, "CE")
    spread_ok = oc.liquidity_ok(strike, "CE")
    delta     = oc.get_delta(strike, "CE")
    live_pcr  = oc.get_live_pcr()
    instr_key = oc.get_instrument_key(strike, "CE")

  CALLED FROM: nifty_bot_v9.py (integrated in v10)
=============================================================
"""

import requests
import datetime
import logging
import math
import time
from typing import Optional, Dict, Tuple

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────
NIFTY_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
OTM_OFFSET           = 100    # strike offset from ATM (fallback)
TARGET_DELTA_MIN     = 0.40   # [REVIEW-2] min delta for entry (scalp mode)
TARGET_DELTA_MAX     = 0.60   # max delta — avoid deep ITM cost
                             # OTM_OFFSET=100 gives delta ~0.25-0.35 on slow days
                             # → theta kills trade even if direction correct
                             # Now: scan strikes to find delta in 0.40-0.60 range
CHAIN_DEPTH          = 5      # strikes above + below ATM to fetch
MAX_SPREAD_RS        = 4.0    # absolute max spread cap in Rs
MAX_SPREAD_PCT       = 0.08   # max spread as % of mid-price (8%)
                             # Rs.4 spread on Rs.50 prem = 8% (borderline)
                             # Rs.4 spread on Rs.20 prem = 20% (blocks rightly)
                             # Rs.4 spread on Rs.300 prem = 1.3% (would wrongly block if fixed=4)
MIN_BID_QTY          = 25     # min bid qty (lots) for liquidity
MIN_OI               = 50000  # min OI per strike to consider liquid
CACHE_TTL_SECS       = 900    # refresh interval normal (15 min)
CACHE_TTL_HIGH_VOL   = 300    # refresh interval high-ATR (5 min)
                             # [REVIEW-3] fast OI/PCR shifts during volatile markets
ATR_HIGH_VOL_THRESH  = 35    # ATR above this = high volatility mode

HEADERS_BASE = {
    "Accept"     : "application/json",
    "Api-Version": "2.0",
}


def get_nearest_expiry() -> str:
    """
    Returns nearest weekly Thursday expiry as YYYY-MM-DD.
    Nifty weekly options expire every Thursday.
    If today IS Thursday, use next Thursday (don't trade expiry day
    unless specifically enabled — theta risk too high).
    """
    today    = datetime.date.today()
    weekday  = today.weekday()   # 0=Mon … 6=Sun
    thu      = 3                 # Thursday
    days_to  = (thu - weekday) % 7
    if days_to == 0:
        days_to = 7              # skip same-day expiry
    return (today + datetime.timedelta(days=days_to)).strftime("%Y-%m-%d")


def round_to_strike(ltp: float, step: int = 50) -> int:
    """Round Nifty LTP to nearest strike (50pt intervals)."""
    return int(round(ltp / step) * step)


class OptionChainData:
    """Holds data for a single option strike (CE or PE)."""
    def __init__(self, raw: dict):
        md = raw.get("market_data", {})
        self.ltp          = float(md.get("ltp", 0) or 0)
        self.bid          = float(md.get("bid_price", 0) or 0)
        self.ask          = float(md.get("ask_price", 0) or 0)
        self.bid_qty      = int(md.get("bid_qty", 0) or 0)
        self.ask_qty      = int(md.get("ask_qty", 0) or 0)
        self.oi           = int(md.get("oi", 0) or 0)
        self.volume       = int(md.get("volume", 0) or 0)
        self.iv           = float(md.get("iv", 0) or 0)
        self.delta        = float(md.get("delta", 0) or 0)
        self.theta        = float(md.get("theta", 0) or 0)
        self.gamma        = float(md.get("gamma", 0) or 0)
        self.instrument_key = raw.get("instrument_key", "")

    @property
    def spread(self) -> float:
        return round(self.ask - self.bid, 2) if self.ask > 0 and self.bid > 0 else 999.0

    @property
    def mid_price(self) -> float:
        return round((self.bid + self.ask) / 2, 2) if self.ask > 0 and self.bid > 0 else self.ltp

    def is_liquid(self, max_spread: float = MAX_SPREAD_RS,
                  max_spread_pct: float = MAX_SPREAD_PCT,
                  min_bid_qty: int = MIN_BID_QTY,
                  min_oi: int = MIN_OI) -> bool:
        """
        [REVIEW-1] Dynamic spread gate: spread < 8% of mid-price
        AND spread < Rs.4 absolute cap.
        Why: Rs.4 fixed threshold wrong for both extremes:
          Rs.4 spread on Rs.20 prem  = 20% — too wide, rightly block
          Rs.4 spread on Rs.300 prem = 1.3% — fine, but fixed gate allowed
        Using both: % gate catches low-priced options,
                    absolute cap catches high-priced ones.
        """
        mid = self.mid_price
        pct_spread = self.spread / mid if mid > 0 else 999
        spread_ok = self.spread <= max_spread and pct_spread <= max_spread_pct
        return (spread_ok and
                self.bid_qty >= min_bid_qty and
                self.oi >= min_oi)

    def premium_sl(self, index_sl_pts: float) -> float:
        """
        Convert index SL pts → option premium SL using actual delta.
        [FUTURE-REVIEW] Full Taylor expansion:
          premium = (delta × move) + (0.5 × gamma × move²) - theta_decay
        Deferred: needs 10+ sessions of gamma behaviour data.
        Current: delta-only (accurate for ATM options, ±15% for OTM weekly).
        """
        if abs(self.delta) < 0.05:
            return index_sl_pts * 0.35   # fallback estimate
        return round(abs(index_sl_pts * self.delta), 2)

    def premium_tgt(self, index_tgt_pts: float) -> float:
        """Convert index target pts → option premium target using delta."""
        if abs(self.delta) < 0.05:
            return index_tgt_pts * 0.35
        return round(abs(index_tgt_pts * self.delta), 2)


class OptionChain:
    """
    Main option chain manager.
    Fetches and caches Upstox option chain for Nifty.
    Refreshes every CACHE_TTL_SECS (15 min).
    """

    def __init__(self, live_token: str):
        self._token      = live_token
        self._headers    = {**HEADERS_BASE, "Authorization": f"Bearer {live_token}"}
        self._chain      : Dict[int, Dict[str, OptionChainData]] = {}
        self._last_refresh = 0.0
        self._expiry     = get_nearest_expiry()
        self._atm_strike = 0
        self._live_pcr   = 0.0
        self._fetch_ok   = False

    # ── Public API ───────────────────────────────────────────

    def refresh(self, nifty_ltp: float, force: bool = False, atr: float = 0) -> bool:
        """
        Fetch option chain if cache is stale (>15 min) or force=True.
        Returns True if data is fresh and valid.
        """
        # [REVIEW-3] Dynamic TTL: 5 min during high vol, 15 min normally
        atr_now = getattr(self, "_last_atr", 0)
        ttl = CACHE_TTL_HIGH_VOL if atr_now >= ATR_HIGH_VOL_THRESH else CACHE_TTL_SECS
        if not force and time.time() - self._last_refresh < ttl:
            return self._fetch_ok

        # Update expiry if Thursday passed
        self._expiry = get_nearest_expiry()
        self._last_atr = atr  # [REVIEW-3] stored for dynamic TTL
        self._atm_strike = round_to_strike(nifty_ltp)

        try:
            data = self._fetch_chain()
            if not data:
                log.warning("Option chain: empty response")
                self._fetch_ok = False
                return False

            self._chain = {}
            total_pe_oi = 0
            total_ce_oi = 0

            for item in data:
                strike = int(item.get("strike_price", 0))
                ce_raw = item.get("call_options", {})
                pe_raw = item.get("put_options", {})
                ce = OptionChainData(ce_raw) if ce_raw else None
                pe = OptionChainData(pe_raw) if pe_raw else None
                self._chain[strike] = {"CE": ce, "PE": pe}
                if ce: total_ce_oi += ce.oi
                if pe: total_pe_oi += pe.oi

            self._live_pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0.0
            self._last_refresh = time.time()
            self._fetch_ok = True
            log.info(f"Option chain refreshed: {len(self._chain)} strikes | "
                     f"ATM={self._atm_strike} Expiry={self._expiry} PCR={self._live_pcr}")
            return True

        except Exception as e:
            log.error(f"Option chain refresh failed: {e}")
            self._fetch_ok = False
            return False

    def get_atm_option(self, nifty_ltp: float,
                       option_type: str) -> Optional[OptionChainData]:
        """Get ATM CE or PE option data."""
        strike = round_to_strike(nifty_ltp)
        return self._get(strike, option_type)

    def get_otm_option(self, nifty_ltp: float,
                       direction: str) -> Optional[OptionChainData]:
        """
        Get the right OTM option for the trade direction.
        direction='bullish' → CE (call), OTM_OFFSET above ATM
        direction='bearish' → PE (put), OTM_OFFSET below ATM
        """
        atm = round_to_strike(nifty_ltp)
        if direction == "bullish":
            strike = atm + OTM_OFFSET
            return self._get(strike, "CE")
        else:
            strike = atm - OTM_OFFSET
            return self._get(strike, "PE")

    def get_best_option(self, nifty_ltp: float,
                        direction: str,
                        prefer_atm: bool = True) -> Optional[OptionChainData]:
        """
        [REVIEW-2] Select strike by TARGET DELTA RANGE (0.40-0.60)
        not by fixed OTM offset.

        Why delta-range matters:
          OTM+100 gives delta ~0.25-0.35 on low-volatility days.
          At delta=0.30, a 12pt Nifty move = only Rs.3.6 premium move.
          With Rs.4 spread, the trade is mathematically impossible.

        Logic:
          1. Find all strikes where option delta is in [0.40, 0.60]
          2. Among those, pick the most liquid one
          3. Fall back to ATM if nothing in delta range
        """
        atm      = round_to_strike(nifty_ltp)
        opt_type = "CE" if direction == "bullish" else "PE"

        # Collect all strikes in target delta range
        delta_candidates = []
        for strike, data in sorted(self._chain.items()):
            opt = data.get(opt_type)
            if opt is None or opt.ltp <= 0: continue
            d = abs(opt.delta)
            if TARGET_DELTA_MIN <= d <= TARGET_DELTA_MAX:
                delta_candidates.append((strike, opt))

        # Sort by liquidity (OI desc) and pick best
        delta_candidates.sort(key=lambda x: -x[1].oi)
        for strike, opt in delta_candidates:
            if opt.is_liquid():
                if strike != atm:
                    log.info(f"[DeltaSelect] Using strike {strike} "
                             f"(delta={opt.delta:.2f}) vs ATM {atm}")
                return opt

        # Fallback 1: ATM (delta ~0.50, always in range)
        atm_opt = self._get(atm, opt_type)
        if atm_opt and atm_opt.is_liquid():
            return atm_opt

        # Fallback 2: OTM offset (original behaviour)
        otm_strike = (atm + OTM_OFFSET if direction == "bullish"
                      else atm - OTM_OFFSET)
        otm_opt = self._get(otm_strike, opt_type)
        if otm_opt and otm_opt.is_liquid():
            return otm_opt

        # Last resort: ATM illiquid with warning
        if atm_opt and atm_opt.ltp > 0:
            log.warning(f"Option illiquid fallback — spread:{atm_opt.spread:.1f} "
                        f"OI:{atm_opt.oi:,} delta:{atm_opt.delta:.2f}")
            return atm_opt
        return None

    def liquidity_ok(self, nifty_ltp: float, direction: str) -> Tuple[bool, str]:
        """
        Gate: is there sufficient option liquidity to trade?
        Returns (ok: bool, reason: str)
        """
        if not self._fetch_ok:
            return True, "Chain not fetched — liquidity unknown"

        opt = self.get_best_option(nifty_ltp, direction)
        if opt is None:
            return False, "No option data found"

        opt_type = "CE" if direction == "bullish" else "PE"
        atm      = round_to_strike(nifty_ltp)

        mid = opt.mid_price
        pct_spread = opt.spread / mid * 100 if mid > 0 else 999
        if opt.spread > MAX_SPREAD_RS or pct_spread > MAX_SPREAD_PCT * 100:
            return False, (f"Illiquid: spread Rs.{opt.spread:.1f} "
                           f"({pct_spread:.1f}% of prem Rs.{mid:.0f}) "
                           f"> max Rs.{MAX_SPREAD_RS}/8% (ATM={atm} {opt_type})")
        if opt.bid_qty < MIN_BID_QTY:
            return False, (f"Thin book: bid_qty={opt.bid_qty} < "
                           f"{MIN_BID_QTY} lots (ATM={atm} {opt_type})")
        if opt.oi < MIN_OI:
            return False, (f"Low OI: {opt.oi:,} < {MIN_OI:,} (ATM={atm} {opt_type})")

        return True, (f"Liquid ✅ spread:{opt.spread:.1f} "
                      f"OI:{opt.oi:,} bid_qty:{opt.bid_qty}")

    def get_delta(self, nifty_ltp: float, direction: str) -> float:
        """
        Get actual delta for the trade option.
        Used to convert index SL/TGT to option premium SL/TGT.
        Falls back to 0.45 (ATM estimate) if chain not available.
        """
        if not self._fetch_ok:
            return 0.45   # ATM estimate fallback

        opt = self.get_best_option(nifty_ltp, direction)
        if opt and abs(opt.delta) > 0.05:
            return abs(opt.delta)
        return 0.45

    def get_live_pcr(self) -> float:
        """
        Live PCR from actual option chain OI.
        More accurate than pre-market PCR API.
        Updates every 15 min with chain refresh.
        """
        return self._live_pcr

    def get_iv(self, nifty_ltp: float, direction: str) -> float:
        """Get IV of the option we'd trade. Used for position sizing."""
        opt = self.get_best_option(nifty_ltp, direction)
        if opt and opt.iv > 0:
            return opt.iv
        return 15.0   # default estimate

    def get_instrument_key(self, nifty_ltp: float, direction: str) -> str:
        """Get Upstox instrument key for the option to trade/track."""
        opt = self.get_best_option(nifty_ltp, direction)
        if opt:
            return opt.instrument_key
        return ""

    def premium_sl(self, nifty_ltp: float,
                   direction: str,
                   index_sl_pts: float) -> float:
        """
        Convert index SL points → option premium SL in Rs.
        Core fix: we were using index pts directly, now use delta.
        
        Example:
          index_sl = 12pts, delta = 0.45
          premium_sl = 12 × 0.45 = Rs.5.40
          
          If bid/ask spread = Rs.3.50 → spread alone eats 65% of SL
          → this scenario should be flagged as low-quality trade
        """
        opt = self.get_best_option(nifty_ltp, direction)
        if opt:
            prem_sl = opt.premium_sl(index_sl_pts)
            # Warn if spread > 50% of SL (dangerous)
            if opt.spread > prem_sl * 0.5:
                log.warning(
                    f"Spread risk: spread Rs.{opt.spread:.1f} > "
                    f"50% of premium SL Rs.{prem_sl:.1f} — "
                    f"consider skipping (spread eats SL)"
                )
            return prem_sl
        return round(index_sl_pts * 0.45, 2)   # fallback

    def get_summary(self, nifty_ltp: float) -> str:
        """Human-readable summary for Telegram / scan log."""
        if not self._fetch_ok:
            return "OptionChain: not fetched"

        ce_opt = self.get_best_option(nifty_ltp, "bullish")
        pe_opt = self.get_best_option(nifty_ltp, "bearish")
        atm    = round_to_strike(nifty_ltp)

        ce_str = (f"CE{atm}: LTP:{ce_opt.ltp:.0f} Δ:{ce_opt.delta:.2f} "
                  f"Sprd:{ce_opt.spread:.1f} IV:{ce_opt.iv:.0f}%"
                  if ce_opt else "CE: N/A")
        pe_str = (f"PE{atm-OTM_OFFSET}: LTP:{pe_opt.ltp:.0f} Δ:{pe_opt.delta:.2f} "
                  f"Sprd:{pe_opt.spread:.1f} IV:{pe_opt.iv:.0f}%"
                  if pe_opt else "PE: N/A")

        return (f"OC [{self._expiry}] PCR:{self._live_pcr:.2f}\n"
                f"  {ce_str}\n  {pe_str}")

    # ── Private ──────────────────────────────────────────────

    def _fetch_chain(self) -> list:
        """Fetch option chain from Upstox API."""
        url = "https://api.upstox.com/v2/option/chain"
        params = {
            "instrument_key": NIFTY_INSTRUMENT_KEY,
            "expiry_date"   : self._expiry,
        }
        resp = requests.get(url, headers=self._headers,
                            params=params, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", [])
        elif resp.status_code == 401:
            log.error("Option chain: token expired or invalid")
            return []
        else:
            log.error(f"Option chain HTTP {resp.status_code}: {resp.text[:100]}")
            return []

    def _get(self, strike: int,
             opt_type: str) -> Optional[OptionChainData]:
        """Get option data for a specific strike and type."""
        strike_data = self._chain.get(strike)
        if not strike_data:
            return None
        return strike_data.get(opt_type)


# ─────────────────────────────────────────────
#  OPTION CHAIN QUALITY GATE
#  Used in try_trade() to gate entries
# ─────────────────────────────────────────────

def check_option_quality(oc: Optional["OptionChain"],
                         nifty_ltp: float,
                         direction: str,
                         index_sl: float,
                         index_tgt: float) -> Tuple[bool, str, dict]:
    """
    Full option quality gate before entering any trade.
    Returns (ok, reason, info_dict)

    Checks:
      1. Liquidity (spread, OI, bid qty)
      2. SL viability (spread < 50% of premium SL)
      3. IV sanity (not entering extremely high IV)

    info_dict contains delta, premium_sl, premium_tgt, spread, iv
    for logging in trade CSV.
    """
    if oc is None or not oc._fetch_ok:
        # Chain not available — allow trade but warn
        return True, "OptionChain N/A — no quality gate", {
            "delta": 0.45, "premium_sl": round(index_sl*0.45,2),
            "premium_tgt": round(index_tgt*0.45,2),
            "spread": 0.0, "iv": 0.0, "liquidity": "unknown"
        }

    opt = oc.get_best_option(nifty_ltp, direction)
    if opt is None:
        return True, "No option data — no quality gate", {
            "delta": 0.45, "premium_sl": round(index_sl*0.45,2),
            "premium_tgt": round(index_tgt*0.45,2),
            "spread": 0.0, "iv": 0.0, "liquidity": "unknown"
        }

    delta       = abs(opt.delta) if abs(opt.delta) > 0.05 else 0.45
    prem_sl     = round(index_sl  * delta, 2)
    prem_tgt    = round(index_tgt * delta, 2)
    spread      = opt.spread
    iv          = opt.iv
    liq_ok, liq_reason = oc.liquidity_ok(nifty_ltp, direction)

    info = {
        "delta"      : round(delta, 3),
        "premium_sl" : prem_sl,
        "premium_tgt": prem_tgt,
        "spread"     : spread,
        "iv"         : round(iv, 1),
        "liquidity"  : liq_reason,
        "instr_key"  : opt.instrument_key,
    }

    # Gate 1: Liquidity
    if not liq_ok:
        return False, f"IlliquidOption: {liq_reason}", info

    # Gate 2: Spread must be < 8% of option mid-price
    # Correct comparison: spread vs premium size (not vs SL)
    # Rs.3 spread on Rs.100 premium = 3% → fine
    # Rs.3 spread on Rs.20 premium  = 15% → blocked (illiquid)
    # Rs.4 spread on Rs.30 premium  = 13% → blocked (dead market)
    mid_price = opt.mid_price
    pct_spread_entry = spread / mid_price * 100 if mid_price > 0 else 999
    if pct_spread_entry > MAX_SPREAD_PCT * 100:
        return False, (
            f"SpreadRisk: spread Rs.{spread:.1f} = {pct_spread_entry:.1f}% "
            f"of premium Rs.{mid_price:.0f} > {MAX_SPREAD_PCT*100:.0f}% limit"
        ), info

    # Gate 3: IV sanity — don't buy extremely expensive options
    if iv > 25.0:
        return False, f"HighIV: {iv:.0f}% — options overpriced (>25%)", info

    return True, f"OK delta:{delta:.2f} prem_SL:{prem_sl} spread:{spread:.1f}", info


# ─────────────────────────────────────────────
#  STANDALONE TEST
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import config
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    print("Testing Upstox Option Chain...")
    oc = OptionChain(config.LIVE_TOKEN)

    # Use current Nifty LTP (approximate)
    test_ltp = 23750.0

    print(f"\nFetching chain for Nifty LTP={test_ltp}...")
    ok = oc.refresh(test_ltp, force=True)
    print(f"Fetch status: {'✅ OK' if ok else '❌ FAILED'}")

    if ok:
        print(f"\n{oc.get_summary(test_ltp)}")
        print(f"\nLive PCR (from OI): {oc.get_live_pcr():.3f}")

        for dirn in ["bullish", "bearish"]:
            liq_ok, liq_reason = oc.liquidity_ok(test_ltp, dirn)
            delta = oc.get_delta(test_ltp, dirn)
            prem_sl  = oc.premium_sl(test_ltp, dirn, 12.0)
            prem_tgt = round(20.0 * delta, 2)
            iv   = oc.get_iv(test_ltp, dirn)

            print(f"\n  {dirn.upper()} option:")
            print(f"    Liquidity : {'✅' if liq_ok else '❌'} {liq_reason}")
            print(f"    Delta     : {delta:.3f}")
            print(f"    Prem SL   : Rs.{prem_sl} (12 index pts)")
            print(f"    Prem TGT  : Rs.{prem_tgt} (20 index pts)")
            print(f"    IV        : {iv:.1f}%")
            print(f"    Instr key : {oc.get_instrument_key(test_ltp, dirn)}")

            gate_ok, gate_reason, info = check_option_quality(
                oc, test_ltp, dirn, 12.0, 20.0
            )
            print(f"    Quality gate: {'✅' if gate_ok else '❌'} {gate_reason}")
    else:
        print("Token may be expired. Run during market hours with fresh token.")
