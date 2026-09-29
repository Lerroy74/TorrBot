#!/bin/sh
# Сторож сервера — вне Docker, пишет в Telegram НАПРЯМУЮ (трафик самого сервера идёт
# мимо туннеля), поэтому сообщит, даже когда туннель лёг или бот упал.
# Проверяет: туннель (xray → VPS) и запущен ли контейнер бота.
# Запускается systemd-таймером (см. install.sh). Ручной запуск:
#   sudo torrbot-host-watch          — одна проверка
#   sudo torrbot-host-watch --test   — тестовое сообщение в Telegram
#   sudo torrbot-host-watch --status — что сторож думает сейчас
ENV_FILE=${WATCH_ENV:-/home/lerroy/torrbot/.env}
STATE=${WATCH_STATE:-/var/lib/torrbot-watch}
URL=${WATCH_URL:-https://www.google.com/generate_204}
CONTAINER=${WATCH_CONTAINER:-torrbot}
FAILS_TO_ALERT=${WATCH_FAILS:-2}

getenv() {  # значение из .env без выполнения файла; кавычки по краям убираем
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -1 | tr -d '\r' \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}
TOKEN=$(getenv BOT_TOKEN)
ADMINS=$(getenv ADMIN_IDS | tr ',;' '  ')
[ -n "$TOKEN" ] && [ -n "$ADMINS" ] || { echo "нет BOT_TOKEN/ADMIN_IDS в $ENV_FILE"; exit 1; }
# прокси xray — тот же, что у бота (HEALTH_PROXY или TG_PROXY); socks5h — DNS тоже через туннель
PROXY=${WATCH_PROXY:-$(getenv HEALTH_PROXY)}
PROXY=${PROXY:-$(getenv TG_PROXY)}
PROXY=$(echo "${PROXY:-socks5://127.0.0.1:1080}" | sed 's#^socks5://#socks5h://#')
mkdir -p "$STATE"
NOW=$(date +%s)

send() {  # 0 — хотя бы одному админу доставлено
  ok=1
  for a in $ADMINS; do
    r=$(curl -s -m 20 "https://api.telegram.org/bot$TOKEN/sendMessage" \
          -d chat_id="$a" --data-urlencode text="$1")
    case "$r" in *'"ok":true'*) ok=0;; *) echo "не отправилось $a: $r" >&2;; esac
  done
  return $ok
}

mins() { m=$(( ($1 + 59) / 60 )); [ "$m" -lt 1 ] && m=1; [ "$m" -lt 60 ] && echo "$m мин" || echo "$((m / 60)) ч $((m % 60)) мин"; }

# итог проверки → оповещение только при смене состояния
#   state/<имя>.fails — неудач подряд, .since — время первой, .down — оповестили о падении
track() {  # имя, 0|1 (ок|нет), подпись, подробности при падении, [подробности при восстановлении]
  n=$1; f="$STATE/$n"
  if [ "$2" = 0 ]; then
    if [ -f "$f.down" ]; then
      since=$(cat "$f.since" 2>/dev/null || echo "$NOW")
      send "🟢 $3: снова работает (простой ~$(mins $((NOW - since)))). ${5:-}" && rm -f "$f.down"
    fi
    [ -f "$f.down" ] || rm -f "$f.fails" "$f.since"
    return
  fi
  c=$(( $(cat "$f.fails" 2>/dev/null || echo 0) + 1 )); echo "$c" > "$f.fails"
  [ -f "$f.since" ] || echo "$NOW" > "$f.since"
  if [ "$c" -ge "$FAILS_TO_ALERT" ] && [ ! -f "$f.down" ]; then
    send "🔴 $3: не работает. $4" && touch "$f.down"   # не доставилось — повторим в следующий раз
  fi
}

case "$1" in
  --test)
    send "🧪 Сторож сервера: связь с Telegram напрямую есть." && echo "отправлено" || exit 1; exit 0;;
  --status)
    for n in tunnel bot; do
      printf "%s: неудач подряд %s, оповещён о падении: %s\n" "$n" \
        "$(cat "$STATE/$n.fails" 2>/dev/null || echo 0)" "$([ -f "$STATE/$n.down" ] && echo да || echo нет)"
    done; exit 0;;
esac

# 1. туннель: запрос через xray к внешнему сайту
code=$(curl -s -m 20 -x "$PROXY" -o /dev/null -w '%{http_code}' "$URL")
case "$code" in 2??|3??) t=0;; *) t=1;; esac
track tunnel $t "🔐 Туннель VPN (xray → VPS)" "Проверка через $PROXY: ответ ${code:-нет}. VPN у пользователей, скорее всего, тоже не работает." \
  "Ответ $code."

# 2. бот: контейнер запущен и не перезапускается по кругу
st=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)
[ "$st" = running ] && b=0 || b=1
track bot $b "🤖 Бот torrbot" "Контейнер: ${st:-не найден}. Логи: cd ~/torrbot && docker compose logs --tail 50 bot"
