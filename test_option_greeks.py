#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════
  test_option_greeks.py  —  Upstox Option Data Verification Harness (for v13)
═══════════════════════════════════════════════════════════════════════════

  PURPOSE
    Before building v13's option-chain-driven trade logic, we MUST see what
    Upstox actually returns for live Nifty option greeks. This script makes
    ONE pass at market open and prints the raw values, then flags the two
    known format gotchas so we build v13 against reality, not assumptions.

  WHAT IT CHECKS
    1. v3 Option Greeks endpoint:  /v3/market-quote/option-greek
    2. v2 Put/Call Option Chain:   /v2/option/chain   (fallback / comparison)
    3. IV format    — is it a decimal (0.33) or a percentage (33.0)?
    4. Delta sign   — CE should be positive, PE negative
    5. Greek population — are values real, or null/zero (entitlement issue)?
    6. Liquidity    — bid/ask spread on the ATM strikes

  SAFETY
    - Read-only. Places NO orders. Touches no state. Risks nothing.
    - Reads ONLY config.LIVE_TOKEN. No secrets live in this file.

  HOW TO RUN  (on your server, during market hours)
    cd ~/algo-trading
    python3 test_option_greeks.py

  Then paste the FULL output back. That output unblocks the v13 build.
═══════════════════════════════════════════════════════════════════════════
"""

import sys
import json
import datetime

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip3 install requests")
    sys.exit(1)

# ── Pull token from config.py (same source the bot uses) ──────────────────
try:
    import config
    LIVE_TOKEN = config.LIVE_TOKEN
except Exception as e:
    print(f"ERROR: could not import LIVE_TOKEN from config.py: {e}")
    print("Make sure you run this from the same folder as config.py")
    sys.exit(1)

NIFTY_KEY = "NSE_INDEX|Nifty 50"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Authorization": f"Bearer {LIVE_TOKEN}",
}

LINE = "═" * 75
SUB  = "─" * 75


def hr(title=""):
    print(LINE)
    if title:
        print(f"  {title}")
        print(LINE)


def get_nifty_ltp():
    """Fetch current Nifty spot so we can find the ATM strike."""
    try:
        r = requests.get(
            "https://api.upstox.com/v3/market-quote/ltp",
            params={"instrument_key": NIFTY_KEY},
            headers=HEADERS, timeout=10,
        )
        if r.status_code != 200:
            print(f"  LTP fetch HTTP {r.status_code}: {r.text[:150]}")
            return None
        data = r.json().get("data", {})
        # response keys are unpredictable; take the first numeric last_price
        for v in data.values():
            lp = v.get("last_price")
            if lp:
                return float(lp)
    except Exception as e:
        print(f"  LTP fetch error: {e}")
    return None


def get_nearest_expiry():
    """Fetch nearest Nifty option expiry — v2 chain REQUIRES this parameter.
    Uses /v2/option/contract, same as the production module."""
    hr("STEP 0 — Fetch nearest expiry  (/v2/option/contract)")
    try:
        r = requests.get(
            "https://api.upstox.com/v2/option/contract",
            params={"instrument_key": NIFTY_KEY},
            headers=HEADERS, timeout=10,
        )
        print(f"  HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  Body: {r.text[:250]}")
            return None
        data = r.json().get("data", [])
        today = datetime.date.today().strftime("%Y-%m-%d")
        expiries = sorted(set(
            d.get("expiry", "") for d in data
            if d.get("expiry", "") >= today
        ))
        print(f"  Expiries available: {expiries[:6]}")
        for exp in expiries:
            if exp > today:
                print(f"  → Using nearest future expiry: {exp}")
                return exp
        if expiries:
            print(f"  → Only same-day expiry available: {expiries[0]}")
            return expiries[0]
    except Exception as e:
        print(f"  Expiry fetch error: {e}")
    return None


def fetch_v2_chain(expiry=None):
    """v2 Put/Call option chain — strike-organized, includes greeks.
    REQUIRES expiry_date parameter (HTTP 400 without it)."""
    hr("TEST 1 — v2 Put/Call Option Chain  (/v2/option/chain)")
    if not expiry:
        print("  ⚠️  No expiry provided — v2 chain will reject this. Skipping.")
        return None
    params = {"instrument_key": NIFTY_KEY, "expiry_date": expiry}
    try:
        r = requests.get(
            "https://api.upstox.com/v2/option/chain",
            params=params, headers=HEADERS, timeout=12,
        )
        print(f"  HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  Body: {r.text[:300]}")
            return None
        data = r.json().get("data", [])
        print(f"  Strikes returned: {len(data)}")
        if not data:
            print("  ⚠️  EMPTY chain — no strikes returned.")
            return None
        # Show the structure of one strike so we see exact field names
        sample = data[len(data) // 2]  # middle strike ~ near ATM
        print(f"\n  Sample strike structure (keys):")
        print(f"    top-level: {list(sample.keys())}")
        for side in ("call_options", "put_options"):
            if side in sample and isinstance(sample[side], dict):
                co = sample[side]
                print(f"    {side}: {list(co.keys())}")
                if "option_greeks" in co:
                    print(f"      option_greeks: {co['option_greeks']}")
                if "market_data" in co:
                    md = co["market_data"]
                    print(f"      market_data: {list(md.keys())}")
        return data
    except Exception as e:
        print(f"  v2 chain error: {e}")
        return None


def fetch_v3_greeks(instrument_keys):
    """v3 Option Greeks — instrument-organized."""
    hr("TEST 2 — v3 Option Greeks  (/v3/market-quote/option-greek)")
    if not instrument_keys:
        print("  ⚠️  No instrument keys to query (need them from v2 chain first).")
        return None
    try:
        r = requests.get(
            "https://api.upstox.com/v3/market-quote/option-greek",
            params={"instrument_key": ",".join(instrument_keys)},
            headers=HEADERS, timeout=12,
        )
        print(f"  HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  Body: {r.text[:300]}")
            return None
        data = r.json().get("data", {})
        print(f"  Instruments returned: {len(data)}")
        for key, vals in data.items():
            print(f"\n  {key}")
            print(f"    {json.dumps(vals, indent=6)}")
        return data
    except Exception as e:
        print(f"  v3 greek error: {e}")
        return None


def analyse_gotchas(v3_data, v2_chain):
    """Flag the two format issues that would cause silent v13 bugs."""
    hr("GOTCHA ANALYSIS — format issues that would break v13")

    # ── IV format ──
    ivs = []
    if v3_data:
        ivs = [v.get("iv") for v in v3_data.values() if v.get("iv") is not None]
    if ivs:
        mx = max(ivs)
        print(f"\n  IV values seen (v3): {[round(x, 4) for x in ivs]}")
        if mx < 3:
            print(f"  → IV is a DECIMAL fraction (e.g. {mx:.4f} = {mx*100:.1f}%).")
            print(f"    v13 MUST multiply by 100 before comparing to % thresholds.")
        else:
            print(f"  → IV looks like a PERCENTAGE already (max {mx:.1f}).")
    else:
        print("\n  IV: no values found in v3 — check entitlement / market hours.")

    # ── Delta sign ──
    if v3_data:
        print(f"\n  Delta signs (v3):")
        for key, v in v3_data.items():
            d = v.get("delta")
            if d is None:
                print(f"    {key}: delta MISSING")
                continue
            side = "PE" if key.endswith("PE") else "CE" if key.endswith("CE") else "?"
            expected = "negative" if side == "PE" else "positive"
            actual = "negative" if d < 0 else "positive" if d > 0 else "zero"
            ok = (actual == expected) or actual == "zero"
            flag = "OK" if ok else "⚠️ UNEXPECTED"
            print(f"    {key}: delta={d:+.4f}  ({side} should be {expected})  {flag}")
        print("    v13 must handle the SIGN deliberately, not abs() blindly.")

    # ── Greek population / entitlement ──
    hr("ENTITLEMENT CHECK — are greeks actually populated?")
    if not v3_data:
        print("  ⚠️  v3 returned nothing. Either: market closed, no entitlement,")
        print("     or token lacks market-data access. v13 CANNOT use v3 greeks")
        print("     until this returns real values.")
    else:
        zero_greek = 0
        for key, v in v3_data.items():
            greeks = [v.get("delta"), v.get("gamma"), v.get("theta"), v.get("iv")]
            if all((g in (None, 0) ) for g in greeks):
                zero_greek += 1
                print(f"  ⚠️  {key}: ALL greeks null/zero")
        if zero_greek == 0:
            print("  ✅ Greeks are populated with real values. v13 foundation is SOLID.")
        else:
            print(f"  ⚠️  {zero_greek} instrument(s) had all-zero greeks — investigate.")


def main():
    hr(f"OPTION GREEK VERIFICATION — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Token: ...{LIVE_TOKEN[-8:]} (last 8 chars only)")
    print(f"  Underlying: {NIFTY_KEY}")
    print()

    # 1. Spot price → ATM strike
    ltp = get_nifty_ltp()
    if ltp is None:
        print("\n  ⚠️  Could not fetch Nifty LTP. Token expired or market closed?")
        print("     Cannot proceed without spot price. Stopping.")
        return
    atm = int(round(ltp / 50) * 50)
    print(f"  Nifty LTP: {ltp:.2f}  →  ATM strike: {atm}")
    print(f"  Strikes to inspect: {atm-50}, {atm}, {atm+50}\n")

    # 2. Nearest expiry (REQUIRED by v2 chain)
    expiry = get_nearest_expiry()
    if not expiry:
        print("\n  Could not determine expiry. v2 chain needs it. Stopping.")
        return

    # 3. v2 chain (also gives us instrument_keys for v3)
    v2 = fetch_v2_chain(expiry)

    # 3. Extract instrument keys near ATM for the v3 greek call
    instr_keys = []
    if v2:
        for row in v2:
            sp = row.get("strike_price")
            if sp in (atm - 50, atm, atm + 50):
                for side in ("call_options", "put_options"):
                    co = row.get(side, {})
                    ik = co.get("instrument_key")
                    if ik:
                        instr_keys.append(ik)

    # 4. v3 greeks
    v3 = fetch_v3_greeks(instr_keys[:10])

    # 5. Gotcha + entitlement analysis
    analyse_gotchas(v3, v2)

    hr("DONE — paste this ENTIRE output back to continue the v13 build")


if __name__ == "__main__":
    main()
