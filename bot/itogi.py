"""v8.2: «🎄 Итоги года» — /itogi [год] и рассылка каждому в конце декабря.

Считается только по базе бота (ни TMDB, ни ИИ не нужны — бесплатно):
посмотрено (оценки + ✅ в своих списках), скачано, любимые жанры, топ оценок,
в группах — общий итог и любимое у группы. Рассылка — ITOGI_DAY декабря в ITOGI_HOUR
(по умолчанию 30-е, 19:00; ITOGI_DAY=0 — не рассылать), каждому, у кого за год что-то есть.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
from collections import Counter
from datetime import datetime

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from . import tmdb
from .extras import now_local

log = logging.getLogger("torrbot.itogi")
esc = html.escape
GENRE = dict(tmdb.GENRES_TV + tmdb.GENRES_MOVIE)
GENRE.update({10759: "Боевик", 10765: "Фантастика", 10762: "Детский", 10768: "Военный",
              37: "Вестерн", 10402: "Музыка", 10770: "ТВ-фильм"})


def year_bounds(year: int) -> tuple[int, int]:
    """Начало и конец года в местном времени (TZ) — как unix-время."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("TZ") or "UTC")
    except Exception:
        tz = None
    a = datetime(year, 1, 1, tzinfo=tz) if tz else datetime(year, 1, 1)
    b = datetime(year + 1, 1, 1, tzinfo=tz) if tz else datetime(year + 1, 1, 1)
    return int(a.timestamp()), int(b.timestamp())


def default_year() -> int:
    now = now_local()
    return now.year - 1 if now.month == 1 and now.day <= 15 else now.year


def _title(st, kind, tid, label=None) -> tuple[str, list[int]]:
    row = st.db.c.execute("SELECT title, year, genres FROM titles WHERE kind=? AND tmdb_id=?",
                          (kind, tid)).fetchone() if kind and tid else None
    if row:
        g = [int(x) for x in (row["genres"] or "").split(",") if x.strip().isdigit()]
        return (f"{row['title']} ({row['year']})" if row["year"] else row["title"]), g
    return (label or "?"), []


def watched(st, uid: int, a: int, b: int) -> dict:
    """Что человек посмотрел за период: {(kind, id) или ('j', jid): (название, оценка|None, жанры)}."""
    out: dict = {}
    for r in st.db.c.execute(
            "SELECT j.id, j.label, j.tmdb_kind, j.tmdb_id, r.score FROM ratings r JOIN journal j ON j.id=r.jid"
            " WHERE r.user_id=? AND r.score IS NOT NULL AND r.at>=? AND r.at<?", (uid, a, b)):
        key = (r["tmdb_kind"], r["tmdb_id"]) if r["tmdb_id"] else ("j", r["id"])
        title, g = _title(st, r["tmdb_kind"], r["tmdb_id"], r["label"])
        out[key] = (title, r["score"], g)
    for r in st.db.c.execute(
            "SELECT DISTINCT i.kind, i.tmdb_id FROM list_items i JOIN lists l ON l.id=i.list_id"
            " WHERE i.watched_at>=? AND i.watched_at<? AND (l.user_id=? OR i.watched_by=?)", (a, b, uid, uid)):
        key = (r["kind"], r["tmdb_id"])
        if key not in out:
            title, g = _title(st, r["kind"], r["tmdb_id"])
            out[key] = (title, None, g)
    return out


