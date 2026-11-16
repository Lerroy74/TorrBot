"""Списки и группы (v8.4 — упрощено).

* /lists — мой список «Хочу посмотреть» и общий список семейной группы (если ты в ней);
* «➕ В список» под карточкой фильма — выбрать, куда (✅ — уже там; нажать ещё раз — убрать);
* карточка фильма в списке: ⭐ оценить, ✅ посмотрели, ⬇ раздачи (если можно качать),
  🎞 подробнее, ➕ в другой список, 🗑 убрать;
* /gruppy — состав семейной группы; создаёт и меняет его только администратор бота.

Права: личный список — владелец; групповой — участники (убрать фильм — кто добавил или админ).
Callback-префиксы: L… (списки), G… (группы).
"""
from __future__ import annotations

import html
import logging
import random
import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

from . import picks, tmdb
from .access import NO_DL, may_download

log = logging.getLogger("torrbot")
esc = html.escape

PAGE = 10
INPUT_TTL = 600


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def name_of(st, uid: int | None) -> str:
    if not uid:
        return "?"
    u = st.db.user(uid)
    name = (u["name"] if u is not None and u["name"] else "") or ("админ" if uid in st.cfg.admin_ids else str(uid))
    return name.split(" (")[0][:20]


def day(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%y") if ts else "—"


def label(row) -> str:
    t = (row["title"] if row is not None and row["title"] else "") or "?"
    y = row["year"] if row is not None and row["year"] else ""
    return f"{t} ({y})" if y else t


def icon(kind: str) -> str:
    return "📺" if kind == "t" else "🎬"


# ---------- права ----------
def role(st, lst, uid: int) -> str | None:
    """owner — личный список этого человека; member — участник группы; None — нет доступа."""
    if lst is None:
        return None
    if lst["user_id"]:
        return "owner" if lst["user_id"] == uid else None
    if lst["group_id"] and st.db.is_member(lst["group_id"], uid):
        return "member"
    return None


def can_edit(st, lst, uid: int) -> bool:
    return role(st, lst, uid) in ("owner", "member")


def can_remove(st, lst, item, uid: int) -> bool:
    r = role(st, lst, uid)
    if r == "owner":
        return True
    return r == "member" and (item["added_by"] == uid or uid in st.cfg.admin_ids)


def list_title(st, lst, uid: int | None = None) -> str:
    """«Хочу посмотреть», «Семья: Хотим посмотреть», «Лена: Ужастики»."""
    if lst["group_id"]:
        g = st.db.group_get(lst["group_id"])
        return f"{g['name'] if g else '?'}: {lst['name']}"
    if uid is not None and lst["user_id"] != uid:
        return f"{name_of(st, lst['user_id'])}: {lst['name']}"
    return lst["name"]


def visible_items(st, lid: int, uid: int, watched: bool) -> list:
    return st.db.list_items(lid, watched)


def seen_marks(st, kind: str, tid: int, people: set[int] | None, skip: int | None = None) -> str:
    """«видели: Лена 8, Боб 6» (только эти люди; «не смотрел» не считается)."""
    parts = []
    for r in st.db.scores_for(kind, tid):
        if r["score"] is None or r["user_id"] == skip or (people is not None and r["user_id"] not in people):
            continue
        parts.append(f"{name_of(st, r['user_id'])} {r['score']}")
    return ", ".join(parts)


# ---------- карточки фильмов (кэш) ----------
def remember_info(st, info: tmdb.Info) -> None:
    kind = "t" if info.is_tv else "m"
    st.infos[(kind, info.tmdb_id)] = info
    try:
        st.db.title_put(kind, info.tmdb_id, info.title, info.year, info.poster, info.genres)
    except Exception as e:
        log.info("titles: %r", e)


async def info_for(st, kind: str, tid: int, full: bool = False) -> tmdb.Info | None:
    """Карточка фильма: из памяти, из кэша в базе или из TMDB (full — нужно описание)."""
    got = st.infos.get((kind, tid))
    if got and (got.overview or not full):
        return got
    row = st.db.title_get(kind, tid)
    if row and not full:
        return tmdb.Info(tid, kind == "t", row["title"] or "", "", row["year"] or "", "", 0.0, row["poster"],
                         genres=[int(x) for x in (row["genres"] or "").split(",") if x.strip().isdigit()])
    if st.cfg.tmdb_key:
        try:
            info = await tmdb.details(st.tmdb_http, st.cfg.tmdb_key, "tv" if kind == "t" else "movie", tid,
                                      st.cfg.tmdb_lang)
            if info:
                remember_info(st, info)
                return info
        except Exception as e:
            log.info("tmdb details %s%s: %r", kind, tid, e)
    if row:
        return tmdb.Info(tid, kind == "t", row["title"] or "", "", row["year"] or "", "", 0.0, row["poster"])
    return got


# ---------- экраны ----------
def menu_view(st, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    lines, items = ["📋 <b>Списки</b>"], []

    def entry(lst, title: str) -> None:
        todo, total = st.db.list_counts(lst["id"])
        n = len(items) + 1
        lines.append(f"<b>{n}.</b> {esc(title)} — {todo}" + (f" (всего {total})" if total != todo else ""))
        items.append((n, f"{'📋' if not lst['group_id'] else '👥'} {title[:40]} ({todo})", f"Lv:{lst['id']}:0:0"))

    lines.append("\n<b>Мои</b>")
    for lst in st.db.lists_of_user(uid):
        entry(lst, lst["name"])
    groups = st.db.groups_of(uid)
    if groups:
        lines.append("\n<b>Группы</b>")
        for g in groups:
            for lst in st.db.lists_of_group(g["id"]):
                entry(lst, f"{g['name']}: {lst['name']}")
    rows, numbers = picks.numbered(items, 5)                # v8.1: много списков — номер текстом
    if groups or uid in st.cfg.admin_ids:
        rows.append([B(text="👥 Семья", callback_data="Gm")])
    lines.append("\nДобавлять фильмы — кнопкой «➕ В список» под карточкой фильма.")
    return picks.finish("\n".join(lines), numbers, "открыть список"), kb(rows)


def list_view(st, lid: int, uid: int, page: int = 0, show_watched: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    lst = st.db.list_get(lid)
    r = role(st, lst, uid)
    if r is None:
        return "Этого списка нет или он тебе больше не доступен.", kb([[B(text="◀ Все списки", callback_data="Lm")]])
    items = visible_items(st, lid, uid, show_watched)
    todo, total = st.db.list_counts(lid)
    group = lst["group_id"]
    people = {m["user_id"] for m in st.db.group_members(group)} if group else None
    head = f"{'👥' if group else '📋'} <b>{esc(list_title(st, lst, uid))}</b> — не смотрели {todo}"
    if total != todo:
        head += f", ✅ {total - todo}"
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    w = int(show_watched)
    lines, btns = [head], []
    if not items:
        lines.append("\nПусто. Добавляй кнопкой «➕ В список» под карточкой фильма." if not total
                     else "\nВсё просмотрено 👍")
    for i in range(page * PAGE, min(len(items), (page + 1) * PAGE)):
        it = items[i]
        bits = []
        if group:
            seen = seen_marks(st, it["kind"], it["tmdb_id"], people)
            if seen:
                bits.append(f"видели: {seen}")
        else:
            mine = st.db.scores_by_tmdb(uid).get((it["kind"], it["tmdb_id"]))
            if mine:
                bits.append(f"ты: {mine}")
        done = "✅ " if it["watched_at"] else ""
        lines.append(f"<b>{i + 1}.</b> {done}{icon(it['kind'])} {esc(label(it))}" + (f" — {esc(' · '.join(bits))}"
                                                                                    if bits else ""))
        btns.append((i + 1, str(i + 1), f"Li:{lid}:{it['kind']}:{it['tmdb_id']}:{page}:{w}"))
    rows, numbers = picks.numbered(btns, 5, per_row=5)      # v8.1: много — номер текстом
    nav = []
    if page > 0:
        nav.append(B(text="◀", callback_data=f"Lv:{lid}:{page - 1}:{w}"))
    if page < pages - 1:
        nav.append(B(text="▶", callback_data=f"Lv:{lid}:{page + 1}:{w}"))
    rows.append(nav)
    extra = []
    if total != todo:
        extra.append(B(text="🙈 Скрыть просмотренные" if show_watched else "👁 Показать просмотренные",
                       callback_data=f"Lv:{lid}:0:{1 - w}"))
    if todo:
        extra.append(B(text="🎲 Случайный", callback_data=f"Lz:{lid}"))
    rows.append(extra)
    if group:
        rows.append([B(text="👥 Группа", callback_data=f"Gv:{group}")])
    rows.append([B(text="◀ Все списки", callback_data="Lm")])
    if len(lines) > 1 and not numbers:
        lines.append("\nНажми номер — карточка фильма.")
    return picks.finish("\n".join(lines), numbers, "карточка фильма"), kb(rows)


def item_view(st, lid: int, kind: str, tid: int, uid: int, page: int, w: int) -> tuple[str, InlineKeyboardMarkup]:
    lst = st.db.list_get(lid)
    r = role(st, lst, uid)
    it = st.db.list_item(lid, kind, tid) if r else None
    back = [B(text="◀ К списку", callback_data=f"Lv:{lid}:{page}:{w}")]
    if it is None:
        return "Этого фильма в списке уже нет.", kb([back])
    group = lst["group_id"]
    lines = [f"{icon(kind)} <b>{esc(label(it))}</b>", f"Список: {esc(list_title(st, lst, uid))}",
             f"Добавил(а): {esc(name_of(st, it['added_by']))}, {day(it['added_at'])}"]
    if group:
        people = {m["user_id"] for m in st.db.group_members(group)}
        seen = seen_marks(st, kind, tid, people)
        lines.append(f"Видели: {esc(seen)}" if seen else "Из группы ещё никто не оценил.")
    mine = st.db.scores_by_tmdb(uid).get((kind, tid), "нет")
    if mine != "нет":
        lines.append(f"Твоя оценка: {mine}" if mine else "Ты отметил(а): не смотрел(а)")
    if it["watched_at"]:
        lines.append(f"✅ Просмотрено {day(it['watched_at'])}")
    tail = f"{lid}:{kind}:{tid}:{page}:{w}"
    rows = []
    rows.append([B(text="⭐ Оценить", callback_data=f"Lr:{tail}")])
    row2 = []
    if can_edit(st, lst, uid):
        row2.append(B(text="↩ Не смотрели" if it["watched_at"] else "✅ Посмотрели", callback_data=f"Lw:{tail}"))
    if may_download(st, uid):
        row2.append(B(text="⬇ Найти раздачи", callback_data=f"Ld:{kind}:{tid}"))
    rows.append(row2)
    rows.append([B(text="🎞 Подробнее", callback_data=f"Lc:{kind}:{tid}"),
                 B(text="➕ В другой список", callback_data=f"La:{kind}:{tid}")])
    if can_remove(st, lst, it, uid):
        rows.append([B(text="🗑 Убрать из списка", callback_data=f"Ly:{tail}")])
    rows.append(back)
    return "\n".join(lines), kb(rows)


def targets(st, uid: int) -> list:
    """Списки, куда человек может добавлять: свои, потом групповые."""
    out = list(st.db.lists_of_user(uid))
    for g in st.db.groups_of(uid):
        out += list(st.db.lists_of_group(g["id"]))
    return out


def add_view(st, kind: str, tid: int, uid: int, title: str) -> tuple[str, InlineKeyboardMarkup]:
    have = st.db.lists_with(kind, tid)
    items, lines = [], []
    for n, lst in enumerate(targets(st, uid), 1):
        mark = "✅ " if lst["id"] in have else ""
        items.append((n, f"{mark}{list_title(st, lst, uid)[:45]}", f"Lt:{lst['id']}:{kind}:{tid}"))
        lines.append(f"<b>{n}.</b> {mark}{esc(list_title(st, lst, uid))}")
    rows, numbers = picks.numbered(items, 5)               # v8.1: много списков — номер текстом
    rows.append([B(text="Готово", callback_data="Lk")])
    text = f"➕ Куда добавить {icon(kind)} <b>{esc(title)}</b>?\n✅ — уже там (ещё раз — убрать)."
    if numbers:
        text += "\n\n" + "\n".join(lines)
    return picks.finish(text, numbers, "добавить / убрать"), kb(rows)


def score_rows(tail: str, current: int | None) -> list[list[B]]:
    def b(n: int) -> B:
        return B(text=f"·{n}·" if n == current else str(n), callback_data=f"Lq:{tail}:{n}")
    return [[b(n) for n in range(1, 6)], [b(n) for n in range(6, 11)],
            [B(text="Не смотрел(а)", callback_data=f"Lq:{tail}:0")]]


# ---------- группа ----------
def groups_view(st, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    groups = st.db.groups_of(uid)
    admin = uid in st.cfg.admin_ids
    lines = ["👥 <b>Семья</b> — общий список фильмов."]
    rows = []
    if not groups:
        lines.append("\nТы пока не в группе. Состав группы задаёт администратор бота."
                     if not admin else "\nГруппы ещё нет — создай, потом добавь людей.")
    for g in groups:
        n = len(st.db.group_members(g["id"]))
        lines.append(f"• {esc(g['name'])} — {n} чел.")
        rows.append([B(text=f"👥 {g['name'][:40]}", callback_data=f"Gv:{g['id']}")])
    if admin and not groups:
        rows.append([B(text="➕ Создать группу «Семья»", callback_data="Gn")])
    rows.append([B(text="📋 Списки", callback_data="Lm")])
    return "\n".join(lines), kb(rows)


def group_view(st, gid: int, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    g = st.db.group_get(gid)
    if g is None or not st.db.is_member(gid, uid):
        return "Этой группы нет или ты в ней больше не состоишь.", kb([[B(text="◀ Группы", callback_data="Gm")]])
    members = st.db.group_members(gid)
    lines = [f"👥 <b>{esc(g['name'])}</b>",
             "Участники: " + ", ".join(esc(name_of(st, m["user_id"])) for m in members)]
    main = st.db.group_main_list(gid)
    rows = [[B(text="📋 Список группы", callback_data=f"Lv:{main}:0:0"),
             B(text="🎯 Что посмотреть вместе", callback_data=f"Rg:{gid}")]]
    if uid in st.cfg.admin_ids:
        rows.append([B(text="➕ Добавить человека", callback_data=f"Ga:{gid}"),
                     B(text="👤 Убрать человека", callback_data=f"Gk:{gid}")])
    rows.append([B(text="◀ Группы", callback_data="Gm")])
    return "\n".join(lines), kb(rows)


# ---------- обработчики ----------
def build_router(st) -> Router:
    r = Router()
    st.awaiting = getattr(st, "awaiting", {})

    def allowed(uid: int) -> bool:
        return st.is_allowed(uid)

    async def edit(cb: CallbackQuery, text: str, markup) -> None:
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception as e:
            if "not modified" in str(e):
                return
            await cb.message.answer(text, reply_markup=markup)

    async def deny(cb: CallbackQuery) -> bool:
        if allowed(cb.from_user.id):
            st.db.touch(cb.from_user.id)
            return False
        await cb.answer("Нет доступа", show_alert=True)
        return True

    def ask_input(uid: int, what: tuple) -> None:
        st.awaiting[uid] = (time.time(), what)

    # --- меню ---
    @r.message(Command("lists", "want", "spiski"))
    async def lists_cmd(msg: Message):
        if not allowed(msg.from_user.id):
            return
        st.db.touch(msg.from_user.id)
        text, markup = menu_view(st, msg.from_user.id)
        await msg.answer(text, reply_markup=markup)

    @r.callback_query(F.data == "Lm")
    async def menu_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        await cb.answer()
        await edit(cb, *menu_view(st, cb.from_user.id))

    @r.callback_query(F.data.regexp(r"^Lv:\d+:\d+:[01]$"))
    async def view_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        _, lid, page, w = cb.data.split(":")
        await cb.answer()
        await edit(cb, *list_view(st, int(lid), cb.from_user.id, int(page), w == "1"))

    @r.callback_query(F.data.regexp(r"^Li:\d+:[mt]:\d+:\d+:[01]$"))
    async def item_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        _, lid, kind, tid, page, w = cb.data.split(":")
        await cb.answer()
        await edit(cb, *item_view(st, int(lid), kind, int(tid), cb.from_user.id, int(page), int(w)))

    def item_args(data: str) -> tuple[int, str, int, int, int]:
        p = data.split(":")
        return int(p[1]), p[2], int(p[3]), int(p[4]), int(p[5])

    @r.callback_query(F.data.regexp(r"^Lw:\d+:[mt]:\d+:\d+:[01]$"))
    async def watched_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        lid, kind, tid, page, w = item_args(cb.data)
        uid = cb.from_user.id
        lst = st.db.list_get(lid)
        it = st.db.list_item(lid, kind, tid) if lst else None
        if not it or not can_edit(st, lst, uid):
            return await cb.answer("Нельзя", show_alert=True)
        on = not it["watched_at"]
        st.db.set_watched(lid, kind, tid, on, uid)
        await cb.answer("✅ Отмечено: посмотрели" if on else "Вернул в непросмотренные")
        if on and st.db.scores_by_tmdb(uid).get((kind, tid), "нет") == "нет":
            t, k = item_view(st, lid, kind, tid, uid, page, w)
            return await edit(cb, t + "\n\n⭐ Оцени, как тебе:", kb(score_rows(f"{lid}:{kind}:{tid}:{page}:{w}", None)
                                                                     + list(k.inline_keyboard[-1:])))
        await edit(cb, *item_view(st, lid, kind, tid, uid, page, w))

    @r.callback_query(F.data.regexp(r"^Lr:\d+:[mt]:\d+:\d+:[01]$"))
    async def rate_open(cb: CallbackQuery):
        if await deny(cb):
            return
        lid, kind, tid, page, w = item_args(cb.data)
        uid = cb.from_user.id
        cur = st.db.scores_by_tmdb(uid).get((kind, tid))
        info = await info_for(st, kind, tid)
        title = f"{icon(kind)} <b>{esc(tmdb.short_label(info, 80) if info else '?')}</b>"
        back = [[B(text="◀ Назад", callback_data=f"Li:{lid}:{kind}:{tid}:{page}:{w}")]] if lid else []
        await cb.answer()
        markup = kb(score_rows(f"{lid}:{kind}:{tid}:{page}:{w}", cur) + back)
        if lid:
            await edit(cb, f"⭐ Как тебе {title}? Оценка одна на фильм — видна в /ocenki и в списках.", markup)
        else:
            await cb.message.answer(f"⭐ Как тебе {title}? Оценка одна на фильм — видна в /ocenki и в списках.",
                                    reply_markup=markup)

    @r.callback_query(F.data.regexp(r"^Lq:\d+:[mt]:\d+:\d+:[01]:\d+$"))
    async def rate_set(cb: CallbackQuery):
        if await deny(cb):
            return
        p = cb.data.split(":")
        lid, kind, tid, page, w, n = int(p[1]), p[2], int(p[3]), int(p[4]), int(p[5]), int(p[6])
        uid = cb.from_user.id
        if not 0 <= n <= 10:
            return await cb.answer()
        info = await info_for(st, kind, tid)
        if info is None:
            return await cb.answer("TMDB не ответил, попробуй позже", show_alert=True)
        jid = st.db.journal_for_title(kind, tid, tmdb.short_label(info, 150), info.poster)
        st.db.rate(jid, uid, n or None)
        if n:
            st.db.watched_personal(uid, kind, tid)
        log.info("оценка %s из списка: %s → %s", uid, info.title, n or "не смотрел")
        await cb.answer(f"⭐ {n}/10" if n else "Ок, не смотрел(а)")
        if lid:
            return await edit(cb, *item_view(st, lid, kind, tid, uid, page, w))
        await edit(cb, f"⭐ {icon(kind)} <b>{esc(tmdb.short_label(info, 80))}</b> — "
                       + (f"<b>{n}</b>/10. Записал." if n else "не смотрел(а). Записал.") + " Все оценки — /ocenki", None)

    @r.callback_query(F.data.regexp(r"^Ly:\d+:[mt]:\d+:\d+:[01]$"))
    async def remove_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        lid, kind, tid, page, w = item_args(cb.data)
        lst = st.db.list_get(lid)
        it = st.db.list_item(lid, kind, tid) if lst else None
        if not it or not can_remove(st, lst, it, cb.from_user.id):
            return await cb.answer("Убрать может тот, кто добавил, или владелец группы", show_alert=True)
        st.db.list_remove(lid, kind, tid)
        await cb.answer("Убрано из списка")
        await edit(cb, *list_view(st, lid, cb.from_user.id, page, w == 1))

    @r.callback_query(F.data.regexp(r"^Ld:[mt]:\d+$"))
    async def download_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        if not may_download(st, cb.from_user.id):
            return await cb.answer(NO_DL, show_alert=True)
        _, kind, tid = cb.data.split(":")
        info = await info_for(st, kind, int(tid), full=True)       # нужно оригинальное название для трекеров
        if not info:
            return await cb.answer("TMDB не ответил, попробуй позже", show_alert=True)
        await cb.answer()
        await st.hooks["search_for"](cb.message, info, uid=cb.from_user.id)

    @r.callback_query(F.data.regexp(r"^Lc:[mt]:\d+$"))
    async def card_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        _, kind, tid = cb.data.split(":")
        info = await info_for(st, kind, int(tid), full=True)
        if not info:
            return await cb.answer("TMDB не ответил, попробуй позже", show_alert=True)
        await cb.answer()
        await st.hooks["show_card"](cb.message, info, cb.from_user.id)

    # --- добавить в список ---
    async def open_add(cb: CallbackQuery, kind: str, tid: int) -> None:
        uid = cb.from_user.id
        info = await info_for(st, kind, tid)
        if not info:
            return await cb.answer("TMDB не ответил, попробуй позже", show_alert=True)
        remember_info(st, info)
        await cb.answer()
        text, markup = add_view(st, kind, tid, uid, tmdb.short_label(info, 80))
        await cb.message.answer(text, reply_markup=markup)

    @r.callback_query(F.data.regexp(r"^La:[mt]:\d+$"))
    async def add_open(cb: CallbackQuery):
        if await deny(cb):
            return
        _, kind, tid = cb.data.split(":")
        await open_add(cb, kind, int(tid))

    st.hooks["open_add"] = open_add

    @r.callback_query(F.data.regexp(r"^Lt:\d+:[mt]:\d+$"))
    async def add_toggle(cb: CallbackQuery):
        if await deny(cb):
            return
        _, lid, kind, tid = cb.data.split(":")
        lid, tid, uid = int(lid), int(tid), cb.from_user.id
        lst = st.db.list_get(lid)
        if not can_edit(st, lst, uid):
            return await cb.answer("В этот список добавлять нельзя", show_alert=True)
        it = st.db.list_item(lid, kind, tid)
        if it:
            if not can_remove(st, lst, it, uid):
                return await cb.answer("Уже там. Убрать может тот, кто добавил, или владелец группы", show_alert=True)
            st.db.list_remove(lid, kind, tid)
            note = f"Убрано из «{lst['name']}»"
        else:
            if st.db.title_get(kind, tid) is None:
                info = await info_for(st, kind, tid)
                if info:
                    remember_info(st, info)
            st.db.list_add(lid, kind, tid, uid)
            if st.db.scores_by_tmdb(uid).get((kind, tid)) and lst["user_id"] == uid:
                st.db.set_watched(lid, kind, tid, True)          # уже оценил — сразу ✅
            note = f"Добавлено в «{list_title(st, lst, uid)}»"
        await cb.answer(note)
        row = st.db.title_get(kind, tid)
        await edit(cb, *add_view(st, kind, tid, uid, label(row) if row else "фильм"))

    @r.callback_query(F.data == "Lk")
    async def close_cb(cb: CallbackQuery):
        st.awaiting.pop(cb.from_user.id, None)
        await cb.answer()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    # --- случайный из списка ---
    @r.callback_query(F.data.regexp(r"^Lz:\d+$"))
    async def random_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        lid, uid = int(cb.data[3:]), cb.from_user.id
        lst = st.db.list_get(lid)
        if role(st, lst, uid) is None:
            return await cb.answer("Список недоступен", show_alert=True)
        items = visible_items(st, lid, uid, False)
        if not items:
            return await cb.answer("В списке нет непросмотренного", show_alert=True)
        it = random.choice(items)
        info = await info_for(st, it["kind"], it["tmdb_id"], full=True)
        await cb.answer()
        if not info:
            return await cb.message.answer(f"🎲 {esc(label(it))}")
        await st.hooks["show_card"](cb.message, info, uid,
                                    head=f"🎲 Из списка «{esc(list_title(st, lst, uid))}»:",
                                    extra=[[B(text="🎲 Ещё", callback_data=f"Lz:{lid}"),
                                            B(text="📋 К списку", callback_data=f"Lv:{lid}:0:0")]])

    # --- группы ---
    @r.message(Command("gruppy", "groups"))
    async def groups_cmd(msg: Message):
        if not allowed(msg.from_user.id):
            return
        text, markup = groups_view(st, msg.from_user.id)
        await msg.answer(text, reply_markup=markup)

    @r.callback_query(F.data == "Gm")
    async def groups_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        await cb.answer()
        await edit(cb, *groups_view(st, cb.from_user.id))

    @r.callback_query(F.data.regexp(r"^Gv:\d+$"))
    async def group_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        await cb.answer()
        await edit(cb, *group_view(st, int(cb.data[3:]), cb.from_user.id))

    @r.callback_query(F.data == "Gn")
    async def group_new(cb: CallbackQuery):
        if cb.from_user.id not in st.cfg.admin_ids:
            return await cb.answer("Только для администратора бота", show_alert=True)
        if not st.db.groups_of(cb.from_user.id):
            st.db.group_create("Семья", cb.from_user.id)
        await cb.answer("Группа создана")
        await edit(cb, *groups_view(st, cb.from_user.id))

    # старые кнопки из прежних версий (приглашения, подборки, 👍, переименование, выход)
    @r.callback_query(F.data.regexp(r"^(L[nesoxXu]|G[eilLxX])(:|$)"))
    async def removed_cb(cb: CallbackQuery):
        await cb.answer("Эту кнопку убрали: списки стали проще — /lists", show_alert=True)

    def owner_of(gid: int, uid: int):
        g = st.db.group_get(gid)
        return g if g is not None and uid in st.cfg.admin_ids else None

    @r.callback_query(F.data.regexp(r"^G[kK]:\d+(:\d+)?$"))
    async def group_kick(cb: CallbackQuery, bot: Bot):
        if await deny(cb):
            return
        p = cb.data.split(":")
        g = owner_of(int(p[1]), cb.from_user.id)
        if not g:
            return await cb.answer("Только для администратора бота", show_alert=True)
        if p[0] == "Gk":
            others = [m for m in st.db.group_members(g["id"]) if m["user_id"] != cb.from_user.id]
            if not others:
                return await cb.answer("В группе больше никого нет", show_alert=True)
            await cb.answer()
            items = [(n, f"✖ {name_of(st, m['user_id'])}", f"GK:{g['id']}:{m['user_id']}")
                     for n, m in enumerate(others, 1)]
            rows, numbers = picks.numbered(items, 6)
            names = "\n".join(f"<b>{n}.</b> {esc(t[2:])}" for n, t, _ in items) if numbers else ""
            return await edit(cb, picks.finish(f"Кого убрать из «{esc(g['name'])}»?" + (f"\n\n{names}" if names else ""),
                                               numbers, "убрать"),
                              kb(rows + [[B(text="◀ Назад", callback_data=f"Gv:{g['id']}")]]))
        who = int(p[2])
        st.db.group_leave(g["id"], who)
        try:
            await bot.send_message(who, f"👥 Тебя убрали из группы «{esc(g['name'])}».")
        except Exception:
            pass
        await cb.answer(f"{name_of(st, who)} больше не в группе")
        await edit(cb, *group_view(st, g["id"], cb.from_user.id))

    @r.callback_query(F.data.regexp(r"^G[aA]:\d+(:\d+)?$"))
    async def group_add_user(cb: CallbackQuery, bot: Bot):
        if cb.from_user.id not in st.cfg.admin_ids:
            return await cb.answer("Только для администратора бота", show_alert=True)
        p = cb.data.split(":")
        gid = int(p[1])
        g = st.db.group_get(gid)
        if g is None:
            return await cb.answer("Группы уже нет", show_alert=True)
        if p[0] == "Ga":
            members = {m["user_id"] for m in st.db.group_members(gid)}
            cand = [u for u in st.db.users() if u["id"] not in members]
            await cb.answer()
            if not cand:
                return await edit(cb, "Все пользователи бота уже в группе.",
                                  kb([[B(text="◀ Назад", callback_data=f"Gv:{gid}")]]))
            items = [(n, f"➕ {name_of(st, u['id'])}", f"GA:{gid}:{u['id']}") for n, u in enumerate(cand[:50], 1)]
            rows, numbers = picks.numbered(items, 6)
            names = "\n".join(f"<b>{n}.</b> {esc(t[2:])}" for n, t, _ in items) if numbers else ""
            return await edit(cb, picks.finish(f"Кого добавить в «{esc(g['name'])}»?" + (f"\n\n{names}" if names else ""),
                                               numbers, "добавить"),
                              kb(rows + [[B(text="◀ Назад", callback_data=f"Gv:{gid}")]]))
        who = int(p[2])
        if st.db.group_join(gid, who):
            try:
                await bot.send_message(who, f"👥 Тебя добавили в группу «{esc(g['name'])}». Общий список — /lists.")
            except Exception:
                pass
        await cb.answer(f"{name_of(st, who)} теперь в группе")
        await edit(cb, *group_view(st, gid, cb.from_user.id))

    # --- ввод текста (запрос к ИИ-подбору) ---
    async def text_input(msg: Message) -> bool:
        uid = msg.from_user.id
        got = st.awaiting.get(uid)
        if not got:
            return False
        ts, what = got
        if time.time() - ts > INPUT_TTL:
            st.awaiting.pop(uid, None)
            return False
        if what[0] == "ai_rec":                       # запрос к ИИ-подбору — обрабатывает recs
            st.awaiting.pop(uid, None)
            await st.hooks["ai_rec"](msg, msg.text.strip()[:300])
            return True
        return False

    st.hooks["text_input"] = text_input
    st.hooks["ask_input"] = ask_input
    st.hooks["random_rows"] = lambda uid: random_rows(st, uid)
    return r


def random_rows(st, uid: int) -> list[list[B]]:
    """Кнопки «🎲 Из списка …» для /random — списки, где есть непросмотренное."""
    out = []
    lists = [st.db.list_get(st.db.main_list(uid))] + [st.db.list_get(st.db.group_main_list(g["id"]))
                                                      for g in st.db.groups_of(uid)]
    for lst in lists:
        if lst is not None and visible_items(st, lst["id"], uid, False):
            out.append([B(text=f"🎲 Из списка «{list_title(st, lst, uid)[:35]}»", callback_data=f"Lz:{lst['id']}")])
    return out[:4]


def mark_kodi_watched(st, row) -> int:
    """Kodi отметил просмотренным то, что скачали: ✅ в списках того, кто качал (и его групп)."""
    if row["tmdb_id"] and row["tmdb_kind"] and row["user_id"]:
        return st.db.watched_kodi(row["user_id"], row["tmdb_kind"], row["tmdb_id"])
    return 0
