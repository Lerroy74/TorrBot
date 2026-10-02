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
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, CopyTextButton,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

from . import (__version__, ai, aictl, cleanup, deleter, extras, jacred, journal, kids, kodi, lists, nfo, picks,
               recs, remote, space, stall, subs, tmdb, wiki)
from .access import DL, NO_DL, may_download
from .access import AI as AI_FLAG
from . import guard as loadguard
from . import inline, itogi, listwatch, updater, voice
from .config import Config, load
from .db import DB
from .transmission import Transmission, TransmissionError

log = logging.getLogger("torrbot")
esc = html.escape

SEARCH_TTL = 3600  # сколько живут результаты поиска для кнопок
PICK_AT = 3        # v8.1: больше стольких вариантов — без кнопки на каждый, выбор номером


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
        # v7
        self.ai: ai.Chain | None = None            # цепочка ИИ для поиска по описанию (Алиса, Groq, Gemini)
        self.kodi_jobs: dict[str, float] = {}     # scan/clean → когда попробовать (повтор, если малинка спит)
        self.guard = loadguard.Guard()                # сторож нагрузки и «черепаха»
        self.pending_adds: dict[str, dict] = {}   # закачки, которые не влезли на диск (кнопка «поставить снова»)
        self.progress: dict[str, tuple] = {}      # hash → (прогресс, с какого времени не меняется)
        self.stall_alts: dict[str, tuple] = {}    # hash зависшей → (время, другие раздачи)
        self.sub_alts: dict[int, tuple] = {}      # подписка → (время, другие раздачи)
        self.sub_meta_done: set[str] = set()      # раздачи подписок, где уже отметили «не качать»
        self.wait_hint: dict[str, tuple] = {}     # sid раздач → (kind, tmdb_id) для кнопки «⏳ ждать»
        # v8
        self.awaiting: dict[int, tuple] = {}      # uid → (время, что ждём текстом: название подборки/группы, запрос к ИИ)
        self.bot_username: str = ""
        self.stt_http = None                      # v8.2: сессия для SpeechKit (голосовые)

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
                           reply_markup: InlineKeyboardMarkup | None = None) -> Message:
    """Сообщение с обложкой, а если с картинкой что-то не так — просто текстом.
    Сначала Telegram пробует забрать картинку по ссылке сам; не вышло — качаем её
    сами (через TMDB_PROXY, если задан) и загружаем файлом."""
    if poster and len(text) <= 1024:
        try:
            return await bot.send_photo(chat_id, poster, caption=text, reply_markup=reply_markup)
        except Exception as e:
            log.info("постер по ссылке не отправился (%r), пробую загрузить файлом", e)
        try:
            data = await tmdb.fetch_image(st.tmdb_http, poster)
            return await bot.send_photo(chat_id, BufferedInputFile(data, "poster.jpg"),
                                        caption=text, reply_markup=reply_markup)
        except Exception as e:
            log.warning("постер не отправился: %r", e)
    return await bot.send_message(chat_id, text, reply_markup=reply_markup)


async def best_magnet(st: State, info: tmdb.Info) -> jacred.Release | None:
    """v8.2: лучшая раздача фильма для magnet-ссылки (без фильтров приставки)."""
    found = await asyncio.gather(*(jacred.search(st.http, st.cfg, q) for q in tmdb.tracker_queries(info)),
                                 return_exceptions=True)
    items = [it for res in found if not isinstance(res, BaseException) for it in res]
    return jacred.best_any(items, lambda t, ser: tmdb.matches(info, t, ser), st.cfg.min_seeders)


