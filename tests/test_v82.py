"""v8.2: magnet для лёгкого режима, инлайн, итоги, голос, слежение за списками, обновление."""
import itertools
import os
from datetime import datetime

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["TMDB_API_KEY"] = "k"

from aiogram.types import InlineQuery, Update, User  # noqa: E402

from test_flows import ADMIN, ALICE, BOB, buttons, env, mv, rel  # noqa: E402,F401

from bot import jacred, main, tmdb  # noqa: E402

CAROL = 300
_n = itertools.count(10_000)


async def inline_q(st, uid, text):
    q = InlineQuery(id=str(next(_n)), from_user=User(id=uid, is_bot=False, first_name="x"), query=text, offset="")
    await st._dp.feed_update(st._bot, Update(update_id=next(st._upd), inline_query=q))


def item(title, h, height=1080, seeders=50, size=5, tr=""):
    return {"Tracker": "rutor", "Title": title, "Size": size * 1024 ** 3, "Seeders": seeders, "Peers": 1,
            "Category": [2000], "MagnetUri": f"magnet:?xt=urn:btih:{h}{tr}",
            "ffprobe": [{"codec_type": "video", "codec_name": "hevc" if height > 1080 else "h264",
                         "width": {2160: 3840, 1080: 1920, 720: 1280}[height], "height": height}],
            "info": {"types": ["movie"]}}


def test_best_any_ignores_pi_filters_but_not_junk():
    items = [item("Дюна (2021) BDRip 1080p", "a" * 40),
             item("Дюна (2021) UHD BDRemux 2160p HDR", "b" * 40, height=2160, size=70),
             item("Дюна (2021) CAMRip 2160p", "c" * 40, height=2160),
             item("Дюна (2021) WEB-DL 2160p", "d" * 40, height=2160, seeders=0),
             item("Другой фильм 2160p", "e" * 40, height=2160)]
    best = jacred.best_any(items, lambda t, s: "Дюна" in t, 2)
    assert best.infohash == "b" * 40


def test_short_magnet_fits_copy_button():
    trs = "".join(f"&tr=http%3A%2F%2Fretracker{i}.example.org%3A2710%2Fannounce" for i in range(10))
    r = jacred.parse(item("Очень длинное название фильма " * 5, "f" * 40, tr=trs))
    m = jacred.short_magnet(r)
    assert len(m) <= 256 and m.startswith("magnet:?xt=urn:btih:" + "f" * 40) and "&tr=" in m


async def test_inline_cards_only_for_users(env):
    st, session, send, press, mp = env
    st.bot_username = "tbot"

    async def fake_find(st_, q):
        return [mv(105, "Назад в будущее", "1985-07-03")]
    mp.setattr(main, "find_info", fake_find)
    await inline_q(st, ALICE, "назад в будущее")
    ans = [m for m in session.calls if type(m).__name__ == "AnswerInlineQuery"][-1]
    assert ans.results and ans.results[0].id == "f_m_105"
    url = ans.results[0].reply_markup.inline_keyboard[0][0].url
    assert url == "https://t.me/tbot?start=f_m_105"
    await inline_q(st, 999, "назад в будущее")                     # чужой — пусто
    ans = [m for m in session.calls if type(m).__name__ == "AnswerInlineQuery"][-1]
    assert ans.results == [] and ans.button


async def test_start_from_inline_opens_film(env):
    st, session, send, press, mp = env
    calls = []

    async def fake_info_for(st_, kind, tid, full=False):
        return tmdb._to_info(mv(tid, "Назад в будущее", "1985-07-03"))
    from bot import lists
    mp.setattr(lists, "info_for", fake_info_for)

    async def fake_jac(http, cfg, q):
        calls.append(q)
        return [rel("Назад в будущее (1985) BDRip 1080p", "1" * 40)]
    mp.setattr(jacred, "search", fake_jac)
    mp.setattr(main, "add_cast", lambda *a, **k: _none())
    await send(ALICE, "/start f_m_105")
    assert calls and any("1080p" in t for _, t, _ in session.sent(ALICE))


async def _none():
    return None


