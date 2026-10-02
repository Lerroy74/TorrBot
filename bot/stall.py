"""v7: зависшие закачки.

Закачка качается (не пауза, не очередь), но STALL_HOURS часов без движения — бот один раз
пишет тому, кто ставил, и предлагает 2–3 другие раздачи того же фильма в том же качестве.
«🔁 N» — старая раздача удаляется, новая ставится в ту же папку. «⏳ Подождать ещё» —
спросит снова через STALL_HOURS.
"""
from __future__ import annotations

import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup

from . import jacred, tmdb

log = logging.getLogger("torrbot")
esc = html.escape
DOWNLOADING = 4          # статус Transmission «качается»
ALTS_TTL = 2 * 86400


def track(st, t: dict, now: float) -> float:
    """Запомнить прогресс; вернёт, сколько секунд закачка стоит на месте (0 — двигается или не качается)."""
    h = t["hashString"].lower()
    if t.get("status") != DOWNLOADING or t.get("error"):
        st.progress.pop(h, None)
        return 0.0
    have = (int(t.get("haveValid") or 0), round(float(t.get("percentDone") or 0), 4))
    prev = st.progress.get(h)
    if prev is None or prev[0] != have:
        st.progress[h] = (have, now)
        return 0.0
    return now - prev[1]


async def alternatives(st, row, t: dict) -> tuple[list[jacred.Release], tmdb.Info | None]:
    """Другие подходящие раздачи того же фильма (без текущей), лучшие первыми."""
    from .extras import order_for_user
    cfg = st.cfg
    info = None
    if row["tmdb_id"] and cfg.tmdb_key:
        try:
            info = await tmdb.details(st.tmdb_http, cfg.tmdb_key, "tv" if row["tmdb_kind"] == "t" else "movie",
                                      row["tmdb_id"], cfg.tmdb_lang)
        except Exception as e:
            log.info("stall: tmdb %r", e)
    queries = tmdb.tracker_queries(info) if info else [q for q in [row["label"] or jacred.ru_title(row["title"] or "")] if q]
    items = []
    for q in queries:
        try:
            items += await jacred.search(st.http, cfg, q)
        except Exception as e:
            log.info("stall: jacred %r", e)
    results, _ = jacred.select(items, cfg)
    is_series = row["category"] == "series"
    h = row["hash"]
    alts = [r for r in results if r.infohash != h and r.is_series == is_series
            and (not info or tmdb.matches(info, r.title, r.is_series)) and r.seeders >= 3]
    return order_for_user(st, row["user_id"] or 0, alts)[:3], info


async def check(bot: Bot, st, torrents: dict[str, dict], rows: dict, now: float | None = None) -> int:
    """Вызывается из watcher. Вернёт, скольким написали."""
    now = now or time.time()
    limit = st.cfg.stall_hours * 3600
    sent = 0
    for h, row in rows.items():
        t = torrents.get(h)
        if t is None:
            continue
        idle = track(st, t, now)
        if limit <= 0 or idle < limit or row["stall_at"]:
            continue
        st.db.set_download(h, stall_at=int(now))
        name = st.hooks["nice_name"](t) if "nice_name" in st.hooks else t.get("name", "")
        alts, _ = await alternatives(st, row, t)
        hours = int(idle // 3600)
        head = (f"🐌 <b>{esc(name[:120])}</b> стоит без движения {hours} ч "
                f"(готово {float(t.get('percentDone') or 0) * 100:.0f}%, раздающих {t.get('peersSendingToUs', 0)}).")
        rows_kb = []
        if alts:
            st.stall_alts[h] = (now, alts)
            lines = [head, "Можно взять другую раздачу:"]
            for i, r in enumerate(alts, 1):
                lines.append(f"<b>{i}.</b> {esc(r.title[:150])}\n   <code>{esc(r.short_line())}</code>")
            rows_kb.append([B(text=f"🔁 {i}", callback_data=f"sw:{h}:{i - 1}") for i in range(1, len(alts) + 1)])
            text = "\n\n".join(lines)
        else:
            text = head + "\n\nДругих подходящих раздач не нашёл — можно подождать или отменить."
        rows_kb.append([B(text="⏳ Подождать ещё", callback_data=f"sww:{h}"),
                        B(text="✖ Отменить", callback_data=f"cx:{h}")])
        try:
            await bot.send_message(row["chat_id"], text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb))
            sent += 1
        except Exception as e:
            log.warning("stall: не смог написать: %r", e)
    return sent


def build_router(st) -> Router:
    r = Router()

    @r.callback_query(F.data.regexp(r"^sww:[0-9a-f]{40}$"))
    async def wait_more(cb: CallbackQuery):
        h = cb.data[4:]
        if not st.hooks["can_cancel"](cb.from_user.id, h):
            await cb.answer("Это не твоя закачка", show_alert=True)
            return
        st.db.set_download(h, stall_at=None)
        st.progress.pop(h, None)
        await cb.answer(f"Ок, подожду ещё {st.cfg.stall_hours:g} ч")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    @r.callback_query(F.data.regexp(r"^sw:[0-9a-f]{40}:\d$"))
    async def switch(cb: CallbackQuery):
        _, h, i = cb.data.split(":")
        uid = cb.from_user.id
        if not st.hooks["can_cancel"](uid, h):
            await cb.answer("Это не твоя закачка", show_alert=True)
            return
        got = st.stall_alts.get(h)
        row = st.db.get(h)
        if not got or row is None or int(i) >= len(got[1]) or time.time() - got[0] > ALTS_TTL:
            await cb.answer("Варианты устарели — найди фильм заново", show_alert=True)
            return
        rel = got[1][int(i)]
        await cb.answer("Меняю раздачу…")
        try:
            await st.tr.remove(h, delete_data=True)
        except Exception as e:
            await cb.message.answer(f"Не получилось убрать старую раздачу: {esc(str(e))}")
            return
        st.db.mark_removed(h, "replaced")
        st.stall_alts.pop(h, None)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await st.hooks["enqueue"](cb.bot, uid=row["user_id"] or uid, chat_id=row["chat_id"] or cb.message.chat.id,
                                  magnet=rel.magnet, title=rel.title, is_series=rel.is_series,
                                  poster=row["poster"], subfolder=row["label"], size=rel.size,
                                  details=rel.details, tmdb_kind=row["tmdb_kind"], tmdb_id=row["tmdb_id"],
                                  sub_id=row["sub_id"])

    return r
