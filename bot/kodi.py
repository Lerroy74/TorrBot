"""Kodi JSON-RPC: обновить медиатеку, узнать, что просмотрено, пульт и сообщения на экране.

В Kodi: Настройки → Службы → Управление → «Разрешить удалённое управление по HTTP»."""
from __future__ import annotations

import aiohttp


class KodiError(Exception):
    pass


class Kodi:
    def __init__(self, http: aiohttp.ClientSession, url: str, user: str | None, password: str | None):
        self.http = http
        self.url = url
        self.auth = aiohttp.BasicAuth(user or "kodi", password or "") if (user or password) else None

    async def call(self, method: str, params: dict | None = None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        try:
            async with self.http.post(self.url, json=body, auth=self.auth,
                                      timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 401:
                    raise KodiError("неверный логин/пароль Kodi")
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except KodiError:
            raise
        except Exception as e:
            raise KodiError(f"Kodi недоступен: {e!r}") from e
        if "error" in data:
            raise KodiError(str(data["error"]))
        return data.get("result")

    async def scan(self) -> None:
        await self.call("VideoLibrary.Scan", {"showdialogs": False})

    async def clean(self) -> None:
        await self.call("VideoLibrary.Clean", {"showdialogs": False})

    async def busy(self) -> bool:
        """Идёт ли сейчас обновление или чистка медиатеки. Пока идёт — новую чистку Kodi отклонит
        («CleanLibrary is not possible while scanning or cleaning»), причём молча, без ошибки в ответе."""
        r = await self.call("XBMC.GetInfoBooleans",
                            {"booleans": ["Library.IsScanningVideo", "Library.IsScanning"]}) or {}
        return any(bool(v) for v in r.values())

    async def mark_watched(self, items: list[dict], when: str) -> int:
        """Отметить фильмы/серии просмотренными (как будто досмотрели на ТВ). when — «ГГГГ-ММ-ДД ЧЧ:ММ:СС»."""
        n = 0
        for it in items:
            common = {"playcount": max(1, int(it.get("playcount") or 0)), "lastplayed": when,
                      "resume": {"position": 0, "total": 0}}
            if it.get("movieid"):
                await self.call("VideoLibrary.SetMovieDetails", {"movieid": it["movieid"], **common})
            elif it.get("episodeid"):
                await self.call("VideoLibrary.SetEpisodeDetails", {"episodeid": it["episodeid"], **common})
            else:
                continue
            n += 1
        return n

    async def videos(self) -> list[dict]:
        """Все фильмы и серии медиатеки: file, playcount, lastplayed, resume (у серий ещё season, episode)."""
        props = ["file", "playcount", "lastplayed", "resume"]
        movies = (await self.call("VideoLibrary.GetMovies", {"properties": props}) or {}).get("movies") or []
        eps = (await self.call("VideoLibrary.GetEpisodes", {"properties": props + ["season", "episode"]})
               or {}).get("episodes") or []
        return movies + eps

    # ---------- v7: сообщение на экране и пульт ----------
    async def notify(self, title: str, message: str, ms: int = 10000) -> None:
        await self.call("GUI.ShowNotification", {"title": title[:60], "message": message[:200],
                                                 "displaytime": ms})

    async def player(self) -> int | None:
        """id активного видеоплеера (или любого, если видео нет); None — ничего не играет."""
        players = await self.call("Player.GetActivePlayers") or []
        for p in players:
            if p.get("type") == "video":
                return p.get("playerid")
        return players[0].get("playerid") if players else None

    async def now_playing(self) -> dict | None:
        """{"title", "time", "total", "paused", "percent", "playerid"} или None."""
        pid = await self.player()
        if pid is None:
            return None
        item = ((await self.call("Player.GetItem", {"playerid": pid, "properties":
                                                    ["title", "showtitle", "season", "episode", "file"]})
                 or {}).get("item") or {})
        props = await self.call("Player.GetProperties", {"playerid": pid, "properties":
                                                         ["time", "totaltime", "speed", "percentage"]}) or {}
        title = item.get("title") or item.get("label") or ""
        if item.get("showtitle"):
            se = (f" · {item['season']}×{item['episode']:02d}"
                  if (item.get("season") or 0) >= 0 and (item.get("episode") or 0) > 0 else "")
            title = f"{item['showtitle']}{se} {title}".strip()
        return {"playerid": pid, "title": title or (item.get("file") or "").rsplit("/", 1)[-1],
                "file": item.get("file") or "", "time": _secs(props.get("time")),
                "total": _secs(props.get("totaltime")), "paused": props.get("speed") == 0,
                "percent": float(props.get("percentage") or 0)}

    async def play_pause(self) -> None:
        pid = await self.player()
        if pid is not None:
            await self.call("Player.PlayPause", {"playerid": pid})

    async def stop(self) -> None:
        pid = await self.player()
        if pid is not None:
            await self.call("Player.Stop", {"playerid": pid})

    async def seek(self, seconds: int) -> None:
        pid = await self.player()
        if pid is not None:
            await self.call("Player.Seek", {"playerid": pid, "value": {"seconds": int(seconds)}})

    async def go(self, to: str) -> None:
        """to: next | previous — следующая/предыдущая серия в списке воспроизведения."""
        pid = await self.player()
        if pid is not None:
            await self.call("Player.GoTo", {"playerid": pid, "to": to})

    async def volume(self, change: str) -> int | None:
        """change: increment | decrement | mute. Вернёт громкость (0..100)."""
        if change == "mute":
            await self.call("Application.SetMute", {"mute": "toggle"})
        else:
            await self.call("Application.SetVolume", {"volume": change})
        props = await self.call("Application.GetProperties", {"properties": ["volume", "muted"]}) or {}
        return None if props.get("muted") else props.get("volume")

    async def open(self, item: dict) -> None:
        """item: {"movieid": N} | {"episodeid": N} | {"file": url} | {"directory": url}. С места остановки."""
        await self.call("Player.Open", {"item": item, "options": {"resume": True}})


def _secs(t: dict | None) -> int:
    t = t or {}
    return int(t.get("hours") or 0) * 3600 + int(t.get("minutes") or 0) * 60 + int(t.get("seconds") or 0)


def fmt_time(sec: int) -> str:
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
