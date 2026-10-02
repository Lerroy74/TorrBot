"""v7: сторож нагрузки и «черепаха».

Данные о диске, сети, просмотре по сети (Samba) и SMART собирает агент на самом сервере
(host/load-agent.py, вне Docker — из контейнера этого не видно) и каждые 5 секунд пишет
в data/host-load.json. Бот читает файл раз в 15 секунд и:

* предупреждает админа: диск захлёбывается (занят ≥ LOAD_UTIL% дольше LOAD_ALERT_SEC),
  канал забит (≥ 90% NET_LIMIT_MBIT), SMART диска ухудшился, агент перестал отвечать —
  с объяснением, кто нагружает (закачки, просмотр на ТВ, просмотр по сети);
* включает «черепаху» в Transmission (TURTLE_DOWN_MB / TURTLE_UP_MB), когда:
  - 🌙 дневной режим включён и сейчас DAY_HOURS;
  - 🛡 кто-то смотрит (Kodi на ТВ играет или по сети открыт фильм) — до конца просмотра;
  - 🛡 диск перегружен — минимум на LOAD_THROTTLE_MIN минут.
  Каждый из трёх поводов админ включает/выключает в /speed.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

log = logging.getLogger("torrbot")
esc = html.escape

# переключатели в prefs (user_id=0); по умолчанию: дневной режим выкл, остальное вкл
NIGHT = "night_mode"
WATCH_OFF = "guard_watch_off"
DISK_OFF = "guard_disk_off"
WATCH_HOLD = 120          # сек: после конца просмотра ещё держим «черепаху» (пауза, переключение серии)
STALE_SEC = 90            # файл агента старше — данных нет
AGENT_DEAD_SEC = 300      # столько без данных — сказать админу, что агент молчит


@dataclass
class Guard:
    utils: deque = field(default_factory=lambda: deque(maxlen=200))    # (время, % занятости диска)
    nets: deque = field(default_factory=lambda: deque(maxlen=200))     # (время, Мбит/с макс. из rx/tx)
    turtle: bool | None = None          # что сейчас выставлено в Transmission (None — не знаем)
    reasons: list[str] = field(default_factory=list)
    watch_seen: float = 0.0              # когда последний раз видели просмотр
    watching: list[str] = field(default_factory=list)
    disk_until: float = 0.0              # до какого времени тормозим из-за перегрузки
    alerted: dict[str, float] = field(default_factory=dict)
    smart_last: dict = field(default_factory=dict)
    agent_dead: bool = False
    last: dict | None = None             # последние данные агента


def in_hours(now: datetime, spec: str) -> bool:
    """«08:00-23:00» → сейчас внутри? Через полночь («22:00-07:00») тоже работает."""
    try:
        a, b = [x.strip() for x in spec.split("-")]
        ah, am = (int(x) for x in a.split(":"))
        bh, bm = (int(x) for x in b.split(":"))
    except ValueError:
        return False
    t, s, e = now.hour * 60 + now.minute, ah * 60 + am, bh * 60 + bm
    return s <= t < e if s <= e else (t >= s or t < e)


def read_load(path: str, now: float) -> tuple[dict | None, bool]:
    """(данные агента или None, файл вообще есть?). Старые данные — None."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True
    return (data if now - float(data.get("ts") or 0) <= STALE_SEC else None), True


def sustained(samples: deque, now: float, window: int, threshold: float) -> bool:
    """Все замеры за последние window секунд ≥ threshold, и замеры покрывают окно."""
    recent = [(t, v) for t, v in samples if now - t <= window]
    if not recent or now - recent[0][0] < window * 0.8:
        return False
    return all(v >= threshold for _, v in recent)


def settings(st) -> dict[str, bool]:
    return {"night": st.db.flag(0, NIGHT), "watch": not st.db.flag(0, WATCH_OFF),
            "disk": not st.db.flag(0, DISK_OFF)}


def mb(n) -> str:
    return f"{(n or 0) / 1024 ** 2:.1f} МБ/с"


