#!/bin/sh
# Установка программ сервера, которые работают вне Docker (один раз, и после обновления бота):
#   sudo sh ~/torrbot/host/install.sh
# 1. Сторож сервера (host-watch.sh): туннель и контейнер бота, раз в 3 минуты, пишет в Telegram напрямую.
# 2. Агент нагрузки (load-agent.py, v7): диск, сеть, просмотр по сети, SMART — каждые 5 секунд
#    в ~/torrbot/data/host-load.json; по этим данным бот тормозит закачки и предупреждает админа.
# Программы копируются в /usr/local/bin (откат бота rollback.sh их не заденет).
set -e
[ "$(id -u)" = 0 ] || { echo "Запусти через sudo: sudo sh $0"; exit 1; }
DIR=$(cd "$(dirname "$0")" && pwd)
APP=$(dirname "$DIR")
ENV_FILE=${WATCH_ENV:-$APP/.env}
[ -f "$ENV_FILE" ] || { echo "Не найден $ENV_FILE"; exit 1; }

# ---------- 1. сторож сервера ----------
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

# ---------- 2. агент нагрузки ----------
command -v python3 >/dev/null || { echo "Нужен python3: sudo apt install python3"; exit 1; }
if ! command -v smartctl >/dev/null; then
  echo "Ставлю smartmontools (SMART диска)…"
  apt-get install -y smartmontools >/dev/null 2>&1 || echo "  не получилось — SMART не будет, остальное работает"
fi
install -m 755 "$DIR/load-agent.py" /usr/local/bin/torrbot-load-agent
mkdir -p "$APP/data"
cat > /etc/systemd/system/torrbot-load-agent.service <<UNIT
[Unit]
Description=torrbot: агент нагрузки (диск, сеть, просмотр по сети, SMART)
After=local-fs.target network-online.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/torrbot-load-agent --env $ENV_FILE --out $APP/data/host-load.json
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now torrbot-host-watch.timer
systemctl enable torrbot-load-agent.service >/dev/null 2>&1
systemctl restart torrbot-load-agent.service
echo "Установлено. Настройки берутся из $ENV_FILE"
WATCH_ENV="$ENV_FILE" /usr/local/bin/torrbot-host-watch --test || true
systemctl list-timers torrbot-host-watch.timer --no-pager | head -3
echo "--- агент нагрузки: один замер ---"
/usr/bin/python3 /usr/local/bin/torrbot-load-agent --env "$ENV_FILE" --once
systemctl is-active torrbot-load-agent.service
