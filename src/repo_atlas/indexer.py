"""Repository enumeration and atomic incremental indexing."""

import hashlib
import logging
import os
from pathlib import Path
import sqlite3

import fnmatch

from repo_atlas.config import Config
from repo_atlas.diff import diff_symbols
from repo_atlas.models import ParseResult, Reference, Symbol
from repo_atlas.parser import parse_source
from repo_atlas.resolver import load_scip_resolver
from repo_atlas.store import SQLiteStore

logger = logging.getLogger(__name__)


class Indexer:
    _EXTENSIONS = {
        ".py": "python", ".pyi": "python",
        ".ts": "typescript", ".tsx": "typescript", ".js": "typescript", ".jsx": "typescript",
        ".java": "java",
    }

    def __init__(self, config: Config, store: SQLiteStore) -> None:
        self.config = config
        self.store = store
        self.scip_resolver = load_scip_resolver(config.resolve.scip_mapping)
        gitignore = config.root / ".gitignore"
        self.ignore_patterns = [
            line.strip().lstrip("/") for line in (
                gitignore.read_text(encoding="utf-8", errors="replace").splitlines() if gitignore.exists() else []
            ) if line.strip() and not line.lstrip().startswith("#")
        ] + list(config.index.exclude) + list(config.privacy.never_index)

    def _ignored(self, relative: str) -> bool:
        path = Path(relative)
        candidates = {relative, path.name}
        candidates.update(parent.as_posix() for parent in path.parents if parent.as_posix() != ".")
        for pattern in self.ignore_patterns:
            normalized = pattern.strip("/")
            if normalized.startswith("**/"):
                normalized = normalized[3:]
            if normalized.endswith("/**"):
                normalized = normalized[:-3].rstrip("/")
            if any(fnmatch.fnmatchcase(candidate, normalized) for candidate in candidates):
                return True
        return False

    def _files(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for directory, names, filenames in os.walk(self.config.root, followlinks=False):
            directory_path = Path(directory)
            names[:] = [name for name in names if not self._ignored((directory_path / name).relative_to(self.config.root).as_posix())]
            for filename in filenames:
                path = directory_path / filename
                relative = path.relative_to(self.config.root).as_posix()
                language = self._EXTENSIONS.get(path.suffix.lower())
                if language in self.config.index.languages and not self._ignored(relative) and any(
                    Path(relative).match(pattern) or (pattern.startswith("**/") and Path(relative).match(pattern[3:]))
                    for pattern in self.config.index.include
                ):
                    try:
                        if path.stat().st_size <= self.config.index.max_file_bytes and not path.is_symlink():
                            result[relative] = path
                    except OSError:
                        continue
        return result

    def index(self, force: bool = False) -> dict[str, int]:
        with self.store.writer_lock():
            return self._index_locked(force)

    def _index_locked(self, force: bool = False) -> dict[str, int]:
        paths = self._files()
        existing = {record.path: record for record in self.store.files()}
        pending: dict[str, tuple[bytes, str, os.stat_result, ParseResult]] = {}
        deleted = set(existing) - set(paths)
        for relative, path in paths.items():
            try:
                stat = path.stat()
                old = existing.get(relative)
                if (
                    not force and old is not None
                    and old.size_bytes == stat.st_size
                    and old.mtime_ns == stat.st_mtime_ns
                ):
                    continue
                source = path.read_bytes()
            except OSError:
                continue
            digest = hashlib.blake2b(source, digest_size=32).hexdigest()
            if force or old is None or old.content_hash != digest:
                pending[relative] = (source, digest, stat, parse_source(source, self._EXTENSIONS[path.suffix.lower()]))

        changed = pending
        if not changed and not deleted:
            return self.store.status()

        with self.store.write() as connection:
            generation = int(connection.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]) + 1
            touched_names = {
                symbol.name
                for relative in set(changed) | deleted
                for symbol in self._old_symbols(connection, relative)
            }
            touched_names.update(
                symbol.name for _, _, _, result in changed.values() for symbol in result.symbols
            )
            for relative in deleted:
                connection.execute("DELETE FROM files WHERE path=?", (relative,))
            for relative, (source, digest, stat, result) in changed.items():
                if result.status == "partial":
                    result = self._retain_damaged_symbols(connection, relative, result)
                cursor = connection.execute(
                    "INSERT INTO files(path,lang,content_hash,size_bytes,mtime_ns,parse_status,last_gen) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash,size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,parse_status=excluded.parse_status,last_gen=excluded.last_gen RETURNING id",
                    (relative, self._EXTENSIONS[Path(relative).suffix.lower()], digest, len(source), stat.st_mtime_ns, result.status, generation),
                )
                file_id = int(cursor.fetchone()[0])
                self._upsert_symbols(connection, file_id, result.symbols)
                connection.execute("DELETE FROM raw_refs WHERE file_id=?", (file_id,))
                connection.executemany(
                    "INSERT INTO raw_refs(file_id,src_qualname,dst_text,kind,line) VALUES(?,?,?,?,?)",
                    [(file_id, ref.source_qualname, ref.text, ref.kind, ref.line) for ref in result.references],
                )
            denominator = max(1, len(existing))
            changed_ratio = 100 * (len(changed) + len(deleted)) / denominator
            if changed_ratio >= self.config.index.bulk_threshold_pct:
                files_to_resolve = {
                    row["path"] for row in connection.execute("SELECT path FROM files").fetchall()
                }
            else:
                files_to_resolve = set(changed)
            self._resolve_files_edges(connection, files_to_resolve)
            self._resolve_dangling(connection, touched_names)
            connection.execute("UPDATE meta SET value=? WHERE key='generation'", (str(generation),))
        return self.store.status()

    @staticmethod
    def _upsert_symbols(connection: sqlite3.Connection, file_id: int, symbols: tuple[Symbol, ...]) -> None:
        old_rows = connection.execute("SELECT * FROM symbols WHERE file_id=?", (file_id,)).fetchall()
        old_symbols = {
            row["qualname"]: Symbol(
                row["qualname"], row["name"], row["kind"], row["signature"], row["doc"],
                row["start_line"], row["end_line"], row["parent_name"], bool(row["is_public"]),
                row["sig_hash"], row["body_hash"],
            ) for row in old_rows
        }
        new_symbols = {symbol.qualname: symbol for symbol in symbols}
        delta = diff_symbols(old_symbols, new_symbols)
        rows_by_qualname = {row["qualname"]: row for row in old_rows}
        for old_name, new_name in delta.renames.items():
            row = rows_by_qualname[old_name]
            connection.execute(
                "UPDATE symbols SET qualname=? WHERE id=?", (new_name, row["id"])
            )
            logger.debug("symbol renamed file_id=%s old=%s new=%s", file_id, old_name, new_name)
        keep_names = set(new_symbols)
        for row in old_rows:
            if row["qualname"] not in keep_names and row["qualname"] not in delta.renames:
                connection.execute("DELETE FROM symbols WHERE id=?", (row["id"],))
        if delta.changes:
            logger.debug("symbol delta file_id=%s changes=%s", file_id, delta.changes)
        for symbol in symbols:
            connection.execute(
                "INSERT INTO symbols(file_id,qualname,name,kind,signature,doc,start_line,end_line,parent_name,is_public,sig_hash,body_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file_id,qualname) DO UPDATE SET "
                "name=excluded.name,kind=excluded.kind,signature=excluded.signature,doc=excluded.doc,start_line=excluded.start_line," 
                "end_line=excluded.end_line,parent_name=excluded.parent_name,is_public=excluded.is_public,sig_hash=excluded.sig_hash,body_hash=excluded.body_hash",
                (file_id, symbol.qualname, symbol.name, symbol.kind, symbol.signature, symbol.doc,
                 symbol.start_line, symbol.end_line, symbol.parent, int(symbol.is_public), symbol.sig_hash, symbol.body_hash),
            )

    def _resolve_files_edges(self, connection: sqlite3.Connection, paths: set[str]) -> None:
        if not paths:
            return
        symbols = connection.execute("SELECT id,file_id,qualname,name FROM symbols").fetchall()
        by_name: dict[str, list[sqlite3.Row]] = {}
        by_file_qual: dict[tuple[int, str], sqlite3.Row] = {}
        for symbol in symbols:
            by_name.setdefault(symbol["name"], []).append(symbol)
            by_file_qual[(symbol["file_id"], symbol["qualname"])] = symbol
        placeholders = ",".join("?" for _ in paths)
        file_ids = [row["id"] for row in connection.execute(
            f"SELECT id FROM files WHERE path IN ({placeholders})", tuple(sorted(paths))
        ).fetchall()]
        if not file_ids:
            return
        file_placeholders = ",".join("?" for _ in file_ids)
        connection.execute(
            f"DELETE FROM edges WHERE src_symbol IN (SELECT id FROM symbols WHERE file_id IN ({file_placeholders}))",
            tuple(file_ids),
        )
        refs = connection.execute(
            f"SELECT r.*,s.id AS src_id,f.path AS file_path FROM raw_refs r JOIN symbols s ON s.file_id=r.file_id AND s.qualname=r.src_qualname JOIN files f ON f.id=r.file_id WHERE r.file_id IN ({file_placeholders})",
            tuple(file_ids),
        ).fetchall()
        for ref in refs:
            text = ref["dst_text"].strip()
            tail = text.rsplit(".", 1)[-1].rstrip("()")
            destination = None
            confidence = 0.0
            precise = self.scip_resolver.resolve(
                connection, ref["file_path"], ref["src_qualname"], text
            ) if self.scip_resolver is not None else None
            if precise is not None:
                destination, confidence = precise
            same_file = by_file_qual.get((ref["file_id"], text)) or by_file_qual.get((ref["file_id"], f"{ref['src_qualname'].rsplit('.', 1)[0]}.{tail}"))
            if destination is None and same_file is not None:
                destination, confidence = same_file["id"], 0.8
            elif destination is None and len(by_name.get(tail, [])) == 1:
                destination, confidence = by_name[tail][0]["id"], 0.5
            connection.execute(
                "INSERT OR IGNORE INTO edges(src_symbol,dst_symbol,kind,dst_text,dst_tail,confidence,line) VALUES(?,?,?,?,?,?,?)",
                (ref["src_id"], destination, ref["kind"], text, tail, confidence, ref["line"]),
            )

    @staticmethod
    def _old_symbols(connection: sqlite3.Connection, relative: str) -> list[Symbol]:
        rows = connection.execute(
            "SELECT qualname,name,kind,signature,doc,start_line,end_line,parent_name,is_public,sig_hash,body_hash "
            "FROM symbols WHERE file_id=(SELECT id FROM files WHERE path=?)", (relative,),
        ).fetchall()
        return [Symbol(row["qualname"], row["name"], row["kind"], row["signature"], row["doc"],
                       row["start_line"], row["end_line"], row["parent_name"], bool(row["is_public"]),
                       row["sig_hash"], row["body_hash"]) for row in rows]

    @staticmethod
    def _retain_damaged_symbols(connection: sqlite3.Connection, relative: str,
                                parsed: ParseResult) -> ParseResult:
        file_row = connection.execute("SELECT id FROM files WHERE path=?", (relative,)).fetchone()
        if file_row is None or not parsed.error_ranges:
            return parsed
        file_id = int(file_row["id"])
        parsed_names = {symbol.qualname for symbol in parsed.symbols}
        retained: list[Symbol] = []
        for symbol in Indexer._old_symbols(connection, relative):
            overlaps_error = any(
                symbol.start_line <= end_line and symbol.end_line >= start_line
                for start_line, end_line in parsed.error_ranges
            )
            if symbol.qualname != "<module>" and overlaps_error and symbol.qualname not in parsed_names:
                retained.append(symbol)
        if not retained:
            return parsed
        names = {symbol.qualname for symbol in retained}
        ref_rows = connection.execute(
            "SELECT src_qualname,dst_text,kind,line FROM raw_refs WHERE file_id=?", (file_id,)
        ).fetchall()
        retained_refs = tuple(
            Reference(row["src_qualname"], row["dst_text"], row["kind"], row["line"])
            for row in ref_rows if row["src_qualname"] in names
        )
        return ParseResult(
            tuple([*parsed.symbols, *retained]),
            tuple([*parsed.references, *retained_refs]),
            parsed.status,
            parsed.error_ranges,
        )

    @staticmethod
    def _resolve_dangling(connection: sqlite3.Connection, touched_names: set[str]) -> None:
        if not touched_names:
            return
        placeholders = ",".join("?" for _ in touched_names)
        unresolved = connection.execute(
            f"SELECT rowid,dst_tail FROM edges WHERE dst_tail IN ({placeholders}) AND confidence<1.0",
            tuple(sorted(touched_names)),
        ).fetchall()
        for edge in unresolved:
            connection.execute("UPDATE edges SET dst_symbol=NULL,confidence=0 WHERE rowid=?", (edge["rowid"],))
            candidates = connection.execute("SELECT id FROM symbols WHERE name=? ORDER BY id", (edge["dst_tail"],)).fetchall()
            if len(candidates) == 1:
                connection.execute("UPDATE edges SET dst_symbol=?,confidence=0.5 WHERE rowid=?", (candidates[0]["id"], edge["rowid"]))
