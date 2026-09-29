"""Поиск фильма по описанию сюжета — полнотекстовый поиск Википедии.

Схема: слова из описания → поиск по статьям ru.wikipedia с карточкой «Фильм» /
«Телесериал» / «Мультфильм» (раздел «Сюжет» там подробный) → у найденных статей
берём из Викиданных ID TMDB → карточки фильмов из TMDB (обложка, год, рейтинг).
Без ключей и без ИИ: ищется по словам, поэтому описание должно содержать
что-то конкретное — предметы, имена, места, профессии.
"""
from __future__ import annotations

import asyncio
import re
import time

import aiohttp

from . import tmdb

WIKI_API = "https://ru.wikipedia.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
SPARQL_TIMEOUT = 5
# по правилам Википедии для программ: имя/версия и контакт владельца
HEADERS = {"User-Agent": "torrbot/6 (https://t.me/lerroy_kino_bot; home media bot)"}
TEMPLATES = ("Фильм", "Телесериал", "Мультфильм")
# одна запись на все типы карточек — один запрос вместо трёх
TEMPLATE_FILTER = 'hastemplate:"' + "|".join(TEMPLATES) + '"'
RETRY_DEFAULT = 120          # сек, если Википедия не сказала, сколько ждать

_busy_until = 0.0            # до какого времени Википедия просила её не трогать (429)


class WikiBusy(Exception):
    """Википедия ответила 429 «слишком много запросов» — ждём, сколько попросила."""

    def __init__(self, seconds: float):
        super().__init__(f"Википедия просит подождать {seconds:.0f} с")
        self.seconds = seconds


def busy_left() -> float:
    return max(0.0, _busy_until - time.monotonic())

# слова, которые есть почти в любом описании и только мешают поиску
_STOP = set("""
фильм фильма фильме фильмы кино кинофильм сериал сериала мультфильм мульт мультик
про о об где в во на с со и а но или что чтобы как кто который которая которое которые
его ее её их он она они оно там тут это этот эта эти тот та те его ему ей им
был была были было будет есть быть очень потом когда после до из за к ко по под над
от для у же ли бы не ни вот весь все всё там такой такая помню вроде кажется типа
один одна одного какой какая какие какого года год старый новый смотрел
мужик мужика мужику мужиком мужики мужиков чувак чувака пацан пацана пацаны парень парня
парню парнем баба бабы бабу тетка тетки тетку девка девки девку девчонка девчонки девчонку
чел чела человек человека штука штуку какой-то какая-то какие-то какого-то какую-то
""".split())
_RE_WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9-]{2,}")
_RE_TITLE_YEAR = re.compile(r"\((?:[^()]*,\s*)?(\d{4})\)\s*$")
_RE_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def keywords(text: str, limit: int = 8) -> list[str]:
    """Значимые слова описания, в исходном порядке, без повторов и стоп-слов."""
    out = []
    for w in _RE_WORD.findall(text.lower().replace("ё", "е")):
        if w not in _STOP and w not in out and not w.isdigit():
            out.append(w)
    return out[:limit]


def parse_title(title: str) -> tuple[str, str | None]:
    """«Маска (фильм, 1994)» → («Маска», «1994»)."""
    m = _RE_TITLE_YEAR.search(title)
    return _RE_PAREN.sub("", title).strip(), (m.group(1) if m else None)