async def test_itogi_personal_and_group(env):
    st, session, send, press, mp = env
    from bot import itogi
    a, _ = itogi.year_bounds(2026)
    db = st.db
    for i, (t, g, sc_a, sc_b) in enumerate([("Дюна", "878", 9, 8), ("Мстители", "28", 7, None),
                                            ("Шрек", "16,35", 10, 9)], 1):
        db.c.execute("INSERT INTO titles VALUES ('m',?,?,?,?,?)", (i, t, "2021", None, g))
        db.c.execute("INSERT INTO journal(key,kind,label,tmdb_kind,tmdb_id) VALUES (?,?,?,?,?)",
                     (f"k{i}", "movie", t, "m", i))
        jid = db.c.execute("SELECT id FROM journal WHERE key=?", (f"k{i}",)).fetchone()[0]
        db.c.execute("INSERT INTO ratings VALUES (?,?,?,?)", (jid, ALICE, sc_a, a + 100))
        if sc_b:
            db.c.execute("INSERT INTO ratings VALUES (?,?,?,?)", (jid, BOB, sc_b, a + 100))
    db.c.execute("INSERT INTO teams(name,owner_id,invite,created_at) VALUES ('Семья',?, 'x', 0)", (ALICE,))
    gid = db.c.execute("SELECT id FROM teams").fetchone()[0]
    db.c.executemany("INSERT INTO group_members VALUES (?,?,0)", [(gid, ALICE), (gid, BOB)])
    db.c.commit()
    await send(ALICE, "/itogi 2026")
    text = [t for _, t, _ in session.sent(ALICE)][-1]
    assert "Посмотрено: <b>3</b>" in text and "1. Шрек (2021) — 10" in text
    assert "Фантастика" in text and "Группа «Семья»" in text and "Любимое у группы: Шрек" in text
    sent = await itogi.send_all(st._bot, st, 2026)
    assert sent == 2 and await itogi.send_all(st._bot, st, 2026) == 0       # второй раз не шлём


async def voice_msg(st, uid, dur=4):
    from aiogram.types import Chat, Message, Voice
    m = Message(message_id=next(_n), date=datetime.now(), chat=Chat(id=uid, type="private"),
                from_user=User(id=uid, is_bot=False, first_name="x"),
                voice=Voice(file_id="f", file_unique_id="u", duration=dur))
    await st._dp.feed_update(st._bot, Update(update_id=next(st._upd), message=m))


async def test_voice_goes_to_search_and_is_metered(env):
    st, session, send, press, mp = env
    from bot import aictl, voice
    import dataclasses
    st.cfg = dataclasses.replace(st.cfg, yandex_key="k", yandex_folder="f", stt_price=0.16)

    async def fake_dl(self, file, destination=None, **kw):
        destination.write(b"ogg")
    mp.setattr(type(st._bot), "download", fake_dl)

    async def fake_rec(http, cfg, audio):
        assert audio == b"ogg"
        return "назад в будущее"
    mp.setattr(voice, "recognize", fake_rec)
    seen = []

    async def fake_find(st_, q):
        seen.append(q)
        return [mv(105, "Назад в будущее", "1985-07-03")]
    mp.setattr(main, "find_info", fake_find)
    mp.setattr(jacred, "search", lambda *a: _list())
    mp.setattr(main, "add_cast", lambda *a, **k: _none())
    await voice_msg(st, ALICE, dur=20)
    assert seen == ["назад в будущее"]
    assert any("🎤 «назад в будущее»" in t for _, t, _ in session.sent(ALICE))
    rows = st.db.ai_usage(aictl.today())
    assert rows[0]["provider"] == "stt" and rows[0]["tin"] == 2 and rows[0]["tout"] == 20
    assert abs(aictl.cost(st, aictl.today()) - 0.32) < 1e-9
    assert st.db.ai_requests(aictl.today(), ALICE) == 0               # дневные лимиты голос не тратит
    await voice_msg(st, ALICE, dur=45)
    assert "Слишком длинно" in [t for _, t, _ in session.sent(ALICE)][-1]
    aictl.set_setting(st, "f_voice", "0")
    await voice_msg(st, ALICE)
    assert "выключены администратором" in [t for _, t, _ in session.sent(ALICE)][-1]


async def _list():
    return []


