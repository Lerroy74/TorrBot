"""Telegram-бот: пишешь название фильма — бот ищет раздачи, ты выбираешь, он ставит на закачку."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

from . import __version__, cleanup, deleter, extras, jacred, journal, kodi, tmdb, wiki
from .config import Config, load
from .db import DB
from .transmission import Transmission, TransmissionError

log = logging.getLogger("torrbot")
esc = html.escape

SEARCH_TTL = 3600  # сколько живут результаты поиска для кнопок


class State:
    def __init__(self, cfg: Config, db: DB, tr: Transmission, http: aiohttp.ClientSession,
                 tmdb_http: aiohttp.ClientSession | None = None):
        self.cfg, self.db, self.tr, self.http = cfg, db, tr, http
        self.tmdb_http = tmdb_http or http
        self.kodi: kodi.Kodi | None = None
        # sid -> (время, запрос, раздачи, найденный фильм в TMDB или None)
        self.searches: dict[str, tuple[float, str, list[jacred.Release], tmdb.Info | None]] = {}
        # cid -> (время, запрос, варианты фильмов, люди) — для кнопок «что именно ищем»
        self.choices: dict[str, tuple[float, str, list[tmdb.Info], list[tmdb.Person]]] = {}
        self.hooks: dict = {}                    # функции роутера для extras (add_magnet, search_for…)
        self.infos: dict = {}                    # (kind, tmdb_id) → Info, для кнопок под обложкой
        self.views: dict[str, tuple] = {}        # cid → (текст, кнопки, фото) списка вариантов — для «◀ К вариантам»
        self.back: dict[str, str] = {}           # sid раздач → cid вариантов, откуда пришли
        self.casts: dict[tuple[bool, int], tuple[float, list[str]]] = {}   # актёры по (сериал?, id)
        self.health = extras.Health()

    def is_allowed(self, uid: int) -> bool:
        return uid in self.cfg.admin_ids or uid in self.cfg.allowed_ids or self.db.is_allowed(uid)

    def put_search(self, query: str, results: list[jacred.Release], info: tmdb.Info | None = None) -> str:
        now = time.time()
        for k in [k for k, v in self.searches.items() if now - v[0] > SEARCH_TTL]:
            del self.searches[k]
            self.back.pop(k, None)
        sid = secrets.token_hex(4)
        self.searches[sid] = (now, query, results, info)
        return sid

    def put_choice(self, query: str, infos: list[tmdb.Info], persons: list[tmdb.Person]) -> str:
        now = time.time()
        for k in [k for k, v in self.choices.items() if now - v[0] > SEARCH_TTL]:
            del self.choices[k]
            self.views.pop(k, None)
        cid = secrets.token_hex(4)
        self.choices[cid] = (now, query, infos, persons)
        return cid

    def remember(self, cid: str, text: str, kb: InlineKeyboardMarkup, photo: str | None = None) -> None:
        """Запомнить список вариантов, чтобы из раздач можно было к нему вернуться."""
        if cid in self.choices:
            self.views[cid] = (text, kb, photo)


async def send_with_poster(bot: Bot, st: State, chat_id: int, text: str, poster: str | None,
                           reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Сообщение с обложкой, а если с картинкой что-то не так — просто текстом.
    Сначала Telegram пробует забрать картинку по ссылке сам; не вышло — качаем её
    сами (через TMDB_PROXY, если задан) и загружаем файлом."""
    if poster and len(text) <= 1024:
        try:
            await bot.send_photo(chat_id, poster, caption=text, reply_markup=reply_markup)
            return
        except Exception as e:
            log.info("постер по ссылке не отправился (%r), пробую загрузить файлом", e)
        try:
            data = await tmdb.fetch_image(st.tmdb_http, poster)
            await bot.send_photo(chat_id, BufferedInputFile(data, "poster.jpg"),
                                 caption=text, reply_markup=reply_markup)
            return
        except Exception as e:
            log.warning("постер не отправился: %r", e)
    await bot.send_message(chat_id, text, reply_markup=reply_markup)


async def find_info(st: State, query: str) -> list[dict]:
    """Кандидаты из TMDB; при любой ошибке — пустой список (бот работает и без обложек)."""
    if not st.cfg.tmdb_key:
        return []
    try:
        return await tmdb.search(st.tmdb_http, st.cfg.tmdb_key, query, st.cfg.tmdb_lang)
    except Exception as e:
        log.warning("tmdb: %r", e)
        return []


CAST_TTL = 7 * 24 * 3600


async def add_cast(st: State, infos: list[tmdb.Info], timeout: float = 6) -> None:
    """Дописывает к вариантам 2–3 главных актёров (чтобы отличить одноимённые фильмы).
    Запросы к TMDB параллельно, с кэшем на неделю; не успели или ошибка — просто без актёров."""
    if not st.cfg.tmdb_key or not infos:
        return
    now = time.time()
    need = []
    for inf in infos:
        got = st.casts.get((inf.is_tv, inf.tmdb_id))
        if got and now - got[0] < CAST_TTL:
            inf.cast = got[1]
        elif inf.tmdb_id and inf not in need:
            need.append(inf)
    if not need:
        return

    async def one(inf: tmdb.Info) -> None:
        try:
            names = await tmdb.cast(st.tmdb_http, st.cfg.tmdb_key, inf, st.cfg.tmdb_lang)
        except Exception as e:
            log.info("tmdb cast %s: %r", inf.tmdb_id, e)
            return
        st.casts[(inf.is_tv, inf.tmdb_id)] = (time.time(), names)
        inf.cast = names

    try:
        await asyncio.wait_for(asyncio.gather(*(one(i) for i in need)), timeout)
    except asyncio.TimeoutError:
        log.info("tmdb cast: не дождался актёров")
    if len(st.casts) > 2000:
        for k in sorted(st.casts, key=lambda k: st.casts[k][0])[:500]:
            del st.casts[k]


