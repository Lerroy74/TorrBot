import os
import time
from datetime import datetime, timedelta

from aiohttp import web

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")

from bot import cleanup, kodi, main, tmdb  # noqa: E402
from bot.config import load  # noqa: E402
from bot.db import DB  # noqa: E402

K = "smb://192.168.1.30/media"


def info(title, year, orig="", tv=False):
    return tmdb.Info(1, tv, title, orig, year, "", 7.0, "http://p")


# ---------- папки ----------
def test_folder_name():
    assert tmdb.folder_name(info("Иван Васильевич меняет всё!", "2023")) == "Иван Васильевич меняет всё! (2023)"
    assert tmdb.folder_name(info("Звёздные войны: Эпизод 4 / Новая надежда", "1977")) == \
        "Звёздные войны Эпизод 4 Новая надежда (1977)"
    assert tmdb.folder_name(info("Без года", "")) == "Без года"
    assert tmdb.safe_folder("Во все тяжкие: сезон 1?") == "Во все тяжкие сезон 1"
    assert tmdb.safe_folder(" / ") is None


def test_matches():
    m = info("Матрица", "1999", "The Matrix")
    assert tmdb.matches(m, "Матрица / The Matrix (1999) BDRip 1080p", False)
    assert tmdb.matches(m, "Matrix.1999.x264.HDTV.mkv", False)          # без артикля
    assert tmdb.matches(m, "Матрица. Трилогия (1999-2003) HybridRip", False)
    assert not tmdb.matches(m, "Матрица: Воскрешение (2021) WEB-DL", False)  # чужой год
    assert not tmdb.matches(m, "Шматрица (2004) DVDRip", False)          # слово целиком
    assert not tmdb.matches(m, "Матрица (1999)", True)                    # сериал ≠ фильм
    iv = info("Иван Васильевич меняет всё!", "2023")
    assert tmdb.matches(iv, "Иван Васильевич меняет всё WEB-DL 1080p.mkv", False)  # года нет — ок
    bb = info("Во все тяжкие", "2008", "Breaking Bad", tv=True)
    assert tmdb.matches(bb, "Во все тяжкие / Breaking Bad / Сезон 5 (2012) WEB-DL", True)


# ---------- решение «просмотрено» ----------
def ep(path, pc=0, last="", pos=0):
    return {"file": f"{K}{path}", "playcount": pc, "lastplayed": last, "resume": {"position": pos}}


def test_to_kodi_path():
    assert cleanup.to_kodi_path("/downloads/movies/X.mkv", "/downloads", K) == f"{K}/movies/X.mkv"
    assert cleanup.to_kodi_path("/elsewhere/X.mkv", "/downloads", K) is None


def test_judge():
    items = [
        ep("/movies/Matrix.1999.mkv", 1, "2026-09-01 21:00:00"),
        ep("/movies/Matrix.1999 2.mkv", 0),                                # похожее имя — чужое
        ep("/series/BB (2008)/S05/S05E01.mkv", 1, "2026-09-02 20:00:00"),
        ep("/series/BB (2008)/S05/S05E02.mkv", 1, "2026-09-03 20:00:00"),
        ep("/series/X (2020)/S01/S01E01.mkv", 1, "2026-09-03 20:00:00"),
        ep("/series/X (2020)/S01/S01E02.mkv", 0),
        ep("/movies/Dune (2021)/Dune.mkv", 0, "", pos=1200),
        {"file": f"stack://{K}/movies/Old/cd1.avi , {K}/movies/Old/cd2.avi", "playcount": 2,
         "lastplayed": "2026-08-01 10:00:00"},
    ]
    v = cleanup.judge(f"{K}/movies/Matrix.1999.mkv", items)
    assert v.status == "watched" and v.files == 1
    v = cleanup.judge(f"{K}/series/BB (2008)/S05", items)
    assert v.status == "watched" and v.files == 2 and v.last_played == datetime(2026, 9, 3, 20)
    assert cleanup.judge(f"{K}/series/X (2020)/S01", items).status == "partial"
    assert cleanup.judge(f"{K}/movies/Dune (2021)", items).status == "partial"
    assert cleanup.judge(f"{K}/movies/Old", items).status == "watched"
    assert cleanup.judge(f"{K}/movies/Nope", items).status == "not_in_library"
    v = cleanup.judge(f"{K}/movies/Matrix.1999.mkv", items)
    assert v.ready(datetime(2026, 9, 15, 21, 0), 14)
    assert not v.ready(datetime(2026, 9, 15, 20, 59), 14)


