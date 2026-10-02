"""Сценарии бота целиком: настоящие обработчики aiogram, подставной Telegram,
подставные TMDB / jac.red / Transmission."""
import itertools
import os
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["TMDB_API_KEY"] = "k"

from bot import extras, jacred, main, tmdb  # noqa: E402
from bot.config import load  # noqa: E402
from bot.db import DB  # noqa: E402

ADMIN, ALICE, BOB = 1, 100, 200
H1 = "a" * 40
_ids = itertools.count(1000)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__returning__ is Message or "Message" in str(method.__returning__):
            chat_id = getattr(method, "chat_id", None) or 0
            return Message(message_id=next(_ids), date=datetime.now(),
                           chat=Chat(id=int(chat_id), type="private"),
                           text=getattr(method, "text", None),
                           caption=getattr(method, "caption", None)).as_(bot)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    def sent(self, chat_id=None):
        """Тексты всего, что бот отправил/отредактировал (опционально — одному чату)."""
        out = []
        for m in self.calls:
            t = getattr(m, "text", None) or getattr(m, "caption", None)
            if t and (chat_id is None or getattr(m, "chat_id", chat_id) == chat_id):
                out.append((type(m).__name__, t, getattr(m, "reply_markup", None)))
        return out

    def alerts(self):
        return [m.text for m in self.calls if isinstance(m, AnswerCallbackQuery) and m.text]


class FakeTr:
    def __init__(self):
        self.torrents, self.removed = {}, []

    async def add(self, magnet, folder):
        h = magnet.split("btih:")[1][:40].lower()
        dup = h in self.torrents
        self.torrents[h] = {"hashString": h, "name": f"T-{h[:4]}", "percentDone": 0.3, "totalSize": 10 ** 9,
                            "rateDownload": 0, "eta": -1, "downloadDir": folder}
        return h, self.torrents[h]["name"], dup

    async def get(self, hashes=None):
        return [t for h, t in self.torrents.items() if hashes is None or h in hashes]

    async def remove(self, h, delete_data=True):
        self.removed.append(h)
        self.torrents.pop(h, None)

    async def free_space(self, path):
        return 10 ** 12


def rel(title, h, series=False, size=5):
    return {"Tracker": "rutor", "Title": title, "Size": size * 1024 ** 3, "Seeders": 50, "Peers": 1,
            "Category": [5000] if series else [2000], "MagnetUri": f"magnet:?xt=urn:btih:{h}",
            "ffprobe": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}],
            "info": {"types": ["serial" if series else "movie"]}}


def mv(i, title, date, orig="", tv=False, votes=100):
    k = "name" if tv else "title"
    return {"id": i, "media_type": "tv" if tv else "movie", k: title,
            ("original_name" if tv else "original_title"): orig or title,
            ("first_air_date" if tv else "release_date"): date, "poster_path": f"/{i}.jpg",
            "overview": "", "vote_average": 7, "vote_count": votes}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = load()
    st = main.State(cfg, DB(str(tmp_path / "db.sqlite3")), FakeTr(), None)
    st.db.allow(ALICE, "Алиса")
    st.db.allow(BOB, "Боб")
    for _u in (ALICE, BOB):        # были в боте до v8 — могут качать
        st.db.set_flag(_u, "can_dl", True)
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    dp = Dispatcher()
    dp.include_router(extras.build_router(st))
    dp.include_router(main.build_router(st))
    upd = itertools.count(1)

    async def send(uid, text):
        u = User(id=uid, is_bot=False, first_name=f"u{uid}")
        m = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
                    from_user=u, text=text)
        await dp.feed_update(bot, Update(update_id=next(upd), message=m))

    async def press(uid, data):
        u = User(id=uid, is_bot=False, first_name=f"u{uid}")
        m = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")
        cb = CallbackQuery(id=str(next(_ids)), from_user=u, chat_instance="c", data=data, message=m)
        await dp.feed_update(bot, Update(update_id=next(upd), callback_query=cb))

    return st, session, send, press, monkeypatch


def buttons(markup):
    return [b for row in (markup.inline_keyboard if markup else []) for b in row]


