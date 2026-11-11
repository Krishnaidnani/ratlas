# RepoAtlas

RepoAtlas builds a local, persistent structural index for Python, TypeScript/JavaScript, and Java repositories. It exposes ranked source maps and dependency queries through a CLI and optional MCP server.

## What it provides

- Incremental SQLite indexing with stable symbol identifiers.
- Symbol, caller, callee, impact, and bounded context queries.
- Optional shared daemon for multiple local clients.
- Configurable file exclusions and output redaction.
- Git-history parity checks and offline retrieval estimates.

Start with [installation](installation.md), then review [architecture and project structure](architecture.md), [configuration](configuration.md), and the [CLI/MCP tools](tools.md).
