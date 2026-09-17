#!/bin/bash
cd ~/algo-trading || exit 1
D=$(date +%F)

# 1. token sanity — a stale/short token means no trades all day
LEN=$(python3 -c "import config;print(len(config.LIVE_TOKEN))" 2>/dev/null)
if [ -z "$LEN" ] || [ "$LEN" -lt 100 ]; then
  echo "❌ config.py token missing or too short (len=$LEN). Refresh it first."; exit 1
fi
echo "✓ token len $LEN"

# 2. logic self-test
python3 verify_v14.py > /tmp/verify.out 2>&1 || { echo "❌ verify_v14 FAILED"; tail -20 /tmp/verify.out; exit 1; }
grep -q "ALL CHECKS PASSED" /tmp/verify.out || { echo "❌ verify_v14 FAILED"; tail -20 /tmp/verify.out; exit 1; }
echo "✓ verify_v14 passed"

# 3. kill anything already running (two live bots = double real orders)
pkill -f "python3 nifty_bot_v14.py"  2>/dev/null
pkill -f "python3 nifty_live_bot.py" 2>/dev/null
sleep 2
if pgrep -f "python3 nifty_(bot_v14|live_bot).py" > /dev/null; then
  echo "❌ old process still alive — kill it manually before starting"; exit 1
fi
echo "✓ no stale processes"

# 4. start both
nohup python3 nifty_bot_v14.py  > run_paper_$D.out 2>&1 & echo $! > paper.pid
nohup python3 nifty_live_bot.py > run_live_$D.out  2>&1 & echo $! > live_bot.pid
sleep 6

# 5. confirm they survived startup
ok=1
for n in paper live_bot; do
  p=$(cat $n.pid)
  if ps -p "$p" > /dev/null 2>&1; then echo "✓ $n running (pid $p)"
  else echo "❌ $n DIED on startup:"; tail -15 run_${n/live_bot/live}_$D.out; ok=0; fi
done
[ $ok -eq 1 ] && echo "" && echo "Both up. Check Telegram for the FLAGS + CAL banner."
