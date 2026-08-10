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


class ImpactError(AtlasError):
    """Raised when a proposed change cannot be analyzed or attested."""


class ImpactStale(ImpactError):
    """Raised when an impact report no longer matches its repository snapshot."""


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


@dataclass(frozen=True, slots=True)
class ChangedRange:
    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    old_path: str | None
    status: Literal["modified", "added", "deleted", "renamed"]
    additions: int
    deletions: int
    ranges: tuple[ChangedRange, ...]
    base_hash: str | None


@dataclass(frozen=True, slots=True)
class ImpactSymbol:
    key: str
    file: str
    kind: str
    name: str
    qualified_name: str
    line: int
    exported: bool
    depth: int
    relation: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ImpactFile:
    path: str
    depth: int
    categories: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImpactReport:
    schema_version: int
    id: str
    repo_root: Path
    common_dir: Path
    indexed_head: str
    graph_indexed_at: str
    patch_sha256: str
    patch_path: Path
    changes: tuple[ChangedFile, ...]
    changed_symbols: tuple[ImpactSymbol, ...]
    impacted_symbols: tuple[ImpactSymbol, ...]
    impacted_files: tuple[ImpactFile, ...]
    tests: tuple[str, ...]
    docs: tuple[str, ...]
    configs: tuple[str, ...]
    migrations: tuple[str, ...]
    public_api_symbols: tuple[str, ...]
    unresolved_targets: tuple[str, ...]
    risk_score: int
    risk_level: Literal["low", "medium", "high", "critical"]
    limitations: tuple[str, ...]
    created_at: str

    def render(self) -> str:
        changed = "\n".join(
            f"- `{item.path}` ({item.status}, +{item.additions}/-{item.deletions})"
            for item in self.changes
        )
        dependents = "\n".join(
            f"- `{item.path}`: {', '.join(item.reasons)}"
            for item in self.impacted_files
        )
        public = ", ".join(f"`{name}`" for name in self.public_api_symbols) or "none"
        tests = ", ".join(f"`{path}`" for path in self.tests) or "none discovered"
        limitations = "\n".join(f"- {item}" for item in self.limitations) or "- none"
        return (
            f"# Impact report `{self.id}`\n\n"
            f"Risk: **{self.risk_level}** ({self.risk_score}/100)  \n"
            f"Indexed HEAD: `{self.indexed_head}`  \n"
            f"Patch SHA-256: `{self.patch_sha256}`\n\n"
            f"## Proposed changes\n\n{changed}\n\n"
            f"## Public symbols touched\n\n{public}\n\n"
            f"## Affected files\n\n{dependents}\n\n"
            f"## Discovered tests\n\n{tests}\n\n"
            f"## Limitations\n\n{limitations}\n"
        )


@dataclass(frozen=True, slots=True)
class ImpactFreshness:
    fresh: bool
    head_changed: bool
    graph_changed: bool
    changed_files: tuple[str, ...]
    patch_applies: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class ImpactApplyResult:
    report_id: str
    applied_files: tuple[str, ...]
    content_hashes: dict[str, str | None]
    applied_at: str
