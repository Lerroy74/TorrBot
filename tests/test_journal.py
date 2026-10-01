"""v6.3: актёры у вариантов поиска; журнал «что смотрели» с оценками (/ocenki)."""
import os

from bot import db as dbmod, main, tmdb
from bot.db import DB
from test_delete import ADMIN, ALICE, BOB, btn, last, lib, open_card  # noqa: F401
from test_flows import buttons, env, mv  # noqa: F401


# ---------- актёры ----------
def test_top_cast_order_and_dedup():
    credits = {"cast": [{"name": "C", "order": 2}, {"name": "A", "order": 0}, {"name": "B", "order": 1},
                        {"name": "A", "order": 5}, {"name": "D", "order": 3}]}
    assert tmdb.top_cast(credits) == ["A", "B", "C"]
    assert tmdb.top_cast({}) == []
    assert tmdb.surname("Jim Carrey") == "Carrey"


async def test_same_titles_get_actors(env):
    st, session, send, press, mp = env
    cands = [mv(1, "Маска", "1994-07-29", orig="The Mask"), mv(2, "Маска", "1994-03-01", orig="Maska")]

    async def fake_find(st_, q):
        return cands
    mp.setattr(main, "find_info", fake_find)
    calls = []

    async def fake_cast(http, key, info, lang="ru-RU", n=3):
        calls.append(info.tmdb_id)
        return {1: ["Jim Carrey", "Cameron Diaz", "Peter Riegert"], 2: ["Иван Петров"]}[info.tmdb_id]
    mp.setattr(tmdb, "cast", fake_cast)

    await send(ALICE, "маска")
    _, text, kb = session.sent(ALICE)[-1]
    assert "👥 Jim Carrey, Cameron Diaz, Peter Riegert" in text and "👥 Иван Петров" in text
    labels = [b.text for b in buttons(kb)]
    assert "1. 🎬 Маска (1994) · Carrey" in labels and "2. 🎬 Маска (1994) · Петров" in labels
    await send(ALICE, "маска")                     # второй раз — из кэша, без запросов
    assert sorted(calls) == [1, 2]


async def test_cast_failure_does_not_break_search(env):
    st, session, send, press, mp = env

    async def fake_find(st_, q):
        return [mv(1, "Маска", "1994-07-29"), mv(3, "Маска 2", "2005-02-18")]
    mp.setattr(main, "find_info", fake_find)

    async def boom(*a, **kw):
        raise RuntimeError("tmdb down")
    mp.setattr(tmdb, "cast", boom)
    await send(ALICE, "маска")
    _, text, kb = session.sent(ALICE)[-1]
    assert "что именно ищем" in text and "👥" not in text
    assert "1. 🎬 Маска (1994)" in [b.text for b in buttons(kb)]


# ---------- журнал ----------
def test_journal_key_and_dedup(tmp_path):
    d = DB(str(tmp_path / "j.sqlite3"))
    assert dbmod.journal_key("Маска (1994)") == dbmod.journal_key("маска 1994")
    a = d.journal_note("movies", "Маска (1994)", None, ALICE)
    b = d.journal_note("movies", "маска 1994", "/p.jpg", BOB)
    assert a == b and d.journal_get(a)["poster"] == "/p.jpg" and d.journal_get(a)["added_by"] == ALICE
    assert d.journal_note("movies", "  ") is None


