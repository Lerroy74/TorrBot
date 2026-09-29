"""Обложка и описание фильма/сериала из TMDB (та же база, что у Kodi).

Ключ берётся из TMDB_API_KEY: подходит и короткий «API Key (v3)», и длинный
«API Read Access Token» (начинается с eyJ...)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import aiohttp

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"

_RE_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_RE_JUNK = re.compile(r"[\[\](){}|/\\:;,.!?\"'«»]+")


@dataclass
class Info:
    tmdb_id: int
    is_tv: bool
    title: str
    original_title: str
    year: str
    overview: str
    rating: float
    poster: str | None

    def caption(self, max_overview: int = 600) -> str:
        """Подпись к обложке (HTML, укладывается в лимит Telegram 1024 символа)."""
        esc = html.escape
        head = f"{'📺' if self.is_tv else '🎬'} <b>{esc(self.title)}</b>"
        if self.year:
            head += f" ({self.year})"
        if self.rating:
            head += f" · ⭐ {self.rating:.1f}"
        lines = [head]
        if self.original_title and self.original_title.lower() != self.title.lower():
            lines.append(f"<i>{esc(self.original_title)}</i>")
        ov = self.overview.strip()
        if len(ov) > max_overview:
            ov = ov[:max_overview].rsplit(" ", 1)[0] + "…"
        if ov:
            lines.append("")
            lines.append(esc(ov))
        return "\n".join(lines)


def split_query(query: str) -> tuple[str, str | None]:
    """«Дюна 2021» → («Дюна», «2021»). Год берётся последний из найденных."""
    matches = list(_RE_YEAR.finditer(query))
    year = matches[-1].group(1) if matches else None
    # вырезаем только последний год: «Бегущий по лезвию 2049 (2017)» → «Бегущий по лезвию 2049»
    text = query[:matches[-1].start()] + " " + query[matches[-1].end():] if matches else query
    text = " ".join(_RE_JUNK.sub(" ", text).split())
    return (text or query.strip()), year


def auth(key: str) -> tuple[dict, dict]:
    """(заголовки, параметры) для ключа v3 или токена v4."""
    key = key.strip()
    if key.startswith("eyJ"):
        return {"Authorization": f"Bearer {key}"}, {}
    return {}, {"api_key": key}


def _to_info(c: dict) -> Info:
    is_tv = c.get("media_type") == "tv"
    date = c.get("first_air_date" if is_tv else "release_date") or ""
    return Info(
        tmdb_id=int(c.get("id") or 0),
        is_tv=is_tv,
        title=c.get("name" if is_tv else "title") or "",
        original_title=c.get("original_name" if is_tv else "original_title") or "",
        year=date[:4],
        overview=c.get("overview") or "",
        rating=float(c.get("vote_average") or 0),
        poster=f"{IMG}{c['poster_path']}" if c.get("poster_path") else None,
    )


def pick(candidates: list[dict], year: str | None, prefer_tv: bool) -> Info | None:
    """Выбирает самый вероятный фильм/сериал из ответа /search/multi.

    TMDB уже сортирует по релевантности; сверху поднимаем совпадение по году
    и нужный тип (фильм или сериал — по тому, что нашлось на трекерах).
    Без обложки кандидат не нужен."""
    pool = [c for c in candidates
            if c.get("media_type") in ("movie", "tv") and c.get("poster_path")]
    if not pool:
        return None

    def year_of(c: dict) -> str:
        return (c.get("first_air_date") or c.get("release_date") or "")[:4]

    def score(item: tuple[int, dict]) -> tuple:
        pos, c = item
        return (
            bool(year) and year_of(c) == year,
            (c.get("media_type") == "tv") == prefer_tv,
            -pos,
        )

    best = max(enumerate(pool), key=score)[1]
    return _to_info(best)


async def search(http: aiohttp.ClientSession, key: str, query: str, lang: str = "ru-RU") -> list[dict]:
    text, _ = split_query(query)
    headers, params = auth(key)
    params.update({"query": text, "language": lang, "include_adult": "false", "page": "1"})
    async with http.get(f"{API}/search/multi", params=params, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return data.get("results") or []


async def _get(http: aiohttp.ClientSession, key: str, path: str, params: dict) -> dict:
    headers, base = auth(key)
    base.update(params)
    async with http.get(f"{API}{path}", params=base, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


# ---------- выбор «что смотреть» ----------
@dataclass
class Person:
    tmdb_id: int
    name: str
    department: str          # Acting / Directing / ...
    known_for: list[str]
    photo: str | None = None


def _year_of(c: dict) -> str:
    return (c.get("first_air_date") or c.get("release_date") or "")[:4]


def choices(candidates: list[dict], year: str | None = None, limit: int = 6) -> list[Info]:
    """Фильмы и сериалы из /search/multi для кнопок выбора. Порядок TMDB
    (релевантность), но совпавшие по году — вперёд; без года и без обложки,
    с нулём голосов (мусор) — не показываем."""
    pool = [c for c in candidates if c.get("media_type") in ("movie", "tv")
            and (c.get("poster_path") or c.get("vote_count"))]
    if year:
        pool.sort(key=lambda c: _year_of(c) != year)       # sort стабильный
    return [_to_info(c) for c in pool[:limit]]


def people(candidates: list[dict], limit: int = 2) -> list[Person]:
    out = []
    for c in candidates:
        if c.get("media_type") != "person" or not c.get("name"):
            continue
        kf = [k.get("title") or k.get("name") or "" for k in (c.get("known_for") or [])]
        photo = f"{IMG}{c['profile_path']}" if c.get("profile_path") else None
        out.append(Person(int(c["id"]), c["name"], c.get("known_for_department") or "", [k for k in kf if k][:3],
                          photo))
    return out[:limit]


def is_person_query(query: str, candidates: list[dict]) -> bool:
    """Запрос — это имя человека: первый результат TMDB — персона с таким именем."""
    if not candidates or candidates[0].get("media_type") != "person":
        return False
    q = _norm(split_query(query)[0])
    names = {_norm(candidates[0].get("name") or ""), _norm(candidates[0].get("original_name") or "")}
    return q in names or any(q and n and (q in n or n in q) for n in names)


_SKIP_GENRES = {10767, 10763}          # ток-шоу, новости — это не «фильмография»


def filmography(credits: dict, department: str = "", limit: int = 12) -> list[Info]:
    """Фильмы/сериалы человека из /person/{id}/combined_credits: для режиссёров —
    то, что снял; для актёров — где играл. Самые известные (по числу голосов) сверху."""
    if department == "Directing":
        items = [c for c in credits.get("crew") or [] if c.get("job") == "Director"]
    else:
        items = [c for c in credits.get("cast") or []
                 if not (set(c.get("genre_ids") or []) & _SKIP_GENRES)
                 and "self" not in (c.get("character") or "").lower()
                 and "himself" not in (c.get("character") or "").lower()]
    seen, out = set(), []
    for c in sorted(items, key=lambda c: -(c.get("vote_count") or 0)):
        key = (c.get("media_type"), c.get("id"))
        if key in seen or c.get("media_type") not in ("movie", "tv") or not _year_of(c):
            continue
        seen.add(key)
        out.append(_to_info(c))
        if len(out) >= limit:
            break
    return out


async def person_credits(http: aiohttp.ClientSession, key: str, pid: int, lang: str = "ru-RU") -> dict:
    return await _get(http, key, f"/person/{pid}/combined_credits", {"language": lang})


async def person_details(http: aiohttp.ClientSession, key: str, pid: int, lang: str = "ru-RU") -> dict:
    """Карточка человека: фото (profile_path), даты рождения и смерти."""
    return await _get(http, key, f"/person/{pid}", {"language": lang})


def person_years(d: dict) -> str:
    """«род. 1962» или «1930–2008»; неизвестно — пусто."""
    b, dd = (d.get("birthday") or "")[:4], (d.get("deathday") or "")[:4]
    if b and dd:
        return f"{b}–{dd}"
    return f"род. {b}" if b else ""


def person_photo(d: dict) -> str | None:
    return f"{IMG}{d['profile_path']}" if d.get("profile_path") else None


def tracker_queries(info: Info) -> list[str]:
    """Что искать на трекерах для выбранного фильма: русское и оригинальное название."""
    out = []
    for t in (info.title, info.original_title):
        t = " ".join(_RE_JUNK.sub(" ", t or "").split())
        if t and t.lower() not in (o.lower() for o in out):
            out.append(t)
    return out


def short_label(info: Info, width: int = 40) -> str:
    """Подпись кнопки: «🎬 Маска (1994)»."""
    t = info.title if len(info.title) <= width else info.title[:width - 1] + "…"
    return f"{'📺' if info.is_tv else '🎬'} {t}" + (f" ({info.year})" if info.year else "")


async def fetch_image(http: aiohttp.ClientSession, url: str) -> bytes:
    async with http.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        return await resp.read()


# ---------- папки для Kodi ----------
_RE_BAD_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RE_NORM = re.compile(r"[\W_]+", re.U)
_RE_ARTICLE = re.compile(r"^(the|a|an) ")


def _norm(s: str) -> str:
    return " ".join(_RE_NORM.sub(" ", s.lower().replace("ё", "е")).split())


def folder_name(info: Info) -> str:
    """«Иван Васильевич меняет всё! (2023)» — так Kodi точно узнаёт фильм/сериал."""
    name = " ".join(_RE_BAD_FS.sub(" ", info.title or info.original_title).split()).strip(" .")
    name = name[:100] or "Без названия"
    return f"{name} ({info.year})" if info.year else name


def safe_folder(name: str) -> str | None:
    """Имя папки из произвольной строки (без запрещённых символов); пусто — None."""
    name = " ".join(_RE_BAD_FS.sub(" ", name or "").split()).strip(" .")[:100]
    return name or None


def matches(info: Info, release_title: str, is_series: bool) -> bool:
    """Раздача точно про этот фильм? Иначе папку по TMDB не делаем — лучше
    положить как есть, чем подписать чужим названием."""
    if info.is_tv != is_series:
        return False
    rel = f" {_norm(release_title)} "
    names = set()
    for n in (info.title, info.original_title):
        n = _norm(n or "")
        if n:
            names.add(n)
            names.add(_RE_ARTICLE.sub("", n))
    if not any(f" {n} " in rel for n in names if n):
        return False
    if is_series or not info.year:
        return True
    years = {int(y) for y in _RE_YEAR.findall(release_title)}
    return not years or any(abs(y - int(info.year)) <= 1 for y in years)


# ---------- карточка по ID и подбор по фильтрам ----------
async def details(http: aiohttp.ClientSession, key: str, kind: str, tid: str | int,
                  lang: str = "ru-RU") -> Info | None:
    """Фильм/сериал по ID TMDB (kind — "movie" или "tv")."""
    data = await _get(http, key, f"/{kind}/{tid}", {"language": lang})
    if not data or not data.get("id"):
        return None
    data["media_type"] = kind
    return _to_info(data)


# жанры TMDB (id одинаковые во всех языках); для сериалов часть жанров своя
GENRES_MOVIE = [(28, "Боевик"), (35, "Комедия"), (18, "Драма"), (878, "Фантастика"),
                (53, "Триллер"), (27, "Ужасы"), (12, "Приключения"), (80, "Криминал"),
                (10749, "Мелодрама"), (16, "Мультфильм"), (14, "Фэнтези"), (9648, "Детектив"),
                (10751, "Семейный"), (36, "История"), (10752, "Военный"), (99, "Документальный")]
GENRES_TV = [(35, "Комедия"), (18, "Драма"), (10765, "Фантастика"), (80, "Криминал"),
             (9648, "Детектив"), (10759, "Боевик"), (16, "Мультфильм"), (10751, "Семейный"),
             (10768, "Военный"), (99, "Документальный")]
DECADES = [("", "Любые годы"), ("2020", "2020-е"), ("2010", "2010-е"), ("2000", "2000-е"),
           ("1990", "90-е"), ("1980", "80-е"), ("1970", "70-е"), ("1900", "до 1970")]


def discover_params(kind: str, genre: str, decade: str, country: str, sort: str, page: int = 1) -> dict:
    """Параметры /discover для выбора из /podbor.
    kind: m|t; genre: id или ""; decade: "1990" или ""; country: ru|""; sort: top|pop."""
    tv = kind == "t"
    p: dict = {"page": str(page), "include_adult": "false"}
    if genre:
        p["with_genres"] = genre
    if decade:
        y0 = int(decade)
        y1 = 1969 if y0 == 1900 else y0 + 9
        field = "first_air_date" if tv else "primary_release_date"
        p[f"{field}.gte"] = f"{y0}-01-01"
        p[f"{field}.lte"] = f"{y1}-12-31"
    if country == "ru":
        p["with_origin_country"] = "RU|SU"
    if sort == "top":
        p["sort_by"] = "vote_average.desc"
        p["vote_count.gte"] = "50" if country == "ru" else "300"
    else:
        p["sort_by"] = "popularity.desc"
        p["vote_count.gte"] = "20"
    return p


async def discover(http: aiohttp.ClientSession, key: str, kind: str, params: dict,
                   lang: str = "ru-RU") -> tuple[list[Info], int]:
    """(фильмы/сериалы страницы, всего страниц)."""
    media = "tv" if kind == "t" else "movie"
    data = await _get(http, key, f"/discover/{media}", {**params, "language": lang})
    out = []
    for c in data.get("results") or []:
        c["media_type"] = media
        if c.get("poster_path"):
            out.append(_to_info(c))
    return out, int(data.get("total_pages") or 1)


async def extras(http: aiohttp.ClientSession, key: str, info: Info, lang: str = "ru-RU") -> dict:
    """Трейлер (YouTube) и коллекция («все части») для карточки фильма/сериала.
    {"trailer": url|None, "collection": (id, name)|None}"""
    kind = "tv" if info.is_tv else "movie"
    data = await _get(http, key, f"/{kind}/{info.tmdb_id}",
                      {"language": lang, "append_to_response": "videos",
                       "include_video_language": f"{lang.split('-')[0]},en,null"})
    vids = [v for v in (data.get("videos") or {}).get("results") or []
            if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser") and v.get("key")]
    pref = lang.split("-")[0]
    vids.sort(key=lambda v: (v.get("iso_639_1") != pref, v.get("type") != "Trailer", not v.get("official")))
    col = data.get("belongs_to_collection") or None
    return {"trailer": f"https://www.youtube.com/watch?v={vids[0]['key']}" if vids else None,
            "collection": (int(col["id"]), col.get("name") or "") if col else None}


async def collection(http: aiohttp.ClientSession, key: str, cid: int, lang: str = "ru-RU") -> tuple[str, list[Info]]:
    """Все фильмы коллекции по порядку выхода (без ещё не вышедших)."""
    data = await _get(http, key, f"/collection/{cid}", {"language": lang})
    parts = []
    for c in data.get("parts") or []:
        c["media_type"] = "movie"
        if c.get("release_date"):
            parts.append(c)
    parts.sort(key=lambda c: c["release_date"])
    return data.get("name") or "", [_to_info(c) for c in parts]
