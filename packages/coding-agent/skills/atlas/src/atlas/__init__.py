"""Code Atlas semantic graph for precise repository context and impact queries."""

from __future__ import annotations

import os
from collections.abc import Sequence

from ._context import ContextCompiler
from ._graph import CodeAtlas
from ._models import (
    AtlasError,
    AtlasNotBuilt,
    AtlasStale,
    BuildReport,
    CapsuleError,
    CapsuleFreshness,
    ContextCapsule,
    ContextItem,
    Edge,
    ExcludedContext,
    FileNode,
    GraphFreshness,
    GraphStats,
    Symbol,
)

_default_atlas = CodeAtlas()
_default_compiler = ContextCompiler(_default_atlas)


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


__all__ = [
    "AtlasError",
    "AtlasNotBuilt",
    "AtlasStale",
    "BuildReport",
    "CapsuleError",
    "CapsuleFreshness",
    "CodeAtlas",
    "ContextCapsule",
    "ContextCompiler",
    "ContextItem",
    "Edge",
    "ExcludedContext",
    "FileNode",
    "GraphFreshness",
    "GraphStats",
    "Symbol",
    "build",
    "capsule_freshness",
    "compile_context",
    "files",
    "freshness",
    "load_capsule",
    "outgoing",
    "references",
    "stats",
    "symbol",
    "symbols",
]
