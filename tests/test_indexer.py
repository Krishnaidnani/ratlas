from pathlib import Path
from threading import Event, Thread

import pytest

from repo_atlas.config import Config
from repo_atlas.engine import ContextEngine
from repo_atlas.indexer import Indexer
from repo_atlas.store import SQLiteStore


def test_incremental_index_preserves_ids_and_skips_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / ".ratlas" / "index.db")
    indexer = Indexer(Config(root=tmp_path), store)

    first = indexer.index()
    first_id = store.symbols_for_file("sample.py")[1]["id"]
    unchanged = indexer.index()
    assert first["generation"] == unchanged["generation"]

    def unexpected_read(_: Path) -> bytes:
        raise AssertionError("unchanged source should not be read")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    assert indexer.index()["generation"] == unchanged["generation"]
    monkeypatch.undo()

    source.write_text("def answer():\n    return 43\n", encoding="utf-8")
    updated = indexer.index()
    second_id = store.symbols_for_file("sample.py")[1]["id"]
    assert updated["generation"] == first["generation"] + 1
    assert first_id == second_id


def test_partial_file_retains_last_good_symbols(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / ".ratlas" / "index.db")
    indexer = Indexer(Config(root=tmp_path), store)
    indexer.index()

    source.write_text("def answer(:\n", encoding="utf-8")
    status = indexer.index()
    assert status["parse_failures"] == 1
    assert any(row["qualname"] == "answer" for row in store.symbols_for_file("sample.py"))


def test_impact_follows_resolved_call_edges(tmp_path: Path) -> None:
    (tmp_path / "library.py").write_text("def normalize(value):\n    return value\n", encoding="utf-8")
    (tmp_path / "client.py").write_text(
        "from library import normalize\n\ndef handle(value):\n    return normalize(value)\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / ".ratlas" / "index.db")
    Indexer(Config(root=tmp_path), store).index()
    impact = ContextEngine(Config(root=tmp_path), store).impact("normalize")
    assert any(item["path"] == "client.py" and item["qualname"] == "handle" for item in impact)


def test_partial_update_recovers_healthy_suffix_and_retains_damaged_symbol(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "def before():\n    return 1\n\ndef broken(value):\n    return value\n\ndef after(value):\n    return value\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / ".ratlas" / "index.db")
    indexer = Indexer(Config(root=tmp_path), store)
    indexer.index()

    source.write_text(
        "def before():\n    return 2\n\ndef broken(:\n    return 9\n\ndef after(value, extra=0):\n    return value + extra\n",
        encoding="utf-8",
    )
    indexer.index()
    symbols = {row["qualname"]: row for row in store.symbols_for_file("sample.py")}
    assert "broken" in symbols
    assert "extra=0" in symbols["after"]["signature"]
    assert symbols["before"]["body_hash"] != ""


def test_added_duplicate_name_invalidates_previously_unique_edge(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("def normalize(value):\n    return value\n", encoding="utf-8")
    (tmp_path / "client.py").write_text("def handle(value):\n    return normalize(value)\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / ".ratlas" / "index.db")
    indexer = Indexer(Config(root=tmp_path), store)
    indexer.index()
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM edges WHERE dst_tail='normalize' AND dst_symbol IS NOT NULL"
        ).fetchone()[0] == 1

    (tmp_path / "two.py").write_text("def normalize(value):\n    return value * 2\n", encoding="utf-8")
    indexer.index()
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM edges WHERE dst_tail='normalize' AND dst_symbol IS NOT NULL"
        ).fetchone()[0] == 0


def test_writer_lock_serializes_threads(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / ".ratlas" / "index.db")
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def first_writer() -> None:
        with store.writer_lock():
            first_entered.set()
            release_first.wait(timeout=2)

    def second_writer() -> None:
        with store.writer_lock():
            second_entered.set()

    first = Thread(target=first_writer)
    second = Thread(target=second_writer)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)
    assert second_entered.is_set()


def test_store_rejects_future_schema_version(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "index.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer than supported"):
        SQLiteStore(database)


def test_unambiguous_symbol_rename_preserves_id(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def old_name(value):\n    return value + 1\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / ".ratlas" / "index.db")
    indexer = Indexer(Config(root=tmp_path), store)
    indexer.index()
    original_id = next(row["id"] for row in store.symbols_for_file("sample.py") if row["name"] == "old_name")

    source.write_text("def new_name(value):\n    return value + 1\n", encoding="utf-8")
    indexer.index()
    renamed = next(row for row in store.symbols_for_file("sample.py") if row["name"] == "new_name")
    assert renamed["id"] == original_id