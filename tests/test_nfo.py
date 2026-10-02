"""v8.3: подсказки .nfo для Kodi."""
import os
import types

from bot import nfo, tmdb

MB = 1024 ** 2


def mkfile(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def test_parse_and_exact_match():
    assert nfo.parse_folder("Троя (2004)") == ("Троя", "2004")
    assert nfo.parse_folder("Angelskie_glazki_MS_AVI.avi") is None
    c = [{"media_type": "movie", "id": 652, "title": "Троя", "original_title": "Troy", "release_date": "2004-05-13"},
         {"media_type": "movie", "id": 9, "title": "Троя", "release_date": "2010-01-01"},
         {"media_type": "tv", "id": 5, "name": "Троя", "first_air_date": "2004-01-01"}]
    assert nfo.exact_match(c, "Троя", "2004", "m") == 652
    assert nfo.exact_match(c, "Троя", "2004", "t") == 5
    assert nfo.exact_match(c, "Троя", "1999", "m") is None
    two = c + [{"media_type": "movie", "id": 653, "title": "Troy", "original_title": "Троя", "release_date": "2004-02-02"}]
    assert nfo.exact_match(two, "Троя", "2004", "m") is None          # два кандидата — не угадываем


def test_write_movie_nested_and_series(tmp_path):
    movies, series = str(tmp_path / "movies"), str(tmp_path / "series")
    d = os.path.join(movies, "Мой ангел-хранитель (2009)")
    mkfile(os.path.join(d, "Moj.angel.2009", "Moj.angel.2009.mkv"), 60 * MB)
    mkfile(os.path.join(d, "Moj.angel.2009", "sample.mkv"), 60 * MB)
    assert nfo.write("m", 123, d, series) == 1
    assert open(os.path.join(d, "Moj.angel.2009", "Moj.angel.2009.nfo")).read().strip() == \
        "https://www.themoviedb.org/movie/123"
    assert nfo.has_hint("m", d) and nfo.write("m", 123, d, series) == 0   # второй раз — не трогаем
    show = os.path.join(series, "Лэндмен (2024)")
    mkfile(os.path.join(show, "S01", "e1.mkv"), 60 * MB)
    assert nfo.write("t", 157741, show, series) == 1
    assert "tv/157741" in open(os.path.join(show, "tvshow.nfo")).read()
    assert nfo.write("t", 1, os.path.join(show, "S01"), series) == 0      # не верхняя папка — нет


def test_tidy_after_cleanup(tmp_path):
    movies = str(tmp_path / "movies")
    d = os.path.join(movies, "Троя (2004)")
    mkfile(os.path.join(d, "Troy.mkv"), 60 * MB)
    nfo.write("m", 652, d, str(tmp_path / "series"))
    nfo.tidy(d, (movies,))
    assert os.path.exists(os.path.join(d, "Troy.nfo"))                  # видео ещё есть — не трогаем
    os.unlink(os.path.join(d, "Troy.mkv"))
    nfo.tidy(d, (movies,))
    assert not os.path.exists(d) and os.path.isdir(movies)


async def test_sync_uses_download_id_then_folder_name(tmp_path, monkeypatch):
    movies, series = str(tmp_path / "movies"), str(tmp_path / "series")
    os.makedirs(series)
    mkfile(os.path.join(movies, "Маска (1994)", "Mask.1994.mkv"), 60 * MB)
    mkfile(os.path.join(movies, "Троя (2004)", "Troy.Directors.Cut.mkv"), 60 * MB)
    mkfile(os.path.join(movies, "Непонятное (2001)", "x.mkv"), 60 * MB)
    mkfile(os.path.join(movies, "Angelskie_glazki.avi"), 60 * MB)
    rows = {"a" * 40: {"tmdb_id": 854, "tmdb_kind": "m"}}

    class Tr:
        async def get(self, hashes=None):
            return [{"hashString": "a" * 40, "name": "Mask.1994.mkv", "downloadDir": f"{movies}/Маска (1994)"}]
    calls = []

    async def search(http, key, q, lang="ru-RU"):
        calls.append(q)
        if q == "Троя":
            return [{"media_type": "movie", "id": 652, "title": "Троя", "release_date": "2004-05-13"}]
        return []
    monkeypatch.setattr(tmdb, "search", search)
    st = types.SimpleNamespace(
        cfg=types.SimpleNamespace(dir_movies=movies, dir_series=series, tmdb_key="k", tmdb_lang="ru-RU"),
        tr=Tr(), db=types.SimpleNamespace(get=lambda h: rows.get(h)), tmdb_http=None, kodi=None)
    assert await nfo.sync(st) == 2
    assert "movie/854" in open(os.path.join(movies, "Маска (1994)", "Mask.1994.nfo")).read()
    assert "movie/652" in open(os.path.join(movies, "Троя (2004)", "Troy.Directors.Cut.nfo")).read()
    assert not os.path.exists(os.path.join(movies, "Непонятное (2001)", "x.nfo"))
    assert sorted(calls) == ["Непонятное", "Троя"]                       # «Маску» знали из закачки
    assert await nfo.sync(st) == 0 and len(calls) == 2                    # промах не ищем сутки
