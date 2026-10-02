"""Удобства v5: любимые озвучки и «⚡ лучшая», выбор сезонов, трейлер и «все части»,
«Хотим посмотреть», пауза/приоритет, сторож сервера, еженедельные отчёт и бэкап,
«🎲 что посмотреть».

Основной модуль (main) отдаёт сюда свои функции через st.hooks, чтобы не было
циклических импортов: add_magnet, search_for, can_cancel.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import random
from urllib.parse import unquote
import re
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton as B,
                           InlineKeyboardMarkup, Message)

from . import jacred, tmdb

log = logging.getLogger("torrbot")
esc = html.escape


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def now_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(os.environ.get("TZ") or "UTC")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def gb(n) -> str:
    return f"{(n or 0) / 1024 ** 3:.1f} ГБ"


# ================= 1–2. Любимые озвучки и «⚡ лучшая раздача» =================
VOICES = [  # (название на кнопке, шаблоны для поиска в озвучках/названии раздачи)
    ("Дубляж", [r"дубляж", r"\bdub\b", r"\bдб\b"]),
    ("LostFilm", [r"lostfilm"]), ("Кубик в Кубе", [r"кубик в кубе", r"kubik"]),
    ("NewStudio", [r"newstudio"]), ("HDRezka", [r"rezka"]), ("Jaskier", [r"jaskier"]),
    ("AlexFilm", [r"alexfilm"]), ("TVShows", [r"tvshows"]), ("Пифагор", [r"пифагор"]),
    ("Кураж-Бамбей", [r"кураж"]), ("Гоблин", [r"гоблин", r"goblin"]),
    ("Гаврилов", [r"гаврилов"]), ("Многоголосый", [r"многоголос", r"\bmvo\b"]),
]


def fav_voices(st, uid: int) -> list[str]:
    return [v for v in st.db.pref(uid, "voices").split("|") if v]


def has_voice(r: jacred.Release, names: list[str]) -> bool:
    text = (" ".join(r.voices) + " " + r.title).lower()
    pats = [p for name, ps in VOICES if name in names for p in ps]
    return any(re.search(p, text) for p in pats)


def order_for_user(st, uid: int, results: list[jacred.Release]) -> list[jacred.Release]:
    """Лучшая раздача — первая: качество, H.264, любимая озвучка, живые сиды, больше сидов."""
    favs = fav_voices(st, uid)
    for r in results:
        r.fav = bool(favs) and has_voice(r, favs)

    def key(r):
        q, h264, seeds = jacred._rank(r, st.cfg.max_height)
        return (q, h264, r.fav, seeds >= 5, seeds)
    return sorted(results, key=key, reverse=True)


def voices_view(st, uid: int):
    favs = fav_voices(st, uid)
    btns = [B(text=("✅ " if n in favs else "") + n, callback_data=f"vo:{i}") for i, (n, _) in enumerate(VOICES)]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)] + [[B(text="Готово", callback_data="vo:ok")]]
    text = ("🎙 <b>Любимые озвучки</b> — такие раздачи будут первыми и со ⭐.\n"
            f"Сейчас: {esc(', '.join(favs)) if favs else 'не выбрано'}")
    return text, kb(rows)


# ================= 4. Выбор сезонов в большой раздаче =================
_RE_SEASON = [re.compile(r"(?:^|[^a-zа-я])s(\d{1,2})(?:e\d|[^0-9]|$)", re.I),
              re.compile(r"(?:season|сезон)\D{0,3}(\d{1,2})", re.I),
              re.compile(r"(\d{1,2})\s*(?:-?й\s*)?сезон", re.I)]


def season_of(path: str) -> str:
    for rx in _RE_SEASON:
        m = rx.search(path)
        if m:
            return f"Сезон {int(m.group(1))}"
    parts = path.split("/")
    return parts[1] if len(parts) > 2 else "Прочее"


def file_groups(files: list[dict]) -> list[tuple[str, list[int], int]]:
    """[(название группы, индексы файлов, размер)], сезоны по порядку."""
    groups: dict[str, list[int]] = {}
    for i, f in enumerate(files):
        groups.setdefault(season_of(f.get("name") or ""), []).append(i)

    def order(name):
        m = re.match(r"Сезон (\d+)$", name)
        return (0, int(m.group(1)), "") if m else (1, 0, name)
    return [(g, idx, sum(int(files[i].get("length") or 0) for i in idx)) for g, idx in sorted(groups.items(), key=lambda x: order(x[0]))]


async def files_view(st, h: str):
    files, wanted, meta = await st.tr.files(h)
    if meta < 1 or not files:
        return None
    rows, lines = [], []
    for gi, (name, idx, size) in enumerate(file_groups(files)[:40]):
        on = sum(wanted[i] for i in idx)
        mark = "✅" if on == len(idx) else ("◻️" if not on else "➖")
        rows.append([B(text=f"{mark} {name[:30]} — {len(idx)} ф., {gb(size)}", callback_data=f"fz:{h}:{gi}")])
    total = sum(int(f.get("length") or 0) for f, w in zip(files, wanted) if w)
    rows.append([B(text=f"Готово (качаю {gb(total)})", callback_data=f"fd:{h}")])
    return "🗂 <b>Что качать?</b> Нажми на сезон, чтобы включить/выключить.", kb(rows)


# ================= 5–6, 12. Кнопки под обложкой: трейлер, все части, «хотим» =================
async def info_kb(st, info: tmdb.Info | None, uid: int | None = None) -> InlineKeyboardMarkup | None:
    """Кнопки под обложкой: трейлер, «➕ В список», «⭐ Оценить», все части, подписка на сериал."""
    if not info or not st.cfg.tmdb_key:
        return None
    kind = "t" if info.is_tv else "m"
    st.infos[(kind, info.tmdb_id)] = info
    try:
        st.db.title_put(kind, info.tmdb_id, info.title, info.year, info.poster, info.genres)
    except Exception as e:
        log.info("titles: %r", e)
    row1 = [B(text="➕ В список", callback_data=f"La:{kind}:{info.tmdb_id}"),
            B(text="⭐ Оценить", callback_data=f"Lr:0:{kind}:{info.tmdb_id}:0:0")]
    rows = [row1]
    try:
        ex = await tmdb.extras(st.tmdb_http, st.cfg.tmdb_key, info, st.cfg.tmdb_lang)
    except Exception as e:
        log.info("tmdb extras: %r", e)
        ex = {}
    if ex.get("trailer"):
        row1.insert(0, B(text="🎞 Трейлер", url=ex["trailer"]))
    if ex.get("collection"):
        cid, name = ex["collection"]
        rows.append([B(text=f"📚 Все части: {name[:35]}", callback_data=f"col:{cid}")])
    can_dl = uid is None or st.hooks.get("may_download", lambda _: True)(uid)
    if info.is_tv and can_dl:
        rows.append([B(text="🔔 Следить за новыми сериями", callback_data=f"sbt:{info.tmdb_id}")])
    return kb(rows)


def wish_view(st):
    items = st.db.wishlist()
    if not items:
        return ("⭐ Список «Хотим посмотреть» пуст. Добавляй кнопкой ⭐ под обложкой фильма.", None)
    names = {u["id"]: (u["name"] or str(u["id"])).split(" (")[0] for u in st.db.users()}
    lines, rows = ["⭐ <b>Хотим посмотреть</b> (больше 👍 — выше):"], []
    for i, w in enumerate(items[:15], 1):
        who = ", ".join(names.get(int(v), "админ") for v in (w["voters"] or "").split(",") if v)
        icon = "📺" if w["kind"] == "t" else "🎬"
        year = f" ({w['year']})" if w["year"] else ""
        voters = f" ({esc(who)})" if who else ""
        lines.append(f"<b>{i}.</b> {icon} {esc(w['title'])}{year} — 👍 {w['n']}{voters}")
        k = f"{w['kind']}:{w['tmdb_id']}"
        rows.append([B(text=f"👍 {i}", callback_data=f"wv:{k}"), B(text=f"⬇ {i}", callback_data=f"wd:{k}"),
                     B(text=f"🗑 {i}", callback_data=f"wr:{k}")])
    return "\n".join(lines), kb(rows)


# ================= 13. Сторож =================
class Health:
    def __init__(self):
        self.bad: dict[str, str] = {}          # что сейчас сломано → описание
        self.fail_since: dict[str, float] = {}
        self.fails: dict[str, int] = {}


async def check_all(st) -> dict[str, tuple[bool, str]]:
    cfg, out = st.cfg, {}
    try:
        free = await st.tr.free_space(cfg.dir_movies)
        out["transmission"] = (True, "работает")
        if free is not None:
            out["disk"] = (free >= cfg.disk_warn_gb * 1024 ** 3, f"свободно {gb(free)}")
    except Exception as e:
        out["transmission"] = (False, f"не отвечает: {e}")
    try:
        await st.tr.get([])
    except Exception as e:
        out["transmission"] = (False, f"не отвечает: {e}")
    if st.kodi:
        try:
            await st.kodi.call("JSONRPC.Ping")
            out["kodi"] = (True, "в сети")
        except Exception:
            out["kodi"] = (False, "не отвечает")
    if cfg.health_proxy:
        try:
            from aiohttp_socks import ProxyConnector
            async with aiohttp.ClientSession(connector=ProxyConnector.from_url(cfg.health_proxy, rdns=True)) as s:
                async with s.get(cfg.health_url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    out["tunnel"] = (r.status < 400, f"ответ {r.status}")
        except Exception as e:
            out["tunnel"] = (False, f"нет связи через прокси: {type(e).__name__}")
    return out


NAMES = {"disk": "💾 Диск", "transmission": "⬇ Transmission", "kodi": "📺 Малинка (Kodi)",
         "tunnel": "🔐 Туннель VPN (xray → VPS)"}


def health_decide(h: Health, results: dict[str, tuple[bool, str]], cfg, now: float) -> list[str]:
    """Сообщения о смене состояния. Малинку считаем упавшей через KODI_OFFLINE_MIN.
    Туннель — только в /health: тревогу о нём шлёт сторож сервера (host-watch.sh),
    который пишет в Telegram мимо туннеля; иначе приходило бы два сообщения."""
    msgs = []
    for name, (ok, detail) in results.items():
        if name == "tunnel":
            continue
        if ok:
            h.fails[name] = 0
            h.fail_since.pop(name, None)
            if name in h.bad:
                del h.bad[name]
                msgs.append(f"✅ {NAMES[name]}: снова в порядке ({detail})")
            continue
        h.fails[name] = h.fails.get(name, 0) + 1
        h.fail_since.setdefault(name, now)
        if name == "kodi" and now - h.fail_since[name] < cfg.kodi_offline_min * 60:
            continue
        if name not in h.bad:
            h.bad[name] = detail
            msgs.append(f"⚠ {NAMES[name]}: {detail}")
    return msgs


async def notify_admins(bot: Bot, st, text: str, **kw) -> None:
    """Админам. Если туннель лёг, отсюда не написать (трафик контейнера тоже идёт в туннель) —
    о туннеле и о падении самого бота сообщает сторож сервера host/host-watch.sh."""
    for a in st.cfg.admin_ids:
        try:
            await bot.send_message(a, text, **kw)
        except Exception as e:
            log.warning("не смог написать админу: %r", e)


async def health_loop(bot: Bot, st) -> None:
    h = st.health
    await asyncio.sleep(60)
    while True:
        try:
            for m in health_decide(h, await check_all(st), st.cfg, time.time()):
                await notify_admins(bot, st, m)
        except Exception as e:
            log.warning("сторож: %r", e)
        await asyncio.sleep(st.cfg.health_interval)


# ================= 14–15. Еженедельные отчёт и бэкап =================
async def weekly_report(st) -> str:
    since = int(time.time()) - 7 * 86400
    rows = st.db.since(since)
    names = {u["id"]: (u["name"] or str(u["id"])).split(" (")[0] for u in st.db.users()}
    added = [r for r in rows if (r["added_at"] or 0) >= since]
    done = [r for r in rows if (r["done_at"] or 0) >= since]
    removed = [r for r in rows if (r["removed_at"] or 0) >= since]
    by_user: dict[str, int] = {}
    for r in added:
        n = names.get(r["user_id"], "админ")
        by_user[n] = by_user.get(n, 0) + 1
    lines = ["📊 <b>Неделя в torrbot</b>",
             f"Поставлено: {len(added)}" + (f" ({', '.join(f'{k} — {v}' for k, v in by_user.items())})" if by_user else ""),
             f"Докачалось: {len(done)}"]
    reasons = {"cleanup": "автоочистка", "cancel": "отменено", "manual": "вручную",
               "replaced": "заменено новой раздачей"}
    removed = [r for r in removed if r["removed_reason"] != "delete"]     # эти — ниже, из /delete
    if removed:
        cnt: dict[str, int] = {}
        for r in removed:
            k = reasons.get(r["removed_reason"] or "manual", "вручную")
            cnt[k] = cnt.get(k, 0) + 1
        lines.append("Удалено: " + ", ".join(f"{k} — {v}" for k, v in cnt.items()))
    for r in done[:10]:
        label = (r["label"] if "label" in r.keys() else None) or jacred.ru_title(r["title"] or "") or r["name"] or ""
        lines.append(f"  • {'📺' if r['category'] == 'series' else '🎬'} {esc(label[:60])}")
    if len(done) > 10:
        lines.append(f"  …и ещё {len(done) - 10}")
    dels = st.db.deletions_since(since)
    if dels:
        lines.append(f"🗑 Удалено через /delete: {len(dels)} ({gb(sum(d['size'] or 0 for d in dels))})")
        for d in dels[:10]:
            who = "админ" if d["user_id"] in st.cfg.admin_ids else names.get(d["user_id"], str(d["user_id"]))
            lines.append(f"  • {'📺' if d['kind'] == 'series' else '🎬'} {esc((d['label'] or '')[:60])} — {esc(who)}")
        if len(dels) > 10:
            lines.append(f"  …и ещё {len(dels) - 10}")
    try:
        ts = await st.tr.get()
        free = await st.tr.free_space(st.cfg.dir_movies)
        lines.append(f"\n💾 На диске: {gb(sum(int(t.get('totalSize') or 0) for t in ts))} в {len(ts)} закачках"
                     + (f", свободно {gb(free)}" if free is not None else ""))
    except Exception:
        lines.append("\n💾 Transmission не ответил")
    wl = st.db.c.execute(
        "SELECT t.title, COUNT(*) AS n FROM list_votes v JOIN list_items i ON i.list_id=v.list_id AND i.kind=v.kind"
        " AND i.tmdb_id=v.tmdb_id AND i.watched_at IS NULL LEFT JOIN titles t ON t.kind=v.kind AND t.tmdb_id=v.tmdb_id"
        " GROUP BY v.list_id, v.kind, v.tmdb_id ORDER BY n DESC LIMIT 3").fetchall()
    if wl:
        lines.append("⭐ Больше всего хотят (в группах): " + ", ".join(f"{esc(w['title'] or '?')} (👍 {w['n']})" for w in wl))
    bad = ", ".join(NAMES[k] for k in st.health.bad) if st.health.bad else ""
    lines.append(f"🩺 Сейчас не в порядке: {bad}" if bad else "🩺 Всё работает")
    return "\n".join(lines)


BACKUP_PREFIX = "torrbot-backup-"
PRE_UPDATE_KEEP = 5


def make_backup(st) -> tuple[bytes, list[str]]:
    """tar.gz: база бота (консистентная копия), .env, settings.json Transmission, файлы из extra/
    и MANIFEST.txt (версия, дата, состав) — его читает restore.sh."""
    from . import __version__
    buf, names = io.BytesIO(), []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar, tempfile.TemporaryDirectory() as tmp:
        dbcopy = os.path.join(tmp, "bot.sqlite3")
        dst = sqlite3.connect(dbcopy)
        st.db.c.backup(dst)
        dst.close()
        tar.add(dbcopy, "data/bot.sqlite3")
        names.append("data/bot.sqlite3")
        b = st.cfg.backup_dir
        for src, arc in [(f"{b}/env", ".env"), (f"{b}/transmission/settings.json", "transmission/settings.json")]:
            if os.path.isfile(src):
                tar.add(src, arc)
                names.append(arc)
        extra = f"{b}/extra"
        if os.path.isdir(extra):
            for f in sorted(os.listdir(extra)):
                p = os.path.join(extra, f)
                if os.path.isfile(p) and os.path.getsize(p) < 20 * 1024 ** 2:
                    tar.add(p, f"extra/{f}")
                    names.append(f"extra/{f}")
        manifest = (f"torrbot backup\nversion={__version__}\ncreated={now_local():%Y-%m-%d %H:%M}\n"
                    f"files={','.join(names)}\n").encode()
        info = tarfile.TarInfo("MANIFEST.txt")
        info.size, info.mtime = len(manifest), int(time.time())
        tar.addfile(info, io.BytesIO(manifest))
    return buf.getvalue(), names


def prune(folder: str, prefix: str, keep: int) -> None:
    """Оставить keep самых новых файлов с этим префиксом (имена содержат дату — сортируются по ней)."""
    files = sorted(f for f in os.listdir(folder) if f.startswith(prefix))
    for f in files[:max(0, len(files) - keep)]:
        try:
            os.remove(os.path.join(folder, f))
        except OSError as e:
            log.warning("не смог удалить старый бэкап %s: %r", f, e)


def save_local_backup(st) -> str:
    """Ежедневный бэкап на диск сервера: ~/torrbot/data/backups/torrbot-backup-ГГГГММДД-ЧЧММ.tar.gz.
    Хранятся последние BACKUP_KEEP. Вернёт путь."""
    folder = st.cfg.backup_local_dir
    os.makedirs(folder, exist_ok=True)
    data, _ = make_backup(st)
    path = os.path.join(folder, f"{BACKUP_PREFIX}{now_local():%Y%m%d-%H%M}.tar.gz")
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)
    prune(folder, BACKUP_PREFIX, max(1, st.cfg.backup_keep))
    return path


def local_backups(st) -> list[tuple[str, int]]:
    folder = st.cfg.backup_local_dir
    if not os.path.isdir(folder):
        return []
    return [(f, os.path.getsize(os.path.join(folder, f))) for f in sorted(os.listdir(folder))
            if f.endswith((".tar.gz", ".sqlite3"))]


def snapshot_before_update(db_path: str, version: str, folder: str) -> str | None:
    """Вызывается при старте ДО открытия базы: если версия бота сменилась (обновление или откат),
    сохраняет копию базы в backups/before-vНОВАЯ-from-vСТАРАЯ-дата.sqlite3 — до того, как новый
    код что-то в ней поменяет. Работает для любых будущих обновлений. Вернёт путь копии."""
    marker = os.path.join(os.path.dirname(db_path) or ".", "LAST_VERSION")
    old = ""
    if os.path.isfile(marker):
        with open(marker) as f:
            old = f.read().strip()
    out = None
    if old != version and os.path.isfile(db_path):
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, f"before-v{version}-from-v{old or 'old'}-{now_local():%Y%m%d-%H%M}.sqlite3")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(out)
        src.backup(dst)
        dst.close()
        src.close()
        prune(folder, "before-v", PRE_UPDATE_KEEP)
    if old != version:
        os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
        with open(marker, "w") as f:
            f.write(version)
    return out


async def send_backup(bot: Bot, st) -> None:
    data, names = make_backup(st)
    fname = f"{BACKUP_PREFIX}{now_local():%Y%m%d}.tar.gz"
    local = local_backups(st)
    cap = ("🗄 Бэкап torrbot: " + ", ".join(names) +
           f"\nНа сервере ещё {len(local)} копий в ~/torrbot/data/backups." +
           "\nВосстановить: sudo sh ~/torrbot/restore.sh <файл>" +
           "\n⚠ Внутри пароли и токены — не пересылай никому.")
    for a in st.cfg.admin_ids:
        try:
            await bot.send_document(a, BufferedInputFile(data, fname), caption=cap)
        except Exception as e:
            log.warning("бэкап не отправился: %r", e)


async def daily_backup_loop(bot: Bot, st) -> None:
    """Каждый день в BACKUP_HOUR — бэкап на диск сервера. Не получилось — сообщить админу."""
    while True:
        now = now_local()
        t = now.replace(hour=st.cfg.backup_hour % 24, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        await asyncio.sleep((t - now).total_seconds())
        try:
            path = await asyncio.to_thread(save_local_backup, st)
            log.info("ежедневный бэкап: %s", path)
        except Exception as e:
            log.warning("ежедневный бэкап не получился: %r", e)
            await notify_admins(bot, st, f"⚠ Ежедневный бэкап не получился: {esc(str(e))}")
        await asyncio.sleep(60)


def next_weekly(now: datetime, day: int, hour: int) -> datetime:
    t = now.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=(day - now.weekday()) % 7)
    return t if t > now else t + timedelta(days=7)


async def weekly_loop(bot: Bot, st) -> None:
    while True:
        now = now_local()
        await asyncio.sleep((next_weekly(now, st.cfg.weekly_day, st.cfg.weekly_hour) - now).total_seconds())
        try:
            await notify_admins(bot, st, await weekly_report(st))
            await send_backup(bot, st)
        except Exception as e:
            log.warning("еженедельное: %r", e)
        await asyncio.sleep(60)


# ================= 17. «🎲 Что посмотреть» =================
def kodi_poster(art: dict | None) -> str | None:
    """Обложка из Kodi: «image://https%3a%2f%2fimage.tmdb.org%2ft%2fp%2foriginal%2fx.jpg/» →
    https-ссылка (размер w500 — «original» бывает больше 5 МБ, Telegram такое по ссылке не берёт).
    Локальные файлы и прочее, что не открыть снаружи, — None."""
    url = (art or {}).get("poster") or (art or {}).get("thumb") or ""
    if url.startswith("image://"):
        url = unquote(url[len("image://"):]).rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None
    return url.replace("/t/p/original/", "/t/p/w500/")


async def random_pick(st, uid: int = 0) -> tuple[str, InlineKeyboardMarkup | None, str | None]:
    """(текст, клавиатура, обложка). Сначала — непросмотренное из медиатеки Kodi, иначе — из TMDB.
    В детском режиме — только мультфильмы и семейное из TMDB."""
    from .kids import is_kid
    kid = is_kid(st, uid)
    can_dl = st.hooks.get("may_download", lambda _: True)(uid) if uid else True
    more = st.hooks["random_rows"](uid) if uid and "random_rows" in st.hooks else []
    if st.kodi and not kid and can_dl:
        try:
            res = await st.kodi.call("VideoLibrary.GetMovies", {
                "properties": ["title", "year", "plot", "rating", "art", "uniqueid"],
                "filter": {"field": "playcount", "operator": "is", "value": "0"}})
            movies = (res or {}).get("movies") or []
            if movies:
                m = random.choice(movies)
                poster = kodi_poster(m.get("art"))
                tid = (m.get("uniqueid") or {}).get("tmdb")
                if not poster and tid and st.cfg.tmdb_key:
                    try:
                        info = await tmdb.details(st.tmdb_http, st.cfg.tmdb_key, "movie", tid, st.cfg.tmdb_lang)
                        poster = info.poster if info else None
                    except Exception as e:
                        log.info("random: обложка из TMDB: %r", e)
                plot = (m.get("plot") or "")[:500]
                return (f"🎲 Уже на диске, ещё не смотрели:\n\n🎬 <b>{esc(m.get('title') or '')}</b>"
                        f" ({m.get('year') or '—'})" + (f" · ⭐ {m['rating']:.1f}" if m.get("rating") else "")
                        + (f"\n\n{esc(plot)}" if plot else "") + f"\n\nНепросмотренных на диске: {len(movies)}",
                        kb([[B(text="🎲 Ещё", callback_data="rnd")]] + more), poster)
        except Exception as e:
            log.info("random: Kodi: %r", e)
    if not st.cfg.tmdb_key:
        return "На диске нет непросмотренного, а TMDB не настроен.", None, None
    params = tmdb.discover_params("m", "10751" if kid else "", "", "", "top", random.randint(1, 20))
    infos, _ = await tmdb.discover(st.tmdb_http, st.cfg.tmdb_key, "m", params, st.cfg.tmdb_lang)
    if not infos:
        return "Не получилось ничего подобрать, попробуй ещё раз.", kb([[B(text="🎲 Ещё", callback_data="rnd")]]), None
    info = random.choice(infos)
    cid = st.put_choice(info.title, [info], [])
    head = "🎲 На диске непросмотренного нет. Как насчёт:" if (st.kodi and can_dl and not kid) else "🎲 Как насчёт:"
    return (f"{head}\n\n{info.caption(400)}",
            kb([[B(text="⬇ Найти раздачи" if can_dl else "🎞 Подробнее", callback_data=f"pk:{cid}:0"),
                 B(text="🎲 Ещё", callback_data="rnd")]] + more),
            info.poster)


# ================= обработчики =================
def build_router(st) -> Router:
    r = Router()
    cfg = st.cfg

    def admin(uid):
        return uid in cfg.admin_ids

    def allowed(uid):
        return st.is_allowed(uid)

    async def deny(cb: CallbackQuery) -> bool:
        if allowed(cb.from_user.id):
            return False
        await cb.answer("Нет доступа", show_alert=True)
        return True

    # --- озвучки ---
    @r.message(Command("voices"))
    async def voices(msg: Message):
        if not allowed(msg.from_user.id):
            return
        t, k = voices_view(st, msg.from_user.id)
        await msg.answer(t, reply_markup=k)

    @r.callback_query(F.data.regexp(r"^vo:(\d+|ok)$"))
    async def voice_toggle(cb: CallbackQuery):
        if await deny(cb):
            return
        uid, v = cb.from_user.id, cb.data[3:]
        if v == "ok":
            favs = fav_voices(st, uid)
            await cb.message.edit_text("🎙 Любимые озвучки: " + (esc(", ".join(favs)) if favs else "не выбраны")
                                       + ".\nИзменить — /voices")
            await cb.answer()
            return
        name = VOICES[int(v)][0]
        favs = fav_voices(st, uid)
        favs = [f for f in favs if f != name] if name in favs else favs + [name]
        st.db.set_pref(uid, "voices", "|".join(favs))
        t, k = voices_view(st, uid)
        await cb.message.edit_text(t, reply_markup=k)
        await cb.answer()

    # --- выбор сезонов ---
    @r.callback_query(F.data.regexp(r"^fs:[0-9a-f]{40}$"))
    async def files_open(cb: CallbackQuery):
        if await deny(cb):
            return
        h = cb.data[3:]
        if not st.hooks["can_cancel"](cb.from_user.id, h):
            await cb.answer("Это не твоя закачка", show_alert=True)
            return
        view = await files_view(st, h)
        if not view:
            await cb.answer("Список файлов ещё загружается — нажми через минуту", show_alert=True)
            return
        await cb.message.answer(view[0], reply_markup=view[1])
        await cb.answer()

    @r.callback_query(F.data.regexp(r"^fz:[0-9a-f]{40}:\d+$"))
    async def files_toggle(cb: CallbackQuery):
        if await deny(cb):
            return
        _, h, gi = cb.data.split(":")
        if not st.hooks["can_cancel"](cb.from_user.id, h):
            await cb.answer("Это не твоя закачка", show_alert=True)
            return
        files, wanted, _ = await st.tr.files(h)
        groups = file_groups(files)
        idx = groups[int(gi)][1]
        turn_on = not all(wanted[i] for i in idx)
        if not turn_on and all(not wanted[i] for i in range(len(files)) if i not in idx):
            await cb.answer("Хоть что-то нужно оставить 🙂", show_alert=True)
            return
        await st.tr.set_wanted(h, idx if turn_on else [], [] if turn_on else idx)
        view = await files_view(st, h)
        if view:
            await cb.message.edit_text(view[0], reply_markup=view[1])
        await cb.answer("Включено" if turn_on else "Выключено")

    @r.callback_query(F.data.regexp(r"^fd:[0-9a-f]{40}$"))
    async def files_done(cb: CallbackQuery):
        files, wanted, _ = await st.tr.files(cb.data[3:])
        on = [g for g, idx, _ in file_groups(files) if all(wanted[i] for i in idx)]
        total = sum(int(f.get("length") or 0) for f, w in zip(files, wanted) if w)
        await cb.message.edit_text(f"🗂 Качаю: {esc(', '.join(on) or 'выбранные файлы')} — {gb(total)}")
        await cb.answer()

    # --- все части (коллекция) ---
    @r.callback_query(F.data.regexp(r"^col:\d+$"))
    async def col_open(cb: CallbackQuery):
        if await deny(cb):
            return
        await cb.answer()
        try:
            name, parts = await tmdb.collection(st.tmdb_http, cfg.tmdb_key, int(cb.data[4:]), cfg.tmdb_lang)
        except Exception as e:
            log.warning("collection: %r", e)
            await cb.message.answer("TMDB сейчас не отвечает, попробуй позже.")
            return
        from .main import render_choice
        from .kids import only_kids
        parts = only_kids(st, cb.from_user.id, parts)
        cid = st.put_choice(name, parts, [])
        text, k = render_choice(f"📚 <b>{esc(name)}</b> — {len(parts)} фильм(ов). Выбери один или скачай все:",
                                parts, [], cid, None,
                                extra_rows=[[B(text=f"⚡ Скачать все лучшие ({len(parts)})",
                                               callback_data=f"cola:{cid}")]])
        await cb.message.answer(text, reply_markup=k)

    @r.callback_query(F.data.regexp(r"^cola:[0-9a-f]+$"))
    async def col_all(cb: CallbackQuery):
        if await deny(cb):
            return
        ch = st.choices.get(cb.data[5:])
        if not ch:
            await cb.answer("Список устарел, открой заново", show_alert=True)
            return
        if not st.hooks.get("may_download", lambda _: True)(cb.from_user.id):
            from .access import NO_DL
            return await cb.answer(NO_DL, show_alert=True)
        await cb.answer("Ищу раздачи…")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        uid, got, miss = cb.from_user.id, [], []
        for info in ch[2]:
            found = await asyncio.gather(*(jacred.search(st.http, cfg, q) for q in tmdb.tracker_queries(info)),
                                         return_exceptions=True)
            items = [it for res in found if not isinstance(res, BaseException) for it in res]
            results, _ = jacred.select(items, cfg)
            exact = order_for_user(st, uid, [x for x in results if tmdb.matches(info, x.title, x.is_series)])
            if not exact:
                miss.append(tmdb.short_label(info, 60))
                continue
            best = exact[0]
            await st.hooks["add_magnet"](cb.message, uid, best.magnet, best.title, False,
                                         info.poster, tmdb.folder_name(info))
            got.append(tmdb.short_label(info, 60))
        text = f"📚 Поставил: {len(got)} из {len(got) + len(miss)}."
        if miss:
            text += "\nНе нашёл подходящих раздач: " + esc(", ".join(miss))
        await cb.message.answer(text)

    # --- «Хотим посмотреть» (до v8; теперь — списки, /lists) ---
    async def info_of(kind: str, tid: int) -> tmdb.Info | None:
        if (kind, tid) in st.infos:
            return st.infos[(kind, tid)]
        row = next((w for w in st.db.wishlist() if w["kind"] == kind and w["tmdb_id"] == tid), None)
        if row:
            return tmdb.Info(tid, kind == "t", row["title"], "", row["year"] or "", "", 0.0, row["poster"])
        try:
            return await tmdb.details(st.tmdb_http, cfg.tmdb_key, "tv" if kind == "t" else "movie", tid, cfg.tmdb_lang)
        except Exception:
            return None

    @r.callback_query(F.data.regexp(r"^w[lvdr]:[mt]:\d+$"))
    async def wish_cb(cb: CallbackQuery):
        if await deny(cb):
            return
        act, kind, tid = cb.data.split(":")
        tid, uid = int(tid), cb.from_user.id
        if "open_add" in st.hooks:                      # v8: старые кнопки → списки
            if act in ("wl", "wv"):
                return await st.hooks["open_add"](cb, kind, tid)
            if act == "wd":
                info = await info_of(kind, tid)
                await cb.answer()
                if info:
                    await st.hooks["search_for"](cb.message, info, uid=uid)
                return
            return await cb.answer("«Хотим посмотреть» переехал в списки — /lists", show_alert=True)
        if act == "wl":
            info = await info_of(kind, tid)
            if not info:
                await cb.answer("Не получилось, попробуй позже", show_alert=True)
                return
            if st.db.wish_add(kind, tid, info.title, info.year, info.poster, uid):
                await cb.answer("Добавлено в «Хотим посмотреть» — /want", show_alert=True)
            else:
                voted = st.db.wish_vote(kind, tid, uid)
                await cb.answer("Уже в списке — твой 👍 " + ("добавлен" if voted else "убран"), show_alert=True)
            return
        if act == "wd":
            info = await info_of(kind, tid)
            await cb.answer()
            if info:
                await st.hooks["search_for"](cb.message, info)
            return
        if act == "wv":
            st.db.wish_vote(kind, tid, uid)
        elif act == "wr":
            row = next((w for w in st.db.wishlist() if w["kind"] == kind and w["tmdb_id"] == tid), None)
            if row and not admin(uid) and row["added_by"] != uid:
                await cb.answer("Удалить может тот, кто добавил, или админ", show_alert=True)
                return
            st.db.wish_remove(kind, tid)
        t, k = wish_view(st)
        try:
            await cb.message.edit_text(t, reply_markup=k)
        except Exception:
            pass
        await cb.answer()

    # --- пауза и приоритет (кнопки в /status) ---
    @r.callback_query(F.data.regexp(r"^t[pu]:[0-9a-f]{40}$"))
    async def pause_prio(cb: CallbackQuery):
        if await deny(cb):
            return
        act, h = cb.data.split(":")
        if not st.hooks["can_cancel"](cb.from_user.id, h):
            await cb.answer("Это не твоя закачка", show_alert=True)
            return
        ts = await st.tr.get([h])
        if not ts:
            await cb.answer("Этой закачки уже нет", show_alert=True)
            return
        t = ts[0]
        name = st.hooks["nice_name"](t) if "nice_name" in st.hooks else t["name"]
        if act == "tu":
            await st.tr.start_now(h)
            peers = t.get("peersSendingToUs")
            tip = f" Скорость зависит от раздающих: сейчас их {peers}." if peers is not None else ""
            await cb.answer(f"⬆ {name[:60]} — теперь качается первой.{tip} Обновить — /status",
                            show_alert=True)
            return
        if t.get("status") == 0:
            await st.tr.start(h)
            note = "▶ Продолжаю"
        else:
            await st.tr.stop(h)
            note = "⏸ На паузе"
        await cb.answer(f"{note}: {name[:60]}. Обновить — /status", show_alert=True)

    # --- сторож, отчёт, бэкап (админ) ---
    @r.message(Command("health"))
    async def health(msg: Message):
        if not admin(msg.from_user.id):
            return
        res = await check_all(st)
        lines = ["🩺 <b>Состояние</b>"] + [f"{'✅' if ok else '⚠'} {NAMES[k]}: {esc(d)}" for k, (ok, d) in res.items()]
        if "load_summary" in st.hooks:
            lines += st.hooks["load_summary"]()
        if getattr(st, "ai", None) is not None:
            lines.append(f"🤖 ИИ для /plot: {esc(st.ai.status())}")
        if st.kodi_jobs:
            lines.append("⏳ Kodi: жду малинку, чтобы " + ", ".join(
                {"scan": "обновить медиатеку", "clean": "почистить медиатеку"}.get(j, j) for j in st.kodi_jobs))
        await msg.answer("\n".join(lines))

    @r.message(Command("report"))
    async def report(msg: Message):
        if admin(msg.from_user.id):
            await msg.answer(await weekly_report(st))

    @r.message(Command("backup"))
    async def backup(msg: Message, bot: Bot):
        """Бэкап сейчас: копия на диск сервера + файл в Telegram."""
        if admin(msg.from_user.id):
            try:
                await asyncio.to_thread(save_local_backup, st)
            except Exception as e:
                await msg.answer(f"⚠ На диск сервера не сохранилось: {esc(str(e))}")
            await send_backup(bot, st)

    @r.message(Command("backups"))
    async def backups(msg: Message):
        """Какие копии лежат на сервере и как восстановить."""
        if not admin(msg.from_user.id):
            return
        files = local_backups(st)
        lines = [f"• <code>{esc(f)}</code> — {sz / 1024:.0f} КБ" for f, sz in files[-20:]]
        await msg.answer(
            "🗄 <b>Копии на сервере</b> (~/torrbot/data/backups):\n" + ("\n".join(lines) or "пока нет") +
            f"\n\nЕжедневно в {st.cfg.backup_hour}:00, хранятся последние {st.cfg.backup_keep}; "
            "перед каждым обновлением — копия базы before-v….\n"
            "Еженедельно и по /backup — файл сюда, в Telegram.\n\n"
            "Восстановить базу: <code>sudo sh ~/torrbot/restore.sh</code> — покажет список, "
            "потом <code>sudo sh ~/torrbot/restore.sh имя-файла</code>.")

    # --- что посмотреть ---
    @r.message(Command("random"))
    async def rnd(msg: Message):
        if not allowed(msg.from_user.id):
            return
        t, k, poster = await random_pick(st, msg.from_user.id)
        await st.hooks["send_with_poster"](msg.bot, st, msg.chat.id, t, poster, reply_markup=k)

    @r.callback_query(F.data == "rnd")
    async def rnd_more(cb: CallbackQuery):
        if await deny(cb):
            return
        await cb.answer()
        t, k, poster = await random_pick(st, cb.from_user.id)
        await st.hooks["send_with_poster"](cb.bot, st, cb.message.chat.id, t, poster, reply_markup=k)

    return r
