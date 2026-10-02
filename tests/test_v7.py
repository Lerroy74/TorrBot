"""v7: пульт и медиатека Kodi, подписки, «ждать качество», Gemini, детский режим,
место на диске, зависшие закачки, сторож нагрузки."""
import json
import os
import time
from datetime import date, datetime, timedelta

import pytest
from aiohttp import web

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["TMDB_API_KEY"] = "k"

from test_flows import ADMIN, ALICE, BOB, FakeSession, FakeTr, buttons, mv, rel  # noqa: E402

from bot import ai, extras, guard, jacred, kids, kodi, main, remote, space, stall, subs, tmdb  # noqa: E402
from bot.config import load  # noqa: E402
from bot.db import DB  # noqa: E402

GB = 1024 ** 3


class Tr(FakeTr):
    """FakeTr + место на диске, файлы, «черепаха», скорость."""

    def __init__(self, free=10 ** 12):
        super().__init__()
        self.free, self.turtles, self.files_of, self.unwanted, self.removed_data = free, [], {}, {}, []

    async def free_space(self, path):
        return self.free

    async def remove(self, h, delete_data=True):
        self.removed_data.append((h, delete_data))
        await super().remove(h, delete_data)

    async def stats(self):
        return {"downloadSpeed": 12 * 1024 ** 2, "uploadSpeed": 10 ** 6, "activeTorrentCount": 2}

    async def turtle(self, on, down_kb=None, up_kb=None):
        self.turtles.append((on, down_kb, up_kb))

    async def files(self, h):
        fs = self.files_of.get(h, [])
        return fs, [True] * len(fs), 1.0

    async def set_wanted(self, h, wanted, unwanted):
        self.unwanted[h] = unwanted


class Kodi:
    def __init__(self, items=None, down=False):
        self.items, self.down, self.calls, self.playing = items or [], down, [], None

    async def _ok(self, name, *a):
        self.calls.append((name, *a))
        if self.down:
            raise kodi.KodiError("Kodi недоступен")

    async def scan(self):
        await self._ok("scan")

    async def clean(self):
        await self._ok("clean")

    async def notify(self, title, message, ms=10000):
        await self._ok("notify", title, message)

    async def videos(self):
        await self._ok("videos")
        return self.items

    async def open(self, item):
        await self._ok("open", item)

    async def now_playing(self):
        await self._ok("now")
        return self.playing

    async def play_pause(self):
        await self._ok("pp")

    async def seek(self, s):
        await self._ok("seek", s)

    async def volume(self, change):
        await self._ok("vol", change)
        return 55