async def _wiki_search(http: aiohttp.ClientSession, query: str, limit: int = 10) -> list[dict]:
    global _busy_until
    if busy_left() > 0:                       # пока просили подождать — не дёргаем, иначе срок продлят
        raise WikiBusy(busy_left())
    params = {"action": "query", "format": "json", "formatversion": "2", "generator": "search",
              "gsrsearch": query, "gsrnamespace": "0", "gsrlimit": str(limit),
              "prop": "pageprops", "ppprop": "wikibase_item"}
    async with http.get(WIKI_API, params=params, headers=HEADERS,
                        timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status == 429:
            try:
                wait = float(resp.headers.get("Retry-After") or RETRY_DEFAULT)
            except ValueError:
                wait = RETRY_DEFAULT
            wait = min(max(wait, 30.0), 3600.0)
            _busy_until = max(_busy_until, time.monotonic() + wait)
            raise WikiBusy(wait)
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    pages = (data.get("query") or {}).get("pages") or []
    return sorted(pages, key=lambda p: p.get("index", 999))


def _add(pages: list[dict], seen: set, out: list[dict]) -> None:
    for p in pages:
        if p.get("pageid") not in seen:
            seen.add(p.get("pageid"))
            out.append(p)


async def _search_many(http: aiohttp.ClientSession, queries: list[str]) -> list[list[dict]]:
    """Несколько запросов (с фильтром по карточке фильма/сериала/мульта) параллельно.
    Упавшие — пустые списки; если упали все — ошибка наружу (WikiBusy при 429)."""
    found = await asyncio.gather(*(_wiki_search(http, f"{q} {TEMPLATE_FILTER}") for q in queries),
                                 return_exceptions=True)
    if found and all(isinstance(f, BaseException) for f in found):
        busy = [f for f in found if isinstance(f, WikiBusy)]
        raise busy[0] if busy else found[0]
    return [f if not isinstance(f, BaseException) else [] for f in found]


async def find_pages(http: aiohttp.ClientSession, text: str, want: int = 6) -> list[dict]:
    """Статьи о фильмах/сериалах, подходящие под описание, по релевантности.
    1) все слова сразу (точнее);
    2) мало — «все слова, кроме одного»: одно неудачное слово, которого нет в статье
       («мужик», «лысый»), не должно ломать поиск. Первым идёт вариант, нашедший больше
       всего: значит, выброшенное в нём слово и было лишним;
    3) совсем мало — любое из слов (шире);
    4) пусто — нечёткий поиск («ахилеса~» найдёт «Ахиллеса»): спасает от опечаток;
    5) всё ещё пусто — без фильтра по карточке (берём только статьи с ID TMDB)."""
    words = keywords(text)
    if not words:
        return []
    seen, out = set(), []
    [full] = await _search_many(http, [" ".join(words)])
    _add(full, seen, out)
    if len(out) < want and len(words) >= 3:
        loo = [" ".join(w for j, w in enumerate(words) if j != i) for i in range(len(words))]
        res = await _search_many(http, loo)
        for lst in sorted(res, key=len, reverse=True):         # стабильно: при равенстве — по порядку
            _add(lst, seen, out)
    if len(out) < 3 and len(words) > 2:
        [anyw] = await _search_many(http, [" OR ".join(words)])
        _add(anyw, seen, out)
    if not out and any(len(w) >= 4 for w in words):
        fuzzy = " ".join(w + "~" if len(w) >= 4 else w for w in words)
        [fz] = await _search_many(http, [fuzzy])
        _add(fz, seen, out)
    if not out:
        variants = [" ".join(words)] + ([" OR ".join(words)] if len(words) > 2 else [])
        for q in variants:
            for page in await _wiki_search(http, f"{q} фильм", 20):
                if page.get("pageid") not in seen:
                    seen.add(page.get("pageid"))
                    out.append({**page, "_fallback": True})
            if out:
                break
    return out


_RE_QID = re.compile(r"^Q\d+$")


async def tmdb_ids(http: aiohttp.ClientSession, qids: list[str]) -> dict[str, tuple[str, str]] | None:
    """Q-номер Викиданных → ("movie"|"tv", TMDB ID).
    Один маленький SPARQL-запрос только за нужными свойствами (P4947 — фильм, P4983 —
    сериал): килобайт вместо мегабайт полных карточек. У сервиса строгий лимит частоты
    (429) — при любой ошибке или долгом ответе возвращаем None, и фильмы ищутся в TMDB
    по названию и году из заголовка статьи."""
    qids = [q for q in dict.fromkeys(qids) if _RE_QID.match(q)][:50]
    if not qids:
        return {}
    query = ("SELECT ?i ?m ?t WHERE { VALUES ?i { " + " ".join(f"wd:{q}" for q in qids) +
             " } OPTIONAL { ?i wdt:P4947 ?m } OPTIONAL { ?i wdt:P4983 ?t } }")
    try:
        async with http.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                            headers={**HEADERS, "Accept": "application/sparql-results+json"},
                            timeout=aiohttp.ClientTimeout(total=SPARQL_TIMEOUT)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except Exception:
        return None
    out = {}
    for row in (data.get("results") or {}).get("bindings") or []:
        qid = ((row.get("i") or {}).get("value") or "").rsplit("/", 1)[-1]
        m, t = (row.get("m") or {}).get("value"), (row.get("t") or {}).get("value")
        if m and qid not in out:
            out[qid] = ("movie", m)
        elif t and qid not in out:
            out[qid] = ("tv", t)
    return out


async def search_by_plot(http: aiohttp.ClientSession, key: str, text: str,
                         lang: str = "ru-RU", limit: int = 6) -> list[tmdb.Info]:
    """Описание → карточки TMDB. Статьи без ID TMDB ищем в TMDB по названию и году.
    Карточки запрашиваются параллельно, порядок — как у статей."""
    pages = await find_pages(http, text)
    if not pages:
        return []
    pages = pages[:limit * 2]
    ids = await tmdb_ids(http, [p["pageprops"]["wikibase_item"] for p in pages
                                if (p.get("pageprops") or {}).get("wikibase_item")])
    wikidata_ok = ids is not None
    ids = ids or {}

    async def resolve(p: dict) -> tmdb.Info | None:
        qid = (p.get("pageprops") or {}).get("wikibase_item")
        try:
            if qid in ids:
                kind, tid = ids[qid]
                return await tmdb.details(http, key, kind, tid, lang)
            if p.get("_fallback") and wikidata_ok:
                return None                   # без фильтра и без ID TMDB — не фильм
            name, year = parse_title(p.get("title") or "")
            if p.get("_fallback") and not year:
                return None                   # Викиданные не ответили: без года не рискуем
            return tmdb.pick(await tmdb.search(http, key, name, lang), year, False)
        except Exception:
            return None

    seen, out = set(), []
    for info in await asyncio.gather(*(resolve(p) for p in pages)):
        if len(out) >= limit:
            break
        if info and (info.is_tv, info.tmdb_id) not in seen:
            seen.add((info.is_tv, info.tmdb_id))
            out.append(info)
    return out
