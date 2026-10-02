"""v8.2: обновление бота из Telegram (только админ).

Админ присылает боту архив torrbot-vX.Y.zip → бот проверяет его и спрашивает «Обновить vA → vB?» →
кладёт архив и заявку в UPDATE_DIR (/data/update = ~/torrbot/data/update на сервере).
Дальше работает программа на сервере вне Docker (host/update-agent.sh, ставится host/install.sh):
deploy.sh → ждёт, что новый бот поднялся (alive.json с новой версией и контейнер не падает) →
если нет — rollback.sh к снимку; если да — git commit + push. Итог пишет админам в Telegram напрямую.

Бот сам в Docker не лезет — у контейнера нет доступа к Docker и к коду на сервере.
"""
from __future__ import annotations

import html
import io
import json
import logging
import os
import re
import time
import zipfile

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

from . import __version__

log = logging.getLogger("torrbot.update")
esc = html.escape
MAX_ZIP = 20 * 1024 * 1024          # больше Telegram боту не отдаст
RE_VER = re.compile(r"^\d+(\.\d+){0,3}$")


class BadZip(Exception):
    pass


def inspect(data: bytes) -> str:
    """Проверить архив torrbot и вернуть его версию; что не так — BadZip с объяснением."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise BadZip("это не zip-архив")
    names = z.namelist()
    if not names:
        raise BadZip("архив пустой")
    for n in names:
        if not n.startswith("torrbot/") or ".." in n.split("/") or n.startswith("/") or "\\" in n:
            raise BadZip(f"в архиве лишнее: {n[:60]} (всё должно лежать в папке torrbot/)")
    for need in ("torrbot/bot/main.py", "torrbot/VERSION", "torrbot/deploy.sh", "torrbot/docker-compose.yml"):
        if need not in names:
            raise BadZip(f"в архиве нет {need}")
    if any(n in names for n in ("torrbot/.env", "torrbot/pi/secrets.env")):
        raise BadZip("в архиве лежат пароли (.env / secrets.env) — такой ставить не буду")
    ver = z.read("torrbot/VERSION").decode("utf-8", "replace").strip()
    if not RE_VER.match(ver):
        raise BadZip(f"странная версия в VERSION: {ver[:20]!r}")
    return ver


def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def paths(st) -> dict[str, str]:
    d = st.cfg.update_dir
    return {k: os.path.join(d, f) for k, f in (("zip", "torrbot-update.zip"), ("req", "request.json"),
                                               ("busy", "processing.json"), ("result", "result.json"),
                                               ("alive", "alive.json"), ("agent", "agent.json"))}


def _write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_alive(st) -> None:
    """При запуске: «я поднялся, версия такая» — по этому программа на сервере понимает, что обновление удалось."""
    try:
        os.makedirs(st.cfg.update_dir, exist_ok=True)
        _write(paths(st)["alive"], json.dumps({"version": __version__, "at": int(time.time())}).encode())
    except Exception as e:
        log.info("alive.json: %r", e)


async def alive_loop(bot, st) -> None:
    """Пишем alive.json, когда бот правда работает: дошли до запуска и Telegram отвечает."""
    import asyncio
    for _ in range(60):
        try:
            await bot.get_me()
            write_alive(st)
            return
        except Exception as e:
            log.info("alive: Telegram пока не отвечает: %r", e)
            await asyncio.sleep(5)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def status_text(st) -> str:
    p = paths(st)
    agent = _read_json(p["agent"])
    lines = [f"🔄 <b>Обновление бота</b>\nСейчас: v{__version__}"]
    if agent:
        lines.append(f"Программа обновления на сервере: установлена ({esc(str(agent.get('installed', '')))})")
    else:
        lines.append("⚠ Программа обновления на сервере не установлена. Один раз на сервере:\n"
                     "<code>sudo sh ~/torrbot/host/install.sh</code>")
    if os.path.exists(p["busy"]) or os.path.exists(p["req"]):
        lines.append("⏳ Сейчас идёт обновление.")
    res = _read_json(p["result"])
    if res:
        when = time.strftime("%d.%m %H:%M", time.localtime(res.get("at", 0)))
        lines.append(f"Последнее: {when} — {esc(str(res.get('text', '')))[:300]}")
    lines.append("\nЧтобы обновить — пришли сюда архив <code>torrbot-vX.Y.zip</code>.")
    return "\n".join(lines)


def build_router(st) -> Router:
    r = Router(name="updater")
    pending: dict[int, tuple[float, bytes, str]] = {}

    def admin(uid: int) -> bool:
        return uid in st.cfg.admin_ids

    @r.message(Command("update"))
    async def update_cmd(msg: Message):
        if admin(msg.from_user.id):
            await msg.answer(status_text(st))

    @r.message(F.document)
    async def got_zip(msg: Message):
        doc = msg.document
        name = (doc.file_name or "").lower()
        if not admin(msg.from_user.id) or not (name.startswith("torrbot") and name.endswith(".zip")):
            return
        if (doc.file_size or 0) > MAX_ZIP:
            await msg.answer("Архив больше 20 МБ — Telegram не даст его скачать боту.")
            return
        buf = io.BytesIO()
        try:
            await msg.bot.download(doc, destination=buf)
            ver = inspect(buf.getvalue())
        except BadZip as e:
            await msg.answer(f"❌ Не возьму этот архив: {esc(str(e))}.")
            return
        except Exception as e:
            log.warning("update: скачать архив: %r", e)
            await msg.answer("Не смог скачать архив из Telegram — пришли ещё раз.")
            return
        pending.clear()
        pending[msg.from_user.id] = (time.time(), buf.getvalue(), ver)
        warn = ""
        if vtuple(ver) == vtuple(__version__):
            warn = "\n⚠ Это та же версия, что сейчас стоит (переставлю заново)."
        elif vtuple(ver) < vtuple(__version__):
            warn = "\n⚠ Это <b>более старая</b> версия — откат."
        if not _read_json(paths(st)["agent"]):
            warn += ("\n⚠ Программа обновления на сервере не установлена — сначала на сервере:\n"
                     "<code>sudo sh ~/torrbot/host/install.sh</code> (из текущей версии; после неё — уже можно отсюда).")
        await msg.answer(f"📦 Архив torrbot v{esc(ver)} ({len(buf.getvalue()) // 1024} КБ) проверен.\n"
                         f"Обновить v{__version__} → v{esc(ver)}?{warn}\n\n"
                         "Бот перезапустится на минуту-две. Если новая версия не поднимется — сервер сам вернёт "
                         "старую. Итог придёт сообщением; при успехе изменения уйдут на GitHub.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                             B(text="✅ Обновить", callback_data="Up:go"), B(text="✖ Отмена", callback_data="Up:no")]]))

    @r.callback_query(F.data.in_({"Up:go", "Up:no"}))
    async def confirm(cb: CallbackQuery):
        uid = cb.from_user.id
        if not admin(uid):
            return await cb.answer("Только для администратора", show_alert=True)
        got = pending.pop(uid, None)
        if cb.data == "Up:no" or not got:
            await cb.answer()
            await cb.message.edit_text("Обновление отменено." if cb.data == "Up:no"
                                       else "Архив уже не помню — пришли его ещё раз.")
            return
        if time.time() - got[0] > 3600:
            await cb.answer()
            await cb.message.edit_text("Прошло больше часа — пришли архив ещё раз.")
            return
        p = paths(st)
        if os.path.exists(p["busy"]) or os.path.exists(p["req"]):
            return await cb.answer("Уже идёт обновление — дождись итога", show_alert=True)
        try:
            os.makedirs(st.cfg.update_dir, exist_ok=True)
            _write(p["zip"], got[1])
            _write(p["req"], json.dumps({"version": got[2], "from": __version__, "by": uid,
                                         "at": int(time.time())}).encode())
        except Exception as e:
            log.warning("update: записать заявку: %r", e)
            return await cb.answer(f"Не смог записать архив в {st.cfg.update_dir}: {e}", show_alert=True)
        await cb.answer()
        await cb.message.edit_text(f"⏳ Обновляю до v{esc(got[2])}… Бот перезапустится; итог пришлёт сервер.")

    return r
