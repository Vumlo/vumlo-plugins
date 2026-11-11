"""A plugin's own small memory: caches, cursors, the last time something ran. SQLite in WAL mode under
`$VUMLO_PLUGIN_DATA` (default `./data`). Never the source of truth for core data: the core can always be read again,
so a lost file costs a refetch, not a record."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import clock


class _Corrupt(Exception):
    pass


def data_dir() -> Path:
    return Path(os.getenv("VUMLO_PLUGIN_DATA", "data"))


class State:
    """`State()` opens `<data dir>/state.db`; `State(":memory:")` is for tests. Values are JSON."""

    def __init__(self, path: str | Path | None = None):
        self._lock = threading.Lock()
        if str(path) == ":memory:":
            self._db = self._open(":memory:")
            return
        folder = Path(path) if path else data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "state.db"
        try:
            self._db = self._open(str(target))
        except _Corrupt:
            # The file is only a cache of what the core can say again: set it aside (for a look) and start empty.
            stamp = clock.timestamp()
            for suffix in ("", "-wal", "-shm"):
                if (aside := Path(f"{target}{suffix}")).exists():
                    aside.rename(f"{target}.corrupt-{stamp}{suffix}")
            logging.getLogger("vumlo_plugins.state").warning("the state file was damaged: set aside, starting empty")
            self._db = self._open(str(target))

    @staticmethod
    def _open(target: str) -> sqlite3.Connection:
        db = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS kv (ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL, PRIMARY KEY (ns, key))")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise _Corrupt
        except sqlite3.DatabaseError as error:
            db.close()
            # 11 is SQLITE_CORRUPT, 26 SQLITE_NOTADB; a locked file (another worker starting) is neither.
            if (getattr(error, "sqlite_errorcode", 0) or 0) & 0xFF in (11, 26):
                raise _Corrupt from None
            raise
        except _Corrupt:
            db.close()
            raise
        return db

    def get(self, ns: str, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
        try:
            return default if row is None else json.loads(row[0])
        except ValueError:
            return default

    def set(self, ns: str, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (ns, key, value, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT (ns, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (ns, key, json.dumps(value), clock.timestamp()))

    def delete(self, ns: str, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))

    def items(self, ns: str) -> dict[str, Any]:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM kv WHERE ns=?", (ns,)).fetchall()
        found = {}
        for key, value in rows:
            try:
                found[key] = json.loads(value)
            except ValueError:
                pass
        return found

    def seen(self, ns: str, key: str) -> bool:
        """Records `key` and says whether it was new. Atomic: of two callers with the same key only one gets True."""
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO kv (ns, key, value, updated_at) VALUES (?,?,'null',?)",
                (ns, key, clock.timestamp()))
        return cursor.rowcount == 1

    def prune(self, ns: str, older_than_seconds: float) -> int:
        """Drops the entries of `ns` last written longer ago than that. Returns how many."""
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM kv WHERE ns=? AND updated_at < ?", (ns, clock.timestamp() - older_than_seconds))
        return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