@pytest.fixture
def env(tmp_path, monkeypatch):
    import itertools
    from aiogram import Bot, Dispatcher
    from aiogram.types import CallbackQuery, Chat, Message, Update, User
    monkeypatch.setenv("LOAD_FILE", str(tmp_path / "host-load.json"))
    cfg = load()
    st = main.State(cfg, DB(str(tmp_path / "db.sqlite3")), Tr(), None)
    st.db.allow(ALICE, "Алиса")
    st.db.allow(BOB, "Боб")
    for _u in (ALICE, BOB):        # были в боте до v8 — могут качать
        st.db.set_flag(_u, "can_dl", True)
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    dp = Dispatcher()
    dp.include_router(extras.build_router(st))
    dp.include_router(main.build_router(st))
    ids, upd = itertools.count(5000), itertools.count(1)

    async def send(uid, text):
        u = User(id=uid, is_bot=False, first_name=f"u{uid}")
        m = Message(message_id=next(ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
                    from_user=u, text=text)
        await dp.feed_update(bot, Update(update_id=next(upd), message=m))

    async def press(uid, data):
        u = User(id=uid, is_bot=False, first_name=f"u{uid}")
        m = Message(message_id=next(ids), date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")
        cb = CallbackQuery(id=str(next(ids)), from_user=u, chat_instance="c", data=data, message=m)
        await dp.feed_update(bot, Update(update_id=next(upd), callback_query=cb))

    return st, session, send, press, monkeypatch, bot, tmp_path


def texts(session, chat=None):
    return [t for _, t, _ in session.sent(chat)]


# ======================= разбор серий =======================
def test_span():
    assert subs.span("Тед Лассо / Ted Lasso [S03E01-05 из 12] (2023) WEB-DL 1080p") == (3, 5, 12)
    assert subs.span("Сёгун / Shogun / Сезон: 1 / Серии: 1-6 из 10 [2024, WEB-DL 1080p]") == (1, 6, 10)
    assert subs.span("Сёгун (1 сезон: 1-6 серии из 10) | LostFilm") == (1, 6, 10)
    assert subs.span("Пингвин / The Penguin [01x01-04 из 08]") == (1, 4, 8)
    assert subs.span("Во все тяжкие / Breaking Bad / Сезон 5 (2012) WEB-DL") == (5, None, None)
    assert subs.span("Фильм (1994) BDRip") == (None, None, None)


# ======================= Gemini =======================
def test_ai_parse():
    raw = '```json\n[{"title": "Маска", "original": "The Mask", "year": 1994, "tv": false}, {"x": 1}]\n```'
    assert ai.parse_answer(raw) == [{"title": "Маска", "original": "The Mask", "year": "1994", "tv": False}]
    assert ai.parse_answer("не знаю") == [] and ai.parse_answer("[битый") == []


async def test_ai_client_and_errors(aiohttp_client, monkeypatch):
    mode = {"m": "ok"}

    async def gen(request):
        assert request.headers["x-goog-api-key"] == "KEY"
        if mode["m"] == "429":
            return web.Response(status=429)
        if mode["m"] == "404" and "gemini-x" in request.path:
            return web.Response(status=404)
        return web.json_response({"candidates": [{"content": {"parts": [
            {"text": '[{"title":"Маска","original":"The Mask","year":1994,"tv":false}]'}]}}]})

    app = web.Application()
    app.router.add_post("/v1beta/models/{m}", gen)
    client = await aiohttp_client(app)
    monkeypatch.setattr(ai, "API", str(client.make_url("/v1beta/models/")) + "{model}:generateContent")
    got = await ai.ask(client.session, "KEY", "gemini-x", "мужик в зелёной маске")
    assert got[0]["title"] == "Маска"
    mode["m"] = "404"                                        # модели нет — берём запасную
    assert (await ai.ask(client.session, "KEY", "gemini-x", "маска"))[0]["year"] == "1994"
    mode["m"] = "429"
    with pytest.raises(ai.AiUnavailable) as e:
        await ai.ask(client.session, "KEY", "gemini-x", "маска")
    assert "лимит" in e.value.reason


async def test_plot_via_ai_then_fallback_to_wiki(env):
    st, session, send, press, mp, bot, _ = env
    st.ai = ai.Chain(st.cfg.__class__(**{**st.cfg.__dict__, "yandex_key": "K", "yandex_folder": "F"}),
                     {"yandex": None})
    from bot import wiki
    calls = {"ai": 0, "wiki": 0}

    async def fake_ai(chain, tmdb_http, cfg, text, meter=None):
        calls["ai"] += 1
        if calls["ai"] == 1:
            return [tmdb._to_info(mv(1, "Маска", "1994-07-29", orig="The Mask"))], "Алиса"
        raise ai.AiUnavailable("Алиса: закончились деньги на счёте")

    async def fake_wiki(http, key, text, lang):
        calls["wiki"] += 1
        return [tmdb._to_info(mv(2, "Маска 2", "2005-02-18"))]

    async def no_cast(st_, infos, timeout=6):
        return None
    mp.setattr(ai, "search_by_plot", fake_ai)
    mp.setattr(wiki, "search_by_plot", fake_wiki)
    mp.setattr(main, "add_cast", no_cast)
    await send(ALICE, "/plot мужик находит маску и становится зелёным")
    t, kb = session.sent(ALICE)[-1][1:]
    assert "🤖 По описанию (ИИ, Алиса)" in t and "Маска (1994)" in t and calls["wiki"] == 0
    assert any("Википедии" in b.text for b in buttons(kb))
    await send(ALICE, "/plot мужик находит маску и становится зелёным")
    t = session.sent(ALICE)[-1][1]
    assert "ИИ сейчас недоступен (Алиса: закончились деньги на счёте)" in t and "Википедия" in t
    assert "Маска 2" in t and calls["wiki"] == 1


async def test_ai_chain_failover(aiohttp_client, monkeypatch):
    """Алиса без денег → Groq; Алиса «отдыхает» час; оба не могут — общая причина."""
    state = {"ya": 402, "groq": 200}
    seen = []

    async def ya(request):
        seen.append(("ya", request.headers["Authorization"], request.headers.get("OpenAI-Project"),
                     (await request.json())["model"]))
        if state["ya"] != 200:
            return web.Response(status=state["ya"],
                                text="billing account is not active" if state["ya"] == 402 else "oops")
        return web.json_response({"choices": [{"message": {"content": '[{"title":"День сурка","year":1993}]'}}]})

    async def groq(request):
        seen.append(("groq", request.headers["Authorization"], None, (await request.json())["model"]))
        if state["groq"] != 200:
            return web.Response(status=state["groq"], headers={"Retry-After": "120"})
        return web.json_response({"choices": [{"message": {"content": '[{"title":"Groundhog Day","year":1993}]'}}]})

    app = web.Application()
    app.router.add_post("/ya/chat/completions", ya)
    app.router.add_post("/groq", groq)
    client = await aiohttp_client(app)
    monkeypatch.setattr(ai, "GROQ_API", str(client.make_url("/groq")))
    monkeypatch.setenv("YANDEX_API_KEY", "YK")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gX")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_1")
    monkeypatch.setenv("YANDEX_URL", str(client.make_url("/ya")))
    cfg = load()
    chain = ai.Chain(cfg, {"yandex": client.session, "groq": client.session})
    t0 = 1_000_000.0
    got, who = await chain.guess("мужик застрял в одном дне", t0)
    assert who == "Groq" and got[0]["title"] == "Groundhog Day"
    assert seen[0] == ("ya", "Api-Key YK", "b1gX", "gpt://b1gX/aliceai-llm/latest")
    assert "Алиса ⏸ закончились деньги на счёте" in chain.status(t0)
    seen.clear()
    await chain.guess("ещё", t0 + 60)                          # Алиса отдыхает — сразу Groq
    assert [x[0] for x in seen] == ["groq"]
    state.update(ya=200, groq=429)
    got, who = await chain.guess("ещё", t0 + 3700)             # час прошёл — снова Алиса
    assert who == "Алиса" and got[0]["title"] == "День сурка"
    state["ya"] = 500
    with pytest.raises(ai.AiUnavailable) as e:
        await chain.guess("ещё", t0 + 8000)
    assert "Алиса: ошибка 500" in e.value.reason and "Groq: закончился лимит" in e.value.reason
    empty = ai.Chain(load().__class__(**{**cfg.__dict__, "yandex_key": None, "groq_key": None, "gemini_key": None}), {})
    assert not empty


# ======================= Kodi: медиатека с повтором, уведомление, «на ТВ» =======================
async def test_kodi_scan_retries_until_pi_is_back(env):
    st, *_ = env
    st.kodi = Kodi(down=True)
    remote.kodi_request(st, "scan", delay=0)
    now = time.time() + 1
    assert await remote.kodi_sync_once(st, now) == []                  # малинка спит
    assert st.kodi_jobs["scan"] >= now + st.cfg.kodi_retry_min * 60 - 1
    assert await remote.kodi_sync_once(st, now + 60) == []             # рано — даже не пробуем
    assert len([c for c in st.kodi.calls if c[0] == "scan"]) == 1
    st.kodi.down = False
    assert await remote.kodi_sync_once(st, now + 3600) == ["scan"] and not st.kodi_jobs


async def test_done_notifies_tv_and_queues_scan(env):
    st, session, send, press, mp, bot, _ = env
    st.kodi = Kodi()
    h = "b" * 40
    st.tr.torrents[h] = {"hashString": h, "name": "Mask.1994.mkv", "percentDone": 1.0, "totalSize": GB,
                         "downloadDir": "/downloads/movies/Маска (1994)", "status": 6}
    st.db.add_download(h, "Mask", "Маска / The Mask", "movies", ALICE, ALICE, None, "Маска (1994)")
    st.db.set_flag(ALICE, remote.REMOTE, True)
    await main.watch_once(bot, st)
    await __import__("asyncio").sleep(0)
    sent = session.sent(ALICE)[-1]
    assert "Скачано" in sent[1]
    labels = [b.text for b in buttons(sent[2])]
    assert "▶ Включить на ТВ" in labels
    assert ("notify", "Скачан фильм", "Маска (1994)") in st.kodi.calls
    assert "scan" in st.kodi_jobs


async def test_play_on_tv_picks_next_unwatched_episode(env, tmp_path):
    st, *_ = env
    K = st.cfg.kodi_media_url
    st.kodi = Kodi([{"episodeid": 11, "file": f"{K}/series/X (2020)/S01/e1.mkv", "season": 1, "episode": 1, "playcount": 1},
                    {"episodeid": 12, "file": f"{K}/series/X (2020)/S01/e2.mkv", "season": 1, "episode": 2, "playcount": 0},
                    {"movieid": 5, "file": f"{K}/movies/Y.mkv"}])
    note = await remote.play_path(st, "/downloads/series/X (2020)")
    assert "1×02" in note and ("open", {"episodeid": 12}) in st.kodi.calls
    note = await remote.play_path(st, "/downloads/movies/Y.mkv")
    assert note.startswith("▶") and ("open", {"movieid": 5}) in st.kodi.calls
    note = await remote.play_path(st, "/downloads/movies/Новый.mkv")          # в медиатеке ещё нет
    assert "попросил обновить" in note and "scan" in st.kodi_jobs


async def test_remote_permission_and_pult(env):
    st, session, send, press, mp, bot, _ = env
    st.kodi = Kodi()
    st.kodi.playing = {"playerid": 1, "title": "Маска", "time": 600, "total": 6000, "paused": False,
                       "percent": 10.0, "file": "x"}
    await send(ALICE, "/tv")
    assert "только у администратора" in texts(session, ALICE)[-1]
    await press(ADMIN, f"urm:{ALICE}")                       # админ даёт пульт
    assert st.db.flag(ALICE, remote.REMOTE)
    assert any("пульт" in t for t in texts(session, ALICE))
    await send(ALICE, "/tv")
    t, kb = session.sent(ALICE)[-1][1:]
    assert "Маска" in t and "10:00 / 1:40:00" in t and "▶ играет" in t
    await press(ALICE, "tv:+30")
    assert ("seek", 30) in st.kodi.calls
    await press(ALICE, "tv:vu")
    assert ("vol", "increment") in st.kodi.calls
    await press(BOB, "tv:pp")
    assert ("pp",) not in st.kodi.calls and any("Пульт" in a for a in session.alerts())


def test_kodi_time_format():
    assert kodi.fmt_time(65) == "1:05" and kodi.fmt_time(3725) == "1:02:05"
    assert kodi._secs({"hours": 1, "minutes": 2, "seconds": 5}) == 3725


async def test_kodi_now_playing_client(aiohttp_client):
    async def rpc(request):
        body = await request.json()
        m = body["method"]
        res = {"Player.GetActivePlayers": [{"playerid": 1, "type": "video"}],
               "Player.GetItem": {"item": {"title": "Пилот", "showtitle": "Сёгун", "season": 1, "episode": 1,
                                           "file": "smb://x/e1.mkv"}},
               "Player.GetProperties": {"time": {"hours": 0, "minutes": 5, "seconds": 0},
                                        "totaltime": {"hours": 1, "minutes": 0, "seconds": 0},
                                        "speed": 0, "percentage": 8.3}}.get(m, "OK")
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": res})
    app = web.Application()
    app.router.add_post("/jsonrpc", rpc)
    client = await aiohttp_client(app)
    k = kodi.Kodi(client.session, str(client.make_url("/jsonrpc")), None, None)
    now = await k.now_playing()
    assert now["title"] == "Сёгун · 1×01 Пилот" and now["paused"] and now["time"] == 300 and now["total"] == 3600


# ======================= место на диске =======================
async def test_no_space_user_without_delete_right_asks_admin(env):
    st, session, send, press, mp, bot, _ = env
    st.tr.free = 8 * GB                                      # свободно 8, запас 5 → можно 3 ГБ

    async def fake_find(st_, q):
        return [mv(9, "Дюна", "2021-09-15", orig="Dune")]
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel("Дюна / Dune (2021) WEB-DL 1080p", "5" * 40, size=6)]
    mp.setattr(jacred, "search", fake_jac)

    async def no_sugg(st_, limit=3):
        return ["• 🎬 Старьё — 9.0 ГБ, просмотрено 01.09"]
    mp.setattr(space, "suggestions", no_sugg)
    await send(ALICE, "дюна 2021")
    kb = session.sent(ALICE)[-1][2]
    await press(ALICE, next(b for b in buttons(kb) if b.text == "⬇ 1").callback_data)
    assert not st.tr.torrents
    assert "Не влезает" in texts(session, ALICE)[-1] and "Попросил администратора" in texts(session, ALICE)[-1]
    admin_msg = session.sent(ADMIN)[-1]
    assert "Алиса" in admin_msg[1] and "Старьё" in admin_msg[1]
    retry = next(b for b in buttons(admin_msg[2]) if b.text.startswith("⬇ Поставить"))
    await press(ADMIN, retry.callback_data)                  # всё ещё нет места
    assert not st.tr.torrents and any("Всё ещё не влезает" in a for a in session.alerts())
    st.tr.free = 100 * GB
    await press(ADMIN, retry.callback_data)
    assert len(st.tr.torrents) == 1
    row = st.db.get("5" * 40)
    assert row["user_id"] == ALICE and row["chat_id"] == ALICE and row["tmdb_id"] == 9


