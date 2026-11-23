#!/bin/sh
# Обновление torrbot со снимком текущей версии (для отката — rollback.sh).
#   sh ~/torrbot/deploy.sh ~/torrbot-v4.zip
# Снимки лежат в ~/torrbot-versions/. В снимок идёт код, .env и база бота,
# но не закачки и не настройки Transmission.
if [ "$TORRBOT_DEPLOY_COPY" != 1 ]; then          # архив перезапишет этот файл — работаем из копии
  cp "$0" /tmp/torrbot-deploy.sh && TORRBOT_DEPLOY_COPY=1 exec sh /tmp/torrbot-deploy.sh "$@"
fi
set -e
ZIP=$1
APP=${TORRBOT_DIR:-$HOME/torrbot}
VERS=${TORRBOT_VERSIONS:-$HOME/torrbot-versions}
[ -n "$ZIP" ] && [ -f "$ZIP" ] || { echo "Использование: sh deploy.sh путь/к/torrbot-vN.zip"; exit 1; }
unzip -l "$ZIP" | grep -q "torrbot/bot/main.py" || { echo "$ZIP — не архив torrbot"; exit 1; }
# S1: распаковка только в staging и только разрешённые пути (белый список = bot/updater.py::inspect).
# Прямой unzip в $APP позволил бы архиву положить что угодно (.git/hooks, подменённый compose) —
# это исполняется на сервере или уходит в git push агентом обновления.
STG=$(mktemp -d /tmp/torrbot-stage-XXXXXX)
trap 'rm -rf "$STG"' EXIT
for p in $(unzip -Z1 "$ZIP"); do
  case "$p" in
    torrbot/bot/*.py|torrbot/tests/*.py|\
    torrbot/host/*.sh|torrbot/host/*.py|torrbot/host/install.*|torrbot/pi/*.sh|torrbot/pi/*.py|\
    torrbot/pi/*.md|torrbot/pi/*.example|\
    torrbot/VERSION|torrbot/deploy.sh|torrbot/rollback.sh|torrbot/docker-compose.yml|\
    torrbot/README.md|torrbot/requirements.txt|torrbot/Dockerfile|torrbot/.dockerignore) ;;
    *) echo "архив содержит недопустимый путь: $p"; exit 1 ;;
  esac
done
unzip -o -q "$ZIP" -d "$STG"
[ -f "$STG/torrbot/bot/main.py" ] || { echo "$ZIP — битый архив torrbot"; exit 1; }
ver(){ grep -o '"[^"]*"' "$1/bot/__init__.py" 2>/dev/null | tr -d '"' || true; }
OLD=$(ver "$APP"); OLD=${OLD:-old}
mkdir -p "$VERS"
SNAP="$VERS/v$OLD-$(date +%Y%m%d-%H%M).tar.gz"
tar -czf "$SNAP" -C "$(dirname "$APP")" --exclude="$(basename "$APP")/transmission" \
    --exclude="$(basename "$APP")/data/backups" --exclude="__pycache__" "$(basename "$APP")"
echo "Снимок текущей версии v$OLD: $SNAP"
cp -R "$STG/torrbot/." "$APP/"
NEW=$(ver "$APP")
cd "$APP" && docker compose up -d --build
echo "Готово: v$OLD → v$NEW. Откат: sh $APP/rollback.sh"
