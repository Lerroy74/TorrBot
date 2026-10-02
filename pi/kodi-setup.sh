#!/bin/sh
# =====================================================================
#  Восстановление настроек Kodi (LibreELEC) на Raspberry Pi:
#  Elementum + Burst + JacRed + прокси + фильтры + постеры + раскладка
#  + медиатека с сервера (SMB «Фильмы»/«Сериалы», TMDB по-русски)
#  + управление по HTTP для torrbot + адреса TMDB (обход DNS-заглушки)
#  + Wi-Fi без энергосбережения
#
#  ПЕРЕД ЗАПУСКОМ (на свежей системе):
#   1. Установить LibreELEC, включить SSH.
#   2. В Kodi: Настройки > Система > Дополнения > Неизвестные источники: ВКЛ.
#   3. Установить из zip репозиторий Elementum (repository.elementumorg),
#      из него — Elementum и Elementum Burst.
#   4. Один раз открыть Elementum (пройти мастер) и Burst, затем выйти.
#   5. Проверить, что lerroy-server с xray включён.
#
#  ЛОГИНЫ ТРЕКЕРОВ — в secrets.env рядом со скриптом (образец secrets.env.example).
#
#  ЗАПУСК:  sh kodi-setup.sh                  — спросит адрес прокси
#           sh kodi-setup.sh 192.168.1.30 1080 — без вопросов (пароль Kodi не меняет)
#  Медиа-сервер (SMB) по умолчанию тот же, что прокси; другой — MEDIA_HOST=... sh kodi-setup.sh
#  Скрипт можно запускать повторно — он просто перезапишет настройки.
# =====================================================================

# ---------- адрес прокси (xray SOCKS5) ----------
DEF_HOST=192.168.1.30
DEF_PORT=1080
PROXY_HOST=$1
PROXY_PORT=$2
if [ -z "$PROXY_HOST" ]; then
  printf "Адрес прокси-сервера [%s]: " "$DEF_HOST"; read PROXY_HOST
  PROXY_HOST=${PROXY_HOST:-$DEF_HOST}
fi
if [ -z "$PROXY_PORT" ]; then
  printf "Порт прокси [%s]: " "$DEF_PORT"; read PROXY_PORT
  PROXY_PORT=${PROXY_PORT:-$DEF_PORT}
fi
case "$PROXY_PORT" in ''|*[!0-9]*) echo "Порт должен быть числом: $PROXY_PORT"; exit 1;; esac
echo "Прокси: $PROXY_HOST:$PROXY_PORT"
MEDIA_HOST=${MEDIA_HOST:-$PROXY_HOST}
echo "Медиа-сервер (SMB): $MEDIA_HOST"

# ---------- логины трекеров: из secrets.env (в git не попадает) ----------
# Ищется рядом со скриптом (pi/secrets.env), потом /storage/.config/torrbot-pi.env.
# Образец — pi/secrets.env.example. Нет файла — трекеры с логином просто не настроятся.
SECRETS=${SECRETS:-$(dirname "$0")/secrets.env}
[ -f "$SECRETS" ] || SECRETS=/storage/.config/torrbot-pi.env
if [ -f "$SECRETS" ]; then . "$SECRETS"; echo "Логины трекеров: $SECRETS"
else echo "  [!!] нет secrets.env — трекеры с логином (nnmclub, rustorka, lostfilm, rutracker) пропущу"; fi
TR_PASS=${TR_PASS:-}
NNM_USER=${NNM_USER:-}
RUSTORKA_USER=${RUSTORKA_USER:-}
LOSTFILM_USER=${LOSTFILM_USER:-}
RUTRACKER_USER=${RUTRACKER_USER:-}
KODI_WEB_PASS=${KODI_WEB_PASS:-}

# пароль веб-управления Kodi (им пользуется torrbot: KODI_PASS в его .env)
KODI_WEB_USER=kodi
if [ -z "$KODI_WEB_PASS" ] && [ -z "$1" ]; then
  printf "Пароль веб-управления Kodi (Enter — не менять): "; read KODI_WEB_PASS
fi

MAX_MOVIE_GB=20
MAX_EPISODE_GB=5
MAX_SEASON_GB=80
BLOCK_WORDS='HEVC,x265,H.265,H265,10bit,10-bit,2160p,UHD,HDR10,Dolby Vision'
# --------------------------------

K=/storage/.kodi
AD=$K/userdata/addon_data
E=$AD/plugin.video.elementum/settings.xml
B=$AD/script.elementum.burst/settings.xml
G=$K/userdata/guisettings.xml
DL=/storage/downloads/elementum
CFG=/storage/.config
SMB_MOVIES="smb://$MEDIA_HOST/media/movies/"
SMB_SERIES="smb://$MEDIA_HOST/media/series/"

ok(){ echo "  [ok] $*"; }
warn(){ echo "  [!!] $*"; }
die(){ echo "  [XX] $*"; exit 1; }

setopt(){ f=$1; id=$2; v=$3
  if grep -q "id=\"$id\"" "$f"; then
    sed -i -e "s|<setting id=\"$id\"[^>]*/>|<setting id=\"$id\">$v</setting>|" \
           -e "s|<setting id=\"$id\"[^>]*>[^<]*</setting>|<setting id=\"$id\">$v</setting>|" "$f"
  else
    sed -i "s|</settings>|    <setting id=\"$id\">$v</setting>\n</settings>|" "$f"
  fi; }

ensure_file(){ [ -f "$1" ] || { mkdir -p "$(dirname "$1")"; printf '<settings version="2">\n</settings>\n' > "$1"; }; }

echo "== 1. Проверка"
for a in plugin.video.elementum script.elementum.burst; do
  [ -d $K/addons/$a ] && ok "$a установлен" || die "$a не установлен — см. шапку скрипта"
done
[ -f "$G" ] || die "нет guisettings.xml — Kodi ещё ни разу не запускался"
if nc -z -w3 $PROXY_HOST $PROXY_PORT 2>/dev/null; then ok "прокси $PROXY_HOST:$PROXY_PORT доступен"
else
  warn "прокси $PROXY_HOST:$PROXY_PORT недоступен (сервер выключен или адрес неверный)"
  printf "  Всё равно применить настройки? [y/N]: "; read ans
  case "$ans" in y|Y|д|Д) warn "продолжаю — поиск заработает, когда прокси станет доступен";; *) die "отменено";; esac
fi

echo "== 2. Остановка Kodi и бэкап"
systemctl stop kodi
ensure_file "$E"; ensure_file "$B"
TS=$(date +%Y%m%d-%H%M%S)
for f in "$E" "$B" "$G"; do cp "$f" "$f.bak-$TS"; done
ok "бэкапы *.bak-$TS"

echo "== 3. Elementum"
mkdir -p $DL
setopt $E download_storage 0
setopt $E download_path "$DL/"
setopt $E proxy_enabled true
setopt $E proxy_type 1
setopt $E proxy_host $PROXY_HOST
setopt $E proxy_port $PROXY_PORT
setopt $E use_proxy_http true
setopt $E use_proxy_tracker false
setopt $E use_proxy_download false
ok "хранение на диске, прокси только для HTTP"

echo "== 4. Burst: прокси и трекеры"
setopt $B use_elementum_proxy false
setopt $B proxy_enabled true
setopt $B proxy_use_type 1
setopt $B proxy_type 5
setopt $B proxy_host $PROXY_HOST
setopt $B proxy_port $PROXY_PORT
setopt $B use_opennic_dns false
for p in rutor rutracker thepiratebay megapeer piratbit rustorka 1337x bitsearch nnmclub lostfilm; do
  setopt $B use_$p true
