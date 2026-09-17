#!/bin/bash
cd ~/algo-trading
source venv/bin/activate
python3 stock_news_analyser.py >> news_scan.log 2>&1
