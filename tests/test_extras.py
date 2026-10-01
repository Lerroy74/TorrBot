"""v5: озвучки и ⚡, сезоны, трейлер/коллекции, «хотим», пауза, сторож, отчёт, бэкап, 🎲."""
import io
import os
import tarfile
import time
from datetime import datetime
from types import SimpleNamespace

from test_flows import ADMIN, ALICE, BOB, H1, buttons, env, mv, rel  # noqa: F401

from bot import extras, jacred, main, tmdb


def R(title, voices=(), seeds=50, h="a" * 40, height=1080):
    return jacred.Release(title, "rutor", 5 * 1024 ** 3, seeds, 1, "m", h, height, "h264", voices=list(voices))


# ---------- озвучки и порядок ----------
def test_voice_match_and_order(env):
    st, *_ = env
    st.db.set_pref(ALICE, "voices", "LostFilm|Дубляж")
    a = R("Сериал / Сезон 1 (2020) WEB-DL 1080p", ["NewStudio"], seeds=300, h="a" * 40)
    b = R("Сериал / Сезон 1 (2020) WEB-DL 1080p | LostFilm", [], seeds=40, h="b" * 40)
    c = R("Сериал (2020) 720p", ["LostFilm"], seeds=999, h="c" * 40, height=720)
    d = R("Фильм (2020) BDRip 1080p Dub", [], seeds=2, h="d" * 40)
    out = extras.order_for_user(st, ALICE, [a, b, c, d])
    assert [x.infohash[0] for x in out] == ["b", "d", "a", "c"]   # 1080p+любимая > 1080p > 720p
    assert out[0].fav and out[1].fav and not out[2].fav
    assert [x.infohash[0] for x in extras.order_for_user(st, BOB, [a, b])] == ["a", "b"]  # без предпочтений


async def test_voices_command_and_best_button(env):
    st, session, send, press, mp = env
    await send(ALICE, "/voices")
    kb = session.sent(ALICE)[-1][2]
    lost = next(b for b in buttons(kb) if b.text == "LostFilm")
    await press(ALICE, lost.callback_data)
    assert st.db.pref(ALICE, "voices") == "LostFilm"
    assert any(b.text == "✅ LostFilm" for b in buttons(session.sent(ALICE)[-1][2]))

    async def fake_find(st_, q):
        return [mv(9, "Дюна", "2021-09-15", orig="Dune")]
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel("Дюна / Dune (2021) WEB-DL 1080p", "1" * 40),
                rel("Дюна / Dune (2021) WEB-DL 1080p LostFilm", "2" * 40)]
    mp.setattr(jacred, "search", fake_jac)

    async def no_extras(*a, **k):
        return {}
    mp.setattr(tmdb, "extras", no_extras)
    await send(ALICE, "дюна")
    _, text, kb = session.sent(ALICE)[-1]
    assert buttons(kb)[0].text.startswith("⚡") and "<b>1.</b> ⭐" in text
    await press(ALICE, buttons(kb)[0].callback_data)
    assert "2" * 40 in st.tr.torrents                       # ⚡ = раздача с LostFilm


