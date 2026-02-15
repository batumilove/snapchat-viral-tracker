#!/bin/bash
# Scrape all creators in the watchlist
# Usage: ./scrape-watchlist.sh [host:port]
HOST="${1:-localhost:5007}"
echo "[$(date -Iseconds)] Starting watchlist scrape..."
RESULT=$(curl -s -X POST "http://${HOST}/api/scrape-watchlist")
echo "[$(date -Iseconds)] Result: $RESULT"