# ---------- «Маска»: сначала выбор фильма, потом только его раздачи ----------
async def test_mask_choice_then_exact_releases(env):
    st, session, send, press, mp = env
    cands = [mv(2, "Маска", "2020-02-02", tv=True), mv(1, "Маска", "1994-07-29", orig="The Mask"),
             mv(3, "Маска 2", "2005-02-18", orig="Son of the Mask")]

    async def fake_find(st_, q):
        return cands
    mp.setattr(main, "find_info", fake_find)
    queries = []

    async def fake_jac(http, cfg, q):
        queries.append(q)
        return [rel("Маска / The Mask (1994) BDRip 1080p", "1" * 40),
                rel("Маска (шоу) / Сезон 4 (2023) WEB-DL 1080p", "2" * 40, series=True),
                rel("Маска 2 / Son of the Mask (2005) BDRip 1080p", "3" * 40),
                rel("Маска / The Mask (1994) HDTV 720p", "4" * 40)]
    mp.setattr(jacred, "search", fake_jac)

    await send(ALICE, "маска")
    _, text, kb = session.sent(ALICE)[-1]
    assert "что именно ищем" in text and "(1994)" in text and "(2020)" in text
    labels = [b.text for b in buttons(kb)]
    assert any("Маска (1994)" in t for t in labels) and any("как есть" in t for t in labels)
    film_btn = next(b for b in buttons(kb) if "Маска (1994)" in b.text)

    await press(ALICE, film_btn.callback_data)
    assert queries == ["Маска", "The Mask"]              # русское и оригинальное название
    _, listing, kb = session.sent(ALICE)[-1]
    assert "The Mask (1994) BDRip" in listing and "HDTV 720p" in listing
    assert "Сезон 4" not in listing and "Son of the Mask" not in listing
    # папка для Kodi — по выбранному фильму
    dl = next(b for b in buttons(kb) if b.text == "⬇ 1")
    await press(ALICE, dl.callback_data)
    t = next(iter(st.tr.torrents.values()))
    assert t["downloadDir"] == "/downloads/movies/Маска (1994)"


async def test_single_candidate_goes_straight(env):
    st, session, send, press, mp = env

    async def fake_find(st_, q):
        return [mv(9, "Дюна", "2021-09-15", orig="Dune")]
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel("Дюна / Dune (2021) WEB-DL 1080p", "5" * 40)]
    mp.setattr(jacred, "search", fake_jac)
    await send(ALICE, "дюна 2021")
    assert "Dune (2021) WEB-DL" in session.sent(ALICE)[-1][1]


async def test_no_exact_shows_all_with_warning(env):
    st, session, send, press, mp = env

    async def fake_find(st_, q):
        return [mv(9, "Редкий фильм", "1970-01-01")]
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel("Совсем другое название 1080p", "6" * 40)]
    mp.setattr(jacred, "search", fake_jac)
    await send(ALICE, "редкий фильм")
    text = session.sent(ALICE)[-1][1]
    assert "не нашёл" in text and "Совсем другое" in text


async def test_unknown_to_tmdb_falls_back_to_raw(env):
    st, session, send, press, mp = env

    async def fake_find(st_, q):
        return []
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel("Самодельное видео 1080p", "7" * 40)]
    mp.setattr(jacred, "search", fake_jac)
    await send(ALICE, "самодельное видео")
    assert "Самодельное видео" in session.sent(ALICE)[-1][1]


# ---------- поиск по актёру ----------
async def test_actor_filmography(env):
    st, session, send, press, mp = env
    person = {"id": 206, "media_type": "person", "name": "Джим Керри", "original_name": "Jim Carrey",
              "known_for_department": "Acting", "known_for": [{"title": "Маска"}]}

    async def fake_find(st_, q):
        return [person, mv(1, "Маска", "1994-07-29")]
    mp.setattr(main, "find_info", fake_find)

    async def fake_credits(http, key, pid, lang):
        assert pid == 206
        return {"cast": [
            {**mv(1, "Маска", "1994-07-29", votes=9000), "character": "Stanley"},
            {**mv(2, "Шоу Трумана", "1998-06-01", votes=18000), "character": "Truman"},
            {**mv(3, "Вечернее шоу", "2010-01-01", tv=True, votes=99999), "character": "Himself"},
            {**mv(4, "Ток-шоу", "2012-01-01", tv=True, votes=5000), "character": "Guest", "genre_ids": [10767]},
        ]}
    mp.setattr(tmdb, "person_credits", fake_credits)

    async def fake_details(http, key, pid, lang):
        return {"birthday": "1962-01-17", "deathday": None, "profile_path": "/jim.jpg"}
    mp.setattr(tmdb, "person_details", fake_details)
    await send(ALICE, "джим керри")
    kind, text, kb = session.sent(ALICE)[-1]
    assert "Джим Керри</b> (род. 1962)" in text and "играл" in text
    assert kind == "SendPhoto"                                   # фото актёра над кнопками
    assert [m for m in session.calls if type(m).__name__ == "SendPhoto"][-1].photo.endswith("/jim.jpg")
    labels = [b.text for b in buttons(kb)]
    assert labels[0].startswith("1. 🎬 Шоу Трумана") and labels[1].startswith("2. 🎬 Маска")
    assert not any("Вечернее" in t or "Ток-шоу" in t for t in labels)


