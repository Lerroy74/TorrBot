"""SQLite: кому разрешён доступ и что кем поставлено на закачку.
Таблица downloads хранит время добавления и завершения — пригодится для будущей
автоочистки старых фильмов."""
from __future__ import annotations

import os
import sqlite3
import time

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
CREATE TABLE IF NOT EXISTS deletions (
    at        INTEGER,
    user_id   INTEGER,
    kind      TEXT,
    label     TEXT,
    size      INTEGER
);
"""


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
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
        self.c.commit()

    # --- users ---
    def allow(self, uid: int, name: str = "") -> None:
        self.c.execute("INSERT OR REPLACE INTO users(id, name, added_at) VALUES (?,?,?)",
                       (uid, name, int(time.time())))
        self.c.execute("DELETE FROM requests WHERE id=?", (uid,))
        self.c.execute("DELETE FROM blocked WHERE id=?", (uid,))
        self.c.commit()

    def revoke(self, uid: int) -> bool:
        self.c.execute("DELETE FROM prefs WHERE user_id=? AND key='can_delete'", (uid,))
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
        self.c.execute("DELETE FROM prefs WHERE user_id=? AND key='can_delete'", (uid,))
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
                     poster: str | None = None, label: str | None = None) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO downloads(hash,name,title,category,chat_id,user_id,added_at,poster,label)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (h, name, title, category, chat_id, user_id, int(time.time()), poster, label))
        self.c.commit()

    def pending(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM downloads WHERE done_at IS NULL AND removed=0").fetchall()

    def mark_done(self, h: str) -> None:
        self.c.execute("UPDATE downloads SET done_at=? WHERE hash=?", (int(time.time()), h))
        self.c.commit()

    def mark_removed(self, h: str, reason: str = "manual") -> None:
        self.c.execute("UPDATE downloads SET removed=1, removed_at=?, removed_reason=? WHERE hash=? AND removed=0",
                       (int(time.time()), reason, h))
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

    def add_deletion(self, uid: int, kind: str, label: str, size: int) -> None:
        self.c.execute("INSERT INTO deletions(at, user_id, kind, label, size) VALUES (?,?,?,?,?)",
                       (int(time.time()), uid, kind, label, size))
        self.c.commit()

    def deletions_since(self, ts: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM deletions WHERE at>=? ORDER BY at", (ts,)).fetchall()

    def get(self, h: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM downloads WHERE hash=?", (h,)).fetchone()