async def test_nospace_error_is_reported_once(env):
    st, session, send, press, mp, bot, _ = env
    h = "c" * 40
    st.tr.torrents[h] = {"hashString": h, "name": "Big", "percentDone": 0.4, "totalSize": GB, "status": 0,
                         "error": 3, "errorString": "No space left on device", "downloadDir": "/downloads/movies"}
    st.db.add_download(h, "Big", "Big", "movies", ALICE, ALICE)
    await main.watch_once(bot, st)
    await main.watch_once(bot, st)
    msgs = [t for t in texts(session, ALICE) if "кончилось место" in t]
    assert len(msgs) == 1 and any("кончилось место" in t for t in texts(session, ADMIN))


# ======================= зависшие закачки =======================
async def test_stalled_download_offers_alternatives(env):
    st, session, send, press, mp, bot, _ = env
    h = "d" * 40
    st.tr.torrents[h] = {"hashString": h, "name": "Dune.2021", "percentDone": 0.1, "totalSize": GB, "status": 4,
                         "haveValid": 100, "peersSendingToUs": 0, "downloadDir": "/downloads/movies/Дюна (2021)"}
    st.db.add_download(h, "Dune", "Дюна / Dune (2021)", "movies", ALICE, ALICE, None, "Дюна (2021)")

    async def fake_jac(http, cfg, q):
        return [rel("Дюна / Dune (2021) BDRip 1080p", "e" * 40), rel("Дюна (2021) WEB-DL 1080p", h)]
    mp.setattr(jacred, "search", fake_jac)
    rows = {h: st.db.get(h)}
    t0 = time.time()
    assert await stall.check(bot, st, st.tr.torrents, rows, t0) == 0
    assert await stall.check(bot, st, st.tr.torrents, rows, t0 + 3 * 3600 + 1) == 1
    msg = session.sent(ALICE)[-1]
    assert "без движения 3 ч" in msg[1] and "BDRip" in msg[1]
    assert await stall.check(bot, st, st.tr.torrents, {h: st.db.get(h)}, t0 + 9 * 3600) == 0   # один раз
    await press(ALICE, f"sw:{h}:0")
    assert h not in st.tr.torrents and ("e" * 40) in st.tr.torrents
    assert st.tr.torrents["e" * 40]["downloadDir"] == "/downloads/movies/Дюна (2021)"
    assert st.db.get(h)["removed_reason"] == "replaced"


