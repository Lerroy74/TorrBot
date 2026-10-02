"""v8.2: «🔔 Сообщать о раздачах» — бот сам следит за фильмами из списков.

Включает каждый сам (кнопка в /lists, по умолчанию выключено). Раз в LISTWATCH_HOURS часов
бот ищет раздачи непросмотренных фильмов из личных списков человека и списков его групп и пишет,
когда раздача ПОЯВИЛАСЬ или впервые появилась в хорошем качестве (≥ WAIT_MIN_HEIGHT, обычно 1080p).
Что уже было на момент первой проверки — не сообщаем (иначе включил — и получил сто сообщений).

* кто может качать — смотрим раздачи, подходящие приставке (фильтры поиска), кнопка «⬇ Найти раздачи»;
  уже скачанное на домашний диск — пропускаем;
* у кого «лёгкий режим» — лучшая раздача без ограничений приставки и кнопка «📋 Magnet»;
* в детском режиме не следим.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup

from . import jacred, kids, tmdb

log = logging.getLogger("torrbot.listwatch")
esc = html.escape
FLAG = "lwatch"
PER_ROUND = 15            # не больше стольких поисков за проход (раз в 10 минут) — не грузим jac.red


def enabled(st, uid: int) -> bool:
    return st.db.flag(uid, FLAG)


def watchers(st) -> list[int]:
    ids = [r["user_id"] for r in st.db.c.execute("SELECT user_id FROM prefs WHERE key=? AND value='1'", (FLAG,))]
    return [u for u in ids if st.is_allowed(u) and not kids.is_kid(st, u)]


def films_of(st, uid: int) -> set[tuple[str, int]]:
    """Непросмотренные фильмы из личных списков и списков групп человека."""
    rows = st.db.c.execute(
        "SELECT DISTINCT i.kind, i.tmdb_id FROM list_items i JOIN lists l ON l.id=i.list_id"
        " WHERE i.watched_at IS NULL AND (l.user_id=? OR l.group_id IN"
        " (SELECT group_id FROM group_members WHERE user_id=?))", (uid, uid)).fetchall()
    return {(r["kind"], r["tmdb_id"]) for r in rows}


def on_disk(st, kind: str, tid: int) -> bool:
    return st.db.c.execute("SELECT 1 FROM downloads WHERE tmdb_kind=? AND tmdb_id=? AND removed=0",
                           (kind, tid)).fetchone() is not None


def level_of(rels: list[jacred.Release], min_h: int) -> int:
    if not rels:
        return 0
    return 2 if max((r.height or 0) for r in rels) >= min_h else 1


async def search(st, info: tmdb.Info) -> tuple[list[jacred.Release], jacred.Release | None]:
    """(раздачи для приставки, лучшая без ограничений) — одним поиском."""
    found = await asyncio.gather(*(jacred.search(st.http, st.cfg, q) for q in tmdb.tracker_queries(info)),
                                 return_exceptions=True)
    if all(isinstance(x, BaseException) for x in found):
        raise RuntimeError(f"jac.red: {found[0]!r}")
    items = [it for res in found if not isinstance(res, BaseException) for it in res]
    match = (lambda t, ser: tmdb.matches(info, t, ser))
    pi, _ = jacred.select(items, st.cfg)
    pi = [r for r in pi if match(r.title, r.is_series)]
    return pi, jacred.best_any(items, match, st.cfg.min_seeders)


def seen(st, uid: int, kind: str, tid: int) -> int | None:
    row = st.db.c.execute("SELECT level FROM list_watch WHERE user_id=? AND kind=? AND tmdb_id=?",
                          (uid, kind, tid)).fetchone()
    return None if row is None else row["level"]


def remember(st, uid: int, kind: str, tid: int, level: int) -> None:
    st.db.c.execute("INSERT OR REPLACE INTO list_watch VALUES (?,?,?,?,?)", (uid, kind, tid, level, int(time.time())))
    st.db.c.commit()


async def check_once(bot: Bot, st, now: float | None = None) -> int:
    """Один проход. Вернёт, сколько сообщений отправили."""
    now = now or time.time()
    every = st.cfg.listwatch_hours * 3600
    users = watchers(st)
    if not users:
        return 0
    want: dict[tuple[str, int], list[int]] = {}
    for uid in users:
        for f in films_of(st, uid):
            want.setdefault(f, []).append(uid)
    checked = {(r["kind"], r["tmdb_id"]): r["checked_at"]
               for r in st.db.c.execute("SELECT * FROM list_watch_film")}
    due = sorted((f for f in want if now - (checked.get(f) or 0) >= every), key=lambda f: checked.get(f) or 0)
    sent = 0
    may = st.hooks["may_download"]
    for kind, tid in due[:PER_ROUND]:
        st.db.c.execute("INSERT OR REPLACE INTO list_watch_film VALUES (?,?,?)", (kind, tid, int(now)))
        st.db.c.commit()
        info = await st.hooks["info_for"](kind, tid)
        if not info:
            continue
        try:
            pi, best = await search(st, info)
        except Exception as e:
            log.info("listwatch %s%s: %r", kind, tid, e)
            continue
        for uid in want[(kind, tid)]:
            dl = may(uid)
            if dl and on_disk(st, kind, tid):
                continue
            lvl = level_of(pi, st.cfg.wait_min_height) if dl else level_of([best] if best else [],
                                                                            st.cfg.wait_min_height)
            before = seen(st, uid, kind, tid)
            if before is None:
                remember(st, uid, kind, tid, lvl)               # первая проверка — тихо
                continue
            if lvl <= before:                                   # ничего нового (пропавшие раздачи не считаем)
                continue
            remember(st, uid, kind, tid, lvl)
            top = max(pi, key=lambda r: (r.height or 0, r.seeders)) if dl else best
            q = f"{top.height}p" if top and top.height else "?"
            what = "появилась хорошая раздача" if lvl == 2 else "появилась раздача"
            text = (f"🔔 {'📺' if info.is_tv else '🎬'} <b>{esc(info.title)}</b>"
                    + (f" ({info.year})" if info.year else "") + f" из твоего списка: {what} ({q}).")
            if dl:
                markup = InlineKeyboardMarkup(inline_keyboard=[[B(text="⬇ Найти раздачи",
                                                                 callback_data=f"Ld:{kind}:{tid}")]])
            else:
                from .main import magnet_row
                markup = InlineKeyboardMarkup(inline_keyboard=[magnet_row(best)])
            try:
                await bot.send_message(uid, text, reply_markup=markup)
                sent += 1
            except Exception as e:
                log.info("listwatch → %s: %r", uid, e)
    return sent


async def loop(bot: Bot, st) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            n = await check_once(bot, st)
            if n:
                log.info("listwatch: отправлено %s", n)
        except Exception as e:
            log.warning("listwatch: %r", e)
        await asyncio.sleep(600)