# ---------- кнопки под обложкой ----------
async def test_poster_buttons_trailer_collection_wish(env):
    st, session, send, press, mp = env

    async def fake_find(st_, q):
        return [mv(105, "Назад в будущее", "1985-07-03", orig="Back to the Future")]
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel(f"{q} (1985) BDRip 1080p", "1" * 40)] if "1985" not in q else []
    mp.setattr(jacred, "search", fake_jac)

    async def fake_extras(http, key, info, lang="ru-RU"):
        return {"trailer": "https://www.youtube.com/watch?v=x", "collection": (264, "Назад в будущее (коллекция)")}
    mp.setattr(tmdb, "extras", fake_extras)
    await send(ALICE, "назад в будущее")
    poster = next(m for m in session.calls if type(m).__name__ in ("SendPhoto", "SendMessage")
                  and getattr(m, "reply_markup", None) and any("Трейлер" in b.text for b in buttons(m.reply_markup)))
    labels = {b.text: (b.url or b.callback_data) for b in buttons(poster.reply_markup)}
    assert labels["🎞 Трейлер"].endswith("v=x") and labels["⭐ Хотим посмотреть"] == "wl:m:105"
    assert labels["📚 Все части: Назад в будущее (коллекция)"] == "col:264"

    # «хотим»: Алиса добавила, Боб проголосовал, список по голосам
    await press(ALICE, "wl:m:105")
    await press(BOB, "wl:m:105")
    await send(BOB, "/want")
    _, text, kb = session.sent(BOB)[-1]
    assert "Назад в будущее (1985) — 👍 2" in text and "Алиса" in text and "Боб" in text
    await press(BOB, "wr:m:105")                               # чужое не удаляет
    assert "добавил" in session.alerts()[-1] and st.db.wishlist()
    await press(ALICE, "wr:m:105")
    assert not st.db.wishlist()

    # все части: одна нашлась, другая нет
    async def fake_col(http, key, cid, lang="ru-RU"):
        return "Назад в будущее", [tmdb._to_info(mv(105, "Назад в будущее", "1985-07-03")),
                                    tmdb._to_info(mv(165, "Назад в будущее 2", "1989-11-22"))]
    mp.setattr(tmdb, "collection", fake_col)

    async def fake_jac2(http, cfg, q):
        return [rel("Назад в будущее / Back to the Future (1985) BDRip 1080p", "5" * 40)]
    mp.setattr(jacred, "search", fake_jac2)
    await press(ALICE, "col:264")
    kb = session.sent(ALICE)[-1][2]
    await press(ALICE, next(b for b in buttons(kb) if b.text.startswith("⚡")).callback_data)
    assert "5" * 40 in st.tr.torrents
    assert st.tr.torrents["5" * 40]["downloadDir"] == "/downloads/movies/Назад в будущее (1985)"
    assert "Поставил: 1 из 2" in session.sent(ALICE)[-1][1] and "Назад в будущее 2" in session.sent(ALICE)[-1][1]


# ---------- сезоны ----------
def test_season_groups():
    files = [{"name": "Show/Season 1/Show.S01E01.mkv", "length": 10}, {"name": "Show/Season 1/Show.S01E02.mkv", "length": 10},
             {"name": "Show/Сезон 2/02x01.mkv", "length": 20}, {"name": "Show/S10E01.mkv", "length": 5},
             {"name": "Show/Бонусы/making.mkv", "length": 1}]
    g = extras.file_groups(files)
    assert [(n, i, s) for n, i, s in g] == [("Сезон 1", [0, 1], 20), ("Сезон 2", [2], 20),
                                             ("Сезон 10", [3], 5), ("Бонусы", [4], 1)]


async def test_season_selection_flow(env):
    st, session, send, press, mp = env
    files = [{"name": "S/S01E01.mkv", "length": 1}, {"name": "S/S01E02.mkv", "length": 1},
             {"name": "S/S02E01.mkv", "length": 1}]
    state = {"wanted": [True, True, True]}

    async def tr_files(h):
        return files, list(state["wanted"]), 1.0

    async def set_wanted(h, wanted, unwanted):
        for i in wanted:
            state["wanted"][i] = True
        for i in unwanted:
            state["wanted"][i] = False
    mp.setattr(st.tr, "files", tr_files, raising=False)
    mp.setattr(st.tr, "set_wanted", set_wanted, raising=False)
    st.db.add_download(H1, "S", "S", "series", ALICE, ALICE)
    await press(ALICE, f"fs:{H1}")
    kb = session.sent(ALICE)[-1][2]
    assert buttons(kb)[0].text.startswith("✅ Сезон 1") and "Готово" in buttons(kb)[-1].text
    await press(ALICE, f"fz:{H1}:0")                           # выключили 1-й сезон
    assert state["wanted"] == [False, False, True]
    await press(ALICE, f"fz:{H1}:1")                           # последний выключить нельзя
    assert state["wanted"] == [False, False, True] and "оставить" in session.alerts()[-1]
    await press(BOB, f"fs:{H1}")
    assert "не твоя" in session.alerts()[-1]


