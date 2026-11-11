# RepoAtlas

**A persistent, incrementally-updated map of your codebase that AI coding assistants can query instead of re-reading the repo every session.**

> **Status:** design / pre-alpha. This document is the end-to-end specification and build guide. Code snippets are reference sketches meant to show the shape of the implementation; they have not been run against a real repository. Numbers labelled *target* are goals to be validated by the benchmark harness in [Section 13](#13-testing-and-evaluation), not measured results.
>
> *"RepoAtlas" / `ratlas` is a working name; rename freely.*

---

## Table of contents

1. [Problem and motivation](#1-problem-and-motivation)
2. [Goals and non-goals](#2-goals-and-non-goals)
3. [How it works (overview)](#3-how-it-works-overview)
4. [Core concepts and glossary](#4-core-concepts-and-glossary)
5. [Architecture in detail](#5-architecture-in-detail)
6. [Data model](#6-data-model)
7. [Write path: the incremental indexer](#7-write-path-the-incremental-indexer)
8. [Read path: the context engine](#8-read-path-the-context-engine)
9. [MCP interface specification](#9-mcp-interface-specification)
10. [Storage abstraction and backends](#10-storage-abstraction-and-backends)
11. [Consistency, concurrency and failure handling](#11-consistency-concurrency-and-failure-handling)
12. [Security and privacy](#12-security-and-privacy)
13. [Testing and evaluation](#13-testing-and-evaluation)
14. [Repository layout and tech stack](#14-repository-layout-and-tech-stack)
15. [Build guide (phased)](#15-build-guide-phased)
16. [Configuration reference](#16-configuration-reference)
17. [CLI reference](#17-cli-reference)
18. [Integrating with assistants](#18-integrating-with-assistants)
19. [Observability](#19-observability)
20. [Risks and mitigations](#20-risks-and-mitigations)
21. [Roadmap](#21-roadmap)
22. [FAQ](#22-faq)
23. [References](#23-references)

---

## 1. Problem and motivation

AI coding assistants (Claude Code, Cursor, Copilot and others) work on a repository they have never seen. Every session they pay an **exploration tax**: listing directories, grepping, opening files, reading half of them, discovering they were the wrong files, and repeating. That exploration is billed in tokens and wall-clock time, and it competes with the actual task for context-window space.

Existing approaches each solve part of the problem:

| Approach | What it does well | Where it falls short |
|---|---|---|
| Agentic search (grep, glob, read) | Always fresh, zero setup, precise | Many turns, many tokens, no global view |
| Embedding index (e.g. chunk + vector search) | Fast semantic lookup | Chunks lose structure; re-embedding on change; no notion of "what depends on this" |
| Repo map (e.g. Aider's tree-sitter + PageRank map) | Compact, structural, token-budgeted | Typically rebuilt per run; no change-impact reasoning; tied to one tool |
| IDE-native indexing | Deep integration | Locked into one editor |

**RepoAtlas' thesis:** keep a *structural* model of the repository (symbols and the relationships between them) that is **updated incrementally at symbol granularity**, and expose it through an open protocol (MCP) with a **token-budgeted query interface**. When a function's signature changes, the system knows *what else is affected*, not just that a file changed.

The value is not "put the whole repo in the prompt." A real repository does not fit in a context window, and if it did, it would be wasteful. The value is giving the assistant the *right few thousand tokens* (a ranked map, plus exact line ranges to read next) so it skips the blind exploration phase.

---

## 2. Goals and non-goals

### Goals

- **G1. Token reduction:** measurably reduce tokens, turns and cost for common coding tasks versus an assistant's default exploration (validated by benchmark, [Section 13](#13-testing-and-evaluation)).
- **G2. Incremental correctness:** the incrementally updated index must be *identical* to a from-scratch index of the same working tree (the central invariant).
- **G3. Low latency:** single-file edits reflected in the index fast enough that a query issued right after an edit sees it (*target:* p95 under 150 ms for a one-file change on a mid-size repo).
- **G4. Tool-agnostic:** work with any MCP client; no per-assistant plugins.
- **G5. Local-first and private:** no source code leaves the machine in the core product; no network calls required.
- **G6. Change-impact awareness:** answer "what breaks if I change this?" and "which tests should I run?" from the dependency graph.
- **G7. Extensible:** languages, edge providers, ranking signals and storage backends are pluggable.

### Non-goals

- Replacing the assistant's own file reading, editing or search tools. RepoAtlas *points* the assistant to what to read; it does not hand over whole files.
- Being a full IDE-grade language server. Name resolution is best-effort by default; precise edges are an optional plugin ([Section 7.4](#74-reference-resolution)).
- Generating code or patches.
- Cross-repository search (v1). Single repo or monorepo only.
- Hosting or multi-tenant SaaS (v1).

---

## 3. How it works (overview)

```
                 ┌──────────────────────────────────────────────┐
                 │   AI coding assistants (MCP clients)         │
                 │   Claude Code · Cursor · Copilot · custom    │
                 └───────────────────────┬──────────────────────┘
                                         │  MCP (stdio / HTTP)
                 ┌───────────────────────▼──────────────────────┐
                 │  MCP SERVER  (tools · resources · prompts)   │
                 │  repo_map  find_symbol  get_symbol           │
                 │  callers_of  callees_of  impact_of           │
                 │  get_context  index_status                   │
                 └───────────────────────┬──────────────────────┘
                                         │ in-process API
                 ┌───────────────────────▼──────────────────────┐
                 │  CONTEXT ENGINE                              │
                 │  ┌───────────┐  ┌────────────┐  ┌─────────┐  │
                 │  │  Query    │→ │  Ranker    │→ │ Budgeter│  │
                 │  │  planner  │  │ (PPR+BM25) │  │ +render │  │
                 │  └───────────┘  └────────────┘  └─────────┘  │
                 └───────────────────────┬──────────────────────┘
                                         │ Storage interface
                 ┌───────────────────────▼──────────────────────┐
                 │  STORE    SQLite (default) · FalkorDB (opt.) │
                 │  files · symbols · edges · derived · ranks   │
                 └───────────────────────▲──────────────────────┘
                                         │ transactional writes
                 ┌───────────────────────┴──────────────────────┐
                 │  INCREMENTAL INDEXER  (single writer)        │
                 │  Watcher → Parser → Diff → Resolver →        │
                 │  Invalidator → Rank refresher                │
                 └───────────────────────▲──────────────────────┘
                                         │
                         git · filesystem · working tree
```

There are three flows through the system.

**Flow A: cold start (first index).**
1. Enumerate files (`git ls-files` plus untracked-not-ignored), filter by include/exclude rules and size.
2. Hash each file; parse in parallel with tree-sitter; extract symbols and raw references.
3. Insert symbols, resolve references into edges, compute file-level ranks, commit.

**Flow B: incremental update (an edit, save, checkout or pull).**
1. The watcher batches events (debounced) into a *changeset* of paths.
2. Changed files are re-parsed. Per file, the **diff engine** compares the old and new symbol tables and classifies every symbol: added, removed, renamed, signature-changed, body-changed, doc-changed, unchanged.
3. The **resolver** rebuilds outgoing edges for changed files and re-resolves dangling incoming edges.
4. The **invalidator** updates content-addressed hashes and marks derived artifacts (summaries, embeddings) whose inputs changed.
5. Ranks are refreshed in the background with a warm start.

**Flow C: query.**
1. An assistant calls a tool, e.g. `get_context(task="add rate limiting to login", budget=4000)`.
2. A *freshness barrier* flushes any pending file events so the answer reflects the current working tree.
3. The query planner extracts seeds (explicit focus files, lexical matches on symbol names and docs, recently modified files).
4. The ranker scores files and symbols (personalized PageRank plus lexical, recency and prior signals).
5. The budgeter greedily packs the highest-value-per-token content and renders a compact text map with **line ranges**, so the assistant can open exactly the slices it needs.

---

## 4. Core concepts and glossary

| Term | Definition |
|---|---|
| **Symbol** | A named code entity: module, class, function, method, constant, type. Every file has a synthetic `<module>` symbol that owns top-level statements and imports. |
| **Qualified name (`qualname`)** | Dotted path of a symbol within its file, e.g. `SessionManager.create`. Together with the file path it is the symbol's logical identity. |
| **Signature** | The declaration line(s) without the body: name, parameters, return type, bases. |
| **Skeleton** | The deterministic, self-contained rendering of a symbol: signature plus first docstring line. A skeleton depends only on the symbol's own file, so it never goes stale because of changes elsewhere. |
| **`sig_hash`** | Hash of the normalized signature. A change here can break callers. |
| **`body_hash`** | Hash of the body's *token stream* (whitespace and comments excluded). A change here cannot break callers' compilation, only behaviour. |
| **Edge** | A directed relationship between symbols: `calls`, `extends`, `imports`, `references`, `tests`. Carries a **confidence** (1.0 = precisely resolved, lower = heuristic). |
| **Delta** | The per-file result of the diff engine: a map from symbol to change kind, plus rename pairs. |
| **Changeset** | The batch of file paths processed in one write transaction. |
| **Derived artifact** | Anything computed from other data: LLM summaries, embeddings, rank scores. Stored with the **hash of its inputs**; valid only while that hash matches (build-system style invalidation). |
| **Generation** | Monotonic integer incremented per committed changeset. Used for consistent reads and cache keys. |
| **Freshness barrier** | A query-time step that applies pending file events before answering. |
| **Budget** | Maximum tokens a response may consume. Every read tool accepts one. |
| **Tier** | A level of detail when rendering: path only, top public symbols, all public symbols, everything. |
| **PPR** | Personalized PageRank: PageRank whose random-restart distribution is biased toward task-relevant seed files. |

---

## 5. Architecture in detail

### 5.1 Components and responsibilities

| Component | Responsibility | Key design rule |
|---|---|---|
| **Watcher** | Detect changes (fs events + git state) and emit debounced changesets | Git and content hashes are the source of truth; fs events are only a hint |
| **Parser** | tree-sitter parse per file; extract symbols and raw references | Pure function: `(path, bytes) -> ParseResult`. No DB access |
| **Diff engine** | Compare old/new symbol tables; classify changes; detect renames/moves | Operates on symbols, not lines |
| **Resolver** | Turn raw reference text into edges to symbol ids | Tiered strategy with confidence scores; pluggable precise providers |
| **Invalidator** | Maintain `deps_hash` and mark derived artifacts stale | Content-addressed; lazy by default |
| **Store** | Persist graph and derived data transactionally | Single writer, many readers; behind an interface |
| **Rank refresher** | Recompute file ranks after a generation commits | Debounced, warm-started, off the critical path |
| **Context engine** | Plan query, rank, pack to budget, render | Deterministic given (index generation, query, config) |
| **MCP server** | Expose tools/resources; enforce limits and freshness | Thin; no business logic |
| **CLI** | `init`, `index`, `watch`, `serve`, `query`, `doctor`, `bench` | Same code paths as the server |

### 5.2 Process model

Two supported topologies:

1. **Embedded (MVP):** `ratlas serve` runs the watcher, indexer and MCP server in one process, communicating over stdio with a single client. Simple. Downside: every client spawns its own process and its own writer.
2. **Daemon + thin proxy (recommended for v1):** one `ratlas daemon` per repo owns the DB and the watcher (single writer). `ratlas serve` is a thin MCP-stdio proxy that talks to the daemon over a Unix domain socket (named pipe on Windows). Multiple assistants share one index and never contend for writes.

Writer election for the embedded mode uses an advisory file lock next to the DB: the process holding the lock is the writer; the others open the DB read-only and poll the generation counter.

### 5.3 Where state lives

- **Index DB:** `~/.cache/ratlas/<repo-id>/index.db` by default (does not pollute the repo). `<repo-id>` is a hash of the canonical repo root plus remote URL. Optional `.ratlas/` in the repo (gitignored) is supported for portability.
- **Config:** `ratlas.toml` at repo root (committed) plus optional user-level overrides.
- **Logs and metrics:** `~/.cache/ratlas/<repo-id>/logs/`.

---

## 6. Data model

SQLite is the default backend. All DDL below is the v1 schema (`schema_version = 1`).

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- keys: schema_version, generation, head_commit, root_path, tokenizer

CREATE TABLE files (
  id           INTEGER PRIMARY KEY,
  path         TEXT    NOT NULL UNIQUE,      -- repo-relative, forward slashes
  lang         TEXT    NOT NULL,
  content_hash TEXT    NOT NULL,             -- blake2b of raw bytes
  size_bytes   INTEGER NOT NULL,
  mtime_ns     INTEGER NOT NULL,
  parse_status TEXT    NOT NULL DEFAULT 'ok',-- ok | partial | failed | skipped
  last_gen     INTEGER NOT NULL
);

CREATE TABLE symbols (
  id          INTEGER PRIMARY KEY,
  file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  qualname    TEXT    NOT NULL,              -- '<module>' for the module symbol
  name        TEXT    NOT NULL,
  kind        TEXT    NOT NULL,              -- module|class|function|method|const|type
  signature   TEXT    NOT NULL,
  doc         TEXT    NOT NULL DEFAULT '',
  start_line  INTEGER NOT NULL,
  end_line    INTEGER NOT NULL,
  parent_id   INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
  is_public   INTEGER NOT NULL DEFAULT 1,
  sig_hash    TEXT    NOT NULL,
  body_hash   TEXT    NOT NULL,
  deps_hash   TEXT,                          -- hash(body_hash + sig_hashes of deps), see 7.6
  UNIQUE (file_id, qualname)
);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_file ON symbols(file_id);

CREATE TABLE edges (
  src_symbol INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  dst_symbol INTEGER          REFERENCES symbols(id) ON DELETE SET NULL,
  kind       TEXT    NOT NULL,               -- calls|extends|imports|references|tests
  dst_text   TEXT    NOT NULL,               -- reference exactly as written
  dst_tail   TEXT    NOT NULL,               -- last dotted segment, for re-resolution
  confidence REAL    NOT NULL,               -- 1.0 precise ... 0.1 guess
  line       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (src_symbol, kind, dst_text, line)
);
CREATE INDEX idx_edges_dst        ON edges(dst_symbol);
CREATE INDEX idx_edges_unresolved ON edges(dst_tail) WHERE dst_symbol IS NULL;

CREATE TABLE derived (
  owner_kind TEXT NOT NULL,                  -- symbol | file | dir
  owner_key  TEXT NOT NULL,                  -- id or path
  artifact   TEXT NOT NULL,                  -- summary | embedding
  input_hash TEXT NOT NULL,                  -- valid only if equals current input hash
  payload    BLOB NOT NULL,
  model      TEXT,
  PRIMARY KEY (owner_kind, owner_key, artifact)
);

CREATE TABLE rank_scores (
  file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  score      REAL    NOT NULL,
  generation INTEGER NOT NULL
);

CREATE VIRTUAL TABLE symbol_fts USING fts5(
  name, qualname, doc, content='symbols', content_rowid='id'
);
CREATE TRIGGER symbols_ai AFTER INSERT ON symbols BEGIN
  INSERT INTO symbol_fts(rowid, name, qualname, doc)
  VALUES (new.id, new.name, new.qualname, new.doc);
END;
CREATE TRIGGER symbols_ad AFTER DELETE ON symbols BEGIN
  INSERT INTO symbol_fts(symbol_fts, rowid, name, qualname, doc)
  VALUES ('delete', old.id, old.name, old.qualname, old.doc);
END;
CREATE TRIGGER symbols_au AFTER UPDATE ON symbols BEGIN
  INSERT INTO symbol_fts(symbol_fts, rowid, name, qualname, doc)
  VALUES ('delete', old.id, old.name, old.qualname, old.doc);
  INSERT INTO symbol_fts(rowid, name, qualname, doc)
  VALUES (new.id, new.name, new.qualname, new.doc);
END;
```

### Schema rules that matter

1. **Symbol ids are stable.** On re-index, upsert by `(file_id, qualname)` and keep the row id. Deleting and re-inserting unchanged symbols would null out every incoming edge (`ON DELETE SET NULL`) and defeat incrementality.
2. **Edges keep the raw text** (`dst_text`, `dst_tail`). That is what allows cheap re-resolution when a target appears, disappears or is renamed, without re-parsing the source file.
3. **Every file has a `<module>` symbol.** Imports and top-level calls are edges *from* it, so the graph needs no separate file-edge table.
4. **Derived data is content-addressed** by `input_hash`. There is no "stale" boolean to forget to reset.

---

## 7. Write path: the incremental indexer

### 7.1 Change detection (Watcher)

Sources, in priority order:

1. **Git state:** `git diff --name-status -z HEAD` plus `git ls-files --others --exclude-standard -z` gives tracked changes and new files. Record `HEAD` in `meta.head_commit`.
2. **Filesystem events** (`watchdog`): used for low-latency detection while the user edits. Events are *hints*; every hinted path is re-`stat`ed and re-hashed.
3. **Reconcile on startup:** walk the tree, compare `(size, mtime_ns)` first, hash only on mismatch. Cheap even for large repos.

Debounce: collect events for a short window (*default 300 ms*), coalesce per path, then emit one changeset. Editors often write via temp-file-and-rename, so treat create/delete/move bursts as "path changed."

Special cases:

- **Branch switch / pull / rebase** can touch thousands of files. If a changeset exceeds a threshold (*default 30% of indexed files*), take the **bulk path**: parse in parallel, then rebuild edges globally instead of per-file re-resolution.
- **Ignored files:** honor `.gitignore` (via `pathspec`) plus `ratlas.toml` excludes. Skip binaries and files over `max_file_bytes`.
- **Symlinks:** do not follow outside the repo root.

### 7.2 Parsing (tree-sitter)

Parsing is a pure function from bytes to a `ParseResult`:

```python
@dataclass(frozen=True, slots=True)
class Symbol:
    qualname: str
    name: str
    kind: str            # module | class | function | method | const | type
    signature: str
    doc: str
    start_line: int
    end_line: int
    parent: str | None   # qualname of enclosing symbol
    sig_hash: str
    body_hash: str

@dataclass(frozen=True, slots=True)
class Ref:
    src_qualname: str    # symbol containing the reference ("<module>" for top level)
    text: str            # as written, e.g. "self.repo.save"
    kind: str            # call | extends | import | reference
    line: int

@dataclass(frozen=True, slots=True)
class ParseResult:
    symbols: list[Symbol]
    refs: list[Ref]
    status: str          # ok | partial | failed
```

Design requirements for every language plugin:

- **Normalize before hashing.** `body_hash` is computed over leaf tokens with comments dropped, so reformatting and comment edits do not register as changes. For classes, exclude nested definitions from the class's own body hash (each nested symbol has its own).
- **Be tolerant of broken code.** Users save in broken states constantly. tree-sitter produces a tree with `ERROR`/`MISSING` nodes instead of failing. Policy: extract symbols from the healthy portions, set `status = "partial"`, and **retain the last-good symbols whose line ranges overlap error nodes** rather than deleting them. This prevents graph flapping while someone is mid-edit.
- **Record raw reference text** exactly as written; resolution is a separate stage.
- **Mark visibility** where the language has it (`_private` prefix in Python, `export` in TypeScript, `public` in Java).

### 7.3 Diff engine

The diff engine compares the previous symbol table for a file with the freshly parsed one.

Change kinds:

| Kind | Condition | Downstream meaning |
|---|---|---|
| `added` | `qualname` only in new | New node; may resolve previously dangling edges |
| `removed` | `qualname` only in old | Node deleted; incoming edges become dangling |
| `renamed` | removed + added with identical `body_hash` (unambiguous) | Retarget incoming edges to the new node |
| `signature` | same `qualname`, different `sig_hash` | **Potentially breaking** for dependents |
| `body` | same `sig_hash`, different `body_hash` | Behavioural only; dependents' structure unaffected |
| `doc` | only docstring differs | Skeleton text changes; nothing else |
| `moved` | cross-file rename detected at changeset level | Retarget edges across files |

Reference implementation:

```python
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

class Change(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    RENAMED = "renamed"
    SIGNATURE = "signature"
    BODY = "body"
    DOC = "doc"

@dataclass
class Delta:
    file: str
    changes: dict[str, Change]      # qualname -> change
    renames: dict[str, str]         # old qualname -> new qualname (same file)

def diff_symbols(file: str, old: dict[str, Symbol], new: dict[str, Symbol]) -> Delta:
    changes: dict[str, Change] = {}

    for q in old.keys() & new.keys():
        o, n = old[q], new[q]
        if o.sig_hash != n.sig_hash:
            changes[q] = Change.SIGNATURE
        elif o.body_hash != n.body_hash:
            changes[q] = Change.BODY
        elif o.doc != n.doc:
            changes[q] = Change.DOC

    removed = {q: old[q] for q in old.keys() - new.keys()}
    added = {q: new[q] for q in new.keys() - old.keys()}

    # Rename detection: unambiguous 1:1 match on body_hash
    by_body: dict[str, list[str]] = defaultdict(list)
    for q, s in added.items():
        by_body[s.body_hash].append(q)
    removed_by_body: dict[str, list[str]] = defaultdict(list)
    for q, s in removed.items():
        removed_by_body[s.body_hash].append(q)

    renames: dict[str, str] = {}
    for body_hash, old_qs in removed_by_body.items():
        new_qs = by_body.get(body_hash, [])
        if len(old_qs) == 1 and len(new_qs) == 1:
            renames[old_qs[0]] = new_qs[0]
            changes[new_qs[0]] = Change.RENAMED

    for q in removed:
        if q not in renames:
            changes[q] = Change.REMOVED
    for q in added:
        if q not in renames.values():
            changes[q] = Change.ADDED

    return Delta(file, changes, renames)
```

**Cross-file move detection** runs once per changeset after all per-file deltas exist: match leftover `removed` symbols in one file with leftover `added` symbols in another by `body_hash` (and, for ties, by `sig_hash` plus name similarity). Ambiguous matches are *not* guessed; they degrade to remove + add.

> The "diff algorithm" in this project is *symbol-level structural diffing*. Line diffs (what git produces) are used only to decide *which files* to re-parse.

### 7.4 Reference resolution

Resolution converts `Ref.text` into `edges.dst_symbol`. It is the hardest part of the system, especially in dynamic languages. Use a **tiered strategy**, each tier assigning a confidence:

| Tier | Method | Confidence |
|---|---|---|
| 0 | **Precise provider** (optional plugin: SCIP index or LSP) | 1.0 |
| 1 | **Import-aware:** map local names to imported modules/symbols from the file's import statements; resolve `module.func()` through the module-path-to-file map | 0.9 |
| 2 | **Scope-aware:** same-file lookup; `self.method`/`this.method` against the enclosing class and its resolved bases (walk the MRO using `extends` edges) | 0.8 |
| 3 | **Unique global name:** exactly one symbol with that tail name in the repo | 0.5 |
| 4 | **Ambiguous global name:** multiple candidates; emit an edge to each with confidence `0.3 / n` | 0.3/n |
| 5 | **Unresolved:** store `dst_symbol = NULL`; keep `dst_text` for later re-resolution | n/a |

Rules:

- Edges below a configurable confidence floor (*default 0.15*) are excluded from ranking and impact analysis but remain stored.
- **Re-resolution triggers:** when a symbol is added/renamed, look up unresolved edges via `idx_edges_unresolved(dst_tail)` and re-run the resolver on just those. When a symbol is removed, the FK nulls its incoming edges and they re-enter the unresolved pool.
- **Outgoing edges of a changed file are replaced wholesale** (delete + insert within the transaction) because they are cheap to recompute and are derived purely from that file plus the current symbol table.
- A `ResolverPlugin` interface lets users plug in SCIP indexers (`scip-python`, `scip-typescript`, `scip-java`) for tier 0 where precision matters.

### 7.5 Applying a changeset (transaction boundary)

```text
apply_changeset(paths):
    parsed = parallel_parse(paths)                      # outside the transaction
    with store.write_txn() as tx:
        gen = tx.bump_generation()
        deltas = []
        for p in paths:
            if p deleted:
                old = tx.load_symbols(p)
                deltas.append(all_removed(p, old))
                tx.delete_file(p)                       # cascades symbols, edges
                continue
            old = tx.load_symbols(p)
            new = parsed[p]
            delta = diff_symbols(p, old, new.symbols)
            tx.upsert_file(p, hash, status, gen)
            tx.upsert_symbols(p, new.symbols, delta)    # stable ids; delete only removed
            tx.replace_outgoing_edges(p, new.refs)      # tiered resolver
            deltas.append(delta)
        moves = match_moves(deltas)                     # cross-file renames
        tx.apply_renames(deltas, moves)                 # retarget incoming edges
        tx.re_resolve_unresolved(touched_tails(deltas)) # added/renamed names
        invalidator.propagate(tx, deltas, depth=cfg.invalidation_depth)
    schedule_rank_refresh(gen)
```

Atomicity: the entire changeset is one SQLite transaction. A crash leaves the previous generation intact; the startup reconcile pass re-detects whatever was lost.

### 7.6 Invalidation (what is actually "stale")

Be precise about what can go stale, because it determines the cost of an update:

- **Skeletons never go stale due to other files.** A skeleton is signature + docstring from the symbol's own file.
- **Edges** can change when *other* files change (targets added/removed). Handled by re-resolution ([7.4](#74-reference-resolution)).
- **Derived artifacts** (LLM summaries, embeddings of enriched text) *can* go stale. They are invalidated by **input hashes**, like a build system:

```text
deps_hash(symbol) = H( body_hash(symbol),
                       sorted(sig_hash(d) for d in direct_deps(symbol)) )   # depth k = 1 by default
```

A derived artifact is valid iff its stored `input_hash` equals the symbol's current `deps_hash` (or the file/dir equivalent). Consequences:

| Event | Effect |
|---|---|
| Body-only change in `f` | `deps_hash(f)` changes; callers' `deps_hash` does **not** (they depend on `sig_hash` of `f`). Only `f`'s derived data refreshes. |
| Signature change in `f` | `deps_hash(f)` and every direct caller's `deps_hash` change. Callers' summaries recompute lazily. |
| Any change in file `F` | `F`'s file-level `input_hash` changes; so do its parent directories' hashes (Merkle-style). |

Refresh policy: **lazy** (recompute on next query that needs the artifact) or **eager in the background** (a worker drains a priority queue ordered by rank). Cascade depth is bounded (*default 1*) to prevent a hot utility function from invalidating the world.

### 7.7 Rank refresh

File ranks are global (PageRank over the file graph), so any changeset can in principle shift them. Practical handling:

- Recompute **in the background**, debounced (*default 2 s after the last commit*).
- **Warm start** power iteration from the previous scores; small edits converge in a few iterations.
- Queries never block on rank refresh; they use the latest completed generation's scores and report the generation used.

---

## 8. Read path: the context engine

### 8.1 Query planning

Every read request is turned into a **query plan**:

```text
Plan {
  budget_tokens: int
  seeds: {
    explicit_files:   [paths]            # focus_files parameter
    lexical_terms:    [terms]            # tokenized task text, identifiers split on camel/snake case
    recent_files:     [paths]            # git: recently modified / currently dirty
    semantic_vector:  optional embedding # only if the embeddings layer is enabled
  }
  min_confidence: float
}
```

Seed extraction:

1. **Explicit:** files the caller names (open editor tab, a stack trace path).
2. **Lexical:** split the task text into terms (`rate limiting` and `RateLimiter` both yield `rate`, `limit`); query `symbol_fts` with BM25; aggregate symbol scores to files.
3. **Recency:** files dirty in the working tree or touched in the last N commits get a small seed weight. Recent edits are strong predictors of relevance.
4. **Semantic (optional):** cosine similarity between a task embedding and symbol embeddings.

### 8.2 The graph used for ranking

Ranking runs on a **file-level graph** (cheap, stable); symbol-level scoring happens only *within* the selected files.

Edge weight from file A to file B:

```text
w(A→B) = Σ over symbol edges (a∈A, b∈B)  kind_weight[kind] · confidence · 1/√(multiplicity)
kind_weight = { extends: 1.5, calls: 1.0, references: 0.7, imports: 0.5, tests: 0.3 }
```

The square-root damping stops one file that calls a helper 200 times from dominating. Add each edge in the **reverse direction at 0.3×** weight: to edit a file well you need both what it depends on (to call things correctly) and what depends on it (to avoid breaking them).

### 8.3 Scoring

```python
import networkx as nx

def score_files(G: nx.DiGraph, seeds: dict[int, float], cfg) -> dict[int, float]:
    # Personalized PageRank; fall back to global PageRank if there are no seeds.
    pers = {n: w for n, w in seeds.items() if n in G} or None
    ppr = nx.pagerank(G, alpha=cfg.alpha, personalization=pers, weight="weight")
    global_rank = load_precomputed_rank_scores()          # background-refreshed
    lexical = lexical_file_scores(seeds)                  # BM25 aggregated per file
    recency = recency_scores()                            # 0..1 from git state

    def norm(d):                                          # rank-percentile normalization
        order = sorted(d, key=d.get)
        return {k: i / max(1, len(order) - 1) for i, k in enumerate(order)}

    nppr, nlex, nrec, nglob = map(norm, (ppr, lexical, recency, global_rank))
    w = cfg.weights   # defaults: ppr .55, lexical .30, recency .10, prior .05
    return {
        f: w.ppr * nppr.get(f, 0) + w.lexical * nlex.get(f, 0)
           + w.recency * nrec.get(f, 0) + w.prior * nglob.get(f, 0)
        for f in G.nodes
    }
```

Notes:

- Default weights are starting guesses; tune them on the benchmark harness ([Section 13](#13-testing-and-evaluation)) and expose them in config.
- For graphs beyond a few hundred thousand edges, replace `nx.pagerank` with a sparse power iteration (SciPy) warm-started from the previous vector.
- Rank-percentile normalization makes the signals comparable without hand-tuned scales.

**Symbol scoring within a selected file:**

```text
s(symbol) = 0.4 · lexical_match(symbol)
          + 0.3 · log(1 + weighted_in_degree(symbol))
          + 0.2 · kind_prior[kind]            # class > function > method > const
          + 0.1 · is_public
```

### 8.4 Budgeting (token-aware packing)

Tokens are estimated with a tokenizer proxy (`tiktoken` `cl100k_base` by default; calibrate against the target model's tokenizer in the benchmark). Always budget conservatively (*default safety factor 0.9*).

Each ranked file offers **increments** of increasing detail:

```text
I0: path only                                 (~8 tokens)
I1: + top 3 public symbols (signatures)
I2: + all public symbols
I3: + private symbols and docstring first lines
```

Greedy packing by marginal value per token:

```python
import heapq

def pack(files_ranked, budget, render_cost, gain):
    """files_ranked: list[(file_id, score)] descending.
    render_cost(file, level) -> tokens; gain(file, level) -> score increment."""
    used, chosen = header_cost(), {}
    heap = []   # max-heap on gain/cost via negative priority
    for f, s in files_ranked:
        c = render_cost(f, 0)
        heapq.heappush(heap, (-(gain(f, 0) / c), f, 0, c))
    while heap and used < budget:
        _, f, level, cost = heapq.heappop(heap)
        delta = cost - (render_cost(f, level - 1) if level else 0)
        if used + delta > budget:
            continue                                  # skip; a cheaper item may still fit
        used += delta
        chosen[f] = level
        if level < 3:
            nc = render_cost(f, level + 1)
            inc = nc - cost
            if inc > 0:
                heapq.heappush(heap, (-(gain(f, level + 1) / inc), f, level + 1, nc))
    return chosen, used
```

(`cost` carried in the heap entry is the *total* cost at that level; `delta` is the extra tokens to move from the previous level.)

### 8.5 Rendering

The output is compact, tree-ordered text. **Line ranges are the key feature**: they let the assistant open exactly the slice it needs with its own file-reading tool, rather than reading whole files.

```text
# repo-atlas map · generation 412 · 3,870/4,000 tokens · 7 of 143 files shown
# task: add rate limiting to login

src/auth/session.py  [rank 0.91]
  class SessionManager(BaseManager)                           L12-88
    def create(self, user_id: str, ttl: int = 3600) -> Session   L20-41
    def revoke(self, session_id: str) -> None                    L43-60
src/auth/login.py  [rank 0.88]
  def login(request: Request) -> Response                     L9-57
    calls: SessionManager.create (0.9), verify_password (0.9)
src/middleware/throttle.py  [rank 0.74]
  class Throttle                                              L5-70
    def allow(self, key: str) -> bool                            L22-48
... 4 more files (use find_symbol or get_context with a larger budget)
```

Rendering rules: stable ordering (by rank then path) so identical queries produce identical output (good for prompt caching); truncate docstrings to the first line; strip control characters; never include function bodies.

### 8.6 Impact analysis

`impact_of(target, depth=3)` answers *what breaks if I change this?*

1. Resolve `target` (a file path or symbol qualname) to symbol ids.
2. Reverse BFS over `calls`, `extends`, `imports`, `references` edges, with confidence multiplied along the path (decay), pruning below `min_confidence`.
3. Map affected symbols to files; identify **test files** among them via the `tests` edges and path conventions (`tests/`, `test_*.py`, `*Test.java`, `*.spec.ts`).
4. Output ranked by path confidence and depth, grouped as *direct dependents*, *transitive dependents*, *tests to run*.

SQLite implementation of the traversal:

```sql
WITH RECURSIVE impacted(sym, depth) AS (
  SELECT :target, 0
  UNION
  SELECT e.src_symbol, i.depth + 1
  FROM edges e JOIN impacted i ON e.dst_symbol = i.sym
  WHERE i.depth < :max_depth
    AND e.kind IN ('calls', 'extends', 'imports', 'references')
    AND e.confidence >= :min_conf
)
SELECT s.qualname, f.path, MIN(i.depth) AS depth
FROM impacted i
JOIN symbols s ON s.id = i.sym
JOIN files   f ON f.id = s.file_id
GROUP BY s.id
ORDER BY depth, f.path;
```

When the change is a *body-only* edit, `impact_of` can say so: dependents' compile-time contracts are unaffected (their `sig_hash` dependencies did not change), only behaviour may differ. When it is a *signature* change, it flags dependents as probably needing edits.

### 8.7 Caching

- Query results are cached by `(generation, normalized_query, budget, config_hash)`. A new generation invalidates naturally.
- Rendered file skeletons are cached by `(file content_hash, level)`.
- Because output ordering is deterministic, identical requests yield byte-identical responses, which maximizes prompt-cache hits on the assistant side.

---

## 9. MCP interface specification

RepoAtlas exposes **read-only** tools. No tool writes to the repository or executes code, which keeps the trust story simple.

### 9.1 Server metadata

The server sets MCP `instructions` so the model knows when to use it:

```text
RepoAtlas maintains a structural index of this repository. Prefer it over
exploratory grep/read when you need to orient yourself.
- Call get_context(task, budget) at the start of a task to get a ranked map with line ranges.
- Use find_symbol for definitions instead of searching the whole tree.
- Call impact_of before changing a public function, class or module.
- After receiving a map, read only the returned line ranges you need.
```

### 9.2 Tools

| Tool | Purpose | Key parameters | Returns |
|---|---|---|---|
| `get_context` | Task-focused, token-budgeted map. **Primary tool.** | `task: str`, `budget: int = 4000`, `focus_files: list[str] = []`, `fresh: bool = true` | Rendered map text + metadata (generation, tokens used) |
| `repo_map` | Task-independent overview of the repo | `budget: int = 2000`, `path_prefix: str = ""` | Rendered map |
| `find_symbol` | Locate definitions | `query: str`, `kind: str? `, `limit: int = 10` | Symbols: file, qualname, signature, line range |
| `get_symbol` | Detail for one symbol | `qualname: str`, `file: str?`, `include_body: bool = false`, `max_body_lines: int = 80` | Signature, doc, line range, optional body slice, in/out edge counts |
| `callers_of` | Who references this symbol | `qualname`, `file?`, `depth: int = 1`, `limit: int = 25` | Callers with confidence and line numbers |
| `callees_of` | What this symbol references | same | Callees with confidence |
| `impact_of` | Blast radius and tests to run | `target: str` (path or qualname), `depth: int = 3`, `min_confidence: float = 0.3` | Grouped dependents + tests |
| `index_status` | Health and freshness | none | generation, counts, lag, parse failures, head commit |

All tools accept an optional `fresh` flag (default true) controlling the freshness barrier. All list-returning tools accept `limit`; large result sets return a `next_cursor`.

### 9.3 Server implementation sketch (Python MCP SDK)

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("repo-atlas", instructions=INSTRUCTIONS)

@mcp.tool()
def get_context(task: str, budget: int = 4000,
                focus_files: list[str] | None = None, fresh: bool = True) -> str:
    """Return a ranked, token-budgeted map of the code most relevant to `task`,
    with line ranges to read next."""
    engine = get_engine()
    if fresh:
        engine.ensure_fresh(timeout_s=2.0)
    budget = max(200, min(budget, 16_000))
    return engine.get_context(task, budget, focus_files or [])

@mcp.tool()
def impact_of(target: str, depth: int = 3, min_confidence: float = 0.3) -> str:
    """List symbols and files that depend on `target`, plus tests likely to be affected."""
    engine = get_engine()
    engine.ensure_fresh(timeout_s=2.0)
    return engine.impact_of(target, min(depth, 6), min_confidence)

if __name__ == "__main__":
    mcp.run()   # stdio transport by default
```

### 9.4 Resources and prompts (optional)

- **Resource** `ratlas://map`: the default repo map as a readable resource for clients that support attaching resources.
- **Prompt** `ratlas-onboard`: a canned prompt that calls `repo_map` then summarizes architecture.

### 9.5 Output contract

- Responses are plain text optimized for model consumption, with a one-line header carrying `generation`, token usage and a `stale` flag.
- If the freshness barrier times out, the response still returns with `stale=true` and `pending_files=N` in the header so the assistant can decide to read those files directly.
- Errors use MCP's tool-error mechanism with an actionable message (e.g. `symbol not found; did you mean: ...` with up to 5 fuzzy suggestions).

---

## 10. Storage abstraction and backends

The engine depends on a narrow interface, not on SQLite.

```python
from typing import Protocol, Iterable

class ReadTxn(Protocol):
    generation: int
    def get_file(self, path: str) -> "FileRow | None": ...
    def symbols_in_file(self, path: str) -> list["SymbolRow"]: ...
    def find_symbols(self, query: str, kind: str | None, limit: int) -> list["SymbolRow"]: ...
    def edges_out(self, symbol_id: int, kinds: set[str], min_conf: float) -> list["EdgeRow"]: ...
    def edges_in(self, symbol_id: int, kinds: set[str], min_conf: float) -> list["EdgeRow"]: ...
    def file_graph(self) -> Iterable[tuple[int, int, float]]: ...
    def reachable_in(self, symbol_id: int, max_depth: int,
                     kinds: set[str], min_conf: float) -> list[tuple["SymbolRow", int]]: ...
    def rank_scores(self) -> dict[int, float]: ...

class WriteTxn(ReadTxn, Protocol):
    def bump_generation(self) -> int: ...
    def upsert_file(self, path: str, content_hash: str, status: str, gen: int) -> int: ...
    def upsert_symbols(self, path: str, symbols: list, delta: "Delta") -> None: ...
    def replace_outgoing_edges(self, path: str, refs: list) -> None: ...
    def delete_file(self, path: str) -> None: ...
    def set_rank_scores(self, scores: dict[int, float], gen: int) -> None: ...

class Store(Protocol):
    def read_txn(self) -> ReadTxn: ...      # snapshot-consistent
    def write_txn(self) -> WriteTxn: ...    # single writer
```

### 10.1 SQLite (default)

- WAL mode: readers never block the writer and see a consistent snapshot.
- One writer connection; reader pool.
- `reachable_in` uses the recursive CTE from [Section 8.6](#86-impact-analysis).
- FTS5 for lexical search.
- Migrations tracked via `PRAGMA user_version`. Because the index is a *cache*, a failed or unknown migration falls back to "drop and rebuild."

### 10.2 FalkorDB (optional graph backend)

Maps naturally to a property graph:

```text
(:File {path, lang, content_hash})-[:DEFINES]->(:Symbol {qualname, kind, signature, sig_hash, body_hash, start_line, end_line})
(:Symbol)-[:CALLS {confidence, line}]->(:Symbol)
(:Symbol)-[:EXTENDS|IMPORTS|REFERENCES|TESTS {confidence}]->(:Symbol)
```

Impact query sketch (openCypher; verify syntax against the FalkorDB version you target):

```cypher
MATCH (t:Symbol {qualname: $qualname})<-[r:CALLS|EXTENDS|IMPORTS*1..3]-(d:Symbol)
RETURN DISTINCT d.qualname, d.file_path
```

Trade-offs: richer traversal and a good fit for demos and graph-native tooling; heavier to operate than an embedded SQLite file, so keep SQLite the default and make FalkorDB an adapter behind `Store`. Use a sidecar or the database's full-text index for lexical search.

### 10.3 Vector store (optional)

Only needed if the embeddings layer is enabled. Reuse the same abstraction pattern: `VectorStore.upsert(id, vector, input_hash)`; `sqlite-vec` or LanceDB are reasonable defaults. Embeddings are *derived artifacts* keyed by `input_hash` like everything else.

---

## 11. Consistency, concurrency and failure handling

**Single-writer, snapshot readers.** All mutations happen in one writer thread/process inside transactions. Readers open a read transaction and see one consistent generation; they never observe a half-applied changeset.

**Freshness barrier.** `ensure_fresh(timeout)`:
1. Ask the watcher to flush its debounce buffer immediately.
2. Stat/hash the affected paths; enqueue a changeset.
3. Wait for the generation to advance, up to `timeout`.
4. On timeout, proceed with `stale=true` and report pending count.

This is what guarantees "I just edited the file, and the very next query sees it."

**Backpressure.** The changeset queue is bounded. Bursts (branch switch) are coalesced into one bulk changeset rather than queued file by file.

**Crash recovery.** Each changeset is atomic. On startup the reconcile pass compares `(size, mtime_ns)` then content hashes against `files`, and re-indexes any divergence. No write-ahead log of our own is needed.

**Idempotency.** Applying the same changeset twice produces the same generation content; hashes make no-ops cheap (an unchanged `content_hash` short-circuits before parsing).

**Corruption / schema drift.** `ratlas doctor` verifies foreign keys, checks `schema_version`, runs the **incremental-equals-full** check on a sample, and can `--repair` by rebuilding. Because the index is a cache, rebuilding is always a safe fallback.

**Git operations.** Detect `HEAD` change: bulk path for large diffs. Multiple worktrees of the same repo get separate indexes keyed by worktree path.

**Resource limits.** Per-file parse timeout and size cap; bounded worker pool; memory cap for the rank computation; skip-and-log (never crash) on pathological files such as minified bundles or giant generated code.

---

## 12. Security and privacy

| Concern | Mitigation |
|---|---|
| **Source code exfiltration** | Core product is local-only: no network calls, no telemetry by default. Optional metrics are opt-in and contain no code or paths. |
| **Secrets in signatures/docstrings** | Never index files matching `never_index` (`.env*`, `*.pem`, `id_rsa*`). Redact string-literal default arguments and high-entropy tokens in signatures via `redact_patterns` and an entropy detector. Never include bodies in default output. |
| **Prompt injection via repo content** | Docstrings and identifiers are *untrusted text* flowing into the model. Truncate docs to the first line, strip control characters and zero-width/bidi characters, never include comments, and render output inside clearly delimited blocks. Document that assistants should treat map text as data. |
| **Path traversal** | Canonicalize every path argument; reject anything resolving outside the repo root (including via symlinks). |
| **Tool abuse** | All tools are read-only. No shell execution, no file writes. |
| **HTTP transport (if enabled)** | Bind to loopback by default; require a bearer token; disable CORS. |
| **Supply chain** | Lockfile with hashes, minimal dependencies, SBOM generation in CI, dependency audit in CI. |
| **Multi-user machines** | Index directory is user-private (`0700`). |

---

## 13. Testing and evaluation

Two separate questions: *is the index correct?* and *does it actually save tokens?* Treat the second as the product's proof.

### 13.1 Correctness tests

| Layer | Technique | What it catches |
|---|---|---|
| Parser | Golden tests: fixture source file → expected symbols/refs (JSON snapshots) per language | Extraction regressions |
| Parser | Fuzz with truncated and syntactically broken variants of fixtures | Crashes, lost symbols on partial parse |
| Diff engine | Table-driven unit tests for every change kind, including ambiguous renames | Misclassification |
| Resolver | Hand-labelled resolution cases per tier; compare against SCIP output on sample repos | Wrong edges, confidence calibration |
| Store | Property tests (Hypothesis): arbitrary sequences of upserts/deletes preserve FK integrity and stable ids | Orphaned edges, id churn |
| **System** | **Incremental == full** (below) | The central invariant |

**The incremental-equals-full test (most important).** Take a real repository with history. For each of the last *N* commits (e.g. 200):

1. Check out commit *k*; apply the diff from *k-1* to the index **incrementally**.
2. Build a fresh index of commit *k* from scratch in a temp DB.
3. Compare the two canonicalized graphs (symbols by `(path, qualname, sig_hash, body_hash)`, edges by `(src, kind, dst, confidence)`).
4. Any difference fails the test and prints a minimal diff.

Replaying real history exercises renames, moves, mass refactors and merges far better than synthetic edits. Run a short version on every PR and a long version nightly.

Also test **mid-edit states**: replay each commit's diff as a sequence of partial hunks (file saved with only some hunks applied) to ensure partial-parse handling never corrupts the graph.

### 13.2 Index quality metrics

- **Symbol recall/precision** against `ctags` or SCIP on sample repos.
- **Edge precision/recall** against SCIP (the ground truth for Python/TS/Java).
- **Update latency:** p50/p95/p99 for 1-file, 10-file and 1,000-file changesets.
- **Cold index time** and peak memory per 100k LOC.

*Targets (to validate, not claims):* single-file update p95 < 150 ms on a ~200k LOC repo; cold index of 200k LOC in under ~60 s on a modern laptop with a process pool.

### 13.3 Token-savings benchmark (the proof)

**Question:** with RepoAtlas available, does an assistant complete the same tasks with fewer tokens, fewer turns, lower cost, and no loss in success rate?

**Design**

- **Repos:** 3 per language, small (<20k LOC), medium (20-200k), large (>200k).
- **Tasks:** ~30 per language with an automated pass/fail check (tests). Sources: a subset of SWE-bench Lite for Python; self-authored issues on open-source repos for TypeScript/Java.
- **Arms:**
  - A: baseline (assistant's default tools)
  - B: + `repo_map` only
  - C: + full toolset (`get_context`, `find_symbol`, `impact_of`, ...)
- **Repetitions:** at least 3 runs per (task, arm) because LLM behaviour is nondeterministic.
- **Harness:** drive the assistant headlessly (e.g. an agent loop on the API, or the assistant's non-interactive CLI mode with an MCP config; check current docs for flags) and log every request's usage.

**Metrics**

| Metric | Why |
|---|---|
| Input tokens (uncached and cached, separately) | Prompt caching changes economics; report both |
| Output tokens | Completeness of cost |
| **Dollar cost** (token counts × price table) | The number users actually care about |
| Turns and tool calls | Exploration overhead |
| Wall time | Latency |
| Success@1 (tests pass) | Savings must not cost correctness |
| Files read; precision/recall vs files in the gold patch | Direct measure of exploration quality |

**Analysis:** paired comparison per task, bootstrap confidence intervals, report per repo size. Publish the harness and raw logs.

**Pitfalls to design around**

- Assistants already explore efficiently; gains may concentrate on medium/large repos and on tasks with unclear entry points. Report that honestly.
- If the assistant ignores the tool, savings are zero. Measure *tool-call adoption rate* and iterate on tool descriptions and the `instructions` string.
- Prompt caching can make repeated reads cheap; compare cost, not just tokens.

---

## 14. Repository layout and tech stack

### 14.1 Layout

```text
repo-atlas/
├── pyproject.toml
├── ratlas.toml.example
├── README.md
├── src/repo_atlas/
│   ├── __init__.py
│   ├── cli.py                    # Typer app: init/index/watch/serve/query/doctor/bench
│   ├── config.py                 # load + validate ratlas.toml (pydantic)
│   ├── models.py                 # Symbol, Ref, ParseResult, Delta, Change
│   ├── hashing.py                # blake2b helpers, token-stream hashing
│   ├── watcher/
│   │   ├── git_state.py          # git diff/ls-files wrappers
│   │   ├── fs_events.py          # watchdog integration + debounce
│   │   └── reconcile.py          # startup tree walk
│   ├── languages/
│   │   ├── base.py               # LanguagePlugin protocol
│   │   ├── python.py
│   │   ├── typescript.py
│   │   ├── java.py
│   │   └── queries/              # optional .scm tree-sitter queries
│   ├── indexer/
│   │   ├── pipeline.py           # apply_changeset, bulk path
│   │   ├── diff.py               # diff_symbols, match_moves
│   │   ├── resolver.py           # tiered resolution
│   │   ├── invalidate.py         # deps_hash, derived invalidation
│   │   └── rank_refresh.py
│   ├── store/
│   │   ├── base.py               # Store/ReadTxn/WriteTxn protocols
│   │   ├── sqlite/
│   │   │   ├── schema.sql
│   │   │   ├── migrations/
│   │   │   └── store.py
│   │   └── falkordb.py           # optional
│   ├── engine/
│   │   ├── planner.py            # seeds
│   │   ├── graph.py              # file graph construction
│   │   ├── rank.py               # PPR + scoring
│   │   ├── budget.py             # token counting + packing
│   │   ├── render.py             # text map rendering
│   │   └── impact.py
│   ├── server/
│   │   ├── mcp_server.py
│   │   └── daemon.py             # unix-socket daemon + proxy
│   └── optional/
│       ├── embeddings.py
│       └── summaries.py
├── bench/
│   ├── harness.py
│   ├── tasks/                    # task definitions + pass/fail checks
│   └── analysis.ipynb
├── tests/
│   ├── fixtures/                 # per-language sample sources + golden JSON
│   ├── test_parser_*.py
│   ├── test_diff.py
│   ├── test_resolver.py
│   ├── test_store_props.py
│   └── test_incremental_equals_full.py
└── docs/
```

### 14.2 Technology choices

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Mature tree-sitter bindings, official MCP SDK, fast iteration. Revisit Rust only if parse/index throughput becomes the bottleneck |
| Parsing | `tree-sitter` + per-language grammar packages | Incremental, error-tolerant, 100+ languages |
| MCP | official `mcp` Python SDK (FastMCP) | Standard protocol |
| Storage | SQLite (WAL, FTS5) | Zero-ops, embedded, transactional |
| Graph algos | `networkx` initially; SciPy sparse later | Simplicity first |
| Tokens | `tiktoken` (proxy) | Fast local estimate |
| Watching | `watchdog` | Cross-platform |
| Ignore rules | `pathspec` | gitignore semantics |
| CLI | `typer` | Typed, good help output |
| Config/validation | `pydantic` v2 + `tomllib` | Strict config |
| Testing | `pytest`, `hypothesis`, `pytest-benchmark` | Property tests + perf tracking |
| Quality | `ruff`, `mypy --strict` on core modules | Hygiene |

`pyproject.toml` skeleton:

```toml
[project]
name = "repo-atlas"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "tree-sitter>=0.23",
  "tree-sitter-python>=0.23",
  "mcp>=1.2",
  "networkx>=3.2",
  "tiktoken>=0.7",
  "watchdog>=4.0",
  "pathspec>=0.12",
  "typer>=0.12",
  "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["pytest", "hypothesis", "pytest-benchmark", "ruff", "mypy"]
ts = ["tree-sitter-typescript>=0.23"]
java = ["tree-sitter-java>=0.23"]

[project.scripts]
ratlas = "repo_atlas.cli:app"
```

> Pin exact versions in a lockfile. tree-sitter's Python API has changed across minor releases; the snippets below use node-walking (`child_by_field_name`, `.type`, byte offsets), which is stable across versions, instead of the query API.

---

## 15. Build guide (phased)

Effort estimates are rough, for one developer working part-time. Each phase has **acceptance criteria**; do not start the next phase until they pass.

### Phase 0: Walking skeleton (about 1-2 weeks)

**Goal:** one language (Python), full index, `repo_map` over MCP, and the benchmark harness. This phase answers "are the token savings real?" before you invest in the graph.

**Tasks**

1. Scaffold the package, CLI and config loading.
2. File enumeration: `git ls-files` + untracked, `.gitignore`, size cap, language detection by extension.
3. Python extractor (below).
4. SQLite schema and a minimal store (files, symbols).
5. A naive `repo_map`: rank files by symbol count and import count, render skeletons within a budget.
6. MCP server exposing `repo_map` and `index_status`.
7. Benchmark harness skeleton: run a fixed set of tasks with and without the MCP server; record tokens, turns, cost, success.

**Python extractor (reference sketch)**

```python
import hashlib
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY = Language(tspython.language())
_parser = Parser(PY)


def _h(s: str) -> str:
    return hashlib.blake2b(s.encode(), digest_size=8).hexdigest()


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", "replace")


def _token_hash(node, src: bytes) -> str:
    """Hash leaf tokens only: ignores whitespace and comments."""
    parts, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type == "comment":
            continue
        if n.child_count == 0:
            parts.append(_text(n, src))
        else:
            stack.extend(reversed(n.children))
    return _h("\x1f".join(parts))


def _docstring(body, src: bytes) -> str:
    if body is None or body.named_child_count == 0:
        return ""
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_child_count \
            and first.named_children[0].type == "string":
        raw = _text(first.named_children[0], src).strip("\"' \n")
        return raw.splitlines()[0] if raw else ""
    return ""


def extract(source: bytes):
    root = _parser.parse(source).root_node
    symbols: list[Symbol] = []
    refs: list[Ref] = []

    # synthetic module symbol
    symbols.append(Symbol("<module>", "<module>", "module", "", "", 1,
                          root.end_point[0] + 1, None, _h(""), _token_hash(root, source)))

    def collect_calls(node, owner: str):
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "call":
                fn = n.child_by_field_name("function")
                if fn is not None:
                    refs.append(Ref(owner, _text(fn, source), "call", n.start_point[0] + 1))
            stack.extend(n.children)

    def visit(node, scope: tuple[str, ...], in_class: bool):
        for child in node.children:
            target = child
            if child.type == "decorated_definition":
                target = child.child_by_field_name("definition") or child

            if target.type in ("import_statement", "import_from_statement"):
                refs.append(Ref("<module>", _text(target, source), "import",
                                target.start_point[0] + 1))
                continue

            if target.type not in ("function_definition", "class_definition"):
                visit(target, scope, in_class)       # descend into if/try/with blocks
                continue

            name_node = target.child_by_field_name("name")
            body = target.child_by_field_name("body")
            if name_node is None or body is None:
                continue

            name = _text(name_node, source)
            qual = ".".join((*scope, name))
            is_class = target.type == "class_definition"
            kind = "class" if is_class else ("method" if in_class else "function")
            sig = source[target.start_byte:body.start_byte].decode("utf8", "replace")
            sig = sig.strip().rstrip(":").strip()

            symbols.append(Symbol(
                qual, name, kind, sig, _docstring(body, source),
                target.start_point[0] + 1, target.end_point[0] + 1,
                ".".join(scope) or None, _h(sig),
                _token_hash(body, source),   # for classes: exclude nested defs (omitted here)
            ))

            if is_class:
                bases = target.child_by_field_name("superclasses")
                if bases is not None:
                    for b in bases.named_children:
                        refs.append(Ref(qual, _text(b, source), "extends",
                                        b.start_point[0] + 1))
                visit(body, (*scope, name), True)
            else:
                collect_calls(body, qual)
                visit(body, (*scope, name), False)   # nested defs

    visit(root, (), False)
    status = "partial" if root.has_error else "ok"
    return symbols, refs, status
```

Known simplifications in the sketch: calls inside nested functions are attributed to both the nested and enclosing function (the `edges` primary key de-duplicates identical rows but not different `src_symbol`s); class body hashes include nested methods; decorators are not part of signatures. Fix these in Phase 1.

**Acceptance criteria**

- Indexes a 50k-LOC Python repo; `repo_map(2000)` returns a map within 5% of the budget.
- Golden parser tests pass for a fixture set covering classes, nested functions, decorators, async defs, syntax errors.
- The benchmark harness runs end-to-end on at least 10 tasks for arms A and B and produces a results table. **Decision gate:** if arm B shows no improvement in cost or turns on any repo size, investigate why before continuing (tool adoption? map quality? task choice?).

### Phase 1: Graph and incremental updates (about 3-4 weeks)

**Goal:** the correctness core: stable symbol ids, edges, diffing, re-resolution, and the incremental-equals-full test.

**Tasks**

1. Add `edges`, `derived`, `rank_scores`, FTS tables and triggers ([Section 6](#6-data-model)).
2. Implement the tiered resolver tiers 1-4 for Python (import map, class/self scope, unique/ambiguous global).
3. Implement `diff_symbols` and `match_moves` ([Section 7.3](#73-diff-engine)).
4. Implement `apply_changeset` with stable-id upserts and wholesale outgoing-edge replacement.
5. Watcher: git state + fs events + debounce + startup reconcile.
6. Freshness barrier and generation counter.
7. `find_symbol`, `callers_of`, `callees_of`, `impact_of` tools (recursive CTE).
8. **Incremental-equals-full replay test** on at least two real repositories.
9. Fix the Phase-0 extractor simplifications (nested-call attribution, class body hash, decorators in signatures).

**Acceptance criteria**

- Incremental == full for 200 consecutive commits on two repos, zero diffs.
- Single-file update p95 under the target on a ~100k LOC repo.
- Mid-edit (partial hunk) replay never produces orphan edges or duplicate symbols.
- `impact_of` on a hand-checked set of 20 symbols matches expected dependents with at least 90% recall.

### Phase 2: Ranking, budgeting, `get_context` (about 2-3 weeks)

**Goal:** the primary product experience.

**Tasks**

1. Build the file graph with kind weights, damping and reverse edges ([Section 8.2](#82-the-graph-used-for-ranking)).
2. Query planner: lexical seeds from FTS5 (split camelCase/snake_case), recency seeds from git, explicit focus files.
3. PPR scoring and symbol scoring ([Section 8.3](#83-scoring)); background rank refresh with warm start.
4. Budgeter with increments and greedy packing; deterministic rendering with line ranges ([Sections 8.4-8.5](#84-budgeting-token-aware-packing)).
5. `get_context` tool; server `instructions`; adoption-friendly tool descriptions.
6. Run the full benchmark (arms A, B, C) and tune weights.

**Acceptance criteria**

- Output never exceeds the requested budget (by the tokenizer proxy) in a 1,000-query property test.
- Deterministic: same index generation + query + config gives byte-identical output.
- Benchmark: arm C is no worse than arm A on success@1 and shows a statistically supported reduction in cost or turns on medium/large repos (if not, publish the negative result and the analysis).

### Phase 3: Hardening and breadth (ongoing)

1. TypeScript and Java language plugins (reuse golden-test and replay infrastructure).
2. Daemon + thin proxy topology; multi-client safety.
3. Bulk path for branch switches; parallel parsing; large-repo profiling.
4. Optional precise edges via SCIP provider (tier 0).
5. Optional embeddings and summaries as derived artifacts with `input_hash` validity.
6. Packaging: `pipx install`, prebuilt wheels, `ratlas doctor`, docs site.
7. Optional FalkorDB backend adapter.
8. CI mode: `ratlas impact --diff origin/main...HEAD --format json` for affected-test selection.

### Build-time tips

- **Write the replay test before the resolver.** It will tell you immediately when incremental logic drifts.
- **Keep the parser pure.** All DB access lives in the pipeline; the parser stays trivially testable and parallelizable.
- **Log every classification decision** at debug level (`Change` kinds, rename matches, re-resolved edge counts). Index bugs are far easier to diagnose from decision logs than from final state.
- **Start with `confidence` as three buckets** (precise, scoped, guess) and refine only if the benchmark shows it matters.
- **Avoid LLM summaries until the benchmark says you need them.** They are the largest source of cost, staleness and nondeterminism.

---

## 16. Configuration reference

`ratlas.toml` at the repo root (commit it so the team shares include/exclude rules):

```toml
[index]
include         = ["src/**", "lib/**", "app/**"]
exclude         = ["**/node_modules/**", "**/dist/**", "**/build/**", "**/*.min.js", "**/generated/**", "**/vendor/**"]
max_file_bytes  = 1_000_000
languages       = ["python", "typescript", "java"]
parse_timeout_s = 5

[watch]
debounce_ms         = 300
bulk_threshold_pct  = 30        # % of files changed before using the bulk path

[resolve]
confidence_floor    = 0.15
precise_provider    = "none"    # none | scip

[invalidate]
depth               = 1         # dependency depth for deps_hash
derived_refresh     = "lazy"    # lazy | background

[rank]
alpha               = 0.85
weights             = { ppr = 0.55, lexical = 0.30, recency = 0.10, prior = 0.05 }
reverse_edge_weight = 0.3
refresh_debounce_s  = 2

[budget]
default_map_tokens     = 2000
default_context_tokens = 4000
max_tokens             = 16000
safety_factor          = 0.9
tokenizer              = "cl100k_base"

[store]
backend = "sqlite"              # sqlite | falkordb
path    = "~/.cache/ratlas"

[optional]
embeddings = false
summaries  = false

[privacy]
never_index     = [".env*", "**/*.pem", "**/id_rsa*", "**/*.p12", "**/secrets/**"]
redact_patterns = ["(?i)(api[_-]?key|token|secret|password)\\s*[:=]\\s*\\S+"]
telemetry       = false

[server]
transport = "stdio"             # stdio | http
http_bind = "127.0.0.1:7077"
```

---

## 17. CLI reference

| Command | Description |
|---|---|
| `ratlas init` | Create `ratlas.toml`, detect languages, add `.ratlas/` to `.gitignore` if in-repo storage is chosen |
| `ratlas index [--full]` | Build or reconcile the index; `--full` forces a rebuild |
| `ratlas watch` | Run the watcher + indexer in the foreground (embedded mode) |
| `ratlas daemon [start\|stop\|status]` | Manage the per-repo daemon |
| `ratlas serve` | Start the MCP server (stdio); spawns/attaches to the daemon |
| `ratlas query map --budget 2000 [--path src/]` | Print a repo map |
| `ratlas query context "task text" --budget 4000 [--focus a.py,b.py]` | Print task-focused context |
| `ratlas query symbol NAME` | Find definitions |
| `ratlas impact TARGET [--depth 3] [--format text\|json]` | Blast radius and tests |
| `ratlas impact --diff A...B --format json` | Affected symbols/tests for a git range (CI use) |
| `ratlas status` | Generation, counts, lag, parse failures |
| `ratlas doctor [--repair]` | Integrity checks, incremental-equals-full sample, rebuild |
| `ratlas bench run --tasks bench/tasks --arms A,B,C --repeat 3` | Run the token-savings benchmark |
| `ratlas bench report` | Aggregate results with confidence intervals |

---

## 18. Integrating with assistants

### Claude Code

```bash
claude mcp add repo-atlas -- ratlas serve
```

(Run from the repo root, or pass a root explicitly; confirm the exact flags against the current Claude Code docs.)

### Cursor (`.cursor/mcp.json`)

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

### Any MCP client

Configure a stdio server with command `ratlas` and args `["serve"]`.

### Make the assistant actually use it

Adoption is the hardest part of this product. Add a short rule to the project's assistant instructions file (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules`, etc.):

```markdown
## Codebase navigation
- At the start of a task, call `get_context(task, budget)` from the repo-atlas MCP server before exploring files.
- Use `find_symbol` for definitions instead of grepping the whole repo.
- Call `impact_of` before changing a public function, class or module signature.
- Read files using the line ranges returned by repo-atlas; avoid reading whole files unless necessary.
```

Track **tool-call adoption rate** in the benchmark and treat low adoption as a product bug, to be fixed with better tool descriptions and server `instructions`.

---

## 19. Observability

Expose metrics via `ratlas status` and optionally a local Prometheus endpoint.

| Metric | Meaning |
|---|---|
| `index_generation` | Current committed generation |
| `index_lag_ms` | Time from file event to committed generation |
| `changeset_files` / `changeset_duration_ms` | Size and time of each update (histograms) |
| `parse_failures_total{lang}` | Files that failed or only partially parsed |
| `edges_unresolved_ratio` | Share of references with no target (resolver quality signal) |
| `edges_by_confidence_bucket` | Distribution of resolution tiers |
| `query_duration_ms{tool}` | Latency per tool |
| `tokens_served{tool}` | Tokens returned to assistants |
| `tokens_naive_estimate{tool}` | What reading the touched files fully would have cost (savings proxy) |
| `freshness_timeouts_total` | Times the barrier gave up |
| `rank_refresh_duration_ms` | Background rank cost |

Structured JSON logs with a per-changeset ID; debug level includes every change classification and rename decision.

---

## 20. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Assistants don't call the tool | High | Savings are zero | Strong `instructions`, rule snippet, tool names that match intent, measure adoption |
| Savings smaller than expected (assistants already efficient) | Medium | Weak value proposition | Benchmark early (Phase 0 gate); target medium/large repos and vague-entry-point tasks; report honestly |
| Name resolution inaccurate in dynamic languages | High | Wrong impact results | Confidence scores, floor thresholds, optional SCIP tier, surface confidence in output |
| Index staleness visible to the assistant | Medium | Wrong answers | Freshness barrier, `stale` flag, content-hash validation |
| Incremental logic drift from full index | Medium | Silent corruption | Replay test in CI, `doctor` sampling, safe rebuild fallback |
| Each language is substantial work | High | Slow breadth | Plugin interface, shared test infrastructure, start with 1-2 languages |
| Large-repo performance | Medium | Poor UX | Process pool parsing, bulk path, warm-started rank, per-file limits |
| Competing built-in indexes in assistants/IDEs | High | Differentiation erodes | Tool-agnostic MCP, impact/test-selection features, CI mode, open benchmark |
| Prompt injection through repo text | Low-Medium | Model manipulation | Sanitize and truncate repo-derived text; read-only tools |
| Over-engineering before validation | Medium | Wasted effort | Phase gates; no LLM summaries/embeddings until benchmark justifies |

---

## 21. Roadmap

- **v0.1** (Phase 0): Python, `repo_map`, benchmark harness.
- **v0.2** (Phase 1): graph, incremental updates, `impact_of`, replay-test CI.
- **v0.3** (Phase 2): `get_context`, ranking, budgeting, published benchmark.
- **v0.4**: TypeScript + Java, daemon topology.
- **v0.5**: SCIP-backed precise edges, bulk path, large-repo tuning.
- **v1.0**: stable schema and MCP surface, packaging, docs site.

Beyond v1: PR-review mode (impact of a diff), affected-test selection in CI, VS Code extension, optional embeddings/summaries, FalkorDB backend, team-shared remote index, cross-repo graphs.

---

## 22. FAQ

**Why not just give the assistant the whole repo?**
It will not fit for real repos, and even when it does, long contexts cost more and degrade focus. The goal is the right few thousand tokens plus pointers to exact lines.

**Why not just use embeddings?**
Embeddings answer "what looks similar?" They do not answer "what calls this?" or "what breaks if this changes?" Structure comes first; embeddings are an optional derived layer.

**Why symbol-level diffing instead of line diffs?**
A line diff says *where text changed*. A symbol diff says *what changed semantically*: a signature change is potentially breaking; a body change is not. That distinction drives impact analysis and cheap invalidation.

**Why are LLM summaries optional?**
They are costly, nondeterministic and the most prone to staleness. Deterministic skeletons (signature + first doc line) capture most of the navigational value. Add summaries only if the benchmark shows a gain.

**What happens when code doesn't parse (mid-edit)?**
tree-sitter still returns a tree. We extract what we can, mark the file `partial`, and keep last-good symbols overlapping error regions so the graph does not flap.

**Is my code sent anywhere?**
No. The core product is local-only. Optional features (summaries via an LLM API, remote embeddings) are off by default and, if enabled, must be explicitly configured.

**How is this different from Aider's repo map?**
Same family of idea (tree-sitter symbols + graph ranking under a token budget). Differences here: persistent incremental index with symbol-level diffing and invalidation, change-impact and test-selection tools, an open MCP interface usable from any assistant, and a published benchmark.

**How do I know the incremental index is right?**
The replay test: apply real commit history incrementally and compare to a fresh full index at every commit. Any difference fails CI.

---

## 23. References

- tree-sitter documentation: https://tree-sitter.github.io/tree-sitter/
- Model Context Protocol: https://modelcontextprotocol.io
- Aider repository map write-up: https://aider.chat/docs/repomap.html
- SCIP (Sourcegraph code intelligence protocol): https://github.com/sourcegraph/scip
- Page, Brin et al., *The PageRank Citation Ranking* (1999); Haveliwala, *Topic-Sensitive PageRank* (2002), the basis for personalization
- SQLite FTS5: https://www.sqlite.org/fts5.html
- SWE-bench: https://www.swebench.com

---

## Contributing

1. Open an issue describing the change (new language, resolver tier, ranking signal).
2. Add golden fixtures for any parser change; add a replay-test case for any indexer change.
3. Run `ruff`, `mypy`, `pytest`, and the short replay test before submitting.

## License

Choose before first release (MIT or Apache-2.0 are typical for developer tooling).