# ---------- полный цикл на подставных Kodi и Transmission ----------
class FakeTr:
    def __init__(self, torrents):
        self.torrents = torrents
        self.removed = []

    async def get(self, hashes=None):
        return [t for t in self.torrents if hashes is None or t["hashString"] in hashes]

    async def remove(self, h, delete_data=True):
        self.removed.append((h, delete_data))
        self.torrents = [t for t in self.torrents if t["hashString"] != h]


class FakeKodi:
    def __init__(self, items):
        self.items, self.cleaned = items, 0

    async def videos(self):
        return self.items

    async def clean(self):
        self.cleaned += 1


class FakeBot:
    def __init__(self):
        self.msgs = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.msgs.append((text, reply_markup))


async def test_cleanup_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANUP_DAYS", "14")
    monkeypatch.setenv("CLEANUP_WARN_HOURS", "24")
    cfg = load()
    db = DB(str(tmp_path / "db.sqlite3"))
    old = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    tr = FakeTr([
        {"hashString": "a", "name": "Old.mkv", "downloadDir": "/downloads/movies", "totalSize": 5},
        {"hashString": "b", "name": "Fresh.mkv", "downloadDir": "/downloads/movies", "totalSize": 5},
        {"hashString": "c", "name": "Kept.mkv", "downloadDir": "/downloads/movies", "totalSize": 5},
        {"hashString": "d", "name": "S01", "downloadDir": "/downloads/series/X (2020)", "totalSize": 5},
    ])
    kd = FakeKodi([ep("/movies/Old.mkv", 1, old), ep("/movies/Fresh.mkv", 1, fresh),
                   ep("/movies/Kept.mkv", 1, old),
                   ep("/series/X (2020)/S01/e1.mkv", 1, old), ep("/series/X (2020)/S01/e2.mkv", 0)])
    for h in "abcd":
        db.add_download(h, h, h, "movies", 1, 1)
        db.mark_done(h)
    db.add_download("gone", "g", "g", "movies", 1, 1)   # удалён руками в Transmission
    db.mark_done("gone")
    db.set_keep("c")

    st = main.State(cfg, db, tr, None)
    st.kodi = kd
    bot = FakeBot()

    await main.cleanup_once(bot, st)              # 1-й проход: только предупреждение по «a»
    assert tr.removed == [] and len(bot.msgs) == 1
    assert "Old.mkv" in bot.msgs[0][0] and bot.msgs[0][1].inline_keyboard[0][0].callback_data == "keep:a"
    assert db.get("gone")["removed"] == 1

    await main.cleanup_once(bot, st)              # сутки не прошли — ничего
    assert tr.removed == [] and len(bot.msgs) == 1

    db.set_warned("a", int(time.time()) - 25 * 3600)
    await main.cleanup_once(bot, st)              # прошло 25 ч — удаляем
    assert tr.removed == [("a", True)] and db.get("a")["removed"] == 1
    assert "Удалил" in bot.msgs[-1][0] and kd.cleaned == 1

    report = await main.cleanup_report(st)
    assert "Fresh.mkv" in report and "удалю после" in report
    assert "оставлено навсегда" in report and "просмотрено 1 из 2" in report


async def test_cleanup_skips_when_kodi_down(tmp_path):
    class DownKodi:
        async def videos(self):
            raise kodi.KodiError("Kodi недоступен")
    db = DB(str(tmp_path / "db.sqlite3"))
    db.add_download("a", "a", "a", "movies", 1, 1)
    db.mark_done("a")
    tr = FakeTr([{"hashString": "a", "name": "A.mkv", "downloadDir": "/downloads/movies"}])
    st = main.State(load(), db, tr, None)
    st.kodi = DownKodi()
    try:
        await main.cleanup_once(FakeBot(), st)
        raise AssertionError("должно было упасть")
    except kodi.KodiError:
        pass
    assert tr.removed == []
    assert "Не получилось спросить Kodi" in await main.cleanup_report(st)


