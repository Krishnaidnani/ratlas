"""SQLite persistence with WAL readers and atomic write transactions."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Iterator

from repo_atlas.models import FileRecord, Symbol

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, lang TEXT NOT NULL,
  content_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  parse_status TEXT NOT NULL DEFAULT 'ok', last_gen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  qualname TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL,
  doc TEXT NOT NULL DEFAULT '', start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
  parent_name TEXT, is_public INTEGER NOT NULL DEFAULT 1, sig_hash TEXT NOT NULL,
  body_hash TEXT NOT NULL, UNIQUE(file_id, qualname)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE TABLE IF NOT EXISTS raw_refs (
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  src_qualname TEXT NOT NULL, dst_text TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
  src_symbol INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  dst_symbol INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
  kind TEXT NOT NULL, dst_text TEXT NOT NULL, dst_tail TEXT NOT NULL,
  confidence REAL NOT NULL, line INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(src_symbol, kind, dst_text, line)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_symbol);
CREATE INDEX IF NOT EXISTS idx_edges_tail ON edges(dst_tail);
CREATE TABLE IF NOT EXISTS derived (
    owner_kind TEXT NOT NULL, owner_key TEXT NOT NULL, artifact TEXT NOT NULL,
    input_hash TEXT NOT NULL, payload BLOB NOT NULL, model TEXT,
    PRIMARY KEY(owner_kind, owner_key, artifact)
);
CREATE TABLE IF NOT EXISTS rank_scores (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    score REAL NOT NULL, generation INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5(name, qualname, doc, content='symbols', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbol_fts(rowid,name,qualname,doc) VALUES(new.id,new.name,new.qualname,new.doc);
END;
CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbol_fts(symbol_fts,rowid,name,qualname,doc) VALUES('delete',old.id,old.name,old.qualname,old.doc);
END;
CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbol_fts(symbol_fts,rowid,name,qualname,doc) VALUES('delete',old.id,old.name,old.qualname,old.doc);
    INSERT INTO symbol_fts(rowid,name,qualname,doc) VALUES(new.id,new.name,new.qualname,new.doc);
END;
"""

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"index schema version {current_version} is newer than supported {SCHEMA_VERSION}; "
                    "use a compatible RepoAtlas release or rebuild the index"
                )
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('generation', '0')")
            connection.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def writer_lock(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_key = str(lock_path.resolve())
        with _LOCKS_GUARD:
            process_lock = _LOCKS.setdefault(lock_key, threading.Lock())
        with process_lock:
            with lock_path.open("a+b") as handle:
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def generation(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
            return int(row[0]) if row else 0

    def files(self) -> list[FileRecord]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM files ORDER BY path").fetchall()
        return [FileRecord(row["id"], row["path"], row["content_hash"], row["size_bytes"],
                           row["mtime_ns"], row["parse_status"], row["last_gen"]) for row in rows]

    def symbols_for_file(self, path: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT s.* FROM symbols s JOIN files f ON f.id=s.file_id WHERE f.path=? ORDER BY s.start_line, s.qualname",
                (path,),
            ).fetchall()

    def status(self) -> dict[str, int]:
        with self.connect() as connection:
            return {
                "generation": self.generation(),
                "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                "files": connection.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                "symbols": connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
                "edges": connection.execute("SELECT COUNT(*) FROM edges WHERE dst_symbol IS NOT NULL").fetchone()[0],
                "unresolved_edges": connection.execute("SELECT COUNT(*) FROM edges WHERE dst_symbol IS NULL").fetchone()[0],
                "parse_failures": connection.execute("SELECT COUNT(*) FROM files WHERE parse_status != 'ok'").fetchone()[0],
            }

    def close(self) -> None:
        """Connections are scoped to operations; retained for a uniform store lifecycle."""