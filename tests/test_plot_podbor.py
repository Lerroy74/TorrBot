"""Поиск по сюжету (Википедия) и /podbor (TMDB discover)."""
import asyncio
import re

from aiohttp import web

from test_flows import ALICE, buttons, env, mv, rel  # noqa: F401  (env — фикстура)

from bot import jacred, main, tmdb, wiki


def sparql_handler(table: dict, seen: list | None = None):
    """Подставной query.wikidata.org: table = {"Q1": ("m", "854"), "Q2": ("t", "1396"), "Q3": None}."""
    async def handler(request):
        qids = re.findall(r"wd:(Q\d+)", request.query["query"])
        if seen is not None:
            seen.append(set(qids))
        rows = []
        for q in qids:
            row = {"i": {"value": f"http://www.wikidata.org/entity/{q}"}}
            v = table.get(q)
            if v:
                row[v[0]] = {"value": v[1]}
            rows.append(row)
        return web.json_response({"results": {"bindings": rows}})
    return handler


# ---------- разбор описания ----------
def test_keywords_and_title():
    assert wiki.keywords("фильм где мужик находит маску и становится зелёным") == \
        ["находит", "маску", "становится", "зеленым"]
    assert wiki.keywords("какой-то чувак с девчонкой ищут человек-паук") == ["девчонкой", "ищут", "человек-паук"]
    assert wiki.keywords("про что-то там, вроде кино") == ["что-то"]
    assert wiki.parse_title("Маска (фильм, 1994)") == ("Маска", "1994")
    assert wiki.parse_title("Во все тяжкие") == ("Во все тяжкие", None)
    assert wiki.parse_title("Солярис (фильм, 1972)")[1] == "1972"


# ---------- Википедия → Викиданные → TMDB на подставных серверах ----------
async def test_search_by_plot(aiohttp_client, monkeypatch):
    searches = []

    asked = []

    async def wiki_api(request):
        q = request.query
        s = q["gsrsearch"]
        searches.append(s)
        if "hastemplate:" in s and " OR " in s:
            return web.json_response({"query": {"pages": [
                {"pageid": 10, "index": 1, "title": "Маска (фильм, 1994)", "pageprops": {"wikibase_item": "Q1"}},
                {"pageid": 11, "index": 2, "title": "Маска 2 (фильм, 2005)", "pageprops": {"wikibase_item": "Q2"}},
                {"pageid": 12, "index": 3, "title": "Без Викиданных (фильм, 1999)"},
            ]}})
        return web.json_response({"batchcomplete": True})        # ничего не найдено

    async def tmdb_movie(request):
        assert request.match_info["id"] == "854"
        return web.json_response({"id": 854, "title": "Маска", "original_title": "The Mask",
                                  "release_date": "1994-07-29", "poster_path": "/m.jpg", "vote_average": 6.9})

    async def tmdb_search(request):
        name = request.query["query"]
        if name == "Маска 2":
            return web.json_response({"results": [mv(3, "Маска 2", "2005-02-18")]})
        return web.json_response({"results": []})

    app = web.Application()
    app.router.add_get("/w/api.php", wiki_api)
    app.router.add_get("/sparql", sparql_handler({"Q1": ("m", "854"), "Q2": None}, asked))  # у Q2 нет ID TMDB
    app.router.add_get("/3/movie/{id}", tmdb_movie)
    app.router.add_get("/3/search/multi", tmdb_search)
    client = await aiohttp_client(app)
    monkeypatch.setattr(wiki, "WIKI_API", str(client.make_url("/w/api.php")))
    monkeypatch.setattr(wiki, "WIKIDATA_SPARQL", str(client.make_url("/sparql")))
    monkeypatch.setattr(tmdb, "API", str(client.make_url("/3")))

    infos = await wiki.search_by_plot(client.session, "k", "мужик находит маску и становится зелёным")
    assert [(i.title, i.year) for i in infos] == [("Маска", "1994"), ("Маска 2", "2005")]
    assert infos[0].poster.endswith("/m.jpg")
    # все слова (И) → «все, кроме одного» (4 варианта) → любое из слов (ИЛИ); одна запись на все карточки
    assert all("мужик" not in s for s in searches)
    assert all('hastemplate:"Фильм|Телесериал|Мультфильм"' in s for s in searches)
    assert sum(" OR " not in s for s in searches) == 1 + 4 and sum(" OR " in s for s in searches) == 1
    assert asked == [{"Q1", "Q2"}]                           # один запрос к Викиданным на всё


