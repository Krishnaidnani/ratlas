"""Validated project configuration with conservative local-first defaults."""

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from typing import Any

_DEFAULT_INCLUDE = [
    "**/*.py", "*.py", "**/*.pyi", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx",
    "**/*.java",
]
_DEFAULT_EXCLUDE = [
    "**/.git/**", "**/.venv/**", "**/venv/**", "**/__pycache__/**",
    "**/build/**", "**/dist/**", "**/node_modules/**", "**/vendor/**",
]
_DEFAULT_NEVER_INDEX = [".env*", "**/*.pem", "**/id_rsa*", "**/*.p12", "**/secrets/**"]
_DEFAULT_REDACT_PATTERNS = [r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"]


@dataclass(frozen=True, slots=True)
class IndexConfig:
    include: tuple[str, ...] = tuple(_DEFAULT_INCLUDE)
    exclude: tuple[str, ...] = tuple(_DEFAULT_EXCLUDE)
    languages: tuple[str, ...] = ("python", "typescript", "java")
    max_file_bytes: int = 1_000_000
    bulk_threshold_pct: int = 30


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    default_map_tokens: int = 2000
    default_context_tokens: int = 4000
    max_tokens: int = 16000
    safety_factor: float = 0.9
    tokenizer: str = "cl100k_base"


@dataclass(frozen=True, slots=True)
class ResolveConfig:
    confidence_floor: float = 0.15
    precise_provider: str = "none"
    scip_mapping: Path | None = None


@dataclass(frozen=True, slots=True)
class PrivacyConfig:
    never_index: tuple[str, ...] = tuple(_DEFAULT_NEVER_INDEX)
    redact_patterns: tuple[str, ...] = tuple(_DEFAULT_REDACT_PATTERNS)


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    index: IndexConfig = field(default_factory=IndexConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    resolve: ResolveConfig = field(default_factory=ResolveConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    database: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.database or (self.root / ".ratlas" / "index.db")


def _positive_int(section: dict[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"configuration value {key!r} must be a positive integer")
    return value


def load_config(root: Path, config_path: Path | None = None) -> Config:
    root = root.resolve()
    path = config_path or root / "ratlas.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    index = data.get("index", {})
    budget = data.get("budget", {})
    store = data.get("store", {})
    resolve = data.get("resolve", {})
    privacy = data.get("privacy", {})
    if not all(isinstance(section, dict) for section in (index, budget, store, resolve, privacy)):
        raise ValueError("[index], [budget], [resolve], [privacy], and [store] must be TOML tables")

    include = index.get("include", _DEFAULT_INCLUDE)
    exclude = index.get("exclude", _DEFAULT_EXCLUDE)
    languages = index.get("languages", ["python", "typescript", "java"])
    if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
        raise ValueError("index.include must be an array of strings")
    if not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude):
        raise ValueError("index.exclude must be an array of strings")
    supported_languages = {"python", "typescript", "java"}
    if not isinstance(languages, list) or not all(item in supported_languages for item in languages):
        raise ValueError("index.languages must contain only python, typescript, and java")
    if not languages:
        raise ValueError("index.languages must contain at least one language")
    max_bytes = _positive_int(index, "max_file_bytes", 1_000_000)
    bulk_threshold = _positive_int(index, "bulk_threshold_pct", 30)
    if bulk_threshold > 100:
        raise ValueError("index.bulk_threshold_pct must be at most 100")
    max_tokens = _positive_int(budget, "max_tokens", 16_000)
    safety = budget.get("safety_factor", 0.9)
    if not isinstance(safety, (int, float)) or not 0 < safety <= 1:
        raise ValueError("budget.safety_factor must be greater than 0 and at most 1")
    confidence_floor = resolve.get("confidence_floor", 0.15)
    if not isinstance(confidence_floor, (int, float)) or not 0 <= confidence_floor <= 1:
        raise ValueError("resolve.confidence_floor must be between 0 and 1")
    precise_provider = resolve.get("precise_provider", "none")
    if precise_provider not in {"none", "scip"}:
        raise ValueError("resolve.precise_provider must be 'none' or 'scip'")
    scip_mapping_value = resolve.get("scip_mapping")
    if scip_mapping_value is not None and not isinstance(scip_mapping_value, str):
        raise ValueError("resolve.scip_mapping must be a path string")
    scip_mapping = Path(scip_mapping_value).expanduser() if scip_mapping_value else None
    if scip_mapping is not None and not scip_mapping.is_absolute():
        scip_mapping = root / scip_mapping
    if precise_provider == "scip" and (scip_mapping is None or not scip_mapping.is_file()):
        raise ValueError("resolve.scip_mapping must point to an existing normalized SCIP mapping JSON file")
    never_index = privacy.get("never_index", _DEFAULT_NEVER_INDEX)
    redact_patterns = privacy.get("redact_patterns", _DEFAULT_REDACT_PATTERNS)
    if not isinstance(never_index, list) or not all(isinstance(item, str) for item in never_index):
        raise ValueError("privacy.never_index must be an array of strings")
    if not isinstance(redact_patterns, list) or not all(isinstance(item, str) for item in redact_patterns):
        raise ValueError("privacy.redact_patterns must be an array of strings")
    try:
        for pattern in redact_patterns:
            re.compile(pattern)
    except re.error as error:
        raise ValueError(f"invalid privacy.redact_patterns expression: {error}") from error

    db_value = store.get("path")
    database = Path(db_value).expanduser() if isinstance(db_value, str) else None
    if database is not None and not database.is_absolute():
        database = root / database
    if database is not None and database.suffix.lower() != ".db":
        database = database / "index.db"
    return Config(
        root=root,
        index=IndexConfig(tuple(include), tuple(exclude), tuple(languages), max_bytes, bulk_threshold),
        budget=BudgetConfig(
            _positive_int(budget, "default_map_tokens", 2000),
            _positive_int(budget, "default_context_tokens", 4000),
            max_tokens,
            float(safety),
            str(budget.get("tokenizer", "cl100k_base")),
        ),
        resolve=ResolveConfig(float(confidence_floor), precise_provider, scip_mapping),
        privacy=PrivacyConfig(tuple(never_index), tuple(redact_patterns)),
        database=database,
    )