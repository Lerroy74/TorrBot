"""v8: списки, подборки, группы и «поделиться».

* /lists (и /want) — мои списки, списки моих групп, открытые мне;
* у каждого личный «Хочу посмотреть» + сколько угодно подборок; у группы — общий список + подборки;
* «➕ В список» под карточкой фильма — выбрать, куда (✅ — уже там; нажать ещё раз — убрать);
* карточка фильма в списке: 👍 (группа), ⭐ оценить, ✅ посмотрели, ⬇ раздачи (если можно качать),
  🎞 подробнее, ➕ в другой список / себе, 🗑 убрать;
* /gruppy — группы: создать, ссылка-приглашение, участники, выйти (владелец уходит — группа переходит
  самому давнему участнику; ушёл последний — группа удаляется);
* «🔗 Поделиться» личным списком — ссылка; получатель видит список только для чтения.

Права: личный список — владелец; групповой — участники (убрать фильм — кто добавил или владелец
группы); открытый — только чтение. Callback-префиксы: L… (списки), G… (группы).
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

from . import tmdb
from .access import NO_DL, may_download

log = logging.getLogger("torrbot")
esc = html.escape

PAGE = 10
INPUT_TTL = 600
KIDS_GENRES = tmdb.KIDS_GENRES


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


def is_kid(st, uid: int) -> bool:
    from .kids import is_kid as k
    return k(st, uid)


def kid_ok_row(row) -> bool:
    g = {int(x) for x in (row["genres"] or "").split(",") if x.strip().isdigit()}
    return bool(g & KIDS_GENRES)


def label(row) -> str:
    t = (row["title"] if row is not None and row["title"] else "") or "?"
    y = row["year"] if row is not None and row["year"] else ""
    return f"{t} ({y})" if y else t


def icon(kind: str) -> str:
    return "📺" if kind == "t" else "🎬"


# ---------- права ----------
def role(st, lst, uid: int) -> str | None:
    """owner — личный список этого человека; member — участник группы; viewer — открыт ему; None — нет доступа."""
    if lst is None:
        return None
    if lst["user_id"]:
        if lst["user_id"] == uid:
            return "owner"
        return "viewer" if st.db.is_shared_with(lst["id"], uid) else None
    if lst["group_id"] and st.db.is_member(lst["group_id"], uid):
        return "member"
    return None


def can_edit(st, lst, uid: int) -> bool:
    return role(st, lst, uid) in ("owner", "member")


def can_remove(st, lst, item, uid: int) -> bool:
    r = role(st, lst, uid)
    if r == "owner":
        return True
    if r == "member":
        g = st.db.group_get(lst["group_id"])
        return item["added_by"] == uid or (g is not None and g["owner_id"] == uid)
    return False


def can_manage(st, lst, uid: int) -> bool:
    """Переименовать / удалить подборку (основной список удалить нельзя)."""
    r = role(st, lst, uid)
    if r == "owner":
        return True
    if r == "member":
        g = st.db.group_get(lst["group_id"])
        return lst["created_by"] == uid or (g is not None and g["owner_id"] == uid)
    return False


def list_title(st, lst, uid: int | None = None) -> str:
    """«Хочу посмотреть», «Семья: Хотим посмотреть», «Лена: Ужастики»."""
    if lst["group_id"]:
        g = st.db.group_get(lst["group_id"])
        return f"{g['name'] if g else '?'}: {lst['name']}"
    if uid is not None and lst["user_id"] != uid:
        return f"{name_of(st, lst['user_id'])}: {lst['name']}"
    return lst["name"]


def visible_items(st, lid: int, uid: int, watched: bool) -> list:
    items = st.db.list_items(lid, watched)
    if is_kid(st, uid):
        items = [i for i in items if kid_ok_row(i)]
    return items


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
    lines, rows = ["📋 <b>Списки</b>"], []

    def entry(lst, title: str) -> None:
        todo, total = st.db.list_counts(lst["id"])
        lines.append(f"• {esc(title)} — {todo}" + (f" (всего {total})" if total != todo else ""))
        rows.append([B(text=f"{'📋' if not lst['group_id'] else '👥'} {title[:40]} ({todo})",
                       callback_data=f"Lv:{lst['id']}:0:0")])

    lines.append("\n<b>Мои</b>")
    for lst in st.db.lists_of_user(uid):
        entry(lst, lst["name"])
    groups = st.db.groups_of(uid)
    if groups:
        lines.append("\n<b>Группы</b>")
        for g in groups:
            for lst in st.db.lists_of_group(g["id"]):
                entry(lst, f"{g['name']}: {lst['name']}")
    shared = st.db.shared_with(uid)
    if shared:
        lines.append("\n<b>Открыли мне</b>")
        for lst in shared:
            entry(lst, list_title(st, lst, uid))
    rows.append([B(text="➕ Новая подборка", callback_data="Ln:0"), B(text="👥 Группы", callback_data="Gm")])
    lines.append("\nДобавлять фильмы — кнопкой «➕ В список» под карточкой фильма.")
    return "\n".join(lines), kb(rows)


def list_view(st, lid: int, uid: int, page: int = 0, show_watched: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    lst = st.db.list_get(lid)
    r = role(st, lst, uid)
    if r is None:
        return "Этого списка нет или он тебе больше не доступен.", kb([[B(text="◀ Все списки", callback_data="Lm")]])
    if r == "viewer":                     # чужой открытый список: видно всё — просмотренное автором тоже совет
        show_watched = True
    items = visible_items(st, lid, uid, show_watched)
    todo, total = st.db.list_counts(lid)
    group = lst["group_id"]
    people = {m["user_id"] for m in st.db.group_members(group)} if group else None
    head = f"{'👥' if group else '📋'} <b>{esc(list_title(st, lst, uid))}</b> — не смотрели {todo}"
    if total != todo:
        head += f", ✅ {total - todo}"
    if r == "viewer":
        head = (f"🔗 <b>{esc(list_title(st, lst, uid))}</b> — {len(items)} фильм.\n"
                f"Открыт тебе для просмотра: «➕ Себе» — забрать фильм в свой список.")
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
        if group and it["votes"]:
            bits.append(f"👍 {it['votes']}")
        if group:
            seen = seen_marks(st, it["kind"], it["tmdb_id"], people)
            if seen:
                bits.append(f"видели: {seen}")
        elif r == "owner":
            mine = st.db.scores_by_tmdb(uid).get((it["kind"], it["tmdb_id"]))
            if mine:
                bits.append(f"ты: {mine}")
        else:
            owner = seen_marks(st, it["kind"], it["tmdb_id"], {lst["user_id"]})
            if owner:
                bits.append(f"автор: {owner.split(' ')[-1]}")
        done = "✅ " if it["watched_at"] and r != "viewer" else ""
        lines.append(f"<b>{i + 1}.</b> {done}{icon(it['kind'])} {esc(label(it))}" + (f" — {esc(' · '.join(bits))}"
                                                                                    if bits else ""))
        btns.append(B(text=str(i + 1), callback_data=f"Li:{lid}:{it['kind']}:{it['tmdb_id']}:{page}:{w}"))
    rows = [btns[j:j + 5] for j in range(0, len(btns), 5)]
    nav = []
    if page > 0:
        nav.append(B(text="◀", callback_data=f"Lv:{lid}:{page - 1}:{w}"))
    if page < pages - 1:
        nav.append(B(text="▶", callback_data=f"Lv:{lid}:{page + 1}:{w}"))
    rows.append(nav)
    extra = []
    if total != todo and r != "viewer":
        extra.append(B(text="🙈 Скрыть просмотренные" if show_watched else "👁 Показать просмотренные",
                       callback_data=f"Lv:{lid}:0:{1 - w}"))
    if todo:
        extra.append(B(text="🎲 Случайный", callback_data=f"Lz:{lid}"))
    rows.append(extra)
    tools = []
    if r == "owner":
        tools.append(B(text="🔗 Поделиться", callback_data=f"Ls:{lid}"))
    if group:
        tools.append(B(text="👥 Группа", callback_data=f"Gv:{group}"))
    if can_manage(st, lst, uid) and not lst["main"]:
        tools.append(B(text="✏ Название", callback_data=f"Le:{lid}"))
        tools.append(B(text="🗑 Удалить", callback_data=f"Lx:{lid}"))
    rows.append(tools)
    rows.append([B(text="◀ Все списки", callback_data="Lm")])
    if len(lines) > 1:
        lines.append("\nНажми номер — карточка фильма.")
    return "\n".join(lines), kb(rows)


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
        voters = [name_of(st, int(v)) for v in (it["voters"] or "").split(",") if v]
        lines.append(f"👍 {len(voters)}" + (f": {esc(', '.join(voters))}" if voters else ""))
        people = {m["user_id"] for m in st.db.group_members(group)}
        seen = seen_marks(st, kind, tid, people)
        lines.append(f"Видели: {esc(seen)}" if seen else "Из группы ещё никто не оценил.")
    elif r == "viewer":
        owner = seen_marks(st, kind, tid, {lst["user_id"]})
        if owner:
            lines.append(f"Оценка автора: {esc(owner.split(' ')[-1])}")
    mine = st.db.scores_by_tmdb(uid).get((kind, tid), "нет")
    if mine != "нет":
        lines.append(f"Твоя оценка: {mine}" if mine else "Ты отметил(а): не смотрел(а)")
    if it["watched_at"]:
        lines.append(f"✅ Просмотрено {day(it['watched_at'])}")
    tail = f"{lid}:{kind}:{tid}:{page}:{w}"
    rows = []
    row1 = []
    if group:
        voted = uid in {int(v) for v in (it["voters"] or "").split(",") if v}
        row1.append(B(text=("👍 Убрать голос" if voted else "👍 Хочу"), callback_data=f"Lu:{tail}"))
    row1.append(B(text="⭐ Оценить", callback_data=f"Lr:{tail}"))
    rows.append(row1)
    row2 = []
    if can_edit(st, lst, uid):
        row2.append(B(text="↩ Не смотрели" if it["watched_at"] else "✅ Посмотрели", callback_data=f"Lw:{tail}"))
    if may_download(st, uid):
        row2.append(B(text="⬇ Найти раздачи", callback_data=f"Ld:{kind}:{tid}"))
    rows.append(row2)
    rows.append([B(text="🎞 Подробнее", callback_data=f"Lc:{kind}:{tid}"),
                 B(text="➕ Себе" if r == "viewer" else "➕ В другой список", callback_data=f"La:{kind}:{tid}")])
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
    rows = []
    for lst in targets(st, uid):
        mark = "✅ " if lst["id"] in have else ""
        rows.append([B(text=f"{mark}{list_title(st, lst, uid)[:45]}", callback_data=f"Lt:{lst['id']}:{kind}:{tid}")])
    rows.append([B(text="➕ Новая подборка с этим фильмом", callback_data=f"Ln:0:{kind}:{tid}")])
    rows.append([B(text="Готово", callback_data="Lk")])
    return (f"➕ Куда добавить {icon(kind)} <b>{esc(title)}</b>?\n✅ — уже там (нажми ещё раз, чтобы убрать).",
            kb(rows))


def score_rows(tail: str, current: int | None) -> list[list[B]]:
    def b(n: int) -> B:
        return B(text=f"·{n}·" if n == current else str(n), callback_data=f"Lq:{tail}:{n}")
    return [[b(n) for n in range(1, 6)], [b(n) for n in range(6, 11)],
            [B(text="Не смотрел(а)", callback_data=f"Lq:{tail}:0")]]


def share_view(st, lid: int, uid: int, bot_name: str) -> tuple[str, InlineKeyboardMarkup]:
    lst = st.db.list_get(lid)
    code = st.db.list_share_code(lid)
    link = f"https://t.me/{bot_name}?start=l_{code}" if bot_name else f"/start l_{code}"
    lines = [f"🔗 <b>Поделиться «{esc(lst['name'])}»</b>",
             f"Отправь ссылку тем, кому хочешь показать список (только просмотр):\n<code>{esc(link)}</code>"]
    shares = st.db.shares_of(lid)
    rows = []
    if shares:
        lines.append("\nСейчас открыт: " + ", ".join(esc(name_of(st, s["user_id"])) for s in shares))
        rows += [[B(text=f"✖ Закрыть для {name_of(st, s['user_id'])}", callback_data=f"Lo:{lid}:{s['user_id']}")]
                 for s in shares[:10]]
    rows.append([B(text="⛔ Закрыть для всех (ссылка сменится)", callback_data=f"Lo:{lid}:0")])
    rows.append([B(text="◀ К списку", callback_data=f"Lv:{lid}:0:0")])
    return "\n".join(lines), kb(rows)


# ---------- группы ----------
def groups_view(st, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    groups = st.db.groups_of(uid)
    lines = ["👥 <b>Группы</b> — общие списки с семьёй и друзьями."]
    rows = []
    if not groups:
        lines.append("\nТы пока не в группе. Создай свою и разошли ссылку-приглашение.")
    for g in groups:
        n = len(st.db.group_members(g["id"]))
        crown = " 👑" if g["owner_id"] == uid else ""
        lines.append(f"• {esc(g['name'])}{crown} — {n} чел.")
        rows.append([B(text=f"👥 {g['name'][:40]}", callback_data=f"Gv:{g['id']}")])
    rows.append([B(text="➕ Создать группу", callback_data="Gn"), B(text="📋 Списки", callback_data="Lm")])
    return "\n".join(lines), kb(rows)


def group_view(st, gid: int, uid: int, bot_name: str) -> tuple[str, InlineKeyboardMarkup]:
    g = st.db.group_get(gid)
    if g is None or not st.db.is_member(gid, uid):
        return "Этой группы нет или ты в ней больше не состоишь.", kb([[B(text="◀ Группы", callback_data="Gm")]])
    owner = g["owner_id"] == uid
    members = st.db.group_members(gid)
    link = f"https://t.me/{bot_name}?start=g_{g['invite']}" if bot_name else f"/start g_{g['invite']}"
    lines = [f"👥 <b>{esc(g['name'])}</b>",
             "Участники: " + ", ".join(esc(name_of(st, m["user_id"])) + (" 👑" if m["user_id"] == g["owner_id"] else "")
                                       for m in members),
             f"\nПриглашение (перешли тому, кого зовёшь):\n<code>{esc(link)}</code>"]
    main = st.db.group_main_list(gid)
    rows = [[B(text="📋 Список группы", callback_data=f"Lv:{main}:0:0"),
             B(text="🎯 Что посмотреть вместе", callback_data=f"Rg:{gid}")],
            [B(text="➕ Подборка группы", callback_data=f"Ln:{gid}")]]
    others = [lst for lst in st.db.lists_of_group(gid) if not lst["main"]]
    for lst in others[:8]:
        rows.append([B(text=f"📁 {lst['name'][:40]}", callback_data=f"Lv:{lst['id']}:0:0")])
    if owner:
        rows.append([B(text="✏ Название", callback_data=f"Ge:{gid}"), B(text="🔄 Новая ссылка", callback_data=f"Gi:{gid}")])
        rows.append([B(text="👤 Убрать участника", callback_data=f"Gk:{gid}")])
    if uid in st.cfg.admin_ids:
        rows.append([B(text="➕ Добавить из пользователей бота", callback_data=f"Ga:{gid}")])
    rows.append([B(text="🚪 Выйти", callback_data=f"Gl:{gid}")] + ([B(text="🗑 Удалить группу", callback_data=f"Gx:{gid}")]
                                                                   if owner else []))
    rows.append([B(text="◀ Группы", callback_data="Gm")])
    return "\n".join(lines), kb(rows)


# ---------- переход с 7.x ----------
def setup(st) -> None:
    """Один раз: «Хотим посмотреть» (/want) → группа «Семья» первого админа (голоса сохраняются)."""
    db = st.db
    if db.pref(0, "v8_family") == "1":
        return
    rows = db.c.execute("SELECT * FROM wishlist ORDER BY added_at").fetchall()
    if rows and st.cfg.admin_ids:
        admin = st.cfg.admin_ids[0]
        gid = db.group_create("Семья", admin)
        lid = db.group_main_list(gid)
        for w in rows:
            db.title_put(w["kind"], w["tmdb_id"], w["title"], w["year"] or "", w["poster"])
            db.c.execute("INSERT OR IGNORE INTO list_items(list_id, kind, tmdb_id, added_by, added_at) VALUES (?,?,?,?,?)",
                         (lid, w["kind"], w["tmdb_id"], w["added_by"], w["added_at"]))
        db.c.execute("INSERT OR IGNORE INTO list_votes(list_id, kind, tmdb_id, user_id)"
                     " SELECT ?, kind, tmdb_id, user_id FROM votes", (lid,))
        log.info("v8: «Хотим посмотреть» (%d) → группа «Семья»", len(rows))
    db.set_pref(0, "v8_family", "1")
    db.c.commit()


# ---------- приглашения (deep link /start g_… / l_…) ----------
def parse_invite(st, arg: str) -> tuple[str, object] | None:
    if arg.startswith("g_"):
        g = st.db.group_by_invite(arg[2:])
        return ("g", g) if g else None
    if arg.startswith("l_"):
        lst = st.db.list_by_share(arg[2:])
        return ("l", lst) if lst else None
    return None


def invite_note(st, arg: str) -> str:
    """Для заявки админу: «приглашение в группу «Семья» от Лена»."""
    got = parse_invite(st, arg)
    if not got:
        return ""
    kind, obj = got
    if kind == "g":
        return f"приглашение в группу «{obj['name']}» от {name_of(st, obj['owner_id'])}"
    return f"открытый список «{obj['name']}» от {name_of(st, obj['user_id'])}"


async def accept_invite(bot: Bot, st, uid: int, arg: str) -> str:
    """Человек (уже допущенный) перешёл по ссылке. Вернёт текст ответа."""
    got = parse_invite(st, arg)
    if not got:
        return "Ссылка устарела — попроси прислать новую."
    kind, obj = got
    if kind == "g":
        if not st.db.group_join(obj["id"], uid):
            return f"Ты уже в группе «{esc(obj['name'])}» — /gruppy"
        try:
            if obj["owner_id"] != uid:
                await bot.send_message(obj["owner_id"], f"👥 {esc(name_of(st, uid))} вступил(а) в группу "
                                                         f"«{esc(obj['name'])}».")
        except Exception as e:
            log.info("не смог сообщить владельцу группы: %r", e)
        return (f"👥 Ты в группе «{esc(obj['name'])}». Общий список — /lists, участники и ссылка — /gruppy.\n"
                f"Добавляй фильмы кнопкой «➕ В список» под карточкой фильма.")
    if obj["user_id"] == uid:
        return "Это твой собственный список 🙂 — /lists"
    st.db.share_add(obj["id"], uid)
    return (f"🔗 {esc(name_of(st, obj['user_id']))} открыл(а) тебе список «{esc(obj['name'])}». "
            f"Смотри в /lists → «Открыли мне».")


# ---------- обработчики ----------
def build_router(st) -> Router:
    r = Router()
    st.awaiting = getattr(st, "awaiting", {})

    def allowed(uid: int) -> bool:
        return st.is_allowed(uid)

    async def bot_name(bot: Bot) -> str:
        if not getattr(st, "bot_username", None):
            try:
                st.bot_username = (await bot.me()).username or ""
            except Exception:
                st.bot_username = ""
        return st.bot_username

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

    @r.callback_query(F.data.regexp(r"^Lu:\d+:[mt]:\d+:\d+:[01]$"))
    async def vote_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        lid, kind, tid, page, w = item_args(cb.data)
        lst = st.db.list_get(lid)
        if role(st, lst, cb.from_user.id) != "member" or not st.db.list_item(lid, kind, tid):
            return await cb.answer("Голосовать могут участники группы", show_alert=True)
        on = st.db.list_vote(lid, kind, tid, cb.from_user.id)
        await cb.answer("👍 Голос учтён" if on else "Голос убран")
        await edit(cb, *item_view(st, lid, kind, tid, cb.from_user.id, page, w))

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
        if is_kid(st, uid) and not tmdb.kid_ok(info):
            return await cb.answer("В детском режиме — только мультфильмы и семейное.", show_alert=True)
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

    # --- подборки: создать, переименовать, удалить ---
    @r.callback_query(F.data.regexp(r"^Ln:\d+(:[mt]:\d+)?$"))
    async def new_list(cb: CallbackQuery):
        if await deny(cb):
            return
        p = cb.data.split(":")
        gid = int(p[1])
        if gid and not st.db.is_member(gid, cb.from_user.id):
            return await cb.answer("Ты не в этой группе", show_alert=True)
        film = (p[2], int(p[3])) if len(p) > 3 else None
        ask_input(cb.from_user.id, ("newlist", gid, film))
        await cb.answer()
        where = f" в группе «{st.db.group_get(gid)['name']}»" if gid else ""
        await cb.message.answer(f"✏ Напиши название новой подборки{esc(where)} (например, «На Новый год»):",
                                reply_markup=kb([[B(text="✖ Отмена", callback_data="Lk")]]))

    @r.callback_query(F.data.regexp(r"^Le:\d+$"))
    async def rename_list(cb: CallbackQuery):
        if await deny(cb):
            return
        lst = st.db.list_get(int(cb.data[3:]))
        if not lst or not can_manage(st, lst, cb.from_user.id) or lst["main"]:
            return await cb.answer("Нельзя", show_alert=True)
        ask_input(cb.from_user.id, ("renamelist", lst["id"]))
        await cb.answer()
        await cb.message.answer(f"✏ Новое название для «{esc(lst['name'])}»:",
                                reply_markup=kb([[B(text="✖ Отмена", callback_data="Lk")]]))

    @r.callback_query(F.data.regexp(r"^L[xX]:\d+$"))
    async def delete_list(cb: CallbackQuery):
        if await deny(cb):
            return
        lid = int(cb.data[3:])
        lst = st.db.list_get(lid)
        if not lst or not can_manage(st, lst, cb.from_user.id) or lst["main"]:
            return await cb.answer("Нельзя", show_alert=True)
        await cb.answer()
        if cb.data[1] == "x":
            todo, total = st.db.list_counts(lid)
            return await edit(cb, f"Удалить подборку «{esc(lst['name'])}» ({total} фильм.)? Оценки останутся.",
                              kb([[B(text="🗑 Да, удалить", callback_data=f"LX:{lid}"),
                                   B(text="Нет", callback_data=f"Lv:{lid}:0:0")]]))
        st.db.list_delete(lid)
        await edit(cb, *menu_view(st, cb.from_user.id))

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

    # --- поделиться ---
    @r.callback_query(F.data.regexp(r"^Ls:\d+$"))
    async def share_cb(cb: CallbackQuery, bot: Bot):
        if await deny(cb):
            return
        lid = int(cb.data[3:])
        lst = st.db.list_get(lid)
        if role(st, lst, cb.from_user.id) != "owner":
            return await cb.answer("Поделиться может только владелец списка", show_alert=True)
        await cb.answer()
        await edit(cb, *share_view(st, lid, cb.from_user.id, await bot_name(bot)))

    @r.callback_query(F.data.regexp(r"^Lo:\d+:\d+$"))
    async def unshare_cb(cb: CallbackQuery, bot: Bot):
        if await deny(cb):
            return
        _, lid, who = cb.data.split(":")
        lid, who = int(lid), int(who)
        lst = st.db.list_get(lid)
        if role(st, lst, cb.from_user.id) != "owner":
            return await cb.answer("Нельзя", show_alert=True)
        st.db.share_revoke(lid, who or None)
        await cb.answer("Доступ закрыт" + ("" if who else " для всех, ссылка сменилась"))
        await edit(cb, *share_view(st, lid, cb.from_user.id, await bot_name(bot)))

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
    async def group_cb(cb: CallbackQuery, bot: Bot):
        if await deny(cb):
            return
        await cb.answer()
        await edit(cb, *group_view(st, int(cb.data[3:]), cb.from_user.id, await bot_name(bot)))

    @r.callback_query(F.data == "Gn")
    async def group_new(cb: CallbackQuery):
        if await deny(cb):
            return
        ask_input(cb.from_user.id, ("newgroup",))
        await cb.answer()
        await cb.message.answer("✏ Как назовём группу? (например, «Семья» или «Киноклуб»)",
                                reply_markup=kb([[B(text="✖ Отмена", callback_data="Lk")]]))

    def owner_of(gid: int, uid: int):
        g = st.db.group_get(gid)
        return g if g is not None and g["owner_id"] == uid else None

    @r.callback_query(F.data.regexp(r"^Ge:\d+$"))
    async def group_rename(cb: CallbackQuery):
        if await deny(cb):
            return
        g = owner_of(int(cb.data[3:]), cb.from_user.id)
        if not g:
            return await cb.answer("Это может только владелец группы", show_alert=True)
        ask_input(cb.from_user.id, ("renamegroup", g["id"]))
        await cb.answer()
        await cb.message.answer(f"✏ Новое название для «{esc(g['name'])}»:",
                                reply_markup=kb([[B(text="✖ Отмена", callback_data="Lk")]]))

    @r.callback_query(F.data.regexp(r"^Gi:\d+$"))
    async def group_invite(cb: CallbackQuery, bot: Bot):
        if await deny(cb):
            return
        g = owner_of(int(cb.data[3:]), cb.from_user.id)
        if not g:
            return await cb.answer("Это может только владелец группы", show_alert=True)
        st.db.group_new_invite(g["id"])
        await cb.answer("Новая ссылка готова, старая больше не работает", show_alert=True)
        await edit(cb, *group_view(st, g["id"], cb.from_user.id, await bot_name(bot)))

    @r.callback_query(F.data.regexp(r"^G[kK]:\d+(:\d+)?$"))
    async def group_kick(cb: CallbackQuery, bot: Bot):
        if await deny(cb):
            return
        p = cb.data.split(":")
        g = owner_of(int(p[1]), cb.from_user.id)
        if not g:
            return await cb.answer("Это может только владелец группы", show_alert=True)
        if p[0] == "Gk":
            others = [m for m in st.db.group_members(g["id"]) if m["user_id"] != cb.from_user.id]
            if not others:
                return await cb.answer("В группе больше никого нет", show_alert=True)
            await cb.answer()
            return await edit(cb, f"Кого убрать из «{esc(g['name'])}»?",
                              kb([[B(text=f"✖ {name_of(st, m['user_id'])}", callback_data=f"GK:{g['id']}:{m['user_id']}")]
                                  for m in others] + [[B(text="◀ Назад", callback_data=f"Gv:{g['id']}")]]))
        who = int(p[2])
        st.db.group_leave(g["id"], who)
        try:
            await bot.send_message(who, f"👥 Тебя убрали из группы «{esc(g['name'])}».")
        except Exception:
            pass
        await cb.answer(f"{name_of(st, who)} больше не в группе")
        await edit(cb, *group_view(st, g["id"], cb.from_user.id, await bot_name(bot)))

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
            return await edit(cb, f"Кого добавить в «{esc(g['name'])}»?",
                              kb([[B(text=f"➕ {name_of(st, u['id'])}", callback_data=f"GA:{gid}:{u['id']}")]
                                  for u in cand[:30]] + [[B(text="◀ Назад", callback_data=f"Gv:{gid}")]]))
        who = int(p[2])
        if st.db.group_join(gid, who):
            try:
                await bot.send_message(who, f"👥 Тебя добавили в группу «{esc(g['name'])}». Общий список — /lists.")
            except Exception:
                pass
        await cb.answer(f"{name_of(st, who)} теперь в группе")
        await edit(cb, *group_view(st, gid, cb.from_user.id, await bot_name(bot)))

    @r.callback_query(F.data.regexp(r"^G[lL]:\d+$"))
    async def group_leave(cb: CallbackQuery, bot: Bot):
        if await deny(cb):
            return
        gid, uid = int(cb.data[3:]), cb.from_user.id
        g = st.db.group_get(gid)
        if g is None or not st.db.is_member(gid, uid):
            return await cb.answer("Ты уже не в этой группе", show_alert=True)
        if cb.data[1] == "l":
            await cb.answer()
            warn = ""
            if g["owner_id"] == uid:
                rest = [m for m in st.db.group_members(gid) if m["user_id"] != uid]
                warn = (f"\nГруппа перейдёт к {esc(name_of(st, rest[0]['user_id']))}." if rest
                        else "\nТы последний — группа удалится вместе со списками.")
            return await edit(cb, f"Выйти из «{esc(g['name'])}»?{warn}",
                              kb([[B(text="🚪 Да, выйти", callback_data=f"GL:{gid}"),
                                   B(text="Нет", callback_data=f"Gv:{gid}")]]))
        res, new = st.db.group_leave(gid, uid)
        if res == "owner" and new:
            try:
                await bot.send_message(new, f"👑 Теперь ты владелец группы «{esc(g['name'])}» — /gruppy")
            except Exception:
                pass
        await cb.answer("Группа удалена" if res == "deleted" else "Ты вышел(ла) из группы")
        await edit(cb, *groups_view(st, uid))

    @r.callback_query(F.data.regexp(r"^G[xX]:\d+$"))
    async def group_delete(cb: CallbackQuery, bot: Bot):
        if await deny(cb):
            return
        g = owner_of(int(cb.data[3:]), cb.from_user.id)
        if not g:
            return await cb.answer("Удалить может только владелец", show_alert=True)
        await cb.answer()
        if cb.data[1] == "x":
            return await edit(cb, f"Удалить группу «{esc(g['name'])}» со всеми её списками? Оценки останутся.",
                              kb([[B(text="🗑 Да, удалить", callback_data=f"GX:{g['id']}"),
                                   B(text="Нет", callback_data=f"Gv:{g['id']}")]]))
        members = [m["user_id"] for m in st.db.group_members(g["id"]) if m["user_id"] != cb.from_user.id]
        st.db.group_delete(g["id"])
        for m in members:
            try:
                await bot.send_message(m, f"👥 Группа «{esc(g['name'])}» удалена владельцем.")
            except Exception:
                pass
        await edit(cb, *groups_view(st, cb.from_user.id))

    # --- ввод текста (название подборки/группы) ---
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
        name = " ".join((msg.text or "").split())[:60]
        if not name:
            return False
        st.awaiting.pop(uid, None)
        if what[0] == "newlist":
            gid, film = what[1], what[2]
            if gid and not st.db.is_member(gid, uid):
                await msg.answer("Ты больше не в этой группе.")
                return True
            lid = st.db.list_create(name, uid, uid=None if gid else uid, gid=gid or None)
            if film:
                st.db.list_add(lid, film[0], film[1], uid)
            await msg.answer(f"📁 Подборка «{esc(name)}» создана" + (" и фильм в неё добавлен." if film else "."),
                             reply_markup=kb([[B(text="📋 Открыть", callback_data=f"Lv:{lid}:0:0")]]))
        elif what[0] == "renamelist":
            lst = st.db.list_get(what[1])
            if lst and can_manage(st, lst, uid):
                st.db.list_rename(lst["id"], name)
                await msg.answer(f"✏ Теперь подборка называется «{esc(name)}».",
                                 reply_markup=kb([[B(text="📋 Открыть", callback_data=f"Lv:{lst['id']}:0:0")]]))
        elif what[0] == "newgroup":
            gid = st.db.group_create(name, uid)
            text, markup = group_view(st, gid, uid, await bot_name(msg.bot))
            await msg.answer(f"👥 Группа создана!\n\n{text}", reply_markup=markup)
        elif what[0] == "renamegroup":
            g = owner_of(what[1], uid)
            if g:
                st.db.group_rename(g["id"], name)
                await msg.answer(f"✏ Группа теперь называется «{esc(name)}» — /gruppy")
        return True

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