def _genres(items) -> list[str]:
    c = Counter(GENRE[g] for _, _, gs in items for g in set(gs) if g in GENRE)
    return [f"{n} ({k})" for n, k in c.most_common(3)]


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def personal(st, uid: int, year: int) -> str | None:
    a, b = year_bounds(year)
    w = watched(st, uid, a, b)
    dl = st.db.c.execute("SELECT category, COUNT(*) n FROM downloads WHERE user_id=? AND added_at>=? AND added_at<?"
                         " GROUP BY category", (uid, a, b)).fetchall()
    added = st.db.c.execute("SELECT COUNT(*) FROM list_items i JOIN lists l ON l.id=i.list_id"
                            " WHERE i.added_by=? AND i.added_at>=? AND i.added_at<?", (uid, a, b)).fetchone()[0]
    n_dl = sum(r["n"] for r in dl)
    if not w and not n_dl and not added:
        return None
    lines = [f"🎄 <b>Твои итоги {year}</b>", ""]
    films = sum(1 for k in w if k[0] == "m")
    series = sum(1 for k in w if k[0] == "t")
    if w:
        s = f"👀 Посмотрено: <b>{len(w)}</b>"
        if films or series:
            s += f" ({films} {_plural(films, 'фильм', 'фильма', 'фильмов')}, {series} {_plural(series, 'сериал', 'сериала', 'сериалов')})"
        lines.append(s)
    scores = [sc for _, sc, _ in w.values() if sc]
    if scores:
        lines.append(f"⭐ Оценок: {len(scores)}, средняя {sum(scores) / len(scores):.1f}")
    if n_dl:
        lines.append(f"⬇ Скачано: {n_dl}")
    if added:
        lines.append(f"📋 Добавлено в списки: {added}")
    g = _genres(w.values())
    if g:
        lines.append("🎭 Любимые жанры: " + ", ".join(g))
    top = sorted((x for x in w.values() if x[1]), key=lambda x: -x[1])[:5]
    if top:
        lines += ["", "🏆 <b>Лучшее за год</b>"]
        lines += [f"{i}. {esc(t)} — {sc}" for i, (t, sc, _) in enumerate(top, 1)]
    worst = [x for x in w.values() if x[1] and x[1] <= 4]
    if worst and len(scores) >= 5:
        t, sc, _ = min(worst, key=lambda x: x[1])
        lines.append(f"🙈 Разочарование года: {esc(t)} — {sc}")
    return "\n".join(lines)


def group(st, gid: int, name: str, year: int) -> str | None:
    a, b = year_bounds(year)
    members = [m["user_id"] for m in st.db.group_members(gid)]
    if len(members) < 2:
        return None
    per = {u: watched(st, u, a, b) for u in members}
    allw = {k for w in per.values() for k in w}
    if not allw:
        return None
    lines = [f"👥 <b>Группа «{esc(name)}» в {year}</b>", f"👀 Вместе посмотрели: {len(allw)}"]
    who = st.hooks.get("short_name", lambda u: str(u))
    leader = max(per, key=lambda u: len(per[u]))
    if per[leader]:
        lines.append(f"🥇 Больше всех: {esc(who(leader))} ({len(per[leader])})")
    loved = []
    for k in allw:
        sc = [per[u][k][1] for u in members if k in per[u] and per[u][k][1]]
        if len(sc) >= 2:
            title = next(per[u][k][0] for u in members if k in per[u])
            loved.append((sum(sc) / len(sc), len(sc), title))
    loved.sort(reverse=True)
    if loved:
        lines.append("❤ Любимое у группы: " + "; ".join(f"{esc(t)} ({avg:.1f})" for avg, _, t in loved[:3]))
    return "\n".join(lines)


def report(st, uid: int, year: int) -> str:
    parts = [p for p in [personal(st, uid, year)] +
             [group(st, g["id"], g["name"], year) for g in st.db.groups_of(uid)] if p]
    return "\n\n".join(parts) if parts else f"За {year} год пока нечего подводить — ни оценок, ни просмотренного."


def recipients(st) -> list[int]:
    ids = [u["id"] for u in st.db.users()]
    return list(dict.fromkeys(list(st.cfg.admin_ids) + ids))


async def send_all(bot: Bot, st, year: int) -> int:
    sent = 0
    for uid in recipients(st):
        if st.db.pref(uid, f"itogi_{year}"):
            continue
        text = "\n\n".join(p for p in [personal(st, uid, year)] +
                           [group(st, g["id"], g["name"], year) for g in st.db.groups_of(uid)] if p)
        if not text:
            continue
        try:
            await bot.send_message(uid, text + "\n\nС наступающим! 🎉 Итоги в любой момент — /itogi")
            st.db.set_pref(uid, f"itogi_{year}", "1")
            sent += 1
        except Exception as e:
            log.info("итоги %s: %r", uid, e)
        await asyncio.sleep(0.2)
    return sent


async def loop(bot: Bot, st) -> None:
    day, hour = st.cfg.itogi_day, st.cfg.itogi_hour
    if not day:
        return
    while True:
        now = now_local()
        if now.month == 12 and (now.day > day or (now.day == day and now.hour >= hour)):
            try:
                await send_all(bot, st, now.year)
            except Exception as e:
                log.warning("итоги: %r", e)
        await asyncio.sleep(3600)


def build_router(st) -> Router:
    r = Router(name="itogi")

    @r.message(Command("itogi"))
    async def itogi(msg: Message, command: CommandObject):
        if not st.is_allowed(msg.from_user.id):
            return
        arg = (command.args or "").strip()
        year = int(arg) if arg.isdigit() and 2000 < int(arg) < 2100 else default_year()
        await msg.answer(report(st, msg.from_user.id, year))

    return r
