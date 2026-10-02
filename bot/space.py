"""v7: хватит ли места на диске.

* Перед закачкой (размер раздачи известен из поиска): свободно − что ещё докачивается −
  запас DISK_RESERVE_GB ≥ размер? Нет — закачку не ставим, а показываем, что можно удалить
  (просмотренное давно, с худшими оценками). Кто может удалять — кнопка «🗑 Освободить место»,
  остальным — «попросил администратора», админу — запрос с кнопкой «⬇ Поставить для …».
* Раздача всё-таки упала с ошибкой «нет места» — сказать тому, кто качал, и админам (один раз).
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup

from . import cleanup, library

log = logging.getLogger("torrbot")
esc = html.escape
GB = 1024 ** 3
PENDING_TTL = 3 * 86400


def gb(n) -> str:
    return f"{(n or 0) / GB:.1f} ГБ"


async def room(st, folder: str) -> tuple[int | None, int]:
    """(сколько можно занять новой закачкой, сколько ещё докачивается). None — не знаем."""
    free = await st.tr.free_space(folder)
    if free is None:
        return None, 0
    try:
        left = sum(int(t.get("leftUntilDone") or 0) for t in await st.tr.get())
    except Exception:
        left = 0
    return free - left - int(st.cfg.disk_reserve_gb * GB), left


def is_nospace(t: dict) -> bool:
    e = (t.get("errorString") or "").lower()
    return bool(t.get("error")) and ("space" in e or "мест" in e or "enospc" in e)


async def suggestions(st, limit: int = 3) -> list[str]:
    """Что удалить в первую очередь: давно просмотренное (по Kodi), потом худшее по оценкам."""
    from .deleter import describe, entry_jid
    cfg = st.cfg
    try:
        torrents = await st.tr.get()
        entries = await asyncio.to_thread(library.scan, cfg.dir_movies, cfg.dir_series)
    except Exception:
        return []
    items = None
    if st.kodi:
        try:
            items = await asyncio.wait_for(st.kodi.videos(), 8)
        except Exception:
            items = None
    scored = []
    for e in entries:
        label = describe(st, e, torrents)[0]
        jid = entry_jid(st, e, torrents)
        avg, cnt = st.db.rating_summary(jid) if jid else (None, 0)
        seen = None
        if items is not None:
            kp = cleanup.to_kodi_path(e.path, cfg.media_root, cfg.kodi_media_url)
            v = cleanup.judge(kp, items) if kp else None
            if v and v.status == "watched":
                seen = v.last_played or datetime(2000, 1, 1)
        if seen is None and not cnt:
            continue
        key = (0, seen.timestamp(), 0) if seen else (1, avg or 0, -e.size)
        bits = [gb(e.size)] + ([f"просмотрено {seen:%d.%m}"] if seen and seen.year > 2000 else
                               (["просмотрено"] if seen else [])) + ([f"⭐ {avg:.1f}"] if cnt else [])
        scored.append((key, f"• {'📺' if e.kind == 'series' else '🎬'} {esc(label[:60])} — {', '.join(bits)}"))
    return [line for _, line in sorted(scored)[:limit]]


def remember(st, **job) -> str:
    now = time.time()
    for k in [k for k, v in st.pending_adds.items() if now - v["ts"] > PENDING_TTL]:
        del st.pending_adds[k]
    token = secrets.token_hex(4)
    st.pending_adds[token] = {**job, "ts": now}
    return token


async def refuse(bot: Bot, st, job: dict, avail: int, left: int) -> None:
    """Места нет: сообщить тому, кто ставил (и админу, если сам удалять не может)."""
    uid, chat = job["uid"], job["chat_id"]
    token = remember(st, **job)
    need = job["size"]
    head = (f"💾 <b>Не влезает</b>: {esc(job['title'][:120])}\n"
            f"Нужно {gb(need)}, а можно занять {gb(max(avail, 0))}"
            + (f" (ещё {gb(left)} докачивается)" if left else "")
            + f", запас {st.cfg.disk_reserve_gb:g} ГБ не трогаю.")
    tips = await suggestions(st)
    tip = ("\n\nЧто можно удалить:\n" + "\n".join(tips)) if tips else ""
    may_delete = st.hooks["may_delete"](uid)
    if may_delete:
        await bot.send_message(chat, head + tip + "\n\nОсвободи место и нажми «Поставить снова».",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                   [B(text="🗑 Освободить место", callback_data="lr")],
                                   [B(text="🔁 Поставить снова", callback_data=f"pa:{token}")]]))
        return
    await bot.send_message(chat, head + "\n\nПопросил администратора освободить место — "
                                        "как освободит, закачка встанет сама.")
    u = st.db.user(uid)
    who = esc(((u["name"] if u is not None else "") or str(uid)).split(" (")[0][:40])
    for a in st.cfg.admin_ids:
        try:
            await bot.send_message(a, f"💾 <b>{who}</b> хочет скачать, но места нет.\n{head}{tip}",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                       [B(text="🗑 Освободить место", callback_data="lr")],
                                       [B(text=f"⬇ Поставить для: {who[:20]}", callback_data=f"pa:{token}")]]))
        except Exception as e:
            log.warning("не смог написать админу о месте: %r", e)


async def nospace_notify(bot: Bot, st, row, name: str) -> None:
    """Раздача остановилась: на диске кончилось место."""
    text = (f"💾 Закачка <b>{esc(name[:150])}</b> остановилась: на диске кончилось место.\n"
            f"Освободи место (/delete), потом в /status нажми ▶ у этой закачки.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text="▶ Продолжить", callback_data=f"tp:{row['hash']}")]])
    sent = set()
    if row["chat_id"]:
        try:
            await bot.send_message(row["chat_id"], text, reply_markup=kb)
            sent.add(row["chat_id"])
        except Exception:
            pass
    for a in st.cfg.admin_ids:
        if a not in sent:
            try:
                await bot.send_message(a, text, reply_markup=kb)
            except Exception:
                pass


def build_router(st) -> Router:
    r = Router()

    @r.callback_query(F.data.regexp(r"^pa:[0-9a-f]{8}$"))
    async def retry(cb: CallbackQuery):
        job = st.pending_adds.get(cb.data[3:])
        if not job:
            await cb.answer("Кнопка устарела — найди фильм заново", show_alert=True)
            return
        uid = cb.from_user.id
        if uid != job["uid"] and uid not in st.cfg.admin_ids:
            await cb.answer("Это не твоя закачка", show_alert=True)
            return
        avail, _ = await room(st, job["folder"])
        if avail is not None and avail < job["size"]:
            await cb.answer(f"Всё ещё не влезает: нужно {gb(job['size'])}, можно занять {gb(max(avail, 0))}",
                            show_alert=True)
            return
        st.pending_adds.pop(cb.data[3:], None)
        await cb.answer("Ставлю…")
        args = {k: v for k, v in job.items() if k not in ("ts", "folder")}
        h = await st.hooks["enqueue"](cb.bot, **args)
        if h and uid != job["uid"]:
            await cb.message.answer("⬇ Поставил на закачку — пользователю написал.")

    return r
