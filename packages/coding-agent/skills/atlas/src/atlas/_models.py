from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


Language = Literal["python", "typescript", "tsx", "javascript", "jsx", "json", "other"]


class AtlasError(RuntimeError):
    """Base error for Code Atlas indexing and queries."""


class AtlasNotBuilt(AtlasError):
    """Raised when a repository has no Code Atlas index yet."""


@dataclass(frozen=True, slots=True)
class FileNode:
    id: int
    path: str
    language: Language
    content_hash: str
    size: int
    parse_status: str


@dataclass(frozen=True, slots=True)
class Symbol:
    id: int
    key: str
    file: str
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    exported: bool
    signature_hash: str


@dataclass(frozen=True, slots=True)
class Edge:
    id: int
    source_symbol_key: str | None
    source_file: str
    target_symbol_key: str | None
    target_name: str
    target_file: str | None
    kind: str
    line: int
    confidence: float


@dataclass(frozen=True, slots=True)
class BuildReport:
    repo_root: Path
    database_path: Path
    head_commit: str
    indexed_files: int
    changed_files: int
    removed_files: int
    symbols: int
    edges: int
    parser_diagnostics: int
    built_at: str


@dataclass(frozen=True, slots=True)
class GraphStats:
    files: int
    symbols: int
    edges: int
    languages: dict[str, int]
    head_commit: str
    indexed_at: str
