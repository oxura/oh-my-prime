"""Code Atlas semantic graph for precise repository context and impact queries."""

from __future__ import annotations

import os
from typing import Sequence

from ._graph import CodeAtlas
from ._models import (
    AtlasError,
    AtlasNotBuilt,
    BuildReport,
    Edge,
    FileNode,
    GraphStats,
    Symbol,
)


_default_atlas = CodeAtlas()


async def build(repo: str | os.PathLike[str] = ".") -> BuildReport:
    """Atomically rebuild the repository semantic graph."""
    return await _default_atlas.build(repo)


async def stats(repo: str | os.PathLike[str] = ".") -> GraphStats:
    """Return graph cardinalities and indexed revision."""
    return await _default_atlas.stats(repo)


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


__all__ = [
    "AtlasError",
    "AtlasNotBuilt",
    "BuildReport",
    "CodeAtlas",
    "Edge",
    "FileNode",
    "GraphStats",
    "Symbol",
    "build",
    "files",
    "outgoing",
    "references",
    "stats",
    "symbol",
    "symbols",
]
