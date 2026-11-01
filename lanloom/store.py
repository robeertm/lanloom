"""SQLite persistence (one file in the data directory, WAL mode).

Only inventory, settings and accounts live here. Live byte counters stay in memory.
Secrets are stored encrypted (see crypto.py); this module never sees plaintext secrets.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    user_handle BLOB NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS passkeys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id BLOB NOT NULL UNIQUE,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    created REAL NOT NULL,
    last_used REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS integrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    config TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    ip TEXT NOT NULL,
    mac TEXT,
    data TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence REAL NOT NULL,
    title TEXT NOT NULL,
    user_title TEXT,
    user_kind TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    last_scan INTEGER
);
CREATE INDEX IF NOT EXISTS devices_ip ON devices(ip);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    finished REAL,
    mode TEXT NOT NULL,
    cidrs TEXT NOT NULL,
    status TEXT NOT NULL,
    hosts INTEGER NOT NULL DEFAULT 0,
    stats TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._migrate()

    # ------------------------------------------------------------------ basics
    def _migrate(self) -> None:
        row = self._db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        current = int(row["value"]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError("database was written by a newer version (schema %d)" % current)
        if current < SCHEMA_VERSION:
            self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),))

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def query(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def transaction(self):
        return _Tx(self)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, json.dumps(value)))

    # ------------------------------------------------------------------ users
    def user_count(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM users")["n"]

    # ------------------------------------------------------------------ devices
    def devices(self, include_hidden: bool = True) -> list:
        rows = self.query("SELECT * FROM devices" + ("" if include_hidden else " WHERE hidden=0") + " ORDER BY ip")
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def device(self, device_id: str) -> Optional[dict]:
        row = self.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if row:
            row["data"] = json.loads(row["data"])
        return row

    def upsert_device(self, dev: dict, scan_id: Optional[int]) -> None:
        now = time.time()
        existing = self.one("SELECT id, first_seen FROM devices WHERE id=?", (dev["id"],))
        payload = json.dumps(dev["data"], separators=(",", ":"))
        if existing:
            self.execute(
                "UPDATE devices SET ip=?, mac=?, data=?, kind=?, confidence=?, title=?, last_seen=?, last_scan=? WHERE id=?",
                (dev["ip"], dev.get("mac"), payload, dev["kind"], dev["confidence"], dev["title"], now, scan_id, dev["id"]),
            )
        else:
            self.execute(
                "INSERT INTO devices(id, ip, mac, data, kind, confidence, title, first_seen, last_seen, last_scan)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (dev["id"], dev["ip"], dev.get("mac"), payload, dev["kind"], dev["confidence"], dev["title"], now, now, scan_id),
            )


class _Tx:
    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        self.store._lock.acquire()
        self.store._db.execute("BEGIN")
        return self.store

    def __exit__(self, exc_type, exc, tb):
        try:
            self.store._db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store._lock.release()
        return False
