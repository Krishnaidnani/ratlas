# CLI and MCP

## CLI

Useful commands:

- `ratlas index [--full]`: build or reconcile the local index.
- `ratlas query map --budget 2000`: print a repository overview.
- `ratlas query context "task" --focus src/file.py`: retrieve task context.
- `ratlas query symbol NAME [--kind function]`: find symbol definitions.
- `ratlas callers TARGET`, `ratlas callees TARGET`, and `ratlas impact TARGET`: inspect graph relationships.
- `ratlas daemon start|status|stop`: share an index writer locally.
- `ratlas replay --commits 50`: compare incremental and fresh graphs over Git history.
- `ratlas bench run --tasks bench/tasks.json`: produce offline A/B/C retrieval estimates.

The benchmark does not run an assistant or infer task success. It reports that field as unmeasured.

## MCP

Install the `mcp` extra and configure a stdio server that runs `ratlas serve` with the repository as its working directory. Tools: `repo_map`, `get_context`, `find_symbol`, `get_symbol`, `callers_of`, `callees_of`, `impact_of`, and `index_status`. Resource: `ratlas://map`. Prompt: `ratlas-onboard`.

The surface is read-only. Freshness requests reconcile via the shared daemon when running and otherwise index in the server process.
