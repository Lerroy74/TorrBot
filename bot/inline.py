"""v8.2: инлайн-режим. В любом чате: «@бот название» → варианты из TMDB → в чат уходит карточка
фильма (обложка, описание) с кнопкой «🤖 Открыть в боте» (ссылка ?start=f_m_123: там списки,
оценка, раздачи или magnet — по правам того, кто откроет).

Работает только у допущенных в бот; чужим — пусто и кнопка «Попросить доступ».
Включается один раз у @BotFather: /setinline → выбрать бота → подсказка, например «название фильма».
"""
from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
                           InlineQueryResultArticle, InlineQueryResultPhoto, InlineQueryResultsButton,
                           InputTextMessageContent, Message)

from . import kids, tmdb

log = logging.getLogger("torrbot.inline")
THUMB = "https://image.tmdb.org/t/p/w185"
RE_START = re.compile(r"^f_([mt])_(\d+)$")


def start_arg(info: tmdb.Info) -> str:
    return f"f_{'t' if info.is_tv else 'm'}_{info.tmdb_id}"


def open_kb(username: str, info: tmdb.Info) -> InlineKeyboardMarkup | None:
    if not username:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🤖 Открыть в боте", url=f"https://t.me/{username}?start={start_arg(info)}")]])


def results_for(infos: list[tmdb.Info], username: str) -> list:
    out = []
    for inf in infos:
        rid = start_arg(inf)
        caption = inf.caption(700)
        title = f"{inf.title} ({inf.year})" if inf.year else inf.title
        descr = (inf.original_title + " · " if inf.original_title and inf.original_title != inf.title else "") + \
            (f"⭐ {inf.rating:.1f}" if inf.rating else "")
        kb = open_kb(username, inf)
        if inf.poster:
            out.append(InlineQueryResultPhoto(
                id=rid, photo_url=inf.poster, thumbnail_url=inf.poster.replace(tmdb.IMG, THUMB),
                title=title, description=descr, caption=caption, parse_mode="HTML", reply_markup=kb))
        else:
            out.append(InlineQueryResultArticle(
                id=rid, title=title, description=descr, reply_markup=kb,
                input_message_content=InputTextMessageContent(message_text=caption, parse_mode="HTML")))
    return out


def build_router(st) -> Router:
    r = Router(name="inline")

    async def username(bot) -> str:
        if not st.bot_username:
            try:
                st.bot_username = (await bot.me()).username or ""
            except Exception:
                st.bot_username = ""
        return st.bot_username

    @r.inline_query()
    async def inline(q: InlineQuery):
        uid = q.from_user.id
        if not st.is_allowed(uid):
            await q.answer([], cache_time=60, is_personal=True,
                           button=InlineQueryResultsButton(text="Попросить доступ к боту", start_parameter="hello"))
            return
        query = (q.query or "").strip()[:100]
        if len(query) < 2 or not st.cfg.tmdb_key:
            await q.answer([], cache_time=5, is_personal=True)
            return
        cands = await st.hooks["find_info"](query)
        _, year = tmdb.split_query(query)
        infos = kids.only_kids(st, uid, tmdb.choices(cands, year, 20))[:10]
        for inf in infos:
            st.infos[("t" if inf.is_tv else "m", inf.tmdb_id)] = inf
        try:
            await q.answer(results_for(infos, await username(q.bot)), cache_time=30, is_personal=True)
        except Exception as e:
            log.warning("inline answer: %r", e)

    return r


async def open_from_start(st, msg: Message, uid: int, arg: str) -> bool:
    """/start f_m_123 — карточка фильма из инлайн-сообщения: дальше как обычный выбор фильма."""
    m = RE_START.match(arg)
    if not m:
        return False
    info = await st.hooks["info_for"](m.group(1), int(m.group(2)))
    if not info:
        await msg.answer("Не смог открыть фильм — TMDB не отвечает, попробуй позже или напиши название.")
        return True
    ok, why = await kids.allowed(st, uid, info)
    if not ok:
        await msg.answer(why or "Этот фильм в детском режиме недоступен.")
        return True
    await st.hooks["search_for"](msg, info, uid=uid)
    return True