async def test_listwatch_quiet_first_then_notifies(env):
    st, session, send, press, mp = env
    from bot import listwatch, lists
    st.db.allow(CAROL, "Карина")                           # лёгкий режим
    for u in (ALICE, CAROL):
        st.db.list_add(st.db.main_list(u), "m", 105, u)
    st.db.set_flag(ALICE, "lwatch", True)
    st.db.set_flag(CAROL, "lwatch", True)

    async def fake_info_for(st_, kind, tid, full=False):
        return tmdb._to_info(mv(tid, "Назад в будущее", "1985-07-03"))
    mp.setattr(lists, "info_for", fake_info_for)
    stock = [item("Назад в будущее (1985) DVDRip 720p", "1" * 40, height=720)]

    async def fake_jac(http, cfg, q):
        return stock
    mp.setattr(jacred, "search", fake_jac)
    t0 = 1_000_000
    assert await listwatch.check_once(st._bot, st, t0) == 0          # первая проверка — тихо
    stock.append(item("Назад в будущее (1985) BDRip 1080p", "2" * 40))
    assert await listwatch.check_once(st._bot, st, t0 + 3600) == 0   # рано — не проверяем
    assert await listwatch.check_once(st._bot, st, t0 + 13 * 3600) == 2
    a = [m for m in session.calls if getattr(m, "chat_id", None) == ALICE and "🔔" in (getattr(m, "text", "") or "")]
    c = [m for m in session.calls if getattr(m, "chat_id", None) == CAROL and "🔔" in (getattr(m, "text", "") or "")]
    assert "хорошая раздача (1080p)" in a[-1].text and buttons(a[-1].reply_markup)[0].callback_data == "Ld:m:105"
    assert buttons(c[-1].reply_markup)[0].copy_text
    assert await listwatch.check_once(st._bot, st, t0 + 26 * 3600) == 0     # второй раз о том же — нет


async def test_listwatch_toggle_in_lists(env):
    st, session, send, press, mp = env
    await send(ALICE, "/lists")
    assert any(b.callback_data == "Lp" for b in buttons(session.sent(ALICE)[-1][2]))
    await press(ALICE, "Lp")
    assert st.db.flag(ALICE, "lwatch")


def make_zip(files: dict) -> bytes:
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    return b.getvalue()


GOOD = {"torrbot/bot/main.py": "", "torrbot/VERSION": "9.0\n", "torrbot/deploy.sh": "", "torrbot/docker-compose.yml": ""}


def test_update_zip_checks():
    import pytest
    from bot import updater
    assert updater.inspect(make_zip(GOOD)) == "9.0"
    for bad in ({**GOOD, "../evil": ""}, {**GOOD, "torrbot/.env": "x"}, {"torrbot/VERSION": "9.0"},
                {**GOOD, "torrbot/VERSION": "rm -rf"}):
        with pytest.raises(updater.BadZip):
            updater.inspect(make_zip(bad))
    with pytest.raises(updater.BadZip):
        updater.inspect(b"not a zip")


async def test_update_flow_writes_request(env, tmp_path):
    import dataclasses
    import json
    from aiogram.types import Chat, Document, Message
    st, session, send, press, mp = env
    st.cfg = dataclasses.replace(st.cfg, update_dir=str(tmp_path / "upd"))
    data = make_zip(GOOD)

    async def fake_dl(self, file, destination=None, **kw):
        destination.write(data)
    mp.setattr(type(st._bot), "download", fake_dl)

    async def doc(uid, name):
        m = Message(message_id=next(_n), date=datetime.now(), chat=Chat(id=uid, type="private"),
                    from_user=User(id=uid, is_bot=False, first_name="x"),
                    document=Document(file_id="f", file_unique_id="u", file_name=name, file_size=len(data)))
        await st._dp.feed_update(st._bot, Update(update_id=next(st._upd), message=m))
    await doc(ALICE, "torrbot-v9.0.zip")                          # не админ — молчим
    assert not session.sent(ALICE)
    await doc(ADMIN, "torrbot-v9.0.zip")
    text, kb = session.sent(ADMIN)[-1][1:]
    assert "→ v9.0" in text and "не установлена" in text
    await press(ADMIN, "Up:go")
    req = json.loads((tmp_path / "upd" / "request.json").read_text())
    assert req["version"] == "9.0" and (tmp_path / "upd" / "torrbot-update.zip").read_bytes() == data
    await send(ADMIN, "/update")
    assert "идёт обновление" in session.sent(ADMIN)[-1][1]
