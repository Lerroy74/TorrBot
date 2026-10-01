#!/bin/sh
# Восстановление torrbot из резервной копии.
#
#   sudo sh ~/torrbot/restore.sh                    — список копий на сервере
#   sudo sh ~/torrbot/restore.sh <копия>            — вернуть базу бота
#                                                    (пользователи, закачки, журнал, оценки, «хотим»…)
#   sudo sh ~/torrbot/restore.sh <копия> --full     — ещё .env, настройки Transmission и extra/
#   добавь -y, чтобы не спрашивать подтверждение
#
# <копия> — любое из:
#   torrbot-backup-*.tar.gz  (из Telegram или ~/torrbot/data/backups; можно просто имя файла)
#   before-v*.sqlite3        (копия базы, которую бот делает перед каждым обновлением)
#   папка before-restore-*   (состояние до прошлого восстановления — так восстановление отменяется)
#
# Перед восстановлением текущее состояние сохраняется в data/backups/before-restore-<дата>/.
# Фильмы на диске не трогаются (в бэкапах их нет — слишком большие).
set -e
APP=${TORRBOT_DIR:-$(cd "$(dirname "$0")" && pwd)}
BK="$APP/data/backups"
FULL=0; YES=0; FORCE=0; ARG=""
for a in "$@"; do
  case "$a" in
    --full) FULL=1 ;;
    -y|--yes) YES=1 ;;
    --force) FORCE=1 ;;
    *) ARG=$a ;;
  esac
done

if [ -z "$ARG" ]; then
  echo "Копии на сервере ($BK), новые внизу:"
  ls -1tr "$BK" 2>/dev/null | grep -E '\.tar\.gz$|\.sqlite3$|^before-restore-' || echo "  пока нет"
  echo
  echo "Восстановить базу:        sudo sh $APP/restore.sh <имя или путь>"
  echo "Базу + .env и настройки:  sudo sh $APP/restore.sh <имя или путь> --full"
  exit 0
fi

[ -w "$APP/data" ] || { echo "Нет прав на $APP/data — запусти через sudo: sudo sh $0 $*"; exit 1; }
SRC=$ARG; [ -e "$SRC" ] || SRC="$BK/$ARG"
[ -e "$SRC" ] || { echo "Не нашёл копию: $ARG (ни как путь, ни в $BK)"; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
case "$SRC" in
  *.tar.gz|*.tgz)
    tar -xzf "$SRC" -C "$TMP" || { echo "Архив повреждён: $SRC"; exit 1; }
    DIR=$TMP ;;
  *.sqlite3|*.bak*)
    mkdir -p "$TMP/data"; cp "$SRC" "$TMP/data/bot.sqlite3"; DIR=$TMP
    [ "$FULL" = 1 ] && { echo "В $SRC только база — .env и настройки восстановить из неё нельзя."; FULL=0; } ;;
  *)
    [ -d "$SRC" ] || { echo "Не понимаю, что это за копия: $SRC"; exit 1; }
    cp -rp "$SRC/." "$TMP/"; DIR=$TMP ;;            # копия источника — его не заденет сохранение ниже
esac
DB="$DIR/data/bot.sqlite3"
[ -f "$DB" ] || { echo "В копии нет базы бота (data/bot.sqlite3)"; exit 1; }
[ "$(head -c 15 "$DB")" = "SQLite format 3" ] || { echo "data/bot.sqlite3 в копии — не база SQLite"; exit 1; }
if command -v python3 >/dev/null 2>&1; then
  python3 -c "import sqlite3,sys; sys.exit(sqlite3.connect(sys.argv[1]).execute('pragma integrity_check').fetchone()[0] != 'ok')" "$DB" \
    || { echo "База в копии повреждена — выбери другую копию"; exit 1; }
fi

CUR=$(grep -o '"[^"]*"' "$APP/bot/__init__.py" | tr -d '"')
BVER=""
[ -f "$DIR/MANIFEST.txt" ] && BVER=$(sed -n 's/^version=//p' "$DIR/MANIFEST.txt")
echo "Копия:  $SRC"
[ -f "$DIR/MANIFEST.txt" ] && echo "        сделана $(sed -n 's/^created=//p' "$DIR/MANIFEST.txt"), версия бота v$BVER"
echo "Сейчас: v$CUR"
if [ -n "$BVER" ] && [ "$BVER" != "$CUR" ] && [ "$(printf '%s\n%s\n' "$BVER" "$CUR" | sort -V | tail -1)" = "$BVER" ]; then
  echo "⚠ Копия от более новой версии v$BVER. Сначала обнови бота до v$BVER (deploy.sh), потом восстанавливай."
  [ "$FORCE" = 1 ] || { echo "  (если точно знаешь, что делаешь, — добавь --force)"; exit 1; }
fi
echo "Будет восстановлено: база бота$( [ "$FULL" = 1 ] && echo ', .env, настройки Transmission, extra/')"
if [ "$YES" != 1 ]; then
  printf "Продолжить? [y/N] "; read ans
  case "$ans" in y|Y|д|Д|yes|да) ;; *) echo "Отменено"; exit 1 ;; esac
fi

cd "$APP"
TS=$(date +%Y%m%d-%H%M%S)
SAVE="$BK/before-restore-$TS"; n=1
while [ -e "$SAVE" ]; do SAVE="$BK/before-restore-$TS-$n"; n=$((n + 1)); done
mkdir -p "$SAVE/data"
docker compose stop bot >/dev/null 2>&1 || true                 # на новом сервере контейнеров ещё нет
[ "$FULL" = 1 ] && { docker compose stop transmission >/dev/null 2>&1 || true; }   # Transmission перезаписывает settings.json при остановке
[ -f data/bot.sqlite3 ] && cp -p data/bot.sqlite3 "$SAVE/data/"
if [ "$FULL" = 1 ]; then
  [ -f .env ] && cp -p .env "$SAVE/"
  [ -f transmission/settings.json ] && mkdir -p "$SAVE/transmission" && cp -p transmission/settings.json "$SAVE/transmission/"
  [ -d extra ] && cp -rp extra "$SAVE/"
fi
echo "Текущее состояние сохранено: $SAVE"

cp "$DB" data/bot.sqlite3
rm -f data/bot.sqlite3-wal data/bot.sqlite3-shm data/bot.sqlite3-journal
if [ "$FULL" = 1 ]; then
  [ -f "$DIR/.env" ] && cp "$DIR/.env" .env && echo "  .env — восстановлен"
  [ -f "$DIR/transmission/settings.json" ] && mkdir -p transmission && cp "$DIR/transmission/settings.json" transmission/ \
    && echo "  настройки Transmission — восстановлены"
  [ -d "$DIR/extra" ] && mkdir -p extra && cp -r "$DIR/extra/." extra/ && echo "  extra/ — восстановлена"
fi
docker compose up -d
echo
echo "✅ Восстановлено. Бот сам обновит старую базу под текущую версию при запуске."
echo "Проверка: docker compose logs --tail=20 bot"
echo "Отменить восстановление: sudo sh $APP/restore.sh $SAVE$( [ "$FULL" = 1 ] && echo ' --full')"
