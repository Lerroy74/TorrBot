"""Поиск фильма по размытому описанию через ИИ — цепочка из нескольких сервисов.

Схема: описание → ИИ возвращает до 6 вариантов «название + год + фильм/сериал» →
каждый проверяем в TMDB (ИИ иногда выдумывает фильмы — такие отсеются) → обычные карточки.

Сервисы — по порядку AI_ORDER (по умолчанию yandex,groq,gemini); без ключа — пропускаются:
* yandex — Алиса (Alice AI LLM) / YandexGPT в Yandex Cloud AI Studio: YANDEX_API_KEY,
  YANDEX_FOLDER_ID, YANDEX_MODEL. Платно, копейки за запрос. Ходит напрямую.
* groq — бесплатный уровень Groq: GROQ_API_KEY, GROQ_MODEL. Через прокси (GROQ_PROXY).
* gemini — бесплатный уровень Google: GEMINI_API_KEY. Через прокси (GEMINI_PROXY).

Сервис ответил ошибкой (лимит, нет денег, неверный ключ, нет связи) — бот берёт следующий,
а этот «отдыхает» (лимит — до сброса, ключ — 6 ч, связь — 5 мин). Не ответил никто —
AiUnavailable: бот скажет об этом и поищет по Википедии.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import aiohttp

from . import tmdb

log = logging.getLogger("torrbot")

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API = GEMINI_API            # старое имя (тесты)
GEMINI_FALLBACK = ("gemini-flash-latest", "gemini-2.5-flash")
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"
GROQ_FALLBACK = ("llama-3.3-70b-versatile",)

PROMPT = """Ты помогаешь найти фильм или сериал по описанию зрителя. Описание может быть неточным,
с ошибками, разговорным. Назови до 6 самых вероятных вариантов, лучший — первым.
Отвечай ТОЛЬКО JSON-массивом без пояснений, элементы такие:
{"title": "название как в российском прокате", "original": "оригинальное название", "year": 1994, "tv": false}
Если ничего не подходит — пустой массив [].
Описание: """

REC_PROMPT = """Ты помогаешь зрителю выбрать, что посмотреть. Ниже его запрос и фильмы, которые он высоко
оценил (оценка из 10). Предложи до 8 фильмов или сериалов, которые подходят под запрос и, судя по оценкам,
понравятся ему. Не предлагай то, что он уже видел. Лучшее — первым.
Отвечай ТОЛЬКО JSON-массивом без пояснений, элементы такие:
{"title": "название как в российском прокате", "original": "оригинальное название", "year": 1994, "tv": false}
"""

TITLES = {"yandex": "Алиса", "groq": "Groq", "gemini": "Gemini"}


class AiUnavailable(Exception):
    """ИИ сейчас недоступен: reason — коротко для пользователя; rest — сколько секунд не трогать."""

    def __init__(self, reason: str, rest: float = 300):
        super().__init__(reason)
        self.reason = reason
        self.rest = rest


def parse_answer(text: str, limit: int = 6) -> list[dict]:
    """JSON-массив из ответа модели (бывает обёрнут в ```json … ```). Мусор — пустой список."""
    text = (text or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        if isinstance(d, dict) and (d.get("title") or d.get("original")):
            y = str(d.get("year") or "")
            out.append({"title": str(d.get("title") or ""), "original": str(d.get("original") or ""),
                        "year": y[:4] if y[:4].isdigit() else "", "tv": bool(d.get("tv"))})
    return out[:limit]


def _retry_after(resp: aiohttp.ClientResponse, default: float) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After") or default), 30.0), 24 * 3600.0)
    except ValueError:
        return default


async def _error(resp: aiohttp.ClientResponse) -> AiUnavailable:
    """HTTP-ошибка сервиса → понятная причина и сколько ему «отдыхать»."""
    body = (await resp.text())[:500].lower()
    if resp.status == 429:
        return AiUnavailable("закончился лимит запросов", _retry_after(resp, 3600))
    if resp.status == 402 or "billing" in body or "balance" in body or "payment" in body:
        return AiUnavailable("закончились деньги на счёте", 3600)
    if resp.status in (401, 403):
        if "location" in body or "region" in body or "country" in body:
            return AiUnavailable("не пускает из этой страны (нужен прокси)", 6 * 3600)
        return AiUnavailable("ключ не подошёл", 6 * 3600)
    return AiUnavailable(f"ошибка {resp.status}", 300)


# ---------- отдельные сервисы ----------
def _meter(meter: dict | None, tin, tout) -> None:
    """Сколько токенов ушло (сервисы сами пишут это в ответе)."""
    if meter is not None:
        meter["tin"] = meter.get("tin", 0) + int(tin or 0)
        meter["tout"] = meter.get("tout", 0) + int(tout or 0)


async def ask(http: aiohttp.ClientSession, key: str, model: str, text: str, prompt: str = PROMPT,
              meter: dict | None = None, limit: int = 500, answers: int = 6) -> list[dict]:
    """Gemini. Ошибки → AiUnavailable."""
    body = {"contents": [{"parts": [{"text": prompt + text[:limit]}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
    last = "нет ответа"
    for m in [model] + [x for x in GEMINI_FALLBACK if x != model]:
        try:
            async with http.post(API.format(model=m), json=body, headers={"x-goog-api-key": key},
                                 timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status == 404:                  # такой модели нет — пробуем следующую
                    last = f"модель {m} не найдена"
                    continue
                if resp.status >= 400:
                    raise await _error(resp)
                data = await resp.json(content_type=None)
        except AiUnavailable:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise AiUnavailable("нет связи") from e
        parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        um = data.get("usageMetadata") or {}
        _meter(meter, um.get("promptTokenCount"), um.get("candidatesTokenCount"))
        return parse_answer("".join(p.get("text") or "" for p in parts), answers)
    raise AiUnavailable(last, 6 * 3600)


async def ask_openai(http: aiohttp.ClientSession, url: str, headers: dict, models: list[str], text: str,
                     max_tokens: int = 1500, prompt: str = PROMPT, meter: dict | None = None,
                     limit: int = 500, answers: int = 6) -> list[dict]:
    """OpenAI-совместимый API (Yandex AI Studio, Groq). Ошибки → AiUnavailable."""
    last = "нет ответа"
    for m in models:
        body = {"model": m, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt + text[:limit]}]}
        try:
            async with http.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 404 or (resp.status == 400 and "model" in (await resp.text()).lower()):
                    last = f"модель {m} не найдена"
                    continue
                if resp.status >= 400:
                    raise await _error(resp)
                data = await resp.json(content_type=None)
        except AiUnavailable:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise AiUnavailable("нет связи") from e
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        usage = data.get("usage") or {}
        _meter(meter, usage.get("prompt_tokens") or usage.get("input_text_tokens"),
               usage.get("completion_tokens") or usage.get("completion_text_tokens"))
        return parse_answer(msg.get("content") or "", answers)
    raise AiUnavailable(last, 6 * 3600)


# ---------- цепочка ----------
@dataclass
class Provider:
    name: str
    http: aiohttp.ClientSession | None
    rest_until: float = 0.0
    why: str = ""

    @property
    def title(self) -> str:
        return TITLES.get(self.name, self.name)


def yandex_model_uri(cfg) -> str:
    m = cfg.yandex_model.strip()
    if m.startswith("gpt://"):
        return m
    return f"gpt://{cfg.yandex_folder}/{m.strip('/')}"


class Chain:
    def __init__(self, cfg, sessions: dict[str, aiohttp.ClientSession | None]):
        self.cfg = cfg
        keys = {"yandex": bool(cfg.yandex_key and cfg.yandex_folder), "groq": bool(cfg.groq_key),
                "gemini": bool(cfg.gemini_key)}
        self.providers = [Provider(n, sessions.get(n)) for n in cfg.ai_order if keys.get(n)]
        self.disabled: set[str] = set()          # v8: выключены админом в /ai

    def __bool__(self) -> bool:
        return bool(self.providers)

    def active(self) -> list[Provider]:
        return [p for p in self.providers if p.name not in self.disabled]

    async def _ask(self, p: Provider, text: str, prompt: str = PROMPT, meter: dict | None = None,
                   limit: int = 500, answers: int = 6) -> list[dict]:
        cfg = self.cfg
        kw = dict(prompt=prompt, meter=meter, limit=limit, answers=answers)
        if p.name == "yandex":
            return await ask_openai(p.http, f"{cfg.yandex_url}/chat/completions",
                                    {"Authorization": f"Api-Key {cfg.yandex_key}", "OpenAI-Project": cfg.yandex_folder},
                                    [yandex_model_uri(cfg)], text, max_tokens=800 if answers <= 6 else 1200, **kw)
        if p.name == "groq":
            return await ask_openai(p.http, GROQ_API, {"Authorization": f"Bearer {cfg.groq_key}"},
                                    [cfg.groq_model] + [m for m in GROQ_FALLBACK if m != cfg.groq_model], text, **kw)
        return await ask(p.http, cfg.gemini_key, cfg.gemini_model, text, **kw)

    async def guess(self, text: str, now: float | None = None, prompt: str = PROMPT, meter: dict | None = None,
                    limit: int = 500, answers: int = 6) -> tuple[list[dict], str]:
        """(варианты, кто ответил). Не ответил никто — AiUnavailable с причинами.
        meter (если передан) получает provider, tin, tout — для учёта расхода (v8)."""
        reasons = []
        for p in self.active():
            now = now or time.time()
            if p.rest_until > now:
                reasons.append(f"{p.title}: {p.why}")
                continue
            m: dict = {}
            try:
                got = await self._ask(p, text, prompt, m, limit, answers)
            except AiUnavailable as e:
                p.rest_until, p.why = now + e.rest, e.reason
                log.info("ИИ %s: %s — отдыхает %.0f мин", p.title, e.reason, e.rest / 60)
                reasons.append(f"{p.title}: {e.reason}")
                continue
            p.why = ""
            log.info("ИИ %s: %d вариантов", p.title, len(got))
            if meter is not None:
                meter.update(provider=p.name, tin=m.get("tin", 0), tout=m.get("tout", 0))
            return got, p.title
        raise AiUnavailable("; ".join(reasons) or ("все сервисы выключены в /ai" if self.providers
                                                   else "нет ни одного ключа"))

    def status(self, now: float | None = None) -> str:
        now = now or time.time()
        parts = []
        for p in self.providers:
            if p.name in self.disabled:
                parts.append(f"{p.title} ⛔ выключен")
            elif p.rest_until > now:
                parts.append(f"{p.title} ⏸ {p.why} (до {time.strftime('%H:%M', time.localtime(p.rest_until))})")
            else:
                parts.append(f"{p.title} ✅")
        return ", ".join(parts) if parts else "ключей нет — только Википедия"


async def resolve(http: aiohttp.ClientSession, key: str, guesses: list[dict],
                  lang: str = "ru-RU") -> list[tmdb.Info]:
    """Варианты ИИ → карточки TMDB (параллельно); несуществующие отбрасываются."""

    async def one(g: dict) -> tmdb.Info | None:
        for name in dict.fromkeys(n for n in (g["title"], g["original"]) if n):
            try:
                cands = await tmdb.search(http, key, f"{name} {g['year']}".strip(), lang)
            except Exception:
                return None
            pool = [c for c in cands if c.get("media_type") in ("movie", "tv")]
            if g["year"]:      # год обязан совпасть (±1): иначе это не тот фильм
                pool = [c for c in pool if abs(int((c.get("release_date") or c.get("first_air_date") or "0")[:4] or 0)
                                               - int(g["year"])) <= 1]
            info = tmdb.pick(pool, g["year"] or None, g["tv"])
            if info:
                return info
        return None

    seen, out = set(), []
    for info in await asyncio.gather(*(one(g) for g in guesses)):
        if info and (info.is_tv, info.tmdb_id) not in seen:
            seen.add((info.is_tv, info.tmdb_id))
            out.append(info)
    return out


async def search_by_plot(chain: Chain, tmdb_http: aiohttp.ClientSession, cfg, text: str,
                         meter: dict | None = None) -> tuple[list[tmdb.Info], str]:
    """(карточки, кто из ИИ ответил)."""
    guesses, who = await chain.guess(text, meter=meter)
    return (await resolve(tmdb_http, cfg.tmdb_key, guesses, cfg.tmdb_lang) if guesses else []), who
