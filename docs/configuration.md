# Configuration

`ratlas init` writes a starter `ratlas.toml`. See the [complete sample configuration](ratlas.toml), also available as `ratlas.toml.example` in the repository root.

- `index.languages`: enabled parser families (`python`, `typescript`, `java`).
- `index.include` and `index.exclude`: repository-relative glob patterns.
- `index.max_file_bytes`: skip larger source files.
- `index.bulk_threshold_pct`: percentage triggering a global edge rebuild.
- `resolve.confidence_floor`: minimum edge confidence for relationship traversal.
- `resolve.precise_provider`: `none` or `scip` normalized mapping provider.
- `privacy.never_index`: paths excluded before reading source.
- `privacy.redact_patterns`: regular expressions applied to returned text.
- `budget.*`: response limits and conservative token estimate settings.
- `store.path`: SQLite database file or directory.

SCIP mappings use JSON entries identifying the source file/symbol/reference and target file/qualified name. Generate the map with an external language-specific SCIP integration; RepoAtlas does not parse SCIP protobuf directly.