# ======================= детский режим =======================
async def test_kids_mode(env):
    st, session, send, press, mp, bot, _ = env
    await press(ADMIN, f"ukd:{BOB}")
    assert kids.is_kid(st, BOB)
    cartoon = {**mv(1, "Тачки", "2006-06-08", orig="Cars"), "genre_ids": [16, 10751]}
    horror = {**mv(2, "Тачки-убийцы", "2006-01-01"), "genre_ids": [27]}

    async def fake_find(st_, q):
        return [cartoon, horror]
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel("Тачки / Cars (2006) BDRip 1080p", "f" * 40)]
    mp.setattr(jacred, "search", fake_jac)

    async def age(http, key, info):
        return 0
    mp.setattr(tmdb, "age_rating", age)
    await send(BOB, "тачки")
    t = texts(session, BOB)[-1]
    assert "Cars (2006) BDRip" in t or "Тачки (2006)" in t
    assert "Тачки-убийцы" not in "".join(texts(session, BOB))
    await send(BOB, "magnet:?xt=urn:btih:" + "1" * 40)
    assert "детском режиме" in texts(session, BOB)[-1] and not st.tr.torrents
    listing = next(m for m in reversed(session.sent(BOB)) if "Cars (2006) BDRip" in m[1])
    dl = [b for b in buttons(listing[2]) if b.text == "⬇ 1"]
    await press(BOB, dl[0].callback_data)
    assert ("f" * 40) in st.tr.torrents
    info = tmdb._to_info(horror)
    ok, why = await kids.allowed(st, BOB, info)
    assert not ok and "не мультфильм" in why
    ok, _ = await kids.allowed(st, ALICE, info)
    assert ok


