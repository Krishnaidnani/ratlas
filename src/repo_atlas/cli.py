"""Command-line entry points for indexing, querying, and serving RepoAtlas."""

from pathlib import Path
import json
import subprocess
import time

import typer

from repo_atlas import __version__
from repo_atlas.benchmark import load_benchmark, run_benchmark
from repo_atlas.config import load_config
from repo_atlas.daemon import request_daemon, start_daemon, stop_daemon
from repo_atlas.engine import ContextEngine
from repo_atlas.indexer import Indexer
from repo_atlas.replay import replay_history
from repo_atlas.server import create_server
from repo_atlas.store import SQLiteStore

app = typer.Typer(name="ratlas", no_args_is_help=True, help="Persistent structural codebase index for AI assistants.")
query_app = typer.Typer(no_args_is_help=True, help="Query the local repository index.")
bench_app = typer.Typer(no_args_is_help=True, help="Run offline retrieval quality benchmarks.")
app.add_typer(query_app, name="query")
app.add_typer(bench_app, name="bench")


def _objects(root: Path) -> tuple[object, SQLiteStore, Indexer, ContextEngine]:
    config = load_config(root)
    store = SQLiteStore(config.db_path)
    return config, store, Indexer(config, store), ContextEngine(config, store)


@app.callback()
def main() -> None:
    """RepoAtlas command line."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def daemon(
    action: str = typer.Argument(..., help="One of: start, stop, status."),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """Start, stop, or inspect the per-repository shared index daemon."""
    if action == "start":
        process = start_daemon(root)
        typer.echo("Already running" if process is None else f"Started daemon pid={process.pid}")
        return
    if action == "stop":
        if not stop_daemon(root):
            raise typer.BadParameter("no running RepoAtlas daemon found")
        typer.echo("Stopped")
        return
    if action == "status":
        result = request_daemon(root, "status")
        typer.echo(json.dumps(result, indent=2) if result else "Not running")
        return
    raise typer.BadParameter("action must be start, stop, or status")


@app.command()
def replay(
    commits: int = typer.Option(50, min=1, max=10000),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """Compare incremental indexing with fresh builds across recent Git history."""
    try:
        typer.echo(json.dumps(replay_history(root, commits), indent=2))
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.CalledProcessError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error


@bench_app.command("run")
def benchmark_run(
    tasks: Path = typer.Option(..., exists=True, dir_okay=False),
    arms: str = typer.Option("A,B,C"),
    repeat: int = typer.Option(3, min=1, max=100),
    output: Path = typer.Option(Path(".ratlas/bench/latest.json")),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """Run offline baseline/map/context retrieval comparisons from a task manifest."""
    _, store, indexer, engine = _objects(root)
    report_output = output if output.is_absolute() else root / output
    report = run_benchmark(root, store, indexer, engine, tasks,
                           tuple(part.strip().upper() for part in arms.split(",") if part.strip()),
                           repeat, report_output)
    typer.echo(json.dumps({"output": str(report_output), "summary": report["summary"]}, indent=2))
    store.close()


@bench_app.command("report")
def benchmark_report(path: Path = typer.Argument(Path(".ratlas/bench/latest.json"))) -> None:
    """Print summary metrics from an offline benchmark report."""
    report = load_benchmark(path)
    typer.echo(json.dumps(report["summary"], indent=2))


@app.command()
def init(root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True)) -> None:
    """Create a starter ratlas.toml in ROOT."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "ratlas.toml"
    if path.exists():
        raise typer.BadParameter(f"{path} already exists")
    path.write_text(
        '[index]\nlanguages = ["python", "typescript", "java"]\n'
        'include = ["**/*.py", "**/*.pyi", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.java"]\n'
        'exclude = ["**/.venv/**", "**/__pycache__/**", "**/build/**", "**/dist/**"]\n'
        'max_file_bytes = 1000000\n\n[budget]\ndefault_map_tokens = 2000\ndefault_context_tokens = 4000\n'
        'max_tokens = 16000\nsafety_factor = 0.9\ntokenizer = "cl100k_base"\n\n'
        '[resolve]\nconfidence_floor = 0.15\nprecise_provider = "none"\n\n'
        '[privacy]\nnever_index = [".env*", "**/*.pem", "**/id_rsa*", "**/*.p12", "**/secrets/**"]\n',
        encoding="utf-8"
    )
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".ratlas/" not in existing.splitlines():
        gitignore.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n") + ".ratlas/\n", encoding="utf-8")
    typer.echo(f"Created {path}")


