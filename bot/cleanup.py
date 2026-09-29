"""Автоочистка: что из скачанного ботом уже просмотрено в Kodi и лежит дольше N дней.

Только решения, без сети — чтобы легко тестировать. Правила безопасности:
  * закачка должна быть в медиатеке Kodi (иначе не знаем, смотрели ли её);
  * просмотрены ВСЕ её файлы (для сезона — все серии), начатые не считаются;
  * с последнего просмотра прошло не меньше `days` дней.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class Verdict:
    status: str                  # "watched" | "unwatched" | "partial" | "not_in_library"
    files: int = 0
    watched: int = 0
    last_played: datetime | None = None

    def ready(self, now: datetime, days: int) -> bool:
        return (self.status == "watched" and self.last_played is not None
                and now - self.last_played >= timedelta(days=days))

    def delete_after(self, days: int) -> datetime | None:
        return self.last_played + timedelta(days=days) if self.last_played else None


def to_kodi_path(tr_path: str, media_root: str, kodi_media_url: str) -> str | None:
    """/downloads/movies/X → smb://192.168.1.30/media/movies/X"""
    root = media_root.rstrip("/")
    if tr_path != root and not tr_path.startswith(root + "/"):
        return None
    return kodi_media_url.rstrip("/") + tr_path[len(root):]


def _files_of(item: dict) -> list[str]:
    f = item.get("file") or ""
    if f.startswith("stack://"):        # фильм из нескольких частей
        return [p.strip() for p in f[len("stack://"):].split(" , ") if p.strip()]
    return [f] if f else []


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") if ts else None
    except ValueError:
        return None


def judge(root: str, items: list[dict]) -> Verdict:
    """root — путь закачки глазами Kodi (файл или папка)."""
    mine = [it for it in items
            if any(f == root or f.startswith(root.rstrip("/") + "/") for f in _files_of(it))]
    if not mine:
        return Verdict("not_in_library")
    watched = [it for it in mine if int(it.get("playcount") or 0) > 0]
    last = max((d for d in (_parse(it.get("lastplayed") or "") for it in watched) if d), default=None)
    if len(watched) == len(mine):
        return Verdict("watched", len(mine), len(watched), last)
    status = "partial" if watched or any((it.get("resume") or {}).get("position") for it in mine) else "unwatched"
    return Verdict(status, len(mine), len(watched), last)
