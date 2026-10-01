"""/ocenki — журнал всего, что скачивали, и оценки от 1 до 10.

В журнал попадает каждое докачанное название (одно название — одна запись, даже если
качали несколько раз или по сезонам). При удалении через /delete и при автоочистке бот
просит оценить фильм; оценку можно поставить или поменять потом в /ocenki.
Оценка одна на семью — ставит любой, у кого есть доступ; видно, кто поставил.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

log = logging.getLogger("torrbot")
esc = html.escape

PAGE = 10
MODES = {"d": "📅 По дате", "r": "⭐ По оценке", "u": "✍ Без оценки"}


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
    return kb(score_kb(jid, current=current) +
              [[B(text="Не смотрели — без оценки", callback_data=f"rt:{jid}:0")]])


def ask_text(row) -> str:
    now = f" (сейчас: {row['rating']}/10)" if row["rating"] else ""
    return (f"⭐ Как вам {icon(row)} <b>{esc(row['label'][:150])}</b>?{now}\n"
            f"Оцени от 1 до 10 — запишу в /ocenki.")


async def ask(bot, st, chat_id: int, jid: int | None) -> None:
    """Попросить оценку (после удаления). Ошибки не мешают удалению."""
    if not jid or not chat_id:
        return
    row = st.db.journal_get(jid)
    if row is None:
        return
    try:
        await bot.send_message(chat_id, ask_text(row), reply_markup=ask_markup(jid, row["rating"]))
    except Exception as e:
        log.warning("не смог спросить оценку: %r", e)


def list_view(st, mode: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    mode = mode if mode in MODES else "d"
    rows = st.db.journal(mode)
    everything = st.db.journal("d") if mode != "d" else rows
    rated = [r["rating"] for r in everything if r["rating"]]
    unrated = len(everything) - len(rated)
    head = f"📒 <b>Что смотрели</b> · всего {len(everything)}"
    if rated:
        head += f" · с оценкой {len(rated)}, средняя {sum(rated) / len(rated):.1f}"
    tabs = [B(text=("• " if m == mode else "") + (f"{t} ({unrated})" if m == "u" else t),
              callback_data=f"jr:{m}:0") for m, t in MODES.items()]
    if not everything:
        return head + "\n\nПока пусто: сюда попадает всё, что докачалось через бота.", kb([])
    if not rows:
        empty = "Все оценены 👍" if mode == "u" else "Оценок пока нет."
        return f"{head}\n\n{empty}", kb([tabs])
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    lines, btns = [], []
    for i in range(page * PAGE, min(len(rows), (page + 1) * PAGE)):
        r = rows[i]
        score = f"⭐ <b>{r['rating']}</b>/10" if r["rating"] else "без оценки"
        disk = " · 💾 на диске" if not r["deleted_at"] else ""
        lines.append(f"<b>{i + 1}.</b> {icon(r)} {esc(r['label'][:70])} — {score} · "
                     f"{day(r['deleted_at'] or r['added_at'])}{disk}")
        btns.append(B(text=str(i + 1), callback_data=f"jq:{r['id']}:{mode}:{page}"))
    nav = []
    if page > 0:
        nav.append(B(text="◀", callback_data=f"jr:{mode}:{page - 1}"))
    if page < pages - 1:
        nav.append(B(text="▶", callback_data=f"jr:{mode}:{page + 1}"))
    title = {"d": "новые сверху", "r": "лучшие сверху", "u": "ещё не оценены"}[mode]
    text = (f"{head}\n{MODES[mode]} — {title}" + (f", стр. {page + 1}/{pages}" if pages > 1 else "") +
            "\n\n" + "\n".join(lines) + "\n\nНажми номер — поставить или изменить оценку.")
    return text, kb([btns[j:j + 5] for j in range(0, len(btns), 5)] + [nav, tabs])


def card_view(st, row, mode: str, page: int, admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"{icon(row)} <b>{esc(row['label'][:150])}</b>"]
    added = who(st, row["added_by"])
    lines.append(f"⬇ Скачано {day(row['added_at'])}" + (f" · 👤 {esc(added)}" if added else ""))
    lines.append(f"🗑 Удалено {day(row['deleted_at'])}" if row["deleted_at"] else "💾 Ещё на диске")
    if row["rating"]:
        by = who(st, row["rated_by"])
        lines.append(f"⭐ Оценка: <b>{row['rating']}</b>/10" + (f" ({esc(by)})" if by else ""))
    lines.append("\nОцени от 1 до 10:")
    tail = f":{mode}:{page}"
    rows = score_kb(row["id"], tail, row["rating"])
    extra = []
    if row["rating"]:
        extra.append(B(text="Сбросить оценку", callback_data=f"rt:{row['id']}:0{tail}"))
    if admin:
        extra.append(B(text="✖ Убрать из списка", callback_data=f"jx:{row['id']}{tail}"))
    rows += [extra, [B(text="◀ К списку", callback_data=f"jr:{mode}:{page}")]]
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
        text, markup = list_view(st, "d", 0)
        await msg.answer(text, reply_markup=markup)

    @r.callback_query(F.data.regexp(r"^jr:[dru]:\d+$"))
    async def page(cb: CallbackQuery):
        if not allowed(cb.from_user.id):
            return await cb.answer()
        _, mode, p = cb.data.split(":")
        await cb.answer()
        await edit(cb, *list_view(st, mode, int(p)))

    @r.callback_query(F.data.regexp(r"^jq:\d+:[dru]:\d+$"))
    async def card(cb: CallbackQuery):
        if not allowed(cb.from_user.id):
            return await cb.answer()
        _, jid, mode, p = cb.data.split(":")
        row = st.db.journal_get(int(jid))
        if row is None:
            await cb.answer("Этой записи уже нет")
            return await edit(cb, *list_view(st, mode, int(p)))
        await cb.answer()
        await edit(cb, *card_view(st, row, mode, int(p), cb.from_user.id in st.cfg.admin_ids))

    @r.callback_query(F.data.regexp(r"^rt:\d+:\d+(:[dru]:\d+)?$"))
    async def rate(cb: CallbackQuery):
        uid = cb.from_user.id
        if not allowed(uid):
            return await cb.answer()
        parts = cb.data.split(":")
        jid, n = int(parts[1]), int(parts[2])
        row = st.db.journal_get(jid)
        if row is None or not 0 <= n <= 10:
            await cb.answer("Этой записи уже нет", show_alert=True)
            return
        st.db.touch(uid)
        from_list = len(parts) == 5
        if n == 0 and not from_list:                # «не смотрели» в вопросе после удаления
            await cb.answer("Ок, без оценки")
            await edit(cb, f"{icon(row)} <b>{esc(row['label'][:150])}</b> — без оценки. "
                           f"Если что, оценить можно в /ocenki.", None)
            return
        st.db.journal_rate(jid, n or None, uid)
        log.info("оценка %s: %s → %s", uid, row["label"], n or "сброс")
        if from_list:
            await cb.answer(f"⭐ {n}/10" if n else "Оценка сброшена")
            await edit(cb, *list_view(st, parts[3], int(parts[4])))
            return
        await cb.answer(f"⭐ {n}/10")
        await edit(cb, f"⭐ {icon(row)} <b>{esc(row['label'][:150])}</b> — <b>{n}</b>/10. "
                       f"Записал, весь список — /ocenki", None)

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
                       f"Файлы это не трогает.",
                   kb([[B(text="✖ Да, убрать", callback_data=f"jy:{jid}:{mode}:{p}"),
                        B(text="Нет", callback_data=f"jq:{jid}:{mode}:{p}")]]))

    @r.callback_query(F.data.regexp(r"^jy:\d+:[dru]:\d+$"))
    async def remove_yes(cb: CallbackQuery):
        if cb.from_user.id not in st.cfg.admin_ids:
            return await cb.answer("Только для администратора", show_alert=True)
        _, jid, mode, p = cb.data.split(":")
        st.db.journal_remove(int(jid))
        await cb.answer("Убрано")
        await edit(cb, *list_view(st, mode, int(p)))

    return r
