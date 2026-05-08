#!/bin/bash
# =============================================================
#  stop_bot.sh — Nifty Bot v5 Cron Stop Script
#  Called by cron at 2:35 PM IST as safety net
#  (Bot should have exited itself at 2:30 PM — this is backup)
# =============================================================

BOT_DIR="$HOME/algo-trading"
LOG_FILE="$BOT_DIR/nifty_v5.log"
PID_FILE="$BOT_DIR/nifty5.pid"

cd "$BOT_DIR" || exit 1

# ── Kill by PID file ─────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        sleep 3
        # Force kill if still running
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID"
        echo "$(date '+%Y-%m-%d %H:%M:%S') Bot PID $PID stopped by cron" >> "$LOG_FILE"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') Bot already exited (PID $PID gone)" >> "$LOG_FILE"
    fi
    rm -f "$PID_FILE"
else
    # Fallback: kill by process name
    if pkill -f "nifty_bot_v5.py" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') Bot killed by pkill (no PID file)" >> "$LOG_FILE"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') Bot not running at 14:35 (already exited clean)" >> "$LOG_FILE"
    fi
fi
