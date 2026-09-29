"""Kodi JSON-RPC: обновить медиатеку и узнать, что уже просмотрено.

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

    async def videos(self) -> list[dict]:
        """Все фильмы и серии медиатеки: file, playcount, lastplayed, resume."""
        props = ["file", "playcount", "lastplayed", "resume"]
        movies = (await self.call("VideoLibrary.GetMovies", {"properties": props}) or {}).get("movies") or []
        eps = (await self.call("VideoLibrary.GetEpisodes", {"properties": props}) or {}).get("episodes") or []
        return movies + eps
