"""Deterministic lexical context ranking and conservative token budgeting."""

from collections import Counter, defaultdict
import math
from pathlib import Path
import re
import unicodedata

from repo_atlas.config import Config
from repo_atlas.store import SQLiteStore

_WORD = re.compile(r"[A-Za-z0-9_]+")


class ContextEngine:
    def __init__(self, config: Config, store: SQLiteStore) -> None:
        self.config = config
        self.store = store
    def _count(self, text: str) -> int:
        return (len(text.encode("utf-8")) + 2) // 3

    @staticmethod
    def _safe_text(value: str, limit: int = 500) -> str:
        value = "".join(
            character for character in value
            if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
        )
        return " ".join(value.split())[:limit]

    def _sanitize_result(self, result: dict[str, object]) -> dict[str, object]:
        sanitized: dict[str, object] = {}
        for key, value in result.items():
            if isinstance(value, str):
                for pattern in self.config.privacy.redact_patterns:
                    value = re.sub(pattern, "[REDACTED]", value)
                if key == "body":
                    value = "".join(
                        character for character in value
                        if character in "\n\t" or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
                    )[:20_000]
                else:
                    value = self._safe_text(value, 2000)
            sanitized[key] = value
        return sanitized

    @staticmethod
    def _personalized_rank(
        nodes: list[int], edges: list[tuple[int, int, str, float]], seeds: dict[int, float],
        damping: float = 0.85, iterations: int = 40,
    ) -> dict[int, float]:
        kind_weights = {"extends": 1.5, "calls": 1.0, "references": 0.7, "imports": 0.5, "tests": 0.3}
        pair_totals: Counter[tuple[int, int]] = Counter()
        pair_counts: Counter[tuple[int, int]] = Counter()
        for source, destination, kind, confidence in edges:
            if source == destination or kind not in kind_weights:
                continue
            pair = (source, destination)
            pair_totals[pair] += kind_weights[kind] * confidence
            pair_counts[pair] += 1
        adjacency: dict[int, dict[int, float]] = defaultdict(dict)
        for (source, destination), total in pair_totals.items():
            weight = total / math.sqrt(pair_counts[(source, destination)])
            adjacency[source][destination] = adjacency[source].get(destination, 0.0) + weight
            adjacency[destination][source] = adjacency[destination].get(source, 0.0) + weight * 0.3
        seed_total = sum(max(weight, 0.0) for weight in seeds.values())
        personalization = {
            node: (max(seeds.get(node, 0.0), 0.0) / seed_total if seed_total else 1.0 / max(len(nodes), 1))
            for node in nodes
        }
        scores = personalization.copy()
        for _ in range(iterations):
            updated = {node: (1 - damping) * personalization[node] for node in nodes}
            dangling = 0.0
            for source in nodes:
                targets = adjacency.get(source, {})
                total_weight = sum(targets.values())
                if not total_weight:
                    dangling += scores[source]
                    continue
                for destination, weight in targets.items():
                    updated[destination] += damping * scores[source] * weight / total_weight
            for node in nodes:
                updated[node] += damping * dangling * personalization[node]
            scores = updated
        return scores

    def _fit(self, text: str, budget: int) -> str:
        encoded = text.encode("utf-8")
        max_bytes = budget * 3
        if len(encoded) <= max_bytes:
            return text
        clipped = encoded[:max_bytes]
        while clipped:
            try:
                return clipped.decode("utf-8").rstrip()
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        return ""

    @staticmethod
    def _terms(task: str) -> list[str]:
        words: list[str] = []
        for raw in _WORD.findall(task):
            split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)
            words.extend(part.lower() for part in re.split(r"[_\s]+", split) if len(part) > 1)
        return list(dict.fromkeys(words))

    def render(self, task: str = "", budget: int | None = None,
               focus_files: list[str] | None = None) -> str:
        requested = budget if budget is not None else self.config.budget.default_context_tokens
        budget = max(0, min(requested, self.config.budget.max_tokens))
        if budget == 0:
            return ""
        focus = {path.replace("\\", "/") for path in (focus_files or [])}
        terms = self._terms(task)
        with self.store.connect() as connection:
            generation = int(connection.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0])
            rows = connection.execute(
                "SELECT f.id,f.path,s.name,s.qualname,s.kind,s.signature,s.doc,s.start_line,s.end_line,s.is_public "
                "FROM files f LEFT JOIN symbols s ON s.file_id=f.id ORDER BY f.path,s.start_line,s.qualname"
            ).fetchall()
            edge_rows = connection.execute(
                "SELECT source.file_id AS source_file,destination.file_id AS destination_file,e.kind,e.confidence "
                "FROM edges e JOIN symbols source ON source.id=e.src_symbol "
                "JOIN symbols destination ON destination.id=e.dst_symbol"
            ).fetchall()
            fts_scores: Counter[int] = Counter()
            for term in terms:
                matches = connection.execute(
                    "SELECT f.id,COUNT(*) AS score FROM symbol_fts JOIN symbols s ON s.id=symbol_fts.rowid "
                    "JOIN files f ON f.id=s.file_id WHERE symbol_fts MATCH ? GROUP BY f.id",
                    (f'"{term.replace(chr(34), "")}"*',),
                ).fetchall()
                for match in matches:
                    fts_scores[int(match["id"])] += int(match["score"])

        files: dict[int, dict[str, object]] = {}
        for row in rows:
            file_id = int(row["id"])
            item = files.setdefault(file_id, {"path": row["path"], "symbols": []})
            if row["qualname"] is not None:
                item["symbols"].append(row)
        ranked = sorted(
            files.items(), key=lambda pair: str(pair[1]["path"]),
        )
        focus_seeds = {
            file_id: 4.0 for file_id, info in files.items() if info["path"] in focus
        }
        lexical_seeds = {file_id: float(score) for file_id, score in fts_scores.items()}
        seeds = Counter(focus_seeds) + Counter(lexical_seeds)
        page_rank = self._personalized_rank(
            list(files),
            [(int(edge["source_file"]), int(edge["destination_file"]), str(edge["kind"]), float(edge["confidence"]))
             for edge in edge_rows],
            dict(seeds),
        )
        max_lexical = max(fts_scores.values(), default=0) or 1
        max_rank = max(page_rank.values(), default=0.0) or 1.0
        ranked.sort(key=lambda pair: (
            -(0.55 * page_rank.get(pair[0], 0.0) / max_rank
              + 0.30 * fts_scores[pair[0]] / max_lexical
              + 0.15 * (1.0 if pair[1]["path"] in focus else 0.0)),
            str(pair[1]["path"]),
        ))
        safe_task = self._safe_text(task, 160) or "repository overview"
        header = f"# repo-atlas map | generation {generation} | task: {safe_task}\n"
        output = header
        for _, file_data in ranked:
            path = self._safe_text(str(file_data["path"]), 300)
            symbols = list(file_data["symbols"])
            symbols.sort(key=lambda symbol: (-int(symbol["is_public"]), symbol["start_line"], symbol["qualname"]))
            lines = [f"{path}"]
            for symbol in symbols[:25]:
                signature = str(self._sanitize_result({"text": str(symbol["signature"])})["text"])
                doc = str(self._sanitize_result({"text": str(symbol["doc"])})["text"])
                line = f"  {signature or symbol['kind'] + ' ' + symbol['name']}  L{symbol['start_line']}-{symbol['end_line']}"
                if doc:
                    line += f"  | {doc[:120]}"
                lines.append(line)
            addition = "\n".join(lines) + "\n"
            if self._count(output + addition) > int(budget * self.config.budget.safety_factor):
                continue
            output += addition
        if output == header:
            output += "(No indexed files yet. Run `ratlas index`.)\n"
        return self._fit(output.rstrip(), budget)

    def find_symbol(self, query: str, limit: int = 10, kind: str | None = None) -> list[dict[str, object]]:
        limit = max(1, min(limit, 100))
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT f.path,s.qualname,s.name,s.kind,s.signature,s.start_line,s.end_line "
                "FROM symbols s JOIN files f ON f.id=s.file_id "
                "WHERE (lower(s.name) LIKE ? OR lower(s.qualname) LIKE ?) AND (? IS NULL OR s.kind=?) "
                "ORDER BY CASE WHEN lower(s.name)=? THEN 0 ELSE 1 END,f.path,s.start_line LIMIT ?",
                (f"%{query.lower()}%", f"%{query.lower()}%", kind, kind, query.lower(), limit),
            ).fetchall()
        return [self._sanitize_result(dict(row)) for row in rows]

    def get_symbol(self, qualname: str, file: str | None = None, include_body: bool = False,
                   max_body_lines: int = 80) -> dict[str, object] | None:
        query = "SELECT f.path,s.* FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.qualname=?"
        parameters: tuple[object, ...] = (qualname,)
        if file is not None:
            query += " AND f.path=?"
            parameters = (qualname, file.replace("\\", "/"))
        with self.store.connect() as connection:
            row = connection.execute(query + " ORDER BY f.path LIMIT 1", parameters).fetchone()
            if row is not None:
                edge_counts = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM edges WHERE dst_symbol=?) AS incoming, "
                    "(SELECT COUNT(*) FROM edges WHERE src_symbol=?) AS outgoing",
                    (row["id"], row["id"]),
                ).fetchone()
        if row is None:
            return None
        result = self._sanitize_result(dict(row))
        result["incoming_edges"] = int(edge_counts["incoming"])
        result["outgoing_edges"] = int(edge_counts["outgoing"])
        if include_body:
            relative = str(row["path"])
            source_path = (self.config.root / relative).resolve()
            if self.config.root not in source_path.parents:
                raise ValueError("indexed symbol path resolves outside the repository")
            source_lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(1, int(row["start_line"]))
            end = min(int(row["end_line"]), start + max(1, min(max_body_lines, 500)) - 1)
            body = "\n".join(source_lines[start - 1:end])
            result["body"] = self._sanitize_result({"body": body})["body"]
        return result

    def _related(self, target: str, direction: str, depth: int, limit: int,
                 min_confidence: float | None = None, file: str | None = None) -> list[dict[str, object]]:
        if direction not in {"in", "out"}:
            raise ValueError("direction must be 'in' or 'out'")
        target = target.replace("\\", "/")
        normalized_file = file.replace("\\", "/") if file else None
        confidence_floor = self.config.resolve.confidence_floor if min_confidence is None else min_confidence
        confidence_floor = max(0.0, min(1.0, confidence_floor))
        edge_join = "e.dst_symbol=w.sym" if direction == "in" else "e.src_symbol=w.sym"
        next_symbol = "e.src_symbol" if direction == "in" else "e.dst_symbol"
        with self.store.connect() as connection:
            if normalized_file:
                symbol_rows = connection.execute(
                    "SELECT s.id FROM symbols s JOIN files f ON f.id=s.file_id WHERE f.path=? AND s.qualname=?",
                    (normalized_file, target),
                ).fetchall()
            else:
                symbol_rows = connection.execute(
                    "SELECT s.id FROM symbols s JOIN files f ON f.id=s.file_id WHERE f.path=? OR s.qualname=?",
                    (target, target),
                ).fetchall()
            if not symbol_rows:
                return []
            targets = [int(row["id"]) for row in symbol_rows]
            placeholders = ",".join("?" for _ in targets)
            rows = connection.execute(
                f"WITH RECURSIVE walk(sym,depth,confidence) AS (SELECT id,0,1.0 FROM symbols WHERE id IN ({placeholders}) "
                f"UNION ALL SELECT {next_symbol},w.depth+1,MIN(w.confidence,e.confidence) "
                f"FROM edges e JOIN walk w ON {edge_join} WHERE w.depth<? AND e.{('src_symbol' if direction == 'in' else 'dst_symbol')} IS NOT NULL "
                "AND e.kind IN ('calls','extends','imports','references') AND e.confidence>=?) "
                "SELECT f.path,s.qualname,MIN(w.depth) AS depth,MAX(w.confidence) AS confidence "
                "FROM walk w JOIN symbols s ON s.id=w.sym JOIN files f ON f.id=s.file_id "
                "WHERE w.depth>0 GROUP BY s.id ORDER BY depth,confidence DESC,f.path LIMIT ?",
                (*targets, max(0, min(depth, 6)), confidence_floor, max(1, min(limit, 500))),
            ).fetchall()
        return [self._sanitize_result(dict(row)) for row in rows]

    def callers(self, target: str, depth: int = 1, limit: int = 25,
            min_confidence: float | None = None, file: str | None = None) -> list[dict[str, object]]:
        return self._related(target, "in", depth, limit, min_confidence, file)

    def callees(self, target: str, depth: int = 1, limit: int = 25,
            min_confidence: float | None = None, file: str | None = None) -> list[dict[str, object]]:
        return self._related(target, "out", depth, limit, min_confidence, file)

    def impact(self, target: str, depth: int = 3, limit: int = 50,
               min_confidence: float | None = None) -> list[dict[str, object]]:
        return self.callers(target, depth, limit, min_confidence)

    def status(self) -> dict[str, int]:
        return self.store.status()
