"""/delete: список, карточка, подтверждение, права, сезоны, безопасность путей."""
import itertools
import os
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")

from bot import extras, library, main  # noqa: E402
from bot.config import load  # noqa: E402
from bot.db import DB  # noqa: E402
from test_flows import FakeSession, buttons  # noqa: E402

ADMIN, ALICE, BOB = 1, 100, 200
_ids = itertools.count(50000)
MB = 1024 ** 2


def put(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


class DiskTr:
    """Transmission, который при удалении с данными действительно удаляет файлы."""
    def __init__(self):
        self.torrents, self.removed = {}, []

    def seed(self, h, download_dir, name, done=1.0):
        self.torrents[h] = {"hashString": h, "name": name, "percentDone": done, "downloadDir": download_dir,
                            "totalSize": library.tree_size(f"{download_dir}/{name}"), "rateDownload": 0,
                            "eta": -1, "status": 6}

    async def get(self, hashes=None):
        return [t for h, t in self.torrents.items() if hashes is None or h in hashes]

    async def remove(self, h, delete_data=True):
        t = self.torrents.pop(h)
        self.removed.append(h)
        if delete_data:
            library.remove_path(library.torrent_root(t))

    async def free_space(self, path):
        return 100 * 1024 ** 3


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "media"
    movies, series = root / "movies", root / "series"
    movies.mkdir(parents=True)
    series.mkdir()
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    monkeypatch.setenv("DIR_MOVIES", str(movies))
    monkeypatch.setenv("DIR_SERIES", str(series))
    monkeypatch.delenv("KODI_URL", raising=False)
    cfg = load()
    tr = DiskTr()
    st = main.State(cfg, DB(str(tmp_path / "db.sqlite3")), tr, None)
    st.db.allow(ALICE, "Алиса (@alice)")
    st.db.allow(BOB, "Боб")
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

    # на диске: фильм бота (папка), фильм, положенный руками (файл), сериал бота с двумя сезонами
    put(f"{movies}/Маска (1994)/The.Mask.1994.BDRip.mkv", 30 * MB)
    tr.seed("a" * 40, f"{movies}/Маска (1994)", "The.Mask.1994.BDRip.mkv")
    st.db.add_download("a" * 40, "The.Mask.1994.BDRip.mkv", "Маска / The Mask (1994)", "movies", ALICE, ALICE,
                       None, "Маска (1994)")
    put(f"{movies}/Old.Movie.avi", 5 * MB)
    put(f"{series}/Во все тяжкие/Breaking.Bad.S01/e01.mkv", 20 * MB)
    put(f"{series}/Во все тяжкие/Breaking.Bad.S02/e01.mkv", 25 * MB)
    tr.seed("b" * 40, f"{series}/Во все тяжкие", "Breaking.Bad.S01")
    tr.seed("c" * 40, f"{series}/Во все тяжкие", "Breaking.Bad.S02")
    st.db.add_download("b" * 40, "Breaking.Bad.S01", "Во все тяжкие S01", "series", BOB, BOB, None, "Во все тяжкие")
    return st, session, send, press, tr, str(movies), str(series)


def last(session, uid):
    return session.sent(uid)[-1]


def open_card(session, uid, name):
    """callback_data карточки по имени в списке /delete (номера зависят от размеров)."""
    _, text, kb = last(session, uid)
    line = next(ln for ln in text.split("\n") if name in ln and ln.startswith("<b>"))
    n = int(line[3:line.index(".")])
    lid = next(b.callback_data for b in buttons(kb) if b.callback_data.startswith("li:")).split(":")[1]
    return f"li:{lid}:{n - 1}"


def btn(markup, part):
    return next(b for b in buttons(markup) if part in b.text)


# ---------- диск ----------
def test_scan_sorted_with_seasons(lib):
    st, *_ , movies, series = lib
    entries = library.scan(movies, series)
    assert [e.name for e in entries] == ["Во все тяжкие", "Маска (1994)", "Old.Movie.avi"]
    assert entries[0].size == 45 * MB and entries[0].kind == "series"
    assert [s.name for s in entries[0].seasons] == ["Breaking.Bad.S01", "Breaking.Bad.S02"]
    assert entries[2].label == "Old.Movie" and not entries[2].is_dir


def test_torrents_inner_outer():
    ts = [{"name": "S01", "downloadDir": "/d/series/Show"}, {"name": "Show", "downloadDir": "/d/series"},
          {"name": "Other", "downloadDir": "/d/series"}]
    inner, outer = library.torrents_for("/d/series/Show", ts)
    assert [t["name"] for t in inner] == ["S01", "Show"] and not outer
    inner, outer = library.torrents_for("/d/series/Show/S02", ts)
    assert not inner and [t["name"] for t in outer] == ["Show"]


def test_safe_target(tmp_path):
    m, s = str(tmp_path / "movies"), str(tmp_path / "series")
    os.makedirs(m)
    os.makedirs(s)
    assert library.safe_target(f"{m}/X", m, s) and library.safe_target(f"{s}/Show/S01", m, s)
    assert not library.safe_target(m, m, s)                       # саму папку фильмов — нельзя
    assert not library.safe_target(str(tmp_path / "etc"), m, s)
    assert not library.safe_target(f"{m}/..", m, s)
    os.symlink("/", f"{m}/root")                                  # ссылка наружу
    assert not library.safe_target(f"{m}/root/etc", m, s)


# ---------- права ----------
async def test_rights_default_admin_only_and_toggle(lib):
    st, session, send, press, tr, movies, series = lib
    await send(ALICE, "/delete")
    assert "Попроси администратора" in last(session, ALICE)[1]
    await press(ALICE, "lr")
    assert "только администратор" in session.alerts()[-1]
    await press(ALICE, f"udl:{ALICE}")                           # сама себе — нельзя
    assert not st.db.can_delete(ALICE)

    await send(ADMIN, "/users")
    assert "удалять: нельзя" in " ".join(b.text for b in buttons(last(session, ADMIN)[2]))
    await press(ADMIN, f"udl:{ALICE}")
    assert st.db.can_delete(ALICE)
    assert "/delete" in last(session, ALICE)[1]                   # ей пришла подсказка
    await send(ALICE, "/delete")
    assert "Удаление" in last(session, ALICE)[1]

    await press(ADMIN, f"urv:{ALICE}")                           # сняли доступ — пропало и право
    st.db.allow(ALICE, "Алиса")
    assert not st.db.can_delete(ALICE)


# ---------- удаление ----------
async def test_user_deletes_own_movie_admin_notified(lib):
    st, session, send, press, tr, movies, series = lib
    st.db.set_can_delete(ALICE, True)
    await send(ALICE, "/delete")
    _, text, kb = last(session, ALICE)
    assert "Во все тяжкие" in text and "👤 Боб" in text and "Old.Movie" in text
    assert text.index("Во все тяжкие") < text.index("Маска") < text.index("Old.Movie")   # большие сверху
    await press(ALICE, open_card(session, ALICE, "Маска"))
    _, card, kb = last(session, ALICE)
    assert "Маска (1994)" in card and "Алиса" in card and "30.0 МБ" in card
    await press(ALICE, btn(kb, "Удалить").callback_data)
    _, ask, kb = last(session, ALICE)
    assert "Точно удалить" in ask
    await press(ALICE, btn(kb, "Да").callback_data)
    assert "🗑 Удалено: <b>Маска (1994)</b>" in last(session, ALICE)[1]
    assert not os.path.exists(f"{movies}/Маска (1994)")          # и файл, и папка «Название (год)»
    assert tr.removed == ["a" * 40]
    assert st.db.get("a" * 40)["removed_reason"] == "delete"
    assert "Алиса</b> удалил(а): Маска (1994)" in last(session, ADMIN)[1]
    d = st.db.deletions_since(0)
    assert len(d) == 1 and d[0]["user_id"] == ALICE and d[0]["size"] == 30 * MB
    report = await extras.weekly_report(st)
    assert "Удалено через /delete: 1" in report and "Маска (1994) — Алиса" in report
    assert "вручную" not in report


async def test_admin_deletes_manual_file_no_notify(lib):
    st, session, send, press, tr, movies, series = lib
    await send(ADMIN, "/delete")
    await press(ADMIN, open_card(session, ADMIN, "Old.Movie"))
    card = last(session, ADMIN)[1]
    assert "не через бота" in card
    await press(ADMIN, btn(last(session, ADMIN)[2], "Удалить").callback_data)
    n = len(session.calls)
    await press(ADMIN, btn(last(session, ADMIN)[2], "Да").callback_data)
    assert not os.path.exists(f"{movies}/Old.Movie.avi")
    assert not tr.removed
    assert not any("удалил(а)" in t for _, t, _ in session.sent()[n:] if "Удалено" not in t)


async def test_delete_one_season_then_last_prunes_show(lib):
    st, session, send, press, tr, movies, series = lib
    await send(ADMIN, "/delete")
    await press(ADMIN, open_card(session, ADMIN, "Во все тяжкие"))
    kb = last(session, ADMIN)[2]
    assert "сериал целиком" in btn(kb, "целиком").text
    await press(ADMIN, btn(kb, "S01").callback_data)
    assert "Во все тяжкие → Breaking.Bad.S01" in last(session, ADMIN)[1]
    await press(ADMIN, btn(last(session, ADMIN)[2], "Да").callback_data)
    assert not os.path.exists(f"{series}/Во все тяжкие/Breaking.Bad.S01")
    assert os.path.exists(f"{series}/Во все тяжкие/Breaking.Bad.S02")
    assert tr.removed == ["b" * 40]
    assert "удалил(а) скачанное" in last(session, BOB)[1]         # хозяину сезона сообщили
    # второй (последний) сезон — вместе с ним уходит и пустая папка сериала
    await press(ADMIN, "lr")
    await press(ADMIN, open_card(session, ADMIN, "Во все тяжкие"))
    kb = last(session, ADMIN)[2]
    assert not any("S02" in b.text for b in buttons(kb))          # сезон один — отдельной кнопки нет
    await press(ADMIN, btn(kb, "Удалить").callback_data)
    await press(ADMIN, btn(last(session, ADMIN)[2], "Да").callback_data)
    assert not os.path.exists(f"{series}/Во все тяжкие") and os.path.isdir(series)
    assert os.path.exists(f"{movies}/Маска (1994)")


async def test_part_of_bigger_torrent_refused(lib):
    st, session, send, press, tr, movies, series = lib
    put(f"{series}/Сериал/Season 1/e1.mkv", 3 * MB)
    put(f"{series}/Сериал/Season 2/e1.mkv", 3 * MB)
    tr.seed("d" * 40, series, "Сериал")                          # одна раздача на весь сериал
    await send(ADMIN, "/delete")
    await press(ADMIN, open_card(session, ADMIN, "Сериал"))
    kb = last(session, ADMIN)[2]
    assert not any("Season" in b.text for b in buttons(kb))       # сезоны отдельно не предлагаем
    lid, idx = btn(kb, "целиком").callback_data.split(":")[1:3]
    await press(ADMIN, f"ly:{lid}:{idx}:0")                      # а если всё же нажать старую кнопку
    assert "часть большей раздачи" in session.alerts()[-1]
    assert os.path.exists(f"{series}/Сериал/Season 1") and "d" * 40 in tr.torrents


async def test_stale_buttons_reload(lib):
    st, session, send, press, tr, movies, series = lib
    await press(ADMIN, "li:deadbeef:0")
    assert "Удаление" in last(session, ADMIN)[1]
    await press(ADMIN, "ly:deadbeef:0:-1")
    assert "устарел" in session.alerts()[-1]
    assert os.path.exists(f"{movies}/Old.Movie.avi")


async def test_status_has_delete_button(lib):
    st, session, send, press, tr, movies, series = lib
    await send(ADMIN, "/status")
    assert any(b.callback_data == "lr" for b in buttons(last(session, ADMIN)[2]))
    await send(BOB, "/status")
    assert not any(b.callback_data == "lr" for b in buttons(last(session, BOB)[2]))