# ---------- пауза / приоритет ----------
async def test_pause_and_priority(env):
    st, session, send, press, mp = env
    calls = []
    for name in ("stop", "start", "start_now"):
        async def f(h, _n=name):
            calls.append(_n)
        mp.setattr(st.tr, name, f, raising=False)
    await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
    st.tr.torrents[H1]["status"] = 4
    await press(ALICE, f"tp:{H1}")
    st.tr.torrents[H1]["status"] = 0
    await press(ALICE, f"tp:{H1}")
    st.tr.torrents[H1]["peersSendingToUs"] = 31
    await press(ALICE, f"tu:{H1}")
    assert "теперь качается первой" in session.alerts()[-1] and "их 31" in session.alerts()[-1]
    await press(BOB, f"tp:{H1}")
    assert calls == ["stop", "start", "start_now"] and "не твоя" in session.alerts()[-1]


async def test_priority_shown_first_in_status(env):
    st, session, send, press, mp = env
    h2 = "b" * 40
    await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
    await send(ALICE, f"magnet:?xt=urn:btih:{h2}")
    st.tr.torrents[H1]["percentDone"] = 0.9
    st.tr.torrents[h2].update(percentDone=0.1, bandwidthPriority=1)
    await send(ALICE, "/status")
    text = session.sent(ALICE)[-1][1]
    first, second = text.index("T-bbbb"), text.index("T-aaaa")
    assert first < second and text.count("⬆ в приоритете") == 1


async def test_start_now_rpc_calls():
    from bot.transmission import Transmission
    tr = Transmission.__new__(Transmission)
    sent = []

    async def call(method, **kw):
        sent.append((method, kw))
        return {}
    tr.call = call
    await tr.start_now(H1)
    assert sent == [("torrent-set", {"bandwidthPriority": 0}),
                    ("torrent-set", {"ids": [H1], "bandwidthPriority": 1}),
                    ("queue-move-top", {"ids": [H1]}), ("torrent-start-now", {"ids": [H1]})]


# ---------- сторож ----------
def test_health_decide():
    cfg = SimpleNamespace(kodi_offline_min=30)
    h = extras.Health()
    t = 1000.0
    assert extras.health_decide(h, {"disk": (True, "ok"), "tunnel": (False, "нет")}, cfg, t) == []
    msgs = extras.health_decide(h, {"disk": (False, "свободно 10 ГБ"), "tunnel": (False, "нет")}, cfg, t + 300)
    assert any("Диск" in m for m in msgs) and not any("Туннель" in m for m in msgs)  # туннель — сторож сервера
    assert extras.health_decide(h, {"disk": (False, "x"), "tunnel": (False, "нет")}, cfg, t + 600) == []  # не повторяем
    assert extras.health_decide(h, {"kodi": (False, "нет")}, cfg, t) == []                       # малинка: ждём 30 мин
    assert extras.health_decide(h, {"kodi": (False, "нет")}, cfg, t + 29 * 60) == []
    assert extras.health_decide(h, {"kodi": (False, "нет")}, cfg, t + 31 * 60)
    assert "снова в порядке" in extras.health_decide(h, {"disk": (True, "ok")}, cfg, t + 900)[0]


async def test_no_tunnel_alerts_from_bot():
    """О туннеле тревожит сторож сервера, бот — нет (ни падение, ни восстановление)."""
    cfg = SimpleNamespace(kodi_offline_min=30)
    h = extras.Health()
    for i in range(5):
        assert extras.health_decide(h, {"tunnel": (False, "нет")}, cfg, 1000.0 + i * 300) == []
    assert extras.health_decide(h, {"tunnel": (True, "204")}, cfg, 3000.0) == []