def magnet_row(rel: jacred.Release) -> list[InlineKeyboardButton]:
    """Кнопка «скопировать magnet»: ссылку не видно, по нажатию она в буфере обмена.
    На кнопке — качество лучшей раздачи."""
    q = f"{rel.height}p" if rel.height else "?p"
    label = f"📋 Magnet · {q}{' HDR' if rel.hdr else ''} · {rel.size_gb:.0f} ГБ · 👤{rel.seeders}"
    return [InlineKeyboardButton(text=label, copy_text=CopyTextButton(text=jacred.short_magnet(rel)))]


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
    items = []
    for i, r in enumerate(chunk, start=page * ps + 1):
        kind = ("⭐ " if r.fav else "") + ("📺 " if r.is_series else "")
        voices = f"\n   🎙 {esc(', '.join(r.voices[:4]))}" if r.voices else ""
        lines.append(f"<b>{i}.</b> {kind}{esc(r.title[:200])}\n   <code>{esc(r.short_line())}</code>{voices}")
        items.append((i, f"⬇ {i}", f"dl:{sid}:{i - 1}"))
    rows, numbers = picks.numbered(items, PICK_AT, per_row=3)      # много — номер текстом, без кнопок
    if page == 0 and results:
        rows.insert(0, [InlineKeyboardButton(text="⚡ Лучшая раздача (№1)", callback_data=f"dl:{sid}:0")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"pg:{sid}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"pg:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)
    hint = st.wait_hint.get(sid)
    if hint:
        rows.append([InlineKeyboardButton(text=f"⏳ Ждать раздачу от {st.cfg.wait_min_height}p",
                                          callback_data=f"wt:{hint[0]}:{hint[1]}")])
    back = st.back.get(sid)
    if back and back in st.views:
        rows.append([InlineKeyboardButton(text="◀ К вариантам", callback_data=f"bk:{back}")])
    return picks.finish("\n\n".join(lines), numbers), InlineKeyboardMarkup(inline_keyboard=rows)


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
    items = []
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
        items.append((i + 1, btn, f"pk:{cid}:{i}"))
    rows, numbers = picks.numbered(items, PICK_AT)          # больше PICK_AT вариантов — выбор номером
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
    return picks.finish("\n\n".join(lines), numbers), InlineKeyboardMarkup(inline_keyboard=rows)


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


def cancel_kb(h: str, series: bool = False, subscribe: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✖ Отменить закачку", callback_data=f"cx:{h}")]]
    if series:
        rows.insert(0, [InlineKeyboardButton(text="🗂 Выбрать сезоны", callback_data=f"fs:{h}")])
    if subscribe:
        rows.append([InlineKeyboardButton(text="🔔 Следить за новыми сериями", callback_data=f"sb:{h}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_stats(st: State, u, sizes: dict[str, int]) -> str:
    dl = st.db.downloads_of(u["id"])
    on_disk = sum(sizes.get(d["hash"], 0) for d in dl if not d["removed"])
    rights = [x for x, on in (("⬇ качает", st.db.flag(u["id"], DL)), ("🗑 удаляет", st.db.can_delete(u["id"])),
                              ("📺 пульт", st.db.flag(u["id"], remote.REMOTE)),
                              ("🧸 детский режим", st.db.flag(u["id"], kids.KIDS)),
                              ("🤖 ИИ", st.db.flag(u["id"], AI_FLAG))) if on]
    return (f"   с {datetime.fromtimestamp(u['added_at'] or 0):%d.%m.%y} · был: {fmt_day(u['last_seen'])}\n"
            f"   закачек: {len(dl)} · на диске: {fmt_size(on_disk)}"
            + (f"\n   {' · '.join(rights)}" if rights else ""))


async def torrent_sizes(st: State) -> dict[str, int]:
    try:
        return {t["hashString"].lower(): int(t.get("totalSize") or 0) for t in await st.tr.get()}
    except Exception as e:
        log.info("users: Transmission недоступен: %s", e)
        return {}


async def users_view(st: State) -> tuple[str, InlineKeyboardMarkup]:
    """Экран /users: пользователи со статистикой (кнопка — карточка с правами), запросы, заблокированные."""
    sizes = await torrent_sizes(st)
    rows: list[list[InlineKeyboardButton]] = []
    lines = []
    users = st.db.users()
    lines.append(f"👥 <b>Пользователи</b> ({len(users)}), админы не показаны:" if users
                 else "👥 Пользователей пока нет (кроме админов).")
    btns = []
    for i, u in enumerate(users, 1):
        name = u["name"] or str(u["id"])
        lines.append(f"<b>{i}. {esc(name)}</b> · <code>{u['id']}</code>\n" + user_stats(st, u, sizes))
        btns.append((i, f"⚙ {i}. {name.split(' (')[0][:16]}", f"uc:{u['id']}"))
    user_rows, numbers = picks.numbered(btns, 6, per_row=2)     # v8.1: много людей — номер текстом
    rows += user_rows
    reqs = st.db.requests()
    if reqs:
        lines.append("⏳ <b>Ждут подтверждения:</b>")
        for q in reqs[:15]:
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
        rows += [[InlineKeyboardButton(text=f"↩ Разблокировать {(b['name'] or str(b['id']))[:18]}",
                                       callback_data=f"uub:{b['id']}")] for b in blocked[:10]]
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="uref")])
    text = "\n\n".join(lines) + ("\n\n⚙ — права человека: качать, удалять, пульт, детский режим, ИИ, убрать доступ."
                                  if users else "")
    return picks.finish(text, numbers, "права человека"), InlineKeyboardMarkup(inline_keyboard=rows)


async def user_card(st: State, uid: int) -> tuple[str, InlineKeyboardMarkup] | None:
    u = st.db.user(uid)
    if u is None:
        return None
    sizes = await torrent_sizes(st)
    can_del, can_tv, kid = st.db.can_delete(uid), st.db.flag(uid, remote.REMOTE), st.db.flag(uid, kids.KIDS)
    can_dl, ai_on = st.db.flag(uid, DL), st.db.flag(uid, AI_FLAG)
    B = InlineKeyboardButton
    text = f"👤 <b>{esc(u['name'] or str(uid))}</b> · <code>{uid}</code>\n" + user_stats(st, u, sizes)
    groups = st.db.groups_of(uid)
    if groups:
        text += "\n   👥 " + esc(", ".join(g["name"] for g in groups))
    ai_who = aictl.setting(st, "who", "all")
    rows = [[B(text=f"⬇ Качать: {'можно ✅ (запретить)' if can_dl else 'нельзя — только списки (разрешить)'}",
               callback_data=f"udw:{uid}:c")],
            [B(text=f"🗑 Удалять: {'можно ✅ (запретить)' if can_del else 'нельзя (разрешить)'}", callback_data=f"udl:{uid}:c")],
            [B(text=f"📺 Пульт ТВ: {'есть ✅ (забрать)' if can_tv else 'нет (дать)'}", callback_data=f"urm:{uid}:c")],
            [B(text=f"🧸 Детский режим: {'вкл ✅ (выключить)' if kid else 'выкл (включить)'}", callback_data=f"ukd:{uid}:c")],
            [B(text=f"🤖 ИИ: {'отмечен ✅' if ai_on else 'не отмечен'}"
                    + (" (сейчас ИИ доступен всем)" if ai_who == "all" else ""), callback_data=f"uai:{uid}:c")],
            [B(text="🚫 Убрать доступ", callback_data=f"urv:{uid}"), B(text="⛔ Заблокировать", callback_data=f"ubl:{uid}")],
            [B(text="◀ К списку", callback_data="uref")]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


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
    def help_text(uid: int) -> str:
        if not may_download(st, uid):                  # v8: лёгкий режим — списки и подбор
            return ("Я помогаю выбрать, что посмотреть:\n"
                    "• напиши название фильма или имя актёра — покажу карточку, её можно добавить в список "
                    "(«➕ В список») и оценить, а «📋 Magnet» скопирует ссылку на лучшую раздачу "
                    "для твоего торрент-клиента;\n"
                    "• можно голосовым — просто скажи название;\n"
                    "• в любом чате: <i>@бот название</i> — отправить карточку фильма другу;\n"
                    "• описание сюжета — <i>/plot мужик находит маску и становится зелёным</i>;\n"
                    "• /podbor — подобрать по жанру, годам и стране;\n"
                    "• /sovet — что посмотреть: похожее на любимое, вместе с группой, по запросу;\n"
                    "• /random — случайный фильм.\n\n"
                    "/lists — мои списки и подборки, списки групп\n"
                    "/itogi — итоги года\n"
                    "/gruppy — группы с семьёй и друзьями (общие списки)\n"
                    "/ocenki — мои оценки\n"
                    "/id — твой Telegram ID")
        return ("Напиши, что хочешь посмотреть:\n"
                "• название — <i>Маска</i> (год не обязателен, покажу варианты на выбор);\n"
                "• имя актёра или режиссёра — <i>Джим Керри</i>, покажу его фильмы;\n"
                "• описание сюжета — <i>/plot мужик находит маску и становится зелёным</i>;\n"
                "• /podbor — подобрать по жанру, годам и стране;\n"
                "• /sovet — что посмотреть: похожее на любимое, вместе с группой, по запросу;\n"
                "• /random — что посмотреть сегодня;\n"
                "• /voices — любимые озвучки: такие раздачи будут первыми со ⭐;\n"
                "• magnet-ссылку — поставлю сразу;\n"
                "• голосовое — скажи название; в любом чате <i>@бот название</i> — отправить фильм другу.\n\n"
                "/lists — мои списки и подборки, списки групп (/want)\n"
                "/itogi — итоги года\n"
                "/gruppy — группы с семьёй и друзьями\n"
                "/status — что сейчас качается (там же можно отменить)\n"
                + ("/delete — удалить скачанное, освободить место\n" if st.hooks["may_delete"](uid) else "")
                + "/ocenki — что смотрели и оценки (у каждого своя)\n"
                + "/podpiski — подписки на сериалы и «жду хорошую раздачу»\n"
                + ("/tv — пульт от телевизора\n" if st.hooks["may_remote"](uid) else "")
                + "/id — твой Telegram ID")

    @r.message(CommandStart())
    async def start(msg: Message, bot: Bot, command: CommandObject):
        u = msg.from_user
        arg = (command.args or "").strip()
        if st.is_allowed(u.id):
            st.db.touch(u.id)
            if arg[:2] in ("g_", "l_"):                # v8: приглашение в группу / открытый список
                await msg.answer(await lists.accept_invite(bot, st, u.id, arg))
                return
            if await inline.open_from_start(st, msg, u.id, arg):     # v8.2: «Открыть в боте» из инлайна
                return
            await msg.answer(help_text(u.id))
            return
        if st.db.is_blocked(u.id):
            await msg.answer("Доступ закрыт.")
            return
        name = u.full_name + (f" (@{u.username})" if u.username else "")
        note = lists.invite_note(st, arg) if arg[:2] in ("g_", "l_") else ""
        if note:
            st.db.set_pref(u.id, "v8_invite", arg)    # после одобрения — сразу в группу / список
        if not st.db.add_request(u.id, name):
            await msg.answer("Запрос уже отправлен — жди, администратор ответит.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data=f"ok:{u.id}"),
            InlineKeyboardButton(text="✅ + ⬇ качать", callback_data=f"okd:{u.id}")], [
            InlineKeyboardButton(text="❌ Отказать", callback_data=f"no:{u.id}"),
            InlineKeyboardButton(text="⛔ Блок", callback_data=f"bn:{u.id}"),
        ]])
        for a in cfg.admin_ids:
            try:
                await bot.send_message(a, f"Запрос доступа: {esc(name)}, ID <code>{u.id}</code>"
                                          + (f"\n🔗 {esc(note)}" if note else ""), reply_markup=kb)
            except Exception as e:
                log.warning("не смог написать админу %s: %s", a, e)
        await msg.answer("Запрос отправлен администратору. Как только он подтвердит — я напишу.")

    async def decide(bot: Bot, action: str, uid: int) -> str:
        """Общая логика для кнопок в уведомлении и в /users."""
        if action in ("ok", "okd"):
            row = next((q for q in st.db.requests() if q["id"] == uid), None)
            name = row["name"] if row else ""
            if not name:
                try:
                    name = (await bot.get_chat(uid)).full_name or ""
                except Exception:
                    name = ""
            st.db.allow(uid, name)
            if action == "okd":                        # v8: сразу с правом качать
                st.db.set_flag(uid, DL, True)
            invite = st.db.pref(uid, "v8_invite")
            try:
                await bot.send_message(uid, "Доступ открыт 🎬\n\n" + help_text(uid))
                if invite:
                    st.db.set_flag(uid, "v8_invite", False)
                    await bot.send_message(uid, await lists.accept_invite(bot, st, uid, invite))
            except Exception:
                pass
            return "✅ разрешено" + ("" if may_download(st, uid) else " (без права качать — включить в /users)")
        st.db.set_flag(uid, "v8_invite", False)
        if action == "no":
            st.db.drop_request(uid)
            try:
                await bot.send_message(uid, "Администратор отклонил запрос.")
            except Exception:
                pass
            return "❌ отказано"
        st.db.block(uid)
        return "⛔ заблокирован"

    @r.callback_query(F.data.regexp(r"^(ok|okd|no|bn):\d+$"))
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

    @r.callback_query(F.data.regexp(r"^(urv|ubl|uub|uok|uno|udl|udw|uai|urm|ukd|uc):\d+(:c)?$|^uref$"))
    async def users_action(cb: CallbackQuery, bot: Bot):
        if not is_admin(cb.from_user.id):
            await cb.answer("Только для администратора", show_alert=True)
            return
        note = "Обновлено"
        card_uid = None
        if cb.data != "uref":
            parts = cb.data.split(":")
            action, uid = parts[0], int(parts[1])
            if len(parts) > 2 or action == "uc":
                card_uid = uid                          # остаёмся в карточке человека
            if action == "uc":
                note = ""
            elif action == "urv":
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
            elif action == "udw":
                on = not st.db.flag(uid, DL)
                st.db.set_flag(uid, DL, on)
                note = "Теперь может качать" if on else "Больше не может качать (только списки и подбор)"
                if on:
                    try:
                        await bot.send_message(uid, "⬇ Администратор разрешил тебе качать фильмы на домашний сервер. "
                                                    "Найди фильм по названию — покажу раздачи. Справка — /start")
                    except Exception:
                        pass
            elif action == "uai":
                on = not st.db.flag(uid, AI_FLAG)
                st.db.set_flag(uid, AI_FLAG, on)
                note = "🤖 Отмечен для ИИ" if on else "Отметка ИИ снята"
            elif action == "urm":
                on = not st.db.flag(uid, remote.REMOTE)
                st.db.set_flag(uid, remote.REMOTE, on)
                note = "Теперь у него есть пульт (/tv)" if on else "Пульт отключён"
                if on:
                    try:
                        await bot.send_message(uid, "📺 Администратор дал тебе пульт от ТВ: /tv")
                    except Exception:
                        pass
            elif action == "ukd":
                on = not st.db.flag(uid, kids.KIDS)
                st.db.set_flag(uid, kids.KIDS, on)
                note = "🧸 Детский режим включён" if on else "Детский режим выключен"
            elif action == "uub":
                st.db.unblock(uid)
                note = "Разблокирован (может снова попросить доступ)"
            else:
                note = await decide(bot, action[1:], uid)
        view = await user_card(st, card_uid) if card_uid is not None else None
        text, kb = view or await users_view(st)
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
        if not may_download(st, uid):
            await msg.answer(NO_DL)
            return
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
        if not may_download(st, cb.from_user.id):
            await cb.answer(NO_DL, show_alert=True)
            return
        rel = st.searches[sid][2][int(idx)]
        info = st.searches[sid][3]
        exact = bool(info and tmdb.matches(info, rel.title, rel.is_series))
        ok, why = await kids.allowed(st, cb.from_user.id, info if exact else None)
        if not ok:
            await cb.answer(why, show_alert=True)
            return
        # своя папка «Название (год)» — если раздача точно про найденный в TMDB фильм
        sub = tmdb.folder_name(info) if exact else None
        if not sub and rel.is_series:
            # сериалу нужна своя папка с человеческим именем: по ней Kodi узнаёт сериал
            sub = tmdb.safe_folder(jacred.ru_title(rel.title))
        await cb.answer("Добавляю…")
        await enqueue(cb.bot, cb.from_user.id, cb.message.chat.id, rel.magnet, rel.title, rel.is_series,
                      info.poster if info else None, sub, size=rel.size, details=rel.details or None,
                      tmdb_kind=("t" if info.is_tv else "m") if exact else None,
                      tmdb_id=info.tmdb_id if exact else None)

    async def add_magnet(msg: Message, uid: int, magnet: str, title: str, is_series: bool,
                         poster: str | None = None, subfolder: str | None = None):
        await enqueue(msg.bot, uid, msg.chat.id, magnet, title, is_series, poster, subfolder)

    async def enqueue(bot: Bot, uid: int, chat_id: int, magnet: str, title: str, is_series: bool,
                      poster: str | None = None, subfolder: str | None = None, size: int = 0,
                      details: str | None = None, tmdb_kind: str | None = None, tmdb_id: int | None = None,
                      sub_id: int | None = None, quiet: bool = False) -> str | None:
        """Поставить на закачку. Проверяет место (если размер известен). Вернёт hash или None.
        quiet — без сообщения «Поставил» (подписки и «ждать качество» пишут своё)."""
        if not quiet and not may_download(st, uid):
            await bot.send_message(chat_id, NO_DL)
            return None
        folder = cfg.dir_series if is_series else cfg.dir_movies
        if subfolder:
            folder = f"{folder.rstrip('/')}/{subfolder}"
        job = dict(uid=uid, chat_id=chat_id, magnet=magnet, title=title, is_series=is_series, poster=poster,
                   subfolder=subfolder, size=size, details=details, tmdb_kind=tmdb_kind, tmdb_id=tmdb_id,
                   sub_id=sub_id, quiet=quiet)
        if size:
            avail, left = await space.room(st, cfg.dir_series if is_series else cfg.dir_movies)
            if avail is not None and avail < size:
                await space.refuse(bot, st, {**job, "folder": cfg.dir_series if is_series else cfg.dir_movies},
                                   avail, left)
                return None
        try:
            h, name, dup = await st.tr.add(magnet, folder)
        except (TransmissionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            await bot.send_message(chat_id, f"❌ Не удалось добавить в Transmission: {esc(str(e))}")
            return None
        if dup:
            if not quiet:
                await bot.send_message(chat_id, f"Это уже есть в закачках: <b>{esc(name or title)}</b>")
            return h if quiet else None
        st.db.add_download(h, name, title, "series" if is_series else "movies", chat_id, uid, poster,
                           subfolder, details, tmdb_kind, tmdb_id, sub_id)
        if not quiet:
            kind = "сериалы" if is_series else "фильмы"
            where = f"\n📁 {esc(subfolder)}" if subfolder else ""
            await send_with_poster(bot, st, chat_id,
                                   f"⬇ Поставил на закачку ({kind}):\n<b>{esc(title[:200])}</b>{where}\n\n"
                                   f"Напишу, когда скачается. Прогресс — /status", poster,
                                   reply_markup=cancel_kb(h, is_series, subscribe=is_series))
        if cfg.notify_adds and not is_admin(uid) and not quiet:
            await notify_admins_add(bot, uid, h, title, is_series, poster, subfolder)
        return h

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
        if info and info.tmdb_id and results and max((x.height or 0) for x in results) < cfg.wait_min_height:
            st.wait_hint[sid] = ("t" if info.is_tv else "m", info.tmdb_id)       # кнопка «⏳ ждать качество»
            for k in [k for k in st.wait_hint if k not in st.searches]:
                del st.wait_hint[k]
        text, kb = render_page(st, sid, 0)
        if note:
            text = f"{note}\n\n{text}"
        if info and info.poster:
            await send_with_poster(msg.bot, st, msg.chat.id, info.caption(), info.poster,
                                   reply_markup=await extras.info_kb(st, info, msg.chat.id))
        await msg.answer(text, reply_markup=kb)

    def back_kb(back: str | None, info: tmdb.Info | None = None) -> InlineKeyboardMarkup | None:
        rows = []
        if info and info.tmdb_id:
            rows.append([InlineKeyboardButton(text="⏳ Ждать, когда появится хорошая раздача",
                                              callback_data=f"wt:{'t' if info.is_tv else 'm'}:{info.tmdb_id}")])
        if back and back in st.views:
            rows.append([InlineKeyboardButton(text="◀ К вариантам", callback_data=f"bk:{back}")])
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    async def show_card(msg: Message, info: tmdb.Info, uid: int, head: str = "", extra: list | None = None,
                        magnet: bool = False) -> None:
        """v8: карточка фильма (обложка, описание, трейлер, «➕ В список», «⭐ Оценить»).
        Тем, кто может качать, — ещё «⬇ Найти раздачи».
        v8.2: magnet=True (поиск в лёгком режиме) — параллельно ищем лучшую раздачу и, когда
        найдётся, добавляем сверху кнопку «📋 Magnet» (копирует ссылку для своего торрент-клиента)."""
        lists.remember_info(st, info)
        mtask = (asyncio.create_task(best_magnet(st, info))
                 if magnet and not may_download(st, uid) and not kids.is_kid(st, uid) else None)
        kind = "t" if info.is_tv else "m"
        markup = await extras.info_kb(st, info, uid)
        rows = list(markup.inline_keyboard) if markup else []
        if may_download(st, uid):
            rows.insert(0, [InlineKeyboardButton(text="⬇ Найти раздачи", callback_data=f"Ld:{kind}:{info.tmdb_id}")])
        rows += extra or []
        await add_cast(st, [info])
        text = (f"{head}\n\n" if head else "") + info.caption(500 if head else 600)
        sent = await send_with_poster(msg.bot, st, msg.chat.id, text, info.poster,
                                      reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
        if mtask:
            try:
                rel = await mtask
            except Exception as e:
                log.info("magnet: %r", e)
                rel = None
            if rel and sent:
                try:
                    await sent.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[magnet_row(rel)] + rows))
                except Exception as e:
                    log.info("magnet: кнопку не добавить: %r", e)

    async def search_for(msg: Message, info: tmdb.Info, back: str | None = None, uid: int | None = None) -> None:
        """Раздачи для выбранного фильма: ищем по русскому и оригинальному
        названию, оставляем только то, что про этот фильм (название + год).
        v8: кто не может качать — получает карточку фильма (списки, оценка) вместо раздач."""
        uid = uid if uid is not None else msg.chat.id
        if not may_download(st, uid):
            extra = ([[InlineKeyboardButton(text="◀ К вариантам", callback_data=f"bk:{back}")]]
                     if back and back in st.views else None)
            await show_card(msg, info, uid, extra=extra, magnet=True)
            return
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
            st.infos[("t" if info.is_tv else "m", info.tmdb_id)] = info
            await wait.edit_text(f"{esc(label)}\n{hint}", reply_markup=back_kb(back, info))
            return
        await wait.delete()
        await cast_task
        await show_releases(msg, label, results, info, note, back)

    async def raw_search(msg: Message, query: str, uid: int | None = None) -> None:
        """Старый режим: текст как есть уходит на трекеры, обложка — догадка."""
        if kids.is_kid(st, uid if uid is not None else msg.chat.id):
            await msg.answer("В детском режиме ищу только по каталогу фильмов — такого там не нашёл. "
                             "Попробуй написать название иначе.")
            return
        if not may_download(st, uid if uid is not None else msg.chat.id):
            await msg.answer(f"В каталоге фильмов не нашёл «{esc(query)}». Попробуй написать название иначе "
                             f"или добавь год.")
            return
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
        infos = kids.only_kids(st, msg.chat.id, tmdb.filmography(credits, person.department, 40))[:12]
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

    async def plot_search(msg: Message, text: str, uid: int | None = None, use_ai: bool = True) -> None:
        """Описание сюжета → Gemini (если есть ключ) → иначе/не вышло — Википедия → варианты кнопками."""
        uid = uid if uid is not None else msg.chat.id
        if not cfg.tmdb_key:
            await msg.answer("Поиск по сюжету работает только с ключом TMDB (TMDB_API_KEY).")
            return
        if len(wiki.keywords(text)) < 2:
            await msg.answer("Опиши сюжет подробнее: кто герой, что происходит, где. "
                             "Например: <i>/plot мужик находит маску и становится зелёным</i>")
            return
        note = ""
        why = aictl.check(st, uid, "plot") if (use_ai and st.ai) else "нет"
        if use_ai and st.ai and why and aictl.feature_on(st, "plot") and st.ai.active():
            note = f"🤖 {esc(why[0].upper() + why[1:])} — ищу по Википедии.\n\n"     # лимит или не положено
        if use_ai and st.ai and not why:
            wait = await msg.answer("🤖 Спрашиваю ИИ…")
            who = ""
            try:
                found, who = await aictl.plot(st, msg.bot, uid, text)
                infos = kids.only_kids(st, uid, found)
            except aictl.AiLimit as e:
                note = f"🤖 {esc(e.reason)} — ищу по Википедии.\n\n"
                infos = []
            except ai.AiUnavailable as e:
                log.info("ИИ: %s", e.reason)
                note = f"🤖 ИИ сейчас недоступен ({esc(e.reason)}) — ищу по Википедии.\n\n"
                infos = []
            except Exception as e:
                log.warning("ИИ: %r", e)
                note = "🤖 ИИ сейчас недоступен — ищу по Википедии.\n\n"
                infos = []
            else:
                if not infos:
                    note = "🤖 ИИ не узнал фильм — ищу по Википедии.\n\n"
            if infos:
                cid = st.put_choice(text[:100], infos, [])
                await add_cast(st, infos)
                t, kb = render_choice(f"🤖 По описанию (ИИ{', ' + esc(who) if who else ''}) похоже на:",
                                      infos, [], cid, None, extra_rows=[[
                    InlineKeyboardButton(text="📖 Не то — поискать по Википедии", callback_data=f"plotw:{cid}")]])
                st.remember(cid, t, kb)
                await wait.edit_text(t, reply_markup=kb)
                return
            await wait.edit_text(note + "📖 Ищу по сюжету…")
        else:
            wait = await msg.answer(note + "📖 Ищу по сюжету…")
        try:
            infos = await wiki.search_by_plot(st.tmdb_http, cfg.tmdb_key, text, cfg.tmdb_lang)
        except wiki.WikiBusy as e:
            log.info("wiki: %s", e)
            mins = max(1, round(e.seconds / 60))
            await wait.edit_text(f"{note}Википедия просит подождать ~{mins} мин (слишком много запросов). "
                                 f"Попробуй чуть позже.")
            return
        except Exception as e:
            log.warning("wiki: %r", e)
            await wait.edit_text(f"{note}Википедия сейчас не отвечает, попробуй позже.")
            return
        infos = kids.only_kids(st, uid, infos)
        cid = st.put_choice(text[:100], infos, [])
        if not infos:
            t, kb = render_choice(f"{note}📖 По описанию ничего не нашёл. Добавь конкретики — имена, предметы, "
                                  "место действия, профессии героев.", [], [], cid, text[:100])
            await wait.edit_text(t, reply_markup=kb)
            return
        await add_cast(st, infos)
        t, kb = render_choice(f"{note}📖 По описанию (Википедия) похоже на:", infos, [], cid, None)
        st.remember(cid, t, kb)
        await wait.edit_text(t, reply_markup=kb)

    @r.callback_query(F.data.regexp(r"^plotw:[0-9a-f]+$"))
    async def plot_wiki(cb: CallbackQuery):
        """«📖 Не то» под ответом ИИ — тот же запрос через Википедию."""
        if not await guard_cb(cb):
            return
        ch = st.choices.get(cb.data[6:])
        if not ch:
            await cb.answer("Поиск устарел, повтори запрос", show_alert=True)
            return
        await cb.answer()
        await plot_search(cb.message, ch[1], cb.from_user.id, use_ai=False)

    @r.message(Command("plot"))
    async def plot_cmd(msg: Message, command: CommandObject):
        if not await guard(msg):
            return
        await plot_search(msg, (command.args or "").strip()[:300], msg.from_user.id)

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
        infos = kids.only_kids(st, cb.from_user.id, infos)
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
            await search_for(cb.message, infos[int(parts[2])], back=parts[1], uid=cb.from_user.id)
        elif parts[0] == "pp":
            await show_person(cb.message, persons[int(parts[2])])
        elif parts[0] == "plot":
            await plot_search(cb.message, query, cb.from_user.id)
        else:
            await raw_search(cb.message, query, cb.from_user.id)

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
        if kids.is_kid(st, msg.from_user.id):
            await msg.answer("В детском режиме magnet-ссылки не принимаю — найди мультфильм по названию.")
            return
        if not may_download(st, msg.from_user.id):
            await msg.answer(NO_DL)
            return
        await add_magnet(msg, msg.from_user.id, msg.text.strip(), "magnet-ссылка", False)

    @r.message(F.text & ~F.text.startswith("/"))
    async def search(msg: Message, dispatcher: Dispatcher):
        if not await guard(msg):
            return
        if "text_input" in st.hooks and await st.hooks["text_input"](msg):     # v8: ждали название / запрос к ИИ
            return
        data = picks.lookup(msg.chat.id, msg.text)            # v8.1: «5» — выбор номера из последнего списка
        if data:
            await picks.dispatch(msg, data, dispatcher)
            return
        await text_query(msg, msg.text, msg.from_user.id)

    async def text_query(msg: Message, text: str, uid: int) -> None:
        """Обычный запрос: название, актёр или описание сюжета (v8.2: и распознанное голосовое)."""
        full = text.strip()
        query = full[:100]
        if not cfg.tmdb_key:
            await raw_search(msg, query, uid)
            return
        cands = await find_info(st, query)
        if tmdb.is_person_query(query, cands):
            await show_person(msg, tmdb.people(cands, 1)[0])
            return
        _, year = tmdb.split_query(query)
        infos = kids.only_kids(st, uid, tmdb.choices(cands, year, 20))[:6]
        persons = tmdb.people(cands)
        # длинный запрос может быть описанием сюжета, а не названием
        maybe_plot = len(wiki.keywords(query)) >= 3
        if len(infos) == 1 and not persons and not maybe_plot:
            await search_for(msg, infos[0], uid=uid)
            return
        if not infos and not persons:
            if maybe_plot:
                await plot_search(msg, full[:300], uid)   # похоже на описание — ищем по сюжету
            else:
                await raw_search(msg, query, uid)    # TMDB не знает — ищем как есть
            return
        await add_cast(st, infos)
        cid = st.put_choice(query, infos, persons)
        text, kb = render_choice(f"🔎 <b>{esc(query)}</b> — что именно ищем?", infos, persons, cid, query,
                                 plot=maybe_plot)
        st.remember(cid, text, kb)
        await msg.answer(text, reply_markup=kb)

    # v8: ждали название подборки/группы, а человек занялся другим — больше не ждём
    KEEP_INPUT = ("Ln:", "Le:", "Gn", "Ge:", "Ra", "Lk")

    @r.message.outer_middleware()
    async def reset_input_msg(handler, event, data):
        if isinstance(event, Message) and (event.text or "").startswith("/") and event.from_user:
            st.awaiting.pop(event.from_user.id, None)
        return await handler(event, data)

    @r.callback_query.outer_middleware()
    async def reset_input_cb(handler, event, data):
        if isinstance(event, CallbackQuery) and not (event.data or "").startswith(KEEP_INPUT):
            st.awaiting.pop(event.from_user.id, None)
        return await handler(event, data)

    r.include_router(deleter.build_router(st))
    r.include_router(journal.build_router(st))
    r.include_router(remote.build_router(st))
    r.include_router(subs.build_router(st))
    r.include_router(space.build_router(st))
    r.include_router(stall.build_router(st))
    r.include_router(loadguard.build_router(st))
    r.include_router(lists.build_router(st))
    r.include_router(recs.build_router(st))
    r.include_router(aictl.build_router(st))
    r.include_router(inline.build_router(st))
    r.include_router(itogi.build_router(st))
    r.include_router(voice.build_router(st))
    r.include_router(updater.build_router(st))
    st.hooks.update(find_info=lambda q: find_info(st, q), text_query=text_query,
                    info_for=lambda kind, tid: lists.info_for(st, kind, tid, full=True),
                    add_magnet=add_magnet, search_for=search_for, can_cancel=can_cancel, show_card=show_card,
                    may_download=lambda uid: may_download(st, uid),
                    send_with_poster=send_with_poster,
                    enqueue=lambda bot, **kw: enqueue(bot, **kw),
                    short_name=lambda uid: deleter.short_name(st, uid),
                    nice_name=lambda t: nice_name(cfg, t, st.db.get(t["hashString"].lower()))[0])
    return r


def done_kb(st: State, row, h: str, series: bool) -> InlineKeyboardMarkup | None:
    """Кнопки под «✅ Скачано»: на ТВ, посмотрели на ПК, подписка на сериал."""
    rows = []
    if st.kodi and row["user_id"] and st.hooks.get("may_remote", lambda _: False)(row["user_id"]):
        rows.append([InlineKeyboardButton(text="▶ Включить на ТВ", callback_data=f"tvp:{h}")])
    if st.kodi:
        rows.append([InlineKeyboardButton(text=journal.PC_BUTTON, callback_data=f"pw:{h}")])
    if series and not row["sub_id"]:
        rows.append([InlineKeyboardButton(text="🔔 Следить за новыми сериями", callback_data=f"sb:{h}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def watch_once(bot: Bot, st: State) -> None:
    """Одна проверка: что докачалось, что зависло, у чего кончилось место."""
    pending = {row["hash"]: row for row in st.db.pending()}
    if not pending:
        return
    torrents = {t["hashString"].lower(): t for t in await st.tr.get(list(pending))}
    finished_any = False
    for h, row in pending.items():
        t = torrents.get(h)
        if t is None:
            st.db.mark_removed(h)
            continue
        if t["percentDone"] >= 1:
            st.db.mark_done(h)
            st.progress.pop(h, None)
            finished_any = True
            name, series = nice_name(st.cfg, t, row)
            st.db.journal_note("series" if series else "movies", name, row["poster"],
                               row["user_id"], h=h, tmdb_kind=row["tmdb_kind"], tmdb_id=row["tmdb_id"])
            raw = t.get("name") or ""
            raw_line = f"\n<i>{esc(raw[:300])}</i>" if raw and raw != name else ""
            text = (f"✅ Скачано: {'📺' if series else '🎬'} <b>{esc(name[:200])}</b> "
                    f"({fmt_size(t.get('sizeWhenDone') or t['totalSize'])}){raw_line}\n"
                    f"Уже можно смотреть на ТВ.")
            await send_with_poster(bot, st, row["chat_id"], text, row["poster"],
                                   reply_markup=done_kb(st, row, h, series))
            if row["sub_id"]:                              # подписка — остальным подписчикам тоже
                for u in st.db.sub_users(row["sub_id"]):
                    if u["chat_id"] != row["chat_id"]:
                        try:
                            await send_with_poster(bot, st, u["chat_id"], text, row["poster"])
                        except Exception:
                            pass
            asyncio.create_task(remote.notify_done(st, name, series))
            continue
        if space.is_nospace(t) and not row["nospace_at"]:
            st.db.set_download(h, nospace_at=int(time.time()))
            await space.nospace_notify(bot, st, row, nice_name(st.cfg, t, row)[0])
        elif not t.get("error") and row["nospace_at"]:
            st.db.set_download(h, nospace_at=None)         # место освободили, закачка пошла
    await stall.check(bot, st, torrents, {h: r for h, r in pending.items()
                                          if h in torrents and torrents[h]["percentDone"] < 1})
    if finished_any:                                      # v8.3: подсказки для Kodi, потом обновление медиатеки
        if st.kodi:
            remote.kodi_request(st, "scan", delay=60)    # запасной вариант, если подсказки задержатся
        asyncio.create_task(nfo.sync_and_scan(st, delay=20))   # 20 с — пока Transmission переносит файлы


async def watcher(bot: Bot, st: State):
    """Раз в POLL_INTERVAL секунд проверяет, что докачалось, и присылает уведомление."""
    while True:
        await asyncio.sleep(st.cfg.poll_interval)
        try:
            await watch_once(bot, st)
        except Exception as e:
            log.warning("watcher: %r", e)


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
            if row["label"]:                   # своя папка «Название (год)» — убрать оставшиеся .nfo
                base = cfg.dir_series if row["category"] == "series" else cfg.dir_movies
                await asyncio.sleep(2)
                await asyncio.to_thread(nfo.tidy, f"{base.rstrip('/')}/{row['label']}",
                                        (cfg.dir_movies, cfg.dir_series))
            log.info("автоочистка: удалено %s", t["name"])
            for a in cfg.admin_ids:
                await bot.send_message(a, f"🗑 Удалил просмотренное: <b>{name}</b> "
                                          f"(освободилось {fmt_size(t.get('totalSize', 0))})")
            await journal.ask(bot, st, row["jid"], row["user_id"], row["chat_id"], "cleanup")   # тот, кто ставил
    if removed_any:
        remote.kodi_request(st, "clean", delay=10)


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
        try:
            lists.mark_kodi_watched(st, row)          # v8: ✅ в списках того, кто качал (и его групп)
        except Exception as e:
            log.info("списки: отметка просмотренного: %r", e)
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
    try:                                          # обновили/откатили бота — копия базы до любых изменений
        snap = extras.snapshot_before_update(cfg.db_path, __version__, cfg.backup_local_dir)
        if snap:
            log.info("версия сменилась — копия базы: %s", snap)
    except Exception as e:
        log.warning("не смог сохранить копию базы перед обновлением: %r", e)
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
    ai_sessions: dict[str, aiohttp.ClientSession] = {}

    def session_for(proxy: str | None) -> aiohttp.ClientSession:
        from aiohttp_socks import ProxyConnector
        return aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy, rdns=True) if proxy else None)
    if cfg.yandex_key and cfg.yandex_folder:
        ai_sessions["yandex"] = aiohttp.ClientSession()             # Яндекс — напрямую
    if cfg.groq_key:
        ai_sessions["groq"] = session_for(cfg.groq_proxy)
    if cfg.gemini_key:
        ai_sessions["gemini"] = session_for(cfg.gemini_proxy)
    st.ai = ai.Chain(cfg, ai_sessions)
    st.stt_http = ai_sessions.get("yandex")                         # v8.2: голосовые — тоже Яндекс, напрямую
    aictl.apply(st)                                # выключенные в /ai сервисы
    try:
        lists.setup(st)                            # v8: «Хотим посмотреть» → группа «Семья» (один раз)
    except Exception as e:
        log.warning("v8: перенос «Хотим посмотреть» не получился: %r", e)
    log.info("ИИ для поиска по описанию: %s", st.ai.status() if st.ai else "нет ключей — только Википедия")
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
    picks.install(bot)                             # v8.1: выбор номером вместо длинных клавиатур
    dp = Dispatcher()
    dp.include_router(extras.build_router(st))
    dp.include_router(build_router(st))
    await bot.set_my_commands([
        BotCommand(command="status", description="Что сейчас качается (и отмена)"),
        BotCommand(command="podbor", description="Подобрать по жанру, годам, стране"),
        BotCommand(command="lists", description="Мои списки и подборки, списки групп"),
        BotCommand(command="sovet", description="Что посмотреть: похожее, вместе, по запросу"),
        BotCommand(command="gruppy", description="Группы: семья, друзья"),
        BotCommand(command="random", description="Что посмотреть сегодня"),
        BotCommand(command="itogi", description="Итоги года: что посмотрели, лучшее"),
        BotCommand(command="voices", description="Любимые озвучки"),
        BotCommand(command="plot", description="Найти фильм по описанию сюжета"),
        BotCommand(command="delete", description="Удалить скачанное (освободить место)"),
        BotCommand(command="ocenki", description="Что смотрели и оценки"),
        BotCommand(command="podpiski", description="Подписки на сериалы, «жду качество»"),
        BotCommand(command="tv", description="Пульт от телевизора"),
        BotCommand(command="id", description="Мой Telegram ID"),
        BotCommand(command="start", description="Справка"),
    ])
    tasks = [asyncio.create_task(watcher(bot, st)),
             asyncio.create_task(extras.health_loop(bot, st)),
             asyncio.create_task(extras.weekly_loop(bot, st))]
    if cfg.backup_keep > 0:
        tasks.append(asyncio.create_task(extras.daily_backup_loop(bot, st)))
    try:                                          # очередь закачек
        await tr.session_set(**{"download-queue-enabled": cfg.queue_size > 0,
                                "download-queue-size": max(cfg.queue_size, 1)})
    except Exception as e:
        log.warning("не удалось настроить очередь Transmission: %r", e)
    tasks.append(asyncio.create_task(subs.loop(bot, st)))
    tasks.append(asyncio.create_task(itogi.loop(bot, st)))
    tasks.append(asyncio.create_task(listwatch.loop(bot, st)))
    tasks.append(asyncio.create_task(loadguard.guard_loop(bot, st)))
    if st.kodi:
        tasks.append(asyncio.create_task(remote.kodi_sync_loop(st)))
    if st.kodi and cfg.rate_watch_minutes > 0:
        tasks.append(asyncio.create_task(rating_watch(bot, st)))
    if st.kodi and cfg.cleanup_days > 0:
        tasks.append(asyncio.create_task(cleaner(bot, st)))
        log.info("автоочистка: через %s дн. после просмотра", cfg.cleanup_days)
    tasks.append(asyncio.create_task(updater.alive_loop(bot, st)))   # v8.2: «поднялся» — для обновления с сервера
    tasks.append(asyncio.create_task(nfo.loop(st)))                  # v8.3: подсказки .nfo для Kodi
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
        for sess in ai_sessions.values():
            await sess.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
