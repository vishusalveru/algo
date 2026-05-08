#!/bin/bash
# =============================================================
#  start_bot.sh — Nifty Bot v5 Cron Wrapper
#  Called by cron at 8:45 AM IST Mon–Fri
#  Handles: holidays, already-running check, logging
# =============================================================

BOT_DIR="$HOME/algo-trading"
BOT_FILE="nifty_bot_v5.py"
LOG_FILE="$BOT_DIR/nifty_v5.log"
PID_FILE="$BOT_DIR/nifty5.pid"
HOLIDAY_FILE="$BOT_DIR/nse_holidays.txt"

cd "$BOT_DIR" || exit 1

# ── Holiday check ────────────────────────────────────────────
TODAY=$(date +%Y-%m-%d)

# NSE 2026 holidays — update this file yearly
# Format: one date per line YYYY-MM-DD
if [ ! -f "$HOLIDAY_FILE" ]; then
cat > "$HOLIDAY_FILE" << 'HOLIDAYS'
2026-01-26
2026-03-02
2026-03-20
2026-04-02
2026-04-14
2026-05-01
2026-08-15
2026-10-02
2026-10-22
2026-11-11
2026-11-12
2026-12-25
HOLIDAYS
fi

if grep -qx "$TODAY" "$HOLIDAY_FILE"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') NSE Holiday $TODAY — bot not started" >> "$LOG_FILE"
    exit 0
fi

# ── Already running check ────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') Bot already running (PID $OLD_PID) — skipping" >> "$LOG_FILE"
        exit 0
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') Stale PID file found — cleaning up" >> "$LOG_FILE"
        rm -f "$PID_FILE"
    fi
fi

# ── Trim log file — keep last 5000 lines only ───────────────
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt 5000 ]; then
    tail -5000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
    echo "$(date '+%Y-%m-%d %H:%M:%S') Log trimmed to 5000 lines" >> "$LOG_FILE"
fi

# ── Start bot ────────────────────────────────────────────────
echo "$(date '+%Y-%m-%d %H:%M:%S') Starting Nifty Bot v5 for $TODAY" >> "$LOG_FILE"

nohup python3 "$BOT_DIR/$BOT_FILE" >> "$LOG_FILE" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$PID_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') Bot started — PID $BOT_PID" >> "$LOG_FILE"