def fmt_size(n: int | float) -> str:
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit not in ("Б", "КБ") else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def fmt_eta(sec: int) -> str:
    """Сколько осталось: «~45 мин», «~12 ч», «~1 д 9 ч»."""
    m = max(1, int(sec) // 60)
    if m < 60:
        return f"~{m} мин"
    h = m // 60
    if h < 24:
        return f"~{h} ч" if h >= 10 or m % 60 < 5 else f"~{h} ч {m % 60} мин"
    return f"~{h // 24} д {h % 24} ч" if h % 24 else f"~{h // 24} д"


def nice_name(cfg, t: dict, row) -> tuple[str, bool]:
    """Понятное название закачки и сериал ли это.
    1) «Название (год)» по TMDB — запомнено при постановке или папка, которую бот создал;
    2) русская часть заголовка раздачи («Задача трёх тел / 3 Body Problem [2024…]»);
    3) имя торрента как есть (например, добавлено через веб-интерфейс Transmission)."""
    d = (t.get("downloadDir") or "").rstrip("/")
    series_base = cfg.dir_series.rstrip("/")
    is_series = (row["category"] == "series") if row is not None else d.startswith(series_base)
    if row is not None and "label" in row.keys() and row["label"]:
        return row["label"], is_series
    for base in (cfg.dir_movies.rstrip("/"), series_base):
        if d.startswith(base + "/"):
            return d[len(base) + 1:].split("/")[0], is_series
    title = (row["title"] if row is not None else "") or ""
    if title and title != "magnet-ссылка":
        ru = jacred.ru_title(title)
        if ru:
            return ru, is_series
    return t.get("name") or "?", is_series


def progress_bar(p: float, width: int = 10) -> str:
    full = int(round(p * width))
    return "▓" * full + "░" * (width - full)


def render_page(st: State, sid: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    _, query, results, _ = st.searches[sid]
    ps = st.cfg.page_size
    pages = max(1, (len(results) + ps - 1) // ps)
    page = max(0, min(page, pages - 1))
    chunk = results[page * ps:(page + 1) * ps]
    lines = [f"🔎 <b>{esc(query)}</b> — {len(results)} подходящих, стр. {page + 1}/{pages}\n"]
    buttons = []
    for i, r in enumerate(chunk, start=page * ps + 1):
        kind = ("⭐ " if r.fav else "") + ("📺 " if r.is_series else "")
        voices = f"\n   🎙 {esc(', '.join(r.voices[:4]))}" if r.voices else ""
        lines.append(f"<b>{i}.</b> {kind}{esc(r.title[:200])}\n   <code>{esc(r.short_line())}</code>{voices}")
        buttons.append(InlineKeyboardButton(text=f"⬇ {i}", callback_data=f"dl:{sid}:{i - 1}"))
    rows = [buttons[j:j + 3] for j in range(0, len(buttons), 3)]
    if page == 0 and results:
        rows.insert(0, [InlineKeyboardButton(text="⚡ Лучшая раздача (№1)", callback_data=f"dl:{sid}:0")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"pg:{sid}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"pg:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)
    back = st.back.get(sid)
    if back and back in st.views:
        rows.append([InlineKeyboardButton(text="◀ К вариантам", callback_data=f"bk:{back}")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def fmt_day(ts: int | None) -> str:
    if not ts:
        return "—"
    days = (datetime.now().date() - datetime.fromtimestamp(ts).date()).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн. назад" if days < 30 else datetime.fromtimestamp(ts).strftime("%d.%m.%y")


def render_choice(title: str, infos: list[tmdb.Info], persons: list[tmdb.Person],
                  cid: str, raw_query: str | None, plot: bool = False,
                  extra_rows: list | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Список «что именно смотреть» — кнопки фильмов/сериалов и людей."""
    lines = [title]
    rows = []
    labels = [tmdb.short_label(inf) for inf in infos]
    for i, inf in enumerate(infos):
        extra = f" · ⭐ {inf.rating:.1f}" if inf.rating else ""
        orig = (f"\n   <i>{esc(inf.original_title)}</i>"
                if inf.original_title and inf.original_title.lower() != inf.title.lower() else "")
        cast = f"\n   👥 {esc(', '.join(inf.cast))}" if inf.cast else ""
        lines.append(f"<b>{i + 1}.</b> {esc(tmdb.short_label(inf, 80))}{extra}{orig}{cast}")
        btn = f"{i + 1}. {labels[i]}"
        if labels.count(labels[i]) > 1 and inf.cast:        # одинаковые кнопки — добавим актёра
            btn += f" · {tmdb.surname(inf.cast[0])}"
        rows.append([InlineKeyboardButton(text=btn, callback_data=f"pk:{cid}:{i}")])
    for i, p in enumerate(persons):
        role = {"Acting": "актёр", "Directing": "режиссёр"}.get(p.department, "")
        kf = f" — {esc(', '.join(p.known_for))}" if p.known_for else ""
        lines.append(f"👤 <b>{esc(p.name)}</b>{f' ({role})' if role else ''}{kf}")
        rows.append([InlineKeyboardButton(text=f"👤 {p.name[:40]} — фильмы", callback_data=f"pp:{cid}:{i}")])
    if plot:
        rows.append([InlineKeyboardButton(text="📖 Это описание — найти фильм по сюжету",
                                          callback_data=f"plot:{cid}")])
    if raw_query:
        rows.append([InlineKeyboardButton(text=f"🔎 Искать раздачи «{raw_query[:30]}» как есть",
                                          callback_data=f"raw:{cid}")])
    rows.extend(extra_rows or [])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- /podbor: пошаговый подбор кнопками ----------
def podbor_step(data: str) -> tuple[str, InlineKeyboardMarkup] | None:
    """Экран выбора для шагов 1–5. None — всё выбрано, пора показывать фильмы.
    data: «pb», «pb:m», «pb:m:35», «pb:m:35:1990», «pb:m:35:1990:ru»."""
    parts = data.split(":")[1:]
    B = InlineKeyboardButton
    kb = lambda rows: InlineKeyboardMarkup(inline_keyboard=rows)    # noqa: E731
    base = "pb:" + ":".join(parts) if parts else "pb"
    back = [[B(text="◀ Назад", callback_data=base.rsplit(":", 1)[0])]] if parts else []
    if not parts:
        return "🎲 <b>Подбор</b>. Что ищем?", kb([[B(text="🎬 Фильмы", callback_data="pb:m"),
                                                  B(text="📺 Сериалы", callback_data="pb:t")]])
    kind = parts[0]
    if len(parts) == 1:
        genres = tmdb.GENRES_TV if kind == "t" else tmdb.GENRES_MOVIE
        btns = [B(text=name, callback_data=f"{base}:{gid}") for gid, name in genres]
        rows = [[B(text="Любой жанр", callback_data=f"{base}:0")]] + [btns[i:i + 3] for i in range(0, len(btns), 3)]
        return "🎲 Жанр:", kb(rows + back)
    if len(parts) == 2:
        btns = [B(text=name, callback_data=f"{base}:{d or 'x'}") for d, name in tmdb.DECADES]
        return "🎲 Годы:", kb([btns[i:i + 4] for i in range(0, len(btns), 4)] + back)
    if len(parts) == 3:
        return "🎲 Страна:", kb([[B(text="🌍 Любая", callback_data=f"{base}:x"),
                                 B(text="🇷🇺 Россия и СССР", callback_data=f"{base}:ru")]] + back)
    if len(parts) == 4:
        return "🎲 Как сортировать:", kb([[B(text="⭐ Лучшие по оценкам", callback_data=f"{base}:top:1"),
                                          B(text="🔥 Популярные", callback_data=f"{base}:pop:1")]] + back)
    return None


def podbor_label(parts: list[str]) -> str:
    kind, genre, decade, country, sort = parts[:5]
    genres = dict(tmdb.GENRES_TV if kind == "t" else tmdb.GENRES_MOVIE)
    bits = ["сериалы" if kind == "t" else "фильмы"]
    if genre != "0":
        bits.append(genres.get(int(genre), "").lower())
    if decade != "x":
        bits.append(dict(tmdb.DECADES).get(decade, decade))
    if country == "ru":
        bits.append("Россия/СССР")
    bits.append("лучшие" if sort == "top" else "популярные")
    return ", ".join(b for b in bits if b)


def cancel_kb(h: str, series: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✖ Отменить закачку", callback_data=f"cx:{h}")]]
    if series:
        rows.insert(0, [InlineKeyboardButton(text="🗂 Выбрать сезоны", callback_data=f"fs:{h}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def users_view(st: State) -> tuple[str, InlineKeyboardMarkup]:
    """Экран /users: пользователи со статистикой, ожидающие запросы, заблокированные."""
    sizes: dict[str, int] = {}
    try:
        sizes = {t["hashString"].lower(): int(t.get("totalSize") or 0) for t in await st.tr.get()}
    except Exception as e:
        log.info("users: Transmission недоступен: %s", e)
    rows: list[list[InlineKeyboardButton]] = []
    lines = []
    users = st.db.users()
    lines.append(f"👥 <b>Пользователи</b> ({len(users)}), админы не показаны:" if users
                 else "👥 Пользователей пока нет (кроме админов).")
    for i, u in enumerate(users, 1):
        dl = st.db.downloads_of(u["id"])
        on_disk = sum(sizes.get(d["hash"], 0) for d in dl if not d["removed"])
        name = u["name"] or str(u["id"])
        can_del = st.db.can_delete(u["id"])
        lines.append(f"<b>{i}. {esc(name)}</b> · <code>{u['id']}</code>\n"
                     f"   с {datetime.fromtimestamp(u['added_at'] or 0):%d.%m.%y} · был: {fmt_day(u['last_seen'])}\n"
                     f"   закачек: {len(dl)} · на диске: {fmt_size(on_disk)}"
                     + ("\n   🗑 может удалять" if can_del else ""))
        rows.append([InlineKeyboardButton(text=f"🚫 {i}. {name[:18]} — убрать доступ", callback_data=f"urv:{u['id']}"),
                     InlineKeyboardButton(text="⛔ блок", callback_data=f"ubl:{u['id']}")])
        rows.append([InlineKeyboardButton(
            text=f"🗑 {i}. удалять: {'можно ✅ (запретить)' if can_del else 'нельзя (разрешить)'}",
            callback_data=f"udl:{u['id']}")])
    reqs = st.db.requests()
    if reqs:
        lines.append("⏳ <b>Ждут подтверждения:</b>")
        for q in reqs:
            name = q["name"] or str(q["id"])
            lines.append(f"• {esc(name)} · <code>{q['id']}</code> · {fmt_day(q['at'])}")
            rows.append([InlineKeyboardButton(text=f"✅ {name[:18]}", callback_data=f"uok:{q['id']}"),
                         InlineKeyboardButton(text="❌", callback_data=f"uno:{q['id']}"),
                         InlineKeyboardButton(text="⛔", callback_data=f"ubl:{q['id']}")])
    blocked = st.db.blocked()
    if blocked:
        lines.append("⛔ <b>Заблокированы</b> (запросы от них не приходят):")
        for b in blocked:
            name = b["name"] or str(b["id"])
            lines.append(f"• {esc(name)} · <code>{b['id']}</code>")
            rows.append([InlineKeyboardButton(text=f"↩ Разблокировать {name[:18]}", callback_data=f"uub:{b['id']}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="uref")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def build_router(st: State) -> Router:
    r = Router()
    cfg = st.cfg

    def is_admin(uid: int) -> bool:
        return uid in cfg.admin_ids

    async def guard(msg: Message) -> bool:
        uid = msg.from_user.id
        if st.is_allowed(uid):
            st.db.touch(uid)
            return True
        if st.db.is_blocked(uid):
            return False
        await msg.answer("Нет доступа. Отправь /start — администратор получит запрос.")
        return False

    async def guard_cb(cb: CallbackQuery) -> bool:
        if st.is_allowed(cb.from_user.id):
            st.db.touch(cb.from_user.id)
            return True
        await cb.answer("Нет доступа", show_alert=True)
        return False

    # ---------- доступ ----------
    @r.message(CommandStart())
    async def start(msg: Message, bot: Bot):
        u = msg.from_user
        if st.is_allowed(u.id):
            st.db.touch(u.id)
            await msg.answer(
                "Напиши, что хочешь посмотреть:\n"
                "• название — <i>Маска</i> (год не обязателен, покажу варианты на выбор);\n"
                "• имя актёра или режиссёра — <i>Джим Керри</i>, покажу его фильмы;\n"
                "• описание сюжета — <i>/plot мужик находит маску и становится зелёным</i>;\n"
                "• /podbor — подобрать по жанру, годам и стране;\n"
                "• /want — что семья хочет посмотреть (👍), /random — что посмотреть сегодня;\n"
                "• /voices — любимые озвучки: такие раздачи будут первыми со ⭐;\n"
                "• magnet-ссылку — поставлю сразу.\n\n"
                "/status — что сейчас качается (там же можно отменить)\n"
                + ("/delete — удалить скачанное, освободить место\n" if st.hooks["may_delete"](u.id) else "")
                + "/ocenki — что смотрели и оценки (у каждого своя)\n"
                + "/id — твой Telegram ID")
            return
        if st.db.is_blocked(u.id):
            await msg.answer("Доступ закрыт.")
            return
        name = u.full_name + (f" (@{u.username})" if u.username else "")
        if not st.db.add_request(u.id, name):
            await msg.answer("Запрос уже отправлен — жди, администратор ответит.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data=f"ok:{u.id}"),
            InlineKeyboardButton(text="❌ Отказать", callback_data=f"no:{u.id}"),
            InlineKeyboardButton(text="⛔ Блок", callback_data=f"bn:{u.id}"),
        ]])
        for a in cfg.admin_ids:
            try:
                await bot.send_message(a, f"Запрос доступа: {esc(name)}, ID <code>{u.id}</code>", reply_markup=kb)
            except Exception as e:
                log.warning("не смог написать админу %s: %s", a, e)
        await msg.answer("Запрос отправлен администратору. Как только он подтвердит — я напишу.")

    async def decide(bot: Bot, action: str, uid: int) -> str:
        """Общая логика для кнопок в уведомлении и в /users."""
        if action == "ok":
            row = next((q for q in st.db.requests() if q["id"] == uid), None)
            name = row["name"] if row else ""
            if not name:
                try:
                    name = (await bot.get_chat(uid)).full_name or ""
                except Exception:
                    name = ""
            st.db.allow(uid, name)
            try:
                await bot.send_message(uid, "Доступ открыт 🎬 Напиши название фильма.")
            except Exception:
                pass
            return "✅ разрешено"
        if action == "no":
            st.db.drop_request(uid)
            try:
                await bot.send_message(uid, "Администратор отклонил запрос.")
            except Exception:
                pass
            return "❌ отказано"
        st.db.block(uid)
        return "⛔ заблокирован"

    @r.callback_query(F.data.regexp(r"^(ok|no|bn):\d+$"))
    async def approve(cb: CallbackQuery, bot: Bot):
        if not is_admin(cb.from_user.id):
            await cb.answer("Только для администратора", show_alert=True)
            return
        action, uid = cb.data.split(":")
        verdict = await decide(bot, action, int(uid))
        await cb.message.edit_text(cb.message.html_text + f"\n\n{verdict}")
        await cb.answer()

    @r.message(Command("version"))
    async def version(msg: Message):
        if is_admin(msg.from_user.id):
            await msg.answer(f"torrbot, версия {__version__}")

    @r.message(Command("id"))
    async def my_id(msg: Message):
        await msg.answer(f"Твой ID: <code>{msg.from_user.id}</code>")

    # ---------- пользователи ----------
    @r.message(Command("users"))
    async def users(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        text, kb = await users_view(st)
        await msg.answer(text, reply_markup=kb)

    @r.callback_query(F.data.regexp(r"^(urv|ubl|uub|uok|uno|udl):\d+$|^uref$"))
    async def users_action(cb: CallbackQuery, bot: Bot):
        if not is_admin(cb.from_user.id):
            await cb.answer("Только для администратора", show_alert=True)
            return
        note = "Обновлено"
        if cb.data != "uref":
            action, uid = cb.data.split(":")
            uid = int(uid)
            if action == "urv":
                st.db.revoke(uid)
                note = "Доступ убран (сможет попросить снова)"
            elif action == "ubl":
                st.db.block(uid)
                note = "Заблокирован"
            elif action == "udl":
                on = not st.db.can_delete(uid)
                st.db.set_can_delete(uid, on)
                note = "Теперь может удалять (/delete)" if on else "Больше не может удалять"
                if on:
                    try:
                        await bot.send_message(uid, "🗑 Администратор разрешил тебе удалять скачанное: /delete")
                    except Exception:
                        pass
            elif action == "uub":
                st.db.unblock(uid)
                note = "Разблокирован (может снова попросить доступ)"
            else:
                note = await decide(bot, action[1:], uid)
        text, kb = await users_view(st)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:                      # «message is not modified»
            pass
        await cb.answer(note)

    @r.message(Command("revoke"))
    async def revoke(msg: Message, command: CommandObject):
        if not is_admin(msg.from_user.id):
            return
        if not command.args or not command.args.strip().isdigit():
            await msg.answer("Использование: /revoke 123456789 (удобнее — кнопками в /users)")
            return
        ok = st.db.revoke(int(command.args.strip()))
        await msg.answer("Удалён" if ok else "Такого пользователя нет")

    # ---------- статус и отмена ----------
    def can_cancel(uid: int, h: str) -> bool:
        row = st.db.get(h)
        return is_admin(uid) or (row is not None and row["user_id"] == uid)

    @r.message(Command("status"))
    async def status(msg: Message):
        if not await guard(msg):
            return
        uid = msg.from_user.id
        try:
            torrents = await st.tr.get()
        except Exception as e:
            await msg.answer(f"Transmission недоступен: {esc(str(e))}")
            return
        active = [t for t in torrents if t["percentDone"] < 1]
        lines, buttons = [], []
        # «качать первой» (⬆) — сверху, дальше по готовности
        order = sorted(active, key=lambda t: (-(t.get("bandwidthPriority") or 0), -t["percentDone"]))
        for n, t in enumerate(order[:15], 1):
            p = t["percentDone"]
            speed = fmt_size(t["rateDownload"]) + "/с"
            eta = f" · {fmt_eta(t['eta'])}" if t.get("eta", -1) > 0 else ""
            if t.get("status") == 0:
                state = "⏸ на паузе"
            elif t.get("status") == 3:
                state = "⏳ в очереди"
            elif t.get("metadataPercentComplete", 1) < 1:
                state = "получаю метаданные…"
            elif t.get("error"):
                state = f"⚠ {t.get('errorString', '')}"
            else:
                state = f"{speed} · пиров {t.get('peersSendingToUs', 0)}{eta}"
            h = t["hashString"].lower()
            row = st.db.get(h)
            name, series = nice_name(cfg, t, row)
            who = ""
            if row is not None and row["user_id"]:
                u = st.db.user(row["user_id"])
                who_name = (u["name"] if u is not None and u["name"] else
                            ("админ" if is_admin(row["user_id"]) else ""))
                who = f" · 👤 {esc(who_name[:20])}" if who_name else ""
            raw = t.get("name") or ""
            raw_line = f"\n   <i>{esc(raw[:80])}</i>" if raw and raw != name else ""
            prio = " · ⬆ в приоритете" if (t.get("bandwidthPriority") or 0) > 0 else ""
            lines.append(f"<b>{n}.</b> {'📺' if series else '🎬'} <b>{esc(name)}</b>{who}{prio}\n"
                         f"   {progress_bar(p)} {p * 100:.0f}% · {state}{raw_line}")
            if can_cancel(uid, h):
                buttons.append([
                    InlineKeyboardButton(text=f"{'▶' if t.get('status') == 0 else '⏸'} {n}", callback_data=f"tp:{h}"),
                    InlineKeyboardButton(text=f"⬆ {n}", callback_data=f"tu:{h}"),
                    InlineKeyboardButton(text=f"✖ {n}", callback_data=f"cx:{h}")])
        free = await st.tr.free_space(cfg.dir_movies)
        head = f"💾 Свободно: {fmt_size(free)}\n\n" if free is not None else ""
        body = "\n\n".join(lines) if lines else "Сейчас ничего не качается."
        done = len(torrents) - len(active)
        tail = "\n\n⏸ пауза · ⬆ качать первой · ✖ отменить (по номеру закачки)" if buttons else ""
        if st.hooks["may_delete"](uid):
            buttons.append([InlineKeyboardButton(text="🗑 Удалить скачанное…", callback_data="lr")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await msg.answer(f"{head}{body}\n\nГотово всего: {done}{tail}", reply_markup=kb)

    @r.callback_query(F.data.regexp(r"^cx:[0-9a-f]{40}$"))
    async def cancel_ask(cb: CallbackQuery):
        if not await guard_cb(cb):
            return
        h = cb.data[3:]
        if not can_cancel(cb.from_user.id, h):
            await cb.answer("Отменить можно только свою закачку", show_alert=True)
            return
        ts = await st.tr.get([h])
        if not ts:
            st.db.mark_removed(h)
            await cb.answer("Этой закачки уже нет", show_alert=True)
            return
        t = ts[0]
        done = t["percentDone"] >= 1
        what = "Удалить скачанное" if done else "Отменить закачку"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✖ Да, {what.lower()}", callback_data=f"cxy:{h}"),
            InlineKeyboardButton(text="Нет", callback_data="cxn")]])
        await cb.message.answer(f"{what} <b>{esc(t['name'][:150])}</b>?\n"
                                f"Файлы ({fmt_size(t.get('totalSize', 0))}) удалятся с диска.", reply_markup=kb)
        await cb.answer()

    @r.callback_query(F.data == "cxn")
    async def cancel_no(cb: CallbackQuery):
        await cb.message.edit_text("Ок, ничего не трогаю.")
        await cb.answer()

    @r.callback_query(F.data.regexp(r"^cxy:[0-9a-f]{40}$"))
    async def cancel_yes(cb: CallbackQuery, bot: Bot):
        if not await guard_cb(cb):
            return
        h = cb.data[4:]
        uid = cb.from_user.id
        if not can_cancel(uid, h):
            await cb.answer("Отменить можно только свою закачку", show_alert=True)
            return
        ts = await st.tr.get([h])
        row = st.db.get(h)
        if not ts:
            st.db.mark_removed(h)
            await cb.message.edit_text("Этой закачки уже нет.")
            await cb.answer()
            return
        name = ts[0]["name"]
        try:
            await st.tr.remove(h, delete_data=True)
        except Exception as e:
            await cb.answer(f"Не получилось: {e}", show_alert=True)
            return
        st.db.mark_removed(h, "cancel")
        log.info("отменено пользователем %s: %s", uid, name)
        await cb.message.edit_text(f"✖ Отменено и удалено: <b>{esc(name[:150])}</b>")
        await cb.answer("Отменено")
        if row is not None and row["user_id"] != uid:
            try:
                await bot.send_message(row["chat_id"], f"✖ Администратор отменил закачку: <b>{esc(name[:150])}</b>")
            except Exception:
                pass

    # ---------- автоочистка ----------
    @r.message(Command("cleanup"))
    async def cleanup_cmd(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        await msg.answer(await cleanup_report(st))

    @r.callback_query(F.data.startswith("keep:"))
    async def keep(cb: CallbackQuery):
        if not is_admin(cb.from_user.id):
            await cb.answer("Только для администратора", show_alert=True)
            return
        h = cb.data.split(":", 1)[1]
        st.db.set_keep(h)
        await cb.message.edit_text(cb.message.html_text + "\n\n📌 Оставлено, удалять не буду.")
        await cb.answer("Оставлено")

    # ---------- раздачи ----------
    @r.callback_query(F.data.startswith("pg:"))
    async def page(cb: CallbackQuery):
        _, sid, p = cb.data.split(":")
        if sid not in st.searches:
            await cb.answer("Поиск устарел, повтори запрос", show_alert=True)
            return
        text, kb = render_page(st, sid, int(p))
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()

    @r.callback_query(F.data.startswith("dl:"))
    async def download(cb: CallbackQuery):
        if not await guard_cb(cb):
            return
        _, sid, idx = cb.data.split(":")
        if sid not in st.searches:
            await cb.answer("Поиск устарел, повтори запрос", show_alert=True)
            return
        rel = st.searches[sid][2][int(idx)]
        info = st.searches[sid][3]
        # своя папка «Название (год)» — если раздача точно про найденный в TMDB фильм
        sub = tmdb.folder_name(info) if info and tmdb.matches(info, rel.title, rel.is_series) else None
        if not sub and rel.is_series:
            # сериалу нужна своя папка с человеческим именем: по ней Kodi узнаёт сериал
            sub = tmdb.safe_folder(jacred.ru_title(rel.title))
        await add_magnet(cb.message, cb.from_user.id, rel.magnet, rel.title, rel.is_series,
                         info.poster if info else None, sub)
        await cb.answer("Добавлено")

    async def add_magnet(msg: Message, uid: int, magnet: str, title: str, is_series: bool,
                         poster: str | None = None, subfolder: str | None = None):
        folder = cfg.dir_series if is_series else cfg.dir_movies
        if subfolder:
            folder = f"{folder.rstrip('/')}/{subfolder}"
        try:
            h, name, dup = await st.tr.add(magnet, folder)
        except (TransmissionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            await msg.answer(f"❌ Не удалось добавить в Transmission: {esc(str(e))}")
            return
        if dup:
            await msg.answer(f"Это уже есть в закачках: <b>{esc(name or title)}</b>")
            return
        st.db.add_download(h, name, title, "series" if is_series else "movies", msg.chat.id, uid, poster,
                           subfolder)
        kind = "сериалы" if is_series else "фильмы"
        where = f"\n📁 {esc(subfolder)}" if subfolder else ""
        await send_with_poster(msg.bot, st, msg.chat.id,
                               f"⬇ Поставил на закачку ({kind}):\n<b>{esc(title[:200])}</b>{where}\n\n"
                               f"Напишу, когда скачается. Прогресс — /status", poster,
                               reply_markup=cancel_kb(h, is_series))
        if cfg.notify_adds and not is_admin(uid):
            await notify_admins_add(msg.bot, uid, h, title, is_series, poster, subfolder)

    async def notify_admins_add(bot: Bot, uid: int, h: str, title: str, is_series: bool,
                                poster: str | None, subfolder: str | None) -> None:
        """Пользователь (не админ) поставил закачку — админам сообщение с кнопкой отмены."""
        u = st.db.user(uid)
        who = esc(((u["name"] if u is not None else "") or str(uid)).split(" (")[0][:40])
        label = subfolder or jacred.ru_title(title) or title
        raw = f"\n<i>{esc(title[:150])}</i>" if title and title != label else ""
        text = (f"📥 <b>{who}</b> поставил(а) на закачку:\n"
                f"{'📺' if is_series else '🎬'} <b>{esc(label[:150])}</b>{raw}")
        for a in cfg.admin_ids:
            try:
                await send_with_poster(bot, st, a, text, poster, reply_markup=cancel_kb(h))
            except Exception as e:
                log.warning("не смог сообщить админу о закачке: %r", e)

    async def show_releases(msg: Message, label: str, results: list[jacred.Release],
                            info: tmdb.Info | None, note: str = "", back: str | None = None) -> None:
        results = extras.order_for_user(st, msg.chat.id, results)
        sid = st.put_search(label, results, info)
        if back:
            st.back[sid] = back
        text, kb = render_page(st, sid, 0)
        if note:
            text = f"{note}\n\n{text}"
        if info and info.poster:
            await send_with_poster(msg.bot, st, msg.chat.id, info.caption(), info.poster,
                                   reply_markup=await extras.info_kb(st, info))
        await msg.answer(text, reply_markup=kb)

    def back_kb(back: str | None) -> InlineKeyboardMarkup | None:
        if back and back in st.views:
            return InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀ К вариантам", callback_data=f"bk:{back}")]])
        return None

    async def search_for(msg: Message, info: tmdb.Info, back: str | None = None) -> None:
        """Раздачи для выбранного фильма: ищем по русскому и оригинальному
        названию, оставляем только то, что про этот фильм (название + год)."""
        label = tmdb.short_label(info, 80)
        wait = await msg.answer(f"🔎 Ищу раздачи: {esc(label)}…")
        cast_task = asyncio.create_task(add_cast(st, [info]))     # для подписи к обложке
        found = await asyncio.gather(*(jacred.search(st.http, cfg, q) for q in tmdb.tracker_queries(info)),
                                     return_exceptions=True)
        items = [it for res in found if not isinstance(res, BaseException) for it in res]
        if not items and all(isinstance(res, BaseException) for res in found):
            log.warning("jacred: %r", found)
            await wait.edit_text("Поиск сейчас недоступен (jac.red не отвечает). Попробуй позже.",
                                 reply_markup=back_kb(back))
            return
        results, total = jacred.select(items, cfg)
        exact = [x for x in results if tmdb.matches(info, x.title, x.is_series)]
        note = ""
        if exact:
            results = exact
        elif results:
            note = "⚠ Раздач именно этого фильма не нашёл — показываю всё похожее по названию."
        else:
            hint = (f"Нашлось {total} раздач, но ни одна не подходит для приставки "
                    f"(4K/HEVC/слишком большие/мало сидов)." if total else "Раздач не нашлось.")
            await wait.edit_text(f"{esc(label)}\n{hint}", reply_markup=back_kb(back))
            return
        await wait.delete()
        await cast_task
        await show_releases(msg, label, results, info, note, back)

    async def raw_search(msg: Message, query: str) -> None:
        """Старый режим: текст как есть уходит на трекеры, обложка — догадка."""
        wait = await msg.answer(f"🔎 Ищу «{esc(query)}»…")
        info_task = asyncio.create_task(find_info(st, query))
        try:
            items = await jacred.search(st.http, cfg, query)
        except Exception as e:
            log.warning("jacred: %r", e)
            info_task.cancel()
            await wait.edit_text("Поиск сейчас недоступен (jac.red не отвечает). Попробуй позже.")
            return
        results, total = jacred.select(items, cfg)
        if not results:
            info_task.cancel()
            hint = (f"Нашлось {total} раздач, но ни одна не подходит для приставки "
                    f"(4K/HEVC/слишком большие/мало сидов)." if total else "Ничего не нашлось.")
            await wait.edit_text(f"{hint}\nПопробуй другое написание или добавь год.")
            return
        _, year = tmdb.split_query(query)
        prefer_tv = sum(x.is_series for x in results) * 2 > len(results)
        info = tmdb.pick(await info_task, year, prefer_tv)
        await wait.delete()
        await show_releases(msg, query, results, info)

    async def show_person(msg: Message, person: tmdb.Person) -> None:
        wait = await msg.answer(f"👤 Ищу фильмы: {esc(person.name)}…")
        credits, details = await asyncio.gather(
            tmdb.person_credits(st.tmdb_http, cfg.tmdb_key, person.tmdb_id, cfg.tmdb_lang),
            tmdb.person_details(st.tmdb_http, cfg.tmdb_key, person.tmdb_id, cfg.tmdb_lang),
            return_exceptions=True)
        if isinstance(credits, BaseException):
            log.warning("tmdb person: %r", credits)
            await wait.edit_text("TMDB сейчас не отвечает, попробуй позже.")
            return
        if isinstance(details, BaseException):          # без фото и дат — не беда
            log.info("tmdb person details: %r", details)
            details = {}
        infos = tmdb.filmography(credits, person.department)
        if not infos:
            await wait.edit_text(f"У {esc(person.name)} не нашёл фильмов.")
            return
        cid = st.put_choice(person.name, infos, [])
        role = "снял" if person.department == "Directing" else "играл"
        years = tmdb.person_years(details)
        head = f"👤 <b>{esc(person.name)}</b>" + (f" ({years})" if years else "")
        text, kb = render_choice(f"{head} — самое известное, где {role}:", infos, [], cid, None)
        photo = tmdb.person_photo(details) or person.photo
        st.remember(cid, text, kb, photo)
        if not photo:
            await wait.edit_text(text, reply_markup=kb)
            return
        try:
            await wait.delete()
        except Exception:
            pass
        await send_with_poster(msg.bot, st, msg.chat.id, text, photo, reply_markup=kb)

    async def plot_search(msg: Message, text: str) -> None:
        """Описание сюжета → Википедия → варианты фильмов кнопками."""
        if not cfg.tmdb_key:
            await msg.answer("Поиск по сюжету работает только с ключом TMDB (TMDB_API_KEY).")
            return
        if len(wiki.keywords(text)) < 2:
            await msg.answer("Опиши сюжет подробнее: кто герой, что происходит, где. "
                             "Например: <i>/plot мужик находит маску и становится зелёным</i>")
            return
        wait = await msg.answer("📖 Ищу по сюжету…")
        try:
            infos = await wiki.search_by_plot(st.tmdb_http, cfg.tmdb_key, text, cfg.tmdb_lang)
        except wiki.WikiBusy as e:
            log.info("wiki: %s", e)
            mins = max(1, round(e.seconds / 60))
            await wait.edit_text(f"Википедия просит подождать ~{mins} мин (слишком много запросов). "
                                 f"Попробуй чуть позже.")
            return
        except Exception as e:
            log.warning("wiki: %r", e)
            await wait.edit_text("Википедия сейчас не отвечает, попробуй позже.")
            return
        cid = st.put_choice(text[:100], infos, [])
        if not infos:
            t, kb = render_choice("📖 По описанию ничего не нашёл. Добавь конкретики — имена, предметы, "
                                  "место действия, профессии героев.", [], [], cid, text[:100])
            await wait.edit_text(t, reply_markup=kb)
            return
        await add_cast(st, infos)
        t, kb = render_choice("📖 По описанию похоже на:", infos, [], cid, None)
        st.remember(cid, t, kb)
        await wait.edit_text(t, reply_markup=kb)

    @r.message(Command("plot"))
    async def plot_cmd(msg: Message, command: CommandObject):
        if not await guard(msg):
            return
        await plot_search(msg, (command.args or "").strip()[:300])

    # ---------- /podbor ----------
    @r.message(Command("podbor"))
    async def podbor_cmd(msg: Message):
        if not await guard(msg):
            return
        text, kb = podbor_step("pb")
        await msg.answer(text, reply_markup=kb)

    @r.callback_query(F.data.regexp(r"^pb(:[a-z0-9]+){0,6}$"))
    async def podbor_cb(cb: CallbackQuery):
        if not await guard_cb(cb):
            return
        step = podbor_step(cb.data)
        if step:
            await cb.message.edit_text(step[0], reply_markup=step[1])
            await cb.answer()
            return
        if not cfg.tmdb_key:
            await cb.answer("Подбор работает только с ключом TMDB", show_alert=True)
            return
        parts = cb.data.split(":")[1:]
        kind, genre, decade, country, sort, page = parts[:6]
        params = tmdb.discover_params(kind, "" if genre == "0" else genre, "" if decade == "x" else decade,
                                      country if country == "ru" else "", sort, int(page))
        await cb.answer()
        try:
            infos, pages = await tmdb.discover(st.tmdb_http, cfg.tmdb_key, kind, params, cfg.tmdb_lang)
        except Exception as e:
            log.warning("tmdb discover: %r", e)
            await cb.message.edit_text("TMDB сейчас не отвечает, попробуй позже.")
            return
        label = podbor_label(parts)
        await add_cast(st, infos[:10])
        cid = st.put_choice(label, infos[:10], [])
        base = "pb:" + ":".join(parts[:5])
        nav = []
        if int(page) > 1:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"{base}:{int(page) - 1}"))
        if int(page) < min(pages, 50):
            nav.append(InlineKeyboardButton(text="Ещё ▶", callback_data=f"{base}:{int(page) + 1}"))
        extra = ([nav] if nav else []) + [[InlineKeyboardButton(text="🎲 Заново", callback_data="pb")]]
        head = f"🎲 <b>{esc(label)}</b>, стр. {page}" + ("" if infos else "\n\nНичего не нашлось — попробуй другие фильтры.")
        text, kb = render_choice(head, infos[:10], [], cid, None, extra_rows=extra)
        st.remember(cid, text, kb)
        await cb.message.edit_text(text, reply_markup=kb)

    # ---------- выбор фильма ----------
    @r.callback_query(F.data.regexp(r"^(pk|pp):[0-9a-f]+:\d+$|^(raw|plot):[0-9a-f]+$"))
    async def choose(cb: CallbackQuery):
        if not await guard_cb(cb):
            return
        parts = cb.data.split(":")
        ch = st.choices.get(parts[1])
        if not ch:
            await cb.answer("Поиск устарел, повтори запрос", show_alert=True)
            return
        _, query, infos, persons = ch
        await cb.answer()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)   # чтобы не нажали дважды
        except Exception:
            pass
        if parts[0] == "pk":
            await search_for(cb.message, infos[int(parts[2])], back=parts[1])
        elif parts[0] == "pp":
            await show_person(cb.message, persons[int(parts[2])])
        elif parts[0] == "plot":
            await plot_search(cb.message, query)
        else:
            await raw_search(cb.message, query)

    @r.callback_query(F.data.regexp(r"^pw:[0-9a-f]{40}$"))
    async def watched_on_pc(cb: CallbackQuery):
        """«✅ Посмотрели на ПК» под «Скачано»: отметить в Kodi, чтобы сработала автоочистка."""
        if not await guard_cb(cb):
            return
        h = cb.data[3:]
        row = st.db.get(h)
        try:
            torrents = await st.tr.get([h])
        except Exception as e:
            await cb.answer(f"Transmission не ответил: {e}", show_alert=True)
            return
        if row is None or row["removed"] or not torrents:
            await cb.answer("Этого уже нет на диске", show_alert=True)
            return
        await cb.answer("Отмечаю…")
        _, note = await journal.mark_on_pc(st, [_torrent_root(torrents[0])])
        await cb.message.answer(note)
        await journal.ask(cb.bot, st, row["jid"], cb.from_user.id, cb.message.chat.id, "pc", force=True)

    @r.callback_query(F.data.regexp(r"^bk:[0-9a-f]+$"))
    async def back_to_choice(cb: CallbackQuery):
        """«◀ К вариантам» — снова показать список фильмов, из которого выбирали."""
        if not await guard_cb(cb):
            return
        view = st.views.get(cb.data[3:])
        if not view:
            await cb.answer("Список устарел, повтори поиск", show_alert=True)
            return
        await cb.answer()
        text, kb, photo = view
        if photo:
            await send_with_poster(cb.bot, st, cb.message.chat.id, text, photo, reply_markup=kb)
        else:
            await cb.message.answer(text, reply_markup=kb)

    # ---------- magnet и поиск ----------
    @r.message(F.text.startswith("magnet:?"))
    async def magnet(msg: Message):
        if not await guard(msg):
            return
        await add_magnet(msg, msg.from_user.id, msg.text.strip(), "magnet-ссылка", False)

    @r.message(F.text & ~F.text.startswith("/"))
    async def search(msg: Message):
        if not await guard(msg):
            return
        query = msg.text.strip()[:100]
        if not cfg.tmdb_key:
            await raw_search(msg, query)
            return
        cands = await find_info(st, query)
        if tmdb.is_person_query(query, cands):
            await show_person(msg, tmdb.people(cands, 1)[0])
            return
        _, year = tmdb.split_query(query)
        infos = tmdb.choices(cands, year)
        persons = tmdb.people(cands)
        # длинный запрос может быть описанием сюжета, а не названием
        maybe_plot = len(wiki.keywords(query)) >= 3
        if len(infos) == 1 and not persons and not maybe_plot:
            await search_for(msg, infos[0])
            return
        if not infos and not persons:
            if maybe_plot:
                await plot_search(msg, query)   # похоже на описание — ищем по сюжету
            else:
                await raw_search(msg, query)    # TMDB не знает — ищем как есть
            return
        await add_cast(st, infos)
        cid = st.put_choice(query, infos, persons)
        text, kb = render_choice(f"🔎 <b>{esc(query)}</b> — что именно ищем?", infos, persons, cid, query,
                                 plot=maybe_plot)
        st.remember(cid, text, kb)
        await msg.answer(text, reply_markup=kb)

    r.include_router(deleter.build_router(st))
    r.include_router(journal.build_router(st))
    st.hooks.update(add_magnet=add_magnet, search_for=search_for, can_cancel=can_cancel,
                    send_with_poster=send_with_poster,
                    nice_name=lambda t: nice_name(cfg, t, st.db.get(t["hashString"].lower()))[0])
    return r


async def watcher(bot: Bot, st: State):
    """Раз в POLL_INTERVAL секунд проверяет, что докачалось, и присылает уведомление."""
    while True:
        await asyncio.sleep(st.cfg.poll_interval)
        try:
            pending = {row["hash"]: row for row in st.db.pending()}
            if not pending:
                continue
            torrents = {t["hashString"].lower(): t for t in await st.tr.get(list(pending))}
            finished_any = False
            for h, row in pending.items():
                t = torrents.get(h)
                if t is None:
                    st.db.mark_removed(h)
                    continue
                if t["percentDone"] >= 1:
                    st.db.mark_done(h)
                    finished_any = True
                    name, series = nice_name(st.cfg, t, row)
                    st.db.journal_note("series" if series else "movies", name, row["poster"],
                                       row["user_id"], h=h)
                    raw = t.get("name") or ""
                    raw_line = f"\n<i>{esc(raw[:300])}</i>" if raw and raw != name else ""
                    await send_with_poster(
                        bot, st, row["chat_id"],
                        f"✅ Скачано: {'📺' if series else '🎬'} <b>{esc(name[:200])}</b> "
                        f"({fmt_size(t['totalSize'])}){raw_line}\n"
                        f"Уже можно смотреть на ТВ.", row["poster"],
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                            text=journal.PC_BUTTON, callback_data=f"pw:{h}")]]) if st.kodi else None)
            if finished_any and st.kodi:
                asyncio.create_task(kodi_scan_later(st))
        except Exception as e:
            log.warning("watcher: %r", e)


async def kodi_scan_later(st: State, delay: int = 20) -> None:
    """Через несколько секунд (пока Transmission переносит файлы) — обновить медиатеку Kodi."""
    await asyncio.sleep(delay)
    try:
        await st.kodi.scan()
        log.info("Kodi: запущено обновление медиатеки")
    except Exception as e:
        log.info("Kodi: не удалось обновить медиатеку: %s", e)


def _torrent_root(t: dict) -> str:
    return f"{(t.get('downloadDir') or '').rstrip('/')}/{t['name']}"


async def _judge_all(st: State):
    """[(строка БД, торрент, вердикт)] по всем докачанным. Kodi недоступен — исключение."""
    rows = st.db.finished()
    if not rows:
        return []
    torrents = {t["hashString"].lower(): t for t in await st.tr.get([r["hash"] for r in rows])}
    items = await st.kodi.videos()
    out = []
    for row in rows:
        t = torrents.get(row["hash"])
        if t is None:                      # удалили руками в Transmission
            st.db.mark_removed(row["hash"])
            continue
        kpath = cleanup.to_kodi_path(_torrent_root(t), st.cfg.media_root, st.cfg.kodi_media_url)
        verdict = cleanup.judge(kpath, items) if kpath else cleanup.Verdict("not_in_library")
        out.append((row, t, verdict))
    return out


async def cleanup_report(st: State) -> str:
    cfg = st.cfg
    if not st.kodi:
        return "Kodi не настроен (KODI_URL в .env) — автоочистка выключена."
    try:
        judged = await _judge_all(st)
    except Exception as e:
        return f"Не получилось спросить Kodi: {esc(str(e))}"
    if not judged:
        return "Скачанного ботом пока нет."
    now = datetime.now()
    names = {"unwatched": "не смотрели", "partial": "начали смотреть",
             "not_in_library": "нет в медиатеке Kodi"}
    lines = []
    for row, t, v in judged:
        if row["keep"]:
            state = "📌 оставлено навсегда"
        elif v.status == "watched" and v.last_played:
            when = v.delete_after(cfg.cleanup_days) if cfg.cleanup_days else None
            state = f"✅ просмотрено {v.last_played:%d.%m}"
            if when:
                state += " → удалю сегодня-завтра" if when <= now else f" → удалю после {when:%d.%m}"
        elif v.status == "partial" and v.files > 1:
            state = f"▶ просмотрено {v.watched} из {v.files}"
        else:
            state = names.get(v.status, v.status)
        lines.append(f"• <b>{esc(t['name'][:70])}</b> ({fmt_size(t.get('totalSize', 0))})\n   {state}")
    head = (f"Автоочистка: удаляю через {cfg.cleanup_days} дн. после просмотра, "
            f"предупреждаю за {cfg.cleanup_warn_hours} ч." if cfg.cleanup_days else "Автоочистка выключена (CLEANUP_DAYS=0).")
    return head + "\n\n" + "\n".join(lines)


async def cleaner(bot: Bot, st: State):
    """Раз в CLEANUP_INTERVAL_HOURS: предупредить админа о просмотренном, а через
    CLEANUP_WARN_HOURS после предупреждения — удалить (если не нажали «Оставить»)."""
    cfg = st.cfg
    await asyncio.sleep(120)
    while True:
        try:
            await cleanup_once(bot, st)
        except Exception as e:
            log.info("автоочистка пропущена: %s", e)
        await asyncio.sleep(cfg.cleanup_interval_hours * 3600)


async def cleanup_once(bot: Bot, st: State) -> None:
    cfg = st.cfg
    now = datetime.now()
    removed_any = False
    for row, t, v in await _judge_all(st):
        h = row["hash"]
        if row["keep"]:
            continue
        if not v.ready(now, cfg.cleanup_days):
            if row["warned_at"]:           # снова начали смотреть — сбрасываем предупреждение
                st.db.set_warned(h, None)
            continue
        name = esc(t["name"][:150])
        if not row["warned_at"]:
            st.db.set_warned(h, int(time.time()))
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📌 Оставить", callback_data=f"keep:{h}")]])
            for a in cfg.admin_ids:
                await bot.send_message(
                    a, f"🗑 Через {cfg.cleanup_warn_hours} ч удалю просмотренное:\n<b>{name}</b> "
                       f"({fmt_size(t.get('totalSize', 0))}, смотрели {v.last_played:%d.%m})", reply_markup=kb)
        elif time.time() - row["warned_at"] >= cfg.cleanup_warn_hours * 3600:
            await st.tr.remove(h, delete_data=True)
            st.db.mark_removed(h, "cleanup")
            removed_any = True
            log.info("автоочистка: удалено %s", t["name"])
            for a in cfg.admin_ids:
                await bot.send_message(a, f"🗑 Удалил просмотренное: <b>{name}</b> "
                                          f"(освободилось {fmt_size(t.get('totalSize', 0))})")
            await journal.ask(bot, st, row["jid"], row["user_id"], row["chat_id"], "cleanup")   # тот, кто ставил
    if removed_any:
        await asyncio.sleep(10)
        await st.kodi.clean()


WATCH_SEEDED = "rate_watch_seeded"      # prefs(user_id=0): первый проход проверки уже был


async def rating_watch_once(bot: Bot, st: State) -> int:
    """v6.5: досмотрели на ТВ (Kodi отметил просмотренным) — спросить оценку у того, кто ставил.
    Первый проход после обновления ничего не шлёт: всё, что уже просмотрено, просто
    помечается «спрошено», чтобы не засыпать людей вопросами про старое. Вернёт, сколько спросили."""
    seeded = st.db.pref(0, WATCH_SEEDED) == "1"
    sent = 0
    for row, t, v in await _judge_all(st):
        if v.status != "watched" or not row["jid"] or not row["user_id"]:
            continue
        if not seeded:
            st.db.note_ask(row["jid"], row["user_id"], "migrated")
        elif await journal.ask(bot, st, row["jid"], row["user_id"], row["chat_id"], "watched"):
            sent += 1
    if not seeded:
        st.db.set_pref(0, WATCH_SEEDED, "1")
    return sent


async def rating_watch(bot: Bot, st: State) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            n = await rating_watch_once(bot, st)
            if n:
                log.info("спросил оценку после просмотра: %d", n)
        except Exception as e:
            log.info("проверка «досмотрели» пропущена: %s", e)
        await asyncio.sleep(st.cfg.rate_watch_minutes * 60)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    log.info("torrbot, версия %s", __version__)
    db = DB(cfg.db_path)

    connector = None
    if cfg.jacred_proxy:
        from aiohttp_socks import ProxyConnector
        connector = ProxyConnector.from_url(cfg.jacred_proxy, rdns=True)
    jac_http = aiohttp.ClientSession(connector=connector)
    tr_http = aiohttp.ClientSession()
    tr = Transmission(tr_http, cfg.tr_url, cfg.tr_user, cfg.tr_pass)
    tmdb_http = None
    if cfg.tmdb_proxy:
        from aiohttp_socks import ProxyConnector
        tmdb_http = aiohttp.ClientSession(connector=ProxyConnector.from_url(cfg.tmdb_proxy, rdns=True))
    st = State(cfg, db, tr, jac_http, tmdb_http)
    kodi_http = None
    if cfg.kodi_url:
        kodi_http = aiohttp.ClientSession()
        st.kodi = kodi.Kodi(kodi_http, cfg.kodi_url, cfg.kodi_user, cfg.kodi_pass)
    else:
        log.info("KODI_URL не задан — медиатеку не обновляю, автоочистка выключена")
    if not cfg.tmdb_key:
        log.info("TMDB_API_KEY не задан — обложек не будет")

    bot = Bot(cfg.bot_token, session=AiohttpSession(proxy=cfg.tg_proxy),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(extras.build_router(st))
    dp.include_router(build_router(st))
    await bot.set_my_commands([
        BotCommand(command="status", description="Что сейчас качается (и отмена)"),
        BotCommand(command="podbor", description="Подобрать по жанру, годам, стране"),
        BotCommand(command="want", description="Хотим посмотреть (голосование)"),
        BotCommand(command="random", description="Что посмотреть сегодня"),
        BotCommand(command="voices", description="Любимые озвучки"),
        BotCommand(command="plot", description="Найти фильм по описанию сюжета"),
        BotCommand(command="delete", description="Удалить скачанное (освободить место)"),
        BotCommand(command="ocenki", description="Что смотрели и оценки"),
        BotCommand(command="id", description="Мой Telegram ID"),
        BotCommand(command="start", description="Справка"),
    ])
    tasks = [asyncio.create_task(watcher(bot, st)),
             asyncio.create_task(extras.health_loop(bot, st)),
             asyncio.create_task(extras.weekly_loop(bot, st))]
    try:                                          # очередь закачек
        await tr.session_set(**{"download-queue-enabled": cfg.queue_size > 0,
                                "download-queue-size": max(cfg.queue_size, 1)})
    except Exception as e:
        log.warning("не удалось настроить очередь Transmission: %r", e)
    if st.kodi and cfg.rate_watch_minutes > 0:
        tasks.append(asyncio.create_task(rating_watch(bot, st)))
    if st.kodi and cfg.cleanup_days > 0:
        tasks.append(asyncio.create_task(cleaner(bot, st)))
        log.info("автоочистка: через %s дн. после просмотра", cfg.cleanup_days)
    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        if kodi_http:
            await kodi_http.close()
        await jac_http.close()
        await tr_http.close()
        if tmdb_http:
            await tmdb_http.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