# ---------- отчёт, бэкап, расписание ----------
async def test_report_and_backup(env, tmp_path):
    st, *_ = env
    st.db.add_download("x" * 40, "Old", "Старый фильм", "movies", ALICE, ALICE)
    st.db.mark_done("x" * 40)
    st.db.mark_removed("x" * 40, "cleanup")
    st.db.add_download("y" * 40, "New", "Новый фильм", "movies", BOB, BOB)
    rep = await extras.weekly_report(st)
    assert "Поставлено: 2" in rep and "Алиса — 1" in rep and "автоочистка — 1" in rep and "Старый фильм" in rep

    (tmp_path / "b" / "extra").mkdir(parents=True)
    (tmp_path / "b" / "env").write_text("BOT_TOKEN=secret")
    (tmp_path / "b" / "extra" / "kodi-setup.sh").write_text("#!/bin/sh")
    st.cfg = st.cfg.__class__(**{**st.cfg.__dict__, "backup_dir": str(tmp_path / "b")})
    data, names = extras.make_backup(st)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        got = set(tar.getnames())
        assert tar.extractfile(".env").read() == b"BOT_TOKEN=secret"
    assert got - {"MANIFEST.txt"} == {"data/bot.sqlite3", ".env", "extra/kodi-setup.sh"} == set(names)
    assert "MANIFEST.txt" in got


def test_next_weekly():
    wed = datetime(2026, 9, 30, 12, 0)                          # среда
    assert extras.next_weekly(wed, 6, 10) == datetime(2026, 10, 4, 10, 0)
    assert extras.next_weekly(datetime(2026, 10, 4, 9, 0), 6, 10) == datetime(2026, 10, 4, 10, 0)
    assert extras.next_weekly(datetime(2026, 10, 4, 10, 0), 6, 10) == datetime(2026, 10, 11, 10, 0)


# ---------- 🎲 ----------
async def test_random_from_kodi_then_tmdb(env):
    st, session, send, press, mp = env

    class K:
        movies = [{"title": "Матрица", "year": 1999, "plot": "Нео…", "rating": 8.1,
                   "art": {"poster": "image://https%3a%2f%2fimage.tmdb.org%2ft%2fp%2foriginal%2fm.jpg/"}}]

        async def call(self, method, params=None):
            assert params["filter"]["field"] == "playcount"
            return {"movies": self.movies}
    st.kodi = K()
    await send(ALICE, "/random")
    kind, text, _ = session.sent(ALICE)[-1]
    assert "Матрица" in text and "Непросмотренных на диске: 1" in text
    assert kind == "SendPhoto"                                          # с обложкой из Kodi
    photo = [m for m in session.calls if type(m).__name__ == "SendPhoto"][-1].photo
    assert photo == "https://image.tmdb.org/t/p/w500/m.jpg"
    K.movies = []

    async def fake_discover(http, key, kind, params, lang="ru-RU"):
        return [tmdb._to_info(mv(1, "Амели", "2001-04-25"))], 5
    mp.setattr(tmdb, "discover", fake_discover)
    await press(ALICE, "rnd")
    _, text, kb = session.sent(ALICE)[-1]
    assert "Амели" in text and buttons(kb)[0].callback_data.startswith("pk:")


def test_kodi_poster():
    assert extras.kodi_poster({"poster": "image://https%3a%2f%2fimage.tmdb.org%2ft%2fp%2foriginal%2fx.jpg/"}) == \
        "https://image.tmdb.org/t/p/w500/x.jpg"
    assert extras.kodi_poster({"poster": "image://smb%3a%2f%2f192.168.1.30%2fmedia%2fposter.jpg/"}) is None
    assert extras.kodi_poster({"thumb": "https://example.org/t.jpg"}) == "https://example.org/t.jpg"
    assert extras.kodi_poster(None) is None and extras.kodi_poster({}) is None


