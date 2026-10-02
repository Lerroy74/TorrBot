"""v7: детский режим (включает админ в /users для конкретного человека).

Такой пользователь видит в поиске, подборе, у актёров и в «все части» только мультфильмы,
семейное и детское (по жанрам TMDB), а скачать может только то, у чего возрастной рейтинг
не выше KIDS_MAX_AGE (RU, иначе US; рейтинга нет — хватает жанра). Поиск «как есть» по
трекерам и magnet-ссылки в детском режиме недоступны: без карточки TMDB не проверить.
"""
from __future__ import annotations

import logging

from . import tmdb

log = logging.getLogger("torrbot")
KIDS = "kids"                    # prefs: детский режим


def is_kid(st, uid: int) -> bool:
    return uid not in st.cfg.admin_ids and st.db.flag(uid, KIDS)


def only_kids(st, uid: int, infos: list[tmdb.Info]) -> list[tmdb.Info]:
    return [i for i in infos if tmdb.kid_ok(i)] if is_kid(st, uid) else infos


async def allowed(st, uid: int, info: tmdb.Info | None) -> tuple[bool, str]:
    """Можно ли этому человеку качать это. (да/нет, почему нет)."""
    if not is_kid(st, uid):
        return True, ""
    if info is None:
        return False, "В детском режиме можно качать только то, что нашлось в каталоге фильмов."
    if not info.genres and st.cfg.tmdb_key:            # жанров нет в карточке — дозапросим
        try:
            full = await tmdb.details(st.tmdb_http, st.cfg.tmdb_key, "tv" if info.is_tv else "movie",
                                      info.tmdb_id, st.cfg.tmdb_lang)
            info.genres = full.genres if full else []
        except Exception as e:
            log.info("kids: tmdb %r", e)
    if not tmdb.kid_ok(info):
        return False, "Это не мультфильм и не семейное кино — в детском режиме нельзя."
    if st.cfg.tmdb_key:
        try:
            age = await tmdb.age_rating(st.tmdb_http, st.cfg.tmdb_key, info)
        except Exception as e:
            log.info("kids: возраст %r", e)
            age = None
        if age is not None and age > st.cfg.kids_max_age:
            return False, f"Это {age}+ — в детском режиме можно до {st.cfg.kids_max_age}+."
    return True, ""