async def watchers(st, data: dict | None) -> list[str]:
    """Кто сейчас смотрит: «ТВ: Маска», «по сети: Маска.mkv»."""
    out = []
    if st.kodi:
        try:
            now = await asyncio.wait_for(st.kodi.now_playing(), 5)
            if now and not now["paused"]:
                out.append(f"ТВ: {now['title'][:60]}")
        except Exception:
            pass
    kodi_host = urlparse(st.cfg.kodi_url or "").hostname
    for it in ((data or {}).get("smb") or {}).get("items") or []:
        if kodi_host and it.get("client") == kodi_host:
            continue                       # это сама малинка читает фильм — уже учтено выше как «ТВ»
        who = f" ({it['client']})" if it.get("client") else ""
        out.append(f"по сети{who}: {os.path.basename(str(it.get('name') or ''))[:60]}")
    return out


async def guard_once(bot: Bot, st, now: float | None = None, local: datetime | None = None) -> None:
    from .extras import notify_admins, now_local
    g: Guard = st.guard
    cfg = st.cfg
    now = now or time.time()
    local = local or now_local()
    data, exists = read_load(cfg.load_file, now)
    g.last = data
    sets = settings(st)

    # --- агент молчит ---
    if exists and data is None:
        since = g.alerted.setdefault("_nodata", now)
        if not g.agent_dead and now - since >= AGENT_DEAD_SEC:
            g.agent_dead = True
            await notify_admins(bot, st, "⚠ Агент нагрузки на сервере молчит — не вижу диск и сеть.\n"
                                         "Проверь: <code>systemctl status torrbot-load-agent</code>")
    elif data is not None:
        g.alerted.pop("_nodata", None)
        if g.agent_dead:
            g.agent_dead = False
            await notify_admins(bot, st, "✅ Агент нагрузки снова присылает данные.")

    disk = (data or {}).get("disk") or {}
    net = (data or {}).get("net") or {}
    if data is not None:
        g.utils.append((now, float(disk.get("util") or 0)))
        g.nets.append((now, max(float(net.get("rx_mbit") or 0), float(net.get("tx_mbit") or 0))))

    # --- кто смотрит ---
    g.watching = await watchers(st, data)
    if g.watching:
        g.watch_seen = now
    overloaded = sustained(g.utils, now, cfg.load_alert_sec, cfg.load_util)
    if overloaded and sets["disk"]:
        g.disk_until = max(g.disk_until, now + cfg.load_throttle_min * 60)

    # --- нужна ли «черепаха» ---
    reasons = []
    if sets["night"] and in_hours(local, cfg.day_hours):
        reasons.append(f"🌙 дневной режим ({cfg.day_hours})")
    if sets["watch"] and g.watch_seen and now - g.watch_seen <= WATCH_HOLD:
        reasons.append("🛡 идёт просмотр")
    if sets["disk"] and now < g.disk_until:
        reasons.append("🛡 диск был перегружен")
    g.reasons = reasons
    want = bool(reasons)
    if want != g.turtle:
        try:
            await st.tr.turtle(want, int(cfg.turtle_down_mb * 1000), int(cfg.turtle_up_mb * 1000))
            log.info("черепаха: %s (%s)", "вкл" if want else "выкл", ", ".join(reasons) or "поводов нет")
            g.turtle = want
        except Exception as e:
            log.info("черепаха: Transmission не ответил: %s", e)

    # --- тревоги ---
    cool = cfg.load_cooldown_min * 60
    if overloaded and now - g.alerted.get("disk", 0) >= cool:
        g.alerted["disk"] = now
        await notify_admins(bot, st, await overload_text(st, data, sets))
    limit = cfg.net_limit_mbit * 0.9
    if cfg.net_limit_mbit and sustained(g.nets, now, cfg.load_alert_sec, limit) and \
            now - g.alerted.get("net", 0) >= cool:
        g.alerted["net"] = now
        tr = await tr_line(st)
        await notify_admins(bot, st, f"🌐 Канал забит: ⬇ {net.get('rx_mbit', 0):.0f} / ⬆ {net.get('tx_mbit', 0):.0f} "
                                     f"Мбит/с при тарифе {cfg.net_limit_mbit} уже {cfg.load_alert_sec // 60} мин.\n"
                                     f"{tr}\nVPN у пользователей и просмотр могут тормозить.")
    await smart_check(bot, st, (data or {}).get("smart"), now)


async def tr_stats(st) -> dict | None:
    try:
        return await st.tr.stats()
    except Exception:
        return None


