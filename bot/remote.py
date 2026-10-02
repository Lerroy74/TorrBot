"""v7: всё, что бот делает с Kodi на малинке сам по себе.

* /tv — пульт: что играет, пауза, перемотка, громкость, следующая серия;
  «📼 Что включить» и кнопки «▶ На ТВ» — включить скачанное на телевизоре.
  Пульт есть у админа и у тех, кому он разрешил в /users (📺).
* сообщение на экране ТВ, когда закачка готова (KODI_NOTIFY=0 — выключить);
* надёжное обновление медиатеки: Kodi не ответил (малинка выключена) — бот повторяет
  каждые KODI_RETRY_MIN минут, пока не получится; и один раз при каждом своём запуске.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

from . import cleanup, kodi

log = logging.getLogger("torrbot")
esc = html.escape
REMOTE = "can_remote"            # prefs: пульт разрешён


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def may_remote(st, uid: int) -> bool:
    return uid in st.cfg.admin_ids or (st.is_allowed(uid) and st.db.flag(uid, REMOTE))


# ---------- обновление медиатеки с повтором ----------
JOB_NAMES = {"scan": "обновление медиатеки", "clean": "чистка медиатеки"}


def kodi_request(st, job: str, delay: float = 20) -> None:
    """Попросить Kodi обновить (scan) или почистить (clean) медиатеку. Сделает kodi_sync_loop:
    через delay секунд, а если малинка не ответит — повторит через KODI_RETRY_MIN минут."""
    if st.kodi:
        st.kodi_jobs[job] = min(st.kodi_jobs.get(job, float("inf")), time.time() + delay)


async def kodi_sync_once(st, now: float | None = None) -> list[str]:
    """Выполнить созревшие задания. Вернёт выполненные."""
    now = now or time.time()
    done = []
    for job, due in sorted(st.kodi_jobs.items()):
        if due > now:
            continue
        try:
            await (st.kodi.scan() if job == "scan" else st.kodi.clean())
        except Exception as e:
            st.kodi_jobs[job] = now + max(1, st.cfg.kodi_retry_min) * 60
            log.info("Kodi: %s не удалось (%s) — повторю через %s мин", JOB_NAMES.get(job, job), e,
                     st.cfg.kodi_retry_min)
            continue
        del st.kodi_jobs[job]
        done.append(job)
        log.info("Kodi: %s — запущено", JOB_NAMES.get(job, job))
        if job == "clean" and "scan" in st.kodi_jobs:     # чистка и обновление разом Kodi не любит
            st.kodi_jobs["scan"] = max(st.kodi_jobs["scan"], now + 30)
    return done


async def kodi_sync_loop(st) -> None:
    kodi_request(st, "scan", delay=60)                    # при запуске бота — одно контрольное обновление
    while True:
        await asyncio.sleep(15)
        try:
            await kodi_sync_once(st)
        except Exception as e:
            log.warning("kodi sync: %r", e)


async def notify_done(st, name: str, series: bool) -> None:
    """Сообщение на экране ТВ: «Скачано: Маска (1994)». Малинка выключена — не беда."""
    if not st.kodi or not st.cfg.kodi_notify:
        return
    try:
        await st.kodi.notify("Скачан сериал" if series else "Скачан фильм", name)
    except Exception as e:
        log.info("Kodi: сообщение на экран не показалось: %s", e)


# ---------- включить на ТВ ----------
async def play_path(st, path: str) -> str:
    """Включить на ТВ то, что лежит по пути (как его видит бот). Вернёт текст для ответа."""
    if not st.kodi:
        return "Kodi не настроен (KODI_URL)."
    kpath = cleanup.to_kodi_path(path, st.cfg.media_root, st.cfg.kodi_media_url)
    if not kpath:
        return "Это лежит не в медиатеке — Kodi его не увидит."
    try:
        items = await asyncio.wait_for(st.kodi.videos(), 15)
    except Exception as e:
        return f"Малинка не отвечает ({esc(str(e)[:80])}). Она включена?"
    mine = cleanup.items_under(kpath, items)
    movies = [it for it in mine if it.get("movieid")]
    eps = sorted((it for it in mine if it.get("episodeid")),
                 key=lambda it: (int(it.get("season") or 0), int(it.get("episode") or 0)))
    try:
        if movies:
            await st.kodi.open({"movieid": movies[0]["movieid"]})
            return "▶ Включаю на ТВ"
        if eps:
            nxt = next((e for e in eps if not int(e.get("playcount") or 0)), eps[0])
            await st.kodi.open({"episodeid": nxt["episodeid"]})
            se = f"{int(nxt.get('season') or 0)}×{int(nxt.get('episode') or 0):02d}"
            return f"▶ Включаю на ТВ серию {se}"
        if os.path.isfile(path):
            await st.kodi.open({"file": kpath})
            return "▶ Включаю на ТВ (файла ещё нет в медиатеке — без описания)"
    except Exception as e:
        return f"Kodi не смог включить: {esc(str(e)[:120])}"
    kodi_request(st, "scan", delay=1)
    return "Kodi ещё не добавил это в медиатеку — попросил обновить. Попробуй через минуту."


# ---------- пульт ----------
async def pult_view(st) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[B(text="⏪ 30с", callback_data="tv:-30"), B(text="⏯", callback_data="tv:pp"),
             B(text="⏩ 30с", callback_data="tv:+30")],
            [B(text="⏪ 5м", callback_data="tv:-300"), B(text="⏹", callback_data="tv:st"),
             B(text="⏩ 5м", callback_data="tv:+300")],
            [B(text="⏮", callback_data="tv:prev"), B(text="🔉", callback_data="tv:vd"),
             B(text="🔇", callback_data="tv:mu"), B(text="🔊", callback_data="tv:vu"),
             B(text="⏭", callback_data="tv:next")],
            [B(text="📼 Что включить", callback_data="tv:ls"), B(text="🔄", callback_data="tv:rf")]]
    if not st.kodi:
        return "Kodi не настроен (KODI_URL в .env).", kb([])
    try:
        now = await asyncio.wait_for(st.kodi.now_playing(), 10)
    except Exception as e:
        return f"📺 <b>Пульт</b>\n\nМалинка не отвечает: {esc(str(e)[:100])}", kb([rows[-1]])
    if not now:
        return "📺 <b>Пульт</b>\n\nСейчас ничего не играет.", kb([rows[-1]])
    pos = f"{kodi.fmt_time(now['time'])} / {kodi.fmt_time(now['total'])}" if now["total"] else ""
    state = "⏸ пауза" if now["paused"] else "▶ играет"
    text = f"📺 <b>Пульт</b>\n\n{state}: <b>{esc(now['title'][:120])}</b>\n{pos} ({now['percent']:.0f}%)"
    return text, kb(rows)


def build_router(st) -> Router:
    r = Router()
    tokens: dict[str, tuple[float, str]] = {}            # токен → путь (кнопки «▶ На ТВ» из /delete)

    def path_token(path: str) -> str:
        now = time.time()
        for k in [k for k, v in tokens.items() if now - v[0] > 3600]:
            del tokens[k]
        t = secrets.token_hex(4)
        tokens[t] = (now, path)
        return t

    st.hooks["play_token"] = path_token
    st.hooks["may_remote"] = lambda uid: may_remote(st, uid)

    async def refuse(cb: CallbackQuery) -> bool:
        if may_remote(st, cb.from_user.id):
            return False
        await cb.answer("Пульт — только у администратора и у тех, кому он разрешил.", show_alert=True)
        return True

    async def show(cb: CallbackQuery, note: str = "") -> None:
        text, markup = await pult_view(st)
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        await cb.answer(note)

    @r.message(Command("tv"))
    async def tv_cmd(msg: Message):
        if not st.is_allowed(msg.from_user.id):
            return
        if not may_remote(st, msg.from_user.id):
            await msg.answer("Пульт — только у администратора и у тех, кому он разрешил в /users.")
            return
        text, markup = await pult_view(st)
        await msg.answer(text, reply_markup=markup)

    @r.callback_query(F.data.regexp(r"^tv:(pp|st|[+-]\d+|prev|next|vd|vu|mu|rf|ls)$"))
    async def tv_btn(cb: CallbackQuery):
        if await refuse(cb):
            return
        act = cb.data[3:]
        if act == "ls":
            await cb.answer()
            t, k = recent_view(st)
            await cb.message.answer(t, reply_markup=k)
            return
        note = ""
        try:
            if act == "pp":
                await st.kodi.play_pause()
            elif act == "st":
                await st.kodi.stop()
            elif act in ("prev", "next"):
                await st.kodi.go("previous" if act == "prev" else "next")
            elif act in ("vd", "vu", "mu"):
                vol = await st.kodi.volume({"vd": "decrement", "vu": "increment", "mu": "mute"}[act])
                note = "🔇 звук выключен" if vol is None else f"🔊 громкость {vol}"
            elif act != "rf":
                await st.kodi.seek(int(act))
            if act in ("pp", "st", "prev", "next") or act[0] in "+-":
                await asyncio.sleep(0.7)                  # пусть Kodi успеет — потом покажем новое состояние
        except Exception as e:
            await cb.answer(f"Малинка не отвечает: {str(e)[:120]}", show_alert=True)
            return
        await show(cb, note)

    @r.callback_query(F.data.regexp(r"^tvp:[0-9a-f]{40}$"))
    async def play_hash(cb: CallbackQuery):
        if await refuse(cb):
            return
        h = cb.data[4:]
        try:
            ts = await st.tr.get([h])
        except Exception as e:
            await cb.answer(f"Transmission не ответил: {e}", show_alert=True)
            return
        if not ts:
            await cb.answer("Этого уже нет на диске", show_alert=True)
            return
        t = ts[0]
        path = f"{(t.get('downloadDir') or '').rstrip('/')}/{t['name']}"
        await cb.answer("Включаю…")
        note = await play_path(st, path)
        await cb.message.answer(note)
        if note.startswith("▶"):
            await asyncio.sleep(2)
            t, k = await pult_view(st)
            await cb.message.answer(t, reply_markup=k)

    @r.callback_query(F.data.regexp(r"^tvf:[0-9a-f]{8}$"))
    async def play_token(cb: CallbackQuery):
        if await refuse(cb):
            return
        got = tokens.get(cb.data[4:])
        if not got:
            await cb.answer("Кнопка устарела — открой заново", show_alert=True)
            return
        await cb.answer("Включаю…")
        note = await play_path(st, got[1])
        await cb.message.answer(note)
        if note.startswith("▶"):
            await asyncio.sleep(2)
            t, k = await pult_view(st)
            await cb.message.answer(t, reply_markup=k)

    return r


def recent_view(st) -> tuple[str, InlineKeyboardMarkup]:
    rows = st.db.recent_done(8)
    if not rows:
        return "📼 Скачанного ботом на диске пока нет.", kb([])
    lines, btns = ["📼 <b>Недавно скачанное</b> — что включить на ТВ:"], []
    for i, row in enumerate(rows, 1):
        label = row["label"] or row["name"] or row["title"] or "?"
        icon = "📺" if row["category"] == "series" else "🎬"
        lines.append(f"<b>{i}.</b> {icon} {esc(label[:70])}")
        btns.append(B(text=f"▶ {i}", callback_data=f"tvp:{row['hash']}"))
    return "\n".join(lines), kb([btns[j:j + 4] for j in range(0, len(btns), 4)])
