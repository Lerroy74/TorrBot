"""v8.2: голосовые сообщения → текст (Yandex SpeechKit) → обычный поиск.

Ключ тот же, что у Алисы (YANDEX_API_KEY + YANDEX_FOLDER_ID); сервисному аккаунту ключа нужна
роль ai.speechkit-stt.user. Распознавание синхронное (v1 stt:recognize): голосовое Telegram (OGG/Opus)
уходит как есть, до STT_MAX_SEC секунд (у Яндекса предел 30 с и 1 МБ).
Яндекс считает каждые начатые 15 секунд; в /ai — выключатель «🎤 Голосовые» и расход
(провайдер «stt»: tin — сколько 15-секундных отрезков, tout — секунд), цена отрезка — AI_PRICE_STT.
Дневные лимиты ИИ-запросов голосовые не тратят, а месячный потолок в ₽ — учитывает.
"""
from __future__ import annotations

import io
import logging
import math

import aiohttp
from aiogram import F, Router
from aiogram.types import Message

from . import aictl

log = logging.getLogger("torrbot.voice")
STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


class SttError(Exception):
    pass


def configured(cfg) -> bool:
    return bool(cfg.yandex_key and cfg.yandex_folder)


async def recognize(http: aiohttp.ClientSession, cfg, audio: bytes) -> str:
    params = {"lang": "ru-RU", "folderId": cfg.yandex_folder, "format": "oggopus"}
    async with http.post(STT_URL, params=params, data=audio,
                         headers={"Authorization": f"Api-Key {cfg.yandex_key}"},
                         timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            body = (await resp.text())[:300]
            log.warning("speechkit %s: %s", resp.status, body)
            if resp.status in (401, 403):
                raise SttError("ключ Яндекса не подходит для распознавания речи (нужна роль ai.speechkit-stt.user)")
            if resp.status == 429:
                raise SttError("Яндекс просит подождать — попробуй через минуту")
            raise SttError("Яндекс не смог распознать голос")
        data = await resp.json()
    return (data.get("result") or "").strip()


def check(st, uid: int) -> str:
    """'' — можно; иначе почему нельзя."""
    if not configured(st.cfg):
        return "голосовые не настроены (нужен ключ Яндекса)"
    if not aictl.feature_on(st, "voice"):
        return "голосовые выключены администратором"
    if not aictl.user_allowed(st, uid):
        return "голосовые доступны не всем — попроси администратора"
    if aictl.capped(st):
        return "голосовые выключены до конца месяца: достигнут потолок расходов на ИИ"
    return ""


def build_router(st) -> Router:
    r = Router(name="voice")

    @r.message(F.voice)
    async def voice(msg: Message):
        uid = msg.from_user.id
        if not st.is_allowed(uid):
            return
        why = check(st, uid)
        if why:
            await msg.answer(f"🎤 Не могу послушать: {why}. Напиши текстом.")
            return
        dur = msg.voice.duration or 0
        if dur > st.cfg.stt_max_sec:
            await msg.answer(f"🎤 Слишком длинно ({dur} с) — скажи короче, до {st.cfg.stt_max_sec} секунд.")
            return
        buf = io.BytesIO()
        try:
            await msg.bot.download(msg.voice, destination=buf)
            http = st.stt_http or st.http
            text = await recognize(http, st.cfg, buf.getvalue())
        except SttError as e:
            await msg.answer(f"🎤 {e}.")
            return
        except Exception as e:
            log.warning("голос: %r", e)
            await msg.answer("🎤 Не получилось распознать — попробуй ещё раз или напиши текстом.")
            return
        st.db.ai_note(aictl.today(), uid, "voice", "stt", max(1, math.ceil(dur / 15)), dur)
        await aictl.after_note(msg.bot, st)
        if not text:
            await msg.answer("🎤 Ничего не разобрал — скажи ещё раз почётче или напиши текстом.")
            return
        await msg.answer(f"🎤 «{text[:200]}»")
        await st.hooks["text_query"](msg, text[:300], uid)

    return r
