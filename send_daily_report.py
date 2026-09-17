import requests
import os
import datetime
import config

def send_telegram(message):
    url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id"   : config.CHAT_ID,
        "text"      : message,
        "parse_mode": "HTML"
    }, timeout=10)

def send_file(filepath, caption):
    if not os.path.exists(filepath):
        send_telegram(f"File not found: {filepath}")
        return False
    url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
    with open(filepath, "rb") as f:
        resp = requests.post(url, data={
            "chat_id" : config.CHAT_ID,
            "caption" : caption
        }, files={"document": f}, timeout=30)
    ok = resp.json().get("ok")
    print(f"Sent {filepath}: {ok}")
    return ok

today = datetime.date.today().strftime("%Y-%m-%d")

send_telegram(
    f"📊 <b>DAILY CSV REPORT — {today}</b>\n"
    f"Sending all 4 log files..."
)

files = [
    ("spy_scan_log.csv",  "SPY Scan Log — 5min conditions"),
    ("spy_trade_log.csv", "SPY Trade Log — entries and exits"),
    ("scan_log_v2.csv",   "Nifty Scan Log — 5min conditions"),
    ("trade_log_v2.csv",  "Nifty Trade Log — entries and exits"),
]

base = "/home/salverukrishna83/algo-trading"
sent = 0
for filename, caption in files:
    filepath = os.path.join(base, filename)
    ok = send_file(filepath, caption)
    if ok:
        sent += 1

send_telegram(
    f"Done! Sent {sent}/4 files\n"
    f"Upload to Claude for analysis!"
)

print(f"Done! Sent {sent}/4 files")
