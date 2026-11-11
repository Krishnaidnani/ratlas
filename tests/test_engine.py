from pathlib import Path

from repo_atlas.config import Config
from repo_atlas.engine import ContextEngine
from repo_atlas.indexer import Indexer
from repo_atlas.store import SQLiteStore


def test_context_is_deterministic_and_within_token_budget(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        'def authenticate(user):\n    """Check credentials."""\n    return user\n', encoding="utf-8"
    )
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    Indexer(config, store).index()
    engine = ContextEngine(config, store)

    rendered = engine.render("authenticate user", budget=150)
    assert rendered == engine.render("authenticate user", budget=150)
    assert "authenticate" in rendered
    assert engine._count(rendered) <= 150


def test_symbol_lookup(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def calculate_total(value):\n    return value\n", encoding="utf-8")
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    Indexer(config, store).index()
    engine = ContextEngine(config, store)
    assert engine.find_symbol("calculate_total")[0]["qualname"] == "calculate_total"


def test_default_config_can_be_loaded(tmp_path: Path) -> None:
    from repo_atlas.config import load_config

    config = load_config(tmp_path)
    assert "**/*.py" in config.index.include
    assert "**/*.ts" in config.index.include
    assert "**/*.java" in config.index.include


def test_context_hard_caps_oversized_task_header(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    engine = ContextEngine(config, store)
    rendered = engine.render("task " * 1000, budget=100)
    assert engine._count(rendered) <= 100


def test_context_respects_small_requested_budgets(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    engine = ContextEngine(config, SQLiteStore(config.db_path))
    for budget in (0, 1, 10, 50):
        assert engine._count(engine.render("a lengthy task header", budget=budget)) <= budget


def test_default_secret_paths_are_not_indexed(tmp_path: Path) -> None:
    (tmp_path / "safe.py").write_text("def safe():\n    return True\n", encoding="utf-8")
    (tmp_path / "credentials.pem").write_text("PRIVATE KEY", encoding="utf-8")
    (tmp_path / ".env.local").write_text("TOKEN=value", encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "credentials.py").write_text("PASSWORD = 'value'\n", encoding="utf-8")
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    status = Indexer(config, store).index()
    assert status["files"] == 1


def test_symbol_results_redact_sensitive_text(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        'def load():\n    """token=supersecret"""\n    return "safe"\n', encoding="utf-8"
    )
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    Indexer(config, store).index()
    result = ContextEngine(config, store).get_symbol("load")
    assert result is not None
    assert "supersecret" not in str(result)
    assert "[REDACTED]" in str(result)


def test_callers_and_callees_report_direction_and_confidence(tmp_path: Path) -> None:
    (tmp_path / "library.py").write_text("def normalize(value):\n    return value\n", encoding="utf-8")
    (tmp_path / "client.py").write_text("def handle(value):\n    return normalize(value)\n", encoding="utf-8")
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    Indexer(config, store).index()
    engine = ContextEngine(config, store)

    callers = engine.callers("normalize", file="library.py")
    callees = engine.callees("handle", file="client.py")
    assert any(row["qualname"] == "handle" and row["confidence"] == 0.5 for row in callers)
    assert any(row["qualname"] == "normalize" and row["path"] == "library.py" for row in callees)


def test_requested_symbol_body_preserves_lines(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def render():\n    first = 1\n    return first\n", encoding="utf-8")
    config = Config(root=tmp_path)
    store = SQLiteStore(config.db_path)
    Indexer(config, store).index()
    body = ContextEngine(config, store).get_symbol("render", include_body=True)["body"]
    assert "\n" in str(body)