@app.command()
def index(
    full: bool = typer.Option(False, "--full", help="Re-parse every eligible file."),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """Build or incrementally update the index."""
    _, store, indexer, _ = _objects(root)
    typer.echo(json.dumps(indexer.index(force=full), indent=2))
    store.close()


@app.command()
def status(root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True)) -> None:
    """Show index generation, counts, and parser health."""
    _, store, _, engine = _objects(root)
    typer.echo(json.dumps(engine.status(), indent=2))
    store.close()


@app.command()
def doctor(
    repair: bool = typer.Option(False, "--repair", help="Rebuild the disposable index database."),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """Check SQLite integrity and reconcile the index."""
    config = load_config(root)
    if repair:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{config.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()
    store = SQLiteStore(config.db_path)
    indexer = Indexer(config, store)
    with store.connect() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    result = indexer.index()
    report = {"integrity": integrity, "schema_version": schema_version,
              "foreign_key_errors": len(foreign_keys), **result}
    typer.echo(json.dumps(report, indent=2))
    store.close()
    if integrity != "ok" or foreign_keys or schema_version != 1:
        raise typer.Exit(1)


@query_app.command("map")
def query_map(
    budget: int = typer.Option(2000, min=100, max=16000),
    path: str = typer.Option("", "--path"),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """Print the repository map."""
    _, store, indexer, engine = _objects(root)
    indexer.index()
    rendered = engine.render("", budget)
    if path:
        rendered = "\n".join(line for line in rendered.splitlines() if line.startswith("#") or line.startswith(path))
    typer.echo(rendered)
    store.close()


@query_app.command("context")
def query_context(
    task: str,
    budget: int = typer.Option(4000, min=100, max=16000),
    focus: str = typer.Option("", "--focus", help="Comma-separated repository-relative paths."),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """Print ranked context for TASK."""
    _, store, indexer, engine = _objects(root)
    indexer.index()
    typer.echo(engine.render(task, budget, [item.strip() for item in focus.split(",") if item.strip()]))
    store.close()


@query_app.command("symbol")
def query_symbol(
    name: str,
    kind: str | None = typer.Option(None, "--kind", help="Filter by class, function, method, or module."),
    limit: int = typer.Option(10, min=1, max=100),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """Find symbol definitions by name."""
    _, store, indexer, engine = _objects(root)
    indexer.index()
    typer.echo(json.dumps(engine.find_symbol(name, limit, kind), indent=2))
    store.close()


@app.command()
def impact(
    target: str,
    depth: int = typer.Option(3, min=0, max=6),
    min_confidence: float = typer.Option(0.15, min=0, max=1),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """List likely dependents of a file or symbol."""
    _, store, indexer, engine = _objects(root)
    indexer.index()
    typer.echo(json.dumps(engine.impact(target, depth, min_confidence=min_confidence), indent=2))
    store.close()


@app.command()
def callers(
    target: str,
    depth: int = typer.Option(1, min=1, max=6),
    limit: int = typer.Option(25, min=1, max=500),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """List symbols that call or otherwise depend on TARGET."""
    _, store, indexer, engine = _objects(root)
    indexer.index()
    typer.echo(json.dumps(engine.callers(target, depth, limit), indent=2))
    store.close()


@app.command()
def callees(
    target: str,
    depth: int = typer.Option(1, min=1, max=6),
    limit: int = typer.Option(25, min=1, max=500),
    root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True),
) -> None:
    """List symbols referenced by TARGET."""
    _, store, indexer, engine = _objects(root)
    indexer.index()
    typer.echo(json.dumps(engine.callees(target, depth, limit), indent=2))
    store.close()


@app.command()
def watch(root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True)) -> None:
    """Poll for repository changes and update the index."""
    _, store, indexer, _ = _objects(root)
    typer.echo(f"Watching {root.resolve()} (Ctrl+C to stop)")
    previous = -1
    try:
        while True:
            result = indexer.index()
            if result["generation"] != previous:
                typer.echo(json.dumps(result))
                previous = result["generation"]
            time.sleep(0.5)
    except KeyboardInterrupt:
        typer.echo("Stopped")
    finally:
        store.close()


@app.command()
def serve(root: Path = typer.Option(Path.cwd(), "--root", file_okay=False, resolve_path=True)) -> None:
    """Run the MCP server over stdio."""
    try:
        create_server(root).run(transport="stdio")
    except RuntimeError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from error


if __name__ == "__main__":
    app()