def test_age_from():
    assert tmdb.age_from({"RU": "16+"}) == 16 and tmdb.age_from({"US": "PG-13"}) == 13
    assert tmdb.age_from({}) is None


# ======================= подписки =======================
SHOW = {"id": 77, "name": "Сёгун", "original_name": "Shogun", "first_air_date": "2024-02-27",
        "poster_path": "/s.jpg", "overview": "", "vote_average": 8.6, "status": "Returning Series",
        "last_episode_to_air": {"season_number": 1, "episode_number": 6,
                                "air_date": (date.today() - timedelta(days=2)).isoformat()}}


def jac_item(title, h, details, size=10):
    it = rel(title, h, series=True, size=size)
    it["Details"] = details
    return it


async def test_subscription_follows_topic(env):
    st, session, send, press, mp, bot, tmp = env
    state = dict(SHOW)

    async def tv_state(http, key, tid, lang="ru-RU"):
        return state
    mp.setattr(tmdb, "tv_state", tv_state)

    async def details(http, key, kind, tid, lang="ru-RU"):
        return tmdb._to_info({**state, "media_type": "tv"})
    mp.setattr(tmdb, "details", details)
    topic = "https://rutracker.org/t=1"
    releases = [jac_item("Сёгун / Shogun / Сезон: 1 / Серии: 1-5 из 10 [2024, WEB-DL 1080p]", "1" * 40, topic)]

    async def fake_jac(http, cfg, q):
        return list(releases)
    mp.setattr(jacred, "search", fake_jac)
    # подписка из карточки сериала: закачки ещё нет — бот сам берёт раздачу текущего сезона
    await press(ALICE, "sbt:77")
    assert any("Подписка на <b>Сёгун</b>" in t for t in texts(session, ALICE))
    sub = st.db.sub_by_tmdb(77)
    assert sub["details"] == topic and sub["hash"] == "1" * 40 and sub["last_ep"] == 5
    assert st.tr.torrents["1" * 40]["downloadDir"] == "/downloads/series/Сёгун (2024)"
    assert st.db.get("1" * 40)["sub_id"] == sub["id"]
    await press(BOB, "sbt:77")                               # второй подписчик
    assert len(st.db.sub_users(sub["id"])) == 2

    # тема обновилась — новая раздача в ту же папку, старая станет «прошлой»
    st.tr.torrents["1" * 40]["percentDone"] = 1.0
    releases[:] = [jac_item("Сёгун / Shogun / Сезон: 1 / Серии: 1-6 из 10 [2024, WEB-DL 1080p]", "2" * 40, topic, 12)]
    assert await subs.check_sub(bot, st, st.db.sub_get(sub["id"])) == "обновил тему"
    sub = st.db.sub_get(sub["id"])
    assert sub["hash"] == "2" * 40 and sub["prev_hash"] == "1" * 40 and sub["last_ep"] == 6
    assert any("раздача обновилась" in t for t in texts(session, BOB))
    assert await subs.check_sub(bot, st, sub) == "без изменений"
    # новая докачалась: ту же папку не трогаем — прошлую раздачу убираем без файлов
    st.tr.torrents["1" * 40].update(name="Shogun.S01", downloadDir="/downloads/series/Сёгун (2024)")
    st.tr.torrents["2" * 40].update(name="Shogun.S01", percentDone=1.0, metadataPercentComplete=1)
    await subs.maintain(st, sub)
    assert ("1" * 40, False) in st.tr.removed_data
    assert st.db.sub_get(sub["id"])["prev_hash"] is None
    # уведомление «Скачано» — всем подписчикам
    st.db.add_download("3" * 40, "x", "x", "series", ALICE, ALICE, sub_id=sub["id"])
    st.tr.torrents["3" * 40] = {"hashString": "3" * 40, "name": "x", "percentDone": 1.0, "totalSize": 1,
                                "downloadDir": "/downloads/series/Сёгун (2024)"}
    await main.watch_once(bot, st)
    assert any("Скачано" in t for t in texts(session, BOB))