# ---------- клиент Kodi ----------
async def test_kodi_client(aiohttp_client):
    seen = []

    async def rpc(request):
        if request.headers.get("Authorization") is None:
            return web.Response(status=401)
        body = await request.json()
        seen.append((body["method"], body["params"]))
        res = {"VideoLibrary.GetMovies": {"movies": [{"file": "m"}]},
               "VideoLibrary.GetEpisodes": {"episodes": [{"file": "e"}]}}.get(body["method"], "OK")
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": res})

    app = web.Application()
    app.router.add_post("/jsonrpc", rpc)
    client = await aiohttp_client(app)
    k = kodi.Kodi(client.session, str(client.make_url("/jsonrpc")), "kodi", "pw")
    await k.scan()
    assert seen[0] == ("VideoLibrary.Scan", {"showdialogs": False})
    assert [v["file"] for v in await k.videos()] == ["m", "e"]

    bad = kodi.Kodi(client.session, str(client.make_url("/jsonrpc")), None, None)
    try:
        await bad.scan()
        raise AssertionError
    except kodi.KodiError as e:
        assert "логин" in str(e)


async def test_cleanup_asks_owner_rating(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANUP_DAYS", "14")
    cfg = load()
    db = DB(str(tmp_path / "db.sqlite3"))
    old = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
    tr = FakeTr([{"hashString": "a", "name": "Old.mkv", "downloadDir": "/downloads/movies", "totalSize": 5}])
    db.allow(100, "Вася")
    db.add_download("a", "a", "a", "movies", 555, 100)
    db.mark_done("a")
    jid = db.journal_note("movies", "Старый фильм (1990)", None, 100, h="a")
    db.set_warned("a", int(time.time()) - 30 * 3600)
    st = main.State(cfg, db, tr, None)
    st.kodi = FakeKodi([ep("/movies/Old.mkv", 1, old)])
    bot = FakeBot()
    await main.cleanup_once(bot, st)
    assert tr.removed == [("a", True)]
    assert "Как вам" in bot.msgs[-1][0] and "Старый фильм (1990)" in bot.msgs[-1][0]
    assert db.journal_get(jid)["deleted_at"]


async def test_rating_after_watching_on_tv(tmp_path, monkeypatch):
    """v6.5: досмотрели на ТВ — спросить того, кто качал, один раз; первый проход — без рассылки."""
    monkeypatch.setenv("CLEANUP_DAYS", "0")
    cfg = load()
    db = DB(str(tmp_path / "db.sqlite3"))
    db.allow(100, "Вася")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tr = FakeTr([{"hashString": "a", "name": "Old.mkv", "downloadDir": "/downloads/movies", "totalSize": 5},
                 {"hashString": "b", "name": "New.mkv", "downloadDir": "/downloads/movies", "totalSize": 5}])
    for h, label in (("a", "Старый (1990)"), ("b", "Новый (2024)")):
        db.add_download(h, h, h, "movies", 555, 100)
        db.mark_done(h)
        db.journal_note("movies", label, None, 100, h=h)
    st = main.State(cfg, db, tr, None)
    st.kodi = FakeKodi([ep("/movies/Old.mkv", 1, now), ep("/movies/New.mkv", 0)])
    bot = FakeBot()
    assert await main.rating_watch_once(bot, st) == 0 and not bot.msgs     # первый проход: старое не трогаем
    st.kodi.items[1] = ep("/movies/New.mkv", 1, now)                       # досмотрели «Новый»
    assert await main.rating_watch_once(bot, st) == 1
    assert "Как вам" in bot.msgs[-1][0] and "Новый (2024)" in bot.msgs[-1][0]
    assert await main.rating_watch_once(bot, st) == 0                      # второй раз не спрашиваем
