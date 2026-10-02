# torrbot v7.1: установка, обновление, ИИ (Алиса, Groq)

## 1. Обновить бота (сервер)

PowerShell на ПК:

```powershell
scp $HOME\Desktop\Ai\torrbot-v7.1.zip lerroy@192.168.1.30:~/
```

На сервере (ssh lerroy@192.168.1.30):

```bash
sh ~/torrbot/deploy.sh ~/torrbot-v7.1.zip
sudo sh ~/torrbot/host/install.sh
cd ~/torrbot && docker compose logs --tail 40 bot | grep -E "версия|ИИ|Traceback|ERROR"
```

`install.sh` обновляет сторожа сервера, ставит smartmontools и агент нагрузки, присылает
тестовое сообщение и показывает один замер (`"disk": {"dev": "sdb", "util": …}`).

Проверка в Telegram: `/version` → 7.1; `/health` → строки «💽 Диск», «🌐 Сеть», «🩺 SMART»;
`/speed` → меню скорости; `/tv` → пульт.

Синхронизировать GitHub:

```bash
cd ~/torrbot && git add -A && git status --short | grep secrets   # должно быть пусто
git commit -m "torrbot v7.1" && git push
```

## 2. Малинка: скрипты и сторож

С сервера:

```bash
scp -r ~/torrbot/pi root@192.168.1.33:/storage/torrbot-pi
ssh root@192.168.1.33
nano /storage/torrbot-pi/secrets.env
```

В `secrets.env` логины трекеров уже заполнены. Допиши:
- `KODI_WEB_PASS` — тот же пароль, что `KODI_PASS` в `~/torrbot/.env`;
- для сообщений от сторожа: `BOT_TOKEN` и `ADMIN_IDS` (те же, что в `.env` бота).

Сохранить (Ctrl+O, Enter, Ctrl+X), затем:

```bash
sh /storage/torrbot-pi/install-watch.sh
```

Должно прийти «🍓 Малинка: проверка связи сторожа».

## 3. ИИ для поиска по описанию

На сервере `nano ~/torrbot/.env`, добавь в конец:

```
YANDEX_API_KEY=секретный_ключ_AQVN…
YANDEX_FOLDER_ID=b1ges9ij94m92ncm29ha
YANDEX_MODEL=aliceai-llm/latest
GROQ_API_KEY=ключ_gsk_…
```

Groq-ключ: console.groq.com → **API Keys** → **Create API Key** → любое имя → скопируй
`gsk_…` (показывается один раз). Нет ключа Groq — оставь строку пустой, бот его пропустит.

Перезапуск и проверка:

```bash
cd ~/torrbot && docker compose up -d --force-recreate bot
docker compose logs --tail 30 bot | grep "ИИ"     # «ИИ для поиска по описанию: Алиса ✅, Groq ✅»
```

В Telegram: `/plot фильм где мужик застревает в одном дне` → «🤖 По описанию (ИИ, Алиса)…»;
`/health` → строка «🤖 ИИ для /plot».

Порядок сервисов — `AI_ORDER=yandex,groq,gemini`. Чтобы экономить деньги Яндекса, поставь
Groq первым: `AI_ORDER=groq,yandex`. Кончился лимит у одного — бот берёт следующий.

## 4. Первые настройки

- `/users` → ⚙ у человека → 📺 пульт / 🧸 детский режим.
- `/speed` → 🌙 дневной режим (по умолчанию выключен). «Тормозить, пока смотрят» и
  «при перегрузке диска» включены сразу.
- Бот сам управляет «черепахой» Transmission: расписание в веб-интерфейсе Transmission
  больше не используется, часы задаются в `DAY_HOURS`.

## Откат

```bash
sh ~/torrbot/rollback.sh            # список снимков
sh ~/torrbot/rollback.sh v6.5.1-….tar.gz
sudo systemctl disable --now torrbot-load-agent   # если нужно выключить агент
```
