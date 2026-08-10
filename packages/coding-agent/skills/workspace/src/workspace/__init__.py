"""Verifier-friendly isolated Git workspaces for Oh My Prime."""

from __future__ import annotations
import builtins
import os

from ._manager import WorkspaceManager
from ._models import (
    FileChange,
    PromotionResult,
    Workspace,
    WorkspaceCommandError,
    WorkspaceDiff,
    WorkspaceDirtyError,
    WorkspaceError,
    WorkspaceStaleError,
    WorkspaceStateError,
    WorkspaceStatus,
)


_default_manager = WorkspaceManager()


async def fork(
    repo: str | os.PathLike[str] = ".",
    *,
    name: str | None = None,
    base: str = "HEAD",
) -> Workspace:
    """Create an isolated candidate from a clean Git worktree."""
    return await _default_manager.fork(repo, name=name, base=base)


async def get(target: str | Workspace, *, repo: str | os.PathLike[str] = ".") -> Workspace:
    """Reload a durable workspace handle."""
    return await _default_manager.get(target, repo=repo)


async def list(repo: str | os.PathLike[str] = ".") -> builtins.list[Workspace]:
    """List durable workspace records for a repository."""
    return await _default_manager.list(repo)


async def diff(target: str | Workspace, *, repo: str | os.PathLike[str] = ".") -> WorkspaceDiff:
    """Create an immutable candidate patch and path summary."""
    return await _default_manager.diff(target, repo=repo)


async def promote(
    target: str | Workspace,
    *,
    repo: str | os.PathLike[str] | None = None,
    expected_patch_sha256: str | None = None,
) -> PromotionResult:
    """Apply a candidate to its unchanged source, optionally hash-gated."""
    return await _default_manager.promote(
        target,
        repo=repo,
        expected_patch_sha256=expected_patch_sha256,
    )


async def discard(
    target: str | Workspace,
    *,
    repo: str | os.PathLike[str] = ".",
) -> Workspace:
    """Permanently remove a candidate worktree and retain its audit manifest."""
    return await _default_manager.discard(target, repo=repo)




__all__ = [
    "FileChange",
    "PromotionResult",
    "Workspace",
    "WorkspaceCommandError",
    "WorkspaceDiff",
    "WorkspaceDirtyError",
    "WorkspaceError",
    "WorkspaceManager",
    "WorkspaceStaleError",
    "WorkspaceStateError",
    "WorkspaceStatus",
    "diff",
    "discard",
    "fork",
    "get",
    "list",
    "promote",
]
