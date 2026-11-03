#!/bin/sh
# Run every minute by papertrade-health.timer. Alerts on Telegram when the engine is down
# or (during market hours) when no tick has arrived for 2 minutes. Reads /opt/papertrade/.env.
set -eu
. /opt/papertrade/.env
STATE=/var/tmp/papertrade-health.state
alert() {
  [ -f "$STATE" ] && [ "$(cat "$STATE")" = "$1" ] && return 0   # don't repeat the same alert
  echo "$1" > "$STATE"
  curl -fsS -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=papertrade: $1" >/dev/null || true
}
BODY=$(curl -fsS -m 5 http://127.0.0.1:8000/health) || { alert "engine API not responding"; exit 0; }
NOW=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$NOW" -ge 0917 ] && [ "$NOW" -le 1528 ]; then
  LAST=$(echo "$BODY" | sed -n 's/.*"last_tick":"\([^"]*\)".*/\1/p')
  if [ -z "$LAST" ]; then alert "no live feed during market hours"; exit 0; fi
  AGE=$(( $(date +%s) - $(date -d "$LAST" +%s) ))
  [ "$AGE" -gt 120 ] && { alert "feed silent for ${AGE}s"; exit 0; }
fi
[ -f "$STATE" ] && { rm -f "$STATE"; curl -fsS -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=papertrade: recovered" >/dev/null || true; }
exit 0
