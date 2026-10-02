#!/bin/sh
# v8.2: обновление бота по заявке из Telegram — вне Docker, на сервере.
# Бот (админ прислал torrbot-vX.Y.zip и нажал «✅ Обновить») кладёт в ~/torrbot/data/update/:
#   torrbot-update.zip + request.json  →  systemd (torrbot-update.path) запускает эту программу.
# Что делает:
#   1. deploy.sh архива (снимок старой версии делает сам deploy.sh);
#   2. ждёт до UPDATE_WAIT секунд, что новый бот поднялся: alive.json с новой версией свежее начала
#      обновления, и ещё 30 с контейнер работает и не перезапускается;
#   3. не поднялся — rollback.sh к снимку, в Telegram «↩ вернул старую»;
#   4. поднялся — git add/commit/push в ~/torrbot (если это git-репозиторий; UPDATE_GIT_PUSH=0 — не пушить);
#   5. итог — админам в Telegram напрямую (как сторож сервера) и в result.json (видно в /update).
# Ручной запуск: sudo torrbot-update-agent    (лог: ~/torrbot/data/update/last.log)
APP=${TORRBOT_DIR:-/home/lerroy/torrbot}
ENV_FILE=${WATCH_ENV:-$APP/.env}
UPD=$APP/data/update
OWNER=${TORRBOT_OWNER:-$(stat -c %U "$APP")}
OWNER_HOME=$(getent passwd "$OWNER" | cut -d: -f6)
VERS=${TORRBOT_VERSIONS:-$OWNER_HOME/torrbot-versions}
CONTAINER=${UPDATE_CONTAINER:-torrbot}
WAIT=${UPDATE_WAIT:-240}
LOG=$UPD/last.log

getenv() {
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -1 | tr -d '\r' \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}
TOKEN=$(getenv BOT_TOKEN)
ADMINS=$(getenv ADMIN_IDS | tr ',;' '  ')
PUSH=${UPDATE_GIT_PUSH:-$(getenv UPDATE_GIT_PUSH)}
PUSH=${PUSH:-1}

send() {
  for a in $ADMINS; do
    curl -s -m 20 "https://api.telegram.org/bot$TOKEN/sendMessage" -d chat_id="$a" \
      --data-urlencode text="$1" >/dev/null || true
  done
}
as_owner() { runuser -u "$OWNER" -- env HOME="$OWNER_HOME" TORRBOT_DIR="$APP" TORRBOT_VERSIONS="$VERS" "$@"; }
json_str() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' '; }
result() {  # ok|fail текст
  printf '{"ok": %s, "at": %s, "text": "%s"}\n' "$([ "$1" = ok ] && echo true || echo false)" \
    "$(date +%s)" "$(json_str "$2")" > "$UPD/result.json.tmp" && mv "$UPD/result.json.tmp" "$UPD/result.json"
  send "$2"
}
ver_of() { grep -o '"[^"]*"' "$1/bot/__init__.py" 2>/dev/null | tr -d '"'; }

[ -f "$UPD/request.json" ] || exit 0
[ -f "$UPD/processing.json" ] && { echo "уже идёт обновление"; exit 0; }
mv "$UPD/request.json" "$UPD/processing.json"
trap 'rm -f "$UPD/processing.json"' EXIT
NEW=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$UPD/processing.json")
OLD=$(ver_of "$APP")
WORK=$(mktemp /tmp/torrbot-update-XXXXXX.zip)
mv "$UPD/torrbot-update.zip" "$WORK" && chmod 644 "$WORK" || { result fail "❌ Обновление: архива нет."; exit 1; }
START=$(date +%s)
echo "=== $(date '+%F %T') обновление v$OLD → v$NEW" > "$LOG"

if ! as_owner sh "$APP/deploy.sh" "$WORK" >> "$LOG" 2>&1; then
  SNAP=$(sed -n 's/^Снимок текущей версии [^:]*: //p' "$LOG" | tail -1)
  if [ -n "$SNAP" ] && [ -f "$SNAP" ]; then
    as_owner sh "$APP/rollback.sh" "$SNAP" >> "$LOG" 2>&1
  fi
  result fail "❌ Обновление до v$NEW не удалось (deploy.sh), оставил v$OLD.
$(tail -n 8 "$LOG")"
  rm -f "$WORK"; exit 1
fi
SNAP=$(sed -n 's/^Снимок текущей версии [^:]*: //p' "$LOG" | tail -1)
rm -f "$WORK"

# ждём, что новый бот поднялся
up=0; i=0
while [ $i -lt "$WAIT" ]; do
  sleep 5; i=$((i + 5))
  A="$UPD/alive.json"
  if [ -f "$A" ] && grep -q "\"version\": *\"$NEW\"" "$A" && [ "$(stat -c %Y "$A")" -ge "$START" ]; then
    up=1; break
  fi
done
if [ $up = 1 ]; then
  RC=$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null)
  sleep 30
  [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = true ] && \
    [ "$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null)" = "$RC" ] || up=0
fi

if [ $up != 1 ]; then
  echo "--- бот не поднялся, логи:" >> "$LOG"
  (cd "$APP" && docker compose logs --tail 30 bot) >> "$LOG" 2>&1
  if [ -n "$SNAP" ] && [ -f "$SNAP" ]; then
    as_owner sh "$APP/rollback.sh" "$SNAP" >> "$LOG" 2>&1
    result fail "↩ v$NEW не запустилась — вернул v$OLD.
Последние строки лога бота:
$(docker logs --tail 6 "$CONTAINER" 2>&1 | tail -6)
Полный лог: ~/torrbot/data/update/last.log"
  else
    result fail "⚠ v$NEW не запустилась, а снимка для отката не нашёл! Нужна помощь руками: sh ~/torrbot/rollback.sh"
  fi
  exit 1
fi

# git
GIT=""
if [ "$PUSH" = 1 ] && [ -d "$APP/.git" ]; then
  if as_owner git -C "$APP" add -A >> "$LOG" 2>&1 && \
     { as_owner git -C "$APP" diff --cached --quiet || as_owner git -C "$APP" commit -q -m "torrbot v$NEW" >> "$LOG" 2>&1; } && \
     as_owner git -C "$APP" push -q >> "$LOG" 2>&1; then
    GIT="
GitHub: запушено (torrbot v$NEW)."
  else
    GIT="
⚠ GitHub: не получилось запушить — см. ~/torrbot/data/update/last.log"
  fi
elif [ "$PUSH" = 1 ]; then
  GIT="
GitHub: ~/torrbot не git-репозиторий — не пушу."
fi
result ok "✅ Обновлено: v$OLD → v$NEW, бот работает.$GIT
Откат, если что: sh ~/torrbot/rollback.sh"
