#!/usr/bin/env bash
# Run on a NEW server before installing anything: can it reach everything the bot needs?
# Binance.com blocks some regions (e.g. the US). If a check fails, pick another region.
set -u
check() {
  local name=$1 url=$2
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url")
  if [[ $code =~ ^(200|401|404)$ ]]; then echo "OK    $name ($code)"; else echo "FAIL  $name (HTTP $code)"; fi
}
check "Binance spot"     "https://api.binance.com/api/v3/ping"
check "Binance futures"  "https://fapi.binance.com/fapi/v1/ping"
check "Telegram"         "https://api.telegram.org/bot0/getMe"
check "OpenRouter"       "https://openrouter.ai/api/v1/models"
check "Fear & Greed"     "https://api.alternative.me/fng/?limit=1"
