"""/delete — удалить скачанное вручную, чтобы освободить место.

Список всего, что лежит в movies/ и series/ (и скачанного ботом, и положенного руками),
от больших к маленьким → карточка (размер, кто поставил, смотрели ли в Kodi) →
подтверждение → раздачи снимаются в Transmission вместе с файлами, остальное бот
удаляет с диска сам → Kodi чистит медиатеку.

Удалять может админ и те, кому он разрешил в /users (🗑). Когда удаляет не админ —
админам приходит сообщение (выключается NOTIFY_ADMIN_ON_DELETE=0).
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

from . import cleanup, journal, library, picks

log = logging.getLogger("torrbot")
esc = html.escape

LIB_TTL = 3600
PAGE = 8
SORTS = {"s": "📦 По размеру", "r": "⭐ По оценке"}
SORT_PREF = "delete_sort"          # prefs: как пользователь последний раз сортировал /delete


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def short_name(st, uid: int | None) -> str:
    if not uid:
        return ""
    if uid in st.cfg.admin_ids:
        return "админ"
    u = st.db.user(uid)
    return ((u["name"] if u is not None and u["name"] else "") or str(uid)).split(" (")[0][:20]


def describe(st, e: library.Entry, torrents: list[dict]) -> tuple[str, list[str], float | None]:
    """(понятное название, кто поставил, готовность закачки если ещё качается)."""
    inner, _ = library.torrents_for(e.path, torrents)
    label, who, progress = e.label, [], None
    rows = [st.db.get(t["hashString"].lower()) for t in inner]
    labels = {r["label"] for r in rows if r is not None and r["label"]}
    if len(labels) == 1 and not e.is_dir:            # у папки «Название (год)» имя и так хорошее
        label = labels.pop()
    for r in rows:
        if r is not None:
            n = short_name(st, r["user_id"])
            if n and n not in who:
                who.append(n)
    active = [t for t in inner if (t.get("percentDone") or 0) < 1]
    if active:
        progress = min(t.get("percentDone") or 0 for t in active)
    return label, who, progress


def entry_jid(st, e: library.Entry, torrents: list[dict]) -> int | None:
    """Запись журнала для пункта /delete: по закачке бота, иначе по названию. Ничего не создаёт."""
    inner, _ = library.torrents_for(e.path, torrents)
    for t in inner:
        row = st.db.get(t["hashString"].lower())
        if row is not None and row["jid"]:
            return row["jid"]
    return st.db.journal_find(describe(st, e, torrents)[0])


def sort_entries(st, entries: list[library.Entry], torrents: list[dict], mode: str) -> list[library.Entry]:
    """s — большие сверху (как было); r — худшие по средней оценке сверху (их проще удалять),
    без оценок — в конце, большие первыми."""
    if mode != "r":
        return sorted(entries, key=lambda e: -e.size)

    def key(e):
        jid = entry_jid(st, e, torrents)
        avg, cnt = st.db.rating_summary(jid) if jid else (None, 0)
        return (0, avg, -cnt, -e.size) if cnt else (1, 0, 0, -e.size)
    return sorted(entries, key=key)


def build_router(st) -> Router:
    r = Router()
    cfg = st.cfg
    libs: dict[str, tuple[float, list[library.Entry], list[dict]]] = {}
    sorts: dict[str, str] = {}             # lid → s/r

    def is_admin(uid: int) -> bool:
        return uid in cfg.admin_ids

    def may_delete(uid: int) -> bool:
        # v8: домашний диск — только у тех, кто может качать
        return is_admin(uid) or (st.is_allowed(uid) and st.db.can_delete(uid)
                                 and st.hooks.get("may_download", lambda _: True)(uid))

    st.hooks["may_delete"] = may_delete

    async def load(mode: str = "s") -> tuple[str, list[library.Entry], list[dict]]:
        torrents = await st.tr.get()
        entries = await asyncio.to_thread(library.scan, cfg.dir_movies, cfg.dir_series)
        return remember(sort_entries(st, entries, torrents, mode), torrents, mode)

    def remember(entries, torrents, mode: str):
        now = time.time()
        for k in [k for k, v in libs.items() if now - v[0] > LIB_TTL]:
            del libs[k]
            sorts.pop(k, None)
        lid = secrets.token_hex(4)
        libs[lid] = (now, entries, torrents)
        sorts[lid] = mode
        return lid, entries, torrents

    def my_sort(uid: int) -> str:
        m = st.db.pref(uid, SORT_PREF, "s")
        return m if m in SORTS else "s"

    async def list_view(lid: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
        _, entries, torrents = libs[lid]
        mode = sorts.get(lid, "s")
        pages = max(1, (len(entries) + PAGE - 1) // PAGE)
        page = max(0, min(page, pages - 1))
        free = await st.tr.free_space(cfg.dir_movies)
        head = "🗑 <b>Удаление</b>" + (f" · 💾 свободно {fmt_size(free)}" if free is not None else "")
        if not entries:
            return head + "\n\nВ папках фильмов и сериалов пусто.", kb([])
        order = "большие сверху" if mode == "s" else "худшие по оценке сверху, без оценок — в конце"
        lines = [f"{head}\nВсего {len(entries)} ({fmt_size(sum(e.size for e in entries))}), "
                 f"{order}. Стр. {page + 1}/{pages}"]
        btns = []
        for i in range(page * PAGE, min(len(entries), (page + 1) * PAGE)):
            e = entries[i]
            label, who, prog = describe(st, e, torrents)
            jid = entry_jid(st, e, torrents)
            avg, cnt = st.db.rating_summary(jid) if jid else (None, 0)
            extra = (f" · ⭐ {avg:.1f} ({cnt})" if cnt else "") + \
                    (f" · 👤 {esc(', '.join(who))}" if who else "") + \
                    (f" · ⬇ {prog * 100:.0f}%" if prog is not None else "")
            lines.append(f"<b>{i + 1}.</b> {'📺' if e.kind == 'series' else '🎬'} {esc(label[:70])}"
                         f" · {fmt_size(e.size)}{extra}")
            btns.append((i + 1, str(i + 1), f"li:{lid}:{i}"))
        rows, numbers = picks.numbered(btns, 5, per_row=5)  # v8.1: много — номер текстом
        nav = []
        if page > 0:
            nav.append(B(text="◀", callback_data=f"lb:{lid}:{page - 1}"))
        if page < pages - 1:
            nav.append(B(text="▶", callback_data=f"lb:{lid}:{page + 1}"))
        rows.append(nav)
        rows.append([B(text=("• " if m == mode else "") + t, callback_data=f"ls:{lid}:{m}") for m, t in SORTS.items()])
        if numbers:
            return picks.finish(lines[0] + "\n\n" + "\n".join(lines[1:]), numbers, "подробности и удаление"), kb(rows)
        return (lines[0] + "\n\n" + "\n".join(lines[1:]) +
                "\n\nНажми номер — покажу подробности и спрошу, удалять ли."), kb(rows)

    def pick(lid: str, idx: int, sidx: int) -> tuple[library.Entry, library.Entry] | None:
        """(что удаляем, запись верхнего уровня) или None, если кнопка устарела."""
        if lid not in libs:
            return None
        entries = libs[lid][1]
        if not 0 <= idx < len(entries):
            return None
        top = entries[idx]
        if sidx < 0:
            return top, top
        if not 0 <= sidx < len(top.seasons):
            return None
        return top.seasons[sidx], top

    async def watched_line(path: str) -> str:
        if not st.kodi:
            return ""
        kpath = cleanup.to_kodi_path(path, cfg.media_root, cfg.kodi_media_url)
        if not kpath:
            return ""
        try:
            items = await asyncio.wait_for(st.kodi.videos(), 8)
        except Exception as ex:
            log.info("delete: Kodi не ответил: %r", ex)
            return "\n📺 Kodi не ответил — не знаю, смотрели ли."
        v = cleanup.judge(kpath, items)
        if v.status == "watched":
            when = f" (последний раз {v.last_played:%d.%m.%y})" if v.last_played else ""
            return f"\n✅ Просмотрено{when}"
        if v.status == "partial":
            return f"\n▶ Смотрят: просмотрено {v.watched} из {v.files}" if v.files > 1 else "\n▶ Начали смотреть"
        if v.status == "unwatched":
            return "\n⚪ Ещё не смотрели"
        return "\n❔ В медиатеке Kodi не найдено"

    async def card(lid: str, idx: int, uid: int | None = None) -> tuple[str, InlineKeyboardMarkup] | None:
        got = pick(lid, idx, -1)
        if not got:
            return None
        e = got[0]
        torrents = libs[lid][2]
        label, who, prog = describe(st, e, torrents)
        lines = [f"{'📺' if e.kind == 'series' else '🎬'} <b>{esc(label[:150])}</b>",
                 f"💾 {fmt_size(e.size)}"]
        if e.label != label or e.name != label:
            lines.append(f"📁 <i>{esc(e.name[:150])}</i>")
        lines.append(f"👤 Поставил(а): {esc(', '.join(who))}" if who else "👤 Положено не через бота")
        if prog is not None:
            lines.append(f"⬇ Ещё качается ({prog * 100:.0f}%) — удаление отменит закачку")
        seen = await watched_line(e.path)
        text = "\n".join(lines) + seen
        whole = "🗑 Удалить сериал целиком" if e.kind == "series" and e.is_dir else "🗑 Удалить"
        rows = [[B(text=f"{whole} ({fmt_size(e.size)})", callback_data=f"ld:{lid}:{idx}:-1")]]
        # отдельно — только папки, которые не часть общей раздачи на весь сериал
        parts = [(si, s) for si, s in enumerate(e.seasons[:20]) if not library.torrents_for(s.path, torrents)[1]]
        if len(e.seasons) > 1 and parts:
            text += "\n\nМожно удалить отдельную папку (сезон):"
            for si, s in parts:
                rows.append([B(text=f"🗑 {s.name[:40]} ({fmt_size(s.size)})", callback_data=f"ld:{lid}:{idx}:{si}")])
        if st.kodi and seen and "Просмотрено" not in seen and "не ответил" not in seen:
            rows.append([B(text=journal.PC_BUTTON, callback_data=f"lw:{lid}:{idx}")])
        if st.kodi and uid is not None and "play_token" in st.hooks and st.hooks["may_remote"](uid) and prog is None:
            rows.append([B(text="▶ Включить на ТВ", callback_data=f"tvf:{st.hooks['play_token'](e.path)}")])
        rows.append([B(text="◀ К списку", callback_data=f"lb:{lid}:{idx // PAGE}")])
        return text, kb(rows)

    async def edit(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:                      # «message is not modified» или старое сообщение
            await cb.message.answer(text, reply_markup=markup)

    async def refuse(cb: CallbackQuery) -> bool:
        if may_delete(cb.from_user.id):
            st.db.touch(cb.from_user.id)
            return False
        await cb.answer("Удалять может только администратор или тот, кому он разрешил.", show_alert=True)
        return True

    @r.message(Command("delete"))
    async def delete_cmd(msg: Message):
        uid = msg.from_user.id
        if not st.is_allowed(uid):
            return
        if not may_delete(uid):
            await msg.answer("Удалять может только администратор или тот, кому он разрешил. "
                             "Попроси администратора — он включит это в /users.")
            return
        st.db.touch(uid)
        try:
            lid, _, _ = await load(my_sort(uid))
        except Exception as ex:
            await msg.answer(f"Transmission недоступен, удалять сейчас небезопасно: {esc(str(ex))}")
            return
        text, markup = await list_view(lid, 0)
        await msg.answer(text, reply_markup=markup)

    @r.callback_query(F.data == "lr")
    async def delete_fresh(cb: CallbackQuery):
        if await refuse(cb):
            return
        try:
            lid, _, _ = await load(my_sort(cb.from_user.id))
        except Exception as ex:
            await cb.answer(f"Transmission недоступен: {ex}", show_alert=True)
            return
        await cb.answer()
        text, markup = await list_view(lid, 0)
        await edit(cb, text, markup)

    @r.callback_query(F.data.regexp(r"^lb:[0-9a-f]{8}:\d+$"))
    async def delete_page(cb: CallbackQuery):
        if await refuse(cb):
            return
        _, lid, page = cb.data.split(":")
        if lid not in libs:
            await cb.answer("Список устарел — открываю заново")
            return await _reload(cb)
        await cb.answer()
        text, markup = await list_view(lid, int(page))
        await edit(cb, text, markup)

    async def _reload(cb: CallbackQuery):
        try:
            lid, _, _ = await load(my_sort(cb.from_user.id))
        except Exception as ex:
            await cb.message.answer(f"Transmission недоступен: {esc(str(ex))}")
            return
        text, markup = await list_view(lid, 0)
        await edit(cb, text, markup)

    @r.callback_query(F.data.regexp(r"^ls:[0-9a-f]{8}:[sr]$"))
    async def delete_sort(cb: CallbackQuery):
        """Переключить сортировку: по размеру / по оценке (запоминается для этого человека)."""
        if await refuse(cb):
            return
        _, lid, mode = cb.data.split(":")
        st.db.set_pref(cb.from_user.id, SORT_PREF, mode)
        if lid not in libs:
            await cb.answer("Список устарел — открываю заново")
            return await _reload(cb)
        await cb.answer(SORTS[mode])
        _, entries, torrents = libs[lid]
        new, _, _ = remember(sort_entries(st, entries, torrents, mode), torrents, mode)
        await edit(cb, *(await list_view(new, 0)))

    @r.callback_query(F.data.regexp(r"^li:[0-9a-f]{8}:\d+$"))
    async def delete_card(cb: CallbackQuery):
        if await refuse(cb):
            return
        _, lid, idx = cb.data.split(":")
        got = await card(lid, int(idx), cb.from_user.id)
        if not got:
            await cb.answer("Список устарел — открываю заново")
            return await _reload(cb)
        await cb.answer()
        await edit(cb, *got)

    def journal_id(lid: str, e) -> int | None:
        """Запись журнала для пункта /delete (создаётся, если положено руками)."""
        inner, _ = library.torrents_for(e.path, libs[lid][2])
        rows = [st.db.get(t["hashString"].lower()) for t in inner]
        jid = next((r["jid"] for r in rows if r is not None and r["jid"]), None)
        return jid or st.db.journal_note(e.kind, describe(st, e, libs[lid][2])[0])

    @r.callback_query(F.data.regexp(r"^lw:[0-9a-f]{8}:\d+$"))
    async def watched_on_pc(cb: CallbackQuery):
        """«Посмотрели на ПК» в /delete: отметить в Kodi и сразу предложить удалить.
        Оценку спросим после удаления (или после «Оставить»)."""
        if await refuse(cb):
            return
        _, lid, idx = cb.data.split(":")
        got = pick(lid, int(idx), -1)
        if not got:
            await cb.answer("Список устарел — открываю заново")
            return await _reload(cb)
        e = got[0]
        await cb.answer("Отмечаю…")
        _, note = await journal.mark_on_pc(st, [e.path], auto=False)
        note = note.split(" Положено не через бота")[0]          # хвост про автоочистку тут не к месту
        label = esc(describe(st, e, libs[lid][2])[0][:120])
        await edit(cb, f"{note}\n\nУдалить <b>{label}</b> сейчас?\n"
                       f"Файлы ({fmt_size(e.size)}) удалятся с диска насовсем.",
                   kb([[B(text="🗑 Да, удалить", callback_data=f"ly:{lid}:{idx}:-1"),
                        B(text="Оставить", callback_data=f"lk:{lid}:{idx}")]]))

    @r.callback_query(F.data.regexp(r"^lk:[0-9a-f]{8}:\d+$"))
    async def keep_after_pc(cb: CallbackQuery, bot: Bot):
        """«Оставить» после отметки — вернуть карточку и спросить оценку."""
        if await refuse(cb):
            return
        _, lid, idx = cb.data.split(":")
        got = pick(lid, int(idx), -1)
        if not got:
            await cb.answer("Список устарел — открываю заново")
            return await _reload(cb)
        await cb.answer()
        jid = journal_id(lid, got[0])
        c = await card(lid, int(idx), cb.from_user.id)
        if c:
            await edit(cb, *c)
        await journal.ask(bot, st, jid, cb.from_user.id, cb.message.chat.id, "pc", force=True)

    @r.callback_query(F.data.regexp(r"^ld:[0-9a-f]{8}:\d+:-?\d+$"))
    async def delete_ask(cb: CallbackQuery):
        if await refuse(cb):
            return
        _, lid, idx, sidx = cb.data.split(":")
        got = pick(lid, int(idx), int(sidx))
        if not got:
            await cb.answer("Список устарел — открываю заново")
            return await _reload(cb)
        e, top = got
        label, _, _ = describe(st, top, libs[lid][2])
        what = esc(label[:120]) + (f" → {esc(e.name[:60])}" if e is not top else "")
        await cb.answer()
        await edit(cb, f"Точно удалить <b>{what}</b>?\n"
                       f"Файлы ({fmt_size(e.size)}) удалятся с диска насовсем.",
                   kb([[B(text="🗑 Да, удалить", callback_data=f"ly:{lid}:{idx}:{sidx}"),
                        B(text="Нет", callback_data=f"li:{lid}:{idx}")]]))

    @r.callback_query(F.data.regexp(r"^ly:[0-9a-f]{8}:\d+:-?\d+$"))
    async def delete_yes(cb: CallbackQuery, bot: Bot):
        if await refuse(cb):
            return
        uid = cb.from_user.id
        _, lid, idx, sidx = cb.data.split(":")
        got = pick(lid, int(idx), int(sidx))
        if not got:
            await cb.answer("Список устарел — открой /delete заново", show_alert=True)
            return
        e, top = got
        label, _, _ = describe(st, top, libs[lid][2])
        top_label = label
        if e is not top:
            label = f"{label} → {e.name}"
        if not library.safe_target(e.path, cfg.dir_movies, cfg.dir_series):
            await cb.answer("Этот путь удалять нельзя", show_alert=True)
            return
        try:
            torrents = await st.tr.get()                # свежий список — с момента показа могло измениться
        except Exception as ex:
            await cb.answer(f"Transmission недоступен, удалять небезопасно: {ex}", show_alert=True)
            return
        inner, outer = library.torrents_for(e.path, torrents)
        if outer:
            await cb.answer("Эта папка — часть большей раздачи. Удали её целиком (выше по списку).",
                            show_alert=True)
            return
        await cb.answer("Удаляю…")
        owners, jid = set(), None
        try:
            for t in inner:
                h = t["hashString"].lower()
                row = st.db.get(h)
                if row is not None and row["jid"] and not jid:
                    jid = row["jid"]
                await st.tr.remove(h, delete_data=True)
                st.db.mark_removed(h, "delete")
                if row is not None and row["user_id"]:
                    owners.add((row["user_id"], row["chat_id"]))
            if inner:
                await asyncio.sleep(1)                  # Transmission удаляет файлы сам — дадим ему секунду
            await asyncio.to_thread(library.remove_path, e.path)
            if e is not top:
                await asyncio.to_thread(library.prune_empty, e.path, (cfg.dir_movies, cfg.dir_series))
        except Exception as ex:
            log.warning("delete %s: %r", e.path, ex)
            await edit(cb, f"❌ Не получилось удалить <b>{esc(label[:150])}</b>: {esc(str(ex))}", None)
            return
        libs.pop(lid, None)
        st.db.add_deletion(uid, e.kind, label[:200], e.size)
        if not jid:                                    # положено руками или скачано до v6.3
            jid = st.db.journal_note(e.kind, top_label)
        if jid and e is top:
            st.db.journal_deleted(jid, uid)
        log.info("удалено пользователем %s: %s (%s)", uid, e.path, fmt_size(e.size))
        free = await st.tr.free_space(cfg.dir_movies)
        tail = f"\n💾 Свободно теперь: {fmt_size(free)}" if free is not None else ""
        await edit(cb, f"🗑 Удалено: <b>{esc(label[:150])}</b> ({fmt_size(e.size)}){tail}",
                   kb([[B(text="🗑 Удалить ещё", callback_data="lr")]]))
        # оценку спрашиваем у того, кто удалил (всегда, если ещё не ответил), и у тех, кто качал (один раз)
        await journal.ask(bot, st, jid, uid, cb.message.chat.id, "delete", force=True)
        await journal.ask_many(bot, st, jid, sorted(owners), "delete")
        if st.kodi:
            from .remote import kodi_request
            kodi_request(st, "clean", delay=10)
        who = short_name(st, uid) or str(uid)
        if cfg.notify_deletes and not is_admin(uid):
            for a in cfg.admin_ids:
                try:
                    await bot.send_message(a, f"🗑 <b>{esc(who)}</b> удалил(а): {esc(label[:150])} "
                                              f"({fmt_size(e.size)})")
                except Exception as ex:
                    log.warning("не смог сообщить админу об удалении: %r", ex)
        for owner, chat in owners:                     # тому, чью закачку удалил кто-то другой
            if owner != uid and owner not in cfg.admin_ids and chat:
                try:
                    await bot.send_message(chat, f"🗑 {esc(who)} удалил(а) скачанное: <b>{esc(label[:150])}</b>")
                except Exception:
                    pass

    return r
