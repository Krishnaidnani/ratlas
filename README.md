# RepoAtlas

RepoAtlas 1.0.0 is a local-first structural index for Python, TypeScript/JavaScript, and Java repositories. It includes transactional SQLite storage, incremental symbol updates and same-file rename identity, partial Python recovery, heuristic reference edges, optional SCIP-mapped edge overrides, personalized PageRank with lexical/focus seeds, bounded context rendering, privacy exclusions/redaction, a shared daemon, read-only MCP tools/resources/prompts, offline retrieval benchmarking, and Git-history incremental-vs-full replay.

The TypeScript/Java extractors are structural scanners, not compiler parsers. Token estimates are byte-based rather than model-tokenizer exact. The offline benchmark does not execute an assistant or report task pass rates. SCIP mappings are normalized JSON produced by external language tooling; RepoAtlas does not parse SCIP protobuf indexes itself. Treat maps and impact output as navigation hints and verify them against source.


## Install

Requires Python 3.11 or newer.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,mcp]"
```

## Quick start

Run from the repository to index:

```powershell
ratlas init
ratlas index
ratlas status
ratlas query map --budget 2000
ratlas query context "add rate limiting to login" --budget 4000
ratlas query symbol SessionManager
ratlas callers SessionManager.create
ratlas callees SessionManager.create
ratlas impact src/auth/session.py
ratlas doctor
```

The index defaults to `.ratlas/index.db`; `ratlas init` adds `.ratlas/` to `.gitignore`. No source contents are sent over the network. `ratlas watch` polls the repository and updates the index; stop with Ctrl+C. The scanner uses a writer lock across scan and commit to serialize concurrent CLI/MCP writers.

Use `ratlas daemon start|status|stop` to share one local writer across clients. It binds to loopback and authenticates requests with a per-run random token.

## MCP

Install with the `mcp` extra, then configure a client to launch `ratlas serve` with its working directory set to the repository root. Read-only tools are `repo_map`, `get_context`, `find_symbol`, `get_symbol`, `callers_of`, `callees_of`, `impact_of`, and `index_status`; resource `ratlas://map` and prompt `ratlas-onboard` are also registered. Query tools accept `fresh` to request reconciliation before reading. Example Cursor configuration:

```json
{
  "mcpServers": {
    "repo-atlas": {
      "command": "ratlas",
      "args": ["serve"]
    }
  }
}
```

## Architecture

See the [architecture guide](docs/architecture.md) for the repository map, module responsibilities, data flow, and extension conventions.

## Configuration

`ratlas init` creates a starter `ratlas.toml`. Supported settings include `index.languages` (`python`, `typescript`, `java`), include/exclude patterns, `index.max_file_bytes`, `index.bulk_threshold_pct`, `resolve.confidence_floor`, `resolve.precise_provider`, `resolve.scip_mapping`, privacy filters, budget settings, and `store.path`.

For precise edge overrides, set `resolve.precise_provider = "scip"` and point `resolve.scip_mapping` to JSON with a `references` array containing `file`, `source`, `text`, `target_file`, `target_qualname`, and optional `confidence` fields. Produce this normalized mapping with language-specific SCIP tooling; RepoAtlas does not invoke SCIP itself.

## Development

```powershell
python -m pytest
ratlas replay --commits 50
ratlas bench run --tasks bench/tasks.json --arms A,B,C --repeat 3
ratlas bench report .ratlas/bench/latest.json
```

SQLite changesets are transactional; unchanged content is skipped and symbol rows are upserted to preserve IDs. A cross-process writer lock covers scan and commit. Python syntax-error files recover healthy declarations and retain prior symbols overlapping damaged ranges. Java/TypeScript support is structural and best-effort. Token budgets use a conservative byte-based proxy and hard cap; they are estimates, not exact model-token counts.

## Security and limitations

The MCP surface is read-only and does not execute indexed code. Treat repository-derived text as untrusted. Secret filters are defense-in-depth; review `privacy.never_index` and `privacy.redact_patterns`. Impact edges use heuristics unless exact SCIP mappings are configured. Java/TypeScript syntax-error recovery and assistant success benchmarking are not implemented. Rebuild the index cache with `ratlas index --full` or `ratlas doctor --repair`.