from pathlib import Path
import json

from repo_atlas.config import Config
from repo_atlas.engine import ContextEngine
from repo_atlas.indexer import Indexer
from repo_atlas.parser import parse_source
from repo_atlas.store import SQLiteStore


def test_typescript_symbols_and_call_references() -> None:
    source = b'''export class SessionStore {
  save(id: string): void {
    validate(id);
  }
}
export function validate(value: string): boolean {
  return Boolean(value);
}
'''
    result = parse_source(source, "typescript")
    symbols = {symbol.qualname: symbol for symbol in result.symbols}
    assert {"SessionStore", "SessionStore.save", "validate"}.issubset(symbols)
    assert symbols["SessionStore"].is_public
    assert any(reference.text == "validate" and reference.source_qualname == "SessionStore.save"
               for reference in result.references)


def test_java_symbols_and_method_calls() -> None:
    source = b'''package demo;
public class SessionStore {
    public void save(String id) {
        validate(id);
    }
    private boolean validate(String value) {
        return value != null;
    }
}
'''
    result = parse_source(source, "java")
    symbols = {symbol.qualname: symbol for symbol in result.symbols}
    assert "SessionStore" in symbols
    assert "SessionStore.save" in symbols
    assert "SessionStore.validate" in symbols
    assert not symbols["SessionStore.validate"].is_public
    assert any(reference.text == "validate" for reference in result.references)


def test_index_mixed_language_repository(tmp_path: Path) -> None:
    (tmp_path / "app.ts").write_text("export function launch() { start(); }\n", encoding="utf-8")
    (tmp_path / "Main.java").write_text("class Main { void run() { start(); } }\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    status = Indexer(config, store).index()
    with store.connect() as connection:
        languages = {row["lang"] for row in connection.execute("SELECT DISTINCT lang FROM files")}
    assert status["files"] == 3
    assert languages == {"python", "typescript", "java"}
    assert len(ContextEngine(config, store).find_symbol("run")) == 2


def test_scip_mapping_overrides_ambiguous_global_resolution(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("def target():\n    return 2\n", encoding="utf-8")
    (tmp_path / "client.py").write_text("def caller():\n    return target()\n", encoding="utf-8")
    mapping = tmp_path / "scip-map.json"
    mapping.write_text(json.dumps({"references": [{
        "file": "client.py", "source": "caller", "text": "target",
        "target_file": "two.py", "target_qualname": "target", "confidence": 1.0,
    }]}), encoding="utf-8")
    config_file = tmp_path / "ratlas.toml"
    config_file.write_text(
        '[resolve]\nprecise_provider = "scip"\nscip_mapping = "scip-map.json"\n', encoding="utf-8"
    )
    from repo_atlas.config import load_config

    config = load_config(tmp_path)
    store = SQLiteStore(config.db_path)
    indexer = Indexer(config, store)
    indexer.index()
    with store.connect() as connection:
        precise = indexer.scip_resolver.resolve(connection, "client.py", "caller", "target")
        raw_refs = [tuple(row) for row in connection.execute("SELECT src_qualname,dst_text FROM raw_refs")]
        edges = [tuple(row) for row in connection.execute("SELECT dst_text,dst_symbol,confidence FROM edges")]
        edge = connection.execute(
            "SELECT f.path,e.confidence FROM edges e JOIN symbols s ON s.id=e.dst_symbol "
            "JOIN files f ON f.id=s.file_id WHERE e.dst_text='target'"
        ).fetchone()
    assert edge is not None, f"missing resolved target edge; precise={precise!r}, refs={raw_refs!r}, edges={edges!r}"
    assert edge["path"] == "two.py"
    assert edge["confidence"] == 1.0