async def test_plot_one_bad_word(aiohttp_client, monkeypatch):
    """Одно слово, которого нет в статье, не ломает поиск: вариант без него идёт первым."""
    def page(i, title):
        return {"pageid": i, "index": i, "title": title}

    searches = []

    async def wiki_api(request):
        q = request.query
        s = q["gsrsearch"]
        searches.append(s)
        words = s.split(' hastemplate:')[0].split()
        if words == ["лысый", "находит", "маску", "зеленым"]:            # все слова — мусор
            return web.json_response({"query": {"pages": [page(1, "Хранители (фильм)")]}})
        if words == ["находит", "маску", "зеленым"]:                     # без «лысый»
            return web.json_response({"query": {"pages": [page(2, "Маска (фильм, 1994)"),
                                                          page(1, "Хранители (фильм)"),
                                                          page(3, "Демоны (фильм, 1985)")]}})
        if words == ["лысый", "маску", "зеленым"]:                       # без «находит»
            return web.json_response({"query": {"pages": [page(4, "Случайный (фильм, 2001)")]}})
        return web.json_response({"batchcomplete": True})

    years = {"Хранители": "2009-03-05", "Маска": "1994-07-29", "Демоны": "1985-10-04", "Случайный": "2001-01-01"}

    async def tmdb_search(request):
        name = request.query["query"]
        return web.json_response({"results": [mv(list(years).index(name) + 1, name, years[name])]})

    app = web.Application()
    app.router.add_get("/w/api.php", wiki_api)
    app.router.add_get("/3/search/multi", tmdb_search)
    client = await aiohttp_client(app)
    monkeypatch.setattr(wiki, "WIKI_API", str(client.make_url("/w/api.php")))
    monkeypatch.setattr(wiki, "WIKIDATA_SPARQL", str(client.make_url("/nosparql")))   # 404 → по названию
    monkeypatch.setattr(tmdb, "API", str(client.make_url("/3")))
    infos = await wiki.search_by_plot(client.session, "k", "лысый мужик находит маску, зеленым")
    assert [i.title for i in infos] == ["Хранители", "Маска", "Демоны", "Случайный"]
    assert not any(" OR " in s for s in searches)          # набрали достаточно — ИЛИ не нужно


# ---------- в боте ----------
async def test_long_unknown_query_goes_to_plot(env):
    st, session, send, press, mp = env

    async def fake_find(st_, q):
        return []
    mp.setattr(main, "find_info", fake_find)
    seen = []

    async def fake_plot(http, key, text, lang="ru-RU", limit=6):
        seen.append(text)
        return [tmdb._to_info(mv(1, "Маска", "1994-07-29"))]
    mp.setattr(wiki, "search_by_plot", fake_plot)
    await send(ALICE, "мужик находит маску и становится зелёным")
    _, text, kb = session.sent(ALICE)[-1]
    assert seen and "По описанию похоже" in text
    assert buttons(kb)[0].text.startswith("1. 🎬 Маска (1994)")


async def test_plot_button_on_choice_and_command(env):
    st, session, send, press, mp = env

    async def fake_find(st_, q):
        return [mv(5, "Зелёная миля", "1999-12-06"), mv(6, "Зелёная книга", "2018-11-16")]
    mp.setattr(main, "find_info", fake_find)
    called = []

    async def fake_plot(http, key, text, lang="ru-RU", limit=6):
        called.append(text)
        return []
    mp.setattr(wiki, "search_by_plot", fake_plot)

    await send(ALICE, "зелёный человек в маске танцует")
    _, text, kb = session.sent(ALICE)[-1]
    plot_btn = next(b for b in buttons(kb) if "по сюжету" in b.text)
    await press(ALICE, plot_btn.callback_data)
    assert called == ["зелёный человек в маске танцует"]
    _, text, kb = session.sent(ALICE)[-1]
    assert "ничего не нашёл" in text and any("как есть" in b.text for b in buttons(kb))

    await send(ALICE, "/plot фильм про")                   # слишком мало слов
    assert "Опиши сюжет подробнее" in session.sent(ALICE)[-1][1]
    await send(ALICE, "дюна")                                  # короткий запрос — без кнопки сюжета
    assert not any("по сюжету" in b.text for b in buttons(session.sent(ALICE)[-1][2]))


def test_discover_params():
    p = tmdb.discover_params("m", "35", "1990", "ru", "top", 2)
    assert p["with_genres"] == "35" and p["page"] == "2" and p["with_origin_country"] == "RU|SU"
    assert p["primary_release_date.gte"] == "1990-01-01" and p["primary_release_date.lte"] == "1999-12-31"
    assert p["sort_by"] == "vote_average.desc"
    t = tmdb.discover_params("t", "", "1900", "", "pop")
    assert "with_genres" not in t and t["first_air_date.lte"] == "1969-12-31" and t["sort_by"] == "popularity.desc"


