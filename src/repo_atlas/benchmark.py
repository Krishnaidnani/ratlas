"""Offline retrieval-quality benchmark for RepoAtlas task fixtures."""

from __future__ import annotations

import json
from pathlib import Path
import statistics
from typing import Any

from repo_atlas.engine import ContextEngine
from repo_atlas.indexer import Indexer
from repo_atlas.store import SQLiteStore


def _read_tasks(path: Path) -> list[dict[str, Any]]:
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in content.splitlines() if line.strip()]
    value = json.loads(content)
    tasks = value.get("tasks", value) if isinstance(value, dict) else value
    if not isinstance(tasks, list):
        raise ValueError("benchmark task file must contain a JSON array or {\"tasks\": [...]} object")
    return tasks


def _file_tokens(engine: ContextEngine, root: Path, paths: list[str]) -> int:
    total = 0
    for relative in paths:
        target = (root / relative).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            continue
        total += engine._count(target.read_text(encoding="utf-8", errors="replace"))
    return total


def run_benchmark(root: Path, store: SQLiteStore, indexer: Indexer, engine: ContextEngine,
                  tasks_path: Path, arms: tuple[str, ...] = ("A", "B", "C"), repeat: int = 1,
                  output: Path | None = None) -> dict[str, Any]:
    valid_arms = {"A", "B", "C"}
    if not arms or not set(arms) <= valid_arms:
        raise ValueError("benchmark arms must be selected from A, B, and C")
    if repeat < 1 or repeat > 100:
        raise ValueError("repeat must be between 1 and 100")
    indexer.index()
    records: list[dict[str, Any]] = []
    for task in _read_tasks(tasks_path):
        task_id = str(task.get("id", "task"))
        query = str(task.get("task", task.get("query", "")))
        focus = [str(path) for path in task.get("focus_files", [])]
        gold = {str(path).replace("\\", "/") for path in task.get("gold_files", [])}
        naive_paths = [str(path) for path in task.get("baseline_files", focus or sorted(gold))]
        for repetition in range(repeat):
            for arm in arms:
                if arm == "A":
                    rendered = "\n".join(naive_paths)
                    token_count = _file_tokens(engine, root, naive_paths)
                    surfaced = set(naive_paths)
                    tool_calls = 0
                else:
                    budget = int(task.get("budget", 4000))
                    rendered = engine.render(query if arm == "C" else "", budget, focus if arm == "C" else None)
                    token_count = engine._count(rendered)
                    surfaced = {
                        line.strip() for line in rendered.splitlines()
                        if line.strip().endswith((".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".java"))
                    }
                    tool_calls = 1
                recall = len(gold & surfaced) / len(gold) if gold else None
                records.append({
                    "task_id": task_id, "repetition": repetition, "arm": arm,
                    "estimated_tokens": token_count, "tool_calls": tool_calls,
                    "gold_file_recall": recall, "assistant_success": None,
                    "note": "Offline retrieval estimate; no assistant execution or pass/fail task check was run.",
                })
    report = {
        "schema_version": 1,
        "root": str(root.resolve()),
        "tasks_file": str(tasks_path.resolve()),
        "repetitions": repeat,
        "records": records,
        "summary": summarize_benchmark(records),
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def summarize_benchmark(records: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    summary: dict[str, dict[str, float | None]] = {}
    for arm in sorted({str(record["arm"]) for record in records}):
        matching = [record for record in records if record["arm"] == arm]
        recalls = [float(record["gold_file_recall"]) for record in matching if record["gold_file_recall"] is not None]
        summary[arm] = {
            "mean_estimated_tokens": statistics.mean(float(row["estimated_tokens"]) for row in matching),
            "mean_tool_calls": statistics.mean(float(row["tool_calls"]) for row in matching),
            "mean_gold_file_recall": statistics.mean(recalls) if recalls else None,
            "assistant_success_rate": None,
        }
    return summary


def load_benchmark(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("records"), list):
        raise ValueError("not a RepoAtlas benchmark report")
    return report
