# Architecture and Project Structure

RepoAtlas uses a `src`-based Python package layout. Runtime code lives under `src/repo_atlas`; tests, documentation, and benchmark inputs stay outside the installable package. This keeps the distribution boundary explicit and lets tests exercise the installed package rather than accidentally importing files from the repository root.

## Repository map

```text
.
|-- src/repo_atlas/       Installable application package
|-- tests/                Automated tests, grouped by behavior
|-- docs/                 User and maintainer documentation
|-- bench/                Offline benchmark task manifests
|-- .github/workflows/    Continuous integration
|-- pyproject.toml        Packaging, dependencies, and tool configuration
|-- ratlas.toml.example   Example repository configuration
|-- README.md             Project overview and quick start
```

The main package modules have distinct responsibilities:

| Module | Responsibility |
| --- | --- |
| `cli.py` | Typer commands and command-line orchestration |
| `server.py` | Optional, read-only MCP tools, resources, and prompts |
| `config.py` | Configuration models, defaults, and validation |
| `indexer.py` | File discovery, incremental updates, and index coordination |
| `parser.py` | Language-specific structural extraction |
| `models.py` | Shared records used across parsing, indexing, and storage |
| `diff.py` | Symbol change and rename matching |
| `resolver.py` | Reference resolution, including optional SCIP mappings |
| `store.py` | SQLite schema, transactions, locks, and persistence |
| `engine.py` | Search, ranking, graph queries, redaction, and context rendering |
| `daemon.py` | Local shared-index process and client requests |
| `benchmark.py` | Offline retrieval benchmark execution and reports |
| `replay.py` | Incremental-versus-full indexing checks across Git history |

## Data flow

```text
CLI or MCP request
        |
        +--> load and validate configuration
        |
        +--> Indexer --> discover eligible files --> parser --> symbols/references
        |                     |                                      |
        |                     +------ incremental changes ------------+
        |                                                            |
        +--> ContextEngine <------------------------------ SQLiteStore
                 |
                 +--> ranked, bounded, redacted results
```

The CLI and MCP server are adapters: they translate external requests into calls to the indexing, storage, and query layers. The indexer coordinates work but delegates parsing, symbol matching, reference resolution, and persistence to their respective modules. The query engine reads the persisted index and shapes results; callers should not reach into SQLite directly for user-facing queries. The optional daemon shares indexing work between local clients but does not change the stored data model.

## Extension conventions

- Keep command and protocol code at the edges. Put reusable behavior in the owning domain module so it can be called and tested without starting a CLI or server.
- Keep parsing deterministic and return the shared records in `models.py`; do not make parsers write to the database.
- Put schema and transaction behavior in `store.py`. Changes to persisted data must account for schema versioning and existing index files.
- Add tests beside the behavior they protect, using temporary repositories and databases. Prefer public behavior over assertions on private implementation details.
- Keep optional integrations optional. Import an integration only when it is used and provide an actionable message when its extra is missing.
- Add a new module when it gives a domain a clear owner. Split a module into a package when the domain has multiple independently understandable components; avoid moving files only to make the tree look larger.

## Quality checks

Run the test and packaging checks from the repository root after installing the development extra. Install the documentation extra to build the site:

```powershell
python -m pytest
ratlas replay --commits 50
python -m pip wheel --no-deps --wheel-dir dist .
python -m pip install -e ".[docs]"
python -m mkdocs build --strict
```

CI currently runs the test suite, Git-history replay check, and wheel build. Ruff and mypy are configured in `pyproject.toml`; add them to the development dependencies and CI before treating them as required checks. Keep CI changes in sync with the checks documented here when adding or replacing tooling.