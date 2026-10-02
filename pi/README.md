# Скрипты малинки (LibreELEC / Kodi)

| Файл | Что делает |
|---|---|
| `kodi-setup.sh` | Восстанавливает все настройки Kodi на свежей LibreELEC: Elementum + Burst + JacRed, прокси, фильтры под Pi 3, источники «Фильмы»/«Сериалы» с сервера, веб-управление для бота, адреса TMDB, обновление медиатеки при запуске. Можно запускать повторно. |
| `pi-watch.sh` | Сторож: пропала сеть — перезапускает сеть, потом перезагружает малинку (не чаще раза в час); Kodi упал или завис — перезапускает. Дописывает скрейперам TMDB тайм-аут; зависшее обновление медиатеки (> 45 мин) — перезапуск Kodi. Может писать админу в Telegram. |
| `install-watch.sh` | Ставит сторожа (systemd-таймер раз в 2 минуты). |
| `secrets.env.example` | Образец секретов: логины трекеров, пароль Kodi, токен для сообщений. |
| `secrets.env` | Твои секреты. **В git не попадает** (`.gitignore`). |

## Установка / обновление (с ПК на Windows, PowerShell)

```powershell
scp -r $HOME\Desktop\Ai\torrbot\pi root@192.168.1.33:/storage/torrbot-pi
ssh root@192.168.1.33 "sh /storage/torrbot-pi/install-watch.sh"
```

Или с сервера: `scp -r ~/torrbot/pi root@192.168.1.33:/storage/torrbot-pi`.

## Полная переустановка Kodi (свежая LibreELEC)

См. шапку `kodi-setup.sh`: поставить Elementum и Burst, один раз их открыть, потом

```sh
sh /storage/torrbot-pi/kodi-setup.sh 192.168.1.30 1080
sh /storage/torrbot-pi/install-watch.sh
```

## Сторож: посмотреть, что он делал

```sh
sh /storage/.config/torrbot-pi-watch.sh --status
```