def tr_text(s: dict | None) -> str:
    if s is None:
        return "Transmission не ответил"
    return (f"Transmission: ⬇ {mb(s.get('downloadSpeed'))}, ⬆ {mb(s.get('uploadSpeed'))}, "
            f"активных раздач {s.get('activeTorrentCount', '?')}")


async def tr_line(st) -> str:
    return tr_text(await tr_stats(st))


async def overload_text(st, data: dict | None, sets: dict) -> str:
    cfg, g = st.cfg, st.guard
    d = (data or {}).get("disk") or {}
    s = await tr_stats(st)
    busy_tr = bool(s) and (s.get("downloadSpeed") or 0) + (s.get("uploadSpeed") or 0) > 512 * 1024
    lines = [f"💽 <b>Диск захлёбывается</b>: занят {d.get('util', 0):.0f}% уже {cfg.load_alert_sec // 60} мин "
             f"(запись {d.get('write_mb', 0):.1f} МБ/с, чтение {d.get('read_mb', 0):.1f} МБ/с, "
             f"отклик {d.get('await_ms', 0):.0f} мс).", tr_text(s)]
    if g.watching:
        lines.append("Смотрят: " + esc("; ".join(g.watching)))
    if not g.watching and not busy_tr:
        lines.append("Закачек и просмотра нет — нагружает что-то другое (Kodi сканирует, бэкап, проверка диска).")
    if sets["disk"]:
        lines.append(f"🐢 Притормозил закачки до {cfg.turtle_down_mb:g} МБ/с минимум на {cfg.load_throttle_min} мин.")
    else:
        lines.append("Автоторможение при перегрузке выключено — /speed.")
    if g.watching and busy_tr:
        lines.append("Если просмотр дёргается — это из-за закачек: диск не успевает и писать, и читать.")
    return "\n".join(lines)


async def smart_check(bot: Bot, st, smart: dict | None, now: float) -> None:
    from .extras import notify_admins
    if not smart or smart.get("error"):
        return
    g = st.guard
    prev = g.smart_last
    msgs = []
    if smart.get("health") and smart.get("health") != "PASSED" and prev.get("health") != smart.get("health"):
        msgs.append(f"‼ SMART диска: <b>{esc(str(smart['health']))}</b> — диск может скоро отказать, сделай копию важного.")
    for k, name in (("realloc", "переназначенных секторов"), ("pending", "нестабильных секторов"),
                    ("uncorrect", "неисправимых ошибок")):
        v, pv = int(smart.get(k) or 0), prev.get(k)
        if pv is not None and v > int(pv):
            msgs.append(f"⚠ SMART: стало больше {name}: {pv} → {v}. Диск начинает сыпаться.")
        elif pv is None and v > 0 and not prev:
            msgs.append(f"⚠ SMART: {name} — {v}. Стоит присматривать за диском.")
    temp = smart.get("temp")
    if temp and int(temp) >= 55 and now - g.alerted.get("temp", 0) >= 6 * 3600:
        g.alerted["temp"] = now
        msgs.append(f"🌡 Диск нагрелся до {temp}°C — проверь охлаждение.")
    g.smart_last = {k: smart.get(k) for k in ("health", "realloc", "pending", "uncorrect")}
    for m in msgs:
        await notify_admins(bot, st, m)


def summary(st) -> list[str]:
    """Строки для /health и /speed."""
    g, cfg = st.guard, st.cfg
    data = g.last
    if data is None:
        exists = os.path.exists(cfg.load_file)
        return ["⚠ 📈 Нагрузка: агент " + ("молчит" if exists else "не установлен (sudo sh ~/torrbot/host/install.sh)")]
    d, n = data.get("disk") or {}, data.get("net") or {}
    out = [f"{'⚠' if d.get('util', 0) >= cfg.load_util else '✅'} 💽 Диск {esc(str(d.get('dev', '?')))}: "
           f"занят {d.get('util', 0):.0f}%, запись {d.get('write_mb', 0):.1f} МБ/с, "
           f"чтение {d.get('read_mb', 0):.1f} МБ/с, отклик {d.get('await_ms', 0):.0f} мс",
           f"✅ 🌐 Сеть: ⬇ {n.get('rx_mbit', 0):.0f} / ⬆ {n.get('tx_mbit', 0):.0f} Мбит/с"]
    sm = data.get("smart") or {}
    if sm.get("health"):
        ok = sm.get("health") == "PASSED" and not any(int(sm.get(k) or 0) for k in ("realloc", "pending", "uncorrect"))
        out.append(f"{'✅' if ok else '⚠'} 🩺 SMART: {esc(str(sm['health']))}"
                   + (f", {sm['temp']}°C" if sm.get("temp") else "")
                   + (f", переназн. {sm.get('realloc', 0)}, нестаб. {sm.get('pending', 0)}" if not ok else ""))
    elif sm.get("error"):
        out.append(f"❔ 🩺 SMART: {esc(str(sm['error'])[:80])}")
    if g.watching:
        out.append("👀 Смотрят: " + esc("; ".join(g.watching)))
    return out


