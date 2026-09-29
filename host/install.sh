#!/bin/sh
# Установка сторожа сервера (один раз, и после изменений host-watch.sh):
#   sudo sh ~/torrbot/host/install.sh
# Копирует скрипт в /usr/local/bin (откат бота rollback.sh его не заденет)
# и включает systemd-таймер: проверка раз в 3 минуты.
set -e
[ "$(id -u)" = 0 ] || { echo "Запусти через sudo: sudo sh $0"; exit 1; }
DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE=${WATCH_ENV:-$(dirname "$DIR")/.env}
[ -f "$ENV_FILE" ] || { echo "Не найден $ENV_FILE"; exit 1; }
install -m 755 "$DIR/host-watch.sh" /usr/local/bin/torrbot-host-watch
cat > /etc/systemd/system/torrbot-host-watch.service <<UNIT
[Unit]
Description=torrbot: сторож сервера (туннель, бот) с оповещением в Telegram напрямую
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
Environment=WATCH_ENV=$ENV_FILE
ExecStart=/usr/local/bin/torrbot-host-watch
UNIT
cat > /etc/systemd/system/torrbot-host-watch.timer <<'UNIT'
[Unit]
Description=torrbot: сторож сервера раз в 3 минуты

[Timer]
OnBootSec=3min
OnUnitActiveSec=3min
AccuracySec=20s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now torrbot-host-watch.timer
echo "Установлено. Настройки берутся из $ENV_FILE"
WATCH_ENV="$ENV_FILE" /usr/local/bin/torrbot-host-watch --test
systemctl list-timers torrbot-host-watch.timer --no-pager | head -3
