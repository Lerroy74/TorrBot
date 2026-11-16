#!/bin/sh
# Сторож сервера — вне Docker, пишет в Telegram сам, без бота: сообщит, когда бот упал.
# Проверяет: туннель (xray → VPS) и запущен ли контейнер бота.
# v8.3.3: при обрыве туннеля выясняет причину (роутер / интернет / VPS / xray), пишет её в
# сообщение и в журнал $STATE/history.log; если VPS напрямую отвечает, а xray не пропускает —
# один раз за обрыв перезапускает xray (WATCH_RESTART_XRAY=0 в .env — не перезапускать).
# Если Telegram без туннеля недоступен (или роутер сам заворачивает его в туннель), «🔴» во время
# обрыва не дойдёт — тогда после восстановления придёт «🟡 был обрыв» с временем и причиной.
# Запускается systemd-таймером (см. install.sh). Ручной запуск:
#   sudo torrbot-host-watch          — одна проверка
#   sudo torrbot-host-watch --test   — тестовое сообщение в Telegram
#   sudo torrbot-host-watch --status — что сторож думает сейчас
#   sudo torrbot-host-watch --why    — проверить связь сейчас: роутер, интернет, VPS
#   sudo torrbot-host-watch --log    — последние записи журнала обрывов
ENV_FILE=${WATCH_ENV:-/home/lerroy/torrbot/.env}
STATE=${WATCH_STATE:-/var/lib/torrbot-watch}
URL=${WATCH_URL:-https://www.google.com/generate_204}
CONTAINER=${WATCH_CONTAINER:-torrbot}
FAILS_TO_ALERT=${WATCH_FAILS:-2}
XRAY_CONF=${WATCH_XRAY_CONF:-/usr/local/etc/xray/config.json}
RU_URL=${WATCH_RU_URL:-https://ya.ru/}          # сайт, который открывается без туннеля

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
RESTART_XRAY=${WATCH_RESTART_XRAY:-$(getenv WATCH_RESTART_XRAY)}; RESTART_XRAY=${RESTART_XRAY:-1}
ZONE=$(getenv TZ); [ -n "$ZONE" ] && export TZ="$ZONE"     # время в сообщениях — как у бота
mkdir -p "$STATE"
LOG="$STATE/history.log"
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

hm() { date -d "@$1" '+%d.%m %H:%M' 2>/dev/null || echo "?"; }
note() { echo "$(date '+%F %T') $*" >> "$LOG"; }

http() { curl -s -k -m 8 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }   # любой ответ сервера = связь есть
alive() { case "$1" in ""|000) return 1;; *) return 0;; esac; }

# почему не работает туннель: печатает «<вид> <пояснение>», вид: router | inet | vps | xray | unknown
diagnose() {
  gw=${WATCH_GW:-$(ip route show default 2>/dev/null | awk '{print $3; exit}')}
  vps=${WATCH_VPS:-$(sed -n 's/.*"address"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$XRAY_CONF" 2>/dev/null | head -1)}
  if [ -n "$gw" ] && ! ping -c 2 -W 2 -q "$gw" >/dev/null 2>&1; then
    echo "router роутер ($gw) не отвечает."; return; fi
  alive "$(http "$RU_URL")" || {
    echo "inet интернета нет совсем: без туннеля не открывается и $RU_URL (провайдер или роутер)."; return; }
  [ -n "$vps" ] || { echo "unknown интернет есть; адрес VPS в $XRAY_CONF не нашёл."; return; }
  alive "$(http "https://$vps/")" || {
    echo "vps интернет есть, а VPS ($vps) напрямую не отвечает — сам VPS или путь до него у провайдера."; return; }
  echo "xray интернет есть, VPS ($vps) напрямую отвечает, а через xray запросы не идут."
}

# итог проверки → оповещение только при смене состояния
#   state/<имя>.fails — неудач подряд, .since — время первой, .down — оповестили о падении
track() {  # имя, 0|1 (ок|нет), подпись, подробности при падении, [подробности при восстановлении]
  n=$1; f="$STATE/$n"
  if [ "$2" = 0 ]; then
    since=$(cat "$f.since" 2>/dev/null || echo "$NOW")
    if [ -f "$f.down" ]; then
      send "🟢 $3: снова работает (простой ~$(mins $((NOW - since)))). ${5:-}" && rm -f "$f.down"
    elif [ "$(cat "$f.fails" 2>/dev/null || echo 0)" -ge "$FAILS_TO_ALERT" ]; then
      # тревога так и не дошла (Telegram был недоступен) — расскажем задним числом, один раз
      send "🟡 $3: был обрыв с $(hm "$since"), ~$(mins $((NOW - since))); сообщить вовремя не получилось. Сейчас работает. ${5:-}"
    fi
    if [ ! -f "$f.down" ]; then
      [ -f "$f.fails" ] && note "$n ok после $(cat "$f.fails") неудач, простой ~$(mins $((NOW - since)))"
      rm -f "$f.fails" "$f.since" "$f.why" "$f.restarted"
    fi
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
    done
    [ -f "$STATE/tunnel.why" ] && echo "причина обрыва туннеля: $(cat "$STATE/tunnel.why")"
    exit 0;;
  --why)
    w=$(diagnose); echo "без туннеля: ${w#* }"
    c=$(curl -s -m 20 -x "$PROXY" -o /dev/null -w '%{http_code}' "$URL"); echo "через туннель ($PROXY): ответ ${c:-нет}"
    exit 0;;
  --log)
    tail -n "${2:-40}" "$LOG" 2>/dev/null || echo "журнал пуст"; exit 0;;
esac

# 1. туннель: запрос через xray к внешнему сайту
code=$(curl -s -m 20 -x "$PROXY" -o /dev/null -w '%{http_code}' "$URL")
case "$code" in 2??|3??) t=0;; *) t=1;; esac
why=""
if [ "$t" = 1 ]; then
  d=$(diagnose); kind=${d%% *}; why=${d#* }
  c=$(( $(cat "$STATE/tunnel.fails" 2>/dev/null || echo 0) + 1 ))
  note "tunnel неудача $c, ответ ${code:-нет}: $why"
  if [ "$c" -ge "$FAILS_TO_ALERT" ]; then
    [ -f "$STATE/tunnel.why" ] || echo "$why" > "$STATE/tunnel.why"       # причина на момент тревоги
    if [ "$kind" = xray ] && [ "$RESTART_XRAY" = 1 ] && [ ! -f "$STATE/tunnel.restarted" ]; then
      touch "$STATE/tunnel.restarted"
      if systemctl restart xray 2>/dev/null; then why="$why Перезапустил xray."; note "tunnel: перезапустил xray"
      else why="$why Перезапустить xray не получилось."; note "tunnel: xray не перезапустился"; fi
      echo "$why" > "$STATE/tunnel.why"
    fi
  fi
elif [ -f "$STATE/tunnel.why" ]; then
  why="Причина обрыва: $(cat "$STATE/tunnel.why")"
fi
track tunnel $t "🔐 Туннель VPN (xray → VPS)" "Проверка через $PROXY: ответ ${code:-нет}. $why" \
  "Ответ $code. $why"

# 2. бот: контейнер запущен и не перезапускается по кругу
st=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)
[ "$st" = running ] && b=0 || b=1
track bot $b "🤖 Бот torrbot" "Контейнер: ${st:-не найден}. Логи: cd ~/torrbot && docker compose logs --tail 50 bot"
