#!/bin/sh
# Сторож малинки (LibreELEC). Запускается systemd-таймером раз в 2 минуты (install-watch.sh).
#  * сеть: шлюз не пингуется 3 раза подряд (≈6 мин) — перезапуск сети;
#    6 раз подряд (≈12 мин) — перезагрузка (не чаще раза в час);
#  * Kodi: процесса нет 2 раза подряд — запуск; веб-управление не отвечает 3 раза подряд
#    (Kodi завис) — перезапуск Kodi;
#  * если в /storage/.config/torrbot-pi.env задан BOT_TOKEN — пишет админам в Telegram,
#    что починил (через прокси сервера).
# Ручной запуск:  sh /storage/.config/torrbot-pi-watch.sh --status | --test
ENV=/storage/.config/torrbot-pi.env
STATE=/storage/.cache/torrbot-pi-watch
LOG=/storage/.kodi/temp/torrbot-pi-watch.log
[ -f "$ENV" ] && . "$ENV"
mkdir -p "$STATE"
NOW=$(date +%s)

log() { echo "$(date '+%d.%m %H:%M:%S') $*" >> "$LOG"; tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; }
send() {
  [ -n "$BOT_TOKEN" ] && [ -n "$ADMIN_IDS" ] || return 0
  for a in $(echo "$ADMIN_IDS" | tr ',;' '  '); do
    curl -s -m 20 ${TG_PROXY:+-x "$TG_PROXY"} "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
      -d chat_id="$a" --data-urlencode text="🍓 Малинка: $1" >/dev/null 2>&1
  done
}
count() { c=$(( $(cat "$STATE/$1" 2>/dev/null || echo 0) + 1 )); echo $c > "$STATE/$1"; echo $c; }
reset() { rm -f "$STATE/$1"; }

case "$1" in
  --test) send "проверка связи сторожа" && echo "отправлено (если задан BOT_TOKEN)"; exit 0;;
  --status)
    for n in net kodi_proc kodi_web; do echo "$n: неудач подряд $(cat "$STATE/$n" 2>/dev/null || echo 0)"; done
    tail -n 15 "$LOG" 2>/dev/null; exit 0;;
esac

# после перезагрузки сторожем — сообщить, когда сеть уже есть
if [ -f "$STATE/rebooted" ] && ping -c1 -W3 "$(ip route | awk '/default/ {print $3; exit}')" >/dev/null 2>&1; then
  send "перезагрузилась из-за пропавшей сети — теперь всё в порядке." && rm -f "$STATE/rebooted"
fi

# 1. сеть
GW=$(ip route | awk '/default/ {print $3; exit}')
if [ -n "$GW" ] && ping -c1 -W3 "$GW" >/dev/null 2>&1; then
  [ -f "$STATE/net" ] && log "сеть снова есть" && send "сеть снова работает."
  reset net
else
  n=$(count net)
  log "шлюз ${GW:-?} не отвечает ($n)"
  if [ "$n" = 3 ]; then
    log "перезапускаю сеть"; systemctl restart connman
  elif [ "$n" -ge 6 ]; then
    last=$(cat "$STATE/reboot_at" 2>/dev/null || echo 0)
    if [ $((NOW - last)) -ge 3600 ]; then
      log "сети нет давно — перезагрузка"; echo "$NOW" > "$STATE/reboot_at"; touch "$STATE/rebooted"; reset net
      sync; reboot; exit 0
    fi
  fi
fi

# 2. Kodi
if pidof kodi.bin >/dev/null 2>&1 || pidof kodi.bin-gbm >/dev/null 2>&1 || pidof kodi-gbm >/dev/null 2>&1; then
  reset kodi_proc
  code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
         ${KODI_WEB_PASS:+-u "kodi:$KODI_WEB_PASS"} \
         -d '{"jsonrpc":"2.0","id":1,"method":"JSONRPC.Ping"}' http://127.0.0.1:8080/jsonrpc)
  case "$code" in
    200|401) reset kodi_web;;
    *) n=$(count kodi_web); log "Kodi не отвечает по HTTP (код ${code:-нет}, $n)"
       if [ "$n" -ge 3 ]; then log "Kodi завис — перезапуск"; reset kodi_web
         systemctl restart kodi; send "Kodi завис — перезапустил."; fi;;
  esac
else
  n=$(count kodi_proc); log "Kodi не запущен ($n)"
  if [ "$n" -ge 2 ]; then log "запускаю Kodi"; reset kodi_proc; systemctl start kodi; send "Kodi был выключен — запустил."; fi
fi