async def test_podbor_flow(env):
    st, session, send, press, mp = env
    got = {}

    async def fake_discover(http, key, kind, params, lang="ru-RU"):
        got.update(kind=kind, **params)
        return [tmdb._to_info(mv(1, "Маска", "1994-07-29")), tmdb._to_info(mv(2, "Тупой и ещё тупее", "1994-12-16"))], 7
    mp.setattr(tmdb, "discover", fake_discover)

    async def fake_jac(http, cfg, q):
        return [rel("Маска / The Mask (1994) BDRip 1080p", "1" * 40)]
    mp.setattr(jacred, "search", fake_jac)

    await send(ALICE, "/podbor")
    for step in ["pb:m", "pb:m:35", "pb:m:35:1990", "pb:m:35:1990:ru"]:
        assert any(b.callback_data == step for b in buttons(session.sent(ALICE)[-1][2]))
        await press(ALICE, step)
    assert any(b.callback_data == "pb:m:35:1990:ru:top:1" for b in buttons(session.sent(ALICE)[-1][2]))
    await press(ALICE, "pb:m:35:1990:ru:top:1")
    _, text, kb = session.sent(ALICE)[-1]
    assert got["kind"] == "m" and got["with_genres"] == "35" and got["with_origin_country"] == "RU|SU"
    assert "фильмы, комедия, 90-е, Россия/СССР, лучшие" in text
    labels = {b.text: b.callback_data for b in buttons(kb)}
    assert labels["Ещё ▶"] == "pb:m:35:1990:ru:top:2" and "🎲 Заново" in labels
    # выбрали фильм из подбора — дальше обычный поиск раздач
    await press(ALICE, next(v for k, v in labels.items() if "Маска (1994)" in k))
    assert "The Mask (1994) BDRip" in session.sent(ALICE)[-1][1]
    # «Назад» на шаге жанра возвращает к выбору типа
    await press(ALICE, "pb")
    assert "Что ищем" in session.sent(ALICE)[-1][1]


async def test_plot_fallback_without_template(aiohttp_client, monkeypatch):
    """Если фильтр по карточке ничего не дал — ищем без него, но берём только статьи с ID TMDB."""
    async def wiki_api(request):
        q = request.query
        if "hastemplate" in q["gsrsearch"]:
            return web.json_response({"batchcomplete": True})
        return web.json_response({"query": {"pages": [
            {"pageid": 1, "index": 1, "title": "Во все тяжкие", "pageprops": {"wikibase_item": "Q1"}},
            {"pageid": 9, "index": 2, "title": "Метамфетамин", "pageprops": {"wikibase_item": "Q9"}}]}})

    async def tmdb_tv(request):
        return web.json_response({"id": 1396, "name": "Во все тяжкие", "original_name": "Breaking Bad",
                                  "first_air_date": "2008-01-20", "poster_path": "/b.jpg"})

    app = web.Application()
    app.router.add_get("/w/api.php", wiki_api)
    app.router.add_get("/3/tv/{id}", tmdb_tv)
    app.router.add_get("/sparql", sparql_handler({"Q1": ("t", "1396"), "Q9": None}))
    client = await aiohttp_client(app)
    monkeypatch.setattr(wiki, "WIKI_API", str(client.make_url("/w/api.php")))
    monkeypatch.setattr(wiki, "WIKIDATA_SPARQL", str(client.make_url("/sparql")))
    monkeypatch.setattr(tmdb, "API", str(client.make_url("/3")))
    infos = await wiki.search_by_plot(client.session, "k", "учитель химии варит метамфетамин")
    assert [(i.title, i.is_tv) for i in infos] == [("Во все тяжкие", True)]


# ---------- Викиданные не отвечают: 429 или долго — ищем в TMDB по названию и году ----------
async def _plot_without_wikidata(aiohttp_client, monkeypatch, sparql):
    async def wiki_api(request):
        if "hastemplate:" in request.query["gsrsearch"]:
            return web.json_response({"query": {"pages": [
                {"pageid": 10, "index": 1, "title": "Маска (фильм, 1994)", "pageprops": {"wikibase_item": "Q1"}},
                {"pageid": 11, "index": 2, "title": "Хранители (фильм)", "pageprops": {"wikibase_item": "Q2"}}]}})
        return web.json_response({"batchcomplete": True})

    got = []

    async def tmdb_search(request):
        name = request.query["query"]
        got.append(name)
        return web.json_response({"results": {
            "Маска": [mv(2, "Маска", "2020-01-01"), mv(1, "Маска", "1994-07-29")],
            "Хранители": [mv(3, "Хранители", "2009-03-05")]}.get(name, [])})

    async def tmdb_details(request):
        raise AssertionError("без ответа Викиданных ID TMDB неизвестны")

    app = web.Application()
    app.router.add_get("/w/api.php", wiki_api)
    app.router.add_get("/sparql", sparql)
    app.router.add_get("/3/search/multi", tmdb_search)
    app.router.add_get("/3/movie/{id}", tmdb_details)
    client = await aiohttp_client(app)
    monkeypatch.setattr(wiki, "WIKI_API", str(client.make_url("/w/api.php")))
    monkeypatch.setattr(wiki, "WIKIDATA_SPARQL", str(client.make_url("/sparql")))
    monkeypatch.setattr(wiki, "SPARQL_TIMEOUT", 0.3)
    monkeypatch.setattr(tmdb, "API", str(client.make_url("/3")))
    infos = await wiki.search_by_plot(client.session, "k", "находит маску становится зеленым")
    assert [(i.title, i.year) for i in infos] == [("Маска", "1994"), ("Хранители", "2009")]  # год из заголовка
    assert sorted(got) == ["Маска", "Хранители"]


