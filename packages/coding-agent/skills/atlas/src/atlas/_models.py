from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Language = Literal["python", "typescript", "tsx", "javascript", "jsx", "json", "other"]


class AtlasError(RuntimeError):
    """Base error for Code Atlas indexing and queries."""


class AtlasNotBuilt(AtlasError):
    """Raised when a repository has no Code Atlas index yet."""


class AtlasStale(AtlasError):
    """Raised when a current semantic graph is required but the index is stale."""


class CapsuleError(AtlasError):
    """Raised when context compilation or capsule validation fails."""


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


@dataclass(frozen=True, slots=True)
class GraphFreshness:
    fresh: bool
    indexed_head: str
    current_head: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextItem:
    source: str
    start_line: int
    end_line: int
    symbol_keys: tuple[str, ...]
    content_hash: str
    excerpt_hash: str
    updated_at: str
    reason: str
    relation: str
    content: str
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ExcludedContext:
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContextCapsule:
    schema_version: int
    id: str
    repo_root: Path
    task: str
    task_contract: str | dict[str, object] | list[object] | None
    roots: tuple[str, ...]
    items: tuple[ContextItem, ...]
    excluded: tuple[ExcludedContext, ...]
    unrelated_files: int
    token_budget: int
    estimated_tokens: int
    built_at: str
    head_commit: str
    graph_indexed_at: str

    def render(self) -> str:
        sections = [
            f"# Task\n\n{self.task}",
            (
                "# Acceptance contract\n\n"
                + (
                    self.task_contract
                    if isinstance(self.task_contract, str)
                    else json.dumps(self.task_contract, indent=2, sort_keys=True)
                )
                if self.task_contract is not None
                else "# Acceptance contract\n\nNot supplied."
            ),
            (
                "# Capsule metadata\n\n"
                f"- id: `{self.id}`\n"
                f"- indexed HEAD: `{self.head_commit}`\n"
                f"- token estimate: {self.estimated_tokens}/{self.token_budget}\n"
                f"- unrelated files excluded: {self.unrelated_files}"
            ),
        ]
        for item in self.items:
            suffix = Path(item.source).suffix.lstrip(".")
            fence = "````" if "```" in item.content else "```"
            sections.append(
                f"## {item.source}:{item.start_line}-{item.end_line}\n\n"
                f"Reason: {item.reason}. Relation: {item.relation}.  \n"
                f"Source hash: `{item.content_hash}`. Updated: `{item.updated_at}`.\n\n"
                f"{fence}{suffix}\n{item.content.rstrip()}\n{fence}"
            )
        if self.excluded:
            exclusions = "\n".join(
                f"- `{item.source}`: {item.reason}" for item in self.excluded
            )
            sections.append(f"# Explicit exclusions\n\n{exclusions}")
        return "\n\n".join(sections) + "\n"


@dataclass(frozen=True, slots=True)
class CapsuleFreshness:
    fresh: bool
    changed_files: tuple[str, ...]
    missing_files: tuple[str, ...]
    graph_changed: bool
