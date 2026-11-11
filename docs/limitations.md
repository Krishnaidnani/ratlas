# Limitations

RepoAtlas 1.0.0 is a useful navigation index, not a compiler or IDE language server.

- Python uses the standard-library AST with best-effort partial recovery.
- TypeScript/JavaScript and Java use structural scanning; modern syntax and malformed files can be missed or misclassified.
- Reference resolution is heuristic except for configured normalized SCIP mappings.
- Token counts use a byte-based estimate and can differ from model tokenizer counts.
- Offline benchmark reports retrieval estimates only; assistant execution, dollar costs, and pass rates are not measured.
- The real-history replay command requires a Git repository with commits.
- This release does not include an LSP/SCIP protobuf importer, alternate graph store, embedding search, or hosted service.

Verify all impact results and symbol ranges against the source repository before making changes.
# Limitations

- TypeScript/JavaScript and Java parsing is structural scanning, not language-server/compiler parsing. Complex syntax, decorators/annotations, overloads, and malformed code can be missed.
- Python partial recovery keeps declarations around damaged ranges but cannot guarantee full fidelity during arbitrary edits.
- Without a normalized SCIP mapping, symbol resolution is heuristic. Impact results are hints, not proof of breakage.
- Token counts use a conservative byte proxy. They are not exact counts for a specific model tokenizer.
- The offline benchmark compares retrieval estimates only. It does not drive an assistant, execute task tests, or establish success@1 or cost savings.
- The SCIP integration accepts a normalized JSON mapping produced externally; it does not invoke SCIP or read protobuf indexes.
- Daemon transport is authenticated loopback TCP; named-pipe transport and robust service-manager integration are not included.
- No embedding service, remote index, alternate graph store, or cross-repository search is included.
