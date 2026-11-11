"""Immutable data contracts shared by parsers, the indexer, and query engine."""

from dataclasses import dataclass
from enum import StrEnum


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    SIGNATURE = "signature"
    BODY = "body"
    DOC = "doc"


@dataclass(frozen=True, slots=True)
class Symbol:
    qualname: str
    name: str
    kind: str
    signature: str
    doc: str
    start_line: int
    end_line: int
    parent: str | None
    is_public: bool
    sig_hash: str
    body_hash: str


@dataclass(frozen=True, slots=True)
class Reference:
    source_qualname: str
    text: str
    kind: str
    line: int


@dataclass(frozen=True, slots=True)
class ParseResult:
    symbols: tuple[Symbol, ...]
    references: tuple[Reference, ...]
    status: str
    error_ranges: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class FileRecord:
    id: int
    path: str
    content_hash: str
    size_bytes: int
    mtime_ns: int
    parse_status: str
    last_gen: int