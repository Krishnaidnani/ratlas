"""Resolver extension points and normalized SCIP symbol mapping adapter."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Protocol


class ResolverProvider(Protocol):
    def resolve(self, connection: sqlite3.Connection, file: str, source: str,
                reference: str) -> tuple[int, float] | None: ...


class ScipJsonResolver:
    """Resolve using symbol locations exported from SCIP into RepoAtlas mapping JSON.

    The mapping format is `{"references": [{"file", "source", "text",
    "target_file", "target_qualname", "confidence"}]}`. A language-specific SCIP
    adapter can produce this compact map without making the core depend on protobuf.
    """

    def __init__(self, mapping_path: Path) -> None:
        raw = json.loads(mapping_path.read_text(encoding="utf-8"))
        entries = raw.get("references") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise ValueError("SCIP mapping must have a 'references' array")
        self.entries: dict[tuple[str, str, str], tuple[str, str, float]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("each SCIP mapping reference must be an object")
            required = ("file", "source", "text", "target_file", "target_qualname")
            if not all(isinstance(entry.get(key), str) and entry[key] for key in required):
                raise ValueError(f"SCIP mapping entries require string fields: {', '.join(required)}")
            confidence = float(entry.get("confidence", 1.0))
            if not 0 <= confidence <= 1:
                raise ValueError("SCIP mapping confidence must be between 0 and 1")
            key = (entry["file"].replace("\\", "/"), entry["source"], entry["text"])
            if key in self.entries:
                raise ValueError(f"duplicate SCIP mapping for {key!r}")
            self.entries[key] = (entry["target_file"].replace("\\", "/"), entry["target_qualname"], confidence)

    def resolve(self, connection: sqlite3.Connection, file: str, source: str,
                reference: str) -> tuple[int, float] | None:
        target = self.entries.get((file, source, reference))
        if target is None:
            return None
        target_file, target_qualname, confidence = target
        row = connection.execute(
            "SELECT s.id FROM symbols s JOIN files f ON f.id=s.file_id WHERE f.path=? AND s.qualname=?",
            (target_file, target_qualname),
        ).fetchone()
        return (int(row["id"]), confidence) if row is not None else None


def load_scip_resolver(mapping_path: Path | None) -> ScipJsonResolver | None:
    return ScipJsonResolver(mapping_path) if mapping_path is not None else None