# ---------- отмена закачки ----------
async def test_cancel_own_download_only(env):
    st, session, send, press, mp = env
    await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
    _, text, kb = session.sent(ALICE)[-1]
    assert "Поставил на закачку" in text
    assert buttons(kb)[0].callback_data == f"cx:{H1}"

    await press(BOB, f"cx:{H1}")                     # чужая — нельзя
    assert "только свою" in session.alerts()[-1]
    await press(BOB, f"cxy:{H1}")
    assert st.tr.removed == []

    await press(ALICE, f"cx:{H1}")                   # своя — спрашиваем подтверждение
    _, text, kb = session.sent(ALICE)[-1]
    assert "Отменить закачку" in text
    await press(ALICE, f"cxy:{H1}")
    assert st.tr.removed == [H1] and st.db.get(H1)["removed"] == 1
    assert "Отменено" in session.sent(ALICE)[-1][1]


async def test_admin_cancels_and_owner_is_notified(env):
    st, session, send, press, mp = env
    await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
    await send(ADMIN, "/status")
    _, text, kb = session.sent(ADMIN)[-1]
    assert "T-aaaa" in text
    assert [b.callback_data for b in buttons(kb)] == [f"tp:{H1}", f"tu:{H1}", f"cx:{H1}", "lr"]  # lr — /delete
    await send(BOB, "/status")                        # Бобу кнопку отмены чужого не показываем
    assert session.sent(BOB)[-1][2] is None
    await press(ADMIN, f"cxy:{H1}")
    assert st.tr.removed == [H1]
    assert any("Администратор отменил" in t for _, t, _ in session.sent(ALICE))


# ---------- пользователи ----------
async def test_access_requests_and_blocking(env):
    st, session, send, press, mp = env
    stranger = 300
    await send(stranger, "/start")
    assert any("Запрос доступа" in t for _, t, _ in session.sent(ADMIN))
    n_admin = len(session.sent(ADMIN))
    await send(stranger, "/start")                    # повторно админа не дёргаем
    assert "уже отправлен" in session.sent(stranger)[-1][1]
    assert len(session.sent(ADMIN)) == n_admin

    await send(ADMIN, "/users")
    _, text, kb = session.sent(ADMIN)[-1]
    assert "Алиса" in text and "Ждут подтверждения" in text and "u300" in text
    await press(ADMIN, f"ubl:{stranger}")
    assert st.db.is_blocked(stranger) and not st.db.requests()
    await send(stranger, "/start")
    assert session.sent(stranger)[-1][1] == "Доступ закрыт."
    await send(stranger, "маска")                     # заблокированному — молчим
    assert session.sent(stranger)[-1][1] == "Доступ закрыт."

    await press(ADMIN, f"urv:{BOB}")
    assert not st.is_allowed(BOB)
    await press(ADMIN, f"uub:{stranger}")
    assert not st.db.is_blocked(stranger)
    await press(ALICE, "uref")                        # не админ — нельзя
    assert "администратора" in session.alerts()[-1]


async def test_users_view_stats(env):
    st, session, send, press, mp = env
    await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
    text, kb = await main.users_view(st)
    alice = text.split("Алиса")[1].split("Боб")[0]
    assert "закачек: 1" in alice and "на диске: 953.7 МБ" in alice
    assert "был: сегодня" in alice


# ---------- понятные названия в /status и «Скачано» ----------
def test_fmt_eta():
    assert main.fmt_eta(30) == "~1 мин" and main.fmt_eta(45 * 60) == "~45 мин"
    assert main.fmt_eta(717 * 60) == "~11 ч" and main.fmt_eta(90 * 60) == "~1 ч 30 мин"
    assert main.fmt_eta(1961 * 60) == "~1 д 8 ч" and main.fmt_eta(48 * 3600) == "~2 д"


def test_nice_name():
    class C:
        dir_movies, dir_series = "/downloads/movies", "/downloads/series"
    row = {"category": "series", "title": "Задача трёх тел / 3 Body Problem [S01] (2024) WEB-DL 1080p"}
    t = {"name": "3 Body Problem (Season 1) WEB-DL 1080p", "downloadDir": "/downloads/series"}
    assert main.nice_name(C, t, row) == ("Задача трёх тел", True)               # из заголовка раздачи
    t2 = {**t, "downloadDir": "/downloads/series/Задача трёх тел (2024)/"}
    assert main.nice_name(C, t2, row) == ("Задача трёх тел (2024)", True)        # папка по TMDB — лучше
    t3 = {"name": "Shell.2024.mkv", "downloadDir": "/downloads/movies"}
    assert main.nice_name(C, t3, None) == ("Shell.2024.mkv", False)             # добавлено мимо бота
    assert main.nice_name(C, t3, {"category": "movies", "title": "magnet-ссылка"}) == ("Shell.2024.mkv", False)
    assert main.nice_name(C, {"name": "x", "downloadDir": "/downloads/series"}, None)[1] is True