def test_backfill_from_old_downloads(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    d = DB(path)
    d.c.execute("DELETE FROM journal")
    d.add_download("a" * 40, "x", "Маска / The Mask (1994) BDRip", "movies", 1, ALICE, None, None)
    d.add_download("b" * 40, "y", "Во все тяжкие", "series", 1, BOB, None, "Во все тяжкие")
    d.add_download("c" * 40, "z", "Отменённое", "movies", 1, BOB)       # не докачано — не в журнал
    d.mark_done("a" * 40)
    d.mark_done("b" * 40)
    d.mark_removed("a" * 40, "cleanup")
    # имитируем базу v6.2: без колонки jid
    d.c.execute("ALTER TABLE downloads DROP COLUMN jid")
    d.c.commit()
    d2 = DB(path)
    rows = {r["label"]: r for r in d2.journal()}
    assert set(rows) == {"Маска", "Во все тяжкие"}
    assert rows["Маска"]["deleted_at"] and not rows["Во все тяжкие"]["deleted_at"]
    assert d2.get("b" * 40)["jid"] == rows["Во все тяжкие"]["id"]


async def test_watcher_notes_finished(lib):
    st, session, send, press, tr, movies, series = lib
    t = tr.torrents["a" * 40]
    row = st.db.get("a" * 40)
    name, series_ = main.nice_name(st.cfg, t, row)
    st.db.journal_note("movies", name, None, row["user_id"], h=row["hash"])
    j = st.db.journal()
    assert [r["label"] for r in j] == ["Маска (1994)"] and j[0]["added_by"] == ALICE
    assert st.db.get("a" * 40)["jid"] == j[0]["id"]


async def test_delete_asks_rating_and_ocenki(lib):
    st, session, send, press, tr, movies, series = lib
    st.db.journal_note("movies", "Маска (1994)", None, ALICE, h="a" * 40)
    st.db.set_can_delete(ALICE, True)
    await send(ALICE, "/delete")
    await press(ALICE, open_card(session, ALICE, "Маска"))
    await press(ALICE, btn(last(session, ALICE)[2], "Удалить").callback_data)
    await press(ALICE, btn(last(session, ALICE)[2], "Да").callback_data)
    _, ask, kb = last(session, ALICE)
    assert "Как вам" in ask and "Маска (1994)" in ask
    assert [b.text for b in buttons(kb)][:10] == [str(n) for n in range(1, 11)]
    await press(ALICE, btn(kb, "8").callback_data)
    assert "<b>8</b>/10" in last(session, ALICE)[1]
    j = st.db.journal()[0]
    assert st.db.rating_of(j["id"], ALICE)["score"] == 8 and j["deleted_at"]

    await send(BOB, "/ocenki")
    _, text, kb = last(session, BOB)
    assert "Что смотрели" in text and "Маска (1994) — ⭐ <b>8.0</b> (1)" in text
    assert "💾" not in text.split("Маска")[1].split("\n")[0]


async def test_manual_file_and_skip(lib):
    st, session, send, press, tr, movies, series = lib
    await send(ADMIN, "/delete")
    await press(ADMIN, open_card(session, ADMIN, "Old.Movie"))
    await press(ADMIN, btn(last(session, ADMIN)[2], "Удалить").callback_data)
    await press(ADMIN, btn(last(session, ADMIN)[2], "Да").callback_data)
    _, ask, kb = last(session, ADMIN)
    assert "Old.Movie" in ask
    await press(ADMIN, btn(kb, "Не смотрел").callback_data)
    assert "не смотрел(а)" in last(session, ADMIN)[1]
    j = st.db.journal()
    assert len(j) == 1 and j[0]["cnt"] == 0 and j[0]["deleted_at"]
    assert st.db.rating_of(j[0]["id"], ADMIN)["score"] is None             # ответ записан


async def test_season_delete_keeps_series_on_disk(lib):
    st, session, send, press, tr, movies, series = lib
    st.db.journal_note("series", "Во все тяжкие", None, BOB, h="b" * 40)
    await send(ADMIN, "/delete")
    await press(ADMIN, open_card(session, ADMIN, "Во все тяжкие"))
    await press(ADMIN, btn(last(session, ADMIN)[2], "S01").callback_data)
    await press(ADMIN, btn(last(session, ADMIN)[2], "Да").callback_data)
    assert "Во все тяжкие" in last(session, ADMIN)[1]          # оценку спрашиваем за сериал
    j = st.db.journal()
    assert len(j) == 1 and not j[0]["deleted_at"]                # второй сезон ещё на диске


async def test_ocenki_tabs_rate_from_list_and_admin_remove(lib):
    st, session, send, press, tr, movies, series = lib
    a = st.db.journal_note("movies", "Маска (1994)", None, ALICE)
    b = st.db.journal_note("series", "Во все тяжкие", None, BOB)
    st.db.rate(a, ALICE, 6)
    await send(ALICE, "/ocenki")
    _, text, kb = last(session, ALICE)
    assert "без оценок" in text and "ты: 6" in text and "✍ Мне оценить (1)" in [x.text for x in buttons(kb)]
    await press(ALICE, "jr:u:0")
    _, text, kb = last(session, ALICE)
    assert "Во все тяжкие" in text and "Маска" not in text
    await press(ALICE, btn(kb, "1").callback_data)                # карточка
    _, card, kb = last(session, ALICE)
    assert "Ты ещё не оценил" in card and not any("Убрать" in x.text for x in buttons(kb))
    await press(ALICE, next(x.callback_data for x in buttons(kb) if x.text == "10"))
    _, card, kb = last(session, ALICE)
    assert "Твоя оценка: <b>10</b>" in card and "Алиса 10" in card
    await press(ALICE, "jr:u:0")
    assert "Ты оценил(а) всё" in last(session, ALICE)[1]
    await press(ALICE, "jr:r:0")
    text = last(session, ALICE)[1]
    assert text.index("Во все тяжкие") < text.index("Маска")      # 10 выше 6

    await press(ALICE, f"jx:{a}:d:0")
    assert "администратора" in session.alerts()[-1]
    await press(ADMIN, f"jq:{a}:d:0")
    await press(ADMIN, btn(last(session, ADMIN)[2], "Убрать").callback_data)
    await press(ADMIN, btn(last(session, ADMIN)[2], "Да").callback_data)
    assert [r["id"] for r in st.db.journal()] == [b] and st.db.rating_of(a, ALICE) is None


async def test_everyone_rates_own_and_resets_only_own(lib):
    """v6.5: у каждого своя оценка, средняя по всем, «не смотрел» не считается, сброс — только свой."""
    st, session, send, press, tr, movies, series = lib
    a = st.db.journal_note("movies", "Маска (1994)", None, ALICE)
    for uid, n in ((ALICE, "8"), (BOB, "7")):
        await press(uid, f"jq:{a}:d:0")
        await press(uid, next(x.callback_data for x in buttons(last(session, uid)[2]) if x.text == n))
    await press(ADMIN, f"jq:{a}:d:0")
    await press(ADMIN, btn(last(session, ADMIN)[2], "Не смотрел").callback_data)
    _, card, kb = last(session, ADMIN)
    assert "⭐ <b>7.5</b> (2)" in card and "Алиса 8" in card and "Боб 7" in card and "не смотрел(а)" in card
    assert "Ты отметил(а): не смотрел(а)" in card
    await press(BOB, f"rt:{a}:0:d:0")                              # Боб сбрасывает свою
    assert st.db.rating_of(a, BOB) is None and st.db.rating_of(a, ALICE)["score"] == 8
    assert st.db.rating_summary(a) == (8.0, 1)
    await send(BOB, "/ocenki")
    assert "✍ Мне оценить (1)" in [x.text for x in buttons(last(session, BOB)[2])]


async def test_delete_asks_deleter_and_downloader(lib):
    """Удаляет Боб фильм Алисы: вопрос обоим; Боб может ответить «не смотрел», удаление уже прошло."""
    st, session, send, press, tr, movies, series = lib
    st.db.journal_note("movies", "Маска (1994)", None, ALICE, h="a" * 40)
    st.db.set_can_delete(BOB, True)
    await send(BOB, "/delete")
    await press(BOB, open_card(session, BOB, "Маска"))
    await press(BOB, btn(last(session, BOB)[2], "Удалить").callback_data)
    await press(BOB, btn(last(session, BOB)[2], "Да").callback_data)
    assert not os.path.exists(f"{movies}/Маска (1994)")
    assert "Как вам" in last(session, BOB)[1]
    alice = [t for _, t, _ in session.sent(ALICE)]
    assert any("Как вам" in t for t in alice) and any("удалил(а) скачанное" in t for t in alice)
    await press(BOB, btn(last(session, BOB)[2], "Не смотрел").callback_data)
    j = st.db.journal()[0]
    assert st.db.rating_of(j["id"], BOB)["score"] is None and j["deleted_at"]


def test_migration_from_family_rating(tmp_path):
    """База v6.4: семейная оценка переезжает в личную оценку того, кто ставил; делается копия базы."""
    path = str(tmp_path / "old.sqlite3")
    d = DB(path)
    a = d.journal_note("movies", "Маска (1994)", None, ALICE)
    d.c.execute("UPDATE journal SET rating=9, rated_by=? WHERE id=?", (BOB, a))
    d.c.execute("DROP TABLE ratings")
    d.c.execute("DROP TABLE rating_asks")
    d.c.commit()
    d2 = DB(path)
    assert d2.rating_of(a, BOB)["score"] == 9 and d2.rating_summary(a) == (9.0, 1)
    assert os.path.exists(path + ".bak-6.4")


async def test_ocenki_empty_and_not_allowed(lib):
    st, session, send, press, tr, movies, series = lib
    await send(ALICE, "/ocenki")
    assert "Пока пусто" in last(session, ALICE)[1]
    n = len(session.calls)
    await send(999, "/ocenki")
    assert not any("Что смотрели" in t for _, t, _ in session.sent(999))


# ---------- «◀ К вариантам» ----------
async def test_back_to_choices(env):
    st, session, send, press, mp = env
    from bot import jacred
    from test_flows import rel
    cands = [mv(1, "Маска", "1994-07-29", orig="The Mask"), mv(3, "Маска 2", "2005-02-18")]

    async def fake_find(st_, q):
        return cands
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        return [rel("Маска / The Mask (1994) BDRip 1080p", "1" * 40)] if "2" not in q else []
    mp.setattr(jacred, "search", fake_jac)

    await send(ALICE, "маска")
    _, choice, kb = session.sent(ALICE)[-1]
    await press(ALICE, next(b for b in buttons(kb) if "Маска (1994)" in b.text).callback_data)
    _, listing, kb = session.sent(ALICE)[-1]
    back = next(b for b in buttons(kb) if "К вариантам" in b.text)
    await press(ALICE, back.callback_data)
    _, again, kb2 = session.sent(ALICE)[-1]
    assert again == choice and any("Маска 2" in b.text for b in buttons(kb2))
    # второй вариант: раздач нет — кнопка «назад» всё равно есть
    await press(ALICE, next(b for b in buttons(kb2) if "Маска 2" in b.text).callback_data)
    _, none, kb3 = session.sent(ALICE)[-1]
    assert "не нашлось" in none and any("К вариантам" in b.text for b in buttons(kb3))
    await press(ALICE, "bk:deadbeef")
    assert "устарел" in session.alerts()[-1]


# ---------- «✅ Посмотрели на ПК» ----------
K = "smb://192.168.1.30/media"


class PcKodi:
    def __init__(self, items):
        self.items, self.calls = items, []

    async def videos(self):
        return self.items

    async def call(self, method, params=None):
        self.calls.append((method, params))

    async def mark_watched(self, items, when):
        from bot.kodi import Kodi
        return await Kodi.mark_watched(self, items, when)

    async def clean(self):
        pass


def kodi_for(st, movies_dir):
    rel_ = movies_dir[len(st.cfg.media_root.rstrip("/")):]
    return PcKodi([
        {"movieid": 7, "file": f"{K}{rel_}/Маска (1994)/The.Mask.1994.BDRip.mkv", "playcount": 0,
         "lastplayed": "", "resume": {"position": 300}},
        {"movieid": 8, "file": f"{K}{rel_}/Other.mkv", "playcount": 0, "lastplayed": "", "resume": {}}])


async def test_pc_from_delete_card(lib):
    st, session, send, press, tr, movies, series = lib
    st.kodi = kodi_for(st, movies)
    st.db.journal_note("movies", "Маска (1994)", None, ALICE, h="a" * 40)
    await send(ADMIN, "/delete")
    await press(ADMIN, open_card(session, ADMIN, "Маска"))
    _, card, kb = last(session, ADMIN)
    assert "Начали смотреть" in card
    await press(ADMIN, btn(kb, "Посмотрели на ПК").callback_data)
    assert st.kodi.calls == [("VideoLibrary.SetMovieDetails",
                              {"movieid": 7, "playcount": 1, "lastplayed": st.kodi.calls[0][1]["lastplayed"],
                               "resume": {"position": 0, "total": 0}})]
    _, text, kb = last(session, ADMIN)
    assert "Отметил в Kodi как просмотренное" in text and "Удалить" in text and "сейчас" in text
    assert "Как вам" not in text and "Положено не через бота" not in text
    await press(ADMIN, btn(kb, "Да, удалить").callback_data)      # v6.4.1: отметка → сразу удаление
    assert not os.path.exists(f"{movies}/Маска (1994)") and tr.removed == ["a" * 40]
    texts = [t for _, t, _ in session.sent(ADMIN)]
    assert any("🗑 Удалено" in t for t in texts) and "Как вам" in texts[-1]
    assert sum("Как вам" in t for t in texts) == 1                 # оценку спросили один раз


async def test_pc_from_delete_card_keep(lib):
    st, session, send, press, tr, movies, series = lib
    st.kodi = kodi_for(st, movies)
    await send(ADMIN, "/delete")
    await press(ADMIN, open_card(session, ADMIN, "Маска"))
    await press(ADMIN, btn(last(session, ADMIN)[2], "Посмотрели на ПК").callback_data)
    await press(ADMIN, btn(last(session, ADMIN)[2], "Оставить").callback_data)
    texts = [t for _, t, _ in session.sent(ADMIN)]
    assert "Как вам" in texts[-1] and "Маска (1994)" in texts[-2]
    assert os.path.exists(f"{movies}/Маска (1994)") and not tr.removed


async def test_pc_from_done_message_and_ocenki(lib):
    st, session, send, press, tr, movies, series = lib
    st.kodi = kodi_for(st, movies)
    jid = st.db.journal_note("movies", "Маска (1994)", None, ALICE, h="a" * 40)
    await press(ALICE, "pw:" + "a" * 40)
    assert st.kodi.calls and st.kodi.calls[0][1]["movieid"] == 7
    assert "Как вам" in last(session, ALICE)[1]
    st.kodi.calls.clear()
    await press(ALICE, f"jq:{jid}:d:0")
    await press(ALICE, btn(last(session, ALICE)[2], "Посмотрели на ПК").callback_data)
    assert st.kodi.calls[0][1]["movieid"] == 7 and "Отметил" in last(session, ALICE)[1]
    await press(ALICE, "pw:" + "f" * 40)                         # чужой/несуществующий хэш
    assert "нет на диске" in session.alerts()[-1]


async def test_pc_not_in_library(lib):
    st, session, send, press, tr, movies, series = lib
    st.kodi = PcKodi([])
    n, note = await __import__("bot.journal", fromlist=["x"]).mark_on_pc(st, [f"{movies}/Маска (1994)"])
    assert n == 0 and "нет" in note and not st.kodi.calls


async def test_ocenki_sorts_all_and_mine(lib):
    """v6.5.1: «По средней» — весь список, без оценок внизу; «По моей» — мои лучшие сверху."""
    st, session, send, press, tr, movies, series = lib
    a = st.db.journal_note("movies", "Альфа (2001)", None, ALICE)
    b = st.db.journal_note("movies", "Бета (2002)", None, ALICE)
    c = st.db.journal_note("movies", "Гамма (2003)", None, ALICE)
    st.db.rate(a, BOB, 6)
    st.db.rate(b, BOB, 9)
    st.db.rate(a, ALICE, 10)
    st.db.rate(b, ALICE, 5)
    await send(ALICE, "/ocenki")
    labels = [x.text for x in buttons(last(session, ALICE)[2])]
    assert "👤 По моей" in labels and "⭐ По средней" in labels
    await press(ALICE, "jr:r:0")
    text = last(session, ALICE)[1]
    assert text.index("Альфа") < text.index("Бета") < text.index("Гамма")   # 8.0, 7.0, без оценок внизу
    await press(ALICE, "jr:m:0")
    text = last(session, ALICE)[1]
    assert text.index("Альфа") < text.index("Бета") < text.index("Гамма")   # мои: 10, 5, нет
    await press(BOB, "jr:m:0")
    text = last(session, BOB)[1]
    assert text.index("Бета") < text.index("Альфа") < text.index("Гамма")   # у Боба 9, 6
    assert c


async def test_delete_sort_by_rating(lib):
    """v6.5.1: /delete — переключатель «по оценке»: худшие сверху, без оценок в конце; запоминается."""
    st, session, send, press, tr, movies, series = lib
    m = st.db.journal_note("movies", "Маска (1994)", None, ALICE, h="a" * 40)
    s_ = st.db.journal_note("series", "Во все тяжкие", None, BOB, h="b" * 40)
    st.db.rate(m, ALICE, 9)
    st.db.rate(s_, BOB, 3)
    await send(ADMIN, "/delete")
    _, text, kb = last(session, ADMIN)
    assert "большие сверху" in text and "⭐ 9.0 (1)" in text
    await press(ADMIN, btn(kb, "По оценке").callback_data)
    _, text, kb = last(session, ADMIN)
    assert text.index("Во все тяжкие") < text.index("Маска") < text.index("Old.Movie")
    assert "• ⭐ По оценке" in [x.text for x in buttons(kb)]
    await press(ADMIN, open_card(session, ADMIN, "Маска"))            # номера — по новому порядку
    assert "Маска (1994)" in last(session, ADMIN)[1]
    await send(ADMIN, "/delete")                                       # выбор запомнился
    assert "худшие по оценке сверху" in last(session, ADMIN)[1]
