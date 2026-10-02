"""v8: контроль ИИ — /ai (только админ).

* функции по отдельности: поиск по описанию (/plot) и подбор по запросу (/sovet);
* сервисы цепочки по отдельности (Алиса, Groq, Gemini) — выключенный пропускается;
* кому доступно: всем или только отмеченным («🤖 ИИ» в карточке человека в /users), админам — всегда;
* лимиты запросов в день: на человека и на всех (0 — без лимита);
* расход: запросы и токены за сегодня и за месяц, примерная сумма в ₽ (если в .env заданы цены);
* месячный потолок в ₽: достигнут — ИИ выключается до следующего месяца, админу сообщение.

Настройки лежат в prefs(user_id=0, key='ai_…'); стартовые значения — из .env (AI_USER_DAY и т.д.).
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

from . import ai
from .access import AI as AI_FLAG

log = logging.getLogger("torrbot")
esc = html.escape

FEATURES = {"plot": "🔎 Поиск по описанию (/plot)", "rec": "🤖 Подбор по запросу (/sovet)",
            "voice": "🎤 Голосовые (SpeechKit)"}
USER_STEPS = [0, 3, 5, 10, 20, 50]
TOTAL_STEPS = [0, 20, 50, 100, 200, 500]
CAP_STEPS = [0, 100, 300, 500, 1000, 2000]


class AiLimit(Exception):
    """ИИ нельзя: выключен, не положено этому человеку или кончился лимит. reason — для пользователя."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now():
    from .extras import now_local
    return now_local()


def today() -> str:
    return _now().strftime("%Y-%m-%d")


def month() -> str:
    return _now().strftime("%Y-%m")


def setting(st, key: str, default: str = "") -> str:
    return st.db.pref(0, f"ai_{key}", default)


def set_setting(st, key: str, value: str) -> None:
    st.db.set_pref(0, f"ai_{key}", value)


def user_day(st) -> int:
    return int(setting(st, "user_day", str(st.cfg.ai_user_day)) or 0)


def total_day(st) -> int:
    return int(setting(st, "total_day", str(st.cfg.ai_total_day)) or 0)


def month_cap(st) -> float:
    return float(setting(st, "month_rub", str(st.cfg.ai_month_rub)) or 0)


def feature_on(st, feature: str) -> bool:
    return setting(st, f"f_{feature}", "1") == "1"


def apply(st) -> None:
    """Выключенные сервисы — в цепочку (при запуске и после переключения)."""
    if st.ai is not None:
        st.ai.disabled = {p.name for p in st.ai.providers if setting(st, f"off_{p.name}") == "1"}


def prices(st) -> dict[str, float]:
    return dict(st.cfg.ai_prices)


def row_cost(st, r) -> float:
    """₽ за строку расхода. v8.2: у распознавания речи (stt) tin — число 15-секундных отрезков."""
    if r["provider"] == "stt":
        return r["tin"] * st.cfg.stt_price
    return (r["tin"] + r["tout"]) / 1000 * prices(st).get(r["provider"], 0)


def cost(st, prefix: str) -> float | None:
    """Примерная сумма в ₽ за день/месяц. None — ни у одного сервиса не задана цена."""
    if not prices(st) and not st.cfg.stt_price:
        return None
    return sum(row_cost(st, r) for r in st.db.ai_usage(prefix))


def capped(st) -> bool:
    cap = month_cap(st)
    if cap <= 0 or setting(st, "override") == month():
        return False
    spent = cost(st, month())
    return spent is not None and spent >= cap


def user_allowed(st, uid: int) -> bool:
    return uid in st.cfg.admin_ids or setting(st, "who", "all") == "all" or st.db.flag(uid, AI_FLAG)


def available(st, uid: int, feature: str) -> bool:
    """Показывать ли человеку кнопку/функцию (без учёта дневных лимитов)."""
    return bool(st.ai) and feature_on(st, feature) and bool(st.ai.active()) and user_allowed(st, uid) \
        and not capped(st)