done
setopt $B use_knaben false
if [ -n "$TR_PASS" ]; then
  [ -n "$NNM_USER" ]       && { setopt $B nnmclub_username   "$NNM_USER";       setopt $B nnmclub_password   "$TR_PASS"; }
  [ -n "$RUSTORKA_USER" ]  && { setopt $B rustorka_username  "$RUSTORKA_USER";  setopt $B rustorka_password  "$TR_PASS"; }
  [ -n "$LOSTFILM_USER" ]  && { setopt $B lostfilm_username  "$LOSTFILM_USER";  setopt $B lostfilm_password  "$TR_PASS"; }
  [ -n "$RUTRACKER_USER" ] && { setopt $B rutracker_username "$RUTRACKER_USER"; setopt $B rutracker_password "$TR_PASS"; }
  ok "SOCKS5h, трекеры и логины"
else warn "логины трекеров не заданы (secrets.env) — только открытые трекеры"; fi
setopt $B max_results 25

echo "== 5. Burst: фильтры под Pi 3"
setopt $B filter_4k false
setopt $B filter_2k false
setopt $B separate_sizes true
setopt $B max_size_movies $MAX_MOVIE_GB
setopt $B max_size_episodes $MAX_EPISODE_GB
setopt $B max_size_seasons $MAX_SEASON_GB
setopt $B additional_filters true
setopt $B block "$BLOCK_WORDS"
ok "без 4K/HEVC, фильм до ${MAX_MOVIE_GB} ГБ"

echo "== 6. Провайдер JacRed"
mkdir -p $AD/script.elementum.burst/providers
python3 - <<'EOF' && ok "jacred.json записан" || warn "не удалось создать jacred.json"
import json
BD='/storage/.kodi/addons/script.elementum.burst'
d=json.load(open(BD+'/burst/providers/providers.json'))['yts']
d.update({
 "name": "JacRed", "color": "FFE0A030",
 "language": "ru", "languages": "ru,en",
 "base_url": "https://jac.red/api/v2.0/indexers/all/results?apikey=null&Query=QUERY",
 "api_format": {"results": "Results", "name": "Title", "torrent": "MagnetUri",
                "size": "Size", "seeds": "Seeders", "peers": "Peers"},
 "general_keywords": "{title}", "movie_keywords": "{title:ru}",
 "tv_keywords": "{title:ru}", "season_keywords": "{title:ru}", "anime_keywords": "{title:ru}",
 "predefined": False, "private": False, "enabled": True})
json.dump({"jacred": d},
  open('/storage/.kodi/userdata/addon_data/script.elementum.burst/providers/jacred.json','w'),
  ensure_ascii=False, indent=1)
EOF

