"""v8: списки, группы, «поделиться», право «⬇ Качать», подбор, контроль ИИ, переход с 7.x."""
import os
import sqlite3
import time

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["TMDB_API_KEY"] = "k"

from test_flows import ADMIN, ALICE, BOB, buttons, env, mv, rel  # noqa: E402,F401

from bot import ai, aictl, jacred, lists, main, tmdb  # noqa: E402
from bot.db import DB  # noqa: E402

CAROL = 300          # новенькая: в бот пришла по приглашению


def info(i, title, date, tv=False, genres=None):
    d = mv(i, title, date, tv=tv)
    d["genre_ids"] = genres or [18]
    return tmdb._to_info(d)


def last(session, uid):
    """Последнее сообщение боту → человеку (всплывающие ответы на кнопки не считаются)."""
    return [m for m in session.sent(uid) if m[0] != "AnswerCallbackQuery"][-1]


def btn(markup, part):
    return next(b.callback_data for b in buttons(markup) if part in b.text)


async def no_cast(st_, infos, timeout=6):
    return None


# ---------- переход с 7.1 ----------
def test_migration_from_v71(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    c = sqlite3.connect(path)                      # база как у 7.1 (без таблиц v8)
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, added_at INTEGER);
        CREATE TABLE prefs (user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (user_id, key));
        CREATE TABLE wishlist (kind TEXT, tmdb_id INTEGER, title TEXT, year TEXT, poster TEXT, added_by INTEGER,
                               added_at INTEGER, PRIMARY KEY (kind, tmdb_id));
        CREATE TABLE votes (kind TEXT, tmdb_id INTEGER, user_id INTEGER, PRIMARY KEY (kind, tmdb_id, user_id));
        INSERT INTO users VALUES (100, 'Алиса', 1), (200, 'Боб', 2);
        INSERT INTO wishlist VALUES ('m', 603, 'Матрица', '1999', NULL, 100, 10), ('t', 1399, 'Игра престолов', '2011', NULL, 200, 20);
        INSERT INTO votes VALUES ('m', 603, 100), ('m', 603, 200), ('t', 1399, 200);
    """)
    c.commit()
    c.close()
    db = DB(path)
    assert db.flag(100, "can_dl") and db.flag(200, "can_dl")          # старые пользователи качают как раньше

    class St:
        pass
    st = St()
    st.db = db
    st.cfg = type("C", (), {"admin_ids": (1,)})()
    lists.setup(st)
    groups = db.groups_of(1)
    assert [g["name"] for g in groups] == ["Семья"] and [m["user_id"] for m in db.group_members(groups[0]["id"])] == [1]
    lid = db.group_main_list(groups[0]["id"])
    items = db.list_items(lid)
    assert [(i["title"], i["votes"]) for i in items] == [("Матрица", 2), ("Игра престолов", 1)]
    lists.setup(st)                                                    # второй раз — ничего не дублирует
    assert len(db.groups_of(1)) == 1
    db.allow(300, "Новенькая")
    assert not db.flag(300, "can_dl")                                  # новые — без права качать


def test_journal_gets_tmdb_from_downloads(tmp_path):
    db = DB(str(tmp_path / "j.sqlite3"))
    db.add_download("h" * 40, "T", "Матрица 1999", "movies", 100, 100, tmdb_kind="m", tmdb_id=603)
    jid = db.journal_note("movies", "Матрица (1999)", None, 100, h="h" * 40, tmdb_kind="m", tmdb_id=603)
    assert db.journal_get(jid)["tmdb_id"] == 603
    # оценили из списка раньше, чем скачали, — та же запись журнала
    j2 = db.journal_for_title("m", 155, "Тёмный рыцарь (2008)")
    assert db.journal_get(j2)["src"] == "list"
    db.add_download("g" * 40, "T2", "Тёмный рыцарь", "movies", 100, 100, tmdb_kind="m", tmdb_id=155)
    assert db.journal_note("movies", "Тёмный рыцарь (2008)", None, 100, h="g" * 40, tmdb_kind="m", tmdb_id=155) == j2
    assert db.journal_get(j2)["src"] is None and db.journal_get(j2)["deleted_at"] is None


# ---------- группы и приглашения ----------
async def test_group_invite_flow(env):
    st, session, send, press, mp = env
    await send(ALICE, "/gruppy")
    await press(ALICE, "Gn")
    await send(ALICE, "Киноклуб")                                     # название ловится, а не ищется
    text = last(session, ALICE)[1]
    assert "Группа создана" in text and "Киноклуб" in text
    g = st.db.groups_of(ALICE)[0]
    code = g["invite"]
    assert f"g_{code}" in text

    # Боб уже в боте — сразу вступает, Алисе приходит сообщение
    await send(BOB, f"/start g_{code}")
    assert st.db.is_member(g["id"], BOB) and "Ты в группе «Киноклуб»" in last(session, BOB)[1]
    assert "вступил(а)" in last(session, ALICE)[1]

    # новенькая — заявка админу с пометкой; после ✅ — сразу в группе, но без права качать
    await send(CAROL, f"/start g_{code}")
    req = last(session, ADMIN)[1]
    assert "Запрос доступа" in req and "приглашение в группу «Киноклуб» от Алиса" in req
    await press(ADMIN, f"ok:{CAROL}")
    assert st.db.is_member(g["id"], CAROL) and not st.hooks["may_download"](CAROL)
    assert any("Ты в группе «Киноклуб»" in t for _, t, _ in session.sent(CAROL))

    # новая ссылка — старая не работает
    await press(ALICE, f"Gi:{g['id']}")
    await send(BOB, f"/start g_{code}")
    assert "устарела" in last(session, BOB)[1]

    # владелец уходит — группа к самому давнему участнику (Боб); последний — группа удаляется
    await press(ALICE, f"Gl:{g['id']}")
    assert "перейдёт к Боб" in last(session, ALICE)[1]
    await press(ALICE, f"GL:{g['id']}")
    assert st.db.group_get(g["id"])["owner_id"] == BOB
    assert "владелец" in last(session, BOB)[1]
    await press(BOB, f"GK:{g['id']}:{CAROL}")                          # владелец убирает участника
    assert not st.db.is_member(g["id"], CAROL)
    await press(BOB, f"GL:{g['id']}")
    assert st.db.group_get(g["id"]) is None


async def test_admin_adds_bot_user_to_group(env):
    st, session, send, press, mp = env
    gid = st.db.group_create("Семья", ADMIN)
    await press(ADMIN, f"Ga:{gid}")
    assert "Кого добавить" in last(session, ADMIN)[1]
    await press(ADMIN, f"GA:{gid}:{BOB}")
    assert st.db.is_member(gid, BOB) and "добавили в группу «Семья»" in last(session, BOB)[1]
    await press(ALICE, f"Ga:{gid}")                                    # не админ — нельзя
    assert "администратора" in session.alerts()[-1] and not st.db.is_member(gid, ALICE)


# ---------- списки ----------
async def test_group_list_votes_rights_and_seen_marks(env):
    st, session, send, press, mp = env
    gid = st.db.group_create("Семья", ALICE)
    st.db.group_join(gid, BOB)
    lid = st.db.group_main_list(gid)
    for i, t, d in ((1, "Матрица", "1999-03-31"), (2, "Амели", "2001-04-25")):
        lists.remember_info(st, info(i, t, d))
    await press(ALICE, f"Lt:{lid}:m:1")
    await press(BOB, f"Lt:{lid}:m:2")
    await press(ALICE, f"Lu:{lid}:m:2:0:0")                            # 👍 Амели от Алисы → 2 голоса, выше
    text = list(st.db.list_items(lid))
    assert [i["title"] for i in text] == ["Амели", "Матрица"] and text[0]["votes"] == 2

    # Боб не может убрать фильм Алисы, Алиса (владелец) — может любой
    await press(BOB, f"Ly:{lid}:m:1:0:0")
    assert "Убрать может" in session.alerts()[-1] and st.db.list_item(lid, "m", 1)

    # Боб оценил Матрицу из карточки → в группе «видели: Боб 9», но не ✅ для всех
    await press(BOB, f"Lq:{lid}:m:1:0:0:9")
    assert not st.db.list_item(lid, "m", 1)["watched_at"]
    await press(ALICE, f"Lv:{lid}:0:0")
    assert "видели: Боб 9" in last(session, ALICE)[1]
    # ✅ кнопкой — скрывается из списка, видно с «👁»
    await press(ALICE, f"Lw:{lid}:m:1:0:0")
    assert st.db.list_item(lid, "m", 1)["watched_at"]
    await press(ALICE, f"Lv:{lid}:0:0")
    assert "Матрица" not in last(session, ALICE)[1]
    await press(ALICE, f"Lv:{lid}:0:1")
    assert "✅ 🎬 Матрица" in last(session, ALICE)[1]
    await press(ALICE, f"Ly:{lid}:m:1:0:0")
    assert st.db.list_item(lid, "m", 1) is None

    # чужие не видят групповой список
    await press(ADMIN, f"Lv:{lid}:0:0")
    assert "не доступен" in last(session, ADMIN)[1]


async def test_personal_list_rating_marks_watched_and_podborka(env):
    st, session, send, press, mp = env
    lists.remember_info(st, info(5, "Дюна", "2021-09-15"))
    lid = st.db.main_list(ALICE)
    await press(ALICE, f"Lt:{lid}:m:5")
    await press(ALICE, "Lr:0:m:5:0:0")                                # оценка из карточки фильма
    await press(ALICE, "Lq:0:m:5:0:0:8")
    assert st.db.list_item(lid, "m", 5)["watched_at"]                  # оценил — ✅ в личном списке
    assert st.db.scores_by_tmdb(ALICE)[("m", 5)] == 8
    await send(ALICE, "/ocenki")
    assert "Дюна (2021)" in last(session, ALICE)[1]
    # новая подборка с фильмом через ввод названия; Боб удалить её не может
    await press(ALICE, "Ln:0:m:5")
    await send(ALICE, "На Новый год")
    new = [x for x in st.db.lists_of_user(ALICE) if x["name"] == "На Новый год"][0]
    assert st.db.list_item(new["id"], "m", 5)
    await press(BOB, f"LX:{new['id']}")
    assert st.db.list_get(new["id"]) is not None
    await press(ALICE, f"LX:{new['id']}")
    assert st.db.list_get(new["id"]) is None


async def test_share_list_read_only_and_revoke(env):
    st, session, send, press, mp = env
    lists.remember_info(st, info(7, "Интерстеллар", "2014-11-05"))
    lid = st.db.main_list(ALICE)
    st.db.list_add(lid, "m", 7, ALICE)
    await press(ALICE, "Lq:0:m:7:0:0:10")
    await press(ALICE, f"Ls:{lid}")
    code = st.db.list_get(lid)["share"]
    assert f"l_{code}" in last(session, ALICE)[1]
    await send(BOB, f"/start l_{code}")
    assert "открыл(а) тебе список" in last(session, BOB)[1]
    await press(BOB, f"Lv:{lid}:0:0")
    t = last(session, BOB)[1]
    assert "Интерстеллар" in t and "автор: 10" in t and "Открыт тебе" in t
    await press(BOB, f"Li:{lid}:m:7:0:0")
    kb = last(session, BOB)[2]
    assert "➕ Себе" in [b.text for b in buttons(kb)] and not any("Убрать" in b.text for b in buttons(kb))
    await press(BOB, f"Lt:{lid}:m:7")                                  # в чужой список добавлять нельзя
    assert "нельзя" in session.alerts()[-1]
    await press(BOB, f"Lt:{st.db.main_list(BOB)}:m:7")                 # «➕ Себе»
    assert st.db.list_item(st.db.main_list(BOB), "m", 7)
    await press(ALICE, f"Lo:{lid}:0")                                  # закрыть для всех
    await press(BOB, f"Lv:{lid}:0:0")
    assert "не доступен" in last(session, BOB)[1] and st.db.list_get(lid)["share"] not in (None, code)
    await send(BOB, f"/start l_{code}")                                # старая ссылка больше не работает
    assert "устарела" in last(session, BOB)[1]


async def test_kids_see_only_kids_items(env):
    st, session, send, press, mp = env
    st.db.set_flag(BOB, "kids", True)
    gid = st.db.group_create("Семья", ALICE)
    st.db.group_join(gid, BOB)
    lid = st.db.group_main_list(gid)
    lists.remember_info(st, info(10, "Пила", "2004-10-01", genres=[27]))
    lists.remember_info(st, info(11, "Тачки", "2006-06-08", genres=[16, 10751]))
    st.db.list_add(lid, "m", 10, ALICE)
    st.db.list_add(lid, "m", 11, ALICE)
    await press(BOB, f"Lv:{lid}:0:0")
    t = last(session, BOB)[1]
    assert "Тачки" in t and "Пила" not in t


# ---------- лёгкий режим (без права качать) ----------
async def test_light_mode_card_instead_of_releases(env):
    st, session, send, press, mp = env
    st.db.allow(CAROL, "Карина")
    calls = []

    async def fake_find(st_, q):
        return [mv(105, "Назад в будущее", "1985-07-03")]
    mp.setattr(main, "find_info", fake_find)

    async def fake_jac(http, cfg, q):
        calls.append(q)
        return [rel("Назад в будущее (1985) BDRip 1080p", "1" * 40)]
    mp.setattr(jacred, "search", fake_jac)

    async def fake_extras(http, key, info_, lang="ru-RU"):
        return {}
    mp.setattr(tmdb, "extras", fake_extras)
    mp.setattr(main, "add_cast", no_cast)
    await send(CAROL, "назад в будущее")
    kind, text, kb = last(session, CAROL)
    labels = [b.text for b in buttons(kb)]
    assert not calls and "➕ В список" in labels and "⭐ Оценить" in labels
    assert not any("раздачи" in x for x in labels)
    await press(CAROL, f"dl:{'0' * 8}:0")                              # старая кнопка раздачи — не сработает
    await send(CAROL, "/status")
    assert "не разрешено" in last(session, CAROL)[1]
    await send(CAROL, "magnet:?xt=urn:btih:" + "2" * 40)
    assert not st.tr.torrents
    await send(CAROL, "/start")
    assert "/lists" in last(session, CAROL)[1] and "/status" not in last(session, CAROL)[1]

    # админ включает «⬇ Качать» — теперь раздачи
    await press(ADMIN, f"udw:{CAROL}:c")
    assert st.hooks["may_download"](CAROL) and "разрешил" in last(session, CAROL)[1]
    await send(CAROL, "назад в будущее")
    assert calls


# ---------- подбор ----------
async def test_similar_skips_seen_and_own_lists(env):
    st, session, send, press, mp = env
    j = st.db.journal_for_title("m", 603, "Матрица (1999)")
    st.db.rate(j, ALICE, 9)
    j2 = st.db.journal_for_title("m", 604, "Матрица: Перезагрузка (2003)")
    st.db.rate(j2, ALICE, None)                                        # «не смотрел» — тоже не предлагать
    lists.remember_info(st, info(50, "Тёмный город", "1998-02-27"))
    st.db.list_add(st.db.main_list(ALICE), "m", 50, ALICE)

    async def fake_recs(http, key, kind, tid, lang="ru-RU"):
        assert (kind, tid) == ("m", 603)
        return [info(604, "Матрица: Перезагрузка", "2003-05-07"), info(50, "Тёмный город", "1998-02-27"),
                info(77, "Начало", "2010-07-15")]
    mp.setattr(tmdb, "recommendations", fake_recs)
    await press(ALICE, "Rs")
    t = last(session, ALICE)[1]
    assert "Начало" in t and "похоже на: Матрица (1999)" in t
    assert "Перезагрузка" not in t and "Тёмный город" not in t


async def test_together_marks_who_has_seen(env):
    st, session, send, press, mp = env
    gid = st.db.group_create("Семья", ALICE)
    st.db.group_join(gid, BOB)
    for jid_t, who, score in ((603, ALICE, 9), (603, BOB, 8), (77, BOB, 7)):
        st.db.rate(st.db.journal_for_title("m", jid_t, f"Фильм {jid_t}"), who, score)

    async def fake_recs(http, key, kind, tid, lang="ru-RU"):
        return [info(77, "Начало", "2010-07-15"), info(88, "Престиж", "2006-10-17")]
    mp.setattr(tmdb, "recommendations", fake_recs)
    await press(ALICE, f"Rg:{gid}")
    t = last(session, ALICE)[1]
    assert "Начало" in t and "Боб уже видел(а) (7)" in t and "Престиж" in t


# ---------- контроль ИИ ----------
async def test_ai_control_limits_toggles_and_usage(env):
    st, session, send, press, mp = env
    st.ai = ai.Chain(st.cfg.__class__(**{**st.cfg.__dict__, "yandex_key": "K", "yandex_folder": "F",
                                         "ai_prices": (("yandex", 1.0),)}), {"yandex": None})
    st.cfg = st.ai.cfg
    asked = []

    async def fake_ask(self, p, text, prompt=ai.PROMPT, meter=None, limit=500, answers=6):
        asked.append(prompt)
        meter.update(tin=900, tout=100)
        return [{"title": "Начало", "original": "Inception", "year": "2010", "tv": False}]
    mp.setattr(ai.Chain, "_ask", fake_ask)

    async def fake_resolve(http, key, guesses, lang="ru-RU"):
        return [info(77, "Начало", "2010-07-15")]
    mp.setattr(ai, "resolve", fake_resolve)

    await send(ADMIN, "/ai")
    t = last(session, ADMIN)[1]
    assert "Алиса ✅" in t and "на человека 10" in t
    await press(ADMIN, "A:u")                                          # 10 → 20 (кнопка листает значения)
    assert aictl.user_day(st) == 20
    aictl.set_setting(st, "user_day", "1")

    # ИИ-подбор: первый запрос проходит, второй — лимит на человека → запасной вариант
    await press(ALICE, "Rm")
    assert "Спросить ИИ" in str(last(session, ALICE)[2])
    await press(ALICE, "Ra")
    await send(ALICE, "что-то про сны и ограбления")
    t = last(session, ALICE)[1]
    assert "Начало" in t and asked[-1] == ai.REC_PROMPT
    await press(ALICE, "Ra")
    assert "закончились" in session.alerts()[-1]

    # расход записан, сумма считается по цене
    assert st.db.ai_requests(aictl.today(), ALICE) == 1
    assert abs(aictl.cost(st, aictl.month()) - 1.0) < 1e-9
    await press(ADMIN, "A:r")
    assert "≈ 1.00 ₽" in last(session, ADMIN)[1]

    # выключить подбор — кнопки нет; выключить Алису — цепочка пустая
    await press(ADMIN, "A:f:rec")
    await press(BOB, "Rm")
    assert "Спросить ИИ" not in str(last(session, BOB)[2])
    await press(ADMIN, "A:p:yandex")
    assert st.ai.disabled == {"yandex"} and not st.ai.active()

    # «только отмеченным»
    await press(ADMIN, "A:p:yandex")
    await press(ADMIN, "A:f:rec")
    await press(ADMIN, "A:w")
    assert aictl.check(st, BOB, "rec") == "ИИ доступен не всем — попроси администратора"
    await press(ADMIN, f"uai:{BOB}:c")
    assert aictl.check(st, BOB, "rec") == ""

    # потолок: 1 ₽ уже потрачен — при потолке 1 ₽ ИИ выключен, админу сообщение; «включить до конца месяца»
    aictl.set_setting(st, "month_rub", "1")
    assert aictl.capped(st) and "потолок" in aictl.check(st, BOB, "rec")
    await aictl.after_note(session_bot(env), st)
    assert any("достиг потолка" in t for _, t, _ in session.sent(ADMIN))
    await press(ADMIN, "A:o")
    assert not aictl.capped(st)


def session_bot(env):
    from aiogram import Bot
    return Bot("123:abc", session=env[1])


async def test_plot_respects_ai_limit(env):
    st, session, send, press, mp = env
    st.ai = ai.Chain(st.cfg.__class__(**{**st.cfg.__dict__, "yandex_key": "K", "yandex_folder": "F"}),
                     {"yandex": None})
    from bot import wiki
    used = []

    async def fake_ai(chain, tmdb_http, cfg, text, meter=None):
        used.append(text)
        meter.update(provider="yandex", tin=10, tout=5)
        return [info(1, "Маска", "1994-07-29")], "Алиса"

    async def fake_wiki(http, key, text, lang):
        return [info(2, "Маска 2", "2005-02-18")]
    mp.setattr(ai, "search_by_plot", fake_ai)
    mp.setattr(wiki, "search_by_plot", fake_wiki)
    mp.setattr(main, "add_cast", no_cast)
    aictl.set_setting(st, "user_day", "1")
    await send(ALICE, "/plot мужик находит маску и становится зелёным")
    assert "ИИ, Алиса" in last(session, ALICE)[1]
    await send(ALICE, "/plot мужик находит маску и становится зелёным")
    t = last(session, ALICE)[1]
    assert "закончились" in t and "Википедия" in t and len(used) == 1
    usage = st.db.ai_usage(aictl.today())
    assert [(u["provider"], u["feature"], u["req"], u["tin"] + u["tout"]) for u in usage] == [("yandex", "plot", 1, 15)]


# ---------- /random из списков, Kodi ✅ ----------
async def test_random_from_list_and_kodi_watched(env):
    st, session, send, press, mp = env
    lists.remember_info(st, info(5, "Дюна", "2021-09-15"))
    lid = st.db.main_list(ALICE)
    st.db.list_add(lid, "m", 5, ALICE)

    async def fake_discover(http, key, kind, params, lang="ru-RU"):
        return [info(1, "Амели", "2001-04-25")], 5
    mp.setattr(tmdb, "discover", fake_discover)

    async def fake_details(http, key, kind, tid, lang="ru-RU"):
        return info(int(tid), "Дюна", "2021-09-15")
    mp.setattr(tmdb, "details", fake_details)

    async def fake_extras(http, key, info_, lang="ru-RU"):
        return {}
    mp.setattr(tmdb, "extras", fake_extras)
    mp.setattr(main, "add_cast", no_cast)
    await send(ALICE, "/random")
    kb = last(session, ALICE)[2]
    await press(ALICE, btn(kb, "Из списка «Хочу посмотреть»"))
    assert "Из списка «Хочу посмотреть»" in last(session, ALICE)[1] and "Дюна" in last(session, ALICE)[1]
    # Kodi отметил просмотренным то, что скачала Алиса → ✅ в её списке и в списке её группы
    gid = st.db.group_create("Семья", BOB)
    st.db.group_join(gid, ALICE)
    glid = st.db.group_main_list(gid)
    st.db.list_add(glid, "m", 5, BOB)
    st.db.add_download("d" * 40, "Dune", "Дюна", "movies", ALICE, ALICE, tmdb_kind="m", tmdb_id=5)
    assert lists.mark_kodi_watched(st, st.db.get("d" * 40)) == 2
    assert st.db.list_item(lid, "m", 5)["watched_at"] and st.db.list_item(glid, "m", 5)["watched_at"]


async def test_ocenki_light_user_sees_only_circle(env):
    st, session, send, press, mp = env
    st.db.allow(CAROL, "Карина")
    st.db.journal_note("movies", "Домашний фильм (2020)", None, ALICE, int(time.time()))
    j = st.db.journal_for_title("m", 9, "Чужой фильм (2001)")
    st.db.rate(j, BOB, 7)                                              # Боб не в круге Карины
    await send(CAROL, "/ocenki")
    assert "Пока пусто" in last(session, CAROL)[1]
    gid = st.db.group_create("Друзья", BOB)
    st.db.group_join(gid, CAROL)
    await send(CAROL, "/ocenki")
    t = last(session, CAROL)[1]
    assert "Чужой фильм" in t and "Домашний фильм" not in t
    await send(ALICE, "/ocenki")
    assert "Домашний фильм" in last(session, ALICE)[1]


async def test_approve_with_download_and_input_reset(env):
    st, session, send, press, mp = env
    await send(CAROL, "/start")
    await press(ADMIN, f"okd:{CAROL}")
    assert st.hooks["may_download"](CAROL)
    # начал создавать подборку, но передумал и пошёл искать фильм — название не «съедается»
    await press(ALICE, "Ln:0")
    assert ALICE in st.awaiting
    await press(ALICE, "Lm")
    assert ALICE not in st.awaiting
    await press(ALICE, "Ln:0")
    await send(ALICE, "/lists")
    assert ALICE not in st.awaiting
