#!/usr/bin/env bash
# Quick Start — Nifty Bot v13 Deployment Guide

echo "═══════════════════════════════════════════════════════════════════════════"
echo "  Nifty Bot v13 — Quick Start Deployment"
echo "═══════════════════════════════════════════════════════════════════════════"
echo ""

# Step 1: Check Python
echo "[STEP 1] Checking Python environment..."
python3 --version || { echo "❌ Python 3 not found"; exit 1; }

# Step 2: Install dependencies
echo ""
echo "[STEP 2] Installing dependencies..."
pip install pandas numpy requests pytz --break-system-packages -q || \
  { echo "❌ Failed to install dependencies"; exit 1; }
echo "✓ Dependencies installed"

# Step 3: Copy files
echo ""
echo "[STEP 3] Files in current directory:"
ls -1 | grep -E "signals.py|nifty_bot_v13.py|verify_v13.py|config.py|README_V13.md"

# Step 4: Check config
echo ""
echo "[STEP 4] Checking config.py..."
if grep -q "your_upstox_live_token" config.py; then
  echo "⚠️  config.py has placeholder token"
  echo "   → Edit config.py and replace with your real Upstox LIVE_TOKEN"
  echo "   → Then proceed to verification"
else
  echo "✓ config.py appears configured"
fi

# Step 5: Run verification
echo ""
echo "[STEP 5] Running verification..."
echo ""
python3 verify_v13.py

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo ""
echo "IF ALL CHECKS PASSED:"
echo ""
echo "  1. Ensure config.py has your real Upstox LIVE_TOKEN"
echo "  2. Run the bot during market hours (9:30 AM – 14:30 PM IST):"
echo "     python3 nifty_bot_v13.py"
echo ""
echo "  3. Monitor output:"
echo "     tail -f nifty_v13.log"
echo ""
echo "  4. After session, review:"
echo "     - scan_v13_YYYY-MM-DD.csv  (market snapshots)"
echo "     - trade_v13_YYYY-MM-DD.csv (all trades + P&L)"
echo "     - skip_v13_YYYY-MM-DD.csv  (filtered signals)"
echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
