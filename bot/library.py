"""Удаление скачанного вручную (/delete): что лежит в movies/ и series/, чьё оно
и как это безопасно удалить.

Здесь только работа с диском и решения — без Telegram, чтобы легко тестировать.
Правила безопасности:
  * удаляется только то, что внутри папок фильмов и сериалов (не сами эти папки);
  * путь проверяется после раскрытия ссылок, символьная ссылка удаляется как ссылка;
  * если удаляемое — часть большей раздачи (раздача шире папки), удалять не даём:
    Transmission потерял бы файлы и начал бы ругаться.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

VIDEO_EXT = {".mkv", ".avi", ".mp4", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm", ".flv"}


@dataclass
class Entry:
    kind: str                     # "movies" | "series"
    path: str                     # полный путь (как его видит бот и Transmission)
    name: str                     # имя файла или папки
    size: int = 0
    is_dir: bool = False
    seasons: list["Entry"] = field(default_factory=list)   # подпапки сериала

    @property
    def label(self) -> str:
        n = self.name
        if not self.is_dir and os.path.splitext(n)[1].lower() in VIDEO_EXT:
            n = os.path.splitext(n)[0]
        return n


def tree_size(path: str) -> int:
    """Размер файла или папки целиком; ссылки не раскрываем."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _listdir(path: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(path) if not n.startswith("."))
    except OSError:
        return []


def scan(dir_movies: str, dir_series: str) -> list[Entry]:
    """Всё, что лежит на верхнем уровне папок фильмов и сериалов, от больших к маленьким.
    У сериалов — ещё и подпапки (обычно это сезоны или отдельные раздачи)."""
    out: list[Entry] = []
    for kind, base in (("movies", dir_movies), ("series", dir_series)):
        base = base.rstrip("/")
        for n in _listdir(base):
            p = f"{base}/{n}"
            is_dir = os.path.isdir(p) and not os.path.islink(p)
            e = Entry(kind, p, n, tree_size(p), is_dir)
            if kind == "series" and is_dir:
                e.seasons = [Entry(kind, f"{p}/{s}", s, tree_size(f"{p}/{s}"),
                                   os.path.isdir(f"{p}/{s}") and not os.path.islink(f"{p}/{s}"))
                             for s in _listdir(p)]
                e.seasons = [s for s in e.seasons if s.is_dir]       # файлы в корне сериала — не «сезоны»
                e.seasons.sort(key=lambda s: s.name)
            out.append(e)
    out.sort(key=lambda e: -e.size)
    return out


def torrent_root(t: dict) -> str:
    return f"{(t.get('downloadDir') or '').rstrip('/')}/{t.get('name') or ''}"


def _inside(p: str, base: str) -> bool:
    return p == base or p.startswith(base.rstrip("/") + "/")


def torrents_for(path: str, torrents: list[dict]) -> tuple[list[dict], list[dict]]:
    """(раздачи целиком внутри path — их снимаем вместе с файлами,
        раздачи шире path — из-за них удалять нельзя)."""
    inner, outer = [], []
    for t in torrents:
        root = torrent_root(t)
        if not t.get("name"):
            continue
        if _inside(root, path):
            inner.append(t)
        elif _inside(path, root):
            outer.append(t)
    return inner, outer


def safe_target(path: str, dir_movies: str, dir_series: str) -> bool:
    """Путь строго внутри папки фильмов или сериалов (после раскрытия ссылок у родителя)."""
    parent = os.path.realpath(os.path.dirname(path.rstrip("/")))
    name = os.path.basename(path.rstrip("/"))
    if not name or name in (".", ".."):
        return False
    for base in (dir_movies, dir_series):
        b = os.path.realpath(base.rstrip("/"))
        if parent == b or parent.startswith(b + "/"):
            return True
    return False


def remove_path(path: str) -> None:
    """Удалить файл, ссылку или папку. Чего уже нет — не ошибка."""
    if os.path.islink(path) or (os.path.exists(path) and not os.path.isdir(path)):
        os.unlink(path)
    elif os.path.isdir(path):
        def gone_ok(func, p, exc_info):       # Transmission мог успеть удалить файл сам
            if not issubclass(exc_info[0], FileNotFoundError):
                raise exc_info[1]
        shutil.rmtree(path, onerror=gone_ok)


def prune_empty(path: str, stop: tuple[str, ...]) -> None:
    """После удаления сезона: если папка сериала опустела — убрать и её (но не movies/series)."""
    stops = {os.path.realpath(s.rstrip("/")) for s in stop}
    d = os.path.dirname(path.rstrip("/"))
    while d and os.path.realpath(d) not in stops:
        try:
            os.rmdir(d)                   # не пустая — OSError, на этом и останавливаемся
        except OSError:
            return
        d = os.path.dirname(d)