async def test_subscription_skips_deleted_episodes_and_offers_alternatives(env):
    st, session, send, press, mp, bot, tmp = env
    state = {**SHOW, "last_episode_to_air": {"season_number": 1, "episode_number": 7,
                                             "air_date": (date.today() - timedelta(days=10)).isoformat()}}

    async def tv_state(http, key, tid, lang="ru-RU"):
        return state
    mp.setattr(tmdb, "tv_state", tv_state)
    topic, other = "https://rutracker.org/t=1", "https://rutor.info/t=9"
    st.cfg = st.cfg.__class__(**{**st.cfg.__dict__, "dir_series": str(tmp / "series")})
    os.makedirs(tmp / "series" / "Сёгун (2024)", exist_ok=True)
    sid = st.db.sub_create(77, "Сёгун", "2024", None, "Сёгун (2024)", ALICE, topic, "1" * 40, 1, 6)
    st.db.sub_join(sid, ALICE, ALICE)
    st.db.sub_files_add(sid, ["e1.mkv", "e2.mkv"])           # e1 уже удалили, e2 ещё на диске
    open(tmp / "series" / "Сёгун (2024)" / "e2.mkv", "w").close()
    st.tr.torrents["1" * 40] = {"hashString": "1" * 40, "name": "S01", "percentDone": 0.5, "totalSize": 1,
                                "downloadDir": str(tmp / "series" / "Сёгун (2024)"), "metadataPercentComplete": 1}
    st.tr.files_of["1" * 40] = [{"name": "S01/e1.mkv"}, {"name": "S01/e2.mkv"}, {"name": "S01/e3.mkv"}]
    await subs.maintain(st, st.db.sub_get(sid))
    assert st.tr.unwanted["1" * 40] == [0]                   # e1 второй раз не качаем
    assert st.db.sub_files(sid) == {"e1.mkv", "e2.mkv", "e3.mkv"}

    async def fake_jac(http, cfg, q):                         # наша тема стоит на 6-й, у другой есть 7-я
        return [jac_item("Сёгун / Shogun / Сезон: 1 / Серии: 1-6 из 10 [2024, WEB-DL 1080p]", "1" * 40, topic),
                jac_item("Сёгун / Shogun [S01E01-07 из 10] (2024) WEB-DL 1080p", "4" * 40, other)]
    mp.setattr(jacred, "search", fake_jac)
    assert await subs.check_sub(bot, st, st.db.sub_get(sid)) == "предложил другие"
    msg = session.sent(ALICE)[-1]
    assert "1×07" in msg[1] and buttons(msg[2])[0].callback_data == f"sbs:{sid}:0"
    assert await subs.check_sub(bot, st, st.db.sub_get(sid)) == "без изменений"     # второй раз не предлагает
    await press(ALICE, f"sbs:{sid}:0")
    sub = st.db.sub_get(sid)
    assert sub["details"] == other and sub["hash"] == "4" * 40 and sub["prev_hash"] == "1" * 40


