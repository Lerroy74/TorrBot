"""/ocenki — журнал всего, что скачивали, и оценки от 1 до 10.

В журнал попадает каждое докачанное название (одно название — одна запись, даже если
качали несколько раз или по сезонам). При удалении через /delete и при автоочистке бот
просит оценить фильм; оценку можно поставить или поменять потом в /ocenki.
v6.5: у каждого своя оценка (таблица ratings), в списке — средняя и число оценок.
Кого и когда спрашивать — ask(): каждого про каждое название автоматически спрашиваем
один раз (rating_asks); тот, кто сам нажал «удалить»/«посмотрели», получает вопрос всегда,
если ещё не ответил. Сбросить можно только свою оценку.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

from . import cleanup

log = logging.getLogger("torrbot")
esc = html.escape

PAGE = 10
MODES = {"d": "📅 По дате", "r": "⭐ По средней", "u": "✍ Мне оценить"}
NOT_SEEN = "Не смотрел(а)"


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def who(st, uid: int | None) -> str:
    if not uid:
        return ""
    u = st.db.user(uid)
    name = (u["name"] if u is not None and u["name"] else "") or ("админ" if uid in st.cfg.admin_ids else str(uid))
    return name.split(" (")[0][:20]


def icon(row) -> str:
    return "📺" if row["kind"] == "series" else "🎬"


def day(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%y") if ts else "—"


def score_kb(jid: int, tail: str = "", current: int | None = None) -> list[list[B]]:
    """Две строки кнопок 1–5 и 6–10; текущая оценка отмечена."""
    def b(n: int) -> B:
        return B(text=f"·{n}·" if n == current else str(n), callback_data=f"rt:{jid}:{n}{tail}")
    return [[b(n) for n in range(1, 6)], [b(n) for n in range(6, 11)]]


def ask_markup(jid: int, current: int | None = None) -> InlineKeyboardMarkup:
    return kb(score_kb(jid, current=current) + [[B(text=NOT_SEEN, callback_data=f"rt:{jid}:0")]])


def avg_text(avg: float | None, cnt: int) -> str:
    return f"⭐ <b>{avg:.1f}</b> ({cnt})" if cnt else "без оценок"


def others_text(st, jid: int, skip: int | None = None) -> str:
    """«Алиса 8 · Боб 7 · Артём — не смотрел(а)»."""
    parts = []
    for r in st.db.ratings(jid):
        if r["user_id"] == skip:
            continue
        name = esc(who(st, r["user_id"]) or "?")
        parts.append(f"{name} {r['score']}" if r["score"] else f"{name} — не смотрел(а)")
    return " · ".join(parts)


def ask_text(st, row) -> str:
    others = others_text(st, row["id"])
    return (f"⭐ Как вам {icon(row)} <b>{esc(row['label'][:150])}</b>?\n"
            + (f"Уже оценили: {others}\n" if others else "")
            + "Оцени от 1 до 10 — запишу в /ocenki.")


async def ask(bot, st, jid: int | None, uid: int | None, chat_id: int | None = None,
              reason: str = "delete", force: bool = False) -> bool:
    """Попросить оценку у одного человека. Не спрашиваем, если он уже ответил (оценка или
    «не смотрел(а)»); если уже спрашивали — только когда force (он сам сейчас нажал кнопку).
    Ошибки не мешают остальному. True — вопрос отправлен."""
    if not jid or not uid or not st.is_allowed(uid):
        return False
    row = st.db.journal_get(jid)
    if row is None or st.db.rating_of(jid, uid) is not None:
        return False
    if not force and st.db.was_asked(jid, uid):
        return False
    st.db.note_ask(jid, uid, reason)
    try:
        await bot.send_message(chat_id or uid, ask_text(st, row), reply_markup=ask_markup(jid))
        return True
    except Exception as e:
        log.warning("не смог спросить оценку: %r", e)
        return False


async def ask_many(bot, st, jid: int | None, people, reason: str) -> None:
    """people — [(uid, chat_id)]; каждого не больше одного раза."""
    seen = set()
    for uid, chat in people:
        if uid and uid not in seen:
            seen.add(uid)
            await ask(bot, st, jid, uid, chat, reason)


PC_BUTTON = "✅ Посмотрели на ПК"


async def mark_on_pc(st, paths: list[str], auto: bool = True) -> tuple[int, str]:
    """Смотрели не на ТВ (на ПК, в VLC и т.п.) — отметить в Kodi на малинке как просмотренное,
    чтобы сработала автоочистка. (сколько отмечено, текст для пользователя)"""
    cfg = st.cfg
    if not st.kodi:
        return 0, "Kodi не настроен — отметить негде."
    roots = [k for k in (cleanup.to_kodi_path(p, cfg.media_root, cfg.kodi_media_url) for p in paths) if k]
    if not roots:
        return 0, "Это лежит не в медиатеке — отметить не могу."
    try:
        items = await asyncio.wait_for(st.kodi.videos(), 15)
        mine, seen = [], set()
        for root in roots:
            for it in cleanup.items_under(root, items):
                key = (it.get("movieid"), it.get("episodeid"))
                if key not in seen:
                    seen.add(key)
                    mine.append(it)
        if not mine:
            return 0, "В медиатеке Kodi этого нет (ещё не появилось?) — отметить не могу."
        n = await st.kodi.mark_watched(mine, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        log.info("отметка «посмотрели на ПК»: %r", e)
        return 0, "Kodi не ответил (малинка выключена?) — попробуй позже."
    files = f"{n} серий" if n > 1 else "фильм"
    if auto and cfg.cleanup_days:
        tail = f"Через {cfg.cleanup_days} дн. удалю сам (админу сначала придёт предупреждение)."
    elif auto:
        tail = "Автоочистка выключена — само не удалится."
    else:
        tail = "Положено не через бота — само не удалится, только через /delete."
    log.info("отмечено просмотренным в Kodi: %s (%d)", roots, n)
    return n, f"✅ Отметил в Kodi как просмотренное ({files}). {tail}"


def list_view(st, mode: str, page: int, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    mode = mode if mode in MODES else "d"
    rows = st.db.journal(mode, uid)
    everything = st.db.journal("d", uid) if mode != "d" else rows
    rated = sum(1 for r in everything if r["cnt"])
    todo = sum(1 for r in everything if not r["answered"])
    head = f"📒 <b>Что смотрели</b> · всего {len(everything)}"
    if rated:
        head += f" · с оценками {rated}"
    tabs = [B(text=("• " if m == mode else "") + (f"{t} ({todo})" if m == "u" else t),
              callback_data=f"jr:{m}:0") for m, t in MODES.items()]
    if not everything:
        return head + "\n\nПока пусто: сюда попадает всё, что докачалось через бота.", kb([])
    if not rows:
        empty = "Ты оценил(а) всё 👍" if mode == "u" else "Оценок пока нет."
        return f"{head}\n\n{empty}", kb([tabs])
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    lines, btns = [], []
    for i in range(page * PAGE, min(len(rows), (page + 1) * PAGE)):
        r = rows[i]
        mine = ""
        if r["answered"]:
            mine = f" · ты: {r['my']}" if r["my"] else " · ты: не смотрел(а)"
        disk = " · 💾 на диске" if not r["deleted_at"] else ""
        lines.append(f"<b>{i + 1}.</b> {icon(r)} {esc(r['label'][:70])} — {avg_text(r['avg'], r['cnt'])}{mine} · "
                     f"{day(r['deleted_at'] or r['added_at'])}{disk}")
        btns.append(B(text=str(i + 1), callback_data=f"jq:{r['id']}:{mode}:{page}"))
    nav = []
    if page > 0:
        nav.append(B(text="◀", callback_data=f"jr:{mode}:{page - 1}"))
    if page < pages - 1:
        nav.append(B(text="▶", callback_data=f"jr:{mode}:{page + 1}"))
    title = {"d": "новые сверху", "r": "лучшие сверху", "u": "ты ещё не оценил(а)"}[mode]
    text = (f"{head}\n{MODES[mode]} — {title}" + (f", стр. {page + 1}/{pages}" if pages > 1 else "") +
            "\n\n" + "\n".join(lines) + "\n\nНажми номер — оценки всех и твоя оценка.")
    return text, kb([btns[j:j + 5] for j in range(0, len(btns), 5)] + [nav, tabs])


def card_view(st, row, mode: str, page: int, uid: int, admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    jid = row["id"]
    lines = [f"{icon(row)} <b>{esc(row['label'][:150])}</b>"]
    added = who(st, row["added_by"])
    lines.append(f"⬇ Скачано {day(row['added_at'])}" + (f" · 👤 {esc(added)}" if added else ""))
    lines.append(f"🗑 Удалено {day(row['deleted_at'])}" if row["deleted_at"] else "💾 Ещё на диске")
    avg, cnt = st.db.rating_summary(jid)
    others = others_text(st, jid)
    lines.append(f"\n{avg_text(avg, cnt)}" + (f" — {others}" if others else ""))
    mine = st.db.rating_of(jid, uid)
    if mine is None:
        lines.append("\nТы ещё не оценил(а). Оцени от 1 до 10:")
    elif mine["score"]:
        lines.append(f"\nТвоя оценка: <b>{mine['score']}</b>/10. Поменять:")
    else:
        lines.append("\nТы отметил(а): не смотрел(а). Посмотрел(а) — оцени:")
    tail = f":{mode}:{page}"
    rows = score_kb(jid, tail, mine["score"] if mine is not None else None)
    extra = []
    if mine is None or mine["score"]:
        extra.append(B(text=NOT_SEEN, callback_data=f"rn:{jid}{tail}"))
    if mine is not None:
        extra.append(B(text="Сбросить мою", callback_data=f"rt:{jid}:0{tail}"))
    rows.append(extra)
    rows.append([B(text=PC_BUTTON, callback_data=f"jw:{jid}{tail}")]
                if st.kodi and not row["deleted_at"] and st.db.journal_downloads(jid) else [])
    if admin:
        rows.append([B(text="✖ Убрать из списка", callback_data=f"jx:{jid}{tail}")])
    rows.append([B(text="◀ К списку", callback_data=f"jr:{mode}:{page}")])
    return "\n".join(lines), kb(rows)


def build_router(st) -> Router:
    r = Router()

    def allowed(uid: int) -> bool:
        return st.is_allowed(uid)

    async def edit(cb: CallbackQuery, text: str, markup) -> None:
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:
            await cb.message.answer(text, reply_markup=markup)

    @r.message(Command("ocenki"))
    async def ocenki(msg: Message):
        if not allowed(msg.from_user.id):
            return
        st.db.touch(msg.from_user.id)
        text, markup = list_view(st, "d", 0, msg.from_user.id)
        await msg.answer(text, reply_markup=markup)

    @r.callback_query(F.data.regexp(r"^jr:[dru]:\d+$"))
    async def page(cb: CallbackQuery):
        if not allowed(cb.from_user.id):
            return await cb.answer()
        _, mode, p = cb.data.split(":")
        await cb.answer()
        await edit(cb, *list_view(st, mode, int(p), cb.from_user.id))

    @r.callback_query(F.data.regexp(r"^jq:\d+:[dru]:\d+$"))
    async def card(cb: CallbackQuery):
        if not allowed(cb.from_user.id):
            return await cb.answer()
        _, jid, mode, p = cb.data.split(":")
        row = st.db.journal_get(int(jid))
        if row is None:
            await cb.answer("Этой записи уже нет")
            return await edit(cb, *list_view(st, mode, int(p), cb.from_user.id))
        await cb.answer()
        await edit(cb, *card_for(st, row, mode, int(p), cb.from_user.id))

    def card_for(st_, row, mode, p, uid):
        return card_view(st_, row, mode, p, uid, uid in st.cfg.admin_ids)

    async def answered(cb: CallbackQuery, row, n: int | None, tail: list[str]) -> None:
        """После ответа: в /ocenki — обновить карточку, в вопросе — заменить его итогом."""
        uid = cb.from_user.id
        if tail:
            return await edit(cb, *card_for(st, row, tail[0], int(tail[1]), uid))
        avg, cnt = st.db.rating_summary(row["id"])
        name = f"{icon(row)} <b>{esc(row['label'][:150])}</b>"
        if n is None:
            text = f"{name} — ок, отметил: не смотрел(а). Если посмотришь — оценить можно в /ocenki."
        else:
            text = f"⭐ {name} — <b>{n}</b>/10. Записал."
        if cnt:
            text += f"\nСредняя сейчас {avg_text(avg, cnt)} — все оценки в /ocenki"
        await edit(cb, text, None)

    @r.callback_query(F.data.regexp(r"^rt:\d+:\d+(:[dru]:\d+)?$"))
    async def rate(cb: CallbackQuery):
        """rt:jid:1..10 — оценка; rt:jid:0 из вопроса — «не смотрел(а)»; rt:jid:0:… в /ocenki — сбросить свою."""
        uid = cb.from_user.id
        if not allowed(uid):
            return await cb.answer()
        parts = cb.data.split(":")
        jid, n, tail = int(parts[1]), int(parts[2]), parts[3:]
        row = st.db.journal_get(jid)
        if row is None or not 0 <= n <= 10:
            await cb.answer("Этой записи уже нет", show_alert=True)
            return
        st.db.touch(uid)
        if n == 0 and tail:
            st.db.unrate(jid, uid)
            log.info("оценка %s: %s → сброс", uid, row["label"])
            await cb.answer("Твоя оценка сброшена")
            return await answered(cb, row, None, tail)
        st.db.rate(jid, uid, n or None)
        log.info("оценка %s: %s → %s", uid, row["label"], n or "не смотрел")
        await cb.answer(f"⭐ {n}/10" if n else "Ок, не смотрел(а)")
        await answered(cb, row, n or None, tail)

    @r.callback_query(F.data.regexp(r"^rn:\d+:[dru]:\d+$"))
    async def not_seen(cb: CallbackQuery):
        uid = cb.from_user.id
        if not allowed(uid):
            return await cb.answer()
        _, jid, mode, p = cb.data.split(":")
        row = st.db.journal_get(int(jid))
        if row is None:
            return await cb.answer("Этой записи уже нет", show_alert=True)
        st.db.touch(uid)
        st.db.rate(row["id"], uid, None)
        await cb.answer("Ок, не смотрел(а)")
        await answered(cb, row, None, [mode, p])

    @r.callback_query(F.data.regexp(r"^jw:\d+:[dru]:\d+$"))
    async def on_pc(cb: CallbackQuery):
        if not allowed(cb.from_user.id):
            return await cb.answer()
        _, jid, mode, p = cb.data.split(":")
        row = st.db.journal_get(int(jid))
        rows = st.db.journal_downloads(int(jid)) if row is not None else []
        if not rows:
            return await cb.answer("Этого уже нет на диске", show_alert=True)
        await cb.answer("Отмечаю…")
        try:
            torrents = await st.tr.get([r["hash"] for r in rows])
        except Exception as e:
            return await cb.message.answer(f"Transmission не ответил: {esc(str(e))}")
        n, note = await mark_on_pc(st, [f"{(t.get('downloadDir') or '').rstrip('/')}/{t['name']}" for t in torrents])
        text, markup = card_for(st, row, mode, int(p), cb.from_user.id)
        await edit(cb, f"{note}\n\n{text}", markup)

    @r.callback_query(F.data.regexp(r"^jx:\d+:[dru]:\d+$"))
    async def remove_ask(cb: CallbackQuery):
        if cb.from_user.id not in st.cfg.admin_ids:
            return await cb.answer("Только для администратора", show_alert=True)
        _, jid, mode, p = cb.data.split(":")
        row = st.db.journal_get(int(jid))
        if row is None:
            return await cb.answer("Этой записи уже нет")
        await cb.answer()
        await edit(cb, f"Убрать <b>{esc(row['label'][:150])}</b> из списка «Что смотрели»?\n"
                       f"Удалятся и все оценки к нему. Файлы это не трогает.",
                   kb([[B(text="✖ Да, убрать", callback_data=f"jy:{jid}:{mode}:{p}"),
                        B(text="Нет", callback_data=f"jq:{jid}:{mode}:{p}")]]))

    @r.callback_query(F.data.regexp(r"^jy:\d+:[dru]:\d+$"))
    async def remove_yes(cb: CallbackQuery):
        if cb.from_user.id not in st.cfg.admin_ids:
            return await cb.answer("Только для администратора", show_alert=True)
        _, jid, mode, p = cb.data.split(":")
        st.db.journal_remove(int(jid))
        await cb.answer("Убрано")
        await edit(cb, *list_view(st, mode, int(p), cb.from_user.id))

    return r
