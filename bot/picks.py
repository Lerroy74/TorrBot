"""v8.1: выбор номером вместо «портянки» кнопок.

Длинный список (варианты фильмов, раздачи, списки, /ocenki, /delete…) показывается без кнопки на
каждый пункт — внизу подсказка «✍ Пришли номер». Человек пишет «5» — бот делает то же, что сделала
бы кнопка №5: создаёт «нажатие» с её callback_data и отдаёт его обычному обработчику.

Как это связано:
* экран строит текст и вызывает finish(st, text, {номер: callback_data}) — к тексту добавляется
  подсказка, а пара (текст → номера) запоминается «в ожидании»;
* при отправке/правке сообщения с таким текстом (install() ставит обработчик запросов к Telegram)
  номера привязываются к этому сообщению и чату;
* main.search: пришло число из диапазона — dispatch() «нажимает» кнопку (правки идут в то же
  сообщение со списком, всплывающие ответы-предупреждения приходят обычным сообщением).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import (AnswerCallbackQuery, EditMessageCaption, EditMessageText, SendMessage,
                             SendPhoto)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, Message, Update, User

log = logging.getLogger("torrbot")

TTL = 3 * 3600
HINT = "✍ Пришли номер"
FAKE = "num:"                     # id «нажатий», сделанных номером
PENDING: dict[str, dict[int, str]] = {}    # текст списка → номера (ещё не отправлен)
PICKS: dict[int, dict] = {}                 # чат → последний список с номерами


def finish(text: str, mapping: dict[int, str], what: str = "") -> str:
    """Добавить подсказку и запомнить номера (привяжутся к сообщению, когда оно уйдёт в Telegram)."""
    if not mapping:
        return text
    lo, hi = min(mapping), max(mapping)
    text = f"{text}\n\n{HINT} ({lo}–{hi})" if lo != hi else f"{text}\n\n{HINT} ({lo})"
    if what:
        text += f" — {what}"
    PENDING[text] = mapping
    if len(PENDING) > 300:
        for k in list(PENDING)[:100]:
            del PENDING[k]
    return text


def numbered(items: list[tuple[int, str, str]], threshold: int, per_row: int = 1):
    """items — [(номер, надпись кнопки, callback_data)]. Мало — кнопки как раньше; много — без кнопок.
    Вернёт (ряды кнопок, mapping для finish)."""
    if len(items) <= threshold:
        btns = [InlineKeyboardButton(text=t, callback_data=d) for _, t, d in items]
        return [btns[i:i + per_row] for i in range(0, len(btns), per_row)], {}
    return [], {n: d for n, _, d in items}


def bind(chat_id: int, message_id: int, text: str | None, photo: bool) -> None:
    if not text or text not in PENDING:
        return
    PICKS[chat_id] = dict(mid=message_id, text=text, photo=photo, map=PENDING.pop(text), at=time.time())


def lookup(chat_id: int, text: str) -> str | None:
    """callback_data для присланного номера (или None — это не выбор)."""
    t = (text or "").strip().lstrip("№#").strip()
    if not t.isdigit() or len(t) > 3:
        return None
    got = PICKS.get(chat_id)
    if not got or time.time() - got["at"] > TTL:
        return None
    return got["map"].get(int(t))


async def dispatch(msg: Message, data: str, dispatcher) -> None:
    """«Нажать» кнопку с этим callback_data от имени человека — на сообщении со списком."""
    got = PICKS[msg.chat.id]
    bot = msg.bot
    base = dict(message_id=got["mid"], date=datetime.now(), chat=Chat(id=msg.chat.id, type="private"))
    src = Message(**base, caption=got["text"], photo=[]) if got["photo"] else Message(**base, text=got["text"])
    cb = CallbackQuery(id=f"{FAKE}{msg.chat.id}:{msg.message_id}", from_user=msg.from_user or User(
        id=msg.chat.id, is_bot=False, first_name="?"), chat_instance="num", data=data, message=src.as_(bot))
    await dispatcher.feed_update(bot, Update(update_id=0, callback_query=cb))


class Middleware(BaseRequestMiddleware):
    """Привязывает номера к отправленным спискам; ответы на «нажатия номером» не шлёт в Telegram
    (там такого нажатия нет) — предупреждения превращает в обычное сообщение."""

    async def __call__(self, make_request, bot: Bot, method):
        if isinstance(method, AnswerCallbackQuery) and str(method.callback_query_id).startswith(FAKE):
            if method.text and method.show_alert:
                chat = int(str(method.callback_query_id)[len(FAKE):].split(":")[0])
                await bot.send_message(chat, method.text)
            return True
        result = await make_request(bot, method)
        try:
            if isinstance(method, (SendMessage, EditMessageText)):
                mid = getattr(result, "message_id", None) or method.message_id
                if method.chat_id is not None and mid:
                    bind(int(method.chat_id), mid, method.text, False)
            elif isinstance(method, (SendPhoto, EditMessageCaption)):
                mid = getattr(result, "message_id", None) or getattr(method, "message_id", None)
                if method.chat_id is not None and mid:
                    bind(int(method.chat_id), mid, method.caption, True)
        except Exception as e:
            log.info("номера: не привязал список: %r", e)
        return result


def install(bot: Bot) -> None:
    bot.session.middleware(Middleware())
