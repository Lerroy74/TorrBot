import os

from aiohttp import web

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")

from bot import main, tmdb  # noqa: E402
from bot.config import load  # noqa: E402
from bot.db import DB  # noqa: E402


def movie(i, title, date, poster="/p.jpg", **kw):
    return {"id": i, "media_type": "movie", "title": title, "original_title": kw.get("orig", title),
            "release_date": date, "poster_path": poster, "overview": kw.get("ov", ""),
            "vote_average": kw.get("vote", 7.0)}


def tv(i, name, date, poster="/t.jpg"):
    return {"id": i, "media_type": "tv", "name": name, "original_name": name,
            "first_air_date": date, "poster_path": poster, "overview": "", "vote_average": 8}


def test_split_query():
    assert tmdb.split_query("Дюна 2021") == ("Дюна", "2021")
    assert tmdb.split_query("Матрица") == ("Матрица", None)
    assert tmdb.split_query("Бегущий по лезвию 2049 (2017)") == ("Бегущий по лезвию 2049", "2017")
    assert tmdb.split_query("Шерлок: сезон 1") == ("Шерлок сезон 1", None)


def test_auth():
    assert tmdb.auth("abc123") == ({}, {"api_key": "abc123"})
    h, p = tmdb.auth("eyJhbGciOi.xxx")
    assert h == {"Authorization": "Bearer eyJhbGciOi.xxx"} and p == {}


def test_pick_order_year_type_poster():
    cands = [
        {"id": 9, "media_type": "person", "name": "Keanu"},
        movie(1, "Матрица: Воскрешение", "2021-12-16"),
        movie(2, "Матрица", "1999-03-30", orig="The Matrix"),
        movie(3, "Без постера", "1999-01-01", poster=None),
        tv(4, "Матрица", "2020-01-01"),
    ]
    # без года — первый подходящий фильм по порядку TMDB
    assert tmdb.pick(cands, None, prefer_tv=False).tmdb_id == 1
    # год важнее порядка
    info = tmdb.pick(cands, "1999", prefer_tv=False)
    assert info.tmdb_id == 2 and info.year == "1999" and info.poster == tmdb.IMG + "/p.jpg"
    # на трекерах в основном сериалы — берём сериал
    assert tmdb.pick(cands, None, prefer_tv=True).tmdb_id == 4
    assert tmdb.pick([movie(1, "x", "2000", poster=None)], None, False) is None
    assert tmdb.pick([], None, False) is None


def test_caption():
    info = tmdb._to_info(movie(2, "Матрица", "1999-03-30", orig="The Matrix", ov="слово " * 300, vote=8.2))
    cap = info.caption()
    assert cap.startswith("🎬 <b>Матрица</b> (1999) · ⭐ 8.2\n<i>The Matrix</i>")
    assert cap.endswith("…") and len(cap) < 1024
    same = tmdb._to_info(movie(1, "Dune", "2021-01-01", orig="Dune")).caption()
    assert "<i>" not in same


async def test_search_request(aiohttp_client, monkeypatch):
    seen = {}

    async def handler(request):
        seen.update(request.query)
        seen["auth"] = request.headers.get("Authorization")
        return web.json_response({"results": [movie(1, "Дюна", "2021-09-15")]})

    app = web.Application()
    app.router.add_get("/3/search/multi", handler)
    client = await aiohttp_client(app)
    monkeypatch.setattr(tmdb, "API", str(client.make_url("/3")))
    res = await tmdb.search(client.session, "KEY", "Дюна 2021")
    assert res[0]["title"] == "Дюна"
    assert seen["query"] == "Дюна" and seen["api_key"] == "KEY" and seen["language"] == "ru-RU"
    assert seen["auth"] is None


class FakeBot:
    def __init__(self, photo_fails=0):
        self.photo_fails = photo_fails
        self.sent = []

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        if self.photo_fails:
            self.photo_fails -= 1
            raise RuntimeError("wrong file identifier/HTTP URL specified")
        self.sent.append(("photo", photo, caption))

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(("text", None, text))


async def test_send_with_poster_fallbacks(aiohttp_client, tmp_path):
    async def img(request):
        return web.Response(body=b"JPEGDATA", content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/p.jpg", img)
    client = await aiohttp_client(app)
    st = main.State(load(), DB(str(tmp_path / "db.sqlite3")), None, client.session)
    url = str(client.make_url("/p.jpg"))

    b = FakeBot()
    await main.send_with_poster(b, st, 1, "hi", url)
    assert b.sent == [("photo", url, "hi")]

    b = FakeBot(photo_fails=1)          # Telegram не смог забрать по ссылке — грузим файлом
    await main.send_with_poster(b, st, 1, "hi", url)
    assert b.sent[0][0] == "photo" and b.sent[0][1].data == b"JPEGDATA"

    b = FakeBot(photo_fails=2)          # совсем не вышло — просто текст
    await main.send_with_poster(b, st, 1, "hi", url)
    assert b.sent == [("text", None, "hi")]

    b = FakeBot()
    await main.send_with_poster(b, st, 1, "hi", None)
    assert b.sent == [("text", None, "hi")]


def test_db_migration_adds_poster(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite3")
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE downloads (hash TEXT PRIMARY KEY, name TEXT, title TEXT, category TEXT,
                       chat_id INTEGER, user_id INTEGER, added_at INTEGER, done_at INTEGER,
                       removed INTEGER DEFAULT 0);
                       INSERT INTO downloads(hash,name) VALUES ('old','старый');""")
    c.commit()
    c.close()
    db = DB(path)
    db.add_download("new", "n", "t", "movies", 1, 1, "http://poster")
    rows = {r["hash"]: r for r in db.pending()}
    assert rows["old"]["poster"] is None and rows["new"]["poster"] == "http://poster"
