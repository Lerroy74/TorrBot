"""v8: /sovet — что посмотреть.

* 🎯 «Похожее на мои любимые» — без ИИ: рекомендации TMDB к фильмам, которые человек оценил на 8+
  (нет таких — к фильмам из его списков). Чем к большему числу любимых подходит, тем выше.
  Убирается то, что он уже оценил/отметил «не смотрел(а)» и что уже лежит в его списках.
* 👥 «Что посмотреть вместе» — для группы: непросмотренное из группового списка (по 👍) +
  рекомендации TMDB к тому, что высоко оценили несколько участников. Уже виденное кем-то из
  группы не убирается — только пометка «Лена уже видела (8)».
* 🤖 «Спросить ИИ» — свободный запрос + лучшие оценки человека → ИИ (через /ai-контроль) → TMDB.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

from . import ai, aictl, picks, tmdb
from .lists import is_kid, kid_ok_row, label, name_of, remember_info, seen_marks

log = logging.getLogger("torrbot")
esc = html.escape

SEEDS = 10
SHOW = 10


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def key(info: tmdb.Info) -> tuple[str, int]:
    return ("t" if info.is_tv else "m", info.tmdb_id)


async def seed_key(st, row) -> tuple[str, int] | None:
    """(kind, tmdb_id) записи журнала; нет — найти в TMDB по названию и запомнить."""
    if row["tmdb_id"]:
        return row["tmdb_kind"], row["tmdb_id"]
    if not st.cfg.tmdb_key:
        return None
    text, year = tmdb.split_query(row["label"] or "")
    try:
        cands = await tmdb.search(st.tmdb_http, st.cfg.tmdb_key, f"{text} {year or ''}".strip(), st.cfg.tmdb_lang)
    except Exception as e:
        log.info("подбор: tmdb search %r", e)
        return None
    info = tmdb.pick(cands, year, row["kind"] == "series")
    if not info:
        return None
    st.db.journal_set_tmdb(row["jid"], "t" if info.is_tv else "m", info.tmdb_id)
    return key(info)


async def collect(st, seeds: list[tuple[tuple[str, int], str]]) -> list[tuple[tmdb.Info, int, str]]:
    """[(карточка, к скольким «семенам» подходит, на что похоже)] — лучшие сверху."""
    async def one(k):
        return await tmdb.recommendations(st.tmdb_http, st.cfg.tmdb_key, k[0], k[1], st.cfg.tmdb_lang)
    got = await asyncio.gather(*(one(k) for k, _ in seeds), return_exceptions=True)
    score: dict[tuple[str, int], list] = {}
    for (k, name), res in zip(seeds, got):
        if isinstance(res, BaseException):
            log.info("подбор: tmdb recommendations %s: %r", k, res)
            continue
        for info in res:
            cur = score.setdefault(key(info), [info, 0, name])
            cur[1] += 1
    out = sorted(score.values(), key=lambda x: (x[1], x[0].rating), reverse=True)
    return [(i, n, s) for i, n, s in out]


def render(st, head: str, items: list[tuple[tmdb.Info, str]], extra_rows=None) -> tuple[str, InlineKeyboardMarkup]:
    """Список вариантов: кнопка — как в обычном поиске (раздачи или карточка, если качать нельзя)."""
    infos = [i for i, _ in items]
    cid = st.put_choice(head[:60], infos, [])
    lines, btns = [head], []
    for n, (info, note) in enumerate(items):
        rating = f" · ⭐ {info.rating:.1f}" if info.rating else ""
        lines.append(f"<b>{n + 1}.</b> {'📺' if info.is_tv else '🎬'} {esc(tmdb.short_label(info, 80))}{rating}"
                     + (f"\n   {esc(note)}" if note else ""))
        btns.append((n + 1, f"{n + 1}. {tmdb.short_label(info)}", f"pk:{cid}:{n}"))
        remember_info(st, info)
    rows, numbers = picks.numbered(btns, 3)                # v8.1: много — номер текстом
    rows += extra_rows or []
    text = picks.finish("\n\n".join(lines), numbers)
    st.remember(cid, text, kb(rows))
    return text, kb(rows)


def menu(st, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[B(text="🎯 Похожее на мои любимые", callback_data="Rs")]]
    for g in st.db.groups_of(uid):
        rows.append([B(text=f"👥 Что посмотреть вместе: {g['name'][:30]}", callback_data=f"Rg:{g['id']}")])
    has_ai = aictl.available(st, uid, "rec")
    if has_ai:
        rows.append([B(text="🤖 Спросить ИИ", callback_data="Ra")])
    return ("💡 <b>Что посмотреть?</b>\n🎯 — по твоим оценкам 8+ (без ИИ)\n👥 — для всей группы, с пометками, "
            "кто что уже видел" + ("\n🤖 — опиши словами, что хочется" if has_ai else ""), kb(rows))


async def similar(st, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    if not st.cfg.tmdb_key:
        return "Подбор работает только с ключом TMDB.", kb([])
    rows = st.db.top_rated([uid], 8, SEEDS)
    seeds = []
    for row in rows:
        k = await seed_key(st, row)
        if k and k not in [s for s, _ in seeds]:
            seeds.append((k, row["label"]))
    base = "твои оценки 8+"
    if not seeds:                                     # оценок нет — от того, что в его списках
        for lst in st.db.lists_of_user(uid):
            for it in st.db.list_items(lst["id"], True)[:SEEDS]:
                if len(seeds) < SEEDS:
                    seeds.append(((it["kind"], it["tmdb_id"]), label(it)))
        base = "фильмы из твоих списков"
    if not seeds:
        return ("🎯 Пока не от чего оттолкнуться: оцени пару любимых фильмов (найди по названию → «⭐ Оценить» "
                "или /ocenki) или добавь фильмы в список — и я подберу похожее."), kb([])
    answered = set(st.db.scores_by_tmdb(uid))
    mine = {(it["kind"], it["tmdb_id"]) for lst in st.db.lists_of_user(uid) for it in st.db.list_items(lst["id"], True)}
    skip = answered | mine | {k for k, _ in seeds}
    found = [(i, n, s) for i, n, s in await collect(st, seeds) if key(i) not in skip]
    if is_kid(st, uid):
        found = [x for x in found if tmdb.kid_ok(x[0])]
    if not found:
        return "🎯 Ничего нового не нашлось — оцени ещё что-нибудь, и попробуем снова.", kb([])
    items = [(i, f"похоже на: {s}" + (f" и ещё {n - 1}" if n > 1 else "")) for i, n, s in found[:SHOW]]
    return render(st, f"🎯 <b>Похожее на {base}</b>:", items,
                  [[B(text="💡 Другие варианты подбора", callback_data="Rm")]])


async def together(st, gid: int, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    g = st.db.group_get(gid)
    if g is None or not st.db.is_member(gid, uid):
        return "Ты не в этой группе.", kb([])
    members = {m["user_id"] for m in st.db.group_members(gid)}
    lid = st.db.group_main_list(gid)
    head = [f"👥 <b>{esc(g['name'])}: что посмотреть вместе</b>"]
    want = [it for it in st.db.list_items(lid, False) if not is_kid(st, uid) or kid_ok_row(it)][:5]
    if want:
        head.append("\n<b>Из вашего списка</b> (больше 👍 — выше):")
        for it in want:
            seen = seen_marks(st, it["kind"], it["tmdb_id"], members)
            head.append(f"• {'📺' if it['kind'] == 't' else '🎬'} {esc(label(it))}"
                        + (f" — 👍 {it['votes']}" if it["votes"] else "") + (f" · видели: {esc(seen)}" if seen else ""))
    have = {(it["kind"], it["tmdb_id"]) for lst in st.db.lists_of_group(gid) for it in st.db.list_items(lst["id"], True)}
    seeds = []
    for row in st.db.top_rated(sorted(members), 8, SEEDS):
        k = await seed_key(st, row)
        if k and k not in [s for s, _ in seeds]:
            seeds.append((k, row["label"]))
    items = []
    if seeds and st.cfg.tmdb_key:
        found = [(i, n, s) for i, n, s in await collect(st, seeds)
                 if key(i) not in have and key(i) not in {k for k, _ in seeds}]
        if is_kid(st, uid):
            found = [x for x in found if tmdb.kid_ok(x[0])]
        for info, n, s in found[:8]:
            seen = []
            for r in st.db.scores_for(*key(info)):
                if r["user_id"] in members and r["score"] is not None:
                    seen.append(f"{name_of(st, r['user_id'])} уже видел(а) ({r['score']})")
            items.append((info, "; ".join(seen) or f"похоже на: {s}"))
    rows = [[B(text="📋 Список группы", callback_data=f"Lv:{lid}:0:0")]]
    if not items:
        tail = ("\n\nЧтобы я подобрал новое, участникам стоит оценить любимые фильмы (8+)." if not seeds
                else "\n\nНовых рекомендаций не нашлось.")
        return "\n".join(head) + tail, kb(rows)
    return render(st, "\n".join(head) + "\n\n<b>Новое по любимым фильмам группы:</b>", items, rows)


def taste(st, uid: int) -> str:
    """Для ИИ: любимые (оценка) и что уже видел."""
    best = st.db.top_rated([uid], 7, 15)
    seen = st.db.c.execute("SELECT j.label FROM ratings r JOIN journal j ON j.id=r.jid WHERE r.user_id=?"
                           " ORDER BY r.at DESC LIMIT 40", (uid,)).fetchall()
    out = []
    if best:
        out.append("Любимые (оценка из 10): " + "; ".join(f"{r['label']} — {r['best']}" for r in best))
    if seen:
        out.append("Уже видел: " + "; ".join(r[0] for r in seen))
    return "\n".join(out)


def build_router(st) -> Router:
    r = Router()

    def allowed(uid: int) -> bool:
        return st.is_allowed(uid)

    async def show(target: Message, text: str, markup) -> None:
        await target.answer(text, reply_markup=markup)

    @r.message(Command("sovet"))
    async def sovet(msg: Message):
        if not allowed(msg.from_user.id):
            return
        text, markup = menu(st, msg.from_user.id)
        await msg.answer(text, reply_markup=markup)

    @r.callback_query(F.data == "Rm")
    async def menu_cb(cb: CallbackQuery):
        if not allowed(cb.from_user.id):
            return await cb.answer()
        await cb.answer()
        text, markup = menu(st, cb.from_user.id)
        await cb.message.answer(text, reply_markup=markup)

    @r.callback_query(F.data == "Rs")
    async def similar_cb(cb: CallbackQuery):
        if not allowed(cb.from_user.id):
            return await cb.answer()
        await cb.answer("Подбираю…")
        wait = await cb.message.answer("🎯 Подбираю по твоим оценкам…")
        text, markup = await similar(st, cb.from_user.id)
        await wait.edit_text(text, reply_markup=markup)

    @r.callback_query(F.data.regexp(r"^Rg:\d+$"))
    async def together_cb(cb: CallbackQuery):
        if not allowed(cb.from_user.id):
            return await cb.answer()
        await cb.answer("Подбираю…")
        wait = await cb.message.answer("👥 Подбираю для группы…")
        text, markup = await together(st, int(cb.data[3:]), cb.from_user.id)
        await wait.edit_text(text, reply_markup=markup)

    @r.callback_query(F.data == "Ra")
    async def ai_open(cb: CallbackQuery):
        uid = cb.from_user.id
        if not allowed(uid):
            return await cb.answer()
        why = aictl.check(st, uid, "rec")
        if why:
            await cb.answer(f"🤖 {why[0].upper()}{why[1:]}.", show_alert=True)
            return
        st.hooks["ask_input"](uid, ("ai_rec",))
        await cb.answer()
        await cb.message.answer("🤖 Опиши, что хочется посмотреть. Например: <i>что-то лёгкое на вечер, "
                                "как «Однажды в Голливуде», но покороче</i>",
                                reply_markup=kb([[B(text="✖ Отмена", callback_data="Lk")]]))

    async def ai_rec(msg: Message, query: str) -> None:
        uid = msg.from_user.id
        fallback = kb([[B(text="🎯 Похожее на мои любимые", callback_data="Rs")]])
        if len(query) < 3:
            await msg.answer("Опиши подробнее, что хочется 🙂", reply_markup=fallback)
            return
        wait = await msg.answer("🤖 Думаю…")
        text = f"Запрос зрителя: {query}\n{taste(st, uid)}"
        try:
            guesses, who = await aictl.guess(st, msg.bot, uid, "rec", text, prompt=ai.REC_PROMPT,
                                             limit=2500, answers=8)
        except aictl.AiLimit as e:
            await wait.edit_text(f"🤖 {esc(e.reason[0].upper() + e.reason[1:])}. Можно подобрать без ИИ:",
                                 reply_markup=fallback)
            return
        except ai.AiUnavailable as e:
            await wait.edit_text(f"🤖 ИИ сейчас недоступен ({esc(e.reason)}). Можно подобрать без ИИ:",
                                 reply_markup=fallback)
            return
        except Exception as e:
            log.warning("ИИ-подбор: %r", e)
            await wait.edit_text("🤖 ИИ сейчас недоступен. Можно подобрать без ИИ:", reply_markup=fallback)
            return
        infos = await ai.resolve(st.tmdb_http, st.cfg.tmdb_key, guesses, st.cfg.tmdb_lang) if guesses else []
        answered = set(st.db.scores_by_tmdb(uid))
        infos = [i for i in infos if key(i) not in answered]
        if is_kid(st, uid):
            infos = [i for i in infos if tmdb.kid_ok(i)]
        if not infos:
            await wait.edit_text("🤖 ИИ не предложил ничего нового. Попробуй сформулировать иначе или без ИИ:",
                                 reply_markup=fallback)
            return
        t, markup = render(st, f"🤖 <b>По запросу «{esc(query[:80])}»</b> (ИИ, {esc(who)}):",
                           [(i, "") for i in infos[:8]], [[B(text="💡 Другие варианты подбора", callback_data="Rm")]])
        await wait.edit_text(t, reply_markup=markup)

    st.hooks["ai_rec"] = ai_rec
    return r
