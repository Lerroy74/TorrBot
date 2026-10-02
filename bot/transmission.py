"""Минимальный клиент Transmission RPC."""
from __future__ import annotations

import aiohttp


class TransmissionError(Exception):
    pass


class Transmission:
    def __init__(self, http: aiohttp.ClientSession, url: str, user: str | None, password: str | None):
        self.http = http
        self.url = url
        self.auth = aiohttp.BasicAuth(user, password or "") if user else None
        self.sid = ""

    async def call(self, method: str, **arguments) -> dict:
        body = {"method": method, "arguments": arguments}
        for _ in range(2):
            async with self.http.post(
                self.url, json=body, auth=self.auth,
                headers={"X-Transmission-Session-Id": self.sid},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 409:  # Transmission выдаёт новый session id
                    self.sid = resp.headers.get("X-Transmission-Session-Id", "")
                    continue
                if resp.status == 401:
                    raise TransmissionError("неверный логин/пароль Transmission")
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                if data.get("result") != "success":
                    raise TransmissionError(data.get("result", "unknown error"))
                return data.get("arguments") or {}
        raise TransmissionError("не удалось получить session id")

    async def add(self, magnet: str, download_dir: str) -> tuple[str, str, bool]:
        """Возвращает (hash, name, уже_был)."""
        args = await self.call("torrent-add", filename=magnet, **{"download-dir": download_dir})
        if "torrent-duplicate" in args:
            t = args["torrent-duplicate"]
            return t["hashString"].lower(), t.get("name", ""), True
        t = args["torrent-added"]
        return t["hashString"].lower(), t.get("name", ""), False

    async def get(self, hashes: list[str] | None = None) -> list[dict]:
        fields = ["hashString", "name", "percentDone", "status", "error", "errorString",
                  "totalSize", "eta", "rateDownload", "peersSendingToUs", "metadataPercentComplete",
                  "downloadDir", "queuePosition", "doneDate", "addedDate", "bandwidthPriority",
                  "leftUntilDone", "sizeWhenDone", "haveValid", "rateUpload"]
        kw = {"fields": fields}
        if hashes is not None:
            if not hashes:
                return []
            kw["ids"] = hashes
        args = await self.call("torrent-get", **kw)
        return args.get("torrents") or []

    async def stop(self, h: str) -> None:
        await self.call("torrent-stop", ids=[h])

    async def start(self, h: str) -> None:
        await self.call("torrent-start", ids=[h])

    async def start_now(self, h: str) -> None:
        """«Качать первой»: первой в очереди, вне очереди и с высоким приоритетом скорости.
        Такая закачка бывает одна — у остальных приоритет возвращаем к обычному."""
        await self.call("torrent-set", bandwidthPriority=0)            # без ids — всем закачкам
        await self.call("torrent-set", ids=[h], bandwidthPriority=1)
        await self.call("queue-move-top", ids=[h])
        await self.call("torrent-start-now", ids=[h])

    async def files(self, h: str) -> tuple[list[dict], list[bool], float]:
        """(файлы [{name, length}], какие выбраны, готовность метаданных 0..1)."""
        ts = (await self.call("torrent-get", ids=[h], fields=["files", "wanted", "metadataPercentComplete"])
              ).get("torrents") or []
        if not ts:
            return [], [], 0.0
        t = ts[0]
        return t.get("files") or [], [bool(w) for w in t.get("wanted") or []], float(t.get("metadataPercentComplete", 1))

    async def set_wanted(self, h: str, wanted: list[int], unwanted: list[int]) -> None:
        args = {"ids": [h]}
        if wanted:
            args["files-wanted"] = wanted
        if unwanted:
            args["files-unwanted"] = unwanted
        await self.call("torrent-set", **args)

    async def session_set(self, **kw) -> None:
        await self.call("session-set", **kw)

    async def session_get(self, fields: list[str] | None = None) -> dict:
        return await self.call("session-get", **({"fields": fields} if fields else {}))

    async def stats(self) -> dict:
        """Общая скорость: downloadSpeed / uploadSpeed (байт/с), activeTorrentCount…"""
        return await self.call("session-stats")

    async def turtle(self, on: bool, down_kb: int | None = None, up_kb: int | None = None) -> None:
        """«Черепаха» (альтернативная скорость Transmission): включить/выключить и задать лимиты, кБ/с."""
        kw: dict = {"alt-speed-enabled": bool(on), "alt-speed-time-enabled": False}
        if down_kb is not None:
            kw["alt-speed-down"] = int(down_kb)
        if up_kb is not None:
            kw["alt-speed-up"] = int(up_kb)
        await self.call("session-set", **kw)

    async def remove(self, h: str, delete_data: bool = True) -> None:
        await self.call("torrent-remove", ids=[h], **{"delete-local-data": delete_data})

    async def free_space(self, path: str) -> int | None:
        try:
            args = await self.call("free-space", path=path)
            return int(args.get("size-bytes"))
        except Exception:
            return None
