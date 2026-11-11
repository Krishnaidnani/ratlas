"""Replay Git history and compare incremental indexes with fresh rebuilds."""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any

from repo_atlas.config import Config
from repo_atlas.indexer import Indexer
from repo_atlas.store import SQLiteStore


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *arguments], stderr=subprocess.PIPE)


def _extract_commit(root: Path, commit: str, archive_root: Path) -> None:
    if archive_root.exists():
        shutil.rmtree(archive_root)
    archive_root.mkdir(parents=True)
    archive = _git(root, "archive", "--format=tar", commit)
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as stream:
        for member in stream.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe path in git archive: {member.name}")
            target = archive_root.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = stream.extractfile(member)
                if extracted is not None:
                    target.write_bytes(extracted.read())


def canonical_graph(store: SQLiteStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as connection:
        symbols = [tuple(row) for row in connection.execute(
            "SELECT f.path,s.qualname,s.sig_hash,s.body_hash FROM symbols s "
            "JOIN files f ON f.id=s.file_id ORDER BY f.path,s.qualname"
        )]
        edges = [tuple(row) for row in connection.execute(
            "SELECT sf.path,ss.qualname,e.kind,df.path,ds.qualname,e.dst_text,e.confidence "
            "FROM edges e JOIN symbols ss ON ss.id=e.src_symbol JOIN files sf ON sf.id=ss.file_id "
            "LEFT JOIN symbols ds ON ds.id=e.dst_symbol LEFT JOIN files df ON df.id=ds.file_id "
            "ORDER BY sf.path,ss.qualname,e.kind,e.dst_text,df.path,ds.qualname"
        )]
    return {"symbols": symbols, "edges": edges}


def replay_history(root: Path, commits: int = 50) -> dict[str, Any]:
    root = root.resolve()
    if commits <= 0:
        raise ValueError("commits must be positive")
    revisions = _git(root, "rev-list", "--first-parent", f"--max-count={commits}", "HEAD").decode().splitlines()
    revisions.reverse()
    if not revisions:
        raise ValueError("repository has no commits to replay")
    with tempfile.TemporaryDirectory(prefix="ratlas-replay-") as temporary:
        temp_root = Path(temporary)
        worktree = temp_root / "source"
        incremental_store = SQLiteStore(temp_root / "incremental.db")
        incremental_indexer: Indexer | None = None
        checked = 0
        for revision in revisions:
            _extract_commit(root, revision, worktree)
            config = Config(root=worktree, database=temp_root / "incremental.db")
            incremental_indexer = Indexer(config, incremental_store)
            incremental_indexer.index()
            fresh_store = SQLiteStore(temp_root / "fresh.db")
            fresh_config = Config(root=worktree, database=temp_root / "fresh.db")
            Indexer(fresh_config, fresh_store).index(force=True)
            incremental = canonical_graph(incremental_store)
            fresh = canonical_graph(fresh_store)
            if incremental != fresh:
                raise AssertionError(json.dumps({
                    "commit": revision,
                    "incremental_only_symbols": sorted(set(incremental["symbols"]) - set(fresh["symbols"])),
                    "fresh_only_symbols": sorted(set(fresh["symbols"]) - set(incremental["symbols"])),
                    "incremental_only_edges": sorted(set(incremental["edges"]) - set(fresh["edges"]), key=str),
                    "fresh_only_edges": sorted(set(fresh["edges"]) - set(incremental["edges"]), key=str),
                }, indent=2))
            fresh_store.close()
            Path(temp_root / "fresh.db").unlink(missing_ok=True)
            checked += 1
        incremental_store.close()
    return {"commits_checked": checked, "status": "passed", "head": revisions[-1]}
