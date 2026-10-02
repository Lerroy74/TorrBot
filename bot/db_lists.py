"""v8: таблицы и запросы для списков, групп, «открытых» списков, кэша карточек TMDB и расхода ИИ.

Подмешивается в DB (bot/db.py). Права (кто что может) решает bot/lists.py — здесь только данные.
Фильм везде задаётся парой (kind, tmdb_id): kind — «m» (фильм) или «t» (сериал).
"""
from __future__ import annotations

import secrets
import sqlite3
import time

SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS titles (           -- кэш карточек TMDB (название, год, обложка, жанры)
    kind      TEXT,
    tmdb_id   INTEGER,
    title     TEXT,
    year      TEXT,
    poster    TEXT,
    genres    TEXT,
    PRIMARY KEY (kind, tmdb_id)
);
CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT,
    owner_id   INTEGER,
    invite     TEXT UNIQUE,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id   INTEGER,
    user_id    INTEGER,
    joined_at  INTEGER,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS lists (            -- владелец: user_id (личный) или group_id (групповой)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    group_id   INTEGER,
    name       TEXT,
    main       INTEGER DEFAULT 0,             -- 1 — основной («Хочу посмотреть» / список группы)
    share      TEXT UNIQUE,                   -- код ссылки «поделиться»
    created_by INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS list_items (
    list_id    INTEGER,
    kind       TEXT,
    tmdb_id    INTEGER,
    added_by   INTEGER,
    added_at   INTEGER,
    watched_at INTEGER,
    watched_by INTEGER,                       -- кто отметил (NULL — Kodi или оценка)
    PRIMARY KEY (list_id, kind, tmdb_id)
);
CREATE TABLE IF NOT EXISTS list_votes (
    list_id    INTEGER,
    kind       TEXT,
    tmdb_id    INTEGER,
    user_id    INTEGER,
    PRIMARY KEY (list_id, kind, tmdb_id, user_id)
);
CREATE TABLE IF NOT EXISTS list_shares (      -- кому открыт личный список
    list_id    INTEGER,
    user_id    INTEGER,
    at         INTEGER,
    PRIMARY KEY (list_id, user_id)
);
CREATE TABLE IF NOT EXISTS list_watch (         -- v8.2: что уже сообщали о раздачах фильма из списков
    user_id    INTEGER,
    kind       TEXT,
    tmdb_id    INTEGER,
    level      INTEGER,                       -- 0 — раздач нет, 1 — есть, 2 — есть хорошего качества
    at         INTEGER,
    PRIMARY KEY (user_id, kind, tmdb_id)
);
CREATE TABLE IF NOT EXISTS list_watch_film (    -- v8.2: когда проверяли фильм
    kind       TEXT,
    tmdb_id    INTEGER,
    checked_at INTEGER,
    PRIMARY KEY (kind, tmdb_id)
);
CREATE TABLE IF NOT EXISTS ai_usage (         -- расход ИИ по дням
    day        TEXT,
    user_id    INTEGER,
    feature    TEXT,
    provider   TEXT,
    req        INTEGER DEFAULT 0,
    tin        INTEGER DEFAULT 0,
    tout       INTEGER DEFAULT 0,
    PRIMARY KEY (day, user_id, feature, provider)
);
"""


def _code() -> str:
    return secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")


class ListsDB:
    c: sqlite3.Connection

    def _v8_migrate(self, first_run: bool) -> None:
        """Вызывается из DB.__init__ после создания таблиц."""
        jcols = {r["name"] for r in self.c.execute("PRAGMA table_info(journal)")}
        for col, typ in (("tmdb_kind", "TEXT"), ("tmdb_id", "INTEGER"), ("src", "TEXT")):
            if col not in jcols:
                self.c.execute(f"ALTER TABLE journal ADD COLUMN {col} {typ}")
        if first_run:                     # все, кто уже был в боте до v8, могут качать как раньше
            self.c.execute("INSERT OR REPLACE INTO prefs(user_id, key, value) SELECT id, 'can_dl', '1' FROM users")
        self.journal_tmdb_backfill(commit=False)

    def journal_tmdb_backfill(self, commit: bool = True) -> None:
        """Записи журнала без карточки TMDB — взять её из закачек этого названия."""
        self.c.execute(
            "UPDATE journal SET tmdb_kind=(SELECT d.tmdb_kind FROM downloads d WHERE d.jid=journal.id"
            " AND d.tmdb_id IS NOT NULL ORDER BY d.added_at DESC LIMIT 1),"
            " tmdb_id=(SELECT d.tmdb_id FROM downloads d WHERE d.jid=journal.id AND d.tmdb_id IS NOT NULL"
            " ORDER BY d.added_at DESC LIMIT 1)"
            " WHERE tmdb_id IS NULL AND EXISTS (SELECT 1 FROM downloads d WHERE d.jid=journal.id AND d.tmdb_id IS NOT NULL)")
        if commit:
            self.c.commit()

    # ---------- карточки TMDB ----------
    def title_put(self, kind: str, tid: int, title: str, year: str = "", poster: str | None = None,
                  genres: list[int] | None = None) -> None:
        old = self.title_get(kind, tid)
        g = ",".join(str(x) for x in (genres or [])) or (old["genres"] if old else "")
        self.c.execute("INSERT OR REPLACE INTO titles(kind, tmdb_id, title, year, poster, genres) VALUES (?,?,?,?,?,?)",
                       (kind, tid, title or (old["title"] if old else ""), year or (old["year"] if old else ""),
                        poster or (old["poster"] if old else None), g))
        self.c.commit()

    def title_get(self, kind: str, tid: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM titles WHERE kind=? AND tmdb_id=?", (kind, tid)).fetchone()

    # ---------- группы ----------
    def group_create(self, name: str, owner: int) -> int:
        now = int(time.time())
        gid = self.c.execute("INSERT INTO teams(name, owner_id, invite, created_at) VALUES (?,?,?,?)",
                             (name[:60], owner, _code(), now)).lastrowid
        self.c.execute("INSERT INTO group_members(group_id, user_id, joined_at) VALUES (?,?,?)", (gid, owner, now))
        self.c.execute("INSERT INTO lists(group_id, name, main, created_by, created_at) VALUES (?,?,1,?,?)",
                       (gid, "Хотим посмотреть", owner, now))
        self.c.commit()
        return gid

    def group_get(self, gid: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM teams WHERE id=?", (gid,)).fetchone()

    def group_by_invite(self, code: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM teams WHERE invite=?", (code,)).fetchone()

    def groups_of(self, uid: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT g.* FROM teams g JOIN group_members m ON m.group_id=g.id"
                              " WHERE m.user_id=? ORDER BY g.created_at", (uid,)).fetchall()

    def group_members(self, gid: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM group_members WHERE group_id=? ORDER BY joined_at, rowid",
                              (gid,)).fetchall()

    def is_member(self, gid: int, uid: int) -> bool:
        return self.c.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?",
                              (gid, uid)).fetchone() is not None

    def group_join(self, gid: int, uid: int) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO group_members(group_id, user_id, joined_at) VALUES (?,?,?)",
                             (gid, uid, int(time.time())))
        self.c.commit()
        return cur.rowcount > 0

    def group_leave(self, gid: int, uid: int) -> tuple[str, int | None]:
        """Выйти из группы. ('left'|'owner'|'deleted', новый владелец). Владелец уходит — группа
        переходит самому давнему участнику; ушёл последний — группа удаляется со списками."""
        g = self.group_get(gid)
        if g is None:
            return "deleted", None
        self.c.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, uid))
        rest = self.group_members(gid)
        if not rest:
            self.group_delete(gid, commit=False)
            self.c.commit()
            return "deleted", None
        if g["owner_id"] == uid:
            new = rest[0]["user_id"]
            self.c.execute("UPDATE teams SET owner_id=? WHERE id=?", (new, gid))
            self.c.commit()
            return "owner", new
        self.c.commit()
        return "left", None

    def group_rename(self, gid: int, name: str) -> None:
        self.c.execute("UPDATE teams SET name=? WHERE id=?", (name[:60], gid))
        self.c.commit()

    def group_new_invite(self, gid: int) -> str:
        code = _code()
        self.c.execute("UPDATE teams SET invite=? WHERE id=?", (code, gid))
        self.c.commit()
        return code

    def group_delete(self, gid: int, commit: bool = True) -> None:
        for (lid,) in self.c.execute("SELECT id FROM lists WHERE group_id=?", (gid,)).fetchall():
            self._list_wipe(lid)
        self.c.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
        self.c.execute("DELETE FROM teams WHERE id=?", (gid,))
        if commit:
            self.c.commit()

    def circle(self, uid: int) -> set[int]:
        """Все, с кем человек состоит хотя бы в одной группе (и он сам)."""
        rows = self.c.execute("SELECT DISTINCT m2.user_id FROM group_members m1 JOIN group_members m2"
                              " ON m1.group_id=m2.group_id WHERE m1.user_id=?", (uid,)).fetchall()
        return {r[0] for r in rows} | {uid}

    # ---------- списки ----------
    def main_list(self, uid: int) -> int:
        """Личный «Хочу посмотреть» (создаётся при первом обращении)."""
        row = self.c.execute("SELECT id FROM lists WHERE user_id=? AND main=1", (uid,)).fetchone()
        if row:
            return row[0]
        lid = self.c.execute("INSERT INTO lists(user_id, name, main, created_by, created_at) VALUES (?,?,1,?,?)",
                             (uid, "Хочу посмотреть", uid, int(time.time()))).lastrowid
        self.c.commit()
        return lid

    def group_main_list(self, gid: int) -> int:
        row = self.c.execute("SELECT id FROM lists WHERE group_id=? AND main=1", (gid,)).fetchone()
        if row:
            return row[0]
        g = self.group_get(gid)
        lid = self.c.execute("INSERT INTO lists(group_id, name, main, created_by, created_at) VALUES (?,?,1,?,?)",
                             (gid, "Хотим посмотреть", g["owner_id"] if g else None, int(time.time()))).lastrowid
        self.c.commit()
        return lid

    def list_get(self, lid: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM lists WHERE id=?", (lid,)).fetchone()

    def lists_of_user(self, uid: int) -> list[sqlite3.Row]:
        self.main_list(uid)
        return self.c.execute("SELECT * FROM lists WHERE user_id=? ORDER BY main DESC, created_at, id",
                              (uid,)).fetchall()

    def lists_of_group(self, gid: int) -> list[sqlite3.Row]:
        self.group_main_list(gid)
        return self.c.execute("SELECT * FROM lists WHERE group_id=? ORDER BY main DESC, created_at, id",
                              (gid,)).fetchall()

    def list_create(self, name: str, created_by: int, uid: int | None = None, gid: int | None = None) -> int:
        lid = self.c.execute("INSERT INTO lists(user_id, group_id, name, main, created_by, created_at)"
                             " VALUES (?,?,?,0,?,?)", (uid, gid, name[:60], created_by, int(time.time()))).lastrowid
        self.c.commit()
        return lid

    def list_rename(self, lid: int, name: str) -> None:
        self.c.execute("UPDATE lists SET name=? WHERE id=?", (name[:60], lid))
        self.c.commit()

    def _list_wipe(self, lid: int) -> None:
        for t in ("list_items", "list_votes", "list_shares"):
            self.c.execute(f"DELETE FROM {t} WHERE list_id=?", (lid,))
        self.c.execute("DELETE FROM lists WHERE id=?", (lid,))

    def list_delete(self, lid: int) -> None:
        self._list_wipe(lid)
        self.c.commit()

    def list_counts(self, lid: int) -> tuple[int, int]:
        """(непросмотренных, всего)."""
        row = self.c.execute("SELECT SUM(watched_at IS NULL), COUNT(*) FROM list_items WHERE list_id=?",
                             (lid,)).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def list_items(self, lid: int, watched: bool = False) -> list[sqlite3.Row]:
        """Фильмы списка с карточкой и числом 👍. watched=False — только непросмотренные.
        Групповой — больше 👍 выше; личный — новые сверху; просмотренные — в конце."""
        lst = self.list_get(lid)
        order = "votes DESC, i.added_at" if lst is not None and lst["group_id"] else "i.added_at DESC"
        q = ("SELECT i.*, t.title, t.year, t.poster, t.genres,"
             " (SELECT COUNT(*) FROM list_votes v WHERE v.list_id=i.list_id AND v.kind=i.kind AND v.tmdb_id=i.tmdb_id) AS votes,"
             " (SELECT group_concat(user_id) FROM list_votes v WHERE v.list_id=i.list_id AND v.kind=i.kind"
             "  AND v.tmdb_id=i.tmdb_id) AS voters"
             " FROM list_items i LEFT JOIN titles t ON t.kind=i.kind AND t.tmdb_id=i.tmdb_id WHERE i.list_id=?")
        if not watched:
            q += " AND i.watched_at IS NULL"
        q += f" ORDER BY i.watched_at IS NOT NULL, {order}"
        return self.c.execute(q, (lid,)).fetchall()

    def list_item(self, lid: int, kind: str, tid: int) -> sqlite3.Row | None:
        return self.c.execute(
            "SELECT i.*, t.title, t.year, t.poster, t.genres,"
            " (SELECT COUNT(*) FROM list_votes v WHERE v.list_id=i.list_id AND v.kind=i.kind AND v.tmdb_id=i.tmdb_id) AS votes,"
            " (SELECT group_concat(user_id) FROM list_votes v WHERE v.list_id=i.list_id AND v.kind=i.kind"
            "  AND v.tmdb_id=i.tmdb_id) AS voters"
            " FROM list_items i LEFT JOIN titles t ON t.kind=i.kind AND t.tmdb_id=i.tmdb_id"
            " WHERE i.list_id=? AND i.kind=? AND i.tmdb_id=?", (lid, kind, tid)).fetchone()

    def list_add(self, lid: int, kind: str, tid: int, uid: int, ts: int | None = None) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO list_items(list_id, kind, tmdb_id, added_by, added_at)"
                             " VALUES (?,?,?,?,?)", (lid, kind, tid, uid, ts or int(time.time())))
        lst = self.list_get(lid)
        if cur.rowcount and lst is not None and lst["group_id"]:     # в группе — сразу голос автора
            self.c.execute("INSERT OR IGNORE INTO list_votes VALUES (?,?,?,?)", (lid, kind, tid, uid))
        self.c.commit()
        return cur.rowcount > 0

    def list_remove(self, lid: int, kind: str, tid: int) -> None:
        self.c.execute("DELETE FROM list_items WHERE list_id=? AND kind=? AND tmdb_id=?", (lid, kind, tid))
        self.c.execute("DELETE FROM list_votes WHERE list_id=? AND kind=? AND tmdb_id=?", (lid, kind, tid))
        self.c.commit()

    def lists_with(self, kind: str, tid: int) -> set[int]:
        return {r[0] for r in self.c.execute("SELECT list_id FROM list_items WHERE kind=? AND tmdb_id=?",
                                             (kind, tid))}

    def list_vote(self, lid: int, kind: str, tid: int, uid: int) -> bool:
        """Переключает 👍; True — поставлен."""
        if self.c.execute("DELETE FROM list_votes WHERE list_id=? AND kind=? AND tmdb_id=? AND user_id=?",
                          (lid, kind, tid, uid)).rowcount:
            self.c.commit()
            return False
        self.c.execute("INSERT INTO list_votes VALUES (?,?,?,?)", (lid, kind, tid, uid))
        self.c.commit()
        return True

    def set_watched(self, lid: int, kind: str, tid: int, on: bool, uid: int | None = None) -> None:
        if on:
            self.c.execute("UPDATE list_items SET watched_at=?, watched_by=? WHERE list_id=? AND kind=? AND tmdb_id=?"
                           " AND watched_at IS NULL", (int(time.time()), uid, lid, kind, tid))
        else:
            self.c.execute("UPDATE list_items SET watched_at=NULL, watched_by=NULL WHERE list_id=? AND kind=?"
                           " AND tmdb_id=?", (lid, kind, tid))
        self.c.commit()

    def watched_personal(self, uid: int, kind: str, tid: int) -> int:
        """Человек оценил фильм — ✅ в его личных списках. Вернёт, сколько отмечено."""
        cur = self.c.execute("UPDATE list_items SET watched_at=? WHERE kind=? AND tmdb_id=? AND watched_at IS NULL"
                             " AND list_id IN (SELECT id FROM lists WHERE user_id=?)",
                             (int(time.time()), kind, tid, uid))
        self.c.commit()
        return cur.rowcount

    def watched_kodi(self, uid: int, kind: str, tid: int) -> int:
        """Kodi отметил просмотренным то, что скачал uid: ✅ в его личных списках и в списках его групп."""
        cur = self.c.execute(
            "UPDATE list_items SET watched_at=? WHERE kind=? AND tmdb_id=? AND watched_at IS NULL AND list_id IN"
            " (SELECT id FROM lists WHERE user_id=? OR group_id IN (SELECT group_id FROM group_members WHERE user_id=?))",
            (int(time.time()), kind, tid, uid, uid))
        self.c.commit()
        return cur.rowcount

    # ---------- «поделиться» ----------
    def list_share_code(self, lid: int) -> str:
        row = self.list_get(lid)
        if row is not None and row["share"]:
            return row["share"]
        code = _code()
        self.c.execute("UPDATE lists SET share=? WHERE id=?", (code, lid))
        self.c.commit()
        return code

    def list_by_share(self, code: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM lists WHERE share=?", (code,)).fetchone()

    def share_add(self, lid: int, uid: int) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO list_shares(list_id, user_id, at) VALUES (?,?,?)",
                             (lid, uid, int(time.time())))
        self.c.commit()
        return cur.rowcount > 0

    def shares_of(self, lid: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM list_shares WHERE list_id=? ORDER BY at", (lid,)).fetchall()

    def shared_with(self, uid: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT l.* FROM lists l JOIN list_shares s ON s.list_id=l.id WHERE s.user_id=?"
                              " ORDER BY s.at", (uid,)).fetchall()

    def is_shared_with(self, lid: int, uid: int) -> bool:
        return self.c.execute("SELECT 1 FROM list_shares WHERE list_id=? AND user_id=?", (lid, uid)).fetchone() is not None

    def share_revoke(self, lid: int, uid: int | None = None) -> None:
        """Закрыть доступ одному человеку или всем (тогда и ссылка перестаёт работать)."""
        if uid is None:
            self.c.execute("DELETE FROM list_shares WHERE list_id=?", (lid,))
            self.c.execute("UPDATE lists SET share=NULL WHERE id=?", (lid,))
        else:
            self.c.execute("DELETE FROM list_shares WHERE list_id=? AND user_id=?", (lid, uid))
        self.c.commit()

    # ---------- оценки по карточке TMDB ----------
    def journal_by_tmdb(self, kind: str, tid: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM journal WHERE tmdb_kind=? AND tmdb_id=? ORDER BY id LIMIT 1",
                              (kind, tid)).fetchone()

    def journal_for_title(self, kind: str, tid: int, label: str, poster: str | None = None) -> int:
        """Запись журнала для фильма из списка (найти по TMDB, потом по названию, иначе создать «из списков»)."""
        row = self.journal_by_tmdb(kind, tid)
        if row:
            return row["id"]
        from .db import journal_key
        key = journal_key(label)
        row = self.c.execute("SELECT * FROM journal WHERE key=?", (key,)).fetchone()
        if row and row["tmdb_id"] is None:
            self.c.execute("UPDATE journal SET tmdb_kind=?, tmdb_id=? WHERE id=?", (kind, tid, row["id"]))
            self.c.commit()
            return row["id"]
        if row:                                   # то же название, но другой фильм — своя запись
            key = f"{key} tmdb{kind}{tid}"
        jid = self.c.execute(
            "INSERT INTO journal(key, kind, label, poster, added_at, deleted_at, tmdb_kind, tmdb_id, src)"
            " VALUES (?,?,?,?,?,?,?,?,'list')",
            (key, "series" if kind == "t" else "movies", label[:200], poster, int(time.time()), int(time.time()),
             kind, tid)).lastrowid
        self.c.commit()
        return jid

    def journal_set_tmdb(self, jid: int, kind: str, tid: int) -> None:
        self.c.execute("UPDATE journal SET tmdb_kind=?, tmdb_id=? WHERE id=?", (kind, tid, jid))
        self.c.commit()

    def scores_by_tmdb(self, uid: int) -> dict[tuple[str, int], int | None]:
        """{(kind, tmdb_id): оценка или None («не смотрел(а)»)} — всё, на что человек ответил."""
        rows = self.c.execute("SELECT j.tmdb_kind, j.tmdb_id, r.score FROM ratings r JOIN journal j ON j.id=r.jid"
                              " WHERE r.user_id=? AND j.tmdb_id IS NOT NULL", (uid,)).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def scores_for(self, kind: str, tid: int) -> list[sqlite3.Row]:
        """Все ответы по фильму: user_id, score (лучшие сверху, «не смотрел» — в конце)."""
        return self.c.execute("SELECT r.user_id, r.score FROM ratings r JOIN journal j ON j.id=r.jid"
                              " WHERE j.tmdb_kind=? AND j.tmdb_id=? ORDER BY r.score IS NULL, r.score DESC, r.at",
                              (kind, tid)).fetchall()

    def top_rated(self, uids: list[int], min_score: int = 8, limit: int = 30) -> list[sqlite3.Row]:
        """Высоко оценённое этими людьми: jid, label, tmdb_kind, tmdb_id, best, cnt (сколько из них оценили)."""
        if not uids:
            return []
        marks = ",".join("?" * len(uids))
        return self.c.execute(
            f"SELECT j.id AS jid, j.label, j.kind, j.tmdb_kind, j.tmdb_id, MAX(r.score) AS best, COUNT(*) AS cnt,"
            f" MAX(r.at) AS at FROM ratings r JOIN journal j ON j.id=r.jid"
            f" WHERE r.user_id IN ({marks}) AND r.score>=? GROUP BY j.id ORDER BY cnt DESC, best DESC, at DESC LIMIT ?",
            (*uids, min_score, limit)).fetchall()

    # ---------- расход ИИ ----------
    def ai_note(self, day: str, uid: int, feature: str, provider: str, tin: int = 0, tout: int = 0) -> None:
        self.c.execute("INSERT OR IGNORE INTO ai_usage(day, user_id, feature, provider) VALUES (?,?,?,?)",
                       (day, uid, feature, provider))
        self.c.execute("UPDATE ai_usage SET req=req+1, tin=tin+?, tout=tout+? WHERE day=? AND user_id=? AND feature=?"
                       " AND provider=?", (tin, tout, day, uid, feature, provider))
        self.c.commit()

    def ai_requests(self, day: str, uid: int | None = None) -> int:
        if uid is None:
            row = self.c.execute("SELECT SUM(req) FROM ai_usage WHERE day=? AND provider!='stt'", (day,)).fetchone()
        else:
            row = self.c.execute("SELECT SUM(req) FROM ai_usage WHERE day=? AND user_id=? AND provider!='stt'",
                                 (day, uid)).fetchone()
        return int(row[0] or 0)

    def ai_usage(self, prefix: str) -> list[sqlite3.Row]:
        """Расход за день («2026-10-02») или месяц («2026-10») по сервисам и функциям."""
        return self.c.execute("SELECT provider, feature, SUM(req) AS req, SUM(tin) AS tin, SUM(tout) AS tout"
                              " FROM ai_usage WHERE day LIKE ? GROUP BY provider, feature ORDER BY provider, feature",
                              (prefix + "%",)).fetchall()