echo "== 7. Kodi: прокси для постеров и русская раскладка"
setopt $G network.usehttpproxy true
setopt $G network.httpproxytype 4
setopt $G network.httpproxyserver $PROXY_HOST
setopt $G network.httpproxyport $PROXY_PORT
L=/usr/share/kodi/system/keyboardlayouts
EN=$(grep -h -o 'language="English" layout="QWERTY"' $L/*.xml | head -1 | sed 's/language="\(.*\)" layout="\(.*\)"/\1 \2/')
RU=$(grep -h -o 'language="Russian" layout="[^"]*"' $L/*.xml | head -1 | sed 's/language="\(.*\)" layout="\(.*\)"/\1 \2/')
if [ -n "$EN" ] && [ -n "$RU" ]; then
  if grep -q 'id="locale.keyboardlayouts"' $G; then
    sed -i "s#<setting id=\"locale.keyboardlayouts\"[^>]*>[^<]*</setting>#<setting id=\"locale.keyboardlayouts\">$EN|$RU</setting>#" $G
  else
    sed -i "s#</settings>#    <setting id=\"locale.keyboardlayouts\">$EN|$RU</setting>\n</settings>#" $G
  fi
  ok "раскладки: $EN / $RU"
else warn "раскладки не найдены, пропущено"; fi

echo "== 7a. Kodi: управление по HTTP (для torrbot)"
setopt $G services.webserver true
setopt $G services.webserverport 8080
setopt $G services.webserverauthentication true
setopt $G services.webserverusername $KODI_WEB_USER
if [ -n "$KODI_WEB_PASS" ]; then
  setopt $G services.webserverpassword "$KODI_WEB_PASS"; ok "http://<малинка>:8080, пароль обновлён"
else ok "http://<малинка>:8080, пароль не менялся"; fi
setopt $G videolibrary.updateonstartup true

echo "== 7b. Медиатека: источники «Фильмы» и «Сериалы» с сервера"
for sc in metadata.themoviedb.org.python metadata.tvshows.themoviedb.org.python; do
  f=$AD/$sc/settings.xml; ensure_file "$f"; setopt "$f" language ru-RU
done
SMB_MOVIES="$SMB_MOVIES" SMB_SERIES="$SMB_SERIES" python3 - <<'EOF' && ok "источники и тип содержимого заданы" || warn "не удалось задать источники"
import glob, os, re, sqlite3
import xml.etree.ElementTree as ET
SRC = [("Фильмы",  os.environ["SMB_MOVIES"], "movies",  "metadata.themoviedb.org.python", 2147483647),
       ("Сериалы", os.environ["SMB_SERIES"], "tvshows", "metadata.tvshows.themoviedb.org.python", 0)]
# sources.xml — добавляем, чего нет; чужие источники не трогаем
p = "/storage/.kodi/userdata/sources.xml"
if os.path.exists(p):
    tree = ET.parse(p); root = tree.getroot()
else:
    root = ET.Element("sources"); tree = ET.ElementTree(root)
video = root.find("video")
if video is None:
    video = ET.SubElement(root, "video")
if video.find("default") is None:
    ET.SubElement(video, "default").set("pathversion", "1")
have = {(s.findtext("path") or "").strip() for s in video.findall("source")}
for name, path, *_ in SRC:
    if path in have:
        continue
    s = ET.SubElement(video, "source")
    ET.SubElement(s, "name").text = name
    e = ET.SubElement(s, "path"); e.set("pathversion", "1"); e.text = path
    ET.SubElement(s, "allowsharing").text = "true"
tree.write(p, encoding="utf-8", xml_declaration=True)
# тип содержимого и скрейпер — в базе видео (как «Сменить содержимое» в меню)
dbs = glob.glob("/storage/.kodi/userdata/Database/MyVideos*.db")
if not dbs:
    print("  база видео ещё не создана — тип содержимого задайте в Kodi вручную"); raise SystemExit(0)
db = max(dbs, key=lambda f: int(re.sub(r"\D", "", os.path.basename(f)) or 0))
c = sqlite3.connect(db)
for name, path, content, scraper, recursive in SRC:
    row = c.execute("SELECT idPath FROM path WHERE strPath=?", (path,)).fetchone()
    if row:
        c.execute("UPDATE path SET strContent=?, strScraper=?, scanRecursive=?, useFolderNames=0,"
                  " noUpdate=0, exclude=0 WHERE idPath=?", (content, scraper, recursive, row[0]))
    else:
        c.execute("INSERT INTO path(strPath, strContent, strScraper, scanRecursive, useFolderNames,"
                  " noUpdate, exclude) VALUES (?,?,?,?,0,0,0)", (path, content, scraper, recursive))
c.commit()
EOF

echo "== 7c. Адреса TMDB (DNS роутера отдаёт на TMDB заглушку 127.0.0.1)"
cat > $CFG/tmdb-hosts.sh <<EOF
#!/bin/sh
# Настоящие адреса TMDB для скрейпера Kodi. Узнаёт их через DNS-over-HTTPS
# через прокси на сервере и пишет в hosts. Запускается при загрузке и раз в неделю.
PROXY=socks5h://$PROXY_HOST:$PROXY_PORT
EOF
cat >> $CFG/tmdb-hosts.sh <<'EOF'
CONF=/storage/.config/hosts.conf
TMP=$(mktemp)
for h in api.themoviedb.org image.tmdb.org; do
  curl -s -m 20 -x $PROXY "https://dns.google/resolve?name=$h&type=A"     | grep -oE '"data":"[0-9.]+"' | cut -d'"' -f4 | sed "s/$/ $h/"
done > $TMP
if ! grep -q "api.themoviedb.org" $TMP; then
  echo "tmdb-hosts: адреса не получены (прокси недоступен?) — оставляю старые"; rm -f $TMP; exit 1
fi
update() {
  f=$1; [ -w "$f" ] || return 0
  sed -i -e '/# tmdb-hosts begin/,/# tmdb-hosts end/d' -e '/ api\.themoviedb\.org$/d' -e '/ image\.tmdb\.org$/d' "$f"
  { echo "# tmdb-hosts begin"; cat $TMP; echo "# tmdb-hosts end"; } >> "$f"
}
touch $CONF; update $CONF
H=$(readlink -f /etc/hosts); [ "$H" != "$CONF" ] && update "$H"
echo "tmdb-hosts: записано адресов: $(grep -c . $TMP)"; rm -f $TMP
EOF
chmod +x $CFG/tmdb-hosts.sh
mkdir -p $CFG/system.d
cat > $CFG/system.d/tmdb-hosts.service <<'EOF'
[Unit]
Description=Update TMDB addresses in hosts
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/sh /storage/.config/tmdb-hosts.sh
EOF
cat > $CFG/system.d/tmdb-hosts.timer <<'EOF'
[Unit]
Description=Update TMDB addresses weekly

[Timer]
OnBootSec=2min
OnUnitActiveSec=7d

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable tmdb-hosts.timer >/dev/null 2>&1 && systemctl start tmdb-hosts.timer
if sh $CFG/tmdb-hosts.sh; then ok "адреса TMDB записаны, обновление раз в неделю"
else warn "адреса TMDB не получены — скрейпер не заработает, пока прокси недоступен"; fi

echo "== 7d. Wi-Fi без энергосбережения (иначе Pi 3 теряет связь)"
touch $CFG/autostart.sh
grep -q power_save $CFG/autostart.sh || echo '(sleep 30; iw wlan0 set power_save off) &' >> $CFG/autostart.sh
iw wlan0 set power_save off 2>/dev/null && ok "power_save off (и в autostart.sh)" || ok "Wi-Fi не используется, пропущено"

echo "== 8. setopt в /storage/.profile"
if ! grep -q "setopt()" /storage/.profile 2>/dev/null; then
cat >> /storage/.profile <<'EOF'
K=/storage/.kodi
E=$K/userdata/addon_data/plugin.video.elementum/settings.xml
B=$K/userdata/addon_data/script.elementum.burst/settings.xml
setopt(){ f=$1; id=$2; v=$3
  if grep -q "id=\"$id\"" $f; then
    sed -i -e "s|<setting id=\"$id\"[^>]*/>|<setting id=\"$id\">$v</setting>|" \
           -e "s|<setting id=\"$id\"[^>]*>[^<]*</setting>|<setting id=\"$id\">$v</setting>|" $f
  else
    sed -i "s|</settings>|    <setting id=\"$id\">$v</setting>\n</settings>|" $f
  fi; }
EOF
ok "добавлено"; else ok "уже есть"; fi

echo "== 9. Запуск и тест поиска"
systemctl start kodi
n=0; until [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:65220/)" != "000" ]; do
  sleep 2; n=$((n+1)); [ $n -gt 60 ] && { warn "Elementum не поднялся за 2 минуты"; exit 1; }; done
ok "Elementum запущен"
curl -s -m 60 "http://127.0.0.1:65220/search?q=matrix" >/dev/null
grep -E "returned .* results|Login failed|Providers returned" $K/temp/kodi.log | tail -15

echo "== 10. Проверка медиатеки"
if [ -n "$KODI_WEB_PASS" ]; then
  r=$(curl -s -m 10 -u "$KODI_WEB_USER:$KODI_WEB_PASS" -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"VideoLibrary.Scan","params":{"showdialogs":false}}' \
      http://127.0.0.1:8080/jsonrpc)
  case "$r" in *'"OK"'*) ok "запущено обновление медиатеки";; *) warn "веб-управление не ответило: $r";; esac
else ok "медиатека обновится при запуске Kodi (videolibrary.updateonstartup)"; fi
python3 -c "import socket;print('  api.themoviedb.org ->', socket.gethostbyname('api.themoviedb.org'))"
echo "== Готово"