async def test_label_saved_on_download(env):
    st, session, send, press, mp = env
    await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
    assert st.db.get(H1)["label"] is None                          # magnet без TMDB — без названия
    st.db.add_download("c" * 40, "x", "y", "movies", ALICE, ALICE, None, "Троя (2004)")
    t = {"name": "Troy.mkv", "downloadDir": st.cfg.dir_movies}      # папка общая, но название запомнено
    assert main.nice_name(st.cfg, t, st.db.get("c" * 40)) == ("Троя (2004)", False)


async def test_status_shows_nice_name_and_owner(env):
    st, session, send, press, mp = env
    await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
    st.tr.torrents[H1]["downloadDir"] = st.cfg.dir_movies.rstrip("/") + "/Маска (1994)"
    st.tr.torrents[H1]["eta"] = 717 * 60
    await send(BOB, "/status")
    text = session.sent(BOB)[-1][1]
    assert "🎬 <b>Маска (1994)</b> · 👤 Алиса" in text
    assert "<i>T-aaaa</i>" in text and "~11 ч" in text


async def test_series_without_tmdb_gets_own_folder(env):
    """Сериал, не узнанный в TMDB, всё равно кладём в свою папку — иначе Kodi его не опознает."""
    st, session, send, press, mp = env

    async def fake_find(st_, q):
        return []
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel("Во все тяжкие / Breaking Bad / Сезон: 1 [2008, BDRip 1080p]", "7" * 40, series=True)]
    mp.setattr(jacred, "search", fake_jac)
    await send(ALICE, "во все тяжкие")
    dl = next(b for b in buttons(session.sent(ALICE)[-1][2]) if b.text == "⬇ 1")
    await press(ALICE, dl.callback_data)
    assert st.tr.torrents["7" * 40]["downloadDir"] == "/downloads/series/Во все тяжкие"
    assert st.db.get("7" * 40)["label"] == "Во все тяжкие"



async def test_actor_without_photo_still_works(env):
    st, session, send, press, mp = env
    person = {"id": 7, "media_type": "person", "name": "Иван Петров", "known_for_department": "Directing"}

    async def fake_find(st_, q):
        return [person]
    mp.setattr(main, "find_info", fake_find)

    async def fake_credits(http, key, pid, lang):
        return {"crew": [{**mv(1, "Фильм", "2001-01-01", votes=500), "job": "Director"}]}
    mp.setattr(tmdb, "person_credits", fake_credits)

    async def broken_details(http, key, pid, lang):
        raise RuntimeError("TMDB упал")
    mp.setattr(tmdb, "person_details", broken_details)
    await send(ALICE, "иван петров")
    kind, text, kb = session.sent(ALICE)[-1]
    assert "Иван Петров</b> — самое известное, где снял" in text and kind != "SendPhoto"


def test_person_years():
    assert tmdb.person_years({"birthday": "1930-05-31", "deathday": "2008-01-01"}) == "1930–2008"
    assert tmdb.person_years({"birthday": "1962-01-17"}) == "род. 1962"
    assert tmdb.person_years({}) == ""



async def test_admin_notified_when_user_adds(env):
    st, session, send, press, mp = env
    n_admin = len(session.sent(ADMIN))
    await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
    new = session.sent(ADMIN)[n_admin:]
    assert len(new) == 1 and "📥 <b>Алиса</b> поставил(а) на закачку" in new[0][1]
    assert [b.callback_data for b in buttons(new[0][2])] == [f"cx:{H1}"]      # можно сразу отменить
    await press(ADMIN, f"cx:{H1}")                                             # админ может отменить чужую
    assert "Отменить закачку" in session.sent(ADMIN)[-1][1]
    n_admin = len(session.sent(ADMIN))
    await send(ADMIN, "magnet:?xt=urn:btih:" + "e" * 40)                       # свои — без уведомления
    assert not any("📥" in t for _, t, _ in session.sent(ADMIN)[n_admin:])


async def test_admin_add_notice_can_be_disabled(env):
    st, session, send, press, mp = env
    object.__setattr__(st.cfg, "notify_adds", False)        # Config неизменяемый — только для теста
    try:
        n_admin = len(session.sent(ADMIN))
        await send(ALICE, f"magnet:?xt=urn:btih:{H1}")
        assert not any("📥" in t for _, t, _ in session.sent(ADMIN)[n_admin:])
    finally:
        object.__setattr__(st.cfg, "notify_adds", True)