async def test_plot_sparql_429(aiohttp_client, monkeypatch):
    async def sparql(request):
        return web.Response(status=429, text="Too Many Requests")
    await _plot_without_wikidata(aiohttp_client, monkeypatch, sparql)


async def test_plot_sparql_timeout(aiohttp_client, monkeypatch):
    async def sparql(request):
        await asyncio.sleep(2)
        return web.json_response({"results": {"bindings": []}})
    await _plot_without_wikidata(aiohttp_client, monkeypatch, sparql)


# ---------- опечатки и лимит Википедии ----------
async def test_plot_typo_fuzzy(aiohttp_client, monkeypatch):
    searches = []

    async def wiki_api(request):
        s = request.query["gsrsearch"]
        searches.append(s)
        if s.startswith("ахилеса~ гектора~ hastemplate:"):
            return web.json_response({"query": {"pages": [
                {"pageid": 1, "index": 1, "title": "Троя (фильм)", "pageprops": {"wikibase_item": "Q1"}}]}})
        return web.json_response({"batchcomplete": True})

    async def tmdb_movie(request):
        return web.json_response({"id": 652, "title": "Троя", "release_date": "2004-05-13", "poster_path": "/t.jpg"})

    app = web.Application()
    app.router.add_get("/w/api.php", wiki_api)
    app.router.add_get("/sparql", sparql_handler({"Q1": ("m", "652")}))
    app.router.add_get("/3/movie/{id}", tmdb_movie)
    client = await aiohttp_client(app)
    monkeypatch.setattr(wiki, "WIKI_API", str(client.make_url("/w/api.php")))
    monkeypatch.setattr(wiki, "WIKIDATA_SPARQL", str(client.make_url("/sparql")))
    monkeypatch.setattr(tmdb, "API", str(client.make_url("/3")))
    infos = await wiki.search_by_plot(client.session, "k", "фильм про ахилеса и гектора")
    assert [i.title for i in infos] == ["Троя"]
    assert searches[0].startswith("ахилеса гектора hastemplate:")          # сначала как есть
    assert searches[1].startswith("ахилеса~ гектора~ hastemplate:")        # потом нечётко


async def test_plot_429_waits(aiohttp_client, monkeypatch):
    calls = []

    async def wiki_api(request):
        calls.append(request.query["gsrsearch"])
        return web.Response(status=429, text="Too many requests", headers={"Retry-After": "100"})

    app = web.Application()
    app.router.add_get("/w/api.php", wiki_api)
    client = await aiohttp_client(app)
    monkeypatch.setattr(wiki, "WIKI_API", str(client.make_url("/w/api.php")))
    monkeypatch.setattr(wiki, "_busy_until", 0.0)
    try:
        await wiki.search_by_plot(client.session, "k", "находит маску становится зеленым")
        raise AssertionError("ожидали WikiBusy")
    except wiki.WikiBusy as e:
        assert 90 <= e.seconds <= 100
    assert len(calls) == 1                           # после 429 дальше не долбим
    try:
        await wiki.search_by_plot(client.session, "k", "другой запрос про пиратов")
        raise AssertionError("ожидали WikiBusy")
    except wiki.WikiBusy:
        pass
    assert len(calls) == 1                           # пока ждём — к Википедии не ходим вовсе
    assert 0 < wiki.busy_left() <= 100


async def test_plot_busy_message(env):
    st, session, send, press, mp = env

    async def busy(http, key, text, lang="ru-RU", limit=6):
        raise wiki.WikiBusy(100)
    mp.setattr(wiki, "search_by_plot", busy)
    await send(ALICE, "/plot мужик находит маску и становится зелёным")
    assert "просит подождать ~2 мин" in session.sent(ALICE)[-1][1]
