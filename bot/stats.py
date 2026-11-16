"""v8.4: счётчик использования функций и /stats (только админ).

Бот молча считает, сколько раз кто нажал/написал: команды, кнопки, поиск по названию, голосовые,
инлайн. Хранится в таблице `usage` (день · функция · человек · сколько раз). Содержимого сообщений
не сохраняется — только название функции. Зачем: решать по цифрам, что оставить, а что убрать.

* /stats [N | год | all] — админу: что использовали за последние N дней (30 по умолчанию),
  за год (/stats 2026) или за всё время; что не использовали совсем;
* то же за год приходит админу вместе с декабрьской рассылкой итогов (itogi.send_all).
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timedelta

from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineQuery, Message

log = logging.getLogger("torrbot.stats")
esc = html.escape

# (ключ, подпись, служебная). Служебные показываются только если ими пользовались.
FEATURES: list[tuple[str, str, bool]] = [
    ("search", "🔎 Поиск по названию", False),
    ("download", "⬇ Скачать раздачу", False),
    ("magnet", "🧲 Magnet-ссылка в чат", False),
    ("voice", "🎤 Голосовой поиск", False),
    ("inline", "@бот — инлайн-режим", False),
    ("plot", "🧩 /plot — по сюжету", False),
    ("podbor", "🎲 /podbor — по жанру и годам", False),
    ("random", "🎰 /random — что посмотреть", False),
    ("sovet", "🎯 /sovet — похожее, вместе, ИИ", False),
    ("lists", "📋 Списки (/lists, /want)", False),
    ("groups", "👥 Группа (/gruppy)", False),
    ("ratings", "⭐ Оценки (/ocenki)", False),
    ("itogi", "🎄 /itogi", False),
    ("voices", "🎙 /voices — озвучки", False),
    ("seasons", "🗂 Выбор сезонов", False),
    ("collection", "📚 Все части франшизы", False),
    ("subs", "🔔 Подписки на сериалы", False),
    ("wait", "⏳ Ждать хорошее качество", False),
    ("status", "📥 /status — закачки", False),
    ("delete", "🗑 /delete — удаление", False),
    ("tv", "📺 /tv — пульт", False),
    ("speed", "🚦 /speed — скорость и нагрузка", True),
    ("cleanup", "🧹 /cleanup", True),
    ("users", "👤 /users", True),
    ("ai_ctl", "🤖 /ai", True),
    ("health", "🩺 /health", True),
    ("report", "📈 /report", True),
    ("backup", "💾 /backup, /backups", True),
    ("update", "⬆ /update", True),
    ("info", "ℹ /start, /id, /version", True),
]
LABEL = {k: v for k, v, _ in FEATURES}
SERVICE = {k for k, _, s in FEATURES if s}

COMMANDS = {
    "status": "status", "podbor": "podbor", "lists": "lists", "want": "lists", "spiski": "lists",
    "sovet": "sovet", "gruppy": "groups", "groups": "groups", "random": "random", "itogi": "itogi",
    "voices": "voices", "plot": "plot", "delete": "delete", "ocenki": "ratings", "podpiski": "subs",
    "tv": "tv", "cleanup": "cleanup", "users": "users", "revoke": "users", "ai": "ai_ctl",
    "speed": "speed", "health": "health", "report": "report", "backup": "backup", "backups": "backup",
    "update": "update", "version": "info", "id": "info", "start": "info",
}

# приставка callback_data (буквы до первого «:» или цифры) → функция; остальные кнопки не считаем
CALLBACKS = {
    "dl": "download", "plot": "plot", "pb": "podbor", "rnd": "random", "vo": "voices",
    "fs": "seasons", "fz": "seasons", "fd": "seasons", "col": "collection", "cola": "collection",
    "jr": "ratings", "jq": "ratings", "rt": "ratings", "rn": "ratings", "jw": "ratings",
    "jx": "ratings", "jy": "ratings", "jd": "ratings",
    "lr": "delete", "lb": "delete", "ls": "delete", "li": "delete", "lw": "delete", "lk": "delete",
    "ld": "delete", "ly": "delete",
    "sb": "subs", "sbt": "subs", "sbu": "subs", "sbs": "subs", "wt": "wait", "wtu": "wait",
    "tv": "tv", "tvp": "tv", "tvf": "tv", "cx": "status", "tp": "status", "tu": "status",
    "sp": "speed",
}
CALLBACK_BY_LETTER = {"L": "lists", "G": "groups", "R": "sovet", "A": "ai_ctl", "U": "update", "u": "users"}

_PREFIX = re.compile(r"[A-Za-z]+")
_last_inline: dict[int, float] = {}          # инлайн шлёт запрос на каждую букву — считаем раз в полминуты
INLINE_GAP = 30.0


def feature_of_text(text: str | None, voice: bool = False, waiting: bool = False) -> str | None:
    if voice:
        return "voice"
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("/"):
        cmd = text[1:].split(None, 1)[0].split("@", 1)[0].lower()
        return COMMANDS.get(cmd)
    if waiting:                              # человек отвечает на вопрос бота (название подборки и т. п.)
        return None
    return "magnet" if text.startswith("magnet:?") else "search"


def feature_of_callback(data: str | None) -> str | None:
    m = _PREFIX.match(data or "")
    if not m:
        return None
    prefix = m.group(0)
    return CALLBACKS.get(prefix) or CALLBACK_BY_LETTER.get(prefix[0])


def note(st, uid: int, feature: str, day: str | None = None) -> None:
    day = day or datetime.now().strftime("%Y-%m-%d")
    c = st.db.c
    if not c.execute("UPDATE usage SET n=n+1 WHERE day=? AND feature=? AND user_id=?",
                     (day, feature, uid)).rowcount:
        c.execute("INSERT INTO usage(day, feature, user_id, n) VALUES (?,?,?,1)", (day, feature, uid))
    c.commit()


def _count(st, upd) -> None:
    msg, cb, iq = upd.message, upd.callback_query, upd.inline_query
    feature = uid = None
    if msg is not None and msg.from_user:
        uid = msg.from_user.id
        feature = feature_of_text(msg.text, voice=msg.voice is not None, waiting=uid in st.awaiting)
    elif cb is not None:
        uid = cb.from_user.id
        feature = feature_of_callback(cb.data)
    elif isinstance(iq, InlineQuery):
        uid = iq.from_user.id
        if time.time() - _last_inline.get(uid, 0) >= INLINE_GAP:
            feature = "inline"
        _last_inline[uid] = time.time()
    if feature and uid is not None and st.is_allowed(uid):
        note(st, uid, feature)


def install(dp: Dispatcher, st) -> None:
    @dp.update.outer_middleware()
    async def count(handler, event, data):
        try:
            _count(st, event)
        except Exception as e:                # счётчик не должен мешать работе бота
            log.debug("счётчик: %r", e)
        return await handler(event, data)


# ---------- отчёт ----------
def _epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").timestamp())


def _scalar(st, sql: str, *args):
    try:
        return st.db.c.execute(sql, args).fetchone()[0] or 0
    except Exception:
        return 0


def text(st, a: str, b: str, title: str) -> str:
    """Отчёт за дни a..b включительно (ГГГГ-ММ-ДД)."""
    c = st.db.c
    period = {r["feature"]: (r["n"], r["u"]) for r in c.execute(
        "SELECT feature, SUM(n) AS n, COUNT(DISTINCT user_id) AS u FROM usage WHERE day BETWEEN ? AND ? "
        "GROUP BY feature", (a, b))}
    ever = {r["feature"]: r["n"] for r in c.execute("SELECT feature, SUM(n) AS n FROM usage GROUP BY feature")}
    first = c.execute("SELECT MIN(day) FROM usage").fetchone()[0]
    ea, eb = _epoch(a), _epoch(b) + 86400
    lines = [f"📊 <b>{esc(title)}</b>"]
    lines.append(f"Счётчик ведётся с {datetime.strptime(first, '%Y-%m-%d'):%d.%m.%Y}."
                 if first else "Счётчик ещё пуст — данные появятся по мере использования.")
    parts = [f"⬇ закачек: {_scalar(st, 'SELECT COUNT(*) FROM downloads WHERE added_at>=? AND added_at<?', ea, eb)}",
             f"⭐ оценок: {_scalar(st, 'SELECT COUNT(*) FROM ratings WHERE at>=? AND at<?', ea, eb)}",
             f"📋 добавлено в списки: {_scalar(st, 'SELECT COUNT(*) FROM list_items WHERE added_at>=? AND added_at<?', ea, eb)}",
             f"🤖 запросов к ИИ: {_scalar(st, 'SELECT SUM(req) FROM ai_usage WHERE day BETWEEN ? AND ?', a, b)}"]
    lines.append(" · ".join(parts))
    used = sorted(((k, *period[k]) for k in period if k in LABEL), key=lambda x: -x[1])
    lines.append("")
    if used:
        lines.append("<b>Пользовались:</b>")
        for k, n, u in used:
            lines.append(f"• {LABEL[k]} — {n} ({u} чел.)")
    else:
        lines.append("За этот период никто ничем не пользовался.")
    never = [(k, v) for k, v, svc in FEATURES if not svc and k not in period]
    if never:
        lines.append("")
        lines.append("<b>Не пользовались за период</b> (в скобках — раз за всё время):")
        lines.extend(f"• {v} ({ever.get(k, 0)})" for k, v in never)
    return "\n".join(lines)


def last_days(st, days: int) -> str:
    today = datetime.now()
    a = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return text(st, a, today.strftime("%Y-%m-%d"), f"Использование бота за {days} дн.")


def whole_year(st, year: int) -> str:
    return text(st, f"{year}-01-01", f"{year}-12-31", f"Использование бота за {year} год")


def everything(st) -> str:
    return text(st, "2000-01-01", "2100-12-31", "Использование бота за всё время")


def build_router(st) -> Router:
    r = Router(name="stats")

    @r.message(Command("stats"))
    async def stats_cmd(msg: Message, command: CommandObject):
        if msg.from_user.id not in st.cfg.admin_ids:
            return
        arg = (command.args or "").strip().lower()
        if arg in ("all", "все", "всё"):
            out = everything(st)
        elif arg.isdigit() and 2000 < int(arg) < 2100:
            out = whole_year(st, int(arg))
        elif arg.isdigit() and 0 < int(arg) <= 3650:
            out = last_days(st, int(arg))
        else:
            out = last_days(st, 30)
        await msg.answer(out + "\n\n<i>/stats 90 — за 90 дней, /stats 2026 — за год, /stats all — за всё время</i>")

    return r
