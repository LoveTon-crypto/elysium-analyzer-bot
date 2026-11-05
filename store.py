"""SQLite: settings, topic bindings, deploys, calls, alert marks."""
from __future__ import annotations

import sqlite3
import time

import calls


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS topics (key TEXT PRIMARY KEY, chat_id INTEGER, thread_id INTEGER);
            CREATE TABLE IF NOT EXISTS marks (k TEXT PRIMARY KEY, ts REAL);
            CREATE TABLE IF NOT EXISTS calls (chat_id INTEGER, token TEXT, user TEXT, mcap REAL, ts REAL,
                                              PRIMARY KEY (chat_id, token));
            CREATE TABLE IF NOT EXISTS deploys (
                token TEXT PRIMARY KEY, dev TEXT, pad TEXT DEFAULT '', venue TEXT DEFAULT '', factory TEXT DEFAULT '',
                ts REAL, block INTEGER, tx TEXT, name TEXT DEFAULT '', symbol TEXT DEFAULT '',
                ath REAL DEFAULT 0, last_mc REAL DEFAULT 0, posted INTEGER DEFAULT 0,
                chat_id TEXT DEFAULT '', msg_id INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS deploys_dev ON deploys(dev);
            CREATE INDEX IF NOT EXISTS deploys_ts ON deploys(ts);
        """)
        self.db.commit()
        calls.migrate(self.db)

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        cur = self.db.execute(sql, args)
        rows = cur.fetchall()
        self.db.commit()
        return rows

    def kv_get(self, k: str) -> str | None:
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return r[0][0] if r else None

    def kv_set(self, k: str, v: str | None):
        if v is None:
            self.q("DELETE FROM kv WHERE k=?", (k,))
        else:
            self.q("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, str(v)))

    def topic(self, key: str) -> tuple[int, int | None] | None:
        r = self.q("SELECT chat_id, thread_id FROM topics WHERE key=?", (key,))
        return (r[0][0], r[0][1]) if r else None

    def set_topic(self, key: str, chat_id: int, thread_id: int | None):
        self.q("INSERT OR REPLACE INTO topics (key, chat_id, thread_id) VALUES (?,?,?)", (key, chat_id, thread_id))

    def seen(self, k: str) -> bool:
        return bool(self.q("SELECT 1 FROM marks WHERE k=?", (k,)))

    def mark(self, k: str):
        self.q("INSERT OR REPLACE INTO marks (k, ts) VALUES (?, ?)", (k, time.time()))

    def deploy(self, token: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM deploys WHERE token=?", (token.lower(),))
        r = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], r)) if r else None