def check(st, uid: int, feature: str) -> str:
    """'' — можно; иначе — почему нельзя (для пользователя)."""
    if not st.ai:
        return "ИИ не настроен"
    if not feature_on(st, feature):
        return "эта функция ИИ выключена администратором"
    if not st.ai.active():
        return "все сервисы ИИ выключены администратором"
    if not user_allowed(st, uid):
        return "ИИ доступен не всем — попроси администратора"
    if capped(st):
        return "ИИ выключен до конца месяца: достигнут потолок расходов"
    total = total_day(st)
    if total and st.db.ai_requests(today()) >= total:
        return "на сегодня ИИ-запросы закончились"
    per = user_day(st)
    if per and uid not in st.cfg.admin_ids and st.db.ai_requests(today(), uid) >= per:
        return f"на сегодня твои ИИ-запросы закончились ({per} в день)"
    return ""


async def guess(st, bot: Bot | None, uid: int, feature: str, text: str, prompt: str = ai.PROMPT,
                limit: int = 500, answers: int = 6) -> tuple[list[dict], str]:
    """Спросить ИИ с учётом настроек и лимитов; записать расход. AiLimit / ai.AiUnavailable — нельзя/не ответил."""
    why = check(st, uid, feature)
    if why:
        raise AiLimit(why)
    meter: dict = {}
    got, who = await st.ai.guess(text, prompt=prompt, meter=meter, limit=limit, answers=answers)
    st.db.ai_note(today(), uid, feature, meter.get("provider") or "?", meter.get("tin", 0), meter.get("tout", 0))
    await after_note(bot, st)
    return got, who


async def plot(st, bot: Bot | None, uid: int, text: str):
    """Поиск по описанию через ИИ с учётом настроек/лимитов: (карточки TMDB, кто ответил)."""
    why = check(st, uid, "plot")
    if why:
        raise AiLimit(why)
    meter: dict = {}
    infos, who = await ai.search_by_plot(st.ai, st.tmdb_http, st.cfg, text, meter=meter)
    st.db.ai_note(today(), uid, "plot", meter.get("provider") or "?", meter.get("tin", 0), meter.get("tout", 0))
    await after_note(bot, st)
    return infos, who


async def after_note(bot: Bot | None, st) -> None:
    """Потолок достигнут — один раз в месяц сообщить админам."""
    if bot is None or not capped(st) or setting(st, "cap_note") == month():
        return
    set_setting(st, "cap_note", month())
    from .extras import notify_admins
    await notify_admins(bot, st, f"🤖 Расход ИИ за месяц достиг потолка {month_cap(st):g} ₽ — ИИ выключен до "
                                 f"начала следующего месяца. Включить раньше — /ai.")


# ---------- экран /ai ----------
def _fmt_tokens(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def usage_lines(st, prefix: str) -> list[str]:
    rows = st.db.ai_usage(prefix)
    if not rows:
        return ["   ничего"]
    pr = prices(st)
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["provider"], []).append(r)
    out = []
    stt = by.pop("stt", None)
    if stt:
        req = sum(r["req"] for r in stt)
        sec = sum(r["tout"] for r in stt)
        rub = f" ≈ {sum(row_cost(st, r) for r in stt):.2f} ₽" if st.cfg.stt_price else ""
        out.append(f"   🎤 SpeechKit: {req} голос. ({sec} с){rub}")
    for prov, items in by.items():
        req = sum(r["req"] for r in items)
        tok = sum(r["tin"] + r["tout"] for r in items)
        feats = ", ".join(f"{'/plot' if r['feature'] == 'plot' else 'подбор'} {r['req']}" for r in items)
        rub = f" ≈ {tok / 1000 * pr[prov]:.2f} ₽" if prov in pr else ""
        out.append(f"   {ai.TITLES.get(prov, prov)}: {req} запр. ({feats}), {_fmt_tokens(tok)} ток.{rub}")
    return out