async def test_random_poster_from_tmdb_when_kodi_has_none(env):
    st, session, send, press, mp = env

    class K:
        async def call(self, method, params=None):
            assert "uniqueid" in params["properties"]
            return {"movies": [{"title": "Троя", "year": 2004, "uniqueid": {"tmdb": "652"}, "art": {}}]}
    st.kodi = K()

    async def fake_details(http, key, kind, tid, lang="ru-RU"):
        assert (kind, str(tid)) == ("movie", "652")
        return tmdb._to_info(mv(652, "Троя", "2004-05-13"))
    mp.setattr(tmdb, "details", fake_details)
    await send(ALICE, "/random")
    assert session.sent(ALICE)[-1][0] == "SendPhoto" and "Троя" in session.sent(ALICE)[-1][1]



def test_ru_title():
    assert jacred.ru_title("Матрица / The Matrix (Энди Вачовски / Andy Wachowski) [1999, BDRip]") == "Матрица"
    assert jacred.ru_title("Киберсталкер (1 сезон: 1-10 серии из 10) / Stalk / 2019") == "Киберсталкер"
    assert jacred.ru_title("Troy.Director's.Cut.2004.Blu-ray.1080p.x264.Rus.Eng.mkv").startswith("Troy.Director")


async def test_report_friendly_titles(env):
    st, *_ = env
    st.db.add_download("a" * 40, "The.Matrix.1999.mkv", "Матрица / The Matrix (Вачовски) [1999, BDRip]",
                       "movies", ALICE, ALICE)
    st.db.add_download("b" * 40, "BB.S01", "Во все тяжкие / Breaking Bad / Сезон: 1", "series", ALICE, ALICE,
                       None, "Во все тяжкие (2008)")
    st.db.mark_done("a" * 40)
    st.db.mark_done("b" * 40)
    text = await extras.weekly_report(st)
    assert "🎬 Матрица\n" in text and "📺 Во все тяжкие (2008)" in text and "Вачовски" not in text


def test_local_backups_keep_and_manifest(env, tmp_path):
    """v6.5.1: ежедневный бэкап на диск, хранятся последние BACKUP_KEEP; внутри MANIFEST с версией."""
    import io as _io
    import tarfile as _tar
    from bot import __version__
    st, session, send, press, mp = env
    st.cfg = st.cfg.__class__(**{**st.cfg.__dict__, "backup_dir": str(tmp_path / "b"),
                                 "backup_local_dir": str(tmp_path / "bk"), "backup_keep": 3})
    stamps = iter(range(10, 60))
    mp.setattr(extras, "now_local", lambda: __import__("datetime").datetime(2026, 10, 2, 4, next(stamps)))
    paths = [extras.save_local_backup(st) for _ in range(5)]
    left = [f for f, _ in extras.local_backups(st)]
    assert left == [p.rsplit("/", 1)[1] for p in paths[-3:]]
    with _tar.open(paths[-1]) as t:
        man = t.extractfile("MANIFEST.txt").read().decode()
    assert f"version={__version__}" in man and "data/bot.sqlite3" in man


def test_snapshot_before_update(tmp_path):
    """Сменилась версия — копия базы до открытия; та же версия — копии нет; хранятся последние 5."""
    from bot.db import DB
    db_path, bk = str(tmp_path / "data" / "bot.sqlite3"), str(tmp_path / "data" / "backups")
    assert extras.snapshot_before_update(db_path, "6.5", bk) is None      # базы ещё нет — копировать нечего
    d = DB(db_path)
    d.allow(42, "Тест")
    d.c.close()
    snap = extras.snapshot_before_update(db_path, "6.5.1", bk)
    assert snap and "before-v6.5.1-from-v6.5" in snap
    assert DB(snap).is_allowed(42)
    assert extras.snapshot_before_update(db_path, "6.5.1", bk) is None    # перезапуск той же версии