async def test_unsubscribe_and_podpiski(env):
    st, session, send, press, mp, bot, _ = env
    sid = st.db.sub_create(5, "Тест", "2020", None, "Тест (2020)", ALICE)
    st.db.sub_join(sid, ALICE, ALICE)
    st.db.wait_add("m", 9, "Дюна", "2021", None, ALICE, ALICE)
    await send(ALICE, "/podpiski")
    t, kb = session.sent(ALICE)[-1][1:]
    assert "Тест" in t and "Дюна" in t and "1080p" in t
    await press(BOB, f"sbu:{sid}")
    assert st.db.sub_get(sid)                                # чужую не снять
    await press(ALICE, f"sbu:{sid}")
    assert st.db.sub_get(sid) is None                        # последний ушёл — подписки нет
    await press(ALICE, "wtu:m:9")
    assert not st.db.waits()


async def test_wait_for_quality(env):
    st, session, send, press, mp, bot, _ = env
    info = tmdb._to_info(mv(9, "Дюна", "2021-09-15", orig="Dune"))

    async def details(http, key, kind, tid, lang="ru-RU"):
        return info
    mp.setattr(tmdb, "details", details)
    r720 = rel("Дюна / Dune (2021) WEB-DL 720p", "5" * 40)
    r720["ffprobe"] = [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}]
    q = {"rels": [r720]}

    async def fake_jac(http, cfg, query):
        return q["rels"]
    mp.setattr(jacred, "search", fake_jac)

    async def fake_find(st_, query):
        return [mv(9, "Дюна", "2021-09-15", orig="Dune")]
    mp.setattr(main, "find_info", fake_find)
    await send(ALICE, "дюна 2021")
    kb = session.sent(ALICE)[-1][2]
    wait_btn = next(b for b in buttons(kb) if b.text.startswith("⏳"))
    await press(ALICE, wait_btn.callback_data)
    assert len(st.db.waits()) == 1
    assert await subs.check_waits(bot, st) == 0              # всё ещё 720p
    st.db.c.execute("UPDATE waits SET checked_at=0")
    q["rels"] = [rel("Дюна / Dune (2021) BDRip 1080p", "6" * 40)]
    assert await subs.check_waits(bot, st) == 1
    assert ("6" * 40) in st.tr.torrents and not st.db.waits()
    assert any("Появилась хорошая раздача" in t for t in texts(session, ALICE))


# ======================= сторож нагрузки =======================
def test_in_hours():
    assert guard.in_hours(datetime(2026, 1, 1, 12, 0), "08:00-23:00")
    assert not guard.in_hours(datetime(2026, 1, 1, 23, 30), "08:00-23:00")
    assert guard.in_hours(datetime(2026, 1, 1, 2, 0), "22:00-07:00")


async def test_guard_throttles_and_alerts(env):
    st, session, send, press, mp, bot, tmp = env
    path = st.cfg.load_file
    st.cfg = st.cfg.__class__(**{**st.cfg.__dict__, "kodi_url": "http://192.168.1.33:8080/jsonrpc"})
    st.kodi = Kodi()
    t0 = time.time()

    def write(ts, util, smb=None, smart=None):
        with open(path, "w") as f:
            json.dump({"ts": ts, "disk": {"dev": "sdb", "util": util, "write_mb": 15, "read_mb": 2, "await_ms": 180},
                       "net": {"rx_mbit": 100, "tx_mbit": 5}, "smb": {"items": smb or []},
                       "smart": smart or {"health": "PASSED", "realloc": 0, "pending": 0, "uncorrect": 0}}, f)
    night = datetime(2026, 1, 1, 2, 0)
    write(t0, 30)
    await guard.guard_once(bot, st, t0, night)
    assert st.tr.turtles == [(False, 5000, 1000)] and not texts(session, ADMIN)
    # смотрят по сети с ПК → «черепаха»
    write(t0 + 15, 40, smb=[{"name": "movies/Маска (1994)/Маска.mkv", "client": "192.168.1.40"}])
    await guard.guard_once(bot, st, t0 + 15, night)
    assert st.tr.turtles[-1][0] is True and "просмотр" in st.guard.reasons[0]
    # малинка сама читает фильм по SMB — это не «по сети», а Kodi (который сейчас не играет)
    write(t0 + 30, 40, smb=[{"name": "movies/X.mkv", "client": "192.168.1.33"}])
    await guard.guard_once(bot, st, t0 + 30, night)
    assert st.guard.watching == []
    write(t0 + 300, 30)
    await guard.guard_once(bot, st, t0 + 300, night)          # просмотр закончился давно → полная скорость
    assert st.tr.turtles[-1][0] is False
    # диск занят 100% дольше 2 минут → тревога и торможение на 15 мин
    for i in range(0, 140, 15):
        write(t0 + 400 + i, 99)
        await guard.guard_once(bot, st, t0 + 400 + i, night)
    alert = [t for t in texts(session, ADMIN) if "захлёбывается" in t]
    assert len(alert) == 1 and "Притормозил" in alert[0] and st.tr.turtles[-1][0] is True
    # SMART ухудшился
    write(t0 + 600, 20, smart={"health": "PASSED", "realloc": 8, "pending": 0, "uncorrect": 0})
    await guard.guard_once(bot, st, t0 + 600, night)
    assert any("переназначенных" in t for t in texts(session, ADMIN))
    # дневной режим
    st.db.set_flag(0, guard.NIGHT, True)
    st.db.set_flag(0, guard.DISK_OFF, True)
    st.guard.disk_until = 0
    write(t0 + 700, 10)
    await guard.guard_once(bot, st, t0 + 700, datetime(2026, 1, 1, 12, 0))
    assert st.tr.turtles[-1][0] is True and "дневной" in st.guard.reasons[0]
    await guard.guard_once(bot, st, t0 + 715, datetime(2026, 1, 1, 23, 30))
    assert st.tr.turtles[-1][0] is False