def panel(st) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🤖 <b>ИИ — настройки и расход</b>"]
    if not st.ai:
        lines.append("Ключей ИИ нет (YANDEX_API_KEY, GROQ_API_KEY, GEMINI_API_KEY в .env) — работают только "
                     "Википедия и подбор без ИИ.")
    else:
        lines.append("Сервисы: " + esc(st.ai.status()))
    who_all = setting(st, "who", "all") == "all"
    marked = sum(1 for u in st.db.users() if st.db.flag(u["id"], AI_FLAG))
    lines.append("Кому: " + ("всем" if who_all else f"только отмеченным в /users ({marked} чел.) и админам"))
    used = st.db.ai_requests(today())
    per, total = user_day(st), total_day(st)
    lines.append(f"Лимиты в день: на человека {per or 'без лимита'}, на всех {total or 'без лимита'} "
                 f"(сегодня использовано {used})")
    cap, spent = month_cap(st), cost(st, month())
    money = (f"≈ {spent:.2f} ₽ за месяц" if spent is not None
             else "цены не заданы (AI_PRICE_YANDEX и др. в .env) — сумму не считаю")
    if spent is not None and not prices(st):
        money += " (только голосовые: цены ИИ — AI_PRICE_YANDEX и др. — не заданы)"
    lines.append(f"Потолок: {f'{cap:g} ₽/мес' if cap else 'выкл'} · {money}")
    if capped(st):
        lines.append("⛔ <b>Потолок достигнут — ИИ выключен до конца месяца.</b>")
    lines.append("\n<b>Сегодня:</b>")
    lines += usage_lines(st, today())
    lines.append("<b>За месяц:</b>")
    lines += usage_lines(st, month())
    rows = [[B(text=f"{'✅' if feature_on(st, f) else '⛔'} {name}", callback_data=f"A:f:{f}")]
            for f, name in FEATURES.items()]
    if st.ai:
        rows.append([B(text=f"{'⛔' if p.name in st.ai.disabled else '✅'} {p.title}", callback_data=f"A:p:{p.name}")
                     for p in st.ai.providers])
    rows.append([B(text="👥 Кому: " + ("всем → только отмеченным" if who_all else "отмеченным → всем"),
                   callback_data="A:w")])
    rows.append([B(text=f"На человека: {per or '∞'} ▸", callback_data="A:u"),
                 B(text=f"На всех: {total or '∞'} ▸", callback_data="A:t")])
    rows.append([B(text=f"Потолок: {f'{cap:g} ₽' if cap else 'выкл'} ▸", callback_data="A:c")])
    if capped(st):
        rows.append([B(text="▶ Включить до конца месяца", callback_data="A:o")])
    rows.append([B(text="🔄 Обновить", callback_data="A:r")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _next(steps: list, cur) -> str:
    later = [s for s in steps if s > cur]
    return str(later[0] if later else steps[0])


def build_router(st) -> Router:
    r = Router()

    @r.message(Command("ai"))
    async def ai_cmd(msg: Message):
        if msg.from_user.id not in st.cfg.admin_ids:
            return
        text, markup = panel(st)
        await msg.answer(text, reply_markup=markup)

    @r.callback_query(F.data.regexp(r"^A:(f:(plot|rec|voice)|p:[a-z]+|[wutcor])$"))
    async def ai_cb(cb: CallbackQuery):
        if cb.from_user.id not in st.cfg.admin_ids:
            return await cb.answer("Только для администратора", show_alert=True)
        parts = cb.data.split(":")
        act = parts[1]
        note = ""
        if act == "f":
            on = not feature_on(st, parts[2])
            set_setting(st, f"f_{parts[2]}", "1" if on else "0")
            note = f"{FEATURES[parts[2]]}: {'включено' if on else 'выключено'}"
        elif act == "p":
            off = setting(st, f"off_{parts[2]}") != "1"
            set_setting(st, f"off_{parts[2]}", "1" if off else "0")
            apply(st)
            note = f"{ai.TITLES.get(parts[2], parts[2])}: {'выключен' if off else 'включён'}"
        elif act == "w":
            set_setting(st, "who", "marked" if setting(st, "who", "all") == "all" else "all")
        elif act == "u":
            set_setting(st, "user_day", _next(USER_STEPS, user_day(st)))
        elif act == "t":
            set_setting(st, "total_day", _next(TOTAL_STEPS, total_day(st)))
        elif act == "c":
            set_setting(st, "month_rub", _next(CAP_STEPS, month_cap(st)))
        elif act == "o":
            set_setting(st, "override", month())
            note = "ИИ снова включён до конца месяца"
        text, markup = panel(st)
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        await cb.answer(note)

    return r
