import os

import pytest
from aiohttp import web

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")

from bot import jacred  # noqa: E402
from bot.config import load  # noqa: E402
from bot.transmission import Transmission  # noqa: E402

CFG = load()
H = "0e6c99417fd5a446ba8f7b9e17dab326553d23ba"


def item(title, size_gb=5.0, seeds=50, ffprobe=None, info=None, cat=(2000,), h=H):
    return {"Tracker": "rutracker", "Title": title, "Size": int(size_gb * 1024 ** 3),
            "Seeders": seeds, "Peers": 3, "Category": list(cat),
            "MagnetUri": f"magnet:?xt=urn:btih:{h}&dn=x", "ffprobe": ffprobe or [], "info": info or {}}


def vid(codec, w, h):
    return [{"codec_type": "video", "codec_name": codec, "width": w, "height": h}]


def test_real_jacred_sample():
    # фрагмент реального ответа jac.red из лога
    it = item("Шматрица / Matrix (2004) DVDRip", 1.2, seeds=1, ffprobe=vid("msmpeg4v3", 704, 288),
              info={"quality": 480, "videotype": "sdr", "types": ["movie"]})
    r = jacred.parse(it)
    assert r.height == 288 and r.codec == "msmpeg4v3" and not r.is_series
    assert not jacred.playable(r, CFG)  # 1 сид < MIN_SEEDERS


@pytest.mark.parametrize("title,ff,ok", [
    ("Дюна / Dune (2021) BDRip 1080p x264", vid("h264", 1920, 800), True),
    ("Дюна / Dune (2021) 2160p HDR10 HEVC", vid("hevc", 3840, 1600), False),
    ("Дюна / Dune (2021) WEB-DL 1080p HEVC", [], False),
    ("Дюна / Dune (2021) WEB-DL 1080p 10bit", [], False),
    ("Дюна / Dune (2021) HDRip AVC", [], True),        # HDRip — это не HDR
    ("Дюна / Dune (2021) DVDRip", [], True),           # DVD не путается с DV
    ("Дюна / Dune (2021) 1080p DV HDR", [], False),
    ("Дюна / Dune (2021) BDRemux 1080p AVC", [], False),
])
def test_filters(title, ff, ok):
    assert jacred.playable(jacred.parse(item(title, ffprobe=ff)), CFG) is ok


def test_size_limits_and_series():
    big_movie = jacred.parse(item("Фильм 1080p x264", size_gb=30))
    assert not jacred.playable(big_movie, CFG)
    season = jacred.parse(item("Сериал / Show [S01] 1080p x264", size_gb=45, info={"types": ["serial"]}))
    assert season.is_series and jacred.playable(season, CFG)
    by_cat = jacred.parse(item("Show S02 720p", cat=(5000,)))
    assert by_cat.is_series


def test_select_dedup_and_rank():
    items = [
        item("A 720p x264", seeds=500, h="a" * 40),
        item("B 1080p x264", seeds=20, h="b" * 40),
        item("B dup 1080p x264", seeds=90, h="b" * 40),
        item("C 2160p HEVC", seeds=999, h="c" * 40),
        {"Title": "без магнета"},
    ]
    res, total = jacred.select(items, CFG)
    assert total == 4
    assert [r.infohash[0] for r in res] == ["b", "a"]
    assert res[0].seeders == 90


async def test_transmission_session_dance(aiohttp_client):
    calls = []

    async def rpc(request):
        if request.headers.get("X-Transmission-Session-Id") != "SID":
            return web.Response(status=409, headers={"X-Transmission-Session-Id": "SID"})
        body = await request.json()
        calls.append(body["method"])
        if body["method"] == "torrent-add":
            assert body["arguments"]["download-dir"] == "/downloads/movies"
            return web.json_response({"result": "success", "arguments": {
                "torrent-added": {"hashString": H.upper(), "name": "Matrix", "id": 1}}})
        return web.json_response({"result": "success", "arguments": {"torrents": [
            {"hashString": H, "name": "Matrix", "percentDone": 1.0}]}})

    app = web.Application()
    app.router.add_post("/transmission/rpc", rpc)
    client = await aiohttp_client(app)
    tr = Transmission(client.session, str(client.make_url("/transmission/rpc")), None, None)
    h, name, dup = await tr.add("magnet:?xt=urn:btih:" + H, "/downloads/movies")
    assert (h, name, dup) == (H, "Matrix", False)
    ts = await tr.get([H])
    assert ts[0]["percentDone"] == 1.0
    assert calls == ["torrent-add", "torrent-get"]
