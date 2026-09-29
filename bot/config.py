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

    queue_size: int
    health_interval: int
    disk_warn_gb: float
    kodi_offline_min: int
    health_url: str
    health_proxy: str | None
    weekly_day: int
    weekly_hour: int
    backup_dir: str

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
    )