async def speed_view(st) -> tuple[str, InlineKeyboardMarkup]:
    cfg, g = st.cfg, st.guard
    sets = settings(st)
    on = lambda b: "вкл ✅" if b else "выкл"          # noqa: E731
    state = (f"🐢 <b>Сейчас притормаживаю</b> до {cfg.turtle_down_mb:g} МБ/с: " + ", ".join(g.reasons)
             if g.reasons else "🚀 Сейчас качаю на полной скорости")
    lines = ["⚙ <b>Скорость и нагрузка</b>", state, await tr_line(st), ""] + summary(st) + [
        "",
        f"🌙 Дневной режим ({cfg.day_hours}): {on(sets['night'])}",
        f"🛡 Тормозить, пока смотрят: {on(sets['watch'])}",
        f"🛡 Тормозить при перегрузке диска: {on(sets['disk'])}",
        f"«Черепаха»: ⬇ {cfg.turtle_down_mb:g} МБ/с, ⬆ {cfg.turtle_up_mb:g} МБ/с (TURTLE_DOWN_MB / TURTLE_UP_MB)"]
    rows = [[B(text=f"🌙 Дневной режим: {'выключить' if sets['night'] else 'включить'}", callback_data="sp:night")],
            [B(text=f"🛡 При просмотре: {'выключить' if sets['watch'] else 'включить'}", callback_data="sp:watch")],
            [B(text=f"🛡 При перегрузке: {'выключить' if sets['disk'] else 'включить'}", callback_data="sp:disk")],
            [B(text="🔄 Обновить", callback_data="sp:rf")]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def build_router(st) -> Router:
    r = Router()
    st.hooks["load_summary"] = lambda: summary(st)

    @r.message(Command("speed"))
    async def speed(msg: Message):
        if msg.from_user.id in st.cfg.admin_ids:
            t, k = await speed_view(st)
            await msg.answer(t, reply_markup=k)

    @r.callback_query(F.data.regexp(r"^sp:(night|watch|disk|rf)$"))
    async def speed_btn(cb: CallbackQuery, bot: Bot):
        if cb.from_user.id not in st.cfg.admin_ids:
            await cb.answer("Только для администратора", show_alert=True)
            return
        act = cb.data[3:]
        note = "Обновлено"
        if act == "night":
            v = not st.db.flag(0, NIGHT)
            st.db.set_flag(0, NIGHT, v)
            note = "🌙 Дневной режим " + ("включён" if v else "выключен")
        elif act == "watch":
            v = st.db.flag(0, WATCH_OFF)
            st.db.set_flag(0, WATCH_OFF, not v)
            note = "Торможение при просмотре " + ("включено" if v else "выключено")
        elif act == "disk":
            v = st.db.flag(0, DISK_OFF)
            st.db.set_flag(0, DISK_OFF, not v)
            if not v:
                st.guard.disk_until = 0
            note = "Торможение при перегрузке " + ("включено" if v else "выключено")
        if act != "rf":
            try:
                await guard_once(bot, st)              # применить сразу, не ждать 15 секунд
            except Exception as e:
                log.info("speed: %r", e)
        text, markup = await speed_view(st)
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        await cb.answer(note)

    return r


async def guard_loop(bot: Bot, st) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await guard_once(bot, st)
        except Exception as e:
            log.warning("сторож нагрузки: %r", e)
        await asyncio.sleep(15)
