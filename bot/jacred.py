"""Поиск раздач через jac.red (Jackett-совместимый агрегатор rutracker, rutor, kinozal...)
и отбор тех, что Raspberry Pi 3 сможет воспроизвести."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import aiohttp

from .config import Config

SERIES_TYPES = {"serial", "multserial", "docuserial", "tvshow"}

_RE_HEVC = re.compile(r"\b(hevc|h\.?265|x\.?265)\b", re.I)
_RE_AVC = re.compile(r"\b(avc|h\.?264|x\.?264)\b", re.I)
_RE_AV1 = re.compile(r"\bav1\b", re.I)
_RE_HDR = re.compile(r"\b(hdr(10\+?)?|dolby\s*vision|dovi|dv)\b", re.I)
_RE_HEIGHT = [
    (re.compile(r"\b(2160p|4k|uhd)\b", re.I), 2160),
    (re.compile(r"\b1440p\b", re.I), 1440),
    (re.compile(r"\b1080[pi]\b", re.I), 1080),
    (re.compile(r"\b720p\b", re.I), 720),
    (re.compile(r"\b(480p|dvdrip|satrip|tvrip)\b", re.I), 480),
]
_RE_HASH = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")


@dataclass
class Release:
    title: str
    tracker: str
    size: int
    seeders: int
    peers: int
    magnet: str
    infohash: str
    height: int | None = None
    codec: str | None = None
    hdr: bool = False
    is_series: bool = False
    voices: list[str] = field(default_factory=list)
    details: str = ""
    fav: bool = False          # есть любимая озвучка пользователя

    @property
    def size_gb(self) -> float:
        return self.size / 1024 ** 3

    def short_line(self) -> str:
        parts = []
        parts.append(f"{self.height}p" if self.height else "?p")
        if self.codec:
            parts.append({"h264": "x264", "hevc": "HEVC"}.get(self.codec, self.codec))
        parts.append(f"{self.size_gb:.1f} ГБ")
        parts.append(f"👤{self.seeders}")
        parts.append(self.tracker)
        return " · ".join(parts)


def _codec_from(item: dict, title: str) -> str | None:
    for s in item.get("ffprobe") or []:
        if s.get("codec_type") == "video" and s.get("codec_name"):
            name = s["codec_name"].lower()
            return {"h265": "hevc", "avc": "h264"}.get(name, name)
    if _RE_HEVC.search(title):
        return "hevc"
    if _RE_AV1.search(title):
        return "av1"
    if _RE_AVC.search(title):
        return "h264"
    return None


def _height_from(item: dict, title: str) -> int | None:
    for s in item.get("ffprobe") or []:
        if s.get("codec_type") == "video" and s.get("height"):
            h, w = int(s["height"]), int(s.get("width") or 0)
            # широкоэкранные 1920x800 — это всё равно 1080p
            if w >= 3000:
                return 2160
            if w >= 1800:
                return 1080
            if w >= 1200:
                return 720
            return h
    q = (item.get("info") or {}).get("quality")
    if isinstance(q, int) and q > 0:
        return q
    for rx, h in _RE_HEIGHT:
        if rx.search(title):
            return h
    return None


def parse(item: dict) -> Release | None:
    magnet = item.get("MagnetUri") or ""
    m = _RE_HASH.search(magnet)
    if not m:
        return None
    title = (item.get("Title") or "").strip()
    info = item.get("info") or {}
    types = set(info.get("types") or [])
    cats = item.get("Category") or []
    is_series = bool(types & SERIES_TYPES) or any(5000 <= int(c) < 6000 for c in cats if str(c).isdigit())
    hdr = str(info.get("videotype", "")).lower() in ("hdr", "dv", "hdr10", "dolbyvision") or bool(_RE_HDR.search(title))
    return Release(
        title=title,
        tracker=item.get("Tracker") or "?",
        size=int(item.get("Size") or 0),
        seeders=int(item.get("Seeders") or 0),
        peers=int(item.get("Peers") or 0),
        magnet=magnet,
        infohash=m.group(1).lower(),
        height=_height_from(item, title),
        codec=_codec_from(item, title),
        hdr=hdr,
        is_series=is_series,
        voices=[v for v in (info.get("voices") or []) if isinstance(v, str)],
        details=item.get("Details") or "",
    )


def playable(r: Release, cfg: Config) -> bool:
    if r.seeders < cfg.min_seeders:
        return False
    if r.codec and r.codec in cfg.block_codecs:
        return False
    if r.height and r.height > cfg.max_height:
        return False
    if cfg.block_hdr and r.hdr:
        return False
    limit = cfg.max_size_series_gb if r.is_series else cfg.max_size_gb
    if r.size and r.size_gb > limit:
        return False
    low = f" {r.title.lower()} "
    if any(w in low for w in cfg.block_words):
        return False
    return True


def _rank(r: Release, max_height: int) -> tuple:
    if r.height == max_height:
        q = 3
    elif r.height and r.height < max_height and r.height >= 720:
        q = 2
    elif r.height is None:
        q = 1
    else:
        q = 0
    return (q, r.codec == "h264", r.seeders)


_JUNK = ("camrip", "telesync", "tsrip", " ts ", "telecine", "trailer", "трейлер")


def _best_key(r: Release) -> tuple:
    return (r.height or 0, r.seeders >= 5, r.hdr, r.seeders)


def best_any(items: list[dict], match=None, min_seeders: int = 1) -> Release | None:
    """v8.2: лучшая раздача для magnet-ссылки — без ограничений приставки (4K, HDR, remux можно),
    только отсекаем «экранки» и мёртвые раздачи. match(title, is_series) — та ли это раздача."""
    best: Release | None = None
    for it in items:
        r = parse(it)
        if not r or r.seeders < max(1, min_seeders):
            continue
        low = f" {r.title.lower()} "
        if any(w in low for w in _JUNK):
            continue
        if match and not match(r.title, r.is_series):
            continue
        if best is None or _best_key(r) > _best_key(best):
            best = r
    return best


def short_magnet(r: Release, limit: int = 256) -> str:
    """Magnet не длиннее limit (кнопка «скопировать» в Telegram держит 256 символов):
    хэш обязателен, дальше — трекеры из исходной ссылки, сколько влезет, потом имя."""
    from urllib.parse import parse_qs, quote, urlsplit
    out = f"magnet:?xt=urn:btih:{r.infohash}"
    qs = parse_qs(urlsplit(r.magnet).query)
    for tr in qs.get("tr", []):
        add = "&tr=" + quote(tr, safe=":/")
        if len(out) + len(add) <= limit:
            out += add
    name = re.sub(r"[^\w\s.\-()\[\]]", "", r.title)[:80].strip()
    for n in range(len(name), 0, -1):
        add = "&dn=" + quote(name[:n].strip())
        if len(out) + len(add) <= limit:
            return out + add
    return out


def select(items: list[dict], cfg: Config) -> tuple[list[Release], int]:
    """Возвращает (подходящие раздачи, сколько всего нашлось)."""
    best: dict[str, Release] = {}
    total = 0
    for it in items:
        r = parse(it)
        if not r:
            continue
        total += 1
        if not playable(r, cfg):
            continue
        prev = best.get(r.infohash)
        if not prev or r.seeders > prev.seeders:
            best[r.infohash] = r
    ranked = sorted(best.values(), key=lambda r: _rank(r, cfg.max_height), reverse=True)
    return ranked[: cfg.max_results], total


async def search(http: aiohttp.ClientSession, cfg: Config, query: str) -> list[dict]:
    url = f"{cfg.jacred_url}/api/v2.0/indexers/all/results"
    params = {"apikey": "null", "Query": query}
    async with http.get(url, params=params, timeout=aiohttp.ClientTimeout(total=40)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return data.get("Results") or []


_RE_TAGS = re.compile(r"\s*[\[(][^\])]*[\])]\s*")


def ru_title(title: str, limit: int = 80) -> str:
    """Короткое название из заголовка раздачи: русская часть до « / » без [тегов] и (скобок).
    «Матрица / The Matrix (Энди Вачовски…) [1999, BDRip]» → «Матрица»."""
    ru = _RE_TAGS.sub(" ", (title or "").split(" / ")[0])
    return " ".join(ru.split()).strip(" .-|")[:limit]
