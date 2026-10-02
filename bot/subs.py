"""v7: подписка на сериалы и «⏳ ждать хорошее качество».

ПОДПИСКА. Кнопка «🔔 Следить за новыми сериями» — в сообщениях о закачке сериала и под
обложкой сериала; список и отписка — /podpiski. Подписаться может любой; на один сериал —
сколько угодно человек, уведомления получают все.

Как работает (раз в SUBS_CHECK_HOURS):
1. Русские раздачи сериалов обычно живут в одной теме трекера, и её обновляют: «серии 1–5
   из 10» → «1–6 из 10». Бот запоминает тему, из которой качали, и следит за ней — та же
   озвучка и качество. Тема обновилась (новый хеш) — ставит её в ту же папку: Transmission
   проверит уже скачанные файлы и докачает новое. Старая раздача убирается, когда новая
   докачается (файлы не трогаются, если папка та же).
2. Серии, которые уже удалили (вручную или автоочисткой), второй раз не качаются: бот
   помнит имена файлов и отмечает их в новой раздаче «не качать».
3. Подписались из карточки сериала, а качать ещё нечего — бот сам выберет лучшую раздачу
   текущего сезона и дальше следит за ней. Начался новый сезон — найдёт его тему.
4. По TMDB серия вышла SUBS_FALLBACK_DAYS дней назад, а тема не обновилась — бот предложит
   подписчикам другие раздачи с этой серией (кнопками).
5. Сериал закончился и всё скачано — подписка снимается, подписчикам сообщение.

ЖДАТЬ КАЧЕСТВО. Для фильма нашлись только раздачи хуже WAIT_MIN_HEIGHT или не нашлось
вовсе — кнопка «⏳ Ждать хорошую раздачу». Раз в WAIT_CHECK_HOURS бот ищет снова и, как
только появится, ставит на закачку и пишет. Через WAIT_DAYS дней ожидание снимается.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from datetime import date

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

from . import jacred, kids, tmdb
from .access import NO_DL, may_download

log = logging.getLogger("torrbot")
esc = html.escape
ALTS_TTL = 3 * 86400


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


# ---------- сезон и серии из заголовка раздачи ----------
_SXE = re.compile(r"\bs(\d{1,2})\s*e(\d{1,3})(?:\s*[-–]\s*(?:s\d{1,2})?\s*e?(\d{1,3}))?", re.I)
_NXM = re.compile(r"\b(\d{1,2})x(\d{2,3})(?:\s*[-–]\s*(?:\d{1,2}x)?(\d{2,3}))?", re.I)
_SEASON = [re.compile(r"(?:сезон|season)\s*[:№#]?\s*(\d{1,2})", re.I),
           re.compile(r"(\d{1,2})\s*(?:-?й\s*)?сезон", re.I),
           re.compile(r"\bs(\d{1,2})\b", re.I)]
_EPS = [re.compile(r"(?:серии|серия|эпизоды|эпизод|episodes?)\s*:?\s*(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?", re.I),
        re.compile(r"(\d{1,3})\s*[-–]\s*(\d{1,3})\s*(?:серии|серия|эпизод)", re.I),
        re.compile(r"\[(\d{1,3})\s*[-–]\s*(\d{1,3})\s*(?:из|of)\s*\d{1,3}\]", re.I)]
_TOTAL = re.compile(r"(?:из|of)\s*(\d{1,3})\b", re.I)


def span(title: str) -> tuple[int | None, int | None, int | None]:
    """(сезон, последняя серия, всего серий в сезоне) — что удалось понять из заголовка."""
    t = title or ""
    season = last = None
    m = _SXE.search(t) or _NXM.search(t)
    if m:
        season, last = int(m.group(1)), int(m.group(3) or m.group(2))
    if season is None:
        for rx in _SEASON:
            m = rx.search(t)
            if m:
                season = int(m.group(1))
                break
    if last is None:
        for rx in _EPS:
            m = rx.search(t)
            if m:
                last = int(m.group(2) or m.group(1))
                break
    tm = _TOTAL.search(t)
    total = int(tm.group(1)) if tm else None
    return season, last, total


def span_text(season: int | None, last: int | None) -> str:
    if season and last:
        return f"сезон {season}, серии по {last}-ю"
    if season:
        return f"сезон {season}"
    return f"серии по {last}-ю" if last else "новая версия раздачи"


def aired(state: dict | None) -> tuple[int, int, date | None] | None:
    """Последняя вышедшая серия по TMDB: (сезон, серия, дата)."""
    ep = (state or {}).get("last_episode_to_air") or {}
    if not ep.get("season_number"):
        return None
    try:
        d = date.fromisoformat(ep.get("air_date") or "")
    except ValueError:
        d = None
    return int(ep["season_number"]), int(ep.get("episode_number") or 0), d


def basenames_under(folder: str) -> set[str]:
    out = set()
    for _root, _dirs, files in os.walk(folder):
        out.update(files)
    return out


# ---------- подписка ----------
async def info_for_row(st, row) -> tmdb.Info | None:
    cfg = st.cfg
    if not cfg.tmdb_key:
        return None
    try:
        if row["tmdb_id"] and row["tmdb_kind"] == "t":
            return await tmdb.details(st.tmdb_http, cfg.tmdb_key, "tv", row["tmdb_id"], cfg.tmdb_lang)
        name = row["label"] or jacred.ru_title(row["title"] or "")
        q, year = tmdb.split_query(name)
        return tmdb.pick(await tmdb.search(st.tmdb_http, cfg.tmdb_key, q, cfg.tmdb_lang), year, True)
    except Exception as e:
        log.info("subs: tmdb %r", e)
        return None


async def find_topic(st, info: tmdb.Info, row) -> tuple[jacred.Release | None, bool]:
    """Тема трекера для закачки, у которой её не запомнили (поставлена до v7 или magnet-ссылкой):
    (раздача, это та же самая раздача?). Сначала ищем тот же хеш, иначе лучшую раздачу того же
    сезона — качать её сразу не будем, только следить, чтобы не скачать сезон второй раз."""
    from .extras import order_for_user
    found = await asyncio.gather(*(jacred.search(st.http, st.cfg, q) for q in tmdb.tracker_queries(info)),
                                 return_exceptions=True)
    items = [it for res in found if not isinstance(res, BaseException) for it in res]
    rels = [r for r in (jacred.parse(it) for it in items) if r and r.details]
    same = [r for r in rels if r.infohash == row["hash"]]
    if same:
        return same[0], True
    season = span(row["title"] or "")[0]
    results, _ = jacred.select(items, st.cfg)
    cands = [r for r in results if r.details and r.is_series and tmdb.matches(info, r.title, True)
             and (season is None or span(r.title)[0] == season)]
    return (order_for_user(st, row["user_id"] or 0, cands)[0] if cands else None), False


async def subscribe(st, uid: int, chat_id: int, info: tmdb.Info, row=None) -> tuple[int | None, str]:
    """Подписать uid на сериал. row — закачка, из которой подписались (тема, хеш)."""
    if not info.is_tv:
        return None, "Подписка бывает только на сериалы."
    ok, why = await kids.allowed(st, uid, info)
    if not ok:
        return None, why
    folder = (row["label"] if row is not None and row["label"] else None) or tmdb.folder_name(info)
    existing = st.db.sub_by_tmdb(info.tmdb_id)
    if row is None and existing is None:                 # закачки нет — может, сериал уже качали
        rows = [r for r in st.db.c.execute("SELECT * FROM downloads WHERE tmdb_kind='t' AND tmdb_id=?"
                                           " AND removed=0 ORDER BY added_at DESC", (info.tmdb_id,))]
        row = rows[0] if rows else None
        if row is not None and row["label"]:
            folder = row["label"]
    season = last = details = None
    h = prev = None
    if row is not None:
        season, last, _ = span(row["title"] or "")
        details, h = row["details"], row["hash"]
        if not details and (existing is None or not existing["details"]):
            try:
                rel, same = await find_topic(st, info, row)
            except Exception as e:
                log.info("subs: тема не нашлась: %r", e)
                rel, same = None, False
            if rel is not None:
                details = rel.details
                if same:
                    st.db.set_download(row["hash"], details=details)
                else:      # другая раздача того же сезона: следим за ней, а нашу закачку она заменит потом
                    h, prev = rel.infohash, row["hash"]
                    season, last = span(rel.title)[0] or season, span(rel.title)[1]
    sid = st.db.sub_create(info.tmdb_id, info.title, info.year, info.poster, folder, uid, details, h, season, last)
    sub = st.db.sub_get(sid)
    if prev and sub["hash"] == h:
        st.db.sub_update(sid, prev_hash=prev)
    if row is not None and not sub["details"] and details:     # подписка была без темы — теперь есть
        st.db.sub_update(sid, details=details, hash=h, prev_hash=prev, season=season, last_ep=last)
    if row is not None:
        st.db.set_download(row["hash"], sub_id=sid)
    st.db.sub_join(sid, uid, chat_id)
    sub = st.db.sub_get(sid)
    every = f"{st.cfg.subs_check_hours:g} ч"
    if sub["details"]:
        return sid, (f"🔔 Подписка на <b>{esc(info.title)}</b>: слежу за раздачей, новые серии "
                     f"буду качать сам и напишу (проверяю раз в {every}). Список — /podpiski")
    return sid, (f"🔔 Подписка на <b>{esc(info.title)}</b>: сейчас найду раздачу текущего сезона и поставлю, "
                 f"дальше буду докачивать новые серии (проверяю раз в {every}). Список — /podpiski")


async def tell(bot: Bot, st, sid: int, text: str, markup=None, skip: int | None = None) -> None:
    for u in st.db.sub_users(sid):
        if u["user_id"] == skip:
            continue
        try:
            await bot.send_message(u["chat_id"], text, reply_markup=markup)
        except Exception as e:
            log.info("subs: не смог написать %s: %r", u["user_id"], e)


async def torrent_size(st, h: str | None) -> int:
    if not h:
        return 0
    try:
        ts = await st.tr.get([h])
        return int(ts[0].get("totalSize") or 0) if ts else 0
    except Exception:
        return 0


async def take(bot: Bot, st, sub, rel: jacred.Release, replace: bool) -> str | None:
    """Поставить раздачу для подписки. replace — это новая версия той же темы/замена (старую уберём,
    когда новая докачается); иначе (новый сезон) старая остаётся."""
    users = st.db.sub_users(sub["id"])
    if not users:
        return None
    owner = next((u for u in users if u["user_id"] == sub["created_by"]), users[0])
    old = sub["hash"] if replace else None
    size = max(0, rel.size - await torrent_size(st, old)) if old else rel.size
    keep_prev = sub["prev_hash"]
    drop = None
    if replace and keep_prev and old and old != keep_prev:
        # прошлое обновление ещё не докачалось — его бросим, «прошлой» останется докачанная раздача
        drop, old = old, keep_prev
    h = await st.hooks["enqueue"](bot, uid=owner["user_id"], chat_id=owner["chat_id"], magnet=rel.magnet,
                                  title=rel.title, is_series=True, poster=sub["poster"],
                                  subfolder=sub["folder"], size=size, details=rel.details,
                                  tmdb_kind="t", tmdb_id=sub["tmdb_id"], sub_id=sub["id"], quiet=True)
    if not h:
        return None
    if drop and drop != h:
        try:
            await st.tr.remove(drop, delete_data=False)
        except Exception as e:
            log.info("subs: не смог убрать недокачанное обновление: %r", e)
        st.db.mark_removed(drop, "replaced")
    season, last, _ = span(rel.title)
    prev = (old if old and old != h else None) if replace else keep_prev
    st.db.sub_update(sub["id"], details=rel.details or None, hash=h, prev_hash=prev,
                     season=season or sub["season"], last_ep=last or (sub["last_ep"] if replace else None),
                     changed_at=int(time.time()), offered=None)
    return h


async def check_sub(bot: Bot, st, sub, now: float | None = None) -> str:
    """Одна проверка подписки. Вернёт, что сделали (для журнала и тестов)."""
    from .extras import order_for_user
    cfg = st.cfg
    now = now or time.time()
    sid = sub["id"]
    state, info = None, None
    if cfg.tmdb_key:
        try:
            state = await tmdb.tv_state(st.tmdb_http, cfg.tmdb_key, sub["tmdb_id"], cfg.tmdb_lang)
            info = tmdb._to_info({**state, "media_type": "tv"})
        except Exception as e:
            log.info("subs: tmdb %s: %r", sub["title"], e)
    info = info or tmdb.Info(sub["tmdb_id"], True, sub["title"], "", sub["year"] or "", "", 0.0, sub["poster"])
    found = await asyncio.gather(*(jacred.search(st.http, cfg, q) for q in tmdb.tracker_queries(info)),
                                 return_exceptions=True)
    items = [it for res in found if not isinstance(res, BaseException) for it in res]
    st.db.sub_update(sid, checked_at=int(now))
    if not items:
        return "нет ответа трекеров" if found and all(isinstance(r, BaseException) for r in found) else "пусто"
    results, _ = jacred.select(items, cfg)
    exact = [r for r in results if r.is_series and tmdb.matches(info, r.title, True)]
    owner = sub["created_by"] or 0
    last_air = aired(state)
    users = st.db.sub_users(sid)
    if not users:
        st.db.sub_delete(sid)
        return "подписчиков нет"

    # 1. тема, за которой следим, обновилась
    if sub["details"]:
        same = sorted((r for r in results if r.details == sub["details"]), key=lambda r: -r.seeders)
        if same and same[0].infohash != sub["hash"]:
            rel = same[0]
            if await take(bot, st, sub, rel, replace=True):
                s, last, _ = span(rel.title)
                await tell(bot, st, sid, f"🔔 <b>{esc(sub['title'])}</b>: раздача обновилась "
                                         f"({span_text(s, last)}) — качаю, напишу, когда будет готово.")
                return "обновил тему"
    # 2. новый сезон (или подписка без темы): лучшая раздача последнего вышедшего сезона
    want_season = last_air[0] if last_air else max((span(r.title)[0] or 0 for r in exact), default=0)
    if want_season and (not sub["details"] or want_season > (sub["season"] or 0)):
        cands = [r for r in exact if span(r.title)[0] == want_season and r.details != sub["details"]]
        if cands and not sub["details"] and sub["hash"] and want_season == (sub["season"] or want_season):
            # этот сезон уже качали (до v7 или magnet-ссылкой) — сейчас второй раз не качаем, просто следим
            # за темой; когда в ней появятся новые серии, новая раздача заменит старую закачку
            best = order_for_user(st, owner, cands)[0]
            st.db.sub_update(sid, details=best.details, hash=best.infohash, prev_hash=sub["hash"],
                             season=want_season, last_ep=span(best.title)[1])
            return "нашёл тему для уже скачанного"
        if cands:
            rel = order_for_user(st, owner, cands)[0]
            new_season = bool(sub["details"])
            if await take(bot, st, sub, rel, replace=False):
                s, last, _ = span(rel.title)
                head = "начался новый сезон" if new_season else "нашёл раздачу"
                await tell(bot, st, sid, f"🔔 <b>{esc(sub['title'])}</b>: {head} — {span_text(s, last)}. "
                                         f"Качаю: <i>{esc(rel.title[:150])}</i>")
                return "новый сезон" if new_season else "взял тему"
    sub = st.db.sub_get(sid)
    # 3. серия давно вышла, а тема не обновляется — предложить другие раздачи
    if last_air and sub["details"] and last_air[2] and last_air[0] == (sub["season"] or 0) \
            and (sub["last_ep"] or 0) < last_air[1] \
            and (date.today() - last_air[2]).days >= cfg.subs_fallback_days \
            and sub["offered"] != f"{last_air[0]}:{last_air[1]}":
        S, E, _ = last_air
        alts = [r for r in exact if r.details != sub["details"] and span(r.title)[0] in (S, None)
                and (span(r.title)[1] or 0) >= E]
        alts = order_for_user(st, owner, alts)[:3]
        st.db.sub_update(sid, offered=f"{S}:{E}")
        if alts:
            st.sub_alts[sid] = (now, alts)
            lines = [f"🔔 <b>{esc(sub['title'])}</b>: серия {S}×{E:02d} вышла {cfg.subs_fallback_days}+ дн. назад, "
                     f"а раздача, за которой слежу, не обновилась. Есть другие:"]
            for i, r in enumerate(alts, 1):
                lines.append(f"<b>{i}.</b> {esc(r.title[:150])}\n   <code>{esc(r.short_line())}</code>")
            lines.append("«🔁 N» — перейти на эту раздачу (уже скачанное заменится на неё).")
            await tell(bot, st, sid, "\n\n".join(lines),
                       kb([[B(text=f"🔁 {i}", callback_data=f"sbs:{sid}:{i - 1}") for i in range(1, len(alts) + 1)]]))
            return "предложил другие"
    # 4. сериал закончился и всё скачано
    if state and state.get("status") in ("Ended", "Canceled") and last_air and sub["details"] \
            and (sub["season"] or 0) >= last_air[0] and (sub["last_ep"] or 0) >= last_air[1] \
            and now - (sub["changed_at"] or now) >= 14 * 86400:
        await tell(bot, st, sid, f"🏁 <b>{esc(sub['title'])}</b> завершён, все серии скачаны — подписку снимаю.")
        st.db.sub_delete(sid)
        return "завершён"
    return "без изменений"


async def maintain(st, sub) -> None:
    """Отметить «не качать» уже удалённые серии в новой раздаче и убрать прошлую раздачу,
    когда новая докачалась."""
    h = sub["hash"]
    if not h:
        return
    hashes = [x for x in (h, sub["prev_hash"]) if x]
    ts = {t["hashString"].lower(): t for t in await st.tr.get(hashes)}
    t = ts.get(h)
    if t is None:
        return
    folder = f"{st.cfg.dir_series.rstrip('/')}/{sub['folder']}"
    if float(t.get("metadataPercentComplete", 1)) >= 1 and h not in st.sub_meta_done:
        files, _wanted, meta = await st.tr.files(h)
        if files and meta >= 1:
            names = [os.path.basename(f.get("name") or "") for f in files]
            known = st.db.sub_files(sub["id"])
            present = await asyncio.to_thread(basenames_under, folder)
            skip = [i for i, n in enumerate(names) if n in known and n not in present]
            if skip and len(skip) < len(names):
                await st.tr.set_wanted(h, [], skip)
                log.info("subs: %s — не качаю уже удалённые серии: %d", sub["title"], len(skip))
            st.db.sub_files_add(sub["id"], [n for n in names if n])
            st.sub_meta_done.add(h)
    prev = sub["prev_hash"]
    if prev and float(t.get("percentDone") or 0) >= 1:
        old = ts.get(prev)
        if old is not None:
            root = lambda x: f"{(x.get('downloadDir') or '').rstrip('/')}/{x.get('name')}"     # noqa: E731
            same = root(old) == root(t)
            await st.tr.remove(prev, delete_data=not same and root(old).startswith(st.cfg.dir_series.rstrip("/") + "/"))
            log.info("subs: %s — убрал прошлую раздачу (%s)", sub["title"], "файлы оставил" if same else "с файлами")
        st.db.mark_removed(prev, "replaced")
        st.db.sub_update(sub["id"], prev_hash=None)


# ---------- ждать качество ----------
async def check_waits(bot: Bot, st, now: float | None = None) -> int:
    """Вернёт, сколько поставили на закачку."""
    from .extras import order_for_user
    cfg = st.cfg
    now = now or time.time()
    groups: dict[tuple[str, int], list] = {}
    for w in st.db.waits():
        groups.setdefault((w["kind"], w["tmdb_id"]), []).append(w)
    got = 0
    for (kind, tid), ws in groups.items():
        first = ws[0]
        if now - (first["created_at"] or now) > cfg.wait_days * 86400:
            st.db.wait_remove(kind, tid)
            for w in ws:
                try:
                    await bot.send_message(w["chat_id"], f"⏳ Перестал ждать <b>{esc(first['title'])}</b>: "
                                                         f"за {cfg.wait_days} дн. хорошей раздачи так и не появилось.")
                except Exception:
                    pass
            continue
        if first["checked_at"] and now - first["checked_at"] < cfg.wait_check_hours * 3600:
            continue
        st.db.wait_checked(kind, tid)
        info = tmdb.Info(tid, kind == "t", first["title"], "", first["year"] or "", "", 0.0, first["poster"])
        if cfg.tmdb_key:
            try:
                info = await tmdb.details(st.tmdb_http, cfg.tmdb_key, "tv" if kind == "t" else "movie", tid,
                                          cfg.tmdb_lang) or info
            except Exception as e:
                log.info("waits: tmdb %r", e)
        found = await asyncio.gather(*(jacred.search(st.http, cfg, q) for q in tmdb.tracker_queries(info)),
                                     return_exceptions=True)
        items = [it for res in found if not isinstance(res, BaseException) for it in res]
        results, _ = jacred.select(items, cfg)
        good = [r for r in results if tmdb.matches(info, r.title, r.is_series)
                and (r.height or 0) >= cfg.wait_min_height]
        if not good:
            continue
        rel = order_for_user(st, first["user_id"], good)[0]
        sub = tmdb.folder_name(info)
        h = await st.hooks["enqueue"](bot, uid=first["user_id"], chat_id=first["chat_id"], magnet=rel.magnet,
                                      title=rel.title, is_series=rel.is_series, poster=info.poster or first["poster"],
                                      subfolder=sub, size=rel.size, details=rel.details,
                                      tmdb_kind=kind, tmdb_id=tid, quiet=True)
        if not h:
            continue                                   # места нет — refuse уже написал; попробуем в следующий раз
        st.db.wait_remove(kind, tid)
        got += 1
        for w in ws:
            try:
                await bot.send_message(w["chat_id"], f"⏳→⬇ Появилась хорошая раздача <b>{esc(info.title)}</b> "
                                                     f"({rel.height}p) — поставил на закачку.\n<i>{esc(rel.title[:150])}</i>")
            except Exception:
                pass
    return got


async def loop(bot: Bot, st) -> None:
    await asyncio.sleep(90)
    while True:
        now = time.time()
        for sub in st.db.subs():
            try:
                await maintain(st, sub)
                sub = st.db.sub_get(sub["id"])
                if sub and (not sub["checked_at"] or now - sub["checked_at"] >= st.cfg.subs_check_hours * 3600):
                    res = await check_sub(bot, st, sub, now)
                    log.info("подписка %s: %s", sub["title"], res)
            except Exception as e:
                log.warning("подписка %s: %r", sub["title"], e)
        try:
            await check_waits(bot, st)
        except Exception as e:
            log.warning("ожидания: %r", e)
        await asyncio.sleep(600)


# ---------- экран /podpiski ----------
def view(st, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    admin = uid in st.cfg.admin_ids
    subs = st.db.subs(None if admin else uid)
    waits = st.db.waits(None if admin else uid)
    lines, rows = [], []
    if subs:
        lines.append("🔔 <b>Подписки на сериалы</b>" + (" (вся семья)" if admin else "") + ":")
        for i, s in enumerate(subs, 1):
            who = ", ".join(st.hooks["short_name"](u["user_id"]) for u in st.db.sub_users(s["id"]))
            where = span_text(s["season"], s["last_ep"]) if s["details"] else "ищу раздачу"
            lines.append(f"<b>{i}.</b> 📺 {esc(s['title'])}" + (f" ({s['year']})" if s["year"] else "")
                         + f"\n   {where}" + (f" · 👤 {esc(who)}" if admin and who else ""))
            mine = st.db.is_subscribed(s["id"], uid)
            rows.append([B(text=f"🔕 {i}. {s['title'][:25]} — отписаться" if mine else f"🗑 {i}. {s['title'][:25]} — снять",
                           callback_data=f"sbu:{s['id']}")])
    if waits:
        lines.append("⏳ <b>Жду хорошую раздачу</b>:")
        seen = set()
        for w in waits:
            k = (w["kind"], w["tmdb_id"])
            if k in seen:
                continue
            seen.add(k)
            n = len(seen)
            lines.append(f"<b>{n}.</b> {'📺' if w['kind'] == 't' else '🎬'} {esc(w['title'])}"
                         + (f" ({w['year']})" if w["year"] else "") + f" — от {st.cfg.wait_min_height}p")
            rows.append([B(text=f"✖ {n}. {w['title'][:25]} — не ждать", callback_data=f"wtu:{w['kind']}:{w['tmdb_id']}")])
    if not lines:
        return ("Подписок нет. Подписаться на сериал — кнопка «🔔 Следить за новыми сериями» под обложкой "
                "сериала или в сообщении о закачке."), kb([])
    return "\n\n".join(lines), kb(rows)


def build_router(st) -> Router:
    r = Router()

    async def info_of(tid: int, kind: str = "t") -> tmdb.Info | None:
        if (kind, tid) in st.infos:
            return st.infos[(kind, tid)]
        if not st.cfg.tmdb_key:
            return None
        try:
            return await tmdb.details(st.tmdb_http, st.cfg.tmdb_key, "tv" if kind == "t" else "movie", tid,
                                      st.cfg.tmdb_lang)
        except Exception:
            return None

    async def after_subscribe(bot: Bot, sid: int | None) -> None:
        if not sid:
            return
        sub = st.db.sub_get(sid)
        if sub and not sub["details"]:                 # темы ещё нет — найти сразу, не ждать 10 минут
            try:
                await check_sub(bot, st, sub)
            except Exception as e:
                log.warning("подписка: первая проверка: %r", e)

    @r.message(Command("podpiski"))
    async def podpiski(msg: Message):
        if st.is_allowed(msg.from_user.id) and not may_download(st, msg.from_user.id):
            await msg.answer(NO_DL)
            return
        if st.is_allowed(msg.from_user.id):
            t, k = view(st, msg.from_user.id)
            await msg.answer(t, reply_markup=k)

    @r.callback_query(F.data.regexp(r"^sb:[0-9a-f]{40}$"))
    async def sub_hash(cb: CallbackQuery):
        uid = cb.from_user.id
        if not st.is_allowed(uid):
            await cb.answer("Нет доступа", show_alert=True)
            return
        if not may_download(st, uid):
            await cb.answer(NO_DL, show_alert=True)
            return
        row = st.db.get(cb.data[3:])
        if row is None:
            await cb.answer("Не нашёл эту закачку", show_alert=True)
            return
        if row["sub_id"] and st.db.is_subscribed(row["sub_id"], uid):
            await cb.answer("Ты уже подписан(а) — /podpiski", show_alert=True)
            return
        await cb.answer("Подписываю…")
        info = await info_for_row(st, row)
        if not info:
            await cb.message.answer("Не понял, что это за сериал в каталоге TMDB. Найди его по названию и "
                                    "подпишись кнопкой 🔔 под обложкой.")
            return
        sid, text = await subscribe(st, uid, cb.message.chat.id, info, row)
        await cb.message.answer(text)
        await after_subscribe(cb.bot, sid)

    @r.callback_query(F.data.regexp(r"^sbt:\d+$"))
    async def sub_info(cb: CallbackQuery):
        uid = cb.from_user.id
        if not st.is_allowed(uid):
            await cb.answer("Нет доступа", show_alert=True)
            return
        if not may_download(st, uid):
            await cb.answer(NO_DL, show_alert=True)
            return
        tid = int(cb.data[4:])
        sub = st.db.sub_by_tmdb(tid)
        if sub and st.db.is_subscribed(sub["id"], uid):
            await cb.answer("Ты уже подписан(а) — /podpiski", show_alert=True)
            return
        info = await info_of(tid)
        if not info:
            await cb.answer("TMDB не ответил, попробуй позже", show_alert=True)
            return
        await cb.answer("Подписываю…")
        sid, text = await subscribe(st, uid, cb.message.chat.id, info)
        await cb.message.answer(text)
        await after_subscribe(cb.bot, sid)

    @r.callback_query(F.data.regexp(r"^sbu:\d+$"))
    async def unsub(cb: CallbackQuery):
        uid = cb.from_user.id
        sid = int(cb.data[4:])
        sub = st.db.sub_get(sid)
        if not sub:
            await cb.answer("Такой подписки уже нет")
        elif st.db.is_subscribed(sid, uid):
            st.db.sub_leave(sid, uid)
            await cb.answer(f"Отписал(а) от «{sub['title']}». Скачанное остаётся на диске.", show_alert=True)
        elif uid in st.cfg.admin_ids:
            await tell(cb.bot, st, sid, f"🔕 Администратор снял подписку на <b>{esc(sub['title'])}</b>.")
            st.db.sub_delete(sid)
            await cb.answer("Подписка снята")
        else:
            await cb.answer("Это не твоя подписка", show_alert=True)
            return
        try:
            t, k = view(st, uid)
            await cb.message.edit_text(t, reply_markup=k)
        except Exception:
            pass

    @r.callback_query(F.data.regexp(r"^sbs:\d+:\d$"))
    async def switch(cb: CallbackQuery):
        _, sid, i = cb.data.split(":")
        sid, uid = int(sid), cb.from_user.id
        sub = st.db.sub_get(sid)
        got = st.sub_alts.get(sid)
        if not sub or not got or int(i) >= len(got[1]) or time.time() - got[0] > ALTS_TTL:
            await cb.answer("Варианты устарели — дождись следующей проверки", show_alert=True)
            return
        if not st.db.is_subscribed(sid, uid) and uid not in st.cfg.admin_ids:
            await cb.answer("Это не твоя подписка", show_alert=True)
            return
        rel = got[1][int(i)]
        st.sub_alts.pop(sid, None)
        await cb.answer("Переключаю…")
        if await take(cb.bot, st, sub, rel, replace=True):
            await tell(cb.bot, st, sid, f"🔁 <b>{esc(sub['title'])}</b>: перешёл на другую раздачу — "
                                        f"<i>{esc(rel.title[:150])}</i>. Старую уберу, когда новая докачается.")

    # --- ждать качество ---
    @r.callback_query(F.data.regexp(r"^wt:[mt]:\d+$"))
    async def wait_add(cb: CallbackQuery):
        uid = cb.from_user.id
        if not st.is_allowed(uid):
            await cb.answer("Нет доступа", show_alert=True)
            return
        if not may_download(st, uid):
            await cb.answer(NO_DL, show_alert=True)
            return
        _, kind, tid = cb.data.split(":")
        info = await info_of(int(tid), kind)
        if not info:
            await cb.answer("TMDB не ответил, попробуй позже", show_alert=True)
            return
        ok, why = await kids.allowed(st, uid, info)
        if not ok:
            await cb.answer(why, show_alert=True)
            return
        new = st.db.wait_add(kind, info.tmdb_id, info.title, info.year, info.poster, uid, cb.message.chat.id)
        await cb.answer((f"⏳ Жду раздачу от {st.cfg.wait_min_height}p. Проверяю раз в {st.cfg.wait_check_hours:g} ч — "
                         f"как появится, поставлю и напишу. Список — /podpiski") if new else "Уже жду — /podpiski",
                        show_alert=True)

    @r.callback_query(F.data.regexp(r"^wtu:[mt]:\d+$"))
    async def wait_drop(cb: CallbackQuery):
        _, kind, tid = cb.data.split(":")
        uid = cb.from_user.id
        st.db.wait_remove(kind, int(tid), None if uid in st.cfg.admin_ids else uid)
        await cb.answer("Больше не жду")
        try:
            t, k = view(st, uid)
            await cb.message.edit_text(t, reply_markup=k)
        except Exception:
            pass

    return r
