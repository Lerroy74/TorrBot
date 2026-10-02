"""SQLite: кому разрешён доступ и что кем поставлено на закачку.
Таблица downloads хранит время добавления и завершения — пригодится для будущей
автоочистки старых фильмов."""
from __future__ import annotations

import os
import re
import sqlite3
import time

from .db_lists import SCHEMA_V8, ListsDB

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY,
    name      TEXT,
    added_at  INTEGER
);
CREATE TABLE IF NOT EXISTS requests (
    id        INTEGER PRIMARY KEY,
    name      TEXT,
    at        INTEGER
);
CREATE TABLE IF NOT EXISTS blocked (
    id        INTEGER PRIMARY KEY,
    name      TEXT,
    at        INTEGER
);
CREATE TABLE IF NOT EXISTS prefs (
    user_id   INTEGER,
    key       TEXT,
    value     TEXT,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS wishlist (
    kind      TEXT,
    tmdb_id   INTEGER,
    title     TEXT,
    year      TEXT,
    poster    TEXT,
    added_by  INTEGER,
    added_at  INTEGER,
    PRIMARY KEY (kind, tmdb_id)
);
CREATE TABLE IF NOT EXISTS votes (
    kind      TEXT,
    tmdb_id   INTEGER,
    user_id   INTEGER,
    PRIMARY KEY (kind, tmdb_id, user_id)
);
CREATE TABLE IF NOT EXISTS downloads (
    hash      TEXT PRIMARY KEY,
    name      TEXT,
    title     TEXT,
    category  TEXT,
    chat_id   INTEGER,
    user_id   INTEGER,
    added_at  INTEGER,
    done_at   INTEGER,
    removed   INTEGER DEFAULT 0,
    poster    TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT UNIQUE,
    kind       TEXT,
    label      TEXT,
    poster     TEXT,
    added_by   INTEGER,
    added_at   INTEGER,
    deleted_at INTEGER,
    deleted_by INTEGER,
    rating     INTEGER,
    rated_by   INTEGER,
    rated_at   INTEGER
);
CREATE TABLE IF NOT EXISTS ratings (
    jid       INTEGER,
    user_id   INTEGER,
    score     INTEGER,          -- 1..10; NULL — «не смотрел(а)»
    at        INTEGER,
    PRIMARY KEY (jid, user_id)
);
CREATE TABLE IF NOT EXISTS rating_asks (
    jid       INTEGER,
    user_id   INTEGER,
    reason    TEXT,             -- watched / delete / cleanup / pc / migrated
    at        INTEGER,
    PRIMARY KEY (jid, user_id, reason)
);
CREATE TABLE IF NOT EXISTS subs (           -- v7: подписка на сериал (одна на сериал, подписчиков много)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id    INTEGER UNIQUE,
    title      TEXT,
    year       TEXT,
    poster     TEXT,
    folder     TEXT,             -- папка сериала в series/
    details    TEXT,             -- тема на трекере, за которой следим
    hash       TEXT,             -- текущая раздача этой темы
    prev_hash  TEXT,             -- прошлая раздача: убрать, когда новая докачается
    season     INTEGER,
    last_ep    INTEGER,
    created_by INTEGER,
    created_at INTEGER,
    checked_at INTEGER,
    changed_at INTEGER,
    offered    TEXT,             -- «сезон:серия», про которую уже предлагали другие раздачи
    ended_note INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sub_users (
    sub_id     INTEGER,
    user_id    INTEGER,
    chat_id    INTEGER,
    PRIMARY KEY (sub_id, user_id)
);
CREATE TABLE IF NOT EXISTS sub_files (       -- файлы, которые уже качали: удалённые второй раз не качаем
    sub_id     INTEGER,
    name       TEXT,
    PRIMARY KEY (sub_id, name)
);
CREATE TABLE IF NOT EXISTS waits (           -- v7: «⏳ ждать хорошее качество»
    kind       TEXT,
    tmdb_id    INTEGER,
    title      TEXT,
    year       TEXT,
    poster     TEXT,
    user_id    INTEGER,
    chat_id    INTEGER,
    created_at INTEGER,
    checked_at INTEGER,
    PRIMARY KEY (kind, tmdb_id, user_id)
);
CREATE TABLE IF NOT EXISTS deletions (
    at        INTEGER,
    user_id   INTEGER,
    kind      TEXT,
    label     TEXT,
    size      INTEGER
);
"""


class DB(ListsDB):
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        tables = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        migrate_ratings = "journal" in tables and "ratings" not in tables
        v8_first = "users" in tables and "lists" not in tables      # обновление с 7.x на 8.0
        if migrate_ratings and path != ":memory:":      # v6.5: копия базы перед переносом оценок
            bak = sqlite3.connect(f"{path}.bak-6.4")
            self.c.backup(bak)
            bak.close()
        self.c.executescript(SCHEMA)
        self.c.executescript(SCHEMA_V8)
        if migrate_ratings:                             # семейная оценка → личная оценка того, кто ставил
            self.c.execute("INSERT OR IGNORE INTO ratings(jid, user_id, score, at)"
                           " SELECT id, rated_by, rating, COALESCE(rated_at, added_at) FROM journal"
                           " WHERE rating IS NOT NULL AND rated_by IS NOT NULL")
        # миграция: колонка poster появилась позже
        ucols = {r["name"] for r in self.c.execute("PRAGMA table_info(users)")}
        if "last_seen" not in ucols:
            self.c.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER")
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(downloads)")}
        if "poster" not in cols:
            self.c.execute("ALTER TABLE downloads ADD COLUMN poster TEXT")
        if "warned_at" not in cols:   # когда админа предупредили об удалении
            self.c.execute("ALTER TABLE downloads ADD COLUMN warned_at INTEGER")
        if "removed_at" not in cols:  # когда и почему удалено: cancel / cleanup / manual
            self.c.execute("ALTER TABLE downloads ADD COLUMN removed_at INTEGER")
            self.c.execute("ALTER TABLE downloads ADD COLUMN removed_reason TEXT")
        if "label" not in cols:       # понятное название «Название (год)» по TMDB
            self.c.execute("ALTER TABLE downloads ADD COLUMN label TEXT")
        if "keep" not in cols:        # админ нажал «Оставить» — не удалять никогда
            self.c.execute("ALTER TABLE downloads ADD COLUMN keep INTEGER DEFAULT 0")
        if "jid" not in cols:         # запись в журнале «что смотрели» (v6.3)
            self.c.execute("ALTER TABLE downloads ADD COLUMN jid INTEGER")
            self._journal_backfill()
        for col, typ in (("details", "TEXT"), ("tmdb_kind", "TEXT"), ("tmdb_id", "INTEGER"),
                         ("sub_id", "INTEGER"), ("stall_at", "INTEGER"), ("nospace_at", "INTEGER")):
            if col not in cols:       # v7: тема на трекере, фильм в TMDB, подписка, «зависла», «нет места»
                self.c.execute(f"ALTER TABLE downloads ADD COLUMN {col} {typ}")
        self._v8_migrate(v8_first)
        self.c.commit()

    # --- users ---
    def allow(self, uid: int, name: str = "") -> None:
        self.c.execute("INSERT OR REPLACE INTO users(id, name, added_at) VALUES (?,?,?)",
                       (uid, name, int(time.time())))
        self.c.execute("DELETE FROM requests WHERE id=?", (uid,))
        self.c.execute("DELETE FROM blocked WHERE id=?", (uid,))
        self.c.commit()

    def revoke(self, uid: int) -> bool:
        self.c.execute("DELETE FROM prefs WHERE user_id=? AND key IN ('can_delete','can_remote','kids','can_dl','ai')", (uid,))
        cur = self.c.execute("DELETE FROM users WHERE id=?", (uid,))
        self.c.commit()
        return cur.rowcount > 0

    def is_allowed(self, uid: int) -> bool:
        return self.c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None

    def users(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM users ORDER BY added_at").fetchall()

    def user(self, uid: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def touch(self, uid: int) -> None:
        self.c.execute("UPDATE users SET last_seen=? WHERE id=?", (int(time.time()), uid))
        self.c.commit()

    # --- запросы доступа и блокировки ---
    def add_request(self, uid: int, name: str) -> bool:
        """True — новый запрос (надо написать админу), False — уже ждёт."""
        cur = self.c.execute("INSERT OR IGNORE INTO requests(id, name, at) VALUES (?,?,?)",
                             (uid, name, int(time.time())))
        self.c.commit()
        return cur.rowcount > 0

    def drop_request(self, uid: int) -> None:
        self.c.execute("DELETE FROM requests WHERE id=?", (uid,))
        self.c.commit()

    def requests(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM requests ORDER BY at").fetchall()

    def block(self, uid: int, name: str = "") -> None:
        row = self.user(uid)
        name = name or (row["name"] if row else "") or ""
        self.c.execute("DELETE FROM users WHERE id=?", (uid,))
        self.c.execute("DELETE FROM requests WHERE id=?", (uid,))
        self.c.execute("DELETE FROM prefs WHERE user_id=? AND key IN ('can_delete','can_remote','kids','can_dl','ai')", (uid,))
        self.c.execute("INSERT OR REPLACE INTO blocked(id, name, at) VALUES (?,?,?)",
                       (uid, name, int(time.time())))
        self.c.commit()

    def unblock(self, uid: int) -> bool:
        cur = self.c.execute("DELETE FROM blocked WHERE id=?", (uid,))
        self.c.commit()
        return cur.rowcount > 0

    def is_blocked(self, uid: int) -> bool:
        return self.c.execute("SELECT 1 FROM blocked WHERE id=?", (uid,)).fetchone() is not None

    def blocked(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM blocked ORDER BY at").fetchall()

    def downloads_of(self, uid: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM downloads WHERE user_id=? ORDER BY added_at", (uid,)).fetchall()

    # --- downloads ---
    def add_download(self, h: str, name: str, title: str, category: str, chat_id: int, user_id: int,
                     poster: str | None = None, label: str | None = None, details: str | None = None,
                     tmdb_kind: str | None = None, tmdb_id: int | None = None, sub_id: int | None = None) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO downloads(hash,name,title,category,chat_id,user_id,added_at,poster,label,"
            "details,tmdb_kind,tmdb_id,sub_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, name, title, category, chat_id, user_id, int(time.time()), poster, label,
             details, tmdb_kind, tmdb_id, sub_id))
        self.c.commit()

    def set_download(self, h: str, **fields) -> None:
        """Обновить поля закачки (stall_at, nospace_at, sub_id…)."""
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.c.execute(f"UPDATE downloads SET {cols} WHERE hash=?", (*fields.values(), h))
        self.c.commit()

    def recent_done(self, limit: int = 8) -> list[sqlite3.Row]:
        """Последние докачанные и ещё не удалённые (для «📼 Что включить» в пульте)."""
        return self.c.execute("SELECT * FROM downloads WHERE done_at IS NOT NULL AND removed=0"
                              " ORDER BY done_at DESC LIMIT ?", (limit,)).fetchall()

    def pending(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM downloads WHERE done_at IS NULL AND removed=0").fetchall()

    def mark_done(self, h: str) -> None:
        self.c.execute("UPDATE downloads SET done_at=? WHERE hash=?", (int(time.time()), h))
        self.c.commit()

    def mark_removed(self, h: str, reason: str = "manual") -> None:
        now = int(time.time())
        self.c.execute("UPDATE downloads SET removed=1, removed_at=?, removed_reason=? WHERE hash=? AND removed=0",
                       (now, reason, h))
        # в журнале «удалено» — когда с диска ушла последняя раздача этого названия
        # (/delete отмечает сам: он знает, удалено название целиком или один сезон)
        if reason != "delete":
            self.c.execute("UPDATE journal SET deleted_at=? WHERE deleted_at IS NULL"
                           " AND id=(SELECT jid FROM downloads WHERE hash=?)"
                           " AND NOT EXISTS (SELECT 1 FROM downloads WHERE jid=journal.id AND removed=0)",
                           (now, h))
        self.c.commit()

    def since(self, ts: int) -> list[sqlite3.Row]:
        """Все закачки, добавленные, докачанные или удалённые после ts (для отчёта)."""
        return self.c.execute("SELECT * FROM downloads WHERE added_at>=? OR done_at>=? OR removed_at>=?",
                              (ts, ts, ts)).fetchall()

    # --- настройки пользователя ---
    def pref(self, uid: int, key: str, default: str = "") -> str:
        row = self.c.execute("SELECT value FROM prefs WHERE user_id=? AND key=?", (uid, key)).fetchone()
        return row["value"] if row else default

    def set_pref(self, uid: int, key: str, value: str) -> None:
        self.c.execute("INSERT OR REPLACE INTO prefs(user_id, key, value) VALUES (?,?,?)", (uid, key, value))
        self.c.commit()

    # --- «Хотим посмотреть» ---
    def wish_add(self, kind: str, tid: int, title: str, year: str, poster: str | None, uid: int) -> bool:
        added = self.c.execute("INSERT OR IGNORE INTO wishlist VALUES (?,?,?,?,?,?,?)",
                               (kind, tid, title, year, poster, uid, int(time.time()))).rowcount > 0
        if added:                      # голос автора; остальные голосуют через wish_vote
            self.c.execute("INSERT OR IGNORE INTO votes VALUES (?,?,?)", (kind, tid, uid))
        self.c.commit()
        return added

    def wish_remove(self, kind: str, tid: int) -> None:
        self.c.execute("DELETE FROM wishlist WHERE kind=? AND tmdb_id=?", (kind, tid))
        self.c.execute("DELETE FROM votes WHERE kind=? AND tmdb_id=?", (kind, tid))
        self.c.commit()

    def wish_vote(self, kind: str, tid: int, uid: int) -> bool:
        """Переключает голос; True — голос поставлен."""
        if self.c.execute("DELETE FROM votes WHERE kind=? AND tmdb_id=? AND user_id=?", (kind, tid, uid)).rowcount:
            self.c.commit()
            return False
        self.c.execute("INSERT INTO votes VALUES (?,?,?)", (kind, tid, uid))
        self.c.commit()
        return True

    def wishlist(self) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT w.*, (SELECT COUNT(*) FROM votes v WHERE v.kind=w.kind AND v.tmdb_id=w.tmdb_id) AS n,"
            " (SELECT group_concat(user_id) FROM votes v WHERE v.kind=w.kind AND v.tmdb_id=w.tmdb_id) AS voters"
            " FROM wishlist w ORDER BY n DESC, added_at").fetchall()

    def finished(self) -> list[sqlite3.Row]:
        """Докачанные и ещё не удалённые — кандидаты на автоочистку."""
        return self.c.execute(
            "SELECT * FROM downloads WHERE done_at IS NOT NULL AND removed=0 ORDER BY done_at").fetchall()

    def set_warned(self, h: str, ts: int | None) -> None:
        self.c.execute("UPDATE downloads SET warned_at=? WHERE hash=?", (ts, h))
        self.c.commit()

    def set_keep(self, h: str, keep: bool = True) -> bool:
        cur = self.c.execute("UPDATE downloads SET keep=?, warned_at=NULL WHERE hash=?", (int(keep), h))
        self.c.commit()
        return cur.rowcount > 0

    # --- удаление вручную (/delete) ---
    def can_delete(self, uid: int) -> bool:
        return self.pref(uid, "can_delete") == "1"

    def set_can_delete(self, uid: int, on: bool) -> None:
        if on:
            self.set_pref(uid, "can_delete", "1")
        else:
            self.c.execute("DELETE FROM prefs WHERE user_id=? AND key='can_delete'", (uid,))
            self.c.commit()

    # --- пульт и детский режим (v7) ---
    def flag(self, uid: int, key: str) -> bool:
        return self.pref(uid, key) == "1"

    def set_flag(self, uid: int, key: str, on: bool) -> None:
        if on:
            self.set_pref(uid, key, "1")
        else:
            self.c.execute("DELETE FROM prefs WHERE user_id=? AND key=?", (uid, key))
            self.c.commit()

    # --- подписки на сериалы (v7) ---
    def sub_get(self, sid: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM subs WHERE id=?", (sid,)).fetchone()

    def sub_by_tmdb(self, tmdb_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM subs WHERE tmdb_id=?", (tmdb_id,)).fetchone()

    def sub_create(self, tmdb_id: int, title: str, year: str, poster: str | None, folder: str,
                   uid: int, details: str | None = None, h: str | None = None,
                   season: int | None = None, last_ep: int | None = None) -> int:
        row = self.sub_by_tmdb(tmdb_id)
        if row:
            return row["id"]
        now = int(time.time())
        cur = self.c.execute(
            "INSERT INTO subs(tmdb_id,title,year,poster,folder,details,hash,season,last_ep,created_by,"
            "created_at,changed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tmdb_id, title, year, poster, folder, details, h, season, last_ep, uid, now, now))
        self.c.commit()
        return cur.lastrowid

    def sub_update(self, sid: int, **fields) -> None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.c.execute(f"UPDATE subs SET {cols} WHERE id=?", (*fields.values(), sid))
            self.c.commit()

    def sub_join(self, sid: int, uid: int, chat_id: int) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO sub_users(sub_id,user_id,chat_id) VALUES (?,?,?)",
                             (sid, uid, chat_id))
        self.c.commit()
        return cur.rowcount > 0

    def sub_leave(self, sid: int, uid: int) -> None:
        """Отписаться; последний ушёл — подписка удаляется."""
        self.c.execute("DELETE FROM sub_users WHERE sub_id=? AND user_id=?", (sid, uid))
        if not self.sub_users(sid):
            self.sub_delete(sid, commit=False)
        self.c.commit()

    def sub_delete(self, sid: int, commit: bool = True) -> None:
        self.c.execute("DELETE FROM sub_users WHERE sub_id=?", (sid,))
        self.c.execute("DELETE FROM sub_files WHERE sub_id=?", (sid,))
        self.c.execute("DELETE FROM subs WHERE id=?", (sid,))
        if commit:
            self.c.commit()

    def sub_users(self, sid: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM sub_users WHERE sub_id=?", (sid,)).fetchall()

    def subs(self, uid: int | None = None) -> list[sqlite3.Row]:
        if uid is None:
            return self.c.execute("SELECT * FROM subs ORDER BY title").fetchall()
        return self.c.execute("SELECT s.* FROM subs s JOIN sub_users u ON u.sub_id=s.id"
                              " WHERE u.user_id=? ORDER BY s.title", (uid,)).fetchall()

    def is_subscribed(self, sid: int, uid: int) -> bool:
        return self.c.execute("SELECT 1 FROM sub_users WHERE sub_id=? AND user_id=?",
                              (sid, uid)).fetchone() is not None

    def sub_files_add(self, sid: int, names: list[str]) -> None:
        self.c.executemany("INSERT OR IGNORE INTO sub_files(sub_id,name) VALUES (?,?)", [(sid, n) for n in names])
        self.c.commit()

    def sub_files(self, sid: int) -> set[str]:
        return {r[0] for r in self.c.execute("SELECT name FROM sub_files WHERE sub_id=?", (sid,))}

    # --- «⏳ ждать хорошее качество» (v7) ---
    def wait_add(self, kind: str, tmdb_id: int, title: str, year: str, poster: str | None,
                 uid: int, chat_id: int) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO waits VALUES (?,?,?,?,?,?,?,?,NULL)",
                             (kind, tmdb_id, title, year, poster, uid, chat_id, int(time.time())))
        self.c.commit()
        return cur.rowcount > 0

    def wait_remove(self, kind: str, tmdb_id: int, uid: int | None = None) -> None:
        if uid is None:
            self.c.execute("DELETE FROM waits WHERE kind=? AND tmdb_id=?", (kind, tmdb_id))
        else:
            self.c.execute("DELETE FROM waits WHERE kind=? AND tmdb_id=? AND user_id=?", (kind, tmdb_id, uid))
        self.c.commit()

    def waits(self, uid: int | None = None) -> list[sqlite3.Row]:
        if uid is None:
            return self.c.execute("SELECT * FROM waits ORDER BY created_at").fetchall()
        return self.c.execute("SELECT * FROM waits WHERE user_id=? ORDER BY created_at", (uid,)).fetchall()

    def wait_checked(self, kind: str, tmdb_id: int) -> None:
        self.c.execute("UPDATE waits SET checked_at=? WHERE kind=? AND tmdb_id=?", (int(time.time()), kind, tmdb_id))
        self.c.commit()

    def add_deletion(self, uid: int, kind: str, label: str, size: int) -> None:
        self.c.execute("INSERT INTO deletions(at, user_id, kind, label, size) VALUES (?,?,?,?,?)",
                       (int(time.time()), uid, kind, label, size))
        self.c.commit()

    def deletions_since(self, ts: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM deletions WHERE at>=? ORDER BY at", (ts,)).fetchall()

    # --- журнал «что смотрели» и оценки ---
    def _journal_backfill(self) -> None:
        """Первый запуск v6.3: всё, что уже докачано ботом раньше, — в журнал."""
        rows = self.c.execute("SELECT * FROM downloads WHERE done_at IS NOT NULL ORDER BY done_at").fetchall()
        for r in rows:
            label = r["label"] or ru_part(r["title"] or "") or r["name"] or ""
            jid = self.journal_note(r["category"] or "movies", label, r["poster"], r["user_id"],
                                    r["done_at"], r["hash"], commit=False)
            if jid and r["removed"]:
                self.c.execute("UPDATE journal SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                               (r["removed_at"] or r["done_at"], jid))
        self.c.execute("UPDATE journal SET deleted_at=NULL WHERE EXISTS"
                       " (SELECT 1 FROM downloads WHERE jid=journal.id AND removed=0)")

    def journal_note(self, kind: str, label: str, poster: str | None = None, uid: int | None = None,
                     ts: int | None = None, h: str | None = None, commit: bool = True,
                     tmdb_kind: str | None = None, tmdb_id: int | None = None) -> int | None:
        """Записать скачанное в журнал (одно название — одна запись). Вернёт id записи."""
        key = journal_key(label)
        if not key:
            return None
        ts = ts or int(time.time())
        if tmdb_id:                            # v8: этот фильм уже оценивали из списков — та же запись
            row = self.c.execute("SELECT id, src FROM journal WHERE tmdb_kind=? AND tmdb_id=?",
                                 (tmdb_kind, tmdb_id)).fetchone()
            if row and row["src"] == "list":
                self.c.execute("UPDATE journal SET src=NULL, added_by=?, added_at=? WHERE id=?", (uid, ts, row["id"]))
            if row:
                key = self.c.execute("SELECT key FROM journal WHERE id=?", (row["id"],)).fetchone()[0]
        self.c.execute("INSERT OR IGNORE INTO journal(key, kind, label, poster, added_by, added_at)"
                       " VALUES (?,?,?,?,?,?)", (key, kind, label.strip()[:200], poster, uid, ts))
        row = self.c.execute("SELECT id, poster, tmdb_id FROM journal WHERE key=?", (key,)).fetchone()
        jid = row["id"]
        if tmdb_id and row["tmdb_id"] is None:
            self.c.execute("UPDATE journal SET tmdb_kind=?, tmdb_id=? WHERE id=?", (tmdb_kind, tmdb_id, jid))
        if h:                                  # скачали заново — снова «на диске»
            self.c.execute("UPDATE journal SET deleted_at=NULL, deleted_by=NULL WHERE id=?", (jid,))
            self.c.execute("UPDATE downloads SET jid=? WHERE hash=?", (jid, h))
        if poster and not row["poster"]:
            self.c.execute("UPDATE journal SET poster=? WHERE id=?", (poster, jid))
        if commit:
            self.c.commit()
        return jid

    def journal_downloads(self, jid: int) -> list[sqlite3.Row]:
        """Раздачи этого названия, которые ещё на диске."""
        return self.c.execute("SELECT * FROM downloads WHERE jid=? AND removed=0", (jid,)).fetchall()

    def journal_find(self, label: str) -> int | None:
        """id записи журнала по названию (без создания)."""
        row = self.c.execute("SELECT id FROM journal WHERE key=?", (journal_key(label),)).fetchone()
        return row["id"] if row else None

    def journal_get(self, jid: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM journal WHERE id=?", (jid,)).fetchone()

    def journal_deleted(self, jid: int, uid: int | None = None) -> None:
        self.c.execute("UPDATE journal SET deleted_at=?, deleted_by=? WHERE id=? AND deleted_at IS NULL",
                       (int(time.time()), uid, jid))
        self.c.commit()

    def journal_remove(self, jid: int) -> None:
        self.c.execute("UPDATE downloads SET jid=NULL WHERE jid=?", (jid,))
        self.c.execute("DELETE FROM ratings WHERE jid=?", (jid,))
        self.c.execute("DELETE FROM rating_asks WHERE jid=?", (jid,))
        self.c.execute("DELETE FROM journal WHERE id=?", (jid,))
        self.c.commit()

    def journal(self, mode: str = "d", uid: int = 0, home: bool = True) -> list[sqlite3.Row]:
        """d — по дате (новые сверху), r — по средней, m — по моей оценке, u — «мне оценить» (uid ещё не ответил).
        В каждой строке: avg (средняя), cnt (сколько оценок), answered/my — ответ uid.
        v8: home — видно скачанное на домашний сервер; «из списков» — только оценённое uid или его кругом."""
        when = "COALESCE(j.deleted_at, j.added_at)"
        circle = sorted(self.circle(uid)) if uid else []
        marks = ",".join(str(int(x)) for x in circle) or "0"
        vis = (f"(j.src='list' AND EXISTS(SELECT 1 FROM ratings r2 WHERE r2.jid=j.id AND r2.user_id IN ({marks})))")
        where = f" WHERE ((j.src IS NULL OR j.src<>'list') OR {vis})" if home else f" WHERE {vis}"
        q = ("SELECT j.*, AVG(r.score) AS avg, COUNT(r.score) AS cnt,"
             " (SELECT score FROM ratings WHERE jid=j.id AND user_id=:u) AS my,"
             " EXISTS(SELECT 1 FROM ratings WHERE jid=j.id AND user_id=:u) AS answered"
             f" FROM journal j LEFT JOIN ratings r ON r.jid=j.id{where} GROUP BY j.id")
        if mode == "r":                                 # все: с оценками — лучшие сверху, без оценок — внизу
            q += f" ORDER BY cnt=0, avg DESC, cnt DESC, {when} DESC"
        elif mode == "m":                               # по моей: оценённые мной, потом «не смотрел», потом остальные
            q += f" ORDER BY answered=0, my IS NULL, my DESC, avg DESC, {when} DESC"
        elif mode == "u":
            q += f" HAVING answered=0 ORDER BY {when} DESC"
        else:
            q += f" ORDER BY {when} DESC"
        return self.c.execute(q, {"u": uid}).fetchall()

    # --- личные оценки (v6.5) ---
    def rate(self, jid: int, uid: int, score: int | None) -> None:
        """score 1..10 или None — «не смотрел(а)»."""
        self.c.execute("INSERT OR REPLACE INTO ratings(jid, user_id, score, at) VALUES (?,?,?,?)",
                       (jid, uid, score, int(time.time())))
        self.c.commit()

    def unrate(self, jid: int, uid: int) -> bool:
        """Сбросить СВОЙ ответ (чужие так не удалить)."""
        cur = self.c.execute("DELETE FROM ratings WHERE jid=? AND user_id=?", (jid, uid))
        self.c.commit()
        return cur.rowcount > 0

    def rating_of(self, jid: int, uid: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM ratings WHERE jid=? AND user_id=?", (jid, uid)).fetchone()

    def ratings(self, jid: int) -> list[sqlite3.Row]:
        """Все ответы по названию: сначала оценки (высокие сверху), потом «не смотрел(а)»."""
        return self.c.execute("SELECT * FROM ratings WHERE jid=? ORDER BY score IS NULL, score DESC, at",
                              (jid,)).fetchall()

    def rating_summary(self, jid: int) -> tuple[float | None, int]:
        row = self.c.execute("SELECT AVG(score), COUNT(score) FROM ratings WHERE jid=?", (jid,)).fetchone()
        return row[0], row[1]

    def was_asked(self, jid: int, uid: int) -> bool:
        return self.c.execute("SELECT 1 FROM rating_asks WHERE jid=? AND user_id=?",
                              (jid, uid)).fetchone() is not None

    def note_ask(self, jid: int, uid: int, reason: str) -> None:
        self.c.execute("INSERT OR IGNORE INTO rating_asks(jid, user_id, reason, at) VALUES (?,?,?,?)",
                       (jid, uid, reason, int(time.time())))
        self.c.commit()

    def get(self, h: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM downloads WHERE hash=?", (h,)).fetchone()


_RE_KEY = re.compile(r"[\W_]+", re.U)


def journal_key(label: str) -> str:
    """«Маска (1994)» и «маска 1994» — одна запись журнала."""
    return " ".join(_RE_KEY.sub(" ", (label or "").lower().replace("ё", "е")).split())


def ru_part(title: str) -> str:
    """Русская часть заголовка раздачи: «Маска / The Mask (1994) BDRip» → «Маска»."""
    t = re.split(r"\s/\s|\[|\(", title or "", maxsplit=1)[0]
    return t.strip(" .-")
