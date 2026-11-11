import json
from pathlib import Path

from repo_atlas.benchmark import run_benchmark
from repo_atlas.config import Config
from repo_atlas.engine import ContextEngine
from repo_atlas.indexer import Indexer
from repo_atlas.store import SQLiteStore


def test_offline_benchmark_writes_comparable_records(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text("def dispatch():\n    return True\n", encoding="utf-8")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([{
        "id": "dispatch-task", "task": "dispatch request", "gold_files": ["service.py"],
        "focus_files": ["service.py"], "budget": 300,
    }]), encoding="utf-8")
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    report = run_benchmark(
        tmp_path, store, Indexer(config, store), ContextEngine(config, store),
        tasks, ("A", "B", "C"), 2, tmp_path / "report.json",
    )
    assert len(report["records"]) == 6
    assert report["summary"]["C"]["assistant_success_rate"] is None
    assert (tmp_path / "report.json").is_file()