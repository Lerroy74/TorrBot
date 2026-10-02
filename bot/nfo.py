"""v8.3: подсказки для Kodi (.nfo со ссылкой на TMDB) — чтобы Kodi точно узнавал фильм/сериал,
как бы ни назывались файлы в раздаче (транслит, «Director's Cut», папка в папке).

Kodi сначала читает .nfo рядом с файлом: если там ссылка themoviedb.org/movie/ID (или /tv/ID в
tvshow.nfo в папке сериала), скрейпер берёт этот фильм, не угадывая по имени файла.

Откуда бот знает ID:
  * скачано через бота с карточки TMDB — ID сохранён при закачке;
  * иначе — по имени папки «Название (год)»: ищем в TMDB, берём только точное совпадение
    названия и года (иначе лучше ничего, чем чужой фильм).
Файлы и папки не переименовываем — раздачи продолжают раздаваться, Transmission не теряет их.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from . import library, tmdb

log = logging.getLogger("torrbot.nfo")

URL = {"m": "https://www.themoviedb.org/movie/{}", "t": "https://www.themoviedb.org/tv/{}"}
MIN_VIDEO = 50 * 1024 ** 2                  # меньше — сэмпл/трейлер, подсказку не кладём
MISS_TTL = 24 * 3600                        # не нашли в TMDB по имени папки — повторим через сутки
_RE_FOLDER = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d\d)\)$")


def videos(path: str) -> list[str]:
    """Видеофайлы раздачи (без сэмплов)."""
    if os.path.isfile(path):
        files = [path]
    else:
        files = [os.path.join(r, f) for r, _d, fs in os.walk(path) for f in fs]
    out = []
    for f in files:
        if os.path.splitext(f)[1].lower() not in library.VIDEO_EXT or "sample" in os.path.basename(f).lower():
            continue
        try:
            if os.path.getsize(f) >= MIN_VIDEO:
                out.append(f)
        except OSError:
            pass
    return sorted(out)


def _write(path: str, text: str) -> bool:
    if os.path.exists(path):
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        return True
    except OSError as e:
        log.warning("nfo %s: %r", path, e)
        return False


def write(kind: str, tmdb_id: int, entry_path: str, series_dir: str) -> int:
    """Положить подсказку. Фильм — «<файл>.nfo» рядом с каждым видео; сериал — tvshow.nfo
    в папке сериала (папка верхнего уровня в series). Вернёт, сколько файлов записано."""
    url = URL[kind].format(tmdb_id)
    if kind == "t":
        if not os.path.isdir(entry_path) or os.path.dirname(entry_path.rstrip("/")) != series_dir.rstrip("/"):
            return 0                         # сериал без своей папки — Kodi его так не поймёт, не трогаем
        return int(_write(os.path.join(entry_path, "tvshow.nfo"), url))
    return sum(_write(os.path.splitext(v)[0] + ".nfo", url) for v in videos(entry_path))


def has_hint(kind: str, entry_path: str) -> bool:
    """Подсказка уже есть (наша или чужая)."""
    if kind == "t":
        return os.path.exists(os.path.join(entry_path, "tvshow.nfo"))
    vids = videos(entry_path)
    return bool(vids) and all(os.path.exists(os.path.splitext(v)[0] + ".nfo") for v in vids)


def parse_folder(name: str) -> tuple[str, str] | None:
    """«Троя (2004)» → («Троя», «2004»); без года — None (угадывать не будем)."""
    m = _RE_FOLDER.match(name.strip())
    return (m.group("title").strip(), m.group("year")) if m else None


def exact_match(candidates: list[dict], title: str, year: str, kind: str) -> int | None:
    """ID, только если ровно один кандидат нужного типа с тем же названием и годом."""
    want = tmdb._norm(title)
    mtype = "tv" if kind == "t" else "movie"
    hits = set()
    for c in candidates:
        if c.get("media_type") != mtype:
            continue
        names = {tmdb._norm(c.get(k) or "") for k in ("title", "original_title", "name", "original_name")}
        y = (c.get("first_air_date" if kind == "t" else "release_date") or "")[:4]
        if want in names and y == year:
            hits.add(int(c["id"]))
    return hits.pop() if len(hits) == 1 else None


def known_id(st, entry, torrents: list[dict]) -> tuple[str, int] | None:
    """ID из закачек бота, лежащих в этой папке."""
    inner, _outer = library.torrents_for(entry.path, torrents)
    for t in inner:
        row = st.db.get(t["hashString"].lower())
        if row is not None and row["tmdb_id"] and row["tmdb_kind"] in URL:
            return row["tmdb_kind"], int(row["tmdb_id"])
    return None


async def sync(st) -> int:
    """Пройти медиатеку на диске и положить подсказки, где их нет. Вернёт, сколько записано."""
    cfg = st.cfg
    try:
        torrents = await st.tr.get()
    except Exception as e:
        log.info("nfo: Transmission не ответил: %r", e)
        torrents = []
    misses = st.__dict__.setdefault("nfo_misses", {})
    now = time.time()
    written = 0
    entries = await asyncio.to_thread(library.scan, cfg.dir_movies, cfg.dir_series)
    for e in entries:
        kind = "t" if e.kind == "series" else "m"
        if await asyncio.to_thread(has_hint, kind, e.path):
            continue
        got = known_id(st, e, torrents)
        if got and got[0] != kind:
            got = None                        # сериал скачан в «фильмы» или наоборот — не путаем
        if not got and e.is_dir and cfg.tmdb_key and now - misses.get(e.path, 0) > MISS_TTL:
            parsed = parse_folder(e.name)
            if parsed:
                try:
                    cands = await tmdb.search(st.tmdb_http, cfg.tmdb_key, parsed[0], cfg.tmdb_lang)
                except Exception as ex:
                    log.info("nfo: TMDB не ответил: %r", ex)
                    cands = None
                tid = exact_match(cands or [], parsed[0], parsed[1], kind)
                if tid:
                    got = (kind, tid)
                elif cands is not None:
                    misses[e.path] = now
        if not got:
            continue
        n = await asyncio.to_thread(write, got[0], got[1], e.path, cfg.dir_series)
        if n:
            log.info("nfo: %s → %s", e.name, URL[got[0]].format(got[1]))
            written += n
    return written


async def loop(st, every: float = 6 * 3600) -> None:
    """При запуске и раз в 6 часов (после закачек — сразу, см. watch_once)."""
    await asyncio.sleep(90)
    while True:
        await sync_and_scan(st)
        await asyncio.sleep(every)


async def sync_and_scan(st, delay: float = 0) -> None:
    """Подсказки → обновление медиатеки Kodi (после закачки — через delay, пока Transmission
    переносит файлы). Ошибки не мешают остальному."""
    from .remote import kodi_request
    await asyncio.sleep(delay)
    try:
        n = await sync(st)
    except Exception as e:
        log.warning("nfo sync: %r", e)
        n = 0
    if st.kodi and (n or delay):
        kodi_request(st, "scan", delay=1)


def tidy(path: str, stop: tuple[str, ...]) -> None:
    """После автоочистки: Transmission удалил видео, а наши .nfo (и пустые папки) остались — убрать.
    Трогаем, только если видео в папке не осталось совсем."""
    if not os.path.isdir(path) or videos(path) or any(
            os.path.splitext(f)[1].lower() in library.VIDEO_EXT
            for _r, _d, fs in os.walk(path) for f in fs):
        return
    for r, _d, fs in os.walk(path, topdown=False):
        for f in fs:
            if f.lower().endswith(".nfo"):
                try:
                    os.unlink(os.path.join(r, f))
                except OSError:
                    pass
        try:
            os.rmdir(r)
        except OSError:
            pass
    library.prune_empty(path, stop)
