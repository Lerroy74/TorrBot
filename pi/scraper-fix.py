#!/usr/bin/env python3
"""Чинит скрейперы TMDB в Kodi (запускает сторож pi-watch.sh; можно и руками).

Скрейперы ходят в TMDB напрямую (urllib мимо прокси Kodi) и без тайм-аута. Провайдер рвёт
соединения с TMDB/CloudFront после ~20 КБ — скрейпер висит вечно, а с ним обновление медиатеки.
Правка: запросы идут через сеть самого Kodi (xbmcvfs → настройки прокси Kodi, свои тайм-ауты),
если не вышло — напрямую с тайм-аутом 30 с. Kodi обновил скрейпер — сторож поправит снова.
"""
import re
import sys

FILES = [
    "/storage/.kodi/addons/metadata.tvshows.themoviedb.org.python/libs/api_utils.py",
    "/storage/.kodi/addons/metadata.themoviedb.org.python/python/lib/tmdbscraper/api_utils.py",
]
MARK = "# torrbot-fix"
HELPER = '''

''' + MARK + '''
def _torrbot_urlopen(req):
    """Запрос через сеть Kodi (его прокси и тайм-ауты); не вышло — напрямую, 30 с."""
    import io
    try:
        import xbmcvfs
        from urllib.parse import quote
        url = req.full_url
        hdrs = "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in req.header_items())
        f = xbmcvfs.File(url + ("|" + hdrs if hdrs else ""))
        try:
            data = f.readBytes()
        finally:
            f.close()
        if data:
            return io.BytesIO(bytes(data))
    except Exception:
        pass
    return urlopen(req, timeout=30)
'''


def fix(path):
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return None
    if MARK in src:
        return False
    new, n = re.subn(r"urlopen\(req(?:,\s*timeout=\d+)?\)", "_torrbot_urlopen(req)", src)
    if not n:
        return False
    open(path, "w", encoding="utf-8").write(new.rstrip("\n") + HELPER)
    return True


if __name__ == "__main__":
    files = sys.argv[1:] or FILES
    changed = [p for p in files if fix(p)]
    for p in changed:
        print("исправлен:", p)
    sys.exit(0)