async def test_guard_agent_silence_and_speed_menu(env):
    st, session, send, press, mp, bot, tmp = env
    with open(st.cfg.load_file, "w") as f:
        json.dump({"ts": 1}, f)                              # старые данные
    t0 = time.time()
    await guard.guard_once(bot, st, t0)
    await guard.guard_once(bot, st, t0 + 400)
    assert any("Агент нагрузки" in t for t in texts(session, ADMIN))
    await send(ADMIN, "/speed")
    t, kb = session.sent(ADMIN)[-1][1:]
    assert "Скорость и нагрузка" in t and "Дневной режим" in t
    await press(ADMIN, "sp:night")
    assert st.db.flag(0, guard.NIGHT)
    await send(ALICE, "/speed")
    assert not any("Скорость и нагрузка" in x for x in texts(session, ALICE))


def test_load_agent_parsers(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent", os.path.join(os.path.dirname(__file__), "..", "host",
                                                                         "load-agent.py"))
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    env = tmp_path / ".env"
    env.write_text('MEDIA_DIR="/mnt/media"\nBOT_TOKEN=1\n')
    assert agent.getenv(str(env), "MEDIA_DIR") == "/mnt/media"
    lock = ("Locked files:\nPid  User(ID)  DenyMode  Access  R/W  Oplock  SharePath  Name  Time\n"
            "12345  65534  DENY_NONE  0x120089  RDONLY  LEASE(RWH)  /mnt/media   movies/Маска (1994)/Маска.mkv"
            "   Fri Oct  2 13:00:00 2026\n"
            "12345  65534  DENY_NONE  0x120089  RDONLY  NONE  /mnt/media   movies/Маска (1994)   Fri Oct  2 13:00:00 2026\n")
    procs = "12345   nobody   nogroup   192.168.1.40 (ipv4:192.168.1.40:51234)   SMB3_11\n"
    monkeypatch.setattr(agent.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(agent, "run", lambda cmd, timeout=10: lock if "-L" in cmd else procs)
    assert agent.smb_open("/mnt/media") == [{"name": "movies/Маска (1994)/Маска.mkv", "client": "192.168.1.40"}]


async def test_subscribe_old_download_does_not_redownload(env):
    """Сериал скачан до v7 (тема не запомнена): подписка находит тему, но сезон второй раз не качает."""
    st, session, send, press, mp, bot, _ = env

    async def tv_state(http, key, tid, lang="ru-RU"):
        return SHOW
    mp.setattr(tmdb, "tv_state", tv_state)

    async def details(http, key, kind, tid, lang="ru-RU"):
        return tmdb._to_info({**SHOW, "media_type": "tv"})
    mp.setattr(tmdb, "details", details)
    old = "9" * 40
    st.tr.torrents[old] = {"hashString": old, "name": "Shogun.S01", "percentDone": 1.0, "totalSize": GB,
                           "downloadDir": "/downloads/series/Сёгун (2024)"}
    st.db.add_download(old, "Shogun.S01", "Сёгун / Shogun / Сезон: 1 / Серии: 1-5 из 10", "series", ALICE, ALICE,
                       None, "Сёгун (2024)", tmdb_kind="t", tmdb_id=77)
    topic = "https://rutracker.org/t=5"

    async def fake_jac(http, cfg, q):
        return [jac_item("Сёгун / Shogun / Сезон: 1 / Серии: 1-6 из 10 [2024, WEB-DL 1080p]", "8" * 40, topic)]
    mp.setattr(jacred, "search", fake_jac)
    await press(ALICE, f"sb:{old}")
    sub = st.db.sub_by_tmdb(77)
    assert sub["details"] == topic and sub["hash"] == "8" * 40 and sub["prev_hash"] == old
    assert list(st.tr.torrents) == [old]                     # ничего не поставил
    assert await subs.check_sub(bot, st, sub) == "без изменений"
    assert list(st.tr.torrents) == [old]
