#!/bin/sh
# Установка сторожа малинки (на самой малинке, один раз и после обновлений):
#   sh /storage/torrbot-pi/install-watch.sh
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
CFG=/storage/.config
cp "$DIR/pi-watch.sh" $CFG/torrbot-pi-watch.sh && chmod 755 $CFG/torrbot-pi-watch.sh
cp "$DIR/scraper-fix.py" $CFG/torrbot-scraper-fix.py
if [ -f "$DIR/secrets.env" ]; then
  cp "$DIR/secrets.env" $CFG/torrbot-pi.env && chmod 600 $CFG/torrbot-pi.env
  echo "Секреты: $CFG/torrbot-pi.env"
else
  echo "Нет $DIR/secrets.env — сторож будет работать без сообщений в Telegram"
fi
mkdir -p $CFG/system.d
cat > $CFG/system.d/torrbot-pi-watch.service <<'UNIT'
[Unit]
Description=torrbot: сторож малинки (сеть, Kodi)
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/sh /storage/.config/torrbot-pi-watch.sh
UNIT
cat > $CFG/system.d/torrbot-pi-watch.timer <<'UNIT'
[Unit]
Description=torrbot: сторож малинки раз в 2 минуты

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable torrbot-pi-watch.timer >/dev/null 2>&1
systemctl restart torrbot-pi-watch.timer
echo "Сторож установлен:"
systemctl list-timers torrbot-pi-watch.timer --no-pager | head -2
sh $CFG/torrbot-pi-watch.sh
sh $CFG/torrbot-pi-watch.sh --status
sh $CFG/torrbot-pi-watch.sh --test
