"""Code Atlas semantic graph for precise repository context and impact queries."""

from __future__ import annotations

import os
from collections.abc import Sequence

from ._context import ContextCompiler
from ._graph import CodeAtlas
from ._impact import ImpactAnalyzer
from ._models import (
    AtlasError,
    AtlasNotBuilt,
    AtlasStale,
    BuildReport,
    CapsuleError,
    CapsuleFreshness,
    ChangedFile,
    ChangedRange,
    ContextCapsule,
    ContextItem,
    Edge,
    ExcludedContext,
    FileNode,
    GraphFreshness,
    GraphStats,
    ImpactApplyResult,
    ImpactError,
    ImpactFile,
    ImpactFreshness,
    ImpactReport,
    ImpactStale,
    ImpactSymbol,
    Symbol,
)

_default_atlas = CodeAtlas()
_default_compiler = ContextCompiler(_default_atlas)
_default_impact = ImpactAnalyzer(_default_atlas)


async def build(repo: str | os.PathLike[str] = ".") -> BuildReport:
    """Atomically rebuild the repository semantic graph."""
    return await _default_atlas.build(repo)


async def stats(repo: str | os.PathLike[str] = ".") -> GraphStats:
    """Return graph cardinalities and indexed revision."""
    return await _default_atlas.stats(repo)


async def freshness(repo: str | os.PathLike[str] = ".") -> GraphFreshness:
    """Report whether the index still matches the current tracked worktree."""
    return await _default_atlas.freshness(repo)


async def files(
    query: str = "",
    *,
    language: str | None = None,
    limit: int = 50,
    repo: str | os.PathLike[str] = ".",
) -> list[FileNode]:
    """Search indexed files by path and optional language."""
    return await _default_atlas.files(query, language=language, limit=limit, repo=repo)


async def symbols(
    query: str = "",
    *,
    kind: str | None = None,
    limit: int = 50,
    repo: str | os.PathLike[str] = ".",
) -> list[Symbol]:
    """Search symbols by name, qualified name, or stable key."""
    return await _default_atlas.symbols(query, kind=kind, limit=limit, repo=repo)


async def symbol(query: str, *, repo: str | os.PathLike[str] = ".") -> Symbol:
    """Resolve one unambiguous symbol."""
    return await _default_atlas.symbol(query, repo=repo)


async def references(
    target: str | Symbol,
    *,
    kinds: Sequence[str] = ("references", "calls", "extends", "implements"),
    limit: int = 200,
    repo: str | os.PathLike[str] = ".",
) -> list[Edge]:
    """Return incoming semantic edges to a symbol."""
    return await _default_atlas.references(target, kinds=kinds, limit=limit, repo=repo)


async def outgoing(
    source: str | Symbol,
    *,
    kinds: Sequence[str] = (),
    limit: int = 200,
    repo: str | os.PathLike[str] = ".",
) -> list[Edge]:
    """Return outgoing semantic edges from a symbol."""
    return await _default_atlas.outgoing(source, kinds=kinds, limit=limit, repo=repo)


async def compile_context(
    task: str,
    *,
    contract: object | None = None,
    roots: Sequence[str] = (),
    paths: Sequence[str] = (),
    token_budget: int = 18_000,
    depth: int = 2,
    auto_refresh: bool = True,
    repo: str | os.PathLike[str] = ".",
) -> ContextCapsule:
    """Compile and persist a bounded, provenance-rich context capsule."""
    return await _default_compiler.compile(
        task,
        contract=contract,
        roots=roots,
        paths=paths,
        token_budget=token_budget,
        depth=depth,
        auto_refresh=auto_refresh,
        repo=repo,
    )


async def load_capsule(
    capsule_id: str,
    *,
    repo: str | os.PathLike[str] = ".",
) -> ContextCapsule:
    """Load a persisted context capsule."""
    return await _default_compiler.load(capsule_id, repo=repo)


async def capsule_freshness(capsule: ContextCapsule) -> CapsuleFreshness:
    """Report whether every source in a capsule still has its attested hash."""
    return await _default_compiler.freshness(capsule)


async def impact(
    proposed_diff: str,
    *,
    max_depth: int = 3,
    auto_refresh: bool = True,
    repo: str | os.PathLike[str] = ".",
) -> ImpactReport:
    """Analyze and attest the transitive static impact of a unified diff."""
    return await _default_impact.analyze(
        proposed_diff,
        max_depth=max_depth,
        auto_refresh=auto_refresh,
        repo=repo,
    )


async def load_impact(
    report_id: str,
    *,
    repo: str | os.PathLike[str] = ".",
) -> ImpactReport:
    """Load a persisted, attested impact report."""
    return await _default_impact.load(report_id, repo=repo)


async def impact_freshness(report: ImpactReport) -> ImpactFreshness:
    """Check the report's HEAD, graph snapshot, target hashes, and patch."""
    return await _default_impact.freshness(report)


async def require_fresh_impact(report: ImpactReport) -> ImpactReport:
    """Fail closed unless every impact precondition still holds."""
    return await _default_impact.require_fresh(report)


async def apply_impact(report: ImpactReport) -> ImpactApplyResult:
    """Apply an attested patch under a repository lock and final hash gate."""
    return await _default_impact.apply(report)


__all__ = [
    "AtlasError",
    "AtlasNotBuilt",
    "AtlasStale",
    "BuildReport",
    "CapsuleError",
    "CapsuleFreshness",
    "ChangedFile",
    "ChangedRange",
    "CodeAtlas",
    "ContextCapsule",
    "ContextCompiler",
    "ContextItem",
    "Edge",
    "ExcludedContext",
    "FileNode",
    "GraphFreshness",
    "GraphStats",
    "ImpactAnalyzer",
    "ImpactApplyResult",
    "ImpactError",
    "ImpactFile",
    "ImpactFreshness",
    "ImpactReport",
    "ImpactStale",
    "ImpactSymbol",
    "Symbol",
    "apply_impact",
    "build",
    "capsule_freshness",
    "compile_context",
    "files",
    "freshness",
    "impact",
    "impact_freshness",
    "load_capsule",
    "load_impact",
    "outgoing",
    "references",
    "require_fresh_impact",
    "stats",
    "symbol",
    "symbols",
]
