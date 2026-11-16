"""v8.4: счётчик использования функций, /stats и годовая статистика админу."""
import os
import time
from datetime import datetime, timedelta

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["TMDB_API_KEY"] = "k"

import pytest  # noqa: E402

from test_flows import ADMIN, ALICE, BOB, env, mv  # noqa: E402,F401

from bot import itogi, main, stats  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")


@pytest.mark.parametrize("text,voice,waiting,want", [
    ("/random", False, False, "random"), ("/podbor@lerroy_kino_bot", False, False, "podbor"),
    ("/want", False, False, "lists"), ("/Lists", False, False, "lists"), ("/gruppy", False, False, "groups"),
    ("/ocenki 5", False, False, "ratings"), ("/start g_abc", False, False, "info"),
    ("/nosuchcommand", False, False, None), ("/stats", False, False, None),
    ("Матрица", False, False, "search"), ("magnet:?xt=urn:btih:abc", False, False, "magnet"),
    ("Название подборки", False, True, None), ("/random", False, True, "random"),
    (None, True, False, "voice"), ("   ", False, False, None),
])
def test_feature_of_text(text, voice, waiting, want):
    assert stats.feature_of_text(text, voice, waiting) == want


@pytest.mark.parametrize("data,want", [
    ("dl:abc:1", "download"), ("rnd", "random"), ("vo:3", "voices"), ("vo:ok", "voices"),
    ("pb:m", "podbor"), ("Lv:1:m:5:2:0", "lists"), ("Gm", "groups"), ("Ra", "sovet"), ("A:x", "ai_ctl"),
    ("Up:go", "update"), ("uok:100", "users"), ("tvp:1", "tv"), ("cx:" + "a" * 40, "status"),
    ("sbt:77", "subs"), ("wt:m:1", "wait"), ("fs:" + "a" * 40, "seasons"), ("col:5", "collection"),
    ("rt:3:8:m:1", "ratings"), ("ld:abcd1234:0:-1", "delete"), ("sp:night", "speed"),
    ("pg:abc:2", None), ("keep:abc", None), ("pa:abcdef12", None), ("", None), (None, None), ("123", None),
])
def test_feature_of_callback(data, want):
    assert stats.feature_of_callback(data) == want


def test_every_mapped_feature_has_a_label():
    for f in list(stats.COMMANDS.values()) + list(stats.CALLBACKS.values()) + list(stats.CALLBACK_BY_LETTER.values()):
        assert f in stats.LABEL, f
    assert {"search", "voice", "inline", "magnet"} <= set(stats.LABEL)


def rows(st):
    return {(r["feature"], r["user_id"]): r["n"] for r in st.db.c.execute("SELECT * FROM usage")}


async def test_counts_through_dispatcher(env):
    st, session, send, press, mp = env
    await send(ALICE, "/ocenki")
    await send(ALICE, "/ocenki")
    await send(BOB, "/lists")
    await press(ALICE, "vo:ok")
    await press(BOB, "pg:zzz:1")                     # навигация по страницам не считается
    await send(999, "/ocenki")                       # чужой, без доступа — не считается
    got = rows(st)
    assert got[("ratings", ALICE)] == 2 and got[("lists", BOB)] == 1 and got[("voices", ALICE)] == 1
    assert not any(u == 999 for _, u in got) and ("pg", BOB) not in got and len(got) == 3


async def test_text_answer_to_bot_question_is_not_search(env):
    st, session, send, press, mp = env
    st.awaiting[ALICE] = (time.time(), ("Ln", 0))
    await send(ALICE, "Мои выходные")
    assert not any(f == "search" for f, _ in rows(st))


async def test_inline_counted_once_per_half_minute(env):
    st, session, send, press, mp = env
    from test_v82 import inline_q
    stats._last_inline.clear()

    async def fake_find(st_, q):
        return []
    mp.setattr(main, "find_info", fake_find)
    for q in ("м", "ма", "мат", "матр"):
        await inline_q(st, ALICE, q)
    assert rows(st)[("inline", ALICE)] == 1


async def test_counter_failure_does_not_break_bot(env):
    st, session, send, press, mp = env
    st.db.c.execute("DROP TABLE usage")
    await send(ALICE, "/lists")                       # счётчик упадёт, бот ответит как обычно
    assert session.sent(ALICE)


async def test_stats_command_admin_only_and_report(env):
    st, session, send, press, mp = env
    stats.note(st, ALICE, "plot")
    stats.note(st, ALICE, "plot")
    stats.note(st, BOB, "plot")
    stats.note(st, ALICE, "random", day="2020-01-01")         # давно — не в окне 30 дней, но «раньше было»
    await send(ALICE, "/stats")
    assert not [t for _, t, _ in session.sent(ALICE) if "Использование" in t]   # не админ — молчим
    await send(ADMIN, "/stats")
    out = [t for _, t, _ in session.sent(ADMIN)][-1]
    assert "за 30 дн." in out and "Счётчик ведётся с 01.01.2020" in out
    assert "🧩 /plot — по сюжету — 3 (2 чел.)" in out
    nope = out.split("Не пользовались за период")[1]
    assert "🎰 /random — что посмотреть (1)" in nope and "/plot" not in nope
    assert "/speed" not in nope                                # служебные не в списке «не пользовались»
    await send(ADMIN, "/stats all")
    assert "за всё время" in [t for _, t, _ in session.sent(ADMIN)][-1]
    await send(ADMIN, "/stats 7")
    assert "за 7 дн." in [t for _, t, _ in session.sent(ADMIN)][-1]


def test_report_empty_and_summary_numbers(env):
    st, session, send, press, mp = env
    assert "Счётчик ещё пуст" in stats.last_days(st, 30)
    st.db.add_download("h1", "n", "t", "movies", 1, ALICE)
    st.db.c.execute("INSERT INTO ratings VALUES (1, ?, 8, ?)", (ALICE, int(time.time())))
    st.db.c.execute("INSERT INTO ai_usage(day,user_id,feature,provider,req) VALUES (?,?,?,?,?)",
                    (TODAY, ALICE, "plot", "yandex", 4))
    st.db.c.commit()
    out = stats.last_days(st, 30)
    assert "⬇ закачек: 1" in out and "⭐ оценок: 1" in out and "🤖 запросов к ИИ: 4" in out
    assert "За этот период никто ничем не пользовался." in out


def test_year_report_bounds(env):
    st, *_ = env
    stats.note(st, ALICE, "plot", day="2025-06-01")
    stats.note(st, ALICE, "voice", day="2026-02-01")
    assert "2025 год" in stats.whole_year(st, 2025)
    y25 = stats.whole_year(st, 2025).split("Не пользовались")[0]
    assert "🧩 /plot" in y25 and "🎤 Голосовой" not in y25


async def test_december_report_sends_stats_to_admin_once(env):
    st, session, send, press, mp = env
    stats.note(st, ALICE, "plot", day="2026-03-03")
    await itogi.send_all(st._bot, st, 2026)
    got = [t for _, t, _ in session.sent(ADMIN) if "Использование бота за 2026" in t]
    assert len(got) == 1 and "🧩 /plot" in got[0]
    await itogi.send_all(st._bot, st, 2026)
    assert len([t for _, t, _ in session.sent(ADMIN) if "Использование бота за 2026" in t]) == 1
