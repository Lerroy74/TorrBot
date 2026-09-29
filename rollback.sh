#!/bin/sh
# Откат torrbot к сохранённой версии.
#   sh ~/torrbot/rollback.sh              — список снимков
#   sh ~/torrbot/rollback.sh <снимок>     — вернуть код этой версии
# Возвращается только код (bot/, docker-compose.yml и т.п.). .env и база бота
# остаются текущими: старые версии их понимают (новые поля просто игнорируют).
if [ "$TORRBOT_ROLLBACK_COPY" != 1 ]; then
  cp "$0" /tmp/torrbot-rollback.sh && TORRBOT_ROLLBACK_COPY=1 exec sh /tmp/torrbot-rollback.sh "$@"
fi
set -e
APP=${TORRBOT_DIR:-$HOME/torrbot}
VERS=${TORRBOT_VERSIONS:-$HOME/torrbot-versions}
if [ -z "$1" ]; then
  echo "Снимки (новые внизу):"; ls -1tr "$VERS"/*.tar.gz 2>/dev/null || echo "  пока нет"
  echo "Откат: sh $APP/rollback.sh <путь к снимку>"; exit 0
fi
SNAP=$1; [ -f "$SNAP" ] || SNAP="$VERS/$1"
[ -f "$SNAP" ] || { echo "Нет снимка $1"; exit 1; }
N=$(basename "$APP")
tar -xzf "$SNAP" -C "$(dirname "$APP")" --exclude="$N/.env" --exclude="$N/data"
cd "$APP" && docker compose up -d --build
echo "Откат выполнен: $(basename "$SNAP"). Текущая версия: $(grep -o '"[^"]*"' bot/__init__.py | tr -d '"')"
