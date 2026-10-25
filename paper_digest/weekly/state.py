"""SQLite transactions, separate fetched/summarized/sent and durable outbox."""

import json
import sqlite3


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, payload TEXT NOT NULL, first_seen TEXT NOT NULL, summarized_at TEXT, sent_at TEXT);
        CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, error TEXT);
        """)
        # Interrupted DATA means delivery is unknown, never automatically resend.
        self.db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")
        self.db.commit()

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def meta(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value))
        )
        self.db.commit()

    def fetched(self, p, now):
        existing = []
        for alias in p["ids"]:
            row = self.db.execute(
                "SELECT id FROM aliases WHERE alias=?", (alias,)
            ).fetchone()
            if row:
                existing.append(row[0])
        key = existing[0] if existing else p["id"]
        p["id"] = key
        rows = [
            self.db.execute(
                "SELECT first_seen,summarized_at,sent_at FROM papers WHERE id=?", (i,)
            ).fetchone()
            for i in set(existing)
        ]
        rows = [r for r in rows if r]
        first = min((r[0] for r in rows), default=now)
        summarized = next((r[1] for r in rows if r[1]), None)
        sent = next((r[2] for r in rows if r[2]), None)
        p["first_seen_at"] = first
        self.db.execute(
            "INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?)",
            (key, json.dumps(p, ensure_ascii=False), first, summarized, sent),
        )
        for old in set(existing) - {key}:
            self.db.execute("UPDATE aliases SET id=? WHERE id=?", (key, old))
            self.db.execute("DELETE FROM papers WHERE id=?", (old,))
        for alias in p["ids"]:
            self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (alias, key))
        self.db.commit()

    def unsent(self):
        return [
            json.loads(r[0])
            for r in self.db.execute("SELECT payload FROM papers WHERE sent_at IS NULL")
        ]

    def cached(self, key):
        row = self.db.execute(
            "SELECT payload FROM cache WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key, payload, paper_id=None, now=None):
        self.db.execute(
            "INSERT OR REPLACE INTO cache VALUES (?,?)",
            (key, json.dumps(payload, ensure_ascii=False)),
        )
        if paper_id:
            self.db.execute(
                "UPDATE papers SET summarized_at=? WHERE id=?", (now, paper_id)
            )
        self.db.commit()

    def close(self):
        self.db.close()
