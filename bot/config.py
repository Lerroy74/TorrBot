"""Все настройки берутся из переменных окружения (файл .env)."""
import os
from dataclasses import dataclass


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in _str(name, default).split(",") if x.strip()]


def _ints(name: str) -> list[int]:
    return [int(x) for x in _list(name)]


def _bool(name: str, default: bool) -> bool:
    v = _str(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on", "да")


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: tuple[int, ...]
    allowed_ids: tuple[int, ...]
    tg_proxy: str | None

    jacred_url: str
    jacred_proxy: str | None

    tmdb_key: str | None
    tmdb_lang: str
    tmdb_proxy: str | None

    tr_url: str
    tr_user: str | None
    tr_pass: str | None
    dir_movies: str
    dir_series: str
    media_root: str

    kodi_url: str | None
    kodi_user: str | None
    kodi_pass: str | None
    kodi_media_url: str
    cleanup_days: int
    cleanup_warn_hours: int
    cleanup_interval_hours: int
    rate_watch_minutes: int

    queue_size: int
    health_interval: int
    disk_warn_gb: float
    kodi_offline_min: int
    health_url: str
    health_proxy: str | None
    weekly_day: int
    weekly_hour: int
    backup_dir: str
    backup_local_dir: str
    backup_keep: int
    backup_hour: int

    max_height: int
    max_size_gb: float
    max_size_series_gb: float
    min_seeders: int
    block_codecs: tuple[str, ...]
    block_hdr: bool
    block_words: tuple[str, ...]

    page_size: int
    max_results: int
    db_path: str
    poll_interval: int
    notify_adds: bool = True       # сообщать админам, когда пользователь ставит закачку
    notify_deletes: bool = True    # сообщать админам, когда пользователь удаляет через /delete

    # v7
    kodi_retry_min: int = 5            # Kodi не ответил на «обнови медиатеку» — повтор через N мин
    kodi_notify: bool = True           # всплывающее сообщение на ТВ, когда закачка готова
    gemini_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    gemini_proxy: str | None = None
    # v7.1: цепочка ИИ (по порядку; кончился лимит — следующий)
    ai_order: tuple[str, ...] = ("yandex", "groq", "gemini")
    yandex_key: str | None = None
    yandex_folder: str | None = None
    yandex_model: str = "aliceai-llm/latest"
    yandex_url: str = "https://ai.api.cloud.yandex.net/v1"
    groq_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_proxy: str | None = None
    subs_check_hours: float = 6        # как часто проверять подписки на сериалы
    subs_fallback_days: int = 7        # серия вышла N дней назад, а раздача не обновилась — предложить другие
    wait_check_hours: float = 12       # «⏳ ждать хорошее качество»: как часто искать
    wait_min_height: int = 1080
    wait_days: int = 180               # сколько ждать, потом забыть
    kids_max_age: int = 12             # детский режим: возрастной рейтинг не выше
    disk_reserve_gb: float = 5         # запас места, который не занимаем закачками
    stall_hours: float = 3             # закачка без движения N часов — предложить другие раздачи
    turtle_down_mb: float = 5          # «черепаха»: МБ/с на закачку
    turtle_up_mb: float = 1            # «черепаха»: МБ/с на раздачу
    day_hours: str = "08:00-23:00"     # дневной режим: когда притормаживать
    load_file: str = "/data/host-load.json"   # что пишет агент нагрузки на сервере
    load_util: int = 90                # диск занят на столько % и больше — перегрузка
    load_alert_sec: int = 120          # …столько секунд подряд
    load_throttle_min: int = 15        # притормозили из-за перегрузки — не меньше N минут
    load_cooldown_min: int = 30        # повтор тревоги о перегрузке не чаще
    net_limit_mbit: int = 300          # тариф: канал считаем забитым от 90% этого
    # v8: контроль ИИ (стартовые значения; дальше меняются в /ai)
    ai_user_day: int = 10              # ИИ-запросов в день на человека (0 — без лимита)
    ai_total_day: int = 50             # ИИ-запросов в день на всех (0 — без лимита)
    ai_month_rub: float = 0            # месячный потолок, ₽ (0 — выкл)
    ai_prices: tuple = ()              # ((сервис, ₽ за 1000 токенов), …); не задано — сумму не считаем
    # v8.2
    itogi_day: int = 30                # «Итоги года»: рассылка такого-то декабря (0 — не рассылать)
    itogi_hour: int = 19               # …в этот час
    stt_max_sec: int = 30              # голосовые длиннее — не распознаём
    stt_price: float = 0.1626          # ₽ за 15 секунд распознавания (прайс Яндекса 2026, с НДС)
    listwatch_hours: float = 12        # как часто проверять раздачи для фильмов из списков
    update_dir: str = "/data/update"   # куда бот кладёт архив для обновления (видит программа на сервере)


def load() -> Config:
    token = _str("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не задан в .env")
    admins = tuple(_ints("ADMIN_IDS"))
    if not admins:
        raise SystemExit("ADMIN_IDS не задан в .env (узнать свой ID: @userinfobot)")
    return Config(
        bot_token=token,
        admin_ids=admins,
        allowed_ids=tuple(_ints("ALLOWED_IDS")),
        tg_proxy=_str("TG_PROXY") or None,
        jacred_url=_str("JACRED_URL", "https://jac.red").rstrip("/"),
        jacred_proxy=_str("JACRED_PROXY") or None,
        tmdb_key=_str("TMDB_API_KEY") or None,
        tmdb_lang=_str("TMDB_LANG", "ru-RU"),
        tmdb_proxy=_str("TMDB_PROXY") or None,
        tr_url=_str("TRANSMISSION_URL", "http://host.docker.internal:9091/transmission/rpc"),
        tr_user=_str("TRANSMISSION_USER") or None,
        tr_pass=_str("TRANSMISSION_PASS") or None,
        dir_movies=_str("DIR_MOVIES", "/downloads/movies"),
        dir_series=_str("DIR_SERIES", "/downloads/series"),
        media_root=_str("MEDIA_ROOT", "/downloads"),
        kodi_url=_str("KODI_URL") or None,
        kodi_user=_str("KODI_USER") or None,
        kodi_pass=_str("KODI_PASS") or None,
        kodi_media_url=_str("KODI_MEDIA_URL", "smb://192.168.1.30/media"),
        cleanup_days=int(_str("CLEANUP_DAYS", "0")),
        cleanup_warn_hours=int(_str("CLEANUP_WARN_HOURS", "24")),
        cleanup_interval_hours=int(_str("CLEANUP_INTERVAL_HOURS", "6")),
        rate_watch_minutes=int(_str("RATE_WATCH_MINUTES", "30")),
        queue_size=int(_str("QUEUE_SIZE", "3")),
        health_interval=int(_str("HEALTH_INTERVAL", "300")),
        disk_warn_gb=float(_str("DISK_WARN_GB", "50")),
        kodi_offline_min=int(_str("KODI_OFFLINE_MIN", "30")),
        health_url=_str("HEALTH_URL", "https://www.google.com/generate_204"),
        notify_adds=_bool("NOTIFY_ADMIN_ON_ADD", True),
        notify_deletes=_bool("NOTIFY_ADMIN_ON_DELETE", True),
        health_proxy=_str("HEALTH_PROXY") or _str("TG_PROXY") or None,
        weekly_day=int(_str("WEEKLY_DAY", "6")),
        weekly_hour=int(_str("WEEKLY_HOUR", "10")),
        backup_dir=_str("BACKUP_DIR", "/backup"),
        backup_local_dir=_str("BACKUP_LOCAL_DIR", "/data/backups"),
        backup_keep=int(_str("BACKUP_KEEP", "14")),
        backup_hour=int(_str("BACKUP_HOUR", "4")),
        max_height=int(_str("MAX_HEIGHT", "1080")),
        max_size_gb=float(_str("MAX_SIZE_GB", "20")),
        max_size_series_gb=float(_str("MAX_SIZE_SERIES_GB", "80")),
        min_seeders=int(_str("MIN_SEEDERS", "2")),
        block_codecs=tuple(c.lower() for c in _list("BLOCK_CODECS", "hevc,av1,vp9")),
        block_hdr=_bool("BLOCK_HDR", True),
        block_words=tuple(w.lower() for w in _list("BLOCK_WORDS", "10bit,10-bit,remux,camrip,telesync,tsrip")),
        page_size=int(_str("PAGE_SIZE", "6")),
        max_results=int(_str("MAX_RESULTS", "30")),
        db_path=_str("DB_PATH", "/data/bot.sqlite3"),
        poll_interval=int(_str("POLL_INTERVAL", "60")),
        kodi_retry_min=int(_str("KODI_RETRY_MIN", "5")),
        kodi_notify=_bool("KODI_NOTIFY", True),
        gemini_key=_str("GEMINI_API_KEY") or None,
        gemini_model=_str("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_proxy=_str("GEMINI_PROXY") or _str("TMDB_PROXY") or _str("TG_PROXY") or None,
        ai_order=tuple(x.lower() for x in _list("AI_ORDER", "yandex,groq,gemini")),
        yandex_key=_str("YANDEX_API_KEY") or None,
        yandex_folder=_str("YANDEX_FOLDER_ID") or None,
        yandex_model=_str("YANDEX_MODEL", "aliceai-llm/latest"),
        yandex_url=_str("YANDEX_URL", "https://ai.api.cloud.yandex.net/v1").rstrip("/"),
        groq_key=_str("GROQ_API_KEY") or None,
        groq_model=_str("GROQ_MODEL", "openai/gpt-oss-120b"),
        groq_proxy=_str("GROQ_PROXY") or _str("TMDB_PROXY") or _str("TG_PROXY") or None,
        subs_check_hours=float(_str("SUBS_CHECK_HOURS", "6")),
        subs_fallback_days=int(_str("SUBS_FALLBACK_DAYS", "7")),
        wait_check_hours=float(_str("WAIT_CHECK_HOURS", "12")),
        wait_min_height=int(_str("WAIT_MIN_HEIGHT", "1080")),
        wait_days=int(_str("WAIT_DAYS", "180")),
        kids_max_age=int(_str("KIDS_MAX_AGE", "12")),
        disk_reserve_gb=float(_str("DISK_RESERVE_GB", "5")),
        stall_hours=float(_str("STALL_HOURS", "3")),
        turtle_down_mb=float(_str("TURTLE_DOWN_MB", "5")),
        turtle_up_mb=float(_str("TURTLE_UP_MB", "1")),
        day_hours=_str("DAY_HOURS", "08:00-23:00"),
        load_file=_str("LOAD_FILE", "/data/host-load.json"),
        load_util=int(_str("LOAD_UTIL", "90")),
        load_alert_sec=int(_str("LOAD_ALERT_SEC", "120")),
        load_throttle_min=int(_str("LOAD_THROTTLE_MIN", "15")),
        load_cooldown_min=int(_str("LOAD_COOLDOWN_MIN", "30")),
        net_limit_mbit=int(_str("NET_LIMIT_MBIT", "300")),
        ai_user_day=int(_str("AI_USER_DAY", "10") or 0),
        ai_total_day=int(_str("AI_TOTAL_DAY", "50") or 0),
        ai_month_rub=float(_str("AI_MONTH_RUB", "0") or 0),
        itogi_day=int(_str("ITOGI_DAY", "30") or 0),
        itogi_hour=int(_str("ITOGI_HOUR", "19") or 19),
        stt_max_sec=int(_str("STT_MAX_SEC", "30") or 30),
        stt_price=float((_str("AI_PRICE_STT", "0.1626") or "0").replace(",", ".")),
        listwatch_hours=float(_str("LISTWATCH_HOURS", "12") or 12),
        update_dir=_str("UPDATE_DIR", "/data/update"),
        ai_prices=tuple((n, float(_str(f"AI_PRICE_{n.upper()}").replace(",", "."))) for n in ("yandex", "groq", "gemini")
                        if _str(f"AI_PRICE_{n.upper()}")),
